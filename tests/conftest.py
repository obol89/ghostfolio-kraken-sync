"""Shared pytest fixtures.

Mocking policy - patch at one of two seams, and pick the higher one unless the
test genuinely needs the lower:

  high (default)  kraken_request, ghost_fetch_all_activities,
                  ghost_import_activities.  Fast, and free of nonce and
                  signature noise.  Use for conversion, classification and
                  reconcile behaviour.

  low             requests.get / requests.post / requests.put via FakeHTTP.
                  Use only for pagination, end-to-end main() runs, and the
                  "reconcile performs no writes" assertion, which needs the
                  call recorder.

Please do not convert the high-tier tests to the low tier: they would gain
nothing but HTTP plumbing.

No third-party mocking library is used.  All four Kraken endpoints funnel
through kraken_request discriminated solely by url_path, so a small URL router
covers every call site, and a hand-rolled fake is what makes the call recorder
possible.
"""

import base64
import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def k2g():
    """The module under test.

    Imported normally rather than through importlib so that string-target
    patching - mock.patch("kraken_to_ghostfolio.requests.post") - resolves.
    """
    import kraken_to_ghostfolio

    return kraken_to_ghostfolio


@pytest.fixture(autouse=True)
def reset_failures(k2g):
    """Clear the module-global FAILURES before and after every test.

    Cleared in place rather than rebound, so any reference captured elsewhere
    still sees the same list.  fail() appends to this global and main() exits
    non-zero while it is non-empty, so leakage between tests would silently
    corrupt every exit-code assertion downstream.
    """
    k2g.FAILURES.clear()
    yield
    k2g.FAILURES.clear()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove every environment variable load_config() reads.

    load_config() reads os.environ directly, so a developer with real
    credentials exported would diverge from CI and, in the worst case, reach a
    live Ghostfolio instance.
    """
    for name in (
        "KRAKEN_API_KEY",
        "KRAKEN_API_SECRET",
        "GHOST_TOKEN",
        "GHOST_HOST",
        "GHOST_CURRENCY",
        "GHOST_PLATFORM_ID",
        "GHOST_ACCOUNT_NAME",
        "MAPPING_FILE",
        "SKIP_CRYPTO_TRANSFERS",
        "API_CALL_DELAY",
        "SYNC_SINCE",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TEST_GHOST_HOST = "http://ghostfolio.test:3333"


@pytest.fixture
def config():
    """A config dict shaped exactly as load_config() returns one.

    api_call_delay is 0.0 so the pagination loops do not really sleep, which
    removes any need to patch time.sleep.
    """
    return {
        "kraken_api_key": "TESTKEY",
        "kraken_api_secret": base64.b64encode(b"test-secret-bytes").decode(),
        "ghost_token": "test-token",
        "ghost_host": TEST_GHOST_HOST,
        "ghost_currency": "USD",
        "ghost_platform_id": "",
        "ghost_account_name": "Kraken",
        "mapping_file": "/nonexistent/mapping.yaml",
        "skip_crypto_transfers": True,
        "api_call_delay": 0.0,
        "sync_since_ts": None,
    }


@pytest.fixture
def env(monkeypatch, config):
    """Export the config fixture as environment variables for load_config()."""
    monkeypatch.setenv("KRAKEN_API_KEY", config["kraken_api_key"])
    monkeypatch.setenv("KRAKEN_API_SECRET", config["kraken_api_secret"])
    monkeypatch.setenv("GHOST_TOKEN", config["ghost_token"])
    monkeypatch.setenv("GHOST_HOST", config["ghost_host"])
    monkeypatch.setenv("API_CALL_DELAY", "0.0")
    monkeypatch.setenv("MAPPING_FILE", config["mapping_file"])
    return config


# ---------------------------------------------------------------------------
# HTTP fakes (low tier)
# ---------------------------------------------------------------------------

class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload=None, status_code=200, text=None):
        self._payload = {} if payload is None else payload
        self.status_code = status_code
        self.text = json.dumps(self._payload) if text is None else text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("HTTP %d" % self.status_code, response=self)


class FakeHTTP:
    """URL-routed fake for requests.get/post/put that records every call.

    Handlers are matched by substring against the URL, longest fragment first,
    so "/api/v1/account/" can be registered alongside "/api/v1/account".  Each
    handler takes (method, url, kwargs) and returns a FakeResponse.

    The recorder is the point: it is what lets a test assert that reconcile
    issued no writes, or that a second sync run POSTed nothing.
    """

    def __init__(self):
        self.calls = []
        self._routes = []

    def route(self, fragment, handler):
        """Register a handler.

        Accepts a callable (method, url, kwargs) -> FakeResponse, a ready-made
        FakeResponse to return every time, or a plain payload to serve as 200
        JSON every time.
        """
        if isinstance(handler, FakeResponse):
            response = handler
            handler = lambda method, url, kwargs: response  # noqa: E731
        elif not callable(handler):
            payload = handler
            handler = lambda method, url, kwargs: FakeResponse(payload)  # noqa: E731
        self._routes.append((fragment, handler))
        self._routes.sort(key=lambda item: len(item[0]), reverse=True)
        return self

    def _dispatch(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for fragment, handler in self._routes:
            if fragment in url:
                return handler(method, url, kwargs)
        raise AssertionError("No FakeHTTP route registered for %s %s" % (method, url))

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._dispatch("PUT", url, **kwargs)

    # -- assertions helpers -------------------------------------------------

    def count(self, fragment, method=None):
        """How many recorded calls contain `fragment` in their URL."""
        return len(self.matching(fragment, method))

    def matching(self, fragment, method=None):
        return [
            call for call in self.calls
            if fragment in call[1] and (method is None or call[0] == method)
        ]

    def to_host(self, host):
        """Every call whose URL targets `host`, regardless of method.

        Keyed on host rather than method on purpose: Kraken calls are POSTs
        too, so "no writes" cannot be expressed as "no POSTs".
        """
        return [call for call in self.calls if call[1].startswith(host)]

    def json_bodies(self, fragment, method=None):
        """The parsed json= payload of each matching call."""
        return [call[2].get("json") for call in self.matching(fragment, method)]

    def form_bodies(self, fragment, method=None):
        """The data= form payload of each matching call (Kraken private API)."""
        return [call[2].get("data") for call in self.matching(fragment, method)]


@pytest.fixture
def http(monkeypatch, k2g):
    """Patch requests.get/post/put inside the module under test."""
    fake = FakeHTTP()
    monkeypatch.setattr(k2g.requests, "get", fake.get)
    monkeypatch.setattr(k2g.requests, "post", fake.post)
    monkeypatch.setattr(k2g.requests, "put", fake.put)
    return fake


class FakeKraken:
    """Stateful fake for the Kraken private endpoints used by a sync.

    Ledgers are served per requested `type`, so the same instance answers a
    filtered pass and a type=all pass consistently.
    """

    def __init__(self, trades=None, ledgers=None, balances=None):
        self.trades = dict(trades or {})
        self.ledgers = {key: dict(value) for key, value in (ledgers or {}).items()}
        self.balances = dict(balances or {})

    def all_entries(self):
        merged = {}
        for entries in self.ledgers.values():
            merged.update(entries)
        return merged

    def entries_for(self, ledger_type):
        if ledger_type in (None, "", "all"):
            return self.all_entries()
        return self.ledgers.get(ledger_type, {})

    def install(self, http):
        http.route("/0/private/TradesHistory", self._trades)
        http.route("/0/private/Ledgers", self._ledgers)
        http.route("/0/private/Balance", self._balance)
        return self

    @staticmethod
    def _ok(result):
        return FakeResponse({"error": [], "result": result})

    def _trades(self, method, url, kwargs):
        offset = int((kwargs.get("data") or {}).get("ofs", 0))
        served = {} if offset else self.trades
        return self._ok({"trades": served, "count": len(self.trades)})

    def _ledgers(self, method, url, kwargs):
        data = kwargs.get("data") or {}
        entries = self.entries_for(data.get("type"))
        served = {} if int(data.get("ofs", 0)) else entries
        return self._ok({"ledger": served, "count": len(entries)})

    def _balance(self, method, url, kwargs):
        return self._ok(dict(self.balances))


class FakeGhostfolio:
    """Stateful fake for the Ghostfolio endpoints used by a sync.

    Imported activities are echoed back from GET /api/v1/activities, which is
    what makes the idempotency test meaningful: the second run sees its own
    comments and has nothing left to import.
    """

    def __init__(self, activities=(), accounts=None, host=TEST_GHOST_HOST):
        self.host = host
        self.activities = [dict(item) for item in activities]
        self.accounts = accounts
        self.imports = []
        self.import_statuses = []
        self.balance_updates = []
        self._next_id = 0

    def install(self, http):
        # Registered longest-fragment-first by FakeHTTP, so the by-id route
        # wins over the account list route.
        http.route("/api/v1/activities", self._activities)
        http.route("/api/v1/import", self._import)
        http.route("/api/v1/account/", self._account_by_id)
        http.route("/api/v1/account", self._accounts)
        return self

    @property
    def comments(self):
        return {item.get("comment") for item in self.activities if item.get("comment")}

    def _accounts(self, method, url, kwargs):
        import ghost_fixtures

        return FakeResponse(self.accounts or ghost_fixtures.GHOST_ACCOUNTS)

    def _account_by_id(self, method, url, kwargs):
        import ghost_fixtures

        if method == "PUT":
            self.balance_updates.append(kwargs.get("json"))
            return FakeResponse({})
        accounts = (self.accounts or ghost_fixtures.GHOST_ACCOUNTS)["accounts"]
        account_id = url.rsplit("/", 1)[-1]
        for account in accounts:
            if account["id"] == account_id:
                return FakeResponse(dict(account))
        return FakeResponse({}, status_code=404)

    def _activities(self, method, url, kwargs):
        import ghost_fixtures

        params = kwargs.get("params") or {}
        return FakeResponse(ghost_fixtures.activities_response(
            self.activities, int(params.get("skip", 0)), int(params.get("take", 500))))

    def _import(self, method, url, kwargs):
        import ghost_fixtures

        payload = (kwargs.get("json") or {}).get("activities", [])
        self.imports.append(payload)
        status = self.import_statuses.pop(0) if self.import_statuses else 200
        if status >= 400:
            return FakeResponse({"message": "rejected"}, status_code=status,
                                text='{"message":"rejected"}')
        for activity in payload:
            self._next_id += 1
            self.activities.append(ghost_fixtures.activity_from_import(
                activity, "fake-%04d" % self._next_id))
        return FakeResponse({"message": "ok"})


@pytest.fixture
def kraken():
    """Factory for a FakeKraken; call .install(http) to register its routes."""
    return FakeKraken


@pytest.fixture
def ghostfolio():
    """Factory for a FakeGhostfolio; call .install(http) to register routes."""
    return FakeGhostfolio


@pytest.fixture
def kraken_pages():
    """Build a Kraken responder that serves pages keyed by the requested `ofs`.

    Usage:
        http.route("/0/private/Ledgers", kraken_pages(pages, "ledger"))

    `pages` is a list of ledger-id-keyed (or txid-keyed) dicts.  Each page is
    registered at the cumulative offset where it really starts, so a client
    that computes the next offset wrongly is served an empty page and the test
    fails on the collected total - which is exactly the bug being guarded
    against.  An offset past the end yields an empty page, which is how the
    real endpoint terminates.
    """

    def build(pages, key, count=None):
        by_offset = {}
        offset = 0
        for page in pages:
            by_offset[offset] = page
            offset += len(page)
        total = offset if count is None else count

        def handler(method, url, kwargs):
            requested = int((kwargs.get("data") or {}).get("ofs", 0))
            served = by_offset.get(requested, {})
            return FakeResponse({"error": [], "result": {key: served, "count": total}})

        return handler

    return build
