#!/usr/bin/env python3
"""Sync Kraken trades, staking rewards, and deposit/withdrawal activity to a self-hosted Ghostfolio instance."""

import argparse
import base64
import collections
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Failures recorded during a run. A non-empty list makes main() exit non-zero so
# the scheduler reports the job as failed even when the run completes.
FAILURES = []


def fail(msg, *args):
    """Log an error and mark the run as failed."""
    log.error(msg, *args)
    FAILURES.append(msg % args if args else msg)


def to_float(value, default=0.0):
    """Parse a Kraken numeric field, which arrives as a string.

    Tolerant by design: a missing, empty or malformed value yields the default
    rather than raising, so one odd ledger entry cannot abort a whole run.
    """
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


KRAKEN_API_BASE = "https://api.kraken.com"

# Page size for the paginated Ghostfolio activities endpoint
GHOST_PAGE_SIZE = 500

# Activities per POST to /api/v1/import. That endpoint is all-or-nothing, so
# this is the blast radius of a single activity Ghostfolio refuses.
GHOST_IMPORT_CHUNK_SIZE = 250

# Activity types Ghostfolio accepts. Only BUY and SELL move position quantity.
GHOST_ACTIVITY_TYPES = {"BUY", "SELL", "DIVIDEND", "INTEREST", "FEE", "ITEM", "LIABILITY"}

# Absolute backstop on Kraken pagination loops. Termination normally comes from
# an empty page or from the reported count, but an inconsistent server-side
# count must not be able to spin forever. 50 entries per page puts this at
# 100k trades or ledger entries, far beyond any real account.
MAX_KRAKEN_PAGES = 2000

# Kraken charges its API counter per call and lets it decay over time. Ledgers
# and TradesHistory cost 2 where Balance costs 1, so paginated calls to those
# endpoints are spaced proportionally further apart. On a starter-tier account
# (counter 15, decay 0.33/s) a cost-2 call sustains roughly one page every six
# seconds, which is what makes a full backfill take minutes rather than
# seconds.
LEDGER_COUNTER_COST = 2

# Kraken errors worth retrying: transient capacity and rate-limit conditions,
# as opposed to a permission or nonce problem, which retrying cannot fix.
KRAKEN_RETRYABLE_ERRORS = (
    "EAPI:Rate limit exceeded",
    "EGeneral:Temporary lockout",
    "EService:Unavailable",
    "EService:Busy",
)
KRAKEN_MAX_ATTEMPTS = 5
KRAKEN_BACKOFF_BASE = 2.0

# Kraken prefixed asset names to standard names
# Includes both X-prefixed (XXBT) and unprefixed (XBT) variants that Kraken
# uses in different contexts (asset names vs pair names)
KRAKEN_ASSET_MAP = {
    # X-prefixed crypto (old style asset names)
    "XXBT": "BTC",
    "XETH": "ETH",
    "XXRP": "XRP",
    "XLTC": "LTC",
    "XMLN": "MLN",
    "XXLM": "XLM",
    "XXDG": "DOGE",
    "XXMR": "XMR",
    "XZEC": "ZEC",
    "XREP": "REP",
    "XETC": "ETC",
    # Unprefixed variants (used in newer pair names like XBTCHF, XBTEUR)
    "XBT": "BTC",
    "XDG": "DOGE",
    "XLM": "XLM",
    "XMR": "XMR",
    "XRP": "XRP",
    # Z-prefixed fiat
    "ZUSD": "USD",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZCAD": "CAD",
    "ZJPY": "JPY",
    "ZAUD": "AUD",
    "ZCHF": "CHF",
}

FIAT_CURRENCIES = {"USD", "EUR", "GBP", "CAD", "JPY", "AUD", "CHF"}

# Stablecoin quote assets to the fiat currency they track.
#
# Kraken quotes a growing number of pairs in stablecoins (XBTUSDC, ETHUSDT).
# Those tickers are not ISO 4217 codes, and Ghostfolio rejects the whole import
# batch with "currency must be a valid ISO4217 currency code" if one reaches the
# activity's currency field, so they are reported as their pegged fiat instead.
#
# TUSD is deliberately absent: it collides with the tail of ordinary pairs such
# as DOTUSD, which must keep splitting as DOT/USD.
STABLECOIN_QUOTE_MAP = {
    "USDC": "USD",
    "USDT": "USD",
    "USDG": "USD",
    "USDQ": "USD",
    "DAI": "USD",
    "PYUSD": "USD",
    "RLUSD": "USD",
    "EURT": "EUR",
    "EURQ": "EUR",
    "EURR": "EUR",
}

# Comment prefixes, one namespace per kind of source record.
#
# The comment is the only deduplication key this script has: a run asks
# Ghostfolio for every existing comment and skips any source record whose
# comment is already there. That makes the exact string load-bearing, so it is
# built in exactly one place - kraken_comment() - rather than once at the
# dedup check and again inside each converter. Any drift between the two would
# make dedup miss everything and re-import the entire history as duplicates.
#
# LEGACY_STAKE is the namespace used by earlier versions for staking rewards
# imported as INTEREST activities. Those rows contribute no quantity, so
# corrected rewards are imported under REWARD instead and the legacy rows are
# only ever read, never written.
COMMENT_PREFIXES = {
    "TRADE": "KRAKEN#",
    "REWARD": "KRAKEN#REWARD#",
    "AIRDROP": "KRAKEN#AIRDROP#",
    "CONVERT": "KRAKEN#CONVERT#",
    "TRANSFER": "KRAKEN#XFER#",
    "TRADE_QUOTE": "KRAKEN#TRADEQ#",
    "TRADE_FEE": "KRAKEN#TRADEFEE#",
    "DEPOSIT": "KRAKEN#DEP#",
    "WITHDRAWAL": "KRAKEN#WDR#",
    "LEGACY_STAKE": "KRAKEN#STAKE#",
}


def kraken_comment(kind, identifier):
    """Build the deduplication comment for a source record.

    `identifier` is a Kraken trade id for TRADE, and a ledger id - the key of
    the Ledgers response map - for every other kind. It is never the entry's
    `refid`, which is shared across the legs of one parent transaction and
    would collapse two entries onto a single comment.
    """
    return COMMENT_PREFIXES[kind] + identifier


# ---------------------------------------------------------------------------
# Ledger classification
#
# Everything the classifier keys off lives in this block, so adjusting the
# taxonomy is a single edit here rather than a hunt through the dispatch.
#
# Provenance matters, because Kraken's own references disagree. The REST
# Ledgers endpoint documents this filter enum:
#
#   all, trade, deposit, withdrawal, transfer, margin, adjustment, rollover,
#   credit, settled, staking, dividend, sale, nft_rebate
#
# while the WebSocket balances channel enumerates a different set, adding
# reserve, conversion, reward and creator_fee. Neither lists `earn`, which is
# what Kraken Earn actually emits, neither lists `spend` or `receive`, which
# Kraken Convert and instant buy/sell emit, and neither enumerates subtypes
# beyond the six spot/staking/futures transfer values. So:
#
#   DOCUMENTED   the two type enums above, and the transfer subtypes
#   OBSERVED     staking, trade/tradespot, spend and receive (subtype absent
#                and `dustsweeping`), transfer/spotfromfutures, transfer with
#                an empty subtype, deposit, withdrawal - all confirmed against
#                a real account via --dump-ledger-types
#   INFERRED     `earn` and `airdrop` as types, and every subtype in
#                EARN_SUBTYPES_REWARD / EARN_SUBTYPES_INTERNAL /
#                SUBTYPES_AIRDROP. None of these appear on the account this
#                was verified against; they are kept as future-proofing.
#
# The inferred half is why anything unrecognised is reported and fails the run
# rather than being skipped, and why --dump-ledger-types exists: one run
# against a real account replaces the inference with fact.
# ---------------------------------------------------------------------------

INGEST_REWARD = "INGEST_REWARD"
INGEST_AIRDROP = "INGEST_AIRDROP"
INGEST_CONVERT = "INGEST_CONVERT"
INGEST_TRANSFER = "INGEST_TRANSFER"
INGEST_TRADE_QUOTE = "INGEST_TRADE_QUOTE"
INGEST_TRADE_FEE = "INGEST_TRADE_FEE"
INGEST_DEPOSIT = "INGEST_DEPOSIT"
INGEST_WITHDRAWAL = "INGEST_WITHDRAWAL"
SKIP_INTERNAL_TRANSFER = "SKIP_INTERNAL_TRANSFER"
SKIP_COVERED_BY_TRADES = "SKIP_COVERED_BY_TRADES"
SKIP_OTHER = "SKIP_OTHER"
UNKNOWN = "UNKNOWN"

# Comment namespace per ingesting classification, so the dedup check and the
# converters cannot disagree about which prefix a bucket uses.
LEDGER_COMMENT_KIND = {
    INGEST_REWARD: "REWARD",
    INGEST_AIRDROP: "AIRDROP",
    INGEST_CONVERT: "CONVERT",
    INGEST_TRANSFER: "TRANSFER",
    INGEST_TRADE_QUOTE: "TRADE_QUOTE",
    INGEST_TRADE_FEE: "TRADE_FEE",
    INGEST_DEPOSIT: "DEPOSIT",
    INGEST_WITHDRAWAL: "WITHDRAWAL",
}

# Kraken Convert, instant buy/sell and dust sweeping. Both legs of a
# conversion are ledger entries: `spend` for what left, `receive` for what
# arrived, sharing one refid.
#
# These are ledger-only events - their refids do not appear in TradesHistory -
# so there is no cross-namespace duplicate to guard against and they dedupe
# purely on their own KRAKEN#CONVERT# comments.
LEDGER_TYPES_CONVERT = {"spend", "receive"}

# Types that credit income in kind: the asset balance grows with no matching
# cash outflow. `staking` is legacy Kraken Staking, `earn` is Kraken Earn,
# `reward` appears in the WebSocket enumeration.
LEDGER_TYPES_REWARD = {"staking", "earn", "reward", "dividend"}

# `earn` subtypes. Only rewards are income; allocation, deallocation and
# migration move an existing balance between the spot and earn wallets.
EARN_SUBTYPES_REWARD = {"reward", "bonus", "yield"}
EARN_SUBTYPES_INTERNAL = {"allocation", "deallocation", "migration"}

# `transfer` subtypes that only shuffle a balance between wallets this tool
# already sees. Each is one half of a refid-paired +x / -x couple, both halves
# are in the spot ledger, and Balance reports the bonded side, so the pair
# nets to zero and both legs are skipped.
TRANSFER_SUBTYPES_INTERNAL = {
    "spottostaking",
    "stakingfromspot",
    "stakingtospot",
    "spotfromstaking",
}

# Futures transfers look like the staking ones but are not internal at all,
# and the asymmetry is worth stating plainly:
#
#   staking   both legs land in the spot ledger, and Balance includes the
#             bonded balance, so the move is invisible to reconciliation.
#   futures   Ledgers covers the spot wallet only and Balance excludes the
#             futures wallet, so only one leg is ever fetched. Money really
#             does leave or enter the holdings being reconciled.
#
# Treating these as internal would drop a real balance change on the floor.
TRANSFER_SUBTYPES_EXTERNAL = {
    "spottofutures",
    "spotfromfutures",
}

# Airdrops arrive either as a type of their own or as a subtype hung off some
# other type. The subtype set is matched before the type dispatch, because
# Kraken has filed airdrops under `deposit`, where SKIP_CRYPTO_TRANSFERS would
# otherwise discard them.
SUBTYPES_AIRDROP = {"airdrop"}
LEDGER_TYPES_AIRDROP = {"airdrop"}

# TradesHistory covers the BASE leg of these, and only the base leg.
#
# A trade writes two ledger entries: +base and -quote. The trade import turns
# the whole trade into one activity on the base asset, so ingesting the base
# leg again would double every traded position. The quote leg is a different
# matter. When the quote is fiat it is cash and belongs to the balance update,
# but when it is crypto or a stablecoin it is a real position that nothing
# else decreases - which is how a USDC balance climbed to +100.24 in
# Ghostfolio while only 0.40 remained on Kraken.
LEDGER_TYPES_COVERED_BY_TRADES = {"trade"}

LEDGER_TYPES_DEPOSIT = {"deposit"}
LEDGER_TYPES_WITHDRAWAL = {"withdrawal"}

# Margin bookkeeping, out of scope (see the README limitations).
LEDGER_TYPES_MARGIN = {"margin", "rollover", "settled"}

# NFT marketplace bookkeeping: no Ghostfolio asset profile exists for these.
LEDGER_TYPES_NFT = {"sale", "creator_fee", "nft_rebate", "nfttrade", "nftcreatorfee"}

# Internal bookkeeping with no net portfolio effect.
LEDGER_TYPES_INTERNAL = {"reserve", "custodytransfer"}

# Real balance impact but ambiguous semantics. Reported, never ingested, so a
# quantity difference stays visible instead of being invented or hidden. A
# delisting swap arrives as a `conversion` pair on two DIFFERENT assets, which
# the same-asset netting rule below cannot pair, so it lands here by design.
LEDGER_TYPES_REVIEW = {"adjustment", "conversion", "credit"}


def find_internal_transfer_refids(ledger):
    """Return the refids that only move an existing balance around.

    Kraken records a spot <-> earn move as two entries sharing one refid:
    -1.0 DOT out of spot and +1.0 DOT.S into the earn wallet.
    normalize_kraken_asset() strips the .S, so both legs resolve to the same
    Ghostfolio position and the pair nets to zero.

    The rule is structural: within one refid, two or more entries on the same
    NORMALIZED asset with opposite signs. A trade also pairs entries under one
    refid, but on two different assets (base in, quote out), so trades are
    untouched. A reward is a single entry per refid, so rewards are untouched.

    Being taxonomy-free is the point - it catches Earn subtypes Kraken has not
    shipped yet. It also has to be structural rather than sign-based: dropping
    only the negative leg, which a naive `amount <= 0` guard does, keeps the
    positive leg and invents quantity out of nothing.
    """
    by_refid = collections.defaultdict(lambda: collections.defaultdict(set))
    for entry in ledger.values():
        amount = to_float(entry.get("amount"))
        if amount == 0:
            continue
        asset = normalize_kraken_asset(entry.get("asset", ""))
        by_refid[entry.get("refid", "")][asset].add(amount > 0)

    return {
        refid for refid, assets in by_refid.items()
        if any(len(signs) > 1 for signs in assets.values())
    }


def group_convert_refids(ledger):
    """Group every spend/receive ledger entry by the refid it shares.

    A conversion is not always a pair. An instant buy is one spend and one
    receive, but a dust sweep is many-to-one: several small spend legs
    collapsing into a single receive leg. So this returns whole groups rather
    than pairs, and the converter decides what each group means.

    Returns {refid: {ledger_id: entry}}.
    """
    groups = collections.defaultdict(dict)
    for ledger_id, entry in ledger.items():
        if (entry.get("type") or "").lower() in LEDGER_TYPES_CONVERT:
            groups[entry.get("refid", "")][ledger_id] = entry
    return dict(groups)


def trade_base_assets(trades):
    """Map each trade id to the normalized base asset its import covers.

    A trade's ledger legs carry the trade id as their refid, so this is what
    lets the classifier tell the already-imported base leg from a quote leg
    nothing else accounts for.
    """
    bases = {}
    for trade_id, trade in trades.items():
        pair = trade.get("pair", "")
        if pair:
            bases[trade_id] = split_kraken_pair(pair)[0]
    return bases


def classify_ledger_entry(entry, internal_refids=frozenset(), convert_refids=frozenset(),
                          trade_bases=None):
    """Classify one raw Kraken ledger entry.

    Returns (classification, reason). Pure: no I/O and no globals, so the
    whole taxonomy is testable against fixtures.
    """
    entry_type = (entry.get("type") or "").lower()
    subtype = (entry.get("subtype") or "").lower()
    amount = to_float(entry.get("amount"))
    fee = to_float(entry.get("fee"))
    label = "%s/%s" % (entry_type or "-", subtype or "-")

    classification, reason = _classify_by_taxonomy(
        entry, entry_type, subtype, amount, fee, label, internal_refids, convert_refids,
        trade_bases or {})

    # Applied last so the histogram reflects Kraken's semantics rather than
    # our plumbing. Fiat moves the account cash balance, which the balance
    # update already handles; it is never a position.
    #
    # Conversions are exempt: the fiat leg of an instant buy is what makes the
    # crypto leg priceable, so it has to survive classification and be dropped
    # later by the group converter instead.
    if classification.startswith("INGEST_") and classification != INGEST_CONVERT:
        if is_fiat(entry.get("asset", "")):
            return SKIP_OTHER, "fiat, handled by the cash balance update"

    return classification, reason


def _classify_by_taxonomy(entry, entry_type, subtype, amount, fee, label,
                          internal_refids, convert_refids, trade_bases):
    """The type/subtype dispatch behind classify_ledger_entry."""
    # 1. A structurally proven internal move can never be read as income.
    if entry.get("refid", "") in internal_refids:
        return SKIP_INTERNAL_TRANSFER, "paired opposite-sign legs share one refid"

    if amount == 0 and fee == 0:
        return SKIP_OTHER, "zero amount"

    # 2. Subtype before type: an airdrop filed under `deposit` would otherwise
    #    be swallowed by SKIP_CRYPTO_TRANSFERS and lost.
    if subtype in SUBTYPES_AIRDROP:
        return INGEST_AIRDROP, "airdrop subtype"

    # 3. Kraken Convert, instant buy/sell and dust sweeping. What the group
    #    means depends on the other legs sharing its refid, so the decision is
    #    deferred to convert_group_to_activities. A group of one is a lone leg
    #    - a conversion straddling the SYNC_SINCE boundary - and cannot be
    #    priced or even given a direction, so it is reported instead.
    if entry_type in LEDGER_TYPES_CONVERT:
        if entry.get("refid", "") in convert_refids:
            return INGEST_CONVERT, "conversion leg (%s)" % label
        return UNKNOWN, "unpaired conversion leg, its counterpart is outside the fetch window"

    # 4. A bare `transfer` is also used for account-to-account moves and for
    #    delisting sweep-outs, both of which really do change holdings, as do
    #    futures transfers - the futures wallet is outside both this ledger
    #    and Balance, so only one leg is ever seen.
    if entry_type == "transfer":
        if subtype in TRANSFER_SUBTYPES_INTERNAL:
            return SKIP_INTERNAL_TRANSFER, "internal transfer subtype %s" % subtype
        if subtype in TRANSFER_SUBTYPES_EXTERNAL:
            return INGEST_TRANSFER, "futures transfer (%s), counterleg is outside the spot ledger" % subtype
        if not subtype:
            return INGEST_TRANSFER, "bare transfer, semantics ambiguous"
        return UNKNOWN, "unrecognised transfer subtype (%s)" % label

    # 5. Earn. Safe to ingest an unrecognised subtype only because step 1
    #    already removed the allocation-style moves structurally.
    if entry_type == "earn":
        if subtype in EARN_SUBTYPES_INTERNAL:
            return SKIP_INTERNAL_TRANSFER, "earn %s moves an existing balance" % subtype
        if subtype not in EARN_SUBTYPES_REWARD:
            return _classify_reward(amount, fee, "unrecognised earn subtype (%s)" % label)
        return _classify_reward(amount, fee, "earn reward")

    # 6. Trade legs. Kraken tags spot trade legs with subtype `tradespot`; the
    #    type settles which branch, the asset settles which leg.
    #
    #    Only the base leg is already imported. A non-fiat quote leg is a real
    #    position the trade import never touches, so it is ingested. Fiat quote
    #    legs fall through to the post-filter and are treated as cash.
    #
    #    When the trade is not in `trade_bases` - because it fell outside the
    #    fetch window, or because the caller had no trades to hand - the leg is
    #    assumed covered. Guessing the other way would double a position.
    if entry_type in LEDGER_TYPES_COVERED_BY_TRADES:
        base = trade_bases.get(entry.get("refid", ""))
        if base is None:
            return SKIP_COVERED_BY_TRADES, "trade leg, assumed covered by TradesHistory"
        if normalize_kraken_asset(entry.get("asset", "")) == base:
            # The amount is covered, but the fee may not be. Kraken sometimes
            # charges a trade fee in the BASE asset, and TradesHistory reports
            # `vol` gross, so the imported activity is larger than what the
            # balance actually moved. The leg stays covered for its amount;
            # only the fee needs a correcting activity.
            if fee > 0:
                return INGEST_TRADE_FEE, "base leg fee charged in the base asset"
            return SKIP_COVERED_BY_TRADES, "base leg, already imported from TradesHistory"
        return INGEST_TRADE_QUOTE, "quote leg of a trade, not covered by the trade import"

    if entry_type in LEDGER_TYPES_AIRDROP:
        return INGEST_AIRDROP, "airdrop"

    # 7. Everything else that credits income in kind.
    if entry_type in LEDGER_TYPES_REWARD:
        return _classify_reward(amount, fee, "%s reward" % entry_type)

    # 8. Real movements in and out of the account.
    if entry_type in LEDGER_TYPES_DEPOSIT:
        return INGEST_DEPOSIT, "deposit"
    if entry_type in LEDGER_TYPES_WITHDRAWAL:
        return INGEST_WITHDRAWAL, "withdrawal"

    # 9. Known and deliberately out of scope.
    if entry_type in LEDGER_TYPES_MARGIN:
        return SKIP_OTHER, "margin bookkeeping"
    if entry_type in LEDGER_TYPES_NFT:
        return SKIP_OTHER, "NFT marketplace bookkeeping"
    if entry_type in LEDGER_TYPES_INTERNAL:
        return SKIP_OTHER, "internal bookkeeping"

    # 10. Known but ambiguous, and 11. entirely unknown.
    if entry_type in LEDGER_TYPES_REVIEW:
        return UNKNOWN, "ambiguous ledger type needing review (%s)" % label
    return UNKNOWN, "unhandled ledger type (%s)" % label


def _classify_reward(amount, fee, reason):
    """Reward entries must credit something after the fee to count as income.

    A negative one is an unstake or a clawback. It is reported rather than
    dropped: silently discarding it is how quantity drifts unnoticed.
    """
    if amount - fee > 0:
        return INGEST_REWARD, reason
    return UNKNOWN, "non-positive reward (%s)" % reason


def classify_ledger(ledger, internal_refids=None, convert_refids=None, trade_bases=None):
    """Split a ledger into per-classification buckets.

    Returns (buckets, histogram, unknowns) where buckets maps a classification
    to {ledger_id: entry}, histogram counts each classification, and unknowns
    is a list of (ledger_id, entry, reason) for everything unrecognised.
    """
    if internal_refids is None:
        internal_refids = find_internal_transfer_refids(ledger)
    if convert_refids is None:
        # Only groups with more than one leg are conversions we can interpret.
        convert_refids = {refid for refid, group in group_convert_refids(ledger).items()
                          if len(group) > 1}

    buckets = collections.defaultdict(dict)
    histogram = collections.Counter()
    unknowns = []

    for ledger_id, entry in ledger.items():
        classification, reason = classify_ledger_entry(
            entry, internal_refids, convert_refids, trade_bases)
        buckets[classification][ledger_id] = entry
        histogram[classification] += 1
        if classification == UNKNOWN:
            unknowns.append((ledger_id, entry, reason))

    return buckets, histogram, unknowns


def report_unknown_ledger_entries(unknowns):
    """Report unrecognised ledger entries and mark the run as failed.

    An unrecognised entry may be a reward type Kraken has only just added. It
    would otherwise be dropped in silence, which is exactly how the quantity
    drift this script exists to fix went unnoticed in the first place, so the
    run is failed rather than merely annotated.
    """
    if not unknowns:
        return

    grouped = collections.defaultdict(list)
    for ledger_id, entry, reason in unknowns:
        key = ((entry.get("type") or "").lower(), (entry.get("subtype") or "").lower(), reason)
        grouped[key].append(ledger_id)

    print("\n" + "=" * 78)
    print("Unrecognised Kraken ledger entries. These were NOT imported, so any")
    print("quantity they carry is missing from Ghostfolio.")
    print()
    print(f"  {'TYPE':<14} {'SUBTYPE':<16} {'COUNT':>6}  EXAMPLE / REASON")
    for (entry_type, subtype, reason), ledger_ids in sorted(grouped.items()):
        print(f"  {entry_type or '-':<14} {subtype or '-':<16} {len(ledger_ids):>6}  "
              f"{ledger_ids[0]}")
        print(f"  {'':<14} {'':<16} {'':>6}  {reason}")
    print()
    print("Run with --dump-ledger-types to see the full taxonomy of this account.")
    print("=" * 78 + "\n")

    fail("%d unrecognised ledger entr%s were skipped (%s)",
         len(unknowns),
         "y" if len(unknowns) == 1 else "ies",
         ", ".join(sorted({"%s/%s" % (key[0] or "-", key[1] or "-") for key in grouped})))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Configuration is missing or malformed and the run cannot start.

    Raised instead of calling sys.exit() directly so that main() owns every
    exit code in one place, which also makes them assertable from tests.
    """


def required_env_vars(args=None):
    """Environment variables the selected mode actually needs.

    --dump-ledger-types reads the Kraken ledger and prints a histogram. It
    never contacts Ghostfolio, so demanding Ghostfolio credentials for it
    would make the diagnostic unusable in exactly the situation it is most
    useful: Ghostfolio down, unconfigured, or not yet set up.
    """
    required = ["KRAKEN_API_KEY", "KRAKEN_API_SECRET"]
    if args is not None and getattr(args, "dump_ledger_types", False):
        return required
    return required + ["GHOST_TOKEN", "GHOST_HOST"]


def load_config(args=None):
    """Load and validate configuration from environment variables."""
    missing = [k for k in required_env_vars(args) if not os.environ.get(k)]
    if missing:
        raise ConfigError("Missing required environment variables: %s" % ", ".join(missing))

    sync_since = os.environ.get("SYNC_SINCE", "")
    sync_since_ts = None
    if sync_since:
        try:
            sync_since_ts = datetime.fromisoformat(sync_since).replace(
                tzinfo=timezone.utc
            ).timestamp()
            log.info("SYNC_SINCE set to %s (timestamp %.0f)", sync_since, sync_since_ts)
        except ValueError:
            raise ConfigError(
                "Invalid SYNC_SINCE format: %s (expected ISO date like 2024-01-01)" % sync_since
            )

    return {
        "kraken_api_key": os.environ["KRAKEN_API_KEY"],
        "kraken_api_secret": os.environ["KRAKEN_API_SECRET"],
        # Absent only in modes that never call Ghostfolio; required_env_vars()
        # guarantees they are set for every mode that does.
        "ghost_token": os.environ.get("GHOST_TOKEN", ""),
        "ghost_host": os.environ.get("GHOST_HOST", "").rstrip("/"),
        "ghost_currency": os.environ.get("GHOST_CURRENCY", "USD"),
        "ghost_platform_id": os.environ.get("GHOST_PLATFORM_ID", ""),
        "ghost_account_name": os.environ.get("GHOST_ACCOUNT_NAME", "Kraken"),
        "mapping_file": os.environ.get("MAPPING_FILE", "/app/mapping.yaml"),
        "skip_crypto_transfers": os.environ.get("SKIP_CRYPTO_TRANSFERS", "true").lower() == "true",
        "api_call_delay": float(os.environ.get("API_CALL_DELAY", "1.0")),
        "sync_since_ts": sync_since_ts,
        "ledger_fetch_mode": os.environ.get("LEDGER_FETCH_MODE", "all").lower(),
    }


# ---------------------------------------------------------------------------
# Kraken API authentication
# ---------------------------------------------------------------------------

def kraken_signature(url_path, data, secret):
    """Generate Kraken API signature (HMAC-SHA512).

    Signature = HMAC-SHA512(url_path + SHA256(nonce + POST data), base64-decoded secret)
    """
    post_data = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + post_data).encode()
    message = url_path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class KrakenTransientError(Exception):
    """A Kraken failure that is worth retrying."""


_LAST_NONCE = 0


def kraken_nonce():
    """Return a strictly increasing millisecond nonce.

    A bare millisecond clock repeats when two calls land in the same
    millisecond, and Kraken rejects a nonce that is not greater than the
    previous one with EAPI:Invalid nonce. Keeping the last value and stepping
    past it makes that impossible.
    """
    global _LAST_NONCE
    _LAST_NONCE = max(int(time.time() * 1000), _LAST_NONCE + 1)
    return str(_LAST_NONCE)


def is_retryable_kraken_error(errors):
    return any(error.startswith(retryable)
               for error in errors
               for retryable in KRAKEN_RETRYABLE_ERRORS)


def kraken_request_once(config, url_path, data=None):
    """Issue one signed request to a Kraken private endpoint."""
    # A copy, so a retry does not resign a payload carrying a stale nonce and
    # so the caller's dict is left alone. Insertion order matters: nonce is
    # added last, exactly as the signature expects.
    payload = dict(data or {})
    payload["nonce"] = kraken_nonce()

    headers = {
        "API-Key": config["kraken_api_key"],
        "API-Sign": kraken_signature(url_path, payload, config["kraken_api_secret"]),
    }

    url = KRAKEN_API_BASE + url_path
    resp = requests.post(url, headers=headers, data=payload, timeout=30)

    if resp.status_code >= 500:
        raise KrakenTransientError("Kraken returned HTTP %d for %s" % (resp.status_code, url_path))
    resp.raise_for_status()
    result = resp.json()

    errors = result.get("error", [])
    if errors:
        message = "Kraken API error: %s" % ", ".join(errors)
        if is_retryable_kraken_error(errors):
            raise KrakenTransientError(message)
        raise RuntimeError(message)

    return result.get("result", {})


def kraken_request(config, url_path, data=None):
    """Make an authenticated request to the Kraken API, retrying transients.

    A full backfill is hundreds of paginated calls spread over many minutes.
    Without a retry a single rate-limit reply or 503 partway through discards
    everything fetched so far and kills the run with a traceback that bypasses
    the FAILURES tally entirely.
    """
    delay = KRAKEN_BACKOFF_BASE
    for attempt in range(1, KRAKEN_MAX_ATTEMPTS + 1):
        try:
            return kraken_request_once(config, url_path, data)
        except (KrakenTransientError, requests.Timeout, requests.ConnectionError) as exc:
            if attempt >= KRAKEN_MAX_ATTEMPTS:
                log.error("Kraken %s failed after %d attempts", url_path, attempt)
                raise
            log.warning("Kraken %s failed (attempt %d/%d): %s - retrying in %.1fs",
                        url_path, attempt, KRAKEN_MAX_ATTEMPTS, exc, delay)
            if delay > 0:
                time.sleep(delay)
            delay *= 2


def throttle(config, cost=1):
    """Pause between Kraken calls, scaled by the endpoint's counter cost."""
    delay = config.get("api_call_delay", 0.0) * cost
    if delay > 0:
        time.sleep(delay)


# ---------------------------------------------------------------------------
# Kraken data fetching (paginated)
# ---------------------------------------------------------------------------

def fetch_all_trades(config, end=None):
    """Fetch all trades from Kraken, handling pagination.

    `end` freezes the result window. Kraken returns newest first, so without
    it a trade landing mid-run shifts every later page down by one and an
    entry is skipped for good.
    """
    all_trades = {}
    offset = 0
    pages = 0

    while True:
        data = {"ofs": offset}
        if config["sync_since_ts"]:
            data["start"] = str(int(config["sync_since_ts"]))
        if end is not None:
            data["end"] = str(int(end))

        result = kraken_request(config, "/0/private/TradesHistory", data)
        trades = result.get("trades", {})
        count = result.get("count", 0)

        if not trades:
            break

        all_trades.update(trades)

        # Advance by what the page actually returned rather than by an assumed
        # page size. A short page while offset < count would otherwise skip
        # every entry between the real page end and the assumed one.
        offset += len(trades)
        pages += 1

        if offset >= count:
            break

        if pages >= MAX_KRAKEN_PAGES:
            fail("Stopped paginating trades after %d pages (count reported %d, "
                 "collected %d) - the reported count looks inconsistent",
                 pages, count, len(all_trades))
            break

        log.info("Fetched %d/%d trades...", len(all_trades), count)
        throttle(config, LEDGER_COUNTER_COST)

    log.info("Fetched %d total trades from Kraken", len(all_trades))
    return all_trades


def fetch_ledger_entries(config, ledger_type=None, end=None):
    """Fetch ledger entries from Kraken, handling pagination.

    With no `ledger_type` the whole ledger is fetched and classified locally,
    which is the only way to see Kraken Earn rewards at all: `earn` is not a
    member of the REST `type` filter enum, so no filtered request can return
    them. It is also cheaper than several filtered passes, each of which pays
    for its own partial final page against a counter cost of 2.

    The returned dict is keyed by ledger id, which is unique per entry. The
    `refid` inside each entry is the parent transaction id and is shared by
    every leg of one event, so it must never be used as a key here.

    `end` freezes the result window against inserts during a long backfill.
    """
    label = ledger_type or "all"
    all_entries = {}
    offset = 0
    pages = 0

    while True:
        data = {"ofs": offset}
        if ledger_type:
            data["type"] = ledger_type
        if config["sync_since_ts"]:
            data["start"] = str(int(config["sync_since_ts"]))
        if end is not None:
            data["end"] = str(int(end))

        result = kraken_request(config, "/0/private/Ledgers", data)
        entries = result.get("ledger", {})
        count = result.get("count", 0)

        if not entries:
            break

        all_entries.update(entries)

        # Advance by the real page length, not an assumed page size.
        offset += len(entries)
        pages += 1

        if offset >= count:
            break

        if pages >= MAX_KRAKEN_PAGES:
            fail("Stopped paginating %s ledger entries after %d pages (count "
                 "reported %d, collected %d) - the reported count looks inconsistent",
                 label, pages, count, len(all_entries))
            break

        log.info("Fetched %d/%d %s ledger entries...", len(all_entries), count, label)
        throttle(config, LEDGER_COUNTER_COST)

    log.info("Fetched %d %s ledger entries from Kraken", len(all_entries), label)
    return all_entries


def fetch_ledger_for_sync(config, end=None):
    """Fetch the ledger a sync will classify.

    The default is one unfiltered pass, because `earn` is absent from Kraken's
    REST type filter enum and no filtered request can return Earn rewards.

    LEDGER_FETCH_MODE=filtered restores the older behaviour of three separate
    staking/deposit/withdrawal passes. It exists for two reasons: it is the
    "before" side of an equivalence check that the unfiltered switch changed
    nothing for the categories already covered, and it is a one-variable
    rollback if the classifier misreads a taxonomy this account really uses.
    """
    if config.get("ledger_fetch_mode") == "filtered":
        log.info("LEDGER_FETCH_MODE=filtered: fetching staking, deposit and "
                 "withdrawal separately. Kraken Earn rewards and airdrops are "
                 "NOT visible in this mode.")
        ledger = {}
        for index, ledger_type in enumerate(("staking", "deposit", "withdrawal")):
            if index:
                throttle(config, LEDGER_COUNTER_COST)
            log.info("Fetching %s ledger entries...", ledger_type)
            ledger.update(fetch_ledger_entries(config, ledger_type, end=end))
        return ledger

    log.info("Fetching the full Kraken ledger...")
    return fetch_ledger_entries(config, end=end)


def warn_about_sync_since_cutoff(config, end=None):
    """Warn when SYNC_SINCE hides ledger entries from the sync.

    A truncated trade history is something the operator chose. A truncated
    reward history is different: the missing rewards are quantity that will
    never appear in Ghostfolio, and nothing downstream can tell that the
    shortfall was deliberate. One count-only call turns that into a visible
    warning.
    """
    if not config["sync_since_ts"]:
        return 0

    data = {"ofs": 0, "end": str(int(config["sync_since_ts"]))}
    try:
        result = kraken_request(config, "/0/private/Ledgers", data)
    except Exception as exc:
        log.warning("Could not check for ledger entries before SYNC_SINCE: %s", exc)
        return 0

    count = result.get("count", 0)
    if count:
        log.warning(
            "SYNC_SINCE excludes %d ledger entries older than the cutoff. Any "
            "staking or earn rewards among them will never be imported and "
            "your Ghostfolio quantities will stay short by that amount. Unset "
            "SYNC_SINCE to backfill the full history.",
            count,
        )
    return count


def fetch_balances(config):
    """Fetch current balances from Kraken."""
    return kraken_request(config, "/0/private/Balance")


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------

def load_mapping(path):
    """Load symbol mapping from a YAML file."""
    if not os.path.isfile(path):
        log.warning("Mapping file %s not found, proceeding without mappings", path)
        return {}
    with open(path, "r") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("symbol_mapping") or {}


def normalize_kraken_asset(asset):
    """Normalize a Kraken asset name to a standard name.

    1. Strip staking suffixes (.S, .M, .B, .F)
    2. Map known Kraken prefixed names to standard names
    3. Return as-is for newer assets with normal names
    """
    # Strip staking variant suffixes
    for suffix in (".S", ".M", ".B", ".F"):
        if asset.endswith(suffix):
            asset = asset[:-len(suffix)]
            break

    # Map known Kraken names
    if asset in KRAKEN_ASSET_MAP:
        return KRAKEN_ASSET_MAP[asset]

    return asset


def fiat_code(asset):
    """Return the ISO 4217 code an asset represents, or None if it is not fiat.

    normalize_kraken_asset() strips the staking-style suffixes it knows about,
    but Kraken hangs other state suffixes off a balance too - EUR.HOLD is euro
    pending settlement. An unrecognised suffix must not turn cash into a
    position, so anything before the first dot is tried as well.

    Returning the code rather than a bool matters at the call sites that need
    a currency: the fiat leg of a conversion prices the crypto leg, and
    "EUR.HOLD" is not something Ghostfolio will accept in a currency field.
    """
    normalized = normalize_kraken_asset(asset)
    if normalized in FIAT_CURRENCIES:
        return normalized

    base = normalize_kraken_asset(normalized.split(".", 1)[0])
    return base if base in FIAT_CURRENCIES else None


def is_fiat(asset):
    """True when `asset` is a fiat currency, whatever state suffix it carries."""
    return fiat_code(asset) is not None


def split_kraken_pair(pair):
    """Split a Kraken trading pair into base and quote assets.

    Returns (base, quote, confident) where confident indicates the split
    was done via a known pattern rather than a midpoint fallback.

    Kraken pairs can be:
    - XXBTZUSD (prefixed both sides)
    - XETHZEUR (prefixed both sides)
    - XBTCHF (unprefixed XBT + fiat)
    - DOTEUR (normal base, 3-char quote)
    - SOLUSD (normal base, 3-char quote)
    - XBTUSDC (normal base, stablecoin quote)

    The returned quote is the Kraken asset, not necessarily an ISO 4217 code -
    normalize_quote_currency turns it into one.
    """
    # Try known prefixed patterns first: X???Z??? (4+4 chars)
    if len(pair) == 8 and pair[:1] == "X" and pair[4:5] == "Z":
        base = pair[:4]
        quote = pair[4:]
        base_norm = normalize_kraken_asset(base)
        quote_norm = normalize_kraken_asset(quote)
        if base_norm != base or quote_norm != quote:
            return base_norm, quote_norm, True

    # Try splitting with known fiat suffixes (3-4 chars)
    for fiat_len in (4, 3):
        if len(pair) > fiat_len:
            potential_quote = pair[-fiat_len:]
            potential_base = pair[:-fiat_len]
            quote_norm = normalize_kraken_asset(potential_quote)
            if quote_norm in FIAT_CURRENCIES:
                base_norm = normalize_kraken_asset(potential_base)
                return base_norm, quote_norm, True

    # Try splitting with known stablecoin suffixes (3-5 chars).  Checked after
    # fiat so that DOTUSD still splits as DOT/USD rather than DO/TUSD.
    for quote_len in (5, 4, 3):
        if len(pair) > quote_len:
            potential_quote = pair[-quote_len:]
            potential_base = pair[:-quote_len]
            quote_norm = normalize_kraken_asset(potential_quote)
            if quote_norm in STABLECOIN_QUOTE_MAP:
                base_norm = normalize_kraken_asset(potential_base)
                return base_norm, quote_norm, True

    # Try known crypto quote currencies, longest suffix first.
    #
    # Ordering alone is not enough, because XETHXXBT (XETH/XXBT) and AVAXXBT
    # (AVAX/XBT) both end in "XXBT".  Kraken's convention breaks the tie: the
    # X-prefixed four-character quote only appears in fully prefixed pair
    # names, where the base is itself a known X-prefixed asset.
    #
    # Getting this wrong is silent.  Shortest-first split every classic pair
    # one character short - XETHXXBT as XETHX/BTC, XXRPXXBT as XXRPX/BTC,
    # XREPXETH as XREPX/ETH - and still returned confident=True, so the bogus
    # base never reached the unmapped report and a symbol like XETHXUSD was
    # imported without a word.
    crypto_quotes = ["XXBT", "XETH", "XBT", "ETH"]
    for cq in crypto_quotes:
        if pair.endswith(cq) and len(pair) > len(cq):
            base = pair[:-len(cq)]
            if len(cq) == 4 and base not in KRAKEN_ASSET_MAP:
                continue
            return normalize_kraken_asset(base), normalize_kraken_asset(cq), True

    # Fallback: try to split in the middle
    mid = len(pair) // 2
    base = normalize_kraken_asset(pair[:mid])
    quote = normalize_kraken_asset(pair[mid:])
    return base, quote, False


def normalize_quote_currency(quote):
    """Map a Kraken quote asset to the currency code Ghostfolio should see.

    Ghostfolio validates the activity currency against ISO 4217, so a stablecoin
    quote is reported as the fiat it tracks (USDC -> USD).  Anything already
    fiat, or unknown, is returned unchanged.
    """
    normalized = normalize_kraken_asset(quote)
    return STABLECOIN_QUOTE_MAP.get(normalized, normalized)


def quote_currency_from_symbol(symbol):
    """Extract the quote currency from a resolved symbol like BTCUSD or BTC-USD.

    Returns None when the symbol does not end in a known fiat code, so the
    caller can fall back to the Kraken pair.
    """
    if not symbol:
        return None
    candidate = symbol.rsplit("-", 1)[-1] if "-" in symbol else symbol[-3:]
    candidate = candidate.upper()
    return candidate if candidate in FIAT_CURRENCIES else None


def resolve_trade_currency(pair, base, quote, mapped_symbol=None):
    """Return the currency a trade in `pair` is denominated in.

    The Kraken quote wins whenever it resolves to a real currency, because it is
    what the trade price is actually expressed in - a CHF pair stays CHF even if
    it is mapped to a USD-quoted symbol.  Only when the quote is not a currency
    at all does the mapped symbol decide, so a mapping.yaml entry like
    XBTUSDC -> BTCUSD yields USD rather than the raw USDC that Ghostfolio
    rejects.

    mapped_symbol is passed only for pairs the user mapped explicitly.  Pairs
    quoted in crypto (ETHXBT) deliberately keep their unusable quote and fail
    the import loudly, rather than being silently relabelled as USD while the
    unit price is still denominated in BTC.
    """
    normalized = normalize_quote_currency(quote)
    if normalized in FIAT_CURRENCIES:
        return normalized

    from_mapping = quote_currency_from_symbol(mapped_symbol)
    if from_mapping:
        log.debug("Pair %s: quote %s is not a currency, using %s from the mapped symbol %s",
                  pair, quote, from_mapping, mapped_symbol)
        return from_mapping

    log.warning(
        "Pair %s has quote asset %s, which is not an ISO 4217 currency code. "
        "Ghostfolio will reject this activity - add a mapping.yaml entry for the "
        "pair pointing at a symbol quoted in a real currency (e.g. %s: %sUSD).",
        pair, normalized, pair, base,
    )
    return normalized


def resolve_symbol(pair, mapping, unmapped):
    """Resolve a Kraken trading pair to a Yahoo Finance symbol.

    Returns a (yahoo_symbol, trade_currency) tuple.
    - yahoo_symbol: always BASEUSD (e.g. BTCUSD, ETHUSD) because
      Ghostfolio with Yahoo data source uses this format for crypto.
    - trade_currency: the quote currency of the Kraken pair (CHF, EUR, USD,
      etc.) for the activity's currency field, resolved to an ISO 4217 code.

    mapping.yaml overrides take priority and are returned as-is.
    Only adds to unmapped if the pair could not be confidently resolved
    (i.e. fell through to the midpoint split fallback).
    """
    base, quote, confident = split_kraken_pair(pair)

    # Check mapping first (keyed by Kraken pair) - returned as-is.  The mapped
    # symbol also settles the currency when the raw quote is not a real one.
    if pair in mapping:
        mapped = mapping[pair]
        return mapped, resolve_trade_currency(pair, base, quote, mapped)

    # Ghostfolio + Yahoo uses BASEUSD format (no hyphen) for crypto
    yahoo_symbol = f"{base}USD"

    # Only track pairs where resolution fell back to the midpoint heuristic
    if not confident and pair not in unmapped:
        unmapped[pair] = {"base": base, "quote": quote, "yahoo": yahoo_symbol}

    return yahoo_symbol, resolve_trade_currency(pair, base, quote)


def resolve_staking_symbol(asset, mapping, unmapped_assets=None):
    """Resolve a bare Kraken asset - not a pair - to a Ghostfolio symbol.

    Returns (symbol, normalized_asset). `symbol` is None when the asset should
    be skipped: fiat, which the cash balance update already handles, or a name
    that cannot form a usable symbol.

    The fallback is f"{normalized}USD", the same shape resolve_symbol()
    produces for trades. That matters more than it looks: returning a bare
    "BTC" here while a trade resolves to "BTCUSD" would put rewards on a
    different Ghostfolio asset profile than the trades and split one holding
    into two positions.

    Note the second element is the normalized asset, not a currency, despite
    the naming symmetry with resolve_symbol().
    """
    normalized = normalize_kraken_asset(asset)

    # mapping.yaml is consulted under the raw Kraken name first (XXBT.F) and
    # then the normalized one (BTC), and is returned as-is either way.
    if asset in mapping:
        return mapping[asset], normalized
    if normalized in mapping:
        return mapping[normalized], normalized

    # Fiat is cash, not a position - including states like EUR.HOLD, whose
    # suffix normalize_kraken_asset() does not recognise.
    if is_fiat(asset):
        return None, normalized

    # An empty name, or one still carrying punctuation after suffix stripping,
    # cannot be concatenated into a symbol.
    if not normalized.isalnum():
        return None, normalized

    symbol = f"{normalized}USD"
    if unmapped_assets is not None and normalized not in unmapped_assets:
        unmapped_assets[normalized] = {"asset": asset, "symbol": symbol}
    return symbol, normalized


# ---------------------------------------------------------------------------
# Ghostfolio API helpers
# ---------------------------------------------------------------------------

def ghost_headers(token):
    """Return common headers for Ghostfolio API calls."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def ghost_get_accounts(config):
    """Fetch all accounts from Ghostfolio."""
    url = f"{config['ghost_host']}/api/v1/account"
    resp = requests.get(url, headers=ghost_headers(config["ghost_token"]), timeout=30)
    resp.raise_for_status()
    return resp.json()


def ghost_find_account_id(config, account_name):
    """Find a Ghostfolio account ID by name."""
    data = ghost_get_accounts(config)
    accounts = data.get("accounts", data) if isinstance(data, dict) else data
    for acc in accounts:
        if acc.get("name") == account_name:
            return acc["id"]
    raise LookupError(
        f"Ghostfolio account '{account_name}' not found. "
        f"Available: {[a['name'] for a in accounts]}"
    )


def ghost_fetch_all_activities(config):
    """Fetch every activity from Ghostfolio, following skip/take pagination.

    GET /api/v1/activities returns {"activities": [...], "count": N} where count
    is the total ignoring pagination. The server always appends a unique id
    tiebreaker to its sort order, so offset paging is stable.
    """
    url = f"{config['ghost_host']}/api/v1/activities"
    headers = ghost_headers(config["ghost_token"])
    collected = []
    skip = 0

    while True:
        resp = requests.get(url, headers=headers,
                            params={"skip": skip, "take": GHOST_PAGE_SIZE},
                            timeout=60)
        resp.raise_for_status()
        data = resp.json()

        page = data.get("activities") or []
        count = data.get("count", 0)
        collected.extend(page)

        # Guard against a non-terminating loop if count is ever inconsistent
        if not page or len(collected) >= count:
            break
        skip += len(page)

    log.info("Fetched %d existing activities from Ghostfolio", len(collected))
    return collected


def warn_if_comments_redacted(activities, matched):
    """Warn when comments look redacted, which would silently defeat dedup.

    Ghostfolio redacts activities[*].comment under impersonation or restricted
    view. If that happens every dedup set comes back empty and the next run
    re-imports the whole history as duplicates.
    """
    if activities and not matched and not any(a.get("comment") for a in activities):
        log.warning(
            "Ghostfolio returned %d activities but none carry a comment. If this "
            "instance uses restricted view, comments are redacted and duplicate "
            "detection will not work - importing may create duplicates.",
            len(activities),
        )


def index_activities_by_comment(activities):
    """Map Kraken comments to the activities carrying them.

    Returns {comment: activity} rather than a bare set of strings, so the
    legacy-row report and the reconcile can inspect type, quantity, id and
    account without triggering a second full page walk.
    """
    return {
        activity["comment"]: activity
        for activity in activities
        if (activity.get("comment") or "").startswith("KRAKEN#")
    }


def validate_activities(activities):
    """Split activities into (importable, rejected) before POSTing them.

    /api/v1/import is all-or-nothing, so one activity Ghostfolio's DTO refuses
    takes every other activity in the batch down with it. That is how a batch
    of trades quoted in a stablecoin was once lost wholesale. Filtering the
    known-bad ones out first means a single unusable pair costs only itself.
    """
    importable = []
    rejected = []
    for activity in activities:
        reason = None
        if activity.get("currency") not in FIAT_CURRENCIES:
            reason = ("currency %r is not an ISO 4217 code Ghostfolio accepts"
                      % activity.get("currency"))
        elif not activity.get("symbol"):
            reason = "no symbol"
        elif to_float(activity.get("quantity")) <= 0:
            reason = "quantity is not positive"
        elif activity.get("type") not in GHOST_ACTIVITY_TYPES:
            reason = "unknown activity type %r" % activity.get("type")

        if reason:
            rejected.append((activity, reason))
        else:
            importable.append(activity)
    return importable, rejected


def ghost_import_activities(config, activities, chunk_size=GHOST_IMPORT_CHUNK_SIZE):
    """Import activities into Ghostfolio, in chunks.

    Chunking bounds the damage of the all-or-nothing endpoint. A rejected
    chunk costs `chunk_size` activities rather than the whole run, and the
    chunks that did land now carry comments, so the next run's deduplication
    picks up exactly where this one stopped. It also keeps a first-run
    backfill of thousands of activities away from the 60 second timeout.

    Returns True when every chunk was accepted.
    """
    if not activities:
        log.info("No new activities to import")
        return True

    url = f"{config['ghost_host']}/api/v1/import"
    headers = ghost_headers(config["ghost_token"])
    ordered = sorted(activities, key=lambda activity: activity.get("date", ""))
    chunks = [ordered[start:start + chunk_size]
              for start in range(0, len(ordered), chunk_size)]

    if len(chunks) > 1:
        log.info("Importing %d activities in %d batches of up to %d",
                 len(ordered), len(chunks), chunk_size)

    imported = 0
    ok = True
    for index, chunk in enumerate(chunks, start=1):
        resp = requests.post(url, headers=headers, json={"activities": chunk}, timeout=60)
        if resp.status_code >= 400:
            fail("Import batch %d/%d failed (%d): %s",
                 index, len(chunks), resp.status_code, resp.text)
            log.error("Check your mapping file - a symbol may not be recognised by Ghostfolio")
            ok = False
            continue
        imported += len(chunk)

    log.info("Successfully imported %d of %d activities", imported, len(ordered))
    return ok


def kraken_cash_balance(balances, currency):
    """Sum every Kraken balance denominated in `currency`.

    Suffixed states of the same currency count too: USD, USD.F and USD.HOLD
    are all dollars.
    """
    total = 0.0
    for asset, raw in balances.items():
        if fiat_code(asset) == currency:
            total += to_float(raw)
    return total


def ghost_update_cash_balance(config, account_id, balances):
    """Update the cash balance on a Ghostfolio account.

    The balance is summed here, against the currency read from the account
    itself, rather than being passed in. The account's `balance` column is
    denominated in the account's own currency, so deriving the figure and the
    currency label from the same GET is what stops them disagreeing.

    Summing against GHOST_CURRENCY instead is what caused a CHF account to be
    overwritten with its owner's USD balance: GHOST_CURRENCY defaults to USD,
    the account was CHF, and nothing compared the two.

    The GET response carries far more than the update DTO accepts (aggregations,
    relations, timestamps).  Ghostfolio 3.x validates bodies with
    forbidNonWhitelisted, so echoing it back is a hard 400.  The payload is
    therefore built explicitly from the five fields UpdateAccountDto requires:
    balance, currency, id, name and platformId (nullable).

    comment, tags and isExcluded are optional in the DTO and deliberately left
    out - Prisma does not touch a column that is absent from the update, so
    omitting them preserves the stored values.  For isExcluded that also keeps
    the payload portable: it was a deprecated DTO field up to Ghostfolio 3.38.0
    and removed in 3.39.0, so sending it fails outright on newer instances.
    """
    url = f"{config['ghost_host']}/api/v1/account/{account_id}"
    resp = requests.get(url, headers=ghost_headers(config["ghost_token"]), timeout=30)
    resp.raise_for_status()
    account_data = resp.json()

    currency = account_data["currency"]
    if config.get("ghost_currency") and config["ghost_currency"] != currency:
        log.warning(
            "GHOST_CURRENCY is %s but the Ghostfolio account '%s' is denominated "
            "in %s. Using %s for the cash balance, since that is the currency "
            "the account's balance column is stored in.",
            config["ghost_currency"], account_data.get("name", account_id),
            currency, currency)

    balance = kraken_cash_balance(balances, currency)

    payload = {
        "balance": balance,
        "currency": currency,
        "id": account_id,
        "name": account_data["name"],
        "platformId": account_data.get("platformId") or config.get("ghost_platform_id") or None,
    }
    resp = requests.put(url, headers=ghost_headers(config["ghost_token"]),
                        json=payload, timeout=30)
    if resp.status_code >= 400:
        fail("Failed to update cash balance (%d): %s", resp.status_code, resp.text)
    else:
        log.info("Updated cash balance for account %s to %.2f %s",
                 account_id, balance, currency)


# ---------------------------------------------------------------------------
# Activity conversion
# ---------------------------------------------------------------------------

def convert_trade_to_activity(trade_id, trade, ghost_account_id, config, mapping, unmapped):
    """Convert a Kraken trade dict to a Ghostfolio activity dict.

    Returns None if the trade should be skipped.
    """
    pair = trade.get("pair", "")
    trade_type = trade.get("type", "")  # buy or sell
    price = float(trade.get("price", "0"))
    vol = float(trade.get("vol", "0"))
    fee = float(trade.get("fee", "0"))
    trade_time = float(trade.get("time", "0"))

    if not pair or vol == 0:
        return None

    yahoo_symbol, quote_currency = resolve_symbol(pair, mapping, unmapped)

    activity_type = "BUY" if trade_type == "buy" else "SELL"
    iso_date = datetime.fromtimestamp(trade_time, tz=timezone.utc).isoformat()

    comment = kraken_comment("TRADE", trade_id)

    return {
        "accountId": ghost_account_id,
        "comment": comment,
        "currency": quote_currency,
        "dataSource": "YAHOO",
        "date": iso_date,
        "fee": fee,
        "quantity": vol,
        "symbol": yahoo_symbol,
        "type": activity_type,
        "unitPrice": price,
    }


def convert_reward_to_activity(ledger_id, entry, kind, ghost_account_id, config,
                               mapping, unmapped_assets, unresolved_assets=None):
    """Convert a reward or airdrop ledger entry to a zero-price Ghostfolio BUY.

    Why BUY and not INTEREST, which this used to emit:

    Ghostfolio's getFactor(activityType) returns +1 for BUY, -1 for SELL and 0
    for everything else, and a position's quantity accumulates as
    quantity.mul(factor). An INTEREST activity therefore contributes exactly
    zero quantity no matter what its `quantity` field says. Every staking
    reward imported by earlier versions of this script is invisible to the
    portfolio, and the holdings are short by the entire reward history.

    INTEREST cannot carry the value either: Ghostfolio computes
    interest = quantity * unitPrice, and this script has always sent
    unitPrice 0, so those rows are inert in both dimensions.

    There is a third reason. Ghostfolio forces every NON_INVESTMENT_ACTIVITY_TYPE
    - which includes INTEREST - onto DataSource.MANUAL with a UUID or GF_
    prefixed symbol at import time. BUY is in INVESTMENT_ACTIVITY_TYPES, so it
    may keep dataSource YAHOO and a plain BTCUSD symbol, which puts the reward
    on the SAME asset profile as the trades instead of splitting the position.

    The trade-off, stated plainly: a zero unitPrice records the reward at a
    zero cost basis. Quantity and market value become correct, while
    realised-gain figures treat the reward as pure profit and it stops being
    categorised as income. Quantity correctness wins, because a wrong holding
    corrupts every number downstream of it. Pricing each reward at its market
    value on receipt would need a public OHLC call per reward, which is
    deliberately not done.

    Quantity is amount - fee rather than amount: Kraken settles a ledger entry
    as balance_new = balance_old + amount - fee, with the fee denominated in
    the entry's OWN asset. Putting that crypto fee into the activity's `fee`
    field would charge it as though it were fiat.

    Returns None if the entry should be skipped.
    """
    asset = entry.get("asset", "")
    amount = to_float(entry.get("amount"))
    fee = to_float(entry.get("fee"))
    entry_time = to_float(entry.get("time"))

    quantity = amount - fee
    if quantity <= 0:
        log.debug("Skipping ledger entry %s: nothing credited after the fee "
                  "(amount %.8f, fee %.8f, asset %s)", ledger_id, amount, fee, asset)
        return None

    symbol, normalized = resolve_staking_symbol(asset, mapping, unmapped_assets)
    if not symbol:
        if is_fiat(asset):
            log.debug("Skipping ledger entry %s: fiat asset %s", ledger_id, asset)
        else:
            log.warning("Ledger entry %s credits %.8f of %r, which cannot be "
                        "resolved to a symbol - it will be missing from Ghostfolio",
                        ledger_id, quantity, asset)
            if unresolved_assets is not None:
                unresolved_assets.add(asset or "<empty>")
        return None

    return {
        "accountId": ghost_account_id,
        "comment": kraken_comment(kind, ledger_id),
        "currency": config["ghost_currency"],
        "dataSource": "YAHOO",
        "date": datetime.fromtimestamp(entry_time, tz=timezone.utc).isoformat(),
        # The Kraken fee is already subtracted from the quantity above; it is
        # denominated in the asset, not in this activity's currency.
        "fee": 0,
        "quantity": quantity,
        "symbol": symbol,
        "type": "BUY",
        "unitPrice": 0,
    }


def ledger_leg(ledger_id, entry):
    """Normalize one ledger entry into the fields every converter needs.

    Quantity is the net balance movement: Kraken settles an entry as
    balance_new = balance_old + amount - fee, and the fee is denominated in
    the entry's own asset, so it belongs in the quantity rather than in the
    activity's currency-denominated `fee` field.
    """
    asset = entry.get("asset", "")
    normalized = normalize_kraken_asset(asset)
    net = to_float(entry.get("amount")) - to_float(entry.get("fee"))
    return {
        "ledger_id": ledger_id,
        "asset": asset,
        "normalized": normalized,
        "net": net,
        "quantity": abs(net),
        # Suffix-tolerant, so a state like EUR.HOLD is recognised as cash
        # instead of being warned about as an unresolvable symbol. fiat_code
        # is what a priced conversion leg uses as its currency: "EUR.HOLD" is
        # not something Ghostfolio accepts there.
        "fiat_code": fiat_code(asset),
        "is_fiat": is_fiat(asset),
        "time": to_float(entry.get("time")),
    }


def convert_group_to_activities(group, ghost_account_id, config, mapping,
                                unmapped_assets, unresolved_assets=None):
    """Convert one Kraken Convert / instant buy / dust-sweep group.

    `group` is {ledger_id: entry} for every spend and receive leg sharing one
    refid. Each emitted activity is commented with its OWN leg's ledger id, so
    a partially imported group resumes cleanly on the next run.

    One fiat leg against one crypto leg is an instant buy or sell, and it is
    the one case here with a real price: the fiat leg IS the cost basis. A
    single priced activity is emitted on the crypto leg at
    unitPrice = |fiat| / crypto quantity, denominated in that fiat currency,
    and the fiat leg is dropped because cash is the balance update's job.
    Importing these at unitPrice 0 instead would throw away the one piece of
    price information Kraken gave us and corrupt every P&L figure downstream.

    Everything else - crypto-to-crypto conversions, and dust sweeps where many
    spend legs collapse into one receive leg - gets one zero-unitPrice
    activity per crypto leg. The same trade-off applies as for rewards: the
    quantities are right and the cost basis is zero, which overstates
    unrealised gain but never corrupts the holding itself. Pricing them would
    need a market lookup per leg at the conversion timestamp.

    A group whose legs are all fiat is a currency exchange and produces
    nothing at all.
    """
    legs = [ledger_leg(ledger_id, entry) for ledger_id, entry in sorted(group.items())]
    legs = [leg for leg in legs if leg["quantity"] > 0]
    if not legs:
        return []

    crypto_legs = [leg for leg in legs if not leg["is_fiat"]]
    fiat_legs = [leg for leg in legs if leg["is_fiat"]]

    if not crypto_legs:
        log.debug("Skipping conversion group %s: every leg is fiat",
                  ", ".join(leg["ledger_id"] for leg in legs))
        return []

    if len(crypto_legs) == 1 and len(fiat_legs) == 1:
        leg = crypto_legs[0]
        fiat = fiat_legs[0]
        return _build_convert_activity(
            leg, ghost_account_id, config, mapping, unmapped_assets, unresolved_assets,
            unit_price=fiat["quantity"] / leg["quantity"],
            currency=fiat["fiat_code"])

    activities = []
    for leg in crypto_legs:
        activities.extend(_build_convert_activity(
            leg, ghost_account_id, config, mapping, unmapped_assets, unresolved_assets,
            unit_price=0, currency=config["ghost_currency"]))
    return activities


def _build_convert_activity(leg, ghost_account_id, config, mapping, unmapped_assets,
                            unresolved_assets, unit_price, currency):
    """One conversion leg to a list holding zero or one activity."""
    symbol, _ = resolve_staking_symbol(leg["asset"], mapping, unmapped_assets)
    if not symbol:
        log.warning("Conversion leg %s moves %.8f of %r, which cannot be resolved "
                    "to a symbol - it will be missing from Ghostfolio",
                    leg["ledger_id"], leg["quantity"], leg["asset"])
        if unresolved_assets is not None:
            unresolved_assets.add(leg["asset"] or "<empty>")
        return []

    return [{
        "accountId": ghost_account_id,
        "comment": kraken_comment("CONVERT", leg["ledger_id"]),
        "currency": currency,
        "dataSource": "YAHOO",
        "date": datetime.fromtimestamp(leg["time"], tz=timezone.utc).isoformat(),
        # Already netted into the quantity, and denominated in the asset.
        "fee": 0,
        "quantity": leg["quantity"],
        "symbol": symbol,
        "type": "BUY" if leg["net"] > 0 else "SELL",
        "unitPrice": unit_price,
    }]


def convert_transfer_to_activity(ledger_id, entry, ghost_account_id, config,
                                 mapping, unmapped_assets, unresolved_assets=None,
                                 kind="TRANSFER"):
    """Convert a transfer or trade-quote ledger entry to a zero-price activity.

    Three cases share this shape, because in all three the quantity is known
    and the price is not:

      bare transfer   Kraken gives no subtype. Used for account-to-account
                      moves and delisting sweep-outs.
      futures move    spottofutures / spotfromfutures. The futures wallet is
                      outside both Ledgers and Balance, so only one leg is
                      ever fetched and it is a genuine balance change.
      trade quote     the non-fiat quote leg of a trade. The trade import
                      emits the base asset only, so nothing else moves this.

    A zero unitPrice keeps the position size correct at the cost of a zero
    cost basis. Every one is logged, since none of them can be priced.
    """
    leg = ledger_leg(ledger_id, entry)
    if leg["quantity"] <= 0:
        return None

    symbol, _ = resolve_staking_symbol(leg["asset"], mapping, unmapped_assets)
    if not symbol:
        log.warning("Ledger entry %s moves %.8f of %r, which cannot be resolved to a "
                    "symbol - it will be missing from Ghostfolio",
                    ledger_id, leg["quantity"], leg["asset"])
        if unresolved_assets is not None:
            unresolved_assets.add(leg["asset"] or "<empty>")
        return None

    direction = "in" if leg["net"] > 0 else "out"
    if kind == "TRADE_QUOTE":
        log.warning("Trade quote leg %s: %.8f %s %s - the trade import only covers "
                    "the base asset, so this leg is imported at unitPrice 0",
                    ledger_id, leg["quantity"], leg["normalized"], direction)
    else:
        log.warning("Bare transfer %s: %.8f %s %s - Kraken gives no usable price for "
                    "these, so the cost basis is unknown and unitPrice is set to 0",
                    ledger_id, leg["quantity"], leg["normalized"], direction)

    return {
        "accountId": ghost_account_id,
        "comment": kraken_comment(kind, ledger_id),
        "currency": config["ghost_currency"],
        "dataSource": "YAHOO",
        "date": datetime.fromtimestamp(leg["time"], tz=timezone.utc).isoformat(),
        "fee": 0,
        "quantity": leg["quantity"],
        "symbol": symbol,
        "type": "BUY" if leg["net"] > 0 else "SELL",
        "unitPrice": 0,
    }


def convert_trade_fee_to_activity(ledger_id, entry, ghost_account_id, config,
                                  mapping, unmapped_assets, unresolved_assets=None):
    """Correct a trade whose fee Kraken charged in the base asset.

    TradesHistory reports `vol` gross, and the trade import uses it directly
    for the activity quantity, so a base-denominated fee never reduces the
    position. Three such fees on one account added up to 0.00002221 BTC -
    exactly the residual the reconcile could not explain.

    The correction is deliberately additive: a SELL of the fee alone, leaving
    the trade activity untouched. Changing the trade import to use a net
    quantity would be the tidier arithmetic, but it would orphan every
    activity already imported under a KRAKEN# comment, since Ghostfolio's
    import creates rows and never updates them.

    Returns None when there is nothing to correct.
    """
    fee = abs(to_float(entry.get("fee")))
    if fee <= 0:
        return None

    asset = entry.get("asset", "")
    symbol, _ = resolve_staking_symbol(asset, mapping, unmapped_assets)
    if not symbol:
        log.warning("Trade fee on %s is charged in %r, which cannot be resolved to a "
                    "symbol - the position will stay high by %.8f",
                    ledger_id, asset, fee)
        if unresolved_assets is not None:
            unresolved_assets.add(asset or "<empty>")
        return None

    log.info("Trade %s charged a fee of %.8f in the base asset; correcting the "
             "position, which TradesHistory reports gross", ledger_id, fee)

    return {
        "accountId": ghost_account_id,
        "comment": kraken_comment("TRADE_FEE", ledger_id),
        "currency": config["ghost_currency"],
        "dataSource": "YAHOO",
        "date": datetime.fromtimestamp(
            to_float(entry.get("time")), tz=timezone.utc).isoformat(),
        # The fee IS the quantity here; it is denominated in the asset, not in
        # the activity's currency, so the fee field stays zero as everywhere else.
        "fee": 0,
        "quantity": fee,
        "symbol": symbol,
        "type": "SELL",
        "unitPrice": 0,
    }


def convert_crypto_transfer_to_activity(ledger_id, entry, transfer_type, ghost_account_id, config, mapping, unmapped):
    """Convert a crypto deposit/withdrawal ledger entry to a Ghostfolio activity.

    Deposits become BUY, withdrawals become SELL.
    Returns None if the entry should be skipped.
    """
    asset = entry.get("asset", "")
    entry_time = to_float(entry.get("time"))
    fee = abs(to_float(entry.get("fee")))
    gross = abs(to_float(entry.get("amount")))

    # The fee is denominated in the entry's own asset, so it belongs in the
    # quantity rather than in the activity's currency-denominated `fee` field.
    # Which way it moves depends on the direction:
    #
    #   withdrawal  the balance falls by the amount AND the fee, so the SELL
    #               has to be the larger figure. Selling only abs(amount) left
    #               the fee behind in Ghostfolio - two BTC withdrawals were
    #               enough to show up as a 0.0000222 residual.
    #   deposit     the fee is taken out of what arrives, so the BUY is the
    #               smaller figure.
    #
    # Written per direction rather than as abs(amount - fee) so it stays
    # correct whichever sign Kraken reports the amount with.
    amount = gross + fee if transfer_type == "withdrawal" else gross - fee

    yahoo_symbol, normalized = resolve_staking_symbol(asset, mapping)
    if not yahoo_symbol or amount <= 0:
        return None

    ghost_currency = config["ghost_currency"]
    iso_date = datetime.fromtimestamp(entry_time, tz=timezone.utc).isoformat()

    if transfer_type == "deposit":
        activity_type = "BUY"
        comment = kraken_comment("DEPOSIT", ledger_id)
        log.warning("Crypto deposit %s: %s %s - cost basis set to 0, may be inaccurate",
                    ledger_id, amount, normalized)
    else:
        activity_type = "SELL"
        comment = kraken_comment("WITHDRAWAL", ledger_id)
        log.warning("Crypto withdrawal %s: %s %s - price set to 0, may not reflect actual sale",
                    ledger_id, amount, normalized)

    return {
        "accountId": ghost_account_id,
        "comment": comment,
        "currency": ghost_currency,
        "dataSource": "YAHOO",
        "date": iso_date,
        # Already folded into the quantity above, and denominated in the
        # asset rather than in this activity's currency.
        "fee": 0,
        "quantity": amount,
        "symbol": yahoo_symbol,
        "type": activity_type,
        "unitPrice": 0,
    }


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

# Mirrors Ghostfolio's getFactor(): BUY +1, SELL -1, everything else 0.
# A position's quantity accumulates as quantity * factor, so INTEREST,
# DIVIDEND, FEE, ITEM and LIABILITY rows carry a quantity in the payload that
# contributes nothing at all to the holding.
GHOST_QUANTITY_FACTOR = {"BUY": 1.0, "SELL": -1.0}

RECONCILE_ABS_TOLERANCE = 1e-8
RECONCILE_REL_TOLERANCE = 1e-6
RECONCILE_RULE_WIDTH = 100


def ghost_activity_factor(activity_type):
    """Return the quantity sign Ghostfolio applies to an activity type."""
    return GHOST_QUANTITY_FACTOR.get((activity_type or "").upper(), 0.0)


def activity_symbol(activity):
    """Read an activity's symbol, tolerating both Ghostfolio response shapes."""
    symbol = activity.get("symbol")
    if symbol:
        return symbol
    return (activity.get("SymbolProfile") or {}).get("symbol", "")


def activity_account_id(activity):
    """Read an activity's account id, tolerating both response shapes."""
    return activity.get("accountId") or (activity.get("Account") or {}).get("id")


def reconcile_tolerance(quantity):
    """Dust threshold for one asset, relative to the size of the holding."""
    return max(RECONCILE_ABS_TOLERANCE, RECONCILE_REL_TOLERANCE * abs(quantity))


def aggregate_ghost_positions(activities, account_id=None):
    """Sum per-symbol quantity the way Ghostfolio itself would.

    `ignored` accumulates the quantity sitting in rows whose factor is zero.
    That column is the whole diagnostic: a shortfall matched by `ignored` was
    imported into an activity type that does not move the position, whereas a
    shortfall with `ignored` at zero was never imported at all.

    Filtering by account matters. ghost_fetch_all_activities returns every
    account's activities, which is right for deduplication but wrong here: the
    same asset held in another account would cancel out the drift being
    measured.
    """
    positions = {}
    for activity in activities:
        if account_id is not None and activity_account_id(activity) != account_id:
            continue

        symbol = activity_symbol(activity)
        if not symbol:
            continue

        position = positions.setdefault(symbol, {
            "symbol": symbol, "quantity": 0.0, "ignored": 0.0,
            "ignored_types": collections.Counter(), "activities": 0,
        })
        quantity = to_float(activity.get("quantity"))
        factor = ghost_activity_factor(activity.get("type"))
        position["activities"] += 1
        if factor:
            position["quantity"] += quantity * factor
        else:
            position["ignored"] += quantity
            position["ignored_types"][(activity.get("type") or "").upper()] += 1

    return positions


def aggregate_kraken_balances(balances):
    """Collapse Kraken's balance keys onto the assets they normalize to.

    Staked, bonded and rewards balances come back as separate keys - XXBT,
    XXBT.B, XXBT.F - and all describe one holding. Summing them is also why
    /0/private/Earn/Allocations is not needed, which keeps the required API
    key permissions unchanged.
    """
    totals = collections.defaultdict(float)
    for asset, raw in balances.items():
        try:
            quantity = float(raw)
        except (TypeError, ValueError):
            fail("Kraken balance for %s is not a number: %r", asset, raw)
            continue
        totals[normalize_kraken_asset(asset)] += quantity
    return dict(totals)


def kraken_expected_symbols(balances, mapping):
    """Map Kraken balances to the Ghostfolio symbols they should appear under.

    Resolution runs asset -> symbol, never the inverse: BTCUSD could have come
    from XXBT, XBT, BTC.S or BTC.F, and a mapping.yaml override makes the
    inverse worse still.

    Returns (positions, cash) where cash holds the fiat balances, which are
    the account balance rather than a holding.
    """
    positions = {}
    cash = {}
    for asset, quantity in aggregate_kraken_balances(balances).items():
        code = fiat_code(asset)
        if code:
            cash[code] = cash.get(code, 0.0) + quantity
            continue
        # A fully withdrawn asset lingers in Balance as a zero row. It is not a
        # holding, and listing it would pad the table with permanent noise.
        if abs(quantity) <= RECONCILE_ABS_TOLERANCE:
            continue
        symbol, _ = resolve_staking_symbol(asset, mapping)
        if not symbol:
            log.warning("Kraken balance for %s cannot be resolved to a symbol; "
                        "excluding it from the reconciliation", asset)
            continue
        positions[symbol] = positions.get(symbol, 0.0) + quantity
    return positions, cash


def find_missing_trades(trades, existing_comments, mapping):
    """Kraken trades with no matching activity in Ghostfolio.

    A throwaway `unmapped` dict is used so a read-only reconcile never mutates
    the sync's unmapped-pairs report. `skip_reason` records trades the
    converter would drop on its own, which would otherwise be indistinguishable
    from an import that failed.
    """
    throwaway = {}
    missing = []
    for trade_id, trade in sorted(trades.items()):
        if kraken_comment("TRADE", trade_id) in existing_comments:
            continue

        pair = trade.get("pair", "")
        volume = to_float(trade.get("vol"))
        if not pair:
            skip_reason = "no pair"
        elif volume == 0:
            skip_reason = "zero volume"
        else:
            skip_reason = None

        symbol, currency = resolve_symbol(pair, mapping, throwaway) if pair else ("", "")
        missing.append({
            "trade_id": trade_id,
            "pair": pair,
            "date": datetime.fromtimestamp(
                to_float(trade.get("time")), tz=timezone.utc).isoformat(),
            "side": trade.get("type", ""),
            "volume": volume,
            "symbol": symbol,
            "currency": currency,
            "iso4217": currency in FIAT_CURRENCIES,
            "skip_reason": skip_reason,
        })
    return missing


def find_missing_ledger_activities(ledger, existing_comments, mapping, config, trades=None):
    """Ledger entries this tool would import that Ghostfolio does not have.

    Conversions, rewards, airdrops and bare transfers are ledger-only events -
    they never appear in TradesHistory - so the missing-trade check cannot see
    them, and a conversion that failed to import would otherwise surface as
    unattributed drift.

    Deposits and withdrawals are excluded when SKIP_CRYPTO_TRANSFERS is on:
    those are absent by configuration, not by fault.
    """
    buckets, _, _ = classify_ledger(ledger, trade_bases=trade_base_assets(trades or {}))
    skipping_transfers = bool(config.get("skip_crypto_transfers"))
    missing = []

    for classification, kind in LEDGER_COMMENT_KIND.items():
        for ledger_id, entry in buckets.get(classification, {}).items():
            if kraken_comment(kind, ledger_id) in existing_comments:
                continue
            symbol, _ = resolve_staking_symbol(entry.get("asset", ""), mapping)
            if not symbol:
                continue
            missing.append({
                "ledger_id": ledger_id,
                "kind": kind,
                "symbol": symbol,
                # Signed, so deposits and withdrawals cancel each other out.
                "net": ledger_leg(ledger_id, entry)["net"],
                # Absent by configuration rather than by fault, so it explains
                # a gap instead of being one.
                "expected": skipping_transfers and classification in (
                    INGEST_DEPOSIT, INGEST_WITHDRAWAL),
            })

    return missing


GHOSTFOLIO_CUSTOM_PREFIX = "GF_"
UUID_SYMBOL_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

LEGACY_PROFILE_CAUSE = ("legacy staking profile (superseded), zero position quantity "
                        "- not drift")


def elide(text, width):
    """Shorten `text` to `width`, so one 36-character UUID symbol cannot push
    every number on its row out of alignment. The full symbols are listed in
    the legacy-profile block below the table."""
    text = text or ""
    return text if len(text) <= width else text[:width - 1] + "~"


def is_custom_asset_profile(symbol):
    """True for a Ghostfolio MANUAL asset profile symbol.

    Ghostfolio forces every non-investment activity type onto
    DataSource.MANUAL with a UUID or GF_-prefixed symbol. The old INTEREST
    staking rows therefore each sit on a synthetic profile of their own,
    unrelated to any Kraken asset.
    """
    if not symbol:
        return False
    return symbol.startswith(GHOSTFOLIO_CUSTOM_PREFIX) or bool(UUID_SYMBOL_RE.match(symbol))


def is_superseded_legacy_profile(row, tolerance):
    """True for an inert leftover profile that must not count as drift.

    One of these holds quantity only in non-BUY/SELL rows, contributes nothing
    to any position, and has no Kraken asset behind it - the old INTEREST
    staking rows, now superseded by KRAKEN#REWARD# BUYs. Thirteen of them were
    silently failing a reconcile whose table showed four.
    """
    return (is_custom_asset_profile(row["symbol"])
            and abs(row["ghost"]) <= tolerance
            and abs(row["kraken"]) <= tolerance)


def _explanation_parts(row, tolerance):
    """Human phrases for each factor that accounts for part of a difference."""
    parts = []
    if abs(row["ignored"]) > tolerance:
        parts.append("%d non-BUY/SELL row(s) hold %.8f that Ghostfolio ignores"
                     % (row["ignored_rows"], row["ignored"]))
    if abs(row["expected_absent"]) > tolerance:
        parts.append("%d deposit(s)/withdrawal(s) excluded by SKIP_CRYPTO_TRANSFERS "
                     "net %+.8f" % (row["expected_absent_rows"], row["expected_absent"]))
    return parts


def classify_delta(row, missing_by_symbol, missing_ledger_by_symbol=None):
    """Explain one asset's difference. First match wins."""
    tolerance = reconcile_tolerance(row["kraken"])
    missing_ledger_by_symbol = missing_ledger_by_symbol or {}
    parts = _explanation_parts(row, tolerance)

    if is_superseded_legacy_profile(row, tolerance):
        return LEGACY_PROFILE_CAUSE

    # Both must be settled, not just the raw delta. A legacy INTEREST profile
    # has delta 0 and a non-zero residual, and calling that "ok" is what hid
    # it from the table while it still failed the run.
    if abs(row["delta"]) <= tolerance and abs(row["residual"]) <= tolerance:
        return "ok"

    # Accounted-for differences are settled before the structural branches.
    # A position Ghostfolio drove negative because the deposit that funded it
    # was skipped by configuration is fully explained, and must not be
    # mislabelled "not held on Kraken" just because the Kraken side is zero.
    if parts and abs(row["residual"]) <= tolerance:
        return "explained: " + "; ".join(parts)

    if row["ghost_activities"] == 0:
        return "missing from Ghostfolio entirely"
    if row["kraken"] <= tolerance and not parts:
        return "not held on Kraken - sold, withdrawn, or a bad symbol split"
    if parts:
        return ("partly explained: %s; %.8f unexplained"
                % ("; ".join(parts), row["residual"]))
    if missing_by_symbol.get(row["symbol"]):
        return "%d Kraken trade(s) never imported" % missing_by_symbol[row["symbol"]]
    if missing_ledger_by_symbol.get(row["symbol"]):
        kinds = missing_ledger_by_symbol[row["symbol"]]
        return ("%d Kraken ledger entr%s never imported (%s)"
                % (sum(kinds.values()),
                   "y" if sum(kinds.values()) == 1 else "ies",
                   ", ".join(sorted(kind.lower() for kind in kinds))))
    if row["delta"] < -tolerance:
        return "Ghostfolio holds more than Kraken - duplicate import or unrecorded withdrawal"
    return "unexplained"


def build_reconcile_rows(kraken_positions, ghost_positions, missing_trades,
                         missing_ledger=(), expected_absent=()):
    """Join both sides per symbol and attribute each difference.

    `expected_absent` holds ledger entries Ghostfolio legitimately does not
    have - today only deposits and withdrawals suppressed by
    SKIP_CRYPTO_TRANSFERS. Their net quantity is subtracted from the residual,
    so a difference they fully account for neither reads as drift nor fails
    the run.
    """
    missing_by_symbol = collections.Counter(
        trade["symbol"] for trade in missing_trades if trade["symbol"])

    missing_ledger_by_symbol = collections.defaultdict(collections.Counter)
    for item in missing_ledger:
        missing_ledger_by_symbol[item["symbol"]][item["kind"]] += 1

    absent_qty = collections.defaultdict(float)
    absent_rows = collections.Counter()
    for item in expected_absent:
        absent_qty[item["symbol"]] += item["net"]
        absent_rows[item["symbol"]] += 1

    rows = []
    for symbol in sorted(set(kraken_positions) | set(ghost_positions)
                         | set(absent_qty)):
        position = ghost_positions.get(symbol) or {}
        kraken_qty = kraken_positions.get(symbol, 0.0)
        ghost_qty = position.get("quantity", 0.0)
        ignored = position.get("ignored", 0.0)
        absent = absent_qty.get(symbol, 0.0)
        delta = kraken_qty - ghost_qty

        row = {
            "symbol": symbol,
            "kraken": kraken_qty,
            "ghost": ghost_qty,
            "ignored": ignored,
            "ignored_rows": sum(position.get("ignored_types", {}).values()),
            "expected_absent": absent,
            "expected_absent_rows": absent_rows.get(symbol, 0),
            "ghost_activities": position.get("activities", 0),
            "delta": delta,
            # What is left once everything Ghostfolio was never going to hold
            # is accounted for: quantity it discarded into non-BUY/SELL rows,
            # and transfers suppressed by configuration. The exit code keys
            # off this rather than the raw delta, so a fully explained
            # difference does not hold the run hostage.
            "residual": delta - ignored - absent,
        }
        row["cause"] = classify_delta(row, missing_by_symbol, missing_ledger_by_symbol)
        # Decided once, here, so the printed table and the exit code can never
        # disagree about which symbols are a problem.
        row["drift"] = (abs(row["residual"]) > reconcile_tolerance(kraken_qty)
                        and row["cause"] != LEGACY_PROFILE_CAUSE)
        rows.append(row)

    return rows


def reconcile_failures(rows):
    """The rows whose difference is real. This is the exit-code set."""
    return [row for row in rows if row["drift"]]


def print_reconcile_report(rows, cash, ghost_cash, missing_trades,
                           missing_ledger=(), show_all=False):
    """Print the diff table, the cash line and the missing-trade breakdown."""
    rule = "=" * RECONCILE_RULE_WIDTH
    # Anything that differs is shown, and anything that fails is shown even if
    # its raw delta is zero. The invariant is that every symbol in the
    # exit-code set appears in this table.
    differing = [row for row in rows if row["cause"] != "ok" or row["drift"]]
    shown = rows if show_all else differing
    shown = sorted(shown, key=lambda row: (not row["drift"], -abs(row["delta"]),
                                           row["symbol"]))

    print("\n" + rule)
    print("Reconciliation: Kraken balances vs Ghostfolio positions")
    print()
    if shown:
        print(f"{'SYMBOL':<12} {'KRAKEN':>16} {'GHOSTFOLIO':>16} "
              f"{'DELTA':>16} {'IGNORED':>13} {'EXP.ABSENT':>13}  LIKELY CAUSE")
        print("-" * RECONCILE_RULE_WIDTH)
        for row in shown:
            print(f"{elide(row['symbol'], 12):<12} {row['kraken']:>16.8f} "
                  f"{row['ghost']:>16.8f} {row['delta']:>+16.8f} "
                  f"{row['ignored']:>13.8f} {row['expected_absent']:>+13.8f}  "
                  f"{row['cause']}")
    else:
        print("Every asset reconciles.")

    print()
    print("IGNORED    = quantity sitting in activities Ghostfolio does not count toward")
    print("             position size (INTEREST, DIVIDEND, FEE, ITEM, LIABILITY). Only")
    print("             BUY and SELL move quantity.")
    print("EXP.ABSENT = net quantity of deposits and withdrawals Ghostfolio was never")
    print("             sent, because SKIP_CRYPTO_TRANSFERS suppresses them. Absent by")
    print("             configuration, not by fault.")

    if cash:
        holdings = ", ".join("%s %.2f" % (asset, amount)
                             for asset, amount in sorted(cash.items()))
        print("Cash (not a position): %s on Kraken, %.2f on the Ghostfolio account."
              % (holdings, ghost_cash))

    unexplained = reconcile_failures(rows)
    print("%d asset(s) differ, %d reconciled. Unexplained drift on %d asset(s)%s"
          % (len(differing), len(rows) - len(differing), len(unexplained),
             ": " + ", ".join(row["symbol"] for row in unexplained) if unexplained else "."))
    print(rule + "\n")

    if missing_trades:
        print_missing_trades(missing_trades)
    if missing_ledger:
        print_missing_ledger_activities(missing_ledger)


def print_missing_ledger_activities(missing_ledger):
    """Report ledger-only activities absent from Ghostfolio, grouped by kind."""
    rule = "=" * RECONCILE_RULE_WIDTH
    grouped = collections.defaultdict(list)
    for item in missing_ledger:
        grouped[(item["kind"], item["symbol"])].append(item["ledger_id"])

    print(rule)
    print("Kraken ledger entries not present in Ghostfolio: %d" % len(missing_ledger))
    print()
    print("These are ledger-only events - conversions, rewards, airdrops and bare")
    print("transfers - so they never appear in TradesHistory. Running a sync imports them.")
    print()
    print(f"  {'KIND':<12} {'SYMBOL':<12} {'COUNT':>6}  EXAMPLE LEDGER ID")
    for (kind, symbol), ledger_ids in sorted(grouped.items()):
        print(f"  {kind:<12} {symbol:<12} {len(ledger_ids):>6}  {ledger_ids[0]}")
    print(rule + "\n")


def print_missing_trades(missing_trades):
    """Report Kraken trades absent from Ghostfolio, grouped by currency.

    Grouping is what makes the report actionable: /api/v1/import is
    all-or-nothing, so a handful of trades in a currency Ghostfolio rejects
    takes every other trade in the batch down with them.
    """
    rule = "=" * RECONCILE_RULE_WIDTH
    by_currency = collections.defaultdict(list)
    for trade in missing_trades:
        by_currency[trade["currency"]].append(trade)

    print(rule)
    print("Kraken trades not present in Ghostfolio: %d" % len(missing_trades))
    print()
    print("  by currency:")
    for currency, trades in sorted(by_currency.items()):
        verdict = ("ISO 4217 ok" if trades[0]["iso4217"]
                   else "NOT ISO 4217 - Ghostfolio rejects the whole import batch")
        print("    %-6s %4d trade(s)   %s" % (currency or "?", len(trades), verdict))

    blocked = [trade for trade in missing_trades if not trade["iso4217"]]
    if blocked:
        print()
        print("  blocked trades (fix these first - they are why the others never landed):")
        for trade in blocked:
            print("    %s  %s  %-5s %.8f %s" % (
                trade["trade_id"], trade["date"], trade["side"],
                trade["volume"], trade["pair"]))
        print()
        print("  add to mapping.yaml under symbol_mapping:")
        for pair in sorted({trade["pair"] for trade in blocked}):
            base, quote, _ = split_kraken_pair(pair)
            print(f"    {pair}: {base}USD  # {base}/{quote}")

    skipped = [trade for trade in missing_trades if trade["skip_reason"]]
    if skipped:
        print()
        print("  never importable, skipped by the converter itself:")
        for trade in skipped:
            print("    %s  %s (%s)" % (trade["trade_id"], trade["pair"], trade["skip_reason"]))
    print(rule + "\n")


def report_legacy_staking(by_comment):
    """List the inert INTEREST rows earlier versions of this script created.

    They are read-only here. Ghostfolio's import creates activities and never
    updates them, so there is no in-place fix; and these rows are provably
    inert - zero quantity via getFactor, zero value via quantity * unitPrice
    with unitPrice 0 - so leaving them costs nothing but clutter. Corrected
    rewards arrive under a different comment namespace, which is what lets
    them be re-imported without the deduplication check skipping them.
    """
    legacy = [
        activity for comment, activity in sorted(by_comment.items())
        if comment.startswith(COMMENT_PREFIXES["LEGACY_STAKE"])
        and (activity.get("type") or "").upper() == "INTEREST"
    ]
    if not legacy:
        return []

    total = sum(to_float(activity.get("quantity")) for activity in legacy)
    print("\n" + "=" * 78)
    print("%d legacy staking activities are present as INTEREST rows." % len(legacy))
    print()
    print("They hold %.8f in quantity that Ghostfolio does not count toward any" % total)
    print("position, and unitPrice 0 means they carry no value either. Rewards are")
    print("now imported as BUY under a %s comment, so these are safe to" % COMMENT_PREFIXES["REWARD"])
    print("leave in place - deleting them in the Ghostfolio UI only removes clutter.")
    print()
    print(f"  {'GHOSTFOLIO ID':<38} {'DATE':<26} {'SYMBOL':<10} QUANTITY")
    for activity in legacy:
        print("  %-38s %-26s %-10s %.8f" % (
            activity.get("id", "?"), activity.get("date", "?"),
            activity_symbol(activity), to_float(activity.get("quantity"))))
    print("=" * 78 + "\n")
    return legacy


def run_reconcile(config, mapping, ghost_account_id, activities, trades=None):
    """Report per-asset drift between Kraken and Ghostfolio. Read-only."""
    log.info("Reconciling Kraken balances against Ghostfolio positions")

    if config["sync_since_ts"]:
        log.warning(
            "SYNC_SINCE is set. Kraken balances are all-time but trades and "
            "ledger entries are windowed, so long-held assets will show as "
            "drift that is not really drift.")

    by_comment = index_activities_by_comment(activities)
    # Under a restricted view Ghostfolio redacts comments. Every trade would
    # then look absent, so this is a hard failure here rather than a warning.
    if activities and not by_comment and not any(a.get("comment") for a in activities):
        fail("Ghostfolio returned %d activities but none carry a comment, so the "
             "missing-trade check cannot run. This instance is likely using a "
             "restricted view, which redacts comments.", len(activities))

    ghost_positions = aggregate_ghost_positions(activities, ghost_account_id)

    balances = fetch_balances(config)
    kraken_positions, cash = kraken_expected_symbols(balances, mapping)

    if trades is None:
        throttle(config, LEDGER_COUNTER_COST)
        trades = fetch_all_trades(config)
    missing_trades = find_missing_trades(trades, set(by_comment), mapping)

    # Conversions, rewards, airdrops and bare transfers exist only in the
    # ledger, so a missing one would otherwise show up as unattributed drift.
    throttle(config, LEDGER_COUNTER_COST)
    ledger = fetch_ledger_for_sync(config)
    ledger_absent = find_missing_ledger_activities(
        ledger, set(by_comment), mapping, config, trades)
    # Split by fault: entries suppressed by configuration explain a gap,
    # entries that simply failed to import are one.
    expected_absent = [item for item in ledger_absent if item["expected"]]
    missing_ledger = [item for item in ledger_absent if not item["expected"]]
    if expected_absent:
        log.info("%d deposit/withdrawal ledger entries are excluded by "
                 "SKIP_CRYPTO_TRANSFERS and are treated as expected absences",
                 len(expected_absent))

    ghost_cash = 0.0
    for account in (ghost_get_accounts(config).get("accounts") or []):
        if account.get("id") == ghost_account_id:
            ghost_cash = to_float(account.get("balance"))

    rows = build_reconcile_rows(kraken_positions, ghost_positions, missing_trades,
                                missing_ledger, expected_absent)
    print_reconcile_report(rows, cash, ghost_cash, missing_trades, missing_ledger)
    report_legacy_staking(by_comment)

    unexplained = reconcile_failures(rows)
    if unexplained:
        fail("Reconciliation found unexplained drift on %d asset(s): %s",
             len(unexplained), ", ".join(row["symbol"] for row in unexplained))
    return rows


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    """Parse command line flags.

    Every flag defaults to the behaviour the script had before they existed,
    so a bare `python kraken_to_ghostfolio.py` from entrypoint.sh or cron runs
    an ordinary sync. Unknown arguments are a hard error rather than being
    ignored, so a stale SYNC_ARGS value is visible in the cron log instead of
    silently doing nothing.
    """
    parser = argparse.ArgumentParser(
        prog="kraken_to_ghostfolio.py",
        description="Sync Kraken trades, rewards and transfers to Ghostfolio.",
    )
    parser.add_argument(
        "--reconcile", action="store_true",
        help="Read-only: report per-asset drift between Kraken balances and "
             "Ghostfolio positions. Makes no writes.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build the activities and print them, but do not import them or "
             "update the cash balance.",
    )
    parser.add_argument(
        "--dump-ledger-types", action="store_true",
        help="Read-only: print a histogram of the (type, subtype) pairs "
             "present in the Kraken ledger, then exit.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def finish():
    """Report accumulated failures and return the process exit code."""
    if FAILURES:
        log.error("Run finished with %d failure(s):", len(FAILURES))
        for item in FAILURES:
            log.error("  - %s", item)
        return 1
    return 0


def print_dry_run_activities(activities):
    """Print the activities a real run would import, as stable JSON.

    Sorted by comment and emitted with sorted keys so two runs can be diffed
    against each other - which is how a change to the fetching or
    classification layer is shown to be behaviour-preserving.
    """
    ordered = sorted(activities, key=lambda activity: activity.get("comment", ""))
    print("\n" + "=" * 60)
    print("Dry run: %d activities would be imported" % len(ordered))
    print()
    print(json.dumps(ordered, indent=2, sort_keys=True))
    print("=" * 60 + "\n")


def run_dump_ledger_types(config):
    """Print a (type, subtype) histogram over the whole Kraken ledger.

    A diagnostic, not part of a sync. Kraken's REST and WebSocket references
    enumerate different ledger types, and the taxonomy changed when Earn
    replaced legacy staking, so this reports what an account actually contains
    rather than what the documentation claims.
    """
    log.info("Fetching the full ledger to inspect its type taxonomy...")
    ledger = fetch_ledger_entries(config)

    histogram = collections.Counter(
        (entry.get("type", ""), entry.get("subtype", ""))
        for entry in ledger.values()
    )
    examples = {}
    for ledger_id, entry in ledger.items():
        examples.setdefault((entry.get("type", ""), entry.get("subtype", "")), ledger_id)

    print("\n" + "=" * 78)
    print("Kraken ledger (type, subtype) histogram over %d entries" % len(ledger))
    print()
    print(f"  {'TYPE':<18} {'SUBTYPE':<20} {'COUNT':>7}  EXAMPLE LEDGER ID")
    for (entry_type, subtype), count in sorted(histogram.items(),
                                               key=lambda item: (-item[1], item[0])):
        print(f"  {entry_type or '-':<18} {subtype or '-':<20} {count:>7}  "
              f"{examples[(entry_type, subtype)]}")
    print("=" * 78 + "\n")


def main(argv=None):
    """Entry point. Returns a process exit code rather than exiting itself."""
    args = parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        log.setLevel(logging.DEBUG)

    try:
        config = load_config(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 1

    # Dispatched before anything touches Ghostfolio, and before load_config
    # asks for Ghostfolio credentials: this is a pure Kraken diagnostic and
    # must be usable when Ghostfolio is down, unconfigured, or not yet set up.
    if args.dump_ledger_types:
        run_dump_ledger_types(config)
        return finish()

    mapping = load_mapping(config["mapping_file"])
    log.info("Loaded %d symbol mappings", len(mapping))

    # Find the Ghostfolio account. Needed by both remaining modes, including
    # the read-only one, because reconcile scopes its position totals to it.
    try:
        ghost_account_id = ghost_find_account_id(config, config["ghost_account_name"])
    except LookupError as exc:
        log.error("%s", exc)
        return 1
    log.info("Found Ghostfolio account '%s' (ID: %s)", config["ghost_account_name"], ghost_account_id)

    # Fetched once here and handed to whichever mode runs, so the reconcile and
    # the legacy-row report do not each repeat the full skip/take page walk.
    activities = ghost_fetch_all_activities(config)

    if args.reconcile:
        run_reconcile(config, mapping, ghost_account_id, activities)
        return finish()

    run_sync(config, mapping, ghost_account_id, args, activities)
    return finish()


def run_sync(config, mapping, ghost_account_id, args, activities_existing):
    """Fetch from Kraken, convert, and import into Ghostfolio."""
    log.info("Starting Kraken to Ghostfolio sync")

    # One window shared by every Kraken fetch in this run. Offset pagination
    # over a newest-first feed is only stable while the feed does not grow, and
    # a shared end also keeps a trade that lands mid-run from showing up in one
    # source but not the other.
    run_end_ts = int(time.time())

    warn_about_sync_since_cutoff(config)

    # Fetch data from Kraken
    log.info("Fetching trades from Kraken...")
    trades = fetch_all_trades(config, end=run_end_ts)
    throttle(config, LEDGER_COUNTER_COST)

    ledger = fetch_ledger_for_sync(config, run_end_ts)
    # The trade bases tell the classifier which trade ledger legs the trade
    # import already covers, and which quote legs nothing else accounts for.
    buckets, histogram, unknowns = classify_ledger(
        ledger, trade_bases=trade_base_assets(trades))

    staking_entries = buckets[INGEST_REWARD]
    airdrop_entries = buckets[INGEST_AIRDROP]
    convert_entries = buckets[INGEST_CONVERT]
    bare_transfers = dict(buckets[INGEST_TRANSFER])
    trade_quote_legs = buckets[INGEST_TRADE_QUOTE]
    trade_fee_legs = buckets[INGEST_TRADE_FEE]
    deposit_entries = buckets[INGEST_DEPOSIT]
    withdrawal_entries = buckets[INGEST_WITHDRAWAL]

    log.info("Found %d trades and %d ledger entries", len(trades), len(ledger))
    log.info("Ledger classification: %s",
             ", ".join("%s=%d" % (name, count)
                       for name, count in sorted(histogram.items())) or "nothing")

    # Deduplication index over the activities main() already fetched.
    by_comment = index_activities_by_comment(activities_existing)
    warn_if_comments_redacted(activities_existing, by_comment)
    existing_comments = set(by_comment)
    log.info("Found %d existing Kraken activities in Ghostfolio", len(existing_comments))

    report_legacy_staking(by_comment)

    # Kept apart because the mapping.yaml key semantics differ: `unmapped`
    # holds Kraken pairs, `unmapped_assets` holds bare asset names.
    unmapped = {}
    unmapped_assets = {}
    unresolved_assets = set()
    activities = []
    skipped_dup = 0

    # Process trades
    for trade_id, trade in trades.items():
        comment = kraken_comment("TRADE", trade_id)
        if comment in existing_comments:
            skipped_dup += 1
            continue

        activity = convert_trade_to_activity(trade_id, trade, ghost_account_id, config, mapping, unmapped)
        if activity:
            activities.append(activity)

    trade_count = len(activities)
    log.info("New trade activities: %d, duplicates skipped: %d", trade_count, skipped_dup)

    # Process rewards and airdrops. Fiat and non-positive entries were already
    # filtered out by the classifier, so these loops only dedupe and convert.
    reward_new = 0
    reward_dup = 0
    reward_skipped = 0
    for kind, entries in (("REWARD", staking_entries), ("AIRDROP", airdrop_entries)):
        for ledger_id, entry in entries.items():
            comment = kraken_comment(kind, ledger_id)
            if comment in existing_comments:
                log.debug("Skipping ledger entry %s: duplicate (already in Ghostfolio)", ledger_id)
                reward_dup += 1
                continue

            activity = convert_reward_to_activity(
                ledger_id, entry, kind, ghost_account_id, config, mapping,
                unmapped_assets, unresolved_assets)
            if activity:
                activities.append(activity)
                reward_new += 1
            else:
                reward_skipped += 1

    log.info("New reward activities: %d, duplicates skipped: %d, filtered: %d",
             reward_new, reward_dup, reward_skipped)

    # One aggregate failure rather than one per occurrence: the exit code is
    # still correct, without N lines of the same message in a cron log.
    if unresolved_assets:
        fail("Dropped reward entries for %d asset(s) that could not be resolved "
             "to a symbol (%s) - Ghostfolio quantities are knowingly incomplete",
             len(unresolved_assets), ", ".join(sorted(unresolved_assets)))

    # Process conversions. Grouped by refid because what a leg means depends
    # on the other legs of the same conversion, but deduplicated per activity
    # because each one carries its own leg's ledger id.
    convert_new = 0
    convert_dup = 0
    for group in group_convert_refids(convert_entries).values():
        for activity in convert_group_to_activities(
            group, ghost_account_id, config, mapping, unmapped_assets, unresolved_assets
        ):
            if activity["comment"] in existing_comments:
                convert_dup += 1
                continue
            activities.append(activity)
            convert_new += 1

    log.info("New conversion activities: %d, duplicates skipped: %d",
             convert_new, convert_dup)

    # Process transfers and trade quote legs. Both are quantities without a
    # usable price, so they share a converter.
    bare_new = 0
    bare_dup = 0
    for kind, entries in (("TRANSFER", bare_transfers),
                          ("TRADE_QUOTE", trade_quote_legs)):
        for ledger_id, entry in entries.items():
            comment = kraken_comment(kind, ledger_id)
            if comment in existing_comments:
                bare_dup += 1
                continue

            activity = convert_transfer_to_activity(
                ledger_id, entry, ghost_account_id, config, mapping,
                unmapped_assets, unresolved_assets, kind=kind)
            if activity:
                activities.append(activity)
                bare_new += 1

    log.info("New transfer and trade-quote activities: %d, duplicates skipped: %d",
             bare_new, bare_dup)

    # Correct trades whose fee Kraken charged in the base asset. Additive: the
    # trade activity itself is left exactly as imported.
    fee_new = 0
    fee_dup = 0
    for ledger_id, entry in trade_fee_legs.items():
        comment = kraken_comment("TRADE_FEE", ledger_id)
        if comment in existing_comments:
            fee_dup += 1
            continue

        activity = convert_trade_fee_to_activity(
            ledger_id, entry, ghost_account_id, config, mapping,
            unmapped_assets, unresolved_assets)
        if activity:
            activities.append(activity)
            fee_new += 1

    log.info("New base-asset trade fee corrections: %d, duplicates skipped: %d",
             fee_new, fee_dup)

    # Process deposits and withdrawals
    transfer_new = 0
    transfer_dup = 0
    transfer_skipped_config = 0

    for kind, transfer_type, entries in (
        ("DEPOSIT", "deposit", deposit_entries),
        ("WITHDRAWAL", "withdrawal", withdrawal_entries),
    ):
        for ledger_id, entry in entries.items():
            # Skip crypto transfers if configured
            if config["skip_crypto_transfers"]:
                transfer_skipped_config += 1
                continue

            comment = kraken_comment(kind, ledger_id)
            if comment in existing_comments:
                transfer_dup += 1
                continue

            activity = convert_crypto_transfer_to_activity(
                ledger_id, entry, transfer_type, ghost_account_id, config, mapping, unmapped
            )
            if activity:
                activities.append(activity)
                transfer_new += 1

    log.info("New transfer activities: %d, duplicates skipped: %d, "
             "crypto transfers skipped (SKIP_CRYPTO_TRANSFERS): %d",
             transfer_new, transfer_dup, transfer_skipped_config)

    report_unknown_ledger_entries(unknowns)

    # Import all activities
    activities, rejected = validate_activities(activities)
    for activity, reason in rejected:
        fail("Not importable, skipped: %s (%s)", activity.get("comment", "?"), reason)

    total_new = len(activities)
    total_dup = skipped_dup + reward_dup + convert_dup + bare_dup + fee_dup + transfer_dup
    log.info("Importing %d new activities (%d skipped as duplicates)", total_new, total_dup)

    if args.dry_run:
        print_dry_run_activities(activities)
        log.info("Dry run: nothing was imported and the cash balance was not touched")
    else:
        if activities:
            ghost_import_activities(config, activities)

        # Update cash balance from Kraken balances. The currency to sum is
        # decided inside, from the account itself.
        try:
            log.info("Fetching Kraken balances for cash balance update...")
            ghost_update_cash_balance(config, ghost_account_id, fetch_balances(config))
        except Exception as exc:
            fail("Failed to update cash balance: %s", exc)

    # Print unmapped symbols summary
    if unmapped:
        print("\n" + "=" * 60)
        print("Unmapped Kraken pairs found. Add to your mapping file under symbol_mapping:")
        print()
        for pair, info in sorted(unmapped.items()):
            base = info.get("base", "")
            quote = info.get("quote", "")
            yahoo = info.get("yahoo", "")
            print(f"  {pair}: {yahoo}  # {base}/{quote}")
        print("=" * 60 + "\n")
    else:
        log.info("All symbols resolved via mapping or automatic conversion")

    # Reward and airdrop assets get their own block: the mapping.yaml key is a
    # bare asset name here, not a pair, and the pair block's "# BASE/QUOTE"
    # suffix would render as "# BABY/" for an asset.
    if unmapped_assets:
        print("\n" + "=" * 60)
        print("Reward and airdrop assets resolved automatically. If Ghostfolio")
        print("does not recognise one of these symbols, add it to your mapping")
        print("file under symbol_mapping:")
        print()
        for asset, info in sorted(unmapped_assets.items()):
            print(f"  {asset}: {info['symbol']}  # from Kraken asset {info['asset']}")
        print("=" * 60 + "\n")

    log.info("Sync complete")


if __name__ == "__main__":
    sys.exit(main())
