"""Asset normalization, pair splitting and symbol/currency resolution.

These are pure functions over plain strings, so nothing here needs HTTP
mocking.  Written before any behaviour changed, to pin what the script already
does; the two xfail cases mark real bugs that a later step fixes.
"""

import pytest


# ---------------------------------------------------------------------------
# normalize_kraken_asset
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asset,expected", [
    # X-prefixed legacy names
    ("XXBT", "BTC"),
    ("XETH", "ETH"),
    ("XXDG", "DOGE"),
    ("XXRP", "XRP"),
    # Unprefixed aliases used in newer pair names
    ("XBT", "BTC"),
    ("XDG", "DOGE"),
    # Z-prefixed fiat
    ("ZUSD", "USD"),
    ("ZCHF", "CHF"),
    ("ZEUR", "EUR"),
    # Staked / bonded / rewards variants all collapse onto the base asset.
    # This collapse is load-bearing: it is why rewards credited to a bonded
    # balance land on the same Ghostfolio position as the spot holding, and
    # why internal transfers between the two must be skipped rather than
    # imported.
    ("DOT.S", "DOT"),
    ("XXBT.S", "BTC"),
    ("XXBT.M", "BTC"),
    ("XXBT.B", "BTC"),
    ("XXBT.F", "BTC"),
    ("SOL.F", "SOL"),
    # Only one suffix is stripped, and only a known one.
    ("ETH2.S", "ETH2"),
    ("BTC.X", "BTC.X"),
    # Newer assets pass through untouched.
    ("BABY", "BABY"),
    ("PEPE", "PEPE"),
    # Degenerate input must not raise.
    (".S", ""),
    ("", ""),
])
def test_normalize_kraken_asset(k2g, asset, expected):
    assert k2g.normalize_kraken_asset(asset) == expected


# ---------------------------------------------------------------------------
# split_kraken_pair
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pair,base,quote,confident", [
    # X???Z??? prefixed pairs
    ("XXBTZUSD", "BTC", "USD", True),
    ("XETHZEUR", "ETH", "EUR", True),
    ("XXDGZUSD", "DOGE", "USD", True),
    # Fiat suffix detection
    ("XBTCHF", "BTC", "CHF", True),
    ("DOTEUR", "DOT", "EUR", True),
    ("SOLUSD", "SOL", "USD", True),
    ("BABYUSD", "BABY", "USD", True),
    # Regression guard: the fiat stage must run before the stablecoin stage,
    # or DOTUSD splits as DO/TUSD.
    ("DOTUSD", "DOT", "USD", True),
    # Stablecoin quotes, longest suffix first
    ("XBTUSDC", "BTC", "USDC", True),
    ("ETHUSDT", "ETH", "USDT", True),
    ("XBTDAI", "BTC", "DAI", True),
    # Crypto quotes
    ("ETHXBT", "ETH", "BTC", True),
    # Midpoint fallback - flagged as not confident so it reaches the report
    ("SOLDOT", "SOL", "DOT", False),
    ("ETHDOT", "ETH", "DOT", False),
    ("WIFSOL", "WIF", "SOL", False),
])
def test_split_kraken_pair(k2g, pair, base, quote, confident):
    assert k2g.split_kraken_pair(pair) == (base, quote, confident)


@pytest.mark.parametrize("pair,base,quote", [
    ("XETHXXBT", "ETH", "BTC"),
    ("XXRPXXBT", "XRP", "BTC"),
    ("XLTCXXBT", "LTC", "BTC"),
    ("XREPXETH", "REP", "ETH"),
])
def test_x_prefixed_crypto_quoted_pairs_split_on_the_longest_suffix(k2g, pair, base, quote):
    """The classic X-prefixed crypto-quoted pairs must not lose a character.

    With crypto_quotes ordered shortest-first, XBT matched the tail of XXBT
    and every one of these split one character short - XETHXXBT as XETHX/BTC.
    They returned confident=True, so the bogus base never reached the unmapped
    report and a symbol like XETHXUSD was imported silently.
    """
    assert k2g.split_kraken_pair(pair) == (base, quote, True)


@pytest.mark.parametrize("pair,base,quote", [
    # Unprefixed crypto quotes must keep working after the reorder.
    ("AVAXXBT", "AVAX", "BTC"),
    ("SOLXBT", "SOL", "BTC"),
    ("MATICXBT", "MATIC", "BTC"),
    ("LINKETH", "LINK", "ETH"),
])
def test_unprefixed_crypto_quoted_pairs_are_unaffected(k2g, pair, base, quote):
    assert k2g.split_kraken_pair(pair) == (base, quote, True)


# ---------------------------------------------------------------------------
# normalize_quote_currency
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("quote,expected", [
    ("USDC", "USD"),
    ("USDT", "USD"),
    ("USDG", "USD"),
    ("USDQ", "USD"),
    ("DAI", "USD"),
    ("PYUSD", "USD"),
    ("RLUSD", "USD"),
    ("EURT", "EUR"),
    ("EURQ", "EUR"),
    ("EURR", "EUR"),
    ("ZUSD", "USD"),
    ("XBT", "BTC"),
])
def test_normalize_quote_currency(k2g, quote, expected):
    assert k2g.normalize_quote_currency(quote) == expected


def test_tusd_is_deliberately_not_a_stablecoin_quote(k2g):
    """TUSD is absent from the map on purpose: it collides with DOTUSD."""
    assert "TUSD" not in k2g.STABLECOIN_QUOTE_MAP
    assert k2g.normalize_quote_currency("TUSD") == "TUSD"


# ---------------------------------------------------------------------------
# quote_currency_from_symbol
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,expected", [
    ("BTCUSD", "USD"),
    ("BTC-USD", "USD"),
    ("btc-usd", "USD"),
    ("ETHCHF", "CHF"),
    # Not a recognisable fiat tail
    ("BTCUSDT", None),
    ("BTC", None),
    ("", None),
    (None, None),
])
def test_quote_currency_from_symbol(k2g, symbol, expected):
    assert k2g.quote_currency_from_symbol(symbol) == expected


# ---------------------------------------------------------------------------
# resolve_trade_currency
# ---------------------------------------------------------------------------

def test_kraken_quote_beats_the_mapped_symbol(k2g):
    """A CHF pair stays CHF even when mapped to a USD-quoted symbol.

    The price really is denominated in CHF; the mapped symbol only decides the
    asset profile.
    """
    assert k2g.resolve_trade_currency("XBTCHF", "BTC", "CHF", "BTCUSD") == "CHF"


def test_mapped_symbol_settles_a_non_currency_quote(k2g):
    assert k2g.resolve_trade_currency("XBTUSDC", "BTC", "USDC", "BTCUSD") == "USD"


def test_crypto_quote_without_mapping_stays_unusable(k2g):
    """ETHXBT keeps BTC and fails loudly rather than being relabelled USD.

    Silently calling a BTC-denominated price "USD" would corrupt the cost
    basis, so this is deliberate.
    """
    assert k2g.resolve_trade_currency("ETHXBT", "ETH", "BTC") == "BTC"


# ---------------------------------------------------------------------------
# resolve_symbol
# ---------------------------------------------------------------------------

def test_resolve_symbol_stablecoin_pair_is_already_iso4217(k2g):
    """XBTUSDC resolves cleanly; the ISO-4217 import rejection is fixed.

    Regression guard for the incident that rejected a whole import batch with
    "currency must be a valid ISO4217 currency code".
    """
    unmapped = {}
    assert k2g.resolve_symbol("XBTUSDC", {}, unmapped) == ("BTCUSD", "USD")
    assert unmapped == {}


@pytest.mark.parametrize("pair,expected", [
    ("XXBTZUSD", ("BTCUSD", "USD")),
    ("XBTCHF", ("BTCUSD", "CHF")),
    ("BABYUSD", ("BABYUSD", "USD")),
])
def test_resolve_symbol_confident_pairs(k2g, pair, expected):
    assert k2g.resolve_symbol(pair, {}, {}) == expected


def test_resolve_symbol_records_only_unconfident_pairs(k2g):
    unmapped = {}
    assert k2g.resolve_symbol("SOLDOT", {}, unmapped) == ("SOLUSD", "DOT")
    assert unmapped == {
        "SOLDOT": {"base": "SOL", "quote": "DOT", "yahoo": "SOLUSD"}
    }


def test_resolve_symbol_unmapped_is_idempotent(k2g):
    unmapped = {}
    k2g.resolve_symbol("SOLDOT", {}, unmapped)
    k2g.resolve_symbol("SOLDOT", {}, unmapped)
    assert len(unmapped) == 1


def test_resolve_symbol_mapping_is_returned_verbatim(k2g):
    assert k2g.resolve_symbol("XBTCHF", {"XBTCHF": "BTC-USD"}, {}) == ("BTC-USD", "CHF")


# ---------------------------------------------------------------------------
# resolve_staking_symbol
# ---------------------------------------------------------------------------

def test_resolve_staking_symbol_mapping_by_raw_and_normalized_key(k2g):
    assert k2g.resolve_staking_symbol("XXBT.F", {"XXBT.F": "BTC-USD"}, {}) == ("BTC-USD", "BTC")
    assert k2g.resolve_staking_symbol("XXBT.F", {"BTC": "BTC-USD"}, {}) == ("BTC-USD", "BTC")


def test_resolve_staking_symbol_skips_fiat(k2g):
    assert k2g.resolve_staking_symbol("ZUSD", {}, {}) == (None, "USD")


def test_reward_and_trade_resolve_to_the_same_asset_profile(k2g):
    """A reward on XXBT.F must land on the same symbol as an XXBTZUSD trade.

    The fallback used to return a bare "BTC" while trades resolve to
    "BTCUSD", which would have split one holding across two Ghostfolio asset
    profiles and made the reconcile look worse rather than better.
    """
    reward_symbol, normalized = k2g.resolve_staking_symbol("XXBT.F", {}, {})
    trade_symbol, _ = k2g.resolve_symbol("XXBTZUSD", {}, {})

    assert reward_symbol == trade_symbol == "BTCUSD"
    assert normalized == "BTC"


def test_resolve_staking_symbol_records_auto_derived_assets(k2g):
    unmapped_assets = {}

    assert k2g.resolve_staking_symbol("BABY", {}, unmapped_assets) == ("BABYUSD", "BABY")
    assert unmapped_assets == {"BABY": {"asset": "BABY", "symbol": "BABYUSD"}}


def test_resolve_staking_symbol_does_not_record_mapped_assets(k2g):
    unmapped_assets = {}

    k2g.resolve_staking_symbol("XXBT.F", {"BTC": "BTC-USD"}, unmapped_assets)

    assert unmapped_assets == {}


@pytest.mark.parametrize("asset", ["", ".S", "BTC.X"])
def test_resolve_staking_symbol_rejects_unusable_names(k2g, asset):
    """Never raise: an odd asset is reported, not allowed to abort the run."""
    symbol, _ = k2g.resolve_staking_symbol(asset, {}, {})
    assert symbol is None
