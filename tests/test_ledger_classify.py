"""Ledger classification and the internal-transfer netting pre-pass.

Pure functions over fixture payloads; nothing here touches the network.
"""

import kraken_fixtures as kf
import pytest


def only(entries):
    (ledger_id, entry), = entries.items()
    return ledger_id, entry


def classify(k2g, entries, internal_refids=frozenset()):
    _, entry = only(entries)
    return k2g.classify_ledger_entry(entry, internal_refids)[0]


# ---------------------------------------------------------------------------
# The taxonomy, one case per (type, subtype)
# ---------------------------------------------------------------------------

def test_legacy_staking_reward(k2g):
    assert classify(k2g, kf.LEDGER_STAKING_REWARD_DOT) == k2g.INGEST_REWARD


def test_earn_reward(k2g):
    """The case the old filtered fetch could never see.

    `earn` is not in Kraken's REST type filter enum, so a type=staking pass
    returns nothing for a Kraken Earn account.
    """
    assert classify(k2g, kf.LEDGER_EARN_REWARD_BTC) == k2g.INGEST_REWARD


def test_airdrop_type(k2g):
    assert classify(k2g, kf.LEDGER_AIRDROP_BABY) == k2g.INGEST_AIRDROP


def test_airdrop_filed_under_deposit_is_not_swallowed(k2g):
    """Subtype is matched before type for exactly this case.

    Under a type-first dispatch this would classify as a deposit and then be
    dropped by SKIP_CRYPTO_TRANSFERS, which is on by default.
    """
    assert classify(k2g, kf.LEDGER_AIRDROP_AS_DEPOSIT) == k2g.INGEST_AIRDROP


@pytest.mark.parametrize("fixture", [
    kf.LEDGER_EARN_ALLOCATION_PAIR,
    kf.LEDGER_EARN_DEALLOCATION_PAIR,
    kf.LEDGER_EARN_MIGRATION_PAIR,
])
def test_earn_internal_subtypes_are_skipped_leg_by_leg(k2g, fixture):
    for entry in fixture.values():
        assert k2g.classify_ledger_entry(entry)[0] == k2g.SKIP_INTERNAL_TRANSFER


def test_unrecognised_earn_subtype_is_still_ingested(k2g):
    """Safe only because the netting pre-pass already removed the moves.

    A new Kraken Earn reward subtype must not be silently dropped, so the
    default for an unknown earn subtype is to ingest and record why.
    """
    _, entry = only(kf.LEDGER_EARN_UNKNOWN_SUBTYPE)
    classification, reason = k2g.classify_ledger_entry(entry)

    assert classification == k2g.INGEST_REWARD
    assert "autocompound" in reason


def test_staking_transfer_subtypes_are_skipped(k2g):
    """Both legs are in the spot ledger and Balance covers the bonded side."""
    for entry in kf.LEDGER_TRANSFER_SPOT_TO_EARN.values():
        assert k2g.classify_ledger_entry(entry)[0] == k2g.SKIP_INTERNAL_TRANSFER


@pytest.mark.parametrize("fixture", [
    kf.LEDGER_TRANSFER_SPOT_FROM_FUTURES,
    kf.LEDGER_TRANSFER_SPOT_TO_FUTURES,
])
def test_futures_transfers_are_not_internal(k2g, fixture):
    """The futures wallet is outside both Ledgers and Balance.

    Only one leg is ever fetched, so treating these as internal would drop a
    real change to the holdings being reconciled.
    """
    assert classify(k2g, fixture) == k2g.INGEST_TRANSFER


def test_futures_transfer_is_not_netted_away_by_the_prepass(k2g):
    """There is no counterleg to pair it with, so nothing cancels it."""
    ledger = dict(kf.LEDGER_TRANSFER_SPOT_FROM_FUTURES)

    assert k2g.find_internal_transfer_refids(ledger) == set()
    buckets, _, _ = k2g.classify_ledger(ledger)
    assert len(buckets[k2g.INGEST_TRANSFER]) == 1


def test_a_paired_staking_move_still_nets_to_zero(k2g):
    """The asymmetry must not leak: staking pairs stay fully skipped."""
    buckets, histogram, _ = k2g.classify_ledger(dict(kf.LEDGER_TRANSFER_SPOT_TO_EARN))

    assert histogram[k2g.SKIP_INTERNAL_TRANSFER] == 2
    assert buckets[k2g.INGEST_TRANSFER] == {}


# ---------------------------------------------------------------------------
# Trade legs: base is covered, a non-fiat quote leg is not
# ---------------------------------------------------------------------------

def test_non_fiat_quote_leg_of_a_trade_is_ingested(k2g):
    """The trade import emits the base asset only.

    Nothing else ever decreases the quote position, which is how a USDC
    balance reached +100.24 in Ghostfolio against 0.40 left on Kraken.
    """
    bases = k2g.trade_base_assets(kf.KRAKEN_TRADES)
    buckets, _, _ = k2g.classify_ledger(dict(kf.LEDGER_TRADE_XBTUSDC), trade_bases=bases)

    assert set(buckets[k2g.SKIP_COVERED_BY_TRADES]) == {"LTRD4Q-6ZXWE-8CVBNM"}
    assert set(buckets[k2g.INGEST_TRADE_QUOTE]) == {"LTRD5W-7ASDF-9GHJKL"}


def test_fiat_quote_leg_of_a_trade_is_cash(k2g):
    """A CHF quote leg belongs to the balance update, not to a position."""
    ledger = {
        "LCHF01-BASE1-AAAAAA": {"refid": "TCHF01", "type": "trade", "subtype": "tradespot",
                                "asset": "XXBT", "amount": "0.01", "fee": "0", "time": 1},
        "LCHF02-QUOTE-BBBBBB": {"refid": "TCHF01", "type": "trade", "subtype": "tradespot",
                                "asset": "CHF", "amount": "-900.0", "fee": "0", "time": 1},
    }
    bases = k2g.trade_base_assets({"TCHF01": {"pair": "XBTCHF"}})
    buckets, _, _ = k2g.classify_ledger(ledger, trade_bases=bases)

    assert buckets[k2g.INGEST_TRADE_QUOTE] == {}
    assert len(buckets[k2g.SKIP_OTHER]) == 1


def test_trade_legs_are_assumed_covered_without_trade_context(k2g):
    """Guessing the other way would double a traded position."""
    buckets, _, _ = k2g.classify_ledger(dict(kf.LEDGER_TRADE_XBTUSDC))

    assert len(buckets[k2g.SKIP_COVERED_BY_TRADES]) == 2
    assert buckets[k2g.INGEST_TRADE_QUOTE] == {}


def test_trade_base_assets_resolves_the_pair(k2g):
    assert k2g.trade_base_assets(kf.KRAKEN_TRADES)["TQXY7Z-4KLMN-8PQRST"] == "BTC"


def test_base_leg_with_a_base_denominated_fee_needs_a_correction(k2g):
    """TradesHistory reports vol gross, so the fee never reduces the position."""
    bases = k2g.trade_base_assets(kf.TRADES_WITH_BASE_FEES)
    buckets, _, _ = k2g.classify_ledger(dict(kf.LEDGER_TRADE_BASE_FEES), trade_bases=bases)

    assert set(buckets[k2g.INGEST_TRADE_FEE]) == set(kf.LEDGER_TRADE_BASE_FEES)
    assert buckets[k2g.SKIP_COVERED_BY_TRADES] == {}


def test_base_leg_without_a_fee_stays_covered(k2g):
    """The common case must not emit an empty correction."""
    bases = k2g.trade_base_assets(kf.KRAKEN_TRADES)
    buckets, _, _ = k2g.classify_ledger(dict(kf.LEDGER_TRADE_XBTUSDC), trade_bases=bases)

    assert buckets[k2g.INGEST_TRADE_FEE] == {}
    assert set(buckets[k2g.SKIP_COVERED_BY_TRADES]) == {"LTRD4Q-6ZXWE-8CVBNM"}


@pytest.mark.parametrize("fixture", [
    kf.LEDGER_TRANSFER_NO_SUBTYPE,
    kf.LEDGER_TRANSFER_BARE_NEGATIVE,
])
def test_bare_transfer_is_ingested(k2g, fixture):
    """A subtype-less transfer really moves the holding.

    Kraken uses it for account-to-account moves and delisting sweep-outs,
    unlike the spot/staking/futures subtypes which only shuffle a balance
    between wallets.
    """
    assert classify(k2g, fixture) == k2g.INGEST_TRANSFER


def test_transfer_with_an_unrecognised_subtype_is_still_unknown(k2g):
    entry = {"type": "transfer", "subtype": "somethingnew", "asset": "XXBT",
             "amount": "1.0", "fee": "0", "refid": "R1", "time": 1}
    assert k2g.classify_ledger_entry(entry)[0] == k2g.UNKNOWN


def test_trade_entries_are_left_to_tradeshistory(k2g):
    """Ingesting these as well would double every traded position."""
    for entry in kf.LEDGER_TRADE_XBTUSDC.values():
        assert k2g.classify_ledger_entry(entry)[0] in (
            k2g.SKIP_COVERED_BY_TRADES, k2g.SKIP_OTHER)


def test_trade_subtype_tradespot_is_still_a_trade(k2g):
    """The real ledger tags spot trade legs with subtype tradespot."""
    assert classify(k2g, kf.LEDGER_TRADE_SPOT_SUBTYPE) == k2g.SKIP_COVERED_BY_TRADES


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------

def convert_refids(k2g, ledger):
    return {refid for refid, group in k2g.group_convert_refids(ledger).items()
            if len(group) > 1}


@pytest.mark.parametrize("fixture", [
    kf.LEDGER_CONVERT_FIAT_TO_CRYPTO,
    kf.LEDGER_CONVERT_CRYPTO_TO_FIAT,
    kf.LEDGER_CONVERT_CRYPTO_TO_CRYPTO,
    kf.LEDGER_CONVERT_DUSTSWEEP,
    kf.LEDGER_CONVERT_FIAT_TO_FIAT,
])
def test_every_conversion_leg_is_classified_as_a_conversion(k2g, fixture):
    refids = convert_refids(k2g, fixture)
    for entry in fixture.values():
        assert k2g.classify_ledger_entry(entry, frozenset(), refids)[0] == k2g.INGEST_CONVERT


def test_fiat_conversion_legs_survive_classification(k2g):
    """The fiat leg carries the price, so the fiat filter must not eat it.

    Without it the crypto leg of an instant buy has no cost basis at all.
    """
    fixture = kf.LEDGER_CONVERT_FIAT_TO_CRYPTO
    fiat_leg = fixture["LCNVSP-CHF01-AAAAAA"]

    assert k2g.normalize_kraken_asset(fiat_leg["asset"]) in k2g.FIAT_CURRENCIES
    assert k2g.classify_ledger_entry(
        fiat_leg, frozenset(), convert_refids(k2g, fixture))[0] == k2g.INGEST_CONVERT


def test_lone_conversion_leg_is_unknown(k2g):
    """A group of one is a conversion straddling the SYNC_SINCE boundary."""
    refids = convert_refids(k2g, kf.LEDGER_CONVERT_LONE_SPEND)

    assert refids == set()
    classification, reason = k2g.classify_ledger_entry(
        only(kf.LEDGER_CONVERT_LONE_SPEND)[1], frozenset(), refids)
    assert classification == k2g.UNKNOWN
    assert "unpaired" in reason


def test_group_convert_refids_groups_many_to_one(k2g):
    groups = k2g.group_convert_refids(kf.LEDGER_CONVERT_DUSTSWEEP)

    assert len(groups) == 1
    assert len(next(iter(groups.values()))) == 3


def test_group_convert_refids_ignores_other_types(k2g):
    assert k2g.group_convert_refids(kf.LEDGER_TRADE_XBTUSDC) == {}
    assert k2g.group_convert_refids(kf.LEDGER_STAKING_REWARD_DOT) == {}


def test_conversion_refids_do_not_collide_with_trades(k2g):
    """Verified against the real account: conversions are ledger-only.

    Their refids never appear in TradesHistory, which is why deduplication on
    KRAKEN#CONVERT# alone is sufficient.
    """
    conversion_refids = {entry["refid"] for entry in kf.LEDGER_CONVERT_FIAT_TO_CRYPTO.values()}
    assert not (conversion_refids & set(kf.KRAKEN_TRADES))


def test_deposit_and_withdrawal(k2g):
    assert classify(k2g, kf.LEDGER_DEPOSIT_BTC) == k2g.INGEST_DEPOSIT
    assert classify(k2g, kf.LEDGER_WITHDRAWAL_BTC) == k2g.INGEST_WITHDRAWAL


def test_fiat_is_never_ingested(k2g):
    """Cash is the account balance, not a position."""
    assert classify(k2g, kf.LEDGER_DEPOSIT_FIAT) == k2g.SKIP_OTHER
    assert classify(k2g, kf.LEDGER_STAKING_REWARD_FIAT) == k2g.SKIP_OTHER


def test_margin_bookkeeping_is_skipped(k2g):
    assert classify(k2g, kf.LEDGER_MARGIN) == k2g.SKIP_OTHER


def test_adjustment_is_reported_not_guessed(k2g):
    """Real balance impact, ambiguous semantics: surface it rather than invent."""
    assert classify(k2g, kf.LEDGER_ADJUSTMENT) == k2g.UNKNOWN


def test_wholly_unknown_type(k2g):
    assert classify(k2g, kf.LEDGER_WHOLLY_UNKNOWN_TYPE) == k2g.UNKNOWN


def test_negative_reward_is_reported_not_dropped(k2g):
    """An unstake or clawback needs eyes, not a silent debug line."""
    _, entry = only(kf.LEDGER_STAKING_NEGATIVE)
    classification, reason = k2g.classify_ledger_entry(entry)

    assert classification == k2g.UNKNOWN
    assert "non-positive" in reason


def test_reward_consumed_entirely_by_its_fee_is_not_income(k2g):
    entry = {"type": "earn", "subtype": "reward", "asset": "XXBT",
             "amount": "0.0001", "fee": "0.0001", "refid": "R1", "time": 1}
    assert k2g.classify_ledger_entry(entry)[0] == k2g.UNKNOWN


# ---------------------------------------------------------------------------
# Malformed input must not raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    {"type": "staking", "asset": "DOT", "amount": "0.5", "refid": "R1"},
    {"type": "staking", "subtype": None, "asset": "DOT", "amount": "0.5", "fee": None},
    {"type": "earn", "subtype": "reward", "asset": "XXBT", "amount": "", "fee": ""},
    {"type": "earn", "subtype": "reward", "asset": "XXBT"},
    {},
])
def test_malformed_entries_classify_without_raising(k2g, entry):
    classification, reason = k2g.classify_ledger_entry(entry)
    assert isinstance(classification, str)
    assert isinstance(reason, str)


def test_type_and_subtype_matching_is_case_insensitive(k2g):
    entry = {"type": "EARN", "subtype": "Reward", "asset": "XXBT.F",
             "amount": "0.01", "fee": "0", "refid": "R1"}
    assert k2g.classify_ledger_entry(entry)[0] == k2g.INGEST_REWARD


# ---------------------------------------------------------------------------
# find_internal_transfer_refids
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", [
    kf.LEDGER_EARN_ALLOCATION_PAIR,
    kf.LEDGER_EARN_DEALLOCATION_PAIR,
    kf.LEDGER_EARN_MIGRATION_PAIR,
    kf.LEDGER_TRANSFER_SPOT_TO_EARN,
])
def test_opposite_sign_legs_on_one_asset_are_flagged_internal(k2g, fixture):
    """-1.0 DOT and +1.0 DOT.S share a refid and normalise to the same asset."""
    refids = k2g.find_internal_transfer_refids(fixture)
    assert refids == {entry["refid"] for entry in fixture.values()}


def test_trade_legs_are_not_flagged(k2g):
    """A trade also pairs under one refid, but across two different assets."""
    assert k2g.find_internal_transfer_refids(kf.LEDGER_TRADE_XBTUSDC) == set()


def test_single_entry_refids_are_not_flagged(k2g):
    assert k2g.find_internal_transfer_refids(kf.LEDGER_EARN_REWARD_BTC) == set()
    assert k2g.find_internal_transfer_refids(kf.LEDGER_STAKING_REWARD_DOT) == set()


def test_netting_survives_an_unknown_subtype(k2g):
    """The structural rule is taxonomy-free, which is the whole point.

    A future Earn move Kraken labels with a subtype nobody has seen still nets
    to zero, because the rule looks at refids, assets and signs only.
    """
    ledger = {
        "LNEWA1-11111-22222": {"refid": "RNEW-1", "type": "earn", "subtype": "quantumshift",
                               "asset": "XXBT", "amount": "-0.05", "fee": "0", "time": 1},
        "LNEWB2-33333-44444": {"refid": "RNEW-1", "type": "earn", "subtype": "quantumshift",
                               "asset": "XXBT.B", "amount": "0.05", "fee": "0", "time": 1},
    }
    internal = k2g.find_internal_transfer_refids(ledger)

    assert internal == {"RNEW-1"}
    for entry in ledger.values():
        assert k2g.classify_ledger_entry(entry, internal)[0] == k2g.SKIP_INTERNAL_TRANSFER


def test_lone_leg_across_a_sync_since_boundary_is_caught_by_subtype(k2g):
    """When only one leg is inside the fetch window there is nothing to pair.

    The subtype sets exist as an independent second signal for exactly this,
    and either signal alone is enough to skip.
    """
    assert k2g.find_internal_transfer_refids(kf.LEDGER_TRANSFER_STRADDLE_LEG) == set()
    assert classify(k2g, kf.LEDGER_TRANSFER_STRADDLE_LEG) == k2g.SKIP_INTERNAL_TRANSFER


def test_refid_wins_over_a_reward_looking_type(k2g):
    """Structural evidence is checked first and cannot be overridden."""
    ledger = {
        "LFAKE1-11111-22222": {"refid": "RFAKE-1", "type": "staking", "subtype": "",
                               "asset": "DOT", "amount": "-1.0", "fee": "0", "time": 1},
        "LFAKE2-33333-44444": {"refid": "RFAKE-1", "type": "staking", "subtype": "",
                               "asset": "DOT.S", "amount": "1.0", "fee": "0", "time": 1},
    }
    internal = k2g.find_internal_transfer_refids(ledger)

    for entry in ledger.values():
        assert k2g.classify_ledger_entry(entry, internal)[0] == k2g.SKIP_INTERNAL_TRANSFER


# ---------------------------------------------------------------------------
# classify_ledger
# ---------------------------------------------------------------------------

@pytest.fixture
def mixed_ledger():
    combined = {}
    for fixture in (
        kf.LEDGER_STAKING_REWARD_DOT,
        kf.LEDGER_EARN_REWARD_BTC,
        kf.LEDGER_EARN_ALLOCATION_PAIR,
        kf.LEDGER_TRANSFER_SPOT_TO_EARN,
        kf.LEDGER_AIRDROP_BABY,
        kf.LEDGER_TRADE_XBTUSDC,
        kf.LEDGER_DEPOSIT_BTC,
        kf.LEDGER_WITHDRAWAL_BTC,
        kf.LEDGER_ADJUSTMENT,
    ):
        combined.update(fixture)
    return combined


def test_classify_ledger_buckets(k2g, mixed_ledger):
    buckets, histogram, unknowns = k2g.classify_ledger(mixed_ledger)

    assert set(buckets[k2g.INGEST_REWARD]) == {
        "L4UESK-KG3EQ-UFO4T5", "LKJH2M-QW3RT-9ZXCVB"}
    assert set(buckets[k2g.INGEST_AIRDROP]) == {"LB4BY9-XKQ2M-7NPRTV"}
    assert set(buckets[k2g.INGEST_DEPOSIT]) == {"LDEP1Q-2WSXC-3EDCVF"}
    assert set(buckets[k2g.INGEST_WITHDRAWAL]) == {"LWDR1A-2SDFG-3HJKLQ"}
    assert histogram[k2g.SKIP_INTERNAL_TRANSFER] == 4
    assert len(unknowns) == 1


def test_classify_ledger_totals_every_entry_exactly_once(k2g, mixed_ledger):
    buckets, histogram, _ = k2g.classify_ledger(mixed_ledger)

    assert sum(histogram.values()) == len(mixed_ledger)
    assert sum(len(bucket) for bucket in buckets.values()) == len(mixed_ledger)


def test_internal_transfers_net_to_zero(k2g, mixed_ledger):
    """Neither leg of an internal move reaches Ghostfolio, so nothing shifts."""
    buckets, _, _ = k2g.classify_ledger(mixed_ledger)
    ingested = set()
    for name in (k2g.INGEST_REWARD, k2g.INGEST_AIRDROP,
                 k2g.INGEST_DEPOSIT, k2g.INGEST_WITHDRAWAL):
        ingested |= set(buckets[name])

    for fixture in (kf.LEDGER_EARN_ALLOCATION_PAIR, kf.LEDGER_TRANSFER_SPOT_TO_EARN):
        assert not (ingested & set(fixture))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_unknown_entries_fail_the_run(k2g, capsys):
    """A new reward type must stop the run, not drift quietly."""
    _, _, unknowns = k2g.classify_ledger(dict(kf.LEDGER_ADJUSTMENT))

    k2g.report_unknown_ledger_entries(unknowns)

    out = capsys.readouterr().out
    assert "Unrecognised Kraken ledger entries" in out
    assert "adjustment" in out
    assert "LADJ9X-2QWER-5TYUIO" in out
    assert len(k2g.FAILURES) == 1


def test_no_unknowns_reports_nothing(k2g, capsys):
    k2g.report_unknown_ledger_entries([])

    assert capsys.readouterr().out == ""
    assert k2g.FAILURES == []
