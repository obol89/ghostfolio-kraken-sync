"""Pagination for the Kraken and Ghostfolio list endpoints.

These patch at the low seam (requests.*) because the offsets and page
boundaries are the thing under test.
"""

import kraken_fixtures as kf
import pytest
from conftest import FakeResponse

KRAKEN_TRADES_URL = "/0/private/TradesHistory"
KRAKEN_LEDGERS_URL = "/0/private/Ledgers"
GHOST_ACTIVITIES_URL = "/api/v1/activities"


def requested_offsets(http, fragment):
    return [int(body.get("ofs", 0)) for body in http.form_bodies(fragment)]


# ---------------------------------------------------------------------------
# fetch_all_trades
# ---------------------------------------------------------------------------

def test_single_page_makes_one_call(k2g, config, http, kraken_pages):
    page = kf.make_trade_page("A", 10)
    http.route(KRAKEN_TRADES_URL, kraken_pages([page], "trades"))

    trades = k2g.fetch_all_trades(config)

    assert len(trades) == 10
    assert http.count(KRAKEN_TRADES_URL) == 1


def test_full_pages_walk_the_offset(k2g, config, http, kraken_pages):
    pages = [kf.make_trade_page("A", 50), kf.make_trade_page("B", 50),
             kf.make_trade_page("C", 20)]
    http.route(KRAKEN_TRADES_URL, kraken_pages(pages, "trades"))

    trades = k2g.fetch_all_trades(config)

    assert len(trades) == 120
    assert requested_offsets(http, KRAKEN_TRADES_URL) == [0, 50, 100]


def test_short_pages_do_not_skip_entries(k2g, config, http, kraken_pages):
    """A page shorter than the assumed size must not advance the cursor past it.

    Advancing by a hardcoded 50 while the server returned 40 walked the offset
    0 -> 50 -> 100 and permanently lost 20 of the 120 entries.
    """
    pages = [kf.make_trade_page("A", 40), kf.make_trade_page("B", 40),
             kf.make_trade_page("C", 40)]
    http.route(KRAKEN_TRADES_URL, kraken_pages(pages, "trades"))

    trades = k2g.fetch_all_trades(config)

    assert len(trades) == 120
    assert requested_offsets(http, KRAKEN_TRADES_URL) == [0, 40, 80]


def test_empty_first_page_terminates(k2g, config, http, kraken_pages):
    http.route(KRAKEN_TRADES_URL, kraken_pages([], "trades", count=0))

    assert k2g.fetch_all_trades(config) == {}
    assert http.count(KRAKEN_TRADES_URL) == 1


def test_sync_since_is_sent_on_every_page(k2g, config, http, kraken_pages):
    config["sync_since_ts"] = 1704067200.0
    pages = [kf.make_trade_page("A", 50), kf.make_trade_page("B", 5)]
    http.route(KRAKEN_TRADES_URL, kraken_pages(pages, "trades"))

    k2g.fetch_all_trades(config)

    bodies = http.form_bodies(KRAKEN_TRADES_URL)
    assert len(bodies) == 2
    assert all(body["start"] == "1704067200" for body in bodies)


# ---------------------------------------------------------------------------
# fetch_ledger_entries
# ---------------------------------------------------------------------------

def test_ledger_type_is_omitted_when_not_given(k2g, config, http, kraken_pages):
    """A type=all pass sends no filter at all.

    `earn` is not a member of Kraken's REST type filter enum, so a filtered
    request cannot return Earn rewards. Fetching unfiltered and classifying
    locally is the only way to see them.
    """
    http.route(KRAKEN_LEDGERS_URL, kraken_pages([kf.make_ledger_page("A", 3)], "ledger"))

    k2g.fetch_ledger_entries(config)

    assert "type" not in http.form_bodies(KRAKEN_LEDGERS_URL)[0]


def test_end_window_is_sent_on_every_page(k2g, config, http, kraken_pages):
    pages = [kf.make_ledger_page("A", 50), kf.make_ledger_page("B", 5)]
    http.route(KRAKEN_LEDGERS_URL, kraken_pages(pages, "ledger"))

    k2g.fetch_ledger_entries(config, end=1750000000)

    bodies = http.form_bodies(KRAKEN_LEDGERS_URL)
    assert len(bodies) == 2
    assert all(body["end"] == "1750000000" for body in bodies)

def test_ledger_pagination_preserves_ledger_id_keys(k2g, config, http, kraken_pages):
    pages = [kf.make_ledger_page("A", 50), kf.make_ledger_page("B", 7)]
    http.route(KRAKEN_LEDGERS_URL, kraken_pages(pages, "ledger"))

    entries = k2g.fetch_ledger_entries(config, "staking")

    assert len(entries) == 57
    assert all(key.startswith("L") for key in entries)
    # The map key is the ledger id, never the shared refid.
    assert len({entry["refid"] for entry in entries.values()}) == 57


def test_ledger_type_filter_is_sent(k2g, config, http, kraken_pages):
    http.route(KRAKEN_LEDGERS_URL, kraken_pages([kf.make_ledger_page("A", 3)], "ledger"))

    k2g.fetch_ledger_entries(config, "staking")

    assert http.form_bodies(KRAKEN_LEDGERS_URL)[0]["type"] == "staking"


def test_ledger_short_pages_do_not_skip_entries(k2g, config, http, kraken_pages):
    pages = [kf.make_ledger_page("A", 40), kf.make_ledger_page("B", 40),
             kf.make_ledger_page("C", 40)]
    http.route(KRAKEN_LEDGERS_URL, kraken_pages(pages, "ledger"))

    assert len(k2g.fetch_ledger_entries(config, "all")) == 120


def test_inconsistent_count_hits_the_page_backstop(k2g, config, http, monkeypatch):
    """A count the server never satisfies must stop, not spin."""
    monkeypatch.setattr(k2g, "MAX_KRAKEN_PAGES", 5)

    def always_one_new_entry(method, url, kwargs):
        offset = int((kwargs.get("data") or {}).get("ofs", 0))
        entry = kf.make_ledger_page("Z%d" % offset, 1)
        return FakeResponse({"error": [], "result": {"ledger": entry, "count": 999}})

    http.route(KRAKEN_LEDGERS_URL, always_one_new_entry)

    entries = k2g.fetch_ledger_entries(config, "all")

    assert len(entries) == 5
    assert http.count(KRAKEN_LEDGERS_URL) == 5
    assert any("inconsistent" in item for item in k2g.FAILURES)


# ---------------------------------------------------------------------------
# ghost_fetch_all_activities
# ---------------------------------------------------------------------------

def test_ghost_activities_skip_take_pagination(k2g, config, http):
    activities = [{"id": "a%d" % index, "comment": None} for index in range(1037)]

    def handler(method, url, kwargs):
        params = kwargs.get("params") or {}
        skip = int(params["skip"])
        take = int(params["take"])
        return FakeResponse({"activities": activities[skip:skip + take],
                             "count": len(activities)})

    http.route(GHOST_ACTIVITIES_URL, handler)

    collected = k2g.ghost_fetch_all_activities(config)

    assert len(collected) == 1037
    params = [call[2]["params"] for call in http.matching(GHOST_ACTIVITIES_URL)]
    assert [item["skip"] for item in params] == [0, 500, 1000]
    assert all(item["take"] == k2g.GHOST_PAGE_SIZE for item in params)


def test_ghost_activities_missing_key_yields_empty_list(k2g, config, http):
    http.route(GHOST_ACTIVITIES_URL, {"count": 0})

    assert k2g.ghost_fetch_all_activities(config) == []


@pytest.mark.parametrize("payload", [
    {"activities": [], "count": 0},
    {"activities": None, "count": 0},
])
def test_ghost_activities_empty_payloads(k2g, config, http, payload):
    http.route(GHOST_ACTIVITIES_URL, payload)

    assert k2g.ghost_fetch_all_activities(config) == []
