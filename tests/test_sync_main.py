"""End-to-end main() runs against fake Kraken and Ghostfolio servers.

Patched at the low seam (requests.*) so the call recorder can assert what was
and was not written.
"""

import ghost_fixtures as gf
import kraken_fixtures as kf
import pytest

IMPORT_URL = "/api/v1/import"


GOOD_TRADE_ID = "TQXY7Z-4KLMN-8PQRST"
BLOCKED_TRADE_ID = "T7JHGF-2WERT-9ZXCVB"


@pytest.fixture
def servers(http, kraken, ghostfolio, env):
    """A Kraken with one importable trade and a mixed ledger, empty Ghostfolio."""
    api = kraken(
        trades={GOOD_TRADE_ID: dict(kf.KRAKEN_TRADES[GOOD_TRADE_ID])},
        ledgers={
            "staking": dict(kf.LEDGER_STAKING_REWARD_DOT),
            "earn": dict(kf.LEDGER_EARN_REWARD_BTC),
            "airdrop": dict(kf.LEDGER_AIRDROP_BABY),
            "transfer": dict(kf.LEDGER_TRANSFER_SPOT_TO_EARN),
            "bare_transfer": dict(kf.LEDGER_TRANSFER_BARE_NEGATIVE),
            "convert": {**kf.LEDGER_CONVERT_FIAT_TO_CRYPTO,
                        **kf.LEDGER_CONVERT_DUSTSWEEP},
            "deposit": dict(kf.LEDGER_DEPOSIT_BTC),
            "withdrawal": dict(kf.LEDGER_WITHDRAWAL_BTC),
        },
        balances=dict(kf.KRAKEN_BALANCES),
    ).install(http)
    ghost = ghostfolio().install(http)
    return api, ghost


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

def test_missing_env_returns_1(k2g, http):
    assert k2g.main([]) == 1


def test_invalid_sync_since_returns_1(k2g, http, env, monkeypatch):
    monkeypatch.setenv("SYNC_SINCE", "not-a-date")
    assert k2g.main([]) == 1


def test_unknown_account_returns_1(k2g, http, servers, monkeypatch):
    monkeypatch.setenv("GHOST_ACCOUNT_NAME", "Nonexistent")
    assert k2g.main([]) == 1


def test_unknown_flag_is_a_hard_error(k2g):
    """A stale SYNC_ARGS value must be loud, not silently ignored."""
    with pytest.raises(SystemExit):
        k2g.main(["--not-a-real-flag"])


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_imports_once_and_returns_0(k2g, servers):
    _, ghost = servers

    assert k2g.main([]) == 0

    assert len(ghost.imports) == 1
    comments = {item["comment"] for item in ghost.imports[0]}
    assert k2g.kraken_comment("TRADE", GOOD_TRADE_ID) in comments
    assert k2g.kraken_comment("REWARD", "L4UESK-KG3EQ-UFO4T5") in comments
    assert k2g.kraken_comment("REWARD", "LKJH2M-QW3RT-9ZXCVB") in comments
    assert k2g.kraken_comment("AIRDROP", "LB4BY9-XKQ2M-7NPRTV") in comments
    assert k2g.FAILURES == []


def test_rewards_are_imported_as_quantity_bearing_buys(k2g, servers):
    """End to end proof of the fix: the reward moves the position.

    Under the old INTEREST path these rows existed but Ghostfolio's getFactor
    scored them zero, so the holding stayed short by the whole reward history.
    """
    _, ghost = servers

    k2g.main([])

    rewards = [item for item in ghost.imports[0]
               if item["comment"].startswith("KRAKEN#REWARD#")]
    assert rewards
    for reward in rewards:
        assert reward["type"] == "BUY"
        assert reward["unitPrice"] == 0
        assert k2g.ghost_activity_factor(reward["type"]) == 1.0


def test_conversions_and_bare_transfers_are_imported(k2g, servers):
    _, ghost = servers

    k2g.main([])

    comments = {item["comment"] for item in ghost.imports[0]}
    # The priced leg of the instant buy, and all three dust-sweep legs.
    assert k2g.kraken_comment("CONVERT", "LCNVRC-XBT01-BBBBBB") in comments
    assert k2g.kraken_comment("CONVERT", "LDUSTS-TRX01-GGGGGG") in comments
    assert k2g.kraken_comment("CONVERT", "LDUSTR-XBT02-IIIIII") in comments
    # The fiat leg is cash, not a position.
    assert k2g.kraken_comment("CONVERT", "LCNVSP-CHF01-AAAAAA") not in comments
    assert k2g.kraken_comment("TRANSFER", "LTRFOU-6YHNM-7UJMIK") in comments


def test_instant_buy_keeps_its_real_cost_basis_end_to_end(k2g, servers):
    _, ghost = servers

    k2g.main([])

    priced = next(item for item in ghost.imports[0]
                  if item["comment"] == k2g.kraken_comment("CONVERT", "LCNVRC-XBT01-BBBBBB"))
    assert priced["currency"] == "CHF"
    assert priced["unitPrice"] == pytest.approx(1404.33 / 0.0252729)
    assert priced["type"] == "BUY"


def test_second_run_imports_no_conversions_or_transfers(k2g, servers, http):
    """Idempotency across the new KRAKEN#CONVERT# and KRAKEN#XFER# namespaces."""
    _, ghost = servers

    assert k2g.main([]) == 0
    k2g.FAILURES.clear()
    assert k2g.main([]) == 0

    assert len(ghost.imports) == 1
    assert http.count(IMPORT_URL) == 1


def test_trade_emits_the_base_from_trades_and_the_quote_from_the_ledger(k2g, servers):
    """One trade, two activities, on two different assets.

    The trade import covers BUY BTCUSD. Nothing but the ledger accounts for
    the USDC that paid for it, which is why the quote balance never fell.
    """
    api, ghost = servers
    api.ledgers["trade"] = dict(kf.LEDGER_TRADE_XBTUSDC)

    k2g.main([])

    indexed = {item["comment"]: item for item in ghost.imports[0]}
    base = indexed[k2g.kraken_comment("TRADE", GOOD_TRADE_ID)]
    quote = indexed[k2g.kraken_comment("TRADE_QUOTE", "LTRD5W-7ASDF-9GHJKL")]

    assert base["type"] == "BUY" and base["symbol"] == "BTCUSD"
    assert quote["type"] == "SELL" and quote["symbol"] == "USDCUSD"
    assert quote["quantity"] == pytest.approx(834.21 + 1.334736)
    # The base leg must not be imported twice.
    assert k2g.kraken_comment("TRADE_QUOTE", "LTRD4Q-6ZXWE-8CVBNM") not in indexed


@pytest.fixture
def base_fee_servers(http, kraken, ghostfolio, env):
    """Three trades whose fee Kraken charged in BTC, and nothing else."""
    api = kraken(
        trades=dict(kf.TRADES_WITH_BASE_FEES),
        ledgers={"trade": dict(kf.LEDGER_TRADE_BASE_FEES)},
        balances={"XXBT": "0.2140"},
    ).install(http)
    ghost = ghostfolio().install(http)
    return api, ghost


def test_base_asset_fees_are_corrected_end_to_end(k2g, base_fee_servers):
    """Each fee-bearing base leg yields exactly one correcting SELL."""
    _, ghost = base_fee_servers

    assert k2g.main([]) == 0

    corrections = [item for item in ghost.imports[0]
                   if item["comment"].startswith("KRAKEN#TRADEFEE#")]
    assert len(corrections) == 3
    assert all(item["type"] == "SELL" for item in corrections)
    assert sum(item["quantity"] for item in corrections) == pytest.approx(0.00002221)


def test_trades_are_still_imported_at_their_gross_volume(k2g, base_fee_servers):
    """The correction is additive; the trade activity is unchanged."""
    _, ghost = base_fee_servers

    k2g.main([])

    trade = next(item for item in ghost.imports[0]
                 if item["comment"] == k2g.kraken_comment("TRADE", "TBFEE01-AAAAAA-000001"))
    assert trade["quantity"] == pytest.approx(0.0035)


def test_a_second_run_adds_no_fee_corrections(k2g, base_fee_servers, http):
    """Idempotency across KRAKEN#TRADEFEE#."""
    _, ghost = base_fee_servers

    assert k2g.main([]) == 0
    k2g.FAILURES.clear()
    assert k2g.main([]) == 0

    assert len(ghost.imports) == 1
    assert http.count(IMPORT_URL) == 1


def test_fee_free_trades_produce_no_corrections(k2g, servers):
    """Regression guard for the common case, end to end."""
    api, ghost = servers
    api.ledgers["trade"] = dict(kf.LEDGER_TRADE_XBTUSDC)

    k2g.main([])

    assert not [item for item in ghost.imports[0]
                if item["comment"].startswith("KRAKEN#TRADEFEE#")]


def test_futures_transfers_are_imported(k2g, servers):
    api, ghost = servers
    api.ledgers["futures"] = dict(kf.LEDGER_TRANSFER_SPOT_FROM_FUTURES)

    k2g.main([])

    comments = {item["comment"] for item in ghost.imports[0]}
    assert k2g.kraken_comment("TRANSFER", "LFUTIN-3EDCV-4RFVBG") in comments


def test_internal_transfers_are_not_imported(k2g, servers):
    """Both legs of a spot-to-earn move are skipped, so they net to zero."""
    _, ghost = servers

    k2g.main([])

    comments = {item["comment"] for item in ghost.imports[0]}
    for ledger_id in kf.LEDGER_TRANSFER_SPOT_TO_EARN:
        for kind in ("REWARD", "AIRDROP", "DEPOSIT", "WITHDRAWAL"):
            assert k2g.kraken_comment(kind, ledger_id) not in comments


def test_a_non_iso4217_trade_is_isolated_not_fatal_to_the_batch(k2g, servers):
    """The failure mode that once lost a whole import batch.

    ETHXBT resolves to currency BTC, which Ghostfolio's DTO refuses. It is
    held back and reported while every other activity still lands.
    """
    api, ghost = servers
    api.trades[BLOCKED_TRADE_ID] = dict(kf.KRAKEN_TRADES[BLOCKED_TRADE_ID])

    assert k2g.main([]) == 1

    comments = {item["comment"] for item in ghost.imports[0]}
    assert k2g.kraken_comment("TRADE", GOOD_TRADE_ID) in comments
    assert k2g.kraken_comment("TRADE", BLOCKED_TRADE_ID) not in comments
    assert any(BLOCKED_TRADE_ID in item for item in k2g.FAILURES)


def test_unrecognised_ledger_entries_fail_the_run(k2g, servers, capsys):
    api, _ = servers
    api.ledgers["adjustment"] = dict(kf.LEDGER_ADJUSTMENT)

    assert k2g.main([]) == 1

    assert "Unrecognised Kraken ledger entries" in capsys.readouterr().out


def test_second_run_imports_nothing(k2g, servers, http):
    """Idempotency: the dedup comments come back and nothing is left to import.

    ghost_import_activities returns early on an empty list without issuing a
    POST, so a clean second run leaves the import count at one.
    """
    _, ghost = servers

    assert k2g.main([]) == 0
    first_import_count = http.count(IMPORT_URL)
    k2g.FAILURES.clear()

    assert k2g.main([]) == 0

    assert http.count(IMPORT_URL) == first_import_count == 1
    assert len(ghost.imports) == 1


def test_import_rejection_records_a_failure_and_returns_1(k2g, servers):
    _, ghost = servers
    ghost.import_statuses = [400]

    assert k2g.main([]) == 1
    assert any("Import batch" in item for item in k2g.FAILURES)


def test_cash_balance_update_still_runs_after_a_rejected_import(k2g, servers):
    _, ghost = servers
    ghost.import_statuses = [400]

    k2g.main([])

    assert len(ghost.balance_updates) == 1


# ---------------------------------------------------------------------------
# Cash balance
# ---------------------------------------------------------------------------

def test_cash_balance_uses_the_account_currency_not_ghost_currency(k2g, servers, monkeypatch):
    """Regression: a CHF account was overwritten with the owner's USD balance.

    The balance was summed against GHOST_CURRENCY, which defaults to USD,
    while the PUT declared the account's own currency. Nothing compared the
    two, so a CHF 196.75 balance became 11.98.
    """
    api, ghost = servers
    api.balances = {"CHF": "196.7500", "ZEUR": "50.0000", "ZUSD": "11.9800",
                    "XXBT": "0.1580"}
    ghost.accounts = {"accounts": [dict(gf.GHOST_ACCOUNTS["accounts"][0], currency="CHF")]}
    monkeypatch.setenv("GHOST_CURRENCY", "USD")

    k2g.main([])

    payload = ghost.balance_updates[-1]
    assert payload["currency"] == "CHF"
    assert payload["balance"] == pytest.approx(196.75)


def test_cash_balance_currency_mismatch_is_warned_about(k2g, servers, monkeypatch, caplog):
    api, ghost = servers
    api.balances = {"CHF": "196.7500", "ZUSD": "11.9800"}
    ghost.accounts = {"accounts": [dict(gf.GHOST_ACCOUNTS["accounts"][0], currency="CHF")]}
    monkeypatch.setenv("GHOST_CURRENCY", "USD")

    k2g.main([])

    assert any("GHOST_CURRENCY is USD" in record.message for record in caplog.records)


def test_cash_balance_sums_suffixed_fiat_keys(k2g, servers, monkeypatch):
    """USD, USD.F and USD.HOLD are all dollars."""
    api, ghost = servers
    api.balances = {"ZUSD": "1000.0000", "USD.F": "42.5500", "USD.HOLD": "7.0000",
                    "XXBT": "0.1580"}

    k2g.main([])

    assert ghost.balance_updates[-1]["balance"] == pytest.approx(1049.55)


def test_cash_balance_update_targets_the_named_account(k2g, servers):
    _, ghost = servers
    k2g.main([])
    assert ghost.balance_updates[-1]["id"] == gf.GHOST_ACCOUNT_ID


# ---------------------------------------------------------------------------
# Transfer handling
# ---------------------------------------------------------------------------

def test_crypto_transfers_skipped_by_default(k2g, servers):
    _, ghost = servers

    k2g.main([])

    comments = {item["comment"] for item in ghost.imports[0]}
    assert not any(comment.startswith("KRAKEN#DEP#") for comment in comments)
    assert not any(comment.startswith("KRAKEN#WDR#") for comment in comments)


def test_crypto_transfers_imported_when_enabled(k2g, servers, monkeypatch):
    _, ghost = servers
    monkeypatch.setenv("SKIP_CRYPTO_TRANSFERS", "false")

    k2g.main([])

    comments = {item["comment"] for item in ghost.imports[0]}
    assert k2g.kraken_comment("DEPOSIT", "LDEP1Q-2WSXC-3EDCVF") in comments
    assert k2g.kraken_comment("WITHDRAWAL", "LWDR1A-2SDFG-3HJKLQ") in comments


def test_fiat_deposits_are_never_imported(k2g, servers, monkeypatch, http):
    api, ghost = servers
    monkeypatch.setenv("SKIP_CRYPTO_TRANSFERS", "false")
    api.ledgers["deposit"] = dict(kf.LEDGER_DEPOSIT_FIAT)

    k2g.main([])

    comments = {item["comment"] for item in ghost.imports[0]}
    assert not any(comment.startswith("KRAKEN#DEP#") for comment in comments)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_unmapped_pairs_summary_is_printed(k2g, servers, capsys):
    api, _ = servers
    api.trades = {"TSOLDOT-11111-22222": dict(
        kf.KRAKEN_TRADES["TQXY7Z-4KLMN-8PQRST"], pair="SOLDOT")}

    k2g.main([])

    out = capsys.readouterr().out
    assert "Unmapped Kraken pairs found" in out
    assert "SOLDOT: SOLUSD  # SOL/DOT" in out
    assert "=" * 60 in out


def test_redacted_comments_are_warned_about(k2g, servers, caplog):
    """Restricted view strips comments, which would silently defeat dedup."""
    _, ghost = servers
    ghost.activities = [dict(gf.ACTIVITY_BTC_BUY, comment=None)]

    k2g.main([])

    assert any("none carry a comment" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(k2g, servers, http, config):
    _, ghost = servers

    assert k2g.main(["--dry-run"]) == 0

    assert ghost.imports == []
    assert ghost.balance_updates == []
    assert [call for call in http.to_host(config["ghost_host"])
            if call[0] in ("POST", "PUT")] == []


def test_dry_run_prints_a_stable_activity_list(k2g, servers, capsys):
    assert k2g.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "Dry run:" in out
    assert '"comment": "KRAKEN#TQXY7Z-4KLMN-8PQRST"' in out


def test_dry_run_output_is_identical_across_runs(k2g, servers, capsys):
    """The dry-run dump is the artefact used to prove a refactor is inert."""
    k2g.main(["--dry-run"])
    first = capsys.readouterr().out
    k2g.FAILURES.clear()

    k2g.main(["--dry-run"])
    second = capsys.readouterr().out

    assert first == second


# ---------------------------------------------------------------------------
# --dump-ledger-types
# ---------------------------------------------------------------------------

def test_dump_ledger_types_reports_a_histogram(k2g, servers, capsys):
    assert k2g.main(["--dump-ledger-types"]) == 0

    out = capsys.readouterr().out
    assert "histogram" in out
    assert "staking" in out
    assert "L4UESK-KG3EQ-UFO4T5" in out


def test_dump_ledger_types_writes_nothing(k2g, servers, http, config):
    _, ghost = servers

    k2g.main(["--dump-ledger-types"])

    assert ghost.imports == []
    assert ghost.balance_updates == []


def test_dump_ledger_types_never_contacts_ghostfolio(k2g, servers, http, config):
    """A pure Kraken diagnostic: not one request may reach Ghostfolio.

    Keyed on host rather than method, since the Kraken calls are POSTs too.
    """
    assert k2g.main(["--dump-ledger-types"]) == 0

    assert http.to_host(config["ghost_host"]) == []
    assert http.count("/api/v1/") == 0


def test_dump_ledger_types_runs_without_ghostfolio_credentials(
        k2g, http, kraken, monkeypatch, config, capsys):
    """It must work when Ghostfolio is unconfigured or not yet set up.

    Only KRAKEN_API_KEY and KRAKEN_API_SECRET are exported here; the autouse
    clean_env fixture has removed GHOST_TOKEN and GHOST_HOST.
    """
    monkeypatch.setenv("KRAKEN_API_KEY", config["kraken_api_key"])
    monkeypatch.setenv("KRAKEN_API_SECRET", config["kraken_api_secret"])
    monkeypatch.setenv("API_CALL_DELAY", "0.0")
    kraken(ledgers={"staking": dict(kf.LEDGER_STAKING_REWARD_DOT)}).install(http)

    assert k2g.main(["--dump-ledger-types"]) == 0

    assert "staking" in capsys.readouterr().out
    assert http.calls
    assert all(call[1].startswith(k2g.KRAKEN_API_BASE) for call in http.calls)


def test_other_modes_still_require_ghostfolio_credentials(k2g, http, monkeypatch, config):
    monkeypatch.setenv("KRAKEN_API_KEY", config["kraken_api_key"])
    monkeypatch.setenv("KRAKEN_API_SECRET", config["kraken_api_secret"])

    assert k2g.main([]) == 1
    assert k2g.main(["--reconcile"]) == 1
