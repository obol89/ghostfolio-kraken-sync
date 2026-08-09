"""Reconciliation: balance aggregation, position arithmetic and the report."""

import ghost_fixtures as gf
import kraken_fixtures as kf
import pytest

ACCOUNT = gf.GHOST_ACCOUNT_ID


# ---------------------------------------------------------------------------
# Kraken side
# ---------------------------------------------------------------------------

def test_suffixed_balances_collapse_onto_one_asset(k2g):
    """XXBT, XXBT.B and XXBT.F all describe one BTC holding.

    Summing them is why /0/private/Earn/Allocations is not needed, and so why
    the required API key permissions are unchanged.
    """
    totals = k2g.aggregate_kraken_balances(kf.KRAKEN_BALANCES)

    assert totals["BTC"] == pytest.approx(0.1835)
    assert totals["DOT"] == pytest.approx(13.0)
    assert totals["USD"] == pytest.approx(1042.55)


def test_non_numeric_balance_fails_without_raising(k2g):
    totals = k2g.aggregate_kraken_balances({"XXBT": "0.5", "BABY": "not-a-number"})

    assert totals == {"BTC": 0.5}
    assert len(k2g.FAILURES) == 1


def test_balances_resolve_forward_to_symbols_and_split_out_cash(k2g):
    positions, cash = k2g.kraken_expected_symbols(kf.KRAKEN_BALANCES, {})

    assert positions == {
        "BTCUSD": pytest.approx(0.1835),
        "DOTUSD": pytest.approx(13.0),
        "BABYUSD": pytest.approx(37.5),
    }
    assert cash == {"USD": pytest.approx(1042.55)}


def test_zero_balances_are_not_listed_as_positions(k2g):
    positions, _ = k2g.kraken_expected_symbols({"USDC": "0.0000"}, {})
    assert positions == {}


def test_mapping_overrides_are_honoured(k2g):
    positions, _ = k2g.kraken_expected_symbols({"XXBT": "1.0"}, {"BTC": "BTC-USD"})
    assert positions == {"BTC-USD": pytest.approx(1.0)}


# ---------------------------------------------------------------------------
# Ghostfolio side
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("activity_type,factor", [
    ("BUY", 1.0),
    ("buy", 1.0),
    ("SELL", -1.0),
    ("INTEREST", 0.0),
    ("DIVIDEND", 0.0),
    ("FEE", 0.0),
    ("ITEM", 0.0),
    ("LIABILITY", 0.0),
    ("", 0.0),
    (None, 0.0),
])
def test_ghost_activity_factor_mirrors_getfactor(k2g, activity_type, factor):
    """Ghostfolio moves quantity for BUY and SELL and for nothing else."""
    assert k2g.ghost_activity_factor(activity_type) == factor


def test_activity_symbol_falls_back_to_symbol_profile(k2g):
    assert k2g.activity_symbol(gf.ACTIVITY_BTC_BUY) == "BTCUSD"
    assert k2g.activity_symbol(gf.ACTIVITY_SYMBOL_PROFILE_ONLY) == "SOLUSD"
    assert k2g.activity_symbol({}) == ""


def test_activity_account_id_tolerates_both_shapes(k2g):
    assert k2g.activity_account_id(gf.ACTIVITY_BTC_BUY) == ACCOUNT
    assert k2g.activity_account_id({"Account": {"id": "abc"}}) == "abc"


def test_interest_rows_are_counted_as_ignored_not_as_quantity(k2g):
    """The heart of the defect, expressed as arithmetic."""
    positions = k2g.aggregate_ghost_positions(gf.GHOST_ACTIVITIES, ACCOUNT)

    btc = positions["BTCUSD"]
    assert btc["quantity"] == pytest.approx(0.158)
    assert btc["ignored"] == pytest.approx(0.0255)
    assert btc["ignored_types"]["INTEREST"] == 2


def test_positions_are_scoped_to_one_account(k2g):
    """A holding in another Ghostfolio account must not mask the drift."""
    activities = gf.GHOST_ACTIVITIES + [gf.ACTIVITY_BTC_OTHER_ACCOUNT]

    positions = k2g.aggregate_ghost_positions(activities, ACCOUNT)

    assert positions["BTCUSD"]["quantity"] == pytest.approx(0.158)


def test_unfiltered_aggregation_includes_every_account(k2g):
    activities = gf.GHOST_ACTIVITIES + [gf.ACTIVITY_BTC_OTHER_ACCOUNT]

    positions = k2g.aggregate_ghost_positions(activities)

    assert positions["BTCUSD"]["quantity"] == pytest.approx(2.158)


def test_sells_reduce_the_position(k2g):
    activities = [
        dict(gf.ACTIVITY_BTC_BUY, quantity=1.0, type="BUY"),
        dict(gf.ACTIVITY_BTC_BUY, id="x", quantity=0.25, type="SELL"),
    ]

    positions = k2g.aggregate_ghost_positions(activities, ACCOUNT)

    assert positions["BTCUSD"]["quantity"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------

def build(k2g, kraken_positions, activities, missing=()):
    return k2g.build_reconcile_rows(
        kraken_positions,
        k2g.aggregate_ghost_positions(activities, ACCOUNT),
        list(missing),
    )


def row_for(rows, symbol):
    return next(row for row in rows if row["symbol"] == symbol)


def test_interest_gap_is_fully_explained(k2g):
    """delta == ignored: the reward was imported, into an inert activity type."""
    rows = build(k2g, {"BTCUSD": 0.1835}, gf.GHOST_ACTIVITIES)

    btc = row_for(rows, "BTCUSD")
    assert btc["delta"] == pytest.approx(0.0255)
    assert btc["ignored"] == pytest.approx(0.0255)
    assert btc["residual"] == pytest.approx(0.0)
    assert btc["cause"].startswith("explained:")


def test_never_fetched_reward_shows_as_missing(k2g):
    """delta > 0 with ignored == 0: the reward was never fetched at all.

    This is what separates the two mechanisms behind an identical symptom.
    """
    rows = build(k2g, {"BABYUSD": 37.5}, gf.GHOST_ACTIVITIES)

    baby = row_for(rows, "BABYUSD")
    assert baby["ignored"] == 0.0
    assert baby["residual"] == pytest.approx(37.5)
    assert baby["cause"] == "missing from Ghostfolio entirely"


def test_matching_quantities_reconcile(k2g):
    rows = build(k2g, {"DOTUSD": 13.0}, gf.GHOST_ACTIVITIES)
    assert row_for(rows, "DOTUSD")["cause"] == "ok"


def test_dust_is_within_tolerance(k2g):
    rows = build(k2g, {"DOTUSD": 13.0 + 1e-9}, gf.GHOST_ACTIVITIES)
    assert row_for(rows, "DOTUSD")["cause"] == "ok"


def test_asset_held_only_in_ghostfolio(k2g):
    rows = build(k2g, {}, gf.GHOST_ACTIVITIES)
    assert "not held on Kraken" in row_for(rows, "DOTUSD")["cause"]


def test_ghostfolio_holding_more_than_kraken(k2g):
    rows = build(k2g, {"DOTUSD": 5.0}, gf.GHOST_ACTIVITIES)
    assert "holds more than Kraken" in row_for(rows, "DOTUSD")["cause"]


def test_partly_explained_gap(k2g):
    rows = build(k2g, {"BTCUSD": 0.5}, gf.GHOST_ACTIVITIES)

    btc = row_for(rows, "BTCUSD")
    assert btc["cause"].startswith("partly explained:")
    assert btc["residual"] > 0


def test_missing_conversions_are_named_as_the_cause(k2g):
    """Conversions are ledger-only, so the missing-trade check cannot see them."""
    missing_ledger = [
        {"ledger_id": "LCNVRC-DOT01-BBBBBB", "kind": "CONVERT", "symbol": "DOTUSD"},
    ]

    rows = k2g.build_reconcile_rows(
        {"DOTUSD": 14.0}, k2g.aggregate_ghost_positions(gf.GHOST_ACTIVITIES, ACCOUNT),
        [], missing_ledger)

    cause = row_for(rows, "DOTUSD")["cause"]
    assert "ledger entr" in cause
    assert "convert" in cause


def test_an_explained_gap_still_wins_over_missing_ledger_entries(k2g):
    """Attribution order matters: the inert INTEREST rows explain BTC fully."""
    missing_ledger = [
        {"ledger_id": "LCNVRC-XBT01-BBBBBB", "kind": "CONVERT", "symbol": "BTCUSD"},
    ]

    rows = k2g.build_reconcile_rows(
        {"BTCUSD": 0.1835}, k2g.aggregate_ghost_positions(gf.GHOST_ACTIVITIES, ACCOUNT),
        [], missing_ledger)

    assert row_for(rows, "BTCUSD")["cause"].startswith("explained:")


def test_missing_bare_transfer_is_named_as_the_cause(k2g):
    missing_ledger = [
        {"ledger_id": "LTRFOU-6YHNM-7UJMIK", "kind": "TRANSFER", "symbol": "DOTUSD"},
    ]

    rows = k2g.build_reconcile_rows(
        {"DOTUSD": 14.0}, k2g.aggregate_ghost_positions(gf.GHOST_ACTIVITIES, ACCOUNT),
        [], missing_ledger)

    assert "transfer" in row_for(rows, "DOTUSD")["cause"]


def test_find_missing_ledger_activities_skips_imported_ones(k2g, config):
    ledger = {**kf.LEDGER_CONVERT_FIAT_TO_CRYPTO, **kf.LEDGER_TRANSFER_BARE_NEGATIVE}
    existing = {k2g.kraken_comment("CONVERT", "LCNVRC-XBT01-BBBBBB")}

    missing = k2g.find_missing_ledger_activities(ledger, existing, {}, config)

    kinds = {(item["kind"], item["symbol"]) for item in missing}
    assert ("TRANSFER", "LUNA2USD") in kinds
    assert ("CONVERT", "BTCUSD") not in kinds


def test_configured_skips_are_flagged_expected_not_dropped(k2g, config):
    """Deposits absent because SKIP_CRYPTO_TRANSFERS is on are not a fault.

    They are still returned, because their quantity is what explains the gap
    they leave in Ghostfolio - they just must not be counted as drift.
    """
    ledger = dict(kf.LEDGER_DEPOSIT_BTC)

    assert config["skip_crypto_transfers"] is True
    absent = k2g.find_missing_ledger_activities(ledger, set(), {}, config)
    assert len(absent) == 1
    assert absent[0]["expected"] is True
    assert absent[0]["net"] == pytest.approx(0.025)

    config["skip_crypto_transfers"] = False
    missing = k2g.find_missing_ledger_activities(ledger, set(), {}, config)
    assert missing[0]["expected"] is False


def test_trade_fee_corrections_are_tracked_as_ledger_activities(k2g, config):
    """A missing fee correction must be reported, not silently absent."""
    missing = k2g.find_missing_ledger_activities(
        dict(kf.LEDGER_TRADE_BASE_FEES), set(), {}, config, kf.TRADES_WITH_BASE_FEES)

    kinds = {item["kind"] for item in missing}
    assert kinds == {"TRADE_FEE"}
    assert {item["symbol"] for item in missing} == {"BTCUSD"}


def test_imported_trade_fee_corrections_are_not_reported_missing(k2g, config):
    existing = {k2g.kraken_comment("TRADE_FEE", ledger_id)
                for ledger_id in kf.LEDGER_TRADE_BASE_FEES}

    assert k2g.find_missing_ledger_activities(
        dict(kf.LEDGER_TRADE_BASE_FEES), existing, {}, config,
        kf.TRADES_WITH_BASE_FEES) == []


def test_withdrawals_carry_a_negative_net(k2g, config):
    """Signed, so a deposit and a withdrawal of the same size cancel out."""
    absent = k2g.find_missing_ledger_activities(
        dict(kf.LEDGER_WITHDRAWAL_BTC), set(), {}, config)

    assert absent[0]["net"] == pytest.approx(-0.00505)


# ---------------------------------------------------------------------------
# SKIP_CRYPTO_TRANSFERS attribution
# ---------------------------------------------------------------------------

def absent(symbol, net, kind="DEPOSIT"):
    return {"ledger_id": "L%s" % symbol, "kind": kind, "symbol": symbol,
            "net": net, "expected": True}


def test_skipped_deposit_explains_a_positive_gap(k2g):
    """Kraken holds it, Ghostfolio was never told: absent by configuration."""
    rows = k2g.build_reconcile_rows(
        {"DOTUSD": 15.0}, k2g.aggregate_ghost_positions(gf.GHOST_ACTIVITIES, ACCOUNT),
        [], [], [absent("DOTUSD", 2.0)])

    row = row_for(rows, "DOTUSD")
    assert row["expected_absent"] == pytest.approx(2.0)
    assert row["residual"] == pytest.approx(0.0)
    assert row["cause"].startswith("explained:")
    assert "SKIP_CRYPTO_TRANSFERS" in row["cause"]


def test_skipped_deposit_explains_a_negative_ghostfolio_position(k2g):
    """A convert SELL with no matching deposit drives the position negative.

    Kraken shows nothing left, Ghostfolio shows -5, and the skipped deposit
    that funded the sale accounts for the whole difference. It must not be
    reported as "not held on Kraken" just because the Kraken side is zero.
    """
    activities = [dict(gf.ACTIVITY_DOT_BUY, id="s1", quantity=5.0, type="SELL",
                       comment="KRAKEN#CONVERT#LCNVSP-DOT01-AAAAAA")]

    rows = k2g.build_reconcile_rows(
        {}, k2g.aggregate_ghost_positions(activities, ACCOUNT),
        [], [], [absent("DOTUSD", 5.0)])

    row = row_for(rows, "DOTUSD")
    assert row["ghost"] == pytest.approx(-5.0)
    assert row["residual"] == pytest.approx(0.0)
    assert row["cause"].startswith("explained:")
    assert "SKIP_CRYPTO_TRANSFERS" in row["cause"]


def test_deposit_and_withdrawal_net_against_each_other(k2g):
    rows = k2g.build_reconcile_rows(
        {"DOTUSD": 13.0}, k2g.aggregate_ghost_positions(gf.GHOST_ACTIVITIES, ACCOUNT),
        [], [], [absent("DOTUSD", 4.0), absent("DOTUSD", -4.0, "WITHDRAWAL")])

    row = row_for(rows, "DOTUSD")
    assert row["expected_absent"] == pytest.approx(0.0)
    assert row["cause"] == "ok"


def test_only_the_remainder_beyond_a_skipped_transfer_fails(k2g):
    rows = k2g.build_reconcile_rows(
        {"DOTUSD": 16.0}, k2g.aggregate_ghost_positions(gf.GHOST_ACTIVITIES, ACCOUNT),
        [], [], [absent("DOTUSD", 2.0)])

    row = row_for(rows, "DOTUSD")
    assert row["residual"] == pytest.approx(1.0)
    assert row["cause"].startswith("partly explained:")
    assert "SKIP_CRYPTO_TRANSFERS" in row["cause"]


def test_ignored_and_skipped_transfers_combine_in_one_explanation(k2g):
    rows = k2g.build_reconcile_rows(
        {"BTCUSD": 0.2835}, k2g.aggregate_ghost_positions(gf.GHOST_ACTIVITIES, ACCOUNT),
        [], [], [absent("BTCUSD", 0.1)])

    row = row_for(rows, "BTCUSD")
    assert row["residual"] == pytest.approx(0.0)
    assert "Ghostfolio ignores" in row["cause"]
    assert "SKIP_CRYPTO_TRANSFERS" in row["cause"]


def test_missing_trades_are_named_as_the_cause(k2g):
    missing = [{"symbol": "DOTUSD", "currency": "USD", "iso4217": True,
                "pair": "DOTUSD", "trade_id": "T1", "date": "", "side": "buy",
                "volume": 1.0, "skip_reason": None}]

    rows = build(k2g, {"DOTUSD": 14.0}, gf.GHOST_ACTIVITIES, missing)

    assert "never imported" in row_for(rows, "DOTUSD")["cause"]


# ---------------------------------------------------------------------------
# Legacy MANUAL profiles, and the table/exit-code invariant
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,expected", [
    ("3f2a91c4-7b6d-4e18-9c05-2ab8d7e6f130", True),
    ("GF_LEGACY_STAKE", True),
    ("GF_FEAR_AND_GREED_INDEX", True),
    ("BTCUSD", False),
    ("DOTUSD", False),
    ("", False),
    (None, False),
])
def test_custom_asset_profile_detection(k2g, symbol, expected):
    assert k2g.is_custom_asset_profile(symbol) is expected


def test_legacy_manual_profiles_are_attributed_not_failed(k2g):
    """Thirteen of these failed a reconcile whose table showed four rows.

    They carry quantity only in INTEREST rows, contribute nothing to any
    position, and have no Kraken asset behind them.
    """
    activities = gf.GHOST_ACTIVITIES + [gf.ACTIVITY_LEGACY_MANUAL_PROFILE,
                                        gf.ACTIVITY_LEGACY_GF_PROFILE]

    rows = build(k2g, {"BTCUSD": 0.1835, "DOTUSD": 13.0}, activities)

    for symbol in ("3f2a91c4-7b6d-4e18-9c05-2ab8d7e6f130", "GF_LEGACY_STAKE"):
        row = row_for(rows, symbol)
        assert row["cause"] == k2g.LEGACY_PROFILE_CAUSE
        assert row["drift"] is False
    assert k2g.reconcile_failures(rows) == []


def test_a_real_symbol_with_only_ignored_quantity_still_fails(k2g):
    """The exemption is for synthetic profiles, not for any zero position."""
    activities = [dict(gf.ACTIVITY_BTC_INTEREST_1, symbol="SOLUSD",
                       SymbolProfile={"symbol": "SOLUSD"})]

    rows = build(k2g, {}, activities)

    row = row_for(rows, "SOLUSD")
    assert row["cause"] != k2g.LEGACY_PROFILE_CAUSE
    assert row["drift"] is True


def test_every_failing_symbol_appears_in_the_printed_table(k2g, capsys):
    """The invariant: the exit-code set is a subset of what was printed."""
    activities = gf.GHOST_ACTIVITIES + [gf.ACTIVITY_LEGACY_MANUAL_PROFILE]
    rows = build(k2g, {"BABYUSD": 37.5, "BTCUSD": 0.1835, "DOTUSD": 13.0}, activities)

    k2g.print_reconcile_report(rows, {}, 0.0, [])

    out = capsys.readouterr().out
    for row in k2g.reconcile_failures(rows):
        assert row["symbol"] in out


def test_a_zero_delta_row_with_a_residual_is_not_reported_ok(k2g):
    """Calling that "ok" is what hid the legacy profiles from the table."""
    activities = [dict(gf.ACTIVITY_BTC_INTEREST_1, symbol="SOLUSD",
                       SymbolProfile={"symbol": "SOLUSD"})]

    rows = build(k2g, {}, activities)

    assert row_for(rows, "SOLUSD")["cause"] != "ok"


# ---------------------------------------------------------------------------
# Missing trades
# ---------------------------------------------------------------------------

def test_imported_trades_are_not_reported_missing(k2g):
    existing = {k2g.kraken_comment("TRADE", trade_id) for trade_id in kf.KRAKEN_TRADES}

    assert k2g.find_missing_trades(kf.KRAKEN_TRADES, existing, {}) == []


def test_crypto_quoted_trade_is_flagged_as_non_iso4217(k2g):
    """ETHXBT resolves to currency BTC, which Ghostfolio's DTO rejects."""
    missing = k2g.find_missing_trades(kf.KRAKEN_TRADES, set(), {})

    by_id = {trade["trade_id"]: trade for trade in missing}
    assert by_id["T7JHGF-2WERT-9ZXCVB"]["iso4217"] is False
    assert by_id["T7JHGF-2WERT-9ZXCVB"]["currency"] == "BTC"


def test_stablecoin_quoted_trade_is_not_flagged(k2g):
    """XBTUSDC resolves to USD; the ISO-4217 rejection it caused is fixed."""
    missing = k2g.find_missing_trades(kf.KRAKEN_TRADES, set(), {})

    by_id = {trade["trade_id"]: trade for trade in missing}
    assert by_id["TQXY7Z-4KLMN-8PQRST"]["iso4217"] is True
    assert by_id["TQXY7Z-4KLMN-8PQRST"]["currency"] == "USD"


def test_zero_volume_trade_records_a_skip_reason(k2g):
    """Otherwise indistinguishable from an import that failed."""
    missing = k2g.find_missing_trades(kf.TRADE_ZERO_VOLUME, set(), {})

    assert missing[0]["skip_reason"] == "zero volume"


def test_find_missing_trades_does_not_mutate_the_sync_report(k2g):
    """A read-only mode must not touch the unmapped-pairs accumulator."""
    unmapped = {}
    trades = {"TSOLDOT-1-2": dict(kf.KRAKEN_TRADES["TQXY7Z-4KLMN-8PQRST"], pair="SOLDOT")}

    k2g.find_missing_trades(trades, set(), {})

    assert unmapped == {}


def test_missing_trades_report_groups_by_currency(k2g, capsys):
    missing = k2g.find_missing_trades(kf.KRAKEN_TRADES, set(), {})

    k2g.print_missing_trades(missing)

    out = capsys.readouterr().out
    assert "by currency:" in out
    assert "NOT ISO 4217" in out
    assert "T7JHGF-2WERT-9ZXCVB" in out
    assert "ETHXBT: ETHUSD  # ETH/BTC" in out


# ---------------------------------------------------------------------------
# Legacy INTEREST rows
# ---------------------------------------------------------------------------

def test_legacy_interest_rows_are_listed_with_their_ids(k2g, capsys):
    by_comment = k2g.index_activities_by_comment(gf.GHOST_ACTIVITIES)

    legacy = k2g.report_legacy_staking(by_comment)

    out = capsys.readouterr().out
    assert len(legacy) == 2
    assert "b1f0e7c2-2222-4aaa-9bbb-0c1d2e3f4a51" in out
    assert "safe to" in out
    assert k2g.FAILURES == []


def test_no_legacy_rows_reports_nothing(k2g, capsys):
    by_comment = k2g.index_activities_by_comment([gf.ACTIVITY_BTC_BUY])

    assert k2g.report_legacy_staking(by_comment) == []
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# run_reconcile end to end
# ---------------------------------------------------------------------------

@pytest.fixture
def reconcile_servers(http, kraken, ghostfolio, env):
    api = kraken(
        trades=dict(kf.KRAKEN_TRADES),
        ledgers={},
        balances=dict(kf.KRAKEN_BALANCES),
    ).install(http)
    ghost = ghostfolio(activities=gf.GHOST_ACTIVITIES).install(http)
    return api, ghost


def test_reconcile_makes_no_ghostfolio_writes(k2g, reconcile_servers, http, config):
    """Keyed on host, not method: Kraken calls are POSTs too."""
    _, ghost = reconcile_servers

    k2g.main(["--reconcile"])

    writes = [call for call in http.to_host(config["ghost_host"])
              if call[0] in ("POST", "PUT")]
    assert writes == []
    assert ghost.imports == []
    assert ghost.balance_updates == []


def test_reconcile_reports_the_expected_table(k2g, reconcile_servers, capsys):
    k2g.main(["--reconcile"])

    out = capsys.readouterr().out
    assert "Reconciliation: Kraken balances vs Ghostfolio positions" in out
    assert "BABYUSD" in out
    assert "missing from Ghostfolio entirely" in out
    assert "explained:" in out
    assert "Cash (not a position)" in out


def test_reconcile_exits_nonzero_on_unexplained_drift(k2g, reconcile_servers):
    """BABY is real drift; the fully explained BTC row must not be the reason."""
    assert k2g.main(["--reconcile"]) == 1

    reasons = " ".join(k2g.FAILURES)
    assert "BABYUSD" in reasons
    assert "BTCUSD" not in reasons


def test_reconcile_exits_zero_when_every_gap_is_explained(k2g, reconcile_servers):
    """A fully attributed difference must not hold the exit code hostage."""
    api, _ = reconcile_servers
    api.balances = {"XXBT": "0.1580", "XXBT.F": "0.0255", "DOT": "13.0", "ZUSD": "1042.55"}

    assert k2g.main(["--reconcile"]) == 0


def test_reconcile_fails_loudly_when_comments_are_redacted(k2g, reconcile_servers):
    """Without comments every trade looks missing, so the report is worthless."""
    _, ghost = reconcile_servers
    ghost.activities = [dict(activity, comment=None) for activity in gf.GHOST_ACTIVITIES]

    assert k2g.main(["--reconcile"]) == 1
    assert any("none carry a comment" in item for item in k2g.FAILURES)


def test_convert_sell_funded_by_a_skipped_deposit_reconciles(k2g, reconcile_servers, capsys):
    """The shape a real account produces with SKIP_CRYPTO_TRANSFERS on.

    A DOT deposit funded a conversion. The conversion imported as a SELL, the
    deposit did not, so Ghostfolio shows -5 DOT against Kraken's 0. That is
    fully accounted for by configuration and must exit 0.
    """
    api, ghost = reconcile_servers
    api.trades = {}
    api.balances = {"DOT": "0.0000000000"}
    api.ledgers = {"deposit": dict(kf.LEDGER_DEPOSIT_DOT)}
    ghost.activities = [dict(gf.ACTIVITY_DOT_BUY, id="c1", quantity=5.0, type="SELL",
                             comment="KRAKEN#CONVERT#LCNVSP-DOT01-AAAAAA")]

    assert k2g.main(["--reconcile"]) == 0

    out = capsys.readouterr().out
    assert "SKIP_CRYPTO_TRANSFERS" in out
    assert "Unexplained drift on 0 asset(s)" in out
    assert k2g.FAILURES == []


def test_the_same_gap_is_real_drift_when_transfers_are_not_skipped(k2g, reconcile_servers,
                                                                   monkeypatch):
    """With SKIP_CRYPTO_TRANSFERS off the deposit is missing, not excluded."""
    api, ghost = reconcile_servers
    monkeypatch.setenv("SKIP_CRYPTO_TRANSFERS", "false")
    api.trades = {}
    api.balances = {"DOT": "0.0000000000"}
    api.ledgers = {"deposit": dict(kf.LEDGER_DEPOSIT_DOT)}
    ghost.activities = [dict(gf.ACTIVITY_DOT_BUY, id="c1", quantity=5.0, type="SELL",
                             comment="KRAKEN#CONVERT#LCNVSP-DOT01-AAAAAA")]

    assert k2g.main(["--reconcile"]) == 1


def test_legacy_manual_profiles_reconcile_clean_end_to_end(k2g, reconcile_servers, capsys):
    """A UUID INTEREST profile is attributed, printed, and does not fail."""
    api, ghost = reconcile_servers
    api.trades = {}
    api.balances = {"XXBT": "0.1580", "XXBT.F": "0.0255", "DOT": "13.0"}
    ghost.activities = gf.GHOST_ACTIVITIES + [gf.ACTIVITY_LEGACY_MANUAL_PROFILE]

    assert k2g.main(["--reconcile"]) == 0

    out = capsys.readouterr().out
    assert "3f2a91c4-7b6d-4e18-9c05-2ab8d7e6f130" in out
    assert k2g.LEGACY_PROFILE_CAUSE in out
    assert k2g.FAILURES == []


def test_reconcile_warns_when_sync_since_is_set(k2g, reconcile_servers, monkeypatch, caplog):
    monkeypatch.setenv("SYNC_SINCE", "2024-01-01")
    api, _ = reconcile_servers
    api.balances = {"XXBT": "0.1580", "XXBT.F": "0.0255", "DOT": "13.0"}

    k2g.main(["--reconcile"])

    assert any("SYNC_SINCE is set" in record.message for record in caplog.records)
