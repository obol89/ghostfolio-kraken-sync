"""Signing, nonce generation and transient-failure retry for kraken_request."""

import base64
import hashlib
import hmac
import urllib.parse

import pytest
import requests
from conftest import FakeResponse

BALANCE_URL = "/0/private/Balance"


@pytest.fixture(autouse=True)
def no_backoff_sleep(k2g, monkeypatch):
    """Retry immediately; the delay schedule is asserted separately."""
    monkeypatch.setattr(k2g, "KRAKEN_BACKOFF_BASE", 0.0)


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def test_signature_matches_krakens_documented_scheme(k2g, config):
    data = {"ofs": 0, "nonce": "1700000000000"}
    secret = config["kraken_api_secret"]

    post_data = urllib.parse.urlencode(data)
    message = BALANCE_URL.encode() + hashlib.sha256(
        (data["nonce"] + post_data).encode()).digest()
    expected = base64.b64encode(
        hmac.new(base64.b64decode(secret), message, hashlib.sha512).digest()).decode()

    assert k2g.kraken_signature(BALANCE_URL, data, secret) == expected


def test_request_sends_key_and_signature_headers(k2g, config, http):
    http.route(BALANCE_URL, {"error": [], "result": {"ZUSD": "1.0"}})

    k2g.kraken_request(config, BALANCE_URL)

    headers = http.matching(BALANCE_URL)[0][2]["headers"]
    assert headers["API-Key"] == config["kraken_api_key"]
    assert headers["API-Sign"]


def test_request_does_not_mutate_the_callers_data(k2g, config, http):
    """A retry must not resign a payload that already carries a stale nonce."""
    http.route(BALANCE_URL, {"error": [], "result": {}})
    data = {"ofs": 0}

    k2g.kraken_request(config, BALANCE_URL, data)

    assert data == {"ofs": 0}


# ---------------------------------------------------------------------------
# Nonce
# ---------------------------------------------------------------------------

def test_nonce_is_strictly_increasing(k2g):
    """Two calls inside one millisecond must not repeat a nonce.

    Kraken rejects a nonce that is not greater than the previous one with
    EAPI:Invalid nonce.
    """
    nonces = [int(k2g.kraken_nonce()) for _ in range(500)]
    assert nonces == sorted(nonces)
    assert len(set(nonces)) == len(nonces)


def test_nonce_survives_a_clock_that_does_not_advance(k2g, monkeypatch):
    monkeypatch.setattr(k2g.time, "time", lambda: 1700000000.0)

    first, second, third = (int(k2g.kraken_nonce()) for _ in range(3))

    assert first < second < third


# ---------------------------------------------------------------------------
# Errors and retry
# ---------------------------------------------------------------------------

def test_permanent_error_is_raised_without_retrying(k2g, config, http):
    http.route(BALANCE_URL, {"error": ["EGeneral:Permission denied"], "result": {}})

    with pytest.raises(RuntimeError, match="Permission denied"):
        k2g.kraken_request(config, BALANCE_URL)

    assert http.count(BALANCE_URL) == 1


@pytest.mark.parametrize("error", [
    "EAPI:Rate limit exceeded",
    "EService:Unavailable",
    "EService:Busy",
    "EGeneral:Temporary lockout",
])
def test_transient_errors_are_retried_then_succeed(k2g, config, http, error):
    responses = [
        FakeResponse({"error": [error], "result": {}}),
        FakeResponse({"error": [], "result": {"ZUSD": "5.0"}}),
    ]
    http.route(BALANCE_URL, lambda method, url, kwargs: responses.pop(0))

    assert k2g.kraken_request(config, BALANCE_URL) == {"ZUSD": "5.0"}
    assert http.count(BALANCE_URL) == 2


def test_transient_error_gives_up_after_the_attempt_limit(k2g, config, http):
    http.route(BALANCE_URL, {"error": ["EAPI:Rate limit exceeded"], "result": {}})

    with pytest.raises(k2g.KrakenTransientError):
        k2g.kraken_request(config, BALANCE_URL)

    assert http.count(BALANCE_URL) == k2g.KRAKEN_MAX_ATTEMPTS


def test_server_error_is_retried(k2g, config, http):
    responses = [
        FakeResponse({}, status_code=503),
        FakeResponse({"error": [], "result": {"ZUSD": "5.0"}}),
    ]
    http.route(BALANCE_URL, lambda method, url, kwargs: responses.pop(0))

    assert k2g.kraken_request(config, BALANCE_URL) == {"ZUSD": "5.0"}
    assert http.count(BALANCE_URL) == 2


def test_client_error_is_not_retried(k2g, config, http):
    http.route(BALANCE_URL, FakeResponse({}, status_code=403))

    with pytest.raises(requests.HTTPError):
        k2g.kraken_request(config, BALANCE_URL)

    assert http.count(BALANCE_URL) == 1


def test_timeout_is_retried(k2g, config, http):
    calls = {"n": 0}

    def flaky(method, url, kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.Timeout("timed out")
        return FakeResponse({"error": [], "result": {"ok": True}})

    http.route(BALANCE_URL, flaky)

    assert k2g.kraken_request(config, BALANCE_URL) == {"ok": True}
    assert calls["n"] == 2


def test_each_retry_uses_a_fresh_nonce(k2g, config, http):
    responses = [
        FakeResponse({"error": ["EAPI:Rate limit exceeded"], "result": {}}),
        FakeResponse({"error": [], "result": {}}),
    ]
    http.route(BALANCE_URL, lambda method, url, kwargs: responses.pop(0))

    k2g.kraken_request(config, BALANCE_URL)

    nonces = [int(body["nonce"]) for body in http.form_bodies(BALANCE_URL)]
    assert nonces[1] > nonces[0]


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

def test_throttle_scales_with_the_endpoint_counter_cost(k2g, monkeypatch):
    slept = []
    monkeypatch.setattr(k2g.time, "sleep", slept.append)

    k2g.throttle({"api_call_delay": 1.5}, k2g.LEDGER_COUNTER_COST)

    assert slept == [3.0]


def test_throttle_does_not_sleep_when_disabled(k2g, monkeypatch):
    slept = []
    monkeypatch.setattr(k2g.time, "sleep", slept.append)

    k2g.throttle({"api_call_delay": 0.0}, k2g.LEDGER_COUNTER_COST)

    assert slept == []


# ---------------------------------------------------------------------------
# SYNC_SINCE cutoff warning
# ---------------------------------------------------------------------------

def test_sync_since_cutoff_warns_about_hidden_entries(k2g, config, http, caplog):
    config["sync_since_ts"] = 1704067200.0
    http.route("/0/private/Ledgers", {"error": [], "result": {"ledger": {}, "count": 412}})

    assert k2g.warn_about_sync_since_cutoff(config) == 412
    assert any("412 ledger entries" in record.message for record in caplog.records)


def test_no_cutoff_check_without_sync_since(k2g, config, http):
    assert k2g.warn_about_sync_since_cutoff(config) == 0
    assert http.calls == []


def test_cutoff_check_failure_is_not_fatal(k2g, config, http):
    config["sync_since_ts"] = 1704067200.0
    http.route("/0/private/Ledgers", {"error": ["EGeneral:Permission denied"], "result": {}})

    assert k2g.warn_about_sync_since_cutoff(config) == 0
    assert k2g.FAILURES == []
