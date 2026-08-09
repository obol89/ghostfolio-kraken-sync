"""Source record -> Ghostfolio activity conversion, and comment identity.

Pure functions over plain dicts, so these patch nothing.
"""

import ghost_fixtures as gf
import kraken_fixtures as kf
import pytest

ACCOUNT = gf.GHOST_ACCOUNT_ID


def only(entries):
    """Unwrap a single-entry ledger fixture into (ledger_id, entry)."""
    (ledger_id, entry), = entries.items()
    return ledger_id, entry


# ---------------------------------------------------------------------------
# Comment identity
# ---------------------------------------------------------------------------
#
# The comment is the only deduplication key. If the string a converter writes
# ever diverges from the string main() checks against Ghostfolio, dedup misses
# everything and the next run re-imports the whole history. Both sides now go
# through kraken_comment(), and these tests pin the strings themselves.

@pytest.mark.parametrize("kind,expected", [
    ("TRADE", "KRAKEN#TXID123"),
    ("REWARD", "KRAKEN#REWARD#TXID123"),
    ("AIRDROP", "KRAKEN#AIRDROP#TXID123"),
    ("CONVERT", "KRAKEN#CONVERT#TXID123"),
    ("TRANSFER", "KRAKEN#XFER#TXID123"),
    ("TRADE_QUOTE", "KRAKEN#TRADEQ#TXID123"),
    ("TRADE_FEE", "KRAKEN#TRADEFEE#TXID123"),
    ("DEPOSIT", "KRAKEN#DEP#TXID123"),
    ("WITHDRAWAL", "KRAKEN#WDR#TXID123"),
    ("LEGACY_STAKE", "KRAKEN#STAKE#TXID123"),
])
def test_kraken_comment_strings(k2g, kind, expected):
    assert k2g.kraken_comment(kind, "TXID123") == expected


def test_every_ingesting_classification_has_a_namespace(k2g):
    for classification, kind in k2g.LEDGER_COMMENT_KIND.items():
        assert classification.startswith("INGEST_")
        assert kind in k2g.COMMENT_PREFIXES


def test_every_comment_is_matched_by_the_dedup_prefix_filter(k2g):
    """Every namespace must survive index_activities_by_comment's filter."""
    for kind in k2g.COMMENT_PREFIXES:
        comment = k2g.kraken_comment(kind, "X")
        assert comment.startswith("KRAKEN#")
        assert k2g.index_activities_by_comment([{"comment": comment}]) == {
            comment: {"comment": comment}}


def test_comment_namespaces_are_unambiguous(k2g):
    """No namespace may be a prefix of another with the same id space.

    KRAKEN# is the trade namespace and is deliberately a prefix of the others,
    so it is compared on the full built comment rather than the raw prefix.
    """
    built = {kind: k2g.kraken_comment(kind, "L4UESK-KG3EQ-UFO4T5")
             for kind in k2g.COMMENT_PREFIXES}
    assert len(set(built.values())) == len(built)


# ---------------------------------------------------------------------------
# convert_trade_to_activity
# ---------------------------------------------------------------------------

def test_trade_buy(k2g, config):
    trade_id = "TQXY7Z-4KLMN-8PQRST"
    activity = k2g.convert_trade_to_activity(
        trade_id, kf.KRAKEN_TRADES[trade_id], ACCOUNT, config, {}, {})

    assert activity == {
        "accountId": ACCOUNT,
        "comment": k2g.kraken_comment("TRADE", trade_id),
        "currency": "USD",
        "dataSource": "YAHOO",
        "date": "2025-03-16T00:01:01.553700+00:00",
        "fee": 1.33474,
        "quantity": 0.01,
        "symbol": "BTCUSD",
        "type": "BUY",
        "unitPrice": 83421.0,
    }


def test_trade_sell(k2g, config):
    trade = dict(kf.KRAKEN_TRADES["TQXY7Z-4KLMN-8PQRST"], type="sell")
    activity = k2g.convert_trade_to_activity("TSELL1", trade, ACCOUNT, config, {}, {})
    assert activity["type"] == "SELL"


def test_trade_date_is_utc_and_deterministic(k2g, config):
    """No local-timezone dependence: the converter passes tz=utc explicitly."""
    trade_id = "T7JHGF-2WERT-9ZXCVB"
    activity = k2g.convert_trade_to_activity(
        trade_id, kf.KRAKEN_TRADES[trade_id], ACCOUNT, config, {}, {})
    assert activity["date"].endswith("+00:00")


def test_stablecoin_quoted_trade_gets_an_iso4217_currency(k2g, config):
    """Regression guard for the batch rejected on ISO-4217 validation."""
    trade_id = "TQXY7Z-4KLMN-8PQRST"
    activity = k2g.convert_trade_to_activity(
        trade_id, kf.KRAKEN_TRADES[trade_id], ACCOUNT, config, {}, {})
    assert activity["currency"] in k2g.FIAT_CURRENCIES


def test_crypto_quoted_trade_keeps_its_unusable_currency(k2g, config):
    """ETHXBT stays BTC so the problem is visible rather than mislabelled."""
    trade_id = "T7JHGF-2WERT-9ZXCVB"
    activity = k2g.convert_trade_to_activity(
        trade_id, kf.KRAKEN_TRADES[trade_id], ACCOUNT, config, {}, {})
    assert activity["currency"] == "BTC"
    assert activity["currency"] not in k2g.FIAT_CURRENCIES


@pytest.mark.parametrize("trade", [
    {"pair": "", "type": "buy", "vol": "1.0", "price": "1", "time": "1", "fee": "0"},
    {"pair": "XXBTZUSD", "type": "buy", "vol": "0", "price": "1", "time": "1", "fee": "0"},
])
def test_trade_skipped_when_unusable(k2g, config, trade):
    assert k2g.convert_trade_to_activity("TX", trade, ACCOUNT, config, {}, {}) is None


# ---------------------------------------------------------------------------
# convert_reward_to_activity
# ---------------------------------------------------------------------------

def convert_reward(k2g, config, fixture, kind="REWARD", **kwargs):
    ledger_id, entry = only(fixture)
    kwargs.setdefault("mapping", {})
    kwargs.setdefault("unmapped_assets", {})
    return ledger_id, k2g.convert_reward_to_activity(
        ledger_id, entry, kind, ACCOUNT, config,
        kwargs["mapping"], kwargs["unmapped_assets"],
        kwargs.get("unresolved_assets"))


def test_reward_becomes_a_zero_price_buy(k2g, config):
    """The fix. Ghostfolio moves quantity for BUY and SELL and nothing else."""
    ledger_id, activity = convert_reward(k2g, config, kf.LEDGER_EARN_REWARD_BTC)

    assert activity["type"] == "BUY"
    assert activity["unitPrice"] == 0
    assert activity["quantity"] == pytest.approx(0.0155)
    assert activity["comment"] == k2g.kraken_comment("REWARD", ledger_id)


def test_reward_is_never_imported_as_interest(k2g, config):
    """Regression guard for the root cause of the whole change.

    getFactor() returns 0 for INTEREST, so such a row contributes no quantity;
    and with unitPrice 0 its value, quantity * unitPrice, is zero as well.
    """
    _, activity = convert_reward(k2g, config, kf.LEDGER_STAKING_REWARD_DOT)
    assert activity["type"] != "INTEREST"
    assert k2g.ghost_activity_factor(activity["type"]) == 1.0


def test_reward_lands_on_the_same_asset_profile_as_a_trade(k2g, config):
    """A bare "BTC" symbol here would split one holding into two positions."""
    _, activity = convert_reward(k2g, config, kf.LEDGER_EARN_REWARD_BTC)
    trade_activity = k2g.convert_trade_to_activity(
        "T1", {"pair": "XXBTZUSD", "type": "buy", "vol": "1", "price": "1",
               "fee": "0", "time": "1"}, ACCOUNT, config, {}, {})

    assert activity["symbol"] == trade_activity["symbol"] == "BTCUSD"
    assert activity["dataSource"] == "YAHOO"


def test_reward_quantity_is_net_of_the_kraken_fee(k2g, config):
    """The Kraken fee is in the asset, Ghostfolio's fee field is in currency.

    Copying a 0.0001 BTC fee into `fee` would book it as 0.0001 USD, so the
    fee is subtracted from the quantity and the field is left at zero.
    """
    _, activity = convert_reward(k2g, config, kf.LEDGER_EARN_REWARD_WITH_FEE)

    assert activity["quantity"] == pytest.approx(0.0099)
    assert activity["fee"] == 0


def test_reward_is_keyed_on_ledger_id_not_refid(k2g, config):
    """refid is shared across the legs of one event and is not unique."""
    ledger_id, entry = only(kf.LEDGER_EARN_REWARD_BTC)
    _, activity = convert_reward(k2g, config, kf.LEDGER_EARN_REWARD_BTC)

    assert ledger_id in activity["comment"]
    assert entry["refid"] not in activity["comment"]


def test_airdrop_uses_its_own_namespace(k2g, config):
    ledger_id, activity = convert_reward(
        k2g, config, kf.LEDGER_AIRDROP_BABY, kind="AIRDROP")

    assert activity["comment"] == k2g.kraken_comment("AIRDROP", ledger_id)
    assert activity["symbol"] == "BABYUSD"
    assert activity["type"] == "BUY"


def test_reward_namespace_differs_from_the_legacy_one(k2g, config):
    """Corrected rewards must not be skipped as duplicates of the inert rows."""
    ledger_id, activity = convert_reward(k2g, config, kf.LEDGER_STAKING_REWARD_BTC)

    assert activity["comment"] != k2g.kraken_comment("LEGACY_STAKE", ledger_id)


def test_reward_skips_fiat(k2g, config):
    _, activity = convert_reward(k2g, config, kf.LEDGER_STAKING_REWARD_FIAT)
    assert activity is None


def test_reward_skips_negative_amounts(k2g, config):
    _, activity = convert_reward(k2g, config, kf.LEDGER_STAKING_NEGATIVE)
    assert activity is None


def test_unresolvable_asset_is_recorded_and_does_not_raise(k2g, config):
    unresolved = set()
    entry = {"type": "earn", "subtype": "reward", "asset": "BTC.X",
             "amount": "1.0", "fee": "0", "time": 1, "refid": "R1"}

    activity = k2g.convert_reward_to_activity(
        "LBAD1Q-2WERT-3YUIOP", entry, "REWARD", ACCOUNT, config, {}, {}, unresolved)

    assert activity is None
    assert unresolved == {"BTC.X"}


def test_mapping_override_wins_for_a_reward(k2g, config):
    _, activity = convert_reward(
        k2g, config, kf.LEDGER_EARN_REWARD_BTC, mapping={"BTC": "BTC-USD"})

    assert activity["symbol"] == "BTC-USD"


# ---------------------------------------------------------------------------
# convert_crypto_transfer_to_activity
# ---------------------------------------------------------------------------

def test_deposit_becomes_a_zero_price_buy(k2g, config):
    ledger_id, entry = only(kf.LEDGER_DEPOSIT_BTC)
    activity = k2g.convert_crypto_transfer_to_activity(
        ledger_id, entry, "deposit", ACCOUNT, config, {}, {})

    assert activity["type"] == "BUY"
    assert activity["unitPrice"] == 0
    assert activity["quantity"] == 0.025
    assert activity["comment"] == k2g.kraken_comment("DEPOSIT", ledger_id)


def test_withdrawal_becomes_a_zero_price_sell_with_absolute_quantity(k2g, config):
    """A withdrawal costs the balance the amount plus its fee, both in-asset."""
    ledger_id, entry = only(kf.LEDGER_WITHDRAWAL_BTC)
    activity = k2g.convert_crypto_transfer_to_activity(
        ledger_id, entry, "withdrawal", ACCOUNT, config, {}, {})

    assert activity["type"] == "SELL"
    assert activity["unitPrice"] == 0
    assert activity["quantity"] == pytest.approx(0.00505)
    assert activity["comment"] == k2g.kraken_comment("WITHDRAWAL", ledger_id)


def test_withdrawal_quantity_includes_the_network_fee(k2g, config):
    """Kraken debits amount AND fee, both denominated in the asset.

    Selling only abs(amount) leaves the fee sitting in Ghostfolio forever;
    two BTC withdrawals were enough to show up as a 0.0000222 residual.
    """
    ledger_id, entry = only(kf.LEDGER_WITHDRAWAL_BTC_WITH_FEE)
    activity = k2g.convert_crypto_transfer_to_activity(
        ledger_id, entry, "withdrawal", ACCOUNT, config, {}, {})

    assert activity["quantity"] == pytest.approx(0.0200111)
    assert activity["quantity"] > 0.02
    assert activity["fee"] == 0


def test_deposit_quantity_excludes_the_fee_taken_out_of_it(k2g, config):
    """The mirror image: a deposit fee reduces what actually arrives."""
    ledger_id, entry = only(kf.LEDGER_DEPOSIT_BTC_WITH_FEE)
    activity = k2g.convert_crypto_transfer_to_activity(
        ledger_id, entry, "deposit", ACCOUNT, config, {}, {})

    assert activity["quantity"] == pytest.approx(0.009975)
    assert activity["quantity"] < 0.01
    assert activity["fee"] == 0


def test_withdrawal_fee_handling_does_not_depend_on_the_amount_sign(k2g, config):
    """Written per direction, so a positive-amount withdrawal is still right."""
    ledger_id, entry = only(kf.LEDGER_WITHDRAWAL_BTC_WITH_FEE)
    positive = dict(entry, amount="0.0200000000")

    signed = k2g.convert_crypto_transfer_to_activity(
        ledger_id, entry, "withdrawal", ACCOUNT, config, {}, {})
    unsigned = k2g.convert_crypto_transfer_to_activity(
        ledger_id, positive, "withdrawal", ACCOUNT, config, {}, {})

    assert signed["quantity"] == pytest.approx(unsigned["quantity"])


def test_transfer_skips_fiat(k2g, config):
    ledger_id, entry = only(kf.LEDGER_DEPOSIT_FIAT)
    assert k2g.convert_crypto_transfer_to_activity(
        ledger_id, entry, "deposit", ACCOUNT, config, {}, {}) is None


# ---------------------------------------------------------------------------
# Conversions (Kraken Convert, instant buy/sell, dust sweeping)
# ---------------------------------------------------------------------------

def convert_group(k2g, config, fixture, mapping=None, unresolved=None):
    return k2g.convert_group_to_activities(
        fixture, ACCOUNT, config, mapping or {}, {}, unresolved)


def by_comment(activities):
    return {activity["comment"]: activity for activity in activities}


def test_instant_buy_is_priced_from_its_fiat_leg(k2g, config):
    """1404.33 CHF for 0.0252729 BTC is a real cost basis, not a zero one.

    One activity is emitted, on the crypto leg. Importing this at unitPrice 0
    would discard the only price Kraken gave us and corrupt P&L.
    """
    activities = convert_group(k2g, config, kf.LEDGER_CONVERT_FIAT_TO_CRYPTO)

    assert len(activities) == 1
    activity = activities[0]
    assert activity["type"] == "BUY"
    assert activity["symbol"] == "BTCUSD"
    assert activity["quantity"] == pytest.approx(0.0252729)
    assert activity["currency"] == "CHF"
    assert activity["unitPrice"] == pytest.approx(1404.33 / 0.0252729)
    assert activity["comment"] == k2g.kraken_comment("CONVERT", "LCNVRC-XBT01-BBBBBB")


def test_instant_buy_drops_the_fiat_leg(k2g, config):
    """Cash belongs to the balance update, not to a position."""
    activities = convert_group(k2g, config, kf.LEDGER_CONVERT_FIAT_TO_CRYPTO)

    assert k2g.kraken_comment("CONVERT", "LCNVSP-CHF01-AAAAAA") not in by_comment(activities)


def test_instant_sell_is_priced_and_becomes_a_sell(k2g, config):
    activities = convert_group(k2g, config, kf.LEDGER_CONVERT_CRYPTO_TO_FIAT)

    assert len(activities) == 1
    activity = activities[0]
    assert activity["type"] == "SELL"
    assert activity["symbol"] == "XRPUSD"
    assert activity["quantity"] == pytest.approx(500.0)
    assert activity["currency"] == "CHF"
    assert activity["unitPrice"] == pytest.approx(1234.56 / 500.0)


def test_crypto_to_crypto_conversion_emits_both_legs_at_zero_price(k2g, config):
    """No fiat leg means no price is available for either side."""
    activities = convert_group(k2g, config, kf.LEDGER_CONVERT_CRYPTO_TO_CRYPTO)
    indexed = by_comment(activities)

    assert len(activities) == 2
    sold = indexed[k2g.kraken_comment("CONVERT", "LCNVSP-ATOM1-EEEEEE")]
    bought = indexed[k2g.kraken_comment("CONVERT", "LCNVRC-SOL01-FFFFFF")]

    assert sold["type"] == "SELL"
    assert sold["symbol"] == "ATOMUSD"
    assert sold["quantity"] == pytest.approx(12.5)
    assert bought["type"] == "BUY"
    assert bought["symbol"] == "SOLUSD"
    assert bought["quantity"] == pytest.approx(0.85)
    assert all(activity["unitPrice"] == 0 for activity in activities)


def test_dust_sweep_emits_one_activity_per_leg(k2g, config):
    """Many-to-one: two spend legs and one receive leg, three activities."""
    activities = convert_group(k2g, config, kf.LEDGER_CONVERT_DUSTSWEEP)
    indexed = by_comment(activities)

    assert len(activities) == 3
    assert indexed[k2g.kraken_comment("CONVERT", "LDUSTS-TRX01-GGGGGG")]["type"] == "SELL"
    assert indexed[k2g.kraken_comment("CONVERT", "LDUSTS-ALGO1-HHHHHH")]["type"] == "SELL"
    assert indexed[k2g.kraken_comment("CONVERT", "LDUSTR-XBT02-IIIIII")]["type"] == "BUY"
    assert all(activity["unitPrice"] == 0 for activity in activities)
    assert all(activity["currency"] == config["ghost_currency"] for activity in activities)


def test_fiat_to_fiat_conversion_emits_nothing(k2g, config):
    assert convert_group(k2g, config, kf.LEDGER_CONVERT_FIAT_TO_FIAT) == []


def test_fiat_with_a_state_suffix_is_treated_as_cash(k2g, config, caplog):
    """EUR.HOLD is euro pending settlement, not an unresolvable crypto asset.

    normalize_kraken_asset() does not strip .HOLD, so a fiat check against the
    normalized name alone reported it as unresolvable and warned on every run.
    """
    unresolved = set()
    activities = convert_group(k2g, config, kf.LEDGER_CONVERT_EUR_HOLD,
                               unresolved=unresolved)

    assert len(activities) == 1
    assert activities[0]["symbol"] == "SOLUSD"
    assert unresolved == set()
    assert not [r for r in caplog.records if "cannot be resolved" in r.message]


def test_fiat_with_a_state_suffix_still_prices_the_crypto_leg(k2g, config):
    """And the currency is the ISO code, not the raw "EUR.HOLD"."""
    activities = convert_group(k2g, config, kf.LEDGER_CONVERT_EUR_HOLD)

    assert activities[0]["currency"] == "EUR"
    assert activities[0]["currency"] in k2g.FIAT_CURRENCIES
    assert activities[0]["unitPrice"] == pytest.approx(250.0 / 1.5)


def test_conversion_comments_are_keyed_per_leg(k2g, config):
    """Each leg carries its own ledger id, so a partial import resumes cleanly."""
    activities = convert_group(k2g, config, kf.LEDGER_CONVERT_DUSTSWEEP)

    assert len(by_comment(activities)) == 3
    for activity in activities:
        assert activity["comment"].startswith("KRAKEN#CONVERT#")


def test_conversion_quantity_is_net_of_the_kraken_fee(k2g, config):
    group = {
        "LFEE01-SPEND-AAAAAA": {"refid": "RF1", "type": "spend", "subtype": "",
                                "asset": "ATOM", "amount": "-10.0", "fee": "0.1",
                                "time": 1742601600.0},
        "LFEE02-RECV-BBBBBBB": {"refid": "RF1", "type": "receive", "subtype": "",
                                "asset": "SOL", "amount": "1.0", "fee": "0.02",
                                "time": 1742601600.0},
    }
    indexed = by_comment(convert_group(k2g, config, group))

    assert indexed[k2g.kraken_comment("CONVERT", "LFEE01-SPEND-AAAAAA")]["quantity"] == pytest.approx(10.1)
    assert indexed[k2g.kraken_comment("CONVERT", "LFEE02-RECV-BBBBBBB")]["quantity"] == pytest.approx(0.98)
    assert all(activity["fee"] == 0 for activity in indexed.values())


def test_unresolvable_conversion_leg_is_recorded(k2g, config):
    unresolved = set()
    group = {
        "LBAD01-SPEND-AAAAAA": {"refid": "RB1", "type": "spend", "subtype": "",
                                "asset": "BTC.X", "amount": "-1.0", "fee": "0",
                                "time": 1742601600.0},
        "LBAD02-RECV-BBBBBBB": {"refid": "RB1", "type": "receive", "subtype": "",
                                "asset": "SOL", "amount": "1.0", "fee": "0",
                                "time": 1742601600.0},
    }

    activities = convert_group(k2g, config, group, unresolved=unresolved)

    assert len(activities) == 1
    assert unresolved == {"BTC.X"}


# ---------------------------------------------------------------------------
# Bare transfers
# ---------------------------------------------------------------------------

def test_bare_transfer_out_becomes_a_zero_price_sell(k2g, config, caplog):
    ledger_id, entry = only(kf.LEDGER_TRANSFER_BARE_NEGATIVE)

    activity = k2g.convert_transfer_to_activity(
        ledger_id, entry, ACCOUNT, config, {}, {})

    assert activity["type"] == "SELL"
    assert activity["unitPrice"] == 0
    assert activity["quantity"] == pytest.approx(1.5)
    assert activity["symbol"] == "LUNA2USD"
    assert activity["comment"] == k2g.kraken_comment("TRANSFER", ledger_id)
    assert any("Bare transfer" in record.message for record in caplog.records)


def test_bare_transfer_in_becomes_a_zero_price_buy(k2g, config):
    ledger_id, entry = only(kf.LEDGER_TRANSFER_NO_SUBTYPE)

    activity = k2g.convert_transfer_to_activity(
        ledger_id, entry, ACCOUNT, config, {}, {})

    assert activity["type"] == "BUY"
    assert activity["quantity"] == pytest.approx(2.0)
    assert activity["symbol"] == "USDCUSD"


def test_futures_transfer_in_becomes_a_buy(k2g, config):
    ledger_id, entry = only(kf.LEDGER_TRANSFER_SPOT_FROM_FUTURES)

    activity = k2g.convert_transfer_to_activity(
        ledger_id, entry, ACCOUNT, config, {}, {})

    assert activity["type"] == "BUY"
    assert activity["quantity"] == pytest.approx(0.0075)
    assert activity["symbol"] == "BTCUSD"
    assert activity["comment"] == k2g.kraken_comment("TRANSFER", ledger_id)


def test_futures_transfer_out_becomes_a_sell(k2g, config):
    ledger_id, entry = only(kf.LEDGER_TRANSFER_SPOT_TO_FUTURES)

    activity = k2g.convert_transfer_to_activity(
        ledger_id, entry, ACCOUNT, config, {}, {})

    assert activity["type"] == "SELL"
    assert activity["quantity"] == pytest.approx(0.003)


def test_trade_quote_leg_uses_its_own_namespace_and_warning(k2g, config, caplog):
    entry = kf.LEDGER_TRADE_XBTUSDC["LTRD5W-7ASDF-9GHJKL"]

    activity = k2g.convert_transfer_to_activity(
        "LTRD5W-7ASDF-9GHJKL", entry, ACCOUNT, config, {}, {}, kind="TRADE_QUOTE")

    assert activity["comment"] == k2g.kraken_comment("TRADE_QUOTE", "LTRD5W-7ASDF-9GHJKL")
    assert activity["type"] == "SELL"
    assert activity["symbol"] == "USDCUSD"
    assert activity["unitPrice"] == 0
    assert any("Trade quote leg" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Base-asset trade fees
# ---------------------------------------------------------------------------

def test_base_asset_fee_becomes_a_sell_of_exactly_the_fee(k2g, config):
    entry = kf.LEDGER_TRADE_BASE_FEES["L6VL2G-WRZIR-QO2PXY"]

    activity = k2g.convert_trade_fee_to_activity(
        "L6VL2G-WRZIR-QO2PXY", entry, ACCOUNT, config, {}, {})

    assert activity["type"] == "SELL"
    assert activity["quantity"] == pytest.approx(0.00000141)
    assert activity["unitPrice"] == 0
    assert activity["fee"] == 0
    assert activity["symbol"] == "BTCUSD"
    assert activity["currency"] == config["ghost_currency"]
    assert activity["comment"] == k2g.kraken_comment("TRADE_FEE", "L6VL2G-WRZIR-QO2PXY")


def test_the_corrections_sum_to_the_observed_residual(k2g, config):
    """The three real fees add up to the gap the reconcile could not explain."""
    total = 0.0
    for ledger_id, entry in kf.LEDGER_TRADE_BASE_FEES.items():
        total += k2g.convert_trade_fee_to_activity(
            ledger_id, entry, ACCOUNT, config, {}, {})["quantity"]

    assert total == pytest.approx(0.00002221)
    assert total == pytest.approx(kf.BASE_FEE_TOTAL)


def test_no_correction_when_the_fee_is_zero(k2g, config):
    """Regression guard: the common case must not emit an empty activity."""
    entry = kf.LEDGER_TRADE_XBTUSDC["LTRD4Q-6ZXWE-8CVBNM"]

    assert to_float_fee(entry) == 0
    assert k2g.convert_trade_fee_to_activity(
        "LTRD4Q-6ZXWE-8CVBNM", entry, ACCOUNT, config, {}, {}) is None


def to_float_fee(entry):
    return float(entry["fee"])


def test_the_trade_activity_itself_is_left_untouched(k2g, config):
    """Additive by design: changing vol would orphan imported activities."""
    trade_id = "TBFEE01-AAAAAA-000001"
    trade = k2g.convert_trade_to_activity(
        trade_id, kf.TRADES_WITH_BASE_FEES[trade_id], ACCOUNT, config, {}, {})

    assert trade["quantity"] == pytest.approx(0.0035)
    assert trade["comment"] == k2g.kraken_comment("TRADE", trade_id)


def test_quote_leg_already_nets_its_fee(k2g, config):
    """Verified unchanged: the TRADEQ path debits amount + fee."""
    entry = kf.LEDGER_TRADE_XBTUSDC["LTRD5W-7ASDF-9GHJKL"]

    activity = k2g.convert_transfer_to_activity(
        "LTRD5W-7ASDF-9GHJKL", entry, ACCOUNT, config, {}, {}, kind="TRADE_QUOTE")

    assert activity["quantity"] == pytest.approx(834.21 + 1.334736)


def test_bare_transfer_warns_on_every_occurrence(k2g, config, caplog):
    """Ambiguous semantics deserve a line each, not one summary."""
    for ledger_id, entry in kf.LEDGER_TRANSFER_BARE_NEGATIVE.items():
        k2g.convert_transfer_to_activity(ledger_id, entry, ACCOUNT, config, {}, {})
    for ledger_id, entry in kf.LEDGER_TRANSFER_NO_SUBTYPE.items():
        k2g.convert_transfer_to_activity(ledger_id, entry, ACCOUNT, config, {}, {})

    warnings = [r for r in caplog.records if "Bare transfer" in r.message]
    assert len(warnings) == 2


# ---------------------------------------------------------------------------
# validate_activities
# ---------------------------------------------------------------------------

def good_activity(**overrides):
    base = {
        "accountId": ACCOUNT, "comment": "KRAKEN#T1", "currency": "USD",
        "dataSource": "YAHOO", "date": "2025-01-01T00:00:00+00:00", "fee": 0,
        "quantity": 1.0, "symbol": "BTCUSD", "type": "BUY", "unitPrice": 1.0,
    }
    base.update(overrides)
    return base


def test_valid_activities_pass_through(k2g):
    importable, rejected = k2g.validate_activities([good_activity()])
    assert len(importable) == 1
    assert rejected == []


@pytest.mark.parametrize("overrides,fragment", [
    ({"currency": "BTC"}, "ISO 4217"),
    ({"currency": "USDC"}, "ISO 4217"),
    ({"symbol": ""}, "no symbol"),
    ({"quantity": 0}, "not positive"),
    ({"quantity": -1.0}, "not positive"),
    ({"type": "TRANSFER"}, "unknown activity type"),
])
def test_invalid_activities_are_held_back(k2g, overrides, fragment):
    """One bad activity must cost only itself, not the whole batch."""
    importable, rejected = k2g.validate_activities([good_activity(**overrides)])

    assert importable == []
    assert fragment in rejected[0][1]


def test_a_bad_activity_does_not_block_the_good_ones(k2g):
    activities = [good_activity(comment="KRAKEN#T%d" % index) for index in range(10)]
    activities.append(good_activity(comment="KRAKEN#TBAD", currency="BTC"))

    importable, rejected = k2g.validate_activities(activities)

    assert len(importable) == 10
    assert len(rejected) == 1


# ---------------------------------------------------------------------------
# Chunked import
# ---------------------------------------------------------------------------

IMPORT_URL = "/api/v1/import"


def test_import_is_chunked(k2g, config, http):
    from conftest import FakeResponse

    http.route(IMPORT_URL, FakeResponse({"message": "ok"}))
    activities = [good_activity(comment="KRAKEN#T%d" % index) for index in range(600)]

    assert k2g.ghost_import_activities(config, activities) is True

    assert http.count(IMPORT_URL) == 3
    sizes = [len(body["activities"]) for body in http.json_bodies(IMPORT_URL)]
    assert sizes == [250, 250, 100]


def test_a_rejected_chunk_does_not_stop_the_rest(k2g, config, http):
    """Successful chunks carry comments, so the next run resumes from there."""
    from conftest import FakeResponse

    statuses = [200, 400, 200]
    http.route(IMPORT_URL, lambda method, url, kwargs: FakeResponse(
        {"message": "x"}, status_code=statuses.pop(0)))
    activities = [good_activity(comment="KRAKEN#T%d" % index) for index in range(600)]

    assert k2g.ghost_import_activities(config, activities) is False

    assert http.count(IMPORT_URL) == 3
    assert len(k2g.FAILURES) == 1
    assert "batch 2/3" in k2g.FAILURES[0]


def test_empty_import_makes_no_request(k2g, config, http):
    assert k2g.ghost_import_activities(config, []) is True
    assert http.calls == []


def test_activities_are_imported_oldest_first(k2g, config, http):
    from conftest import FakeResponse

    http.route(IMPORT_URL, FakeResponse({"message": "ok"}))
    activities = [
        good_activity(comment="KRAKEN#TNEW", date="2025-06-01T00:00:00+00:00"),
        good_activity(comment="KRAKEN#TOLD", date="2024-01-01T00:00:00+00:00"),
    ]

    k2g.ghost_import_activities(config, activities)

    posted = http.json_bodies(IMPORT_URL)[0]["activities"]
    assert [item["comment"] for item in posted] == ["KRAKEN#TOLD", "KRAKEN#TNEW"]
