#!/usr/bin/env python3
"""Sync Kraken trades, staking rewards, and deposit/withdrawal activity to a self-hosted Ghostfolio instance."""

import base64
import hashlib
import hmac
import logging
import os
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

KRAKEN_API_BASE = "https://api.kraken.com"

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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config():
    """Load and validate configuration from environment variables."""
    required = ["KRAKEN_API_KEY", "KRAKEN_API_SECRET", "GHOST_TOKEN", "GHOST_HOST"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    sync_since = os.environ.get("SYNC_SINCE", "")
    sync_since_ts = None
    if sync_since:
        try:
            sync_since_ts = datetime.fromisoformat(sync_since).replace(
                tzinfo=timezone.utc
            ).timestamp()
            log.info("SYNC_SINCE set to %s (timestamp %.0f)", sync_since, sync_since_ts)
        except ValueError:
            log.error("Invalid SYNC_SINCE format: %s (expected ISO date like 2024-01-01)", sync_since)
            sys.exit(1)

    return {
        "kraken_api_key": os.environ["KRAKEN_API_KEY"],
        "kraken_api_secret": os.environ["KRAKEN_API_SECRET"],
        "ghost_token": os.environ["GHOST_TOKEN"],
        "ghost_host": os.environ["GHOST_HOST"].rstrip("/"),
        "ghost_currency": os.environ.get("GHOST_CURRENCY", "USD"),
        "ghost_platform_id": os.environ.get("GHOST_PLATFORM_ID", ""),
        "ghost_account_name": os.environ.get("GHOST_ACCOUNT_NAME", "Kraken"),
        "mapping_file": os.environ.get("MAPPING_FILE", "/app/mapping.yaml"),
        "skip_crypto_transfers": os.environ.get("SKIP_CRYPTO_TRANSFERS", "true").lower() == "true",
        "api_call_delay": float(os.environ.get("API_CALL_DELAY", "1.0")),
        "sync_since_ts": sync_since_ts,
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


def kraken_request(config, url_path, data=None):
    """Make an authenticated request to the Kraken API."""
    if data is None:
        data = {}
    data["nonce"] = str(int(time.time() * 1000))

    headers = {
        "API-Key": config["kraken_api_key"],
        "API-Sign": kraken_signature(url_path, data, config["kraken_api_secret"]),
    }

    url = KRAKEN_API_BASE + url_path
    resp = requests.post(url, headers=headers, data=data, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    errors = result.get("error", [])
    if errors:
        raise RuntimeError(f"Kraken API error: {', '.join(errors)}")

    return result.get("result", {})


# ---------------------------------------------------------------------------
# Kraken data fetching (paginated)
# ---------------------------------------------------------------------------

def fetch_all_trades(config):
    """Fetch all trades from Kraken, handling pagination."""
    all_trades = {}
    offset = 0
    page_size = 50

    while True:
        data = {"ofs": offset}
        if config["sync_since_ts"]:
            data["start"] = str(int(config["sync_since_ts"]))

        result = kraken_request(config, "/0/private/TradesHistory", data)
        trades = result.get("trades", {})
        count = result.get("count", 0)

        if not trades:
            break

        all_trades.update(trades)
        offset += page_size

        if offset >= count:
            break

        log.info("Fetched %d/%d trades...", len(all_trades), count)
        time.sleep(config["api_call_delay"])

    log.info("Fetched %d total trades from Kraken", len(all_trades))
    return all_trades


def fetch_ledger_by_type(config, ledger_type):
    """Fetch all ledger entries of a given type from Kraken, handling pagination."""
    all_entries = {}
    offset = 0
    page_size = 50

    while True:
        data = {"type": ledger_type, "ofs": offset}
        if config["sync_since_ts"]:
            data["start"] = str(int(config["sync_since_ts"]))

        result = kraken_request(config, "/0/private/Ledgers", data)
        entries = result.get("ledger", {})
        count = result.get("count", 0)

        if not entries:
            break

        all_entries.update(entries)
        offset += page_size

        if offset >= count:
            break

        log.info("Fetched %d/%d %s ledger entries...", len(all_entries), count, ledger_type)
        time.sleep(config["api_call_delay"])

    log.info("Fetched %d %s ledger entries from Kraken", len(all_entries), ledger_type)
    return all_entries


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


def is_fiat(asset):
    """Check if an asset is a fiat currency."""
    return normalize_kraken_asset(asset) in FIAT_CURRENCIES


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

    # Try known crypto quote currencies
    crypto_quotes = ["XBT", "ETH", "XXBT", "XETH"]
    for cq in crypto_quotes:
        if pair.endswith(cq) and len(pair) > len(cq):
            base = pair[:-len(cq)]
            return normalize_kraken_asset(base), normalize_kraken_asset(cq), True

    # Fallback: try to split in the middle
    mid = len(pair) // 2
    base = normalize_kraken_asset(pair[:mid])
    quote = normalize_kraken_asset(pair[mid:])
    return base, quote, False


def resolve_symbol(pair, mapping, unmapped):
    """Resolve a Kraken trading pair to a Yahoo Finance symbol.

    Returns a (yahoo_symbol, trade_currency) tuple.
    - yahoo_symbol: always BASEUSD (e.g. BTCUSD, ETHUSD) because
      Ghostfolio with Yahoo data source uses this format for crypto.
    - trade_currency: the original quote currency from the Kraken pair
      (CHF, EUR, USD, etc.) for the activity's currency field.

    mapping.yaml overrides take priority and are returned as-is.
    Only adds to unmapped if the pair could not be confidently resolved
    (i.e. fell through to the midpoint split fallback).
    """
    # Check mapping first (keyed by Kraken pair) - returned as-is
    if pair in mapping:
        mapped = mapping[pair]
        _base, quote, _ = split_kraken_pair(pair)
        return mapped, quote

    base, quote, confident = split_kraken_pair(pair)

    # Ghostfolio + Yahoo uses BASEUSD format (no hyphen) for crypto
    yahoo_symbol = f"{base}USD"

    # Only track pairs where resolution fell back to the midpoint heuristic
    if not confident and pair not in unmapped:
        unmapped[pair] = {"base": base, "quote": quote, "yahoo": yahoo_symbol}

    return yahoo_symbol, quote


def resolve_staking_symbol(asset, mapping, unmapped):
    """Resolve a Kraken asset from a staking ledger entry to a Yahoo Finance symbol.

    For staking rewards, we only have the asset name, not a pair.
    We use the GHOST_CURRENCY as the quote to build the Yahoo symbol.
    """
    normalized = normalize_kraken_asset(asset)

    # Check mapping (keyed by Kraken asset name)
    if asset in mapping:
        return mapping[asset], normalized

    if normalized in mapping:
        return mapping[normalized], normalized

    # For fiat staking (rare but possible), skip
    if normalized in FIAT_CURRENCIES:
        return None, normalized

    return normalized, normalized


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
    log.error("Ghostfolio account '%s' not found. Available: %s",
              account_name, [a["name"] for a in accounts])
    sys.exit(1)


def ghost_get_existing_comments(config):
    """Fetch all existing order comments from Ghostfolio for deduplication.

    Returns a set of comment strings.
    """
    url = f"{config['ghost_host']}/api/v1/order"
    resp = requests.get(url, headers=ghost_headers(config["ghost_token"]), timeout=60)
    resp.raise_for_status()
    data = resp.json()
    comments = set()
    activities = data.get("activities", data) if isinstance(data, dict) else data
    for order in activities:
        comment = order.get("comment", "")
        if comment and comment.startswith("KRAKEN#"):
            comments.add(comment)
    return comments


def ghost_import_activities(config, activities):
    """Import activities into Ghostfolio."""
    if not activities:
        log.info("No new activities to import")
        return
    url = f"{config['ghost_host']}/api/v1/import"
    payload = {"activities": activities}
    resp = requests.post(url, headers=ghost_headers(config["ghost_token"]),
                         json=payload, timeout=60)
    if resp.status_code >= 400:
        log.error("Import failed (%d): %s", resp.status_code, resp.text)
        log.error("Check your mapping file - a symbol may not be recognised by Ghostfolio")
        return
    log.info("Successfully imported %d activities", len(activities))


def ghost_update_cash_balance(config, account_id, balance):
    """Update the cash balance on a Ghostfolio account."""
    url = f"{config['ghost_host']}/api/v1/account/{account_id}"
    resp = requests.get(url, headers=ghost_headers(config["ghost_token"]), timeout=30)
    resp.raise_for_status()
    account_data = resp.json()

    payload = {
        "balance": balance,
        "currency": account_data["currency"],
        "id": account_id,
        "isExcluded": account_data.get("isExcluded", False),
        "name": account_data["name"],
        "platformId": account_data.get("platformId") or config.get("ghost_platform_id") or None,
    }
    resp = requests.put(url, headers=ghost_headers(config["ghost_token"]),
                        json=payload, timeout=30)
    if resp.status_code >= 400:
        log.error("Failed to update cash balance (%d): %s", resp.status_code, resp.text)
    else:
        log.info("Updated cash balance for account %s to %.2f", account_id, balance)


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

    comment = f"KRAKEN#{trade_id}"

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


def convert_staking_to_activity(refid, entry, ghost_account_id, config, mapping, unmapped):
    """Convert a Kraken staking ledger entry to a Ghostfolio INTEREST activity.

    Returns None if the entry should be skipped.
    """
    asset = entry.get("asset", "")
    amount = float(entry.get("amount", "0"))
    fee = float(entry.get("fee", "0"))
    entry_time = float(entry.get("time", "0"))

    if amount <= 0:
        log.debug("Skipping staking entry %s: non-positive amount %.8f for asset %s",
                  refid, amount, asset)
        return None

    normalized = normalize_kraken_asset(asset)
    if normalized in FIAT_CURRENCIES:
        log.debug("Skipping staking entry %s: fiat asset %s (%s)", refid, asset, normalized)
        return None

    # Ghostfolio + Yahoo uses BASEUSD format (no hyphen) for crypto
    yahoo_symbol = f"{normalized}USD"

    # mapping.yaml overrides take priority and are returned as-is
    if asset in mapping:
        yahoo_symbol = mapping[asset]
    elif normalized in mapping:
        yahoo_symbol = mapping[normalized]

    iso_date = datetime.fromtimestamp(entry_time, tz=timezone.utc).isoformat()
    comment = f"KRAKEN#STAKE#{refid}"
    ghost_currency = config["ghost_currency"]

    return {
        "accountId": ghost_account_id,
        "comment": comment,
        "currency": ghost_currency,
        "dataSource": "YAHOO",
        "date": iso_date,
        "fee": fee,
        "quantity": amount,
        "symbol": yahoo_symbol,
        "type": "INTEREST",
        "unitPrice": 0,
    }


def convert_crypto_transfer_to_activity(refid, entry, transfer_type, ghost_account_id, config, mapping, unmapped):
    """Convert a crypto deposit/withdrawal ledger entry to a Ghostfolio activity.

    Deposits become BUY, withdrawals become SELL.
    Returns None if the entry should be skipped.
    """
    asset = entry.get("asset", "")
    amount = abs(float(entry.get("amount", "0")))
    fee = abs(float(entry.get("fee", "0")))
    entry_time = float(entry.get("time", "0"))

    normalized = normalize_kraken_asset(asset)

    # Skip fiat transfers
    if normalized in FIAT_CURRENCIES:
        return None

    # Ghostfolio + Yahoo uses BASEUSD format (no hyphen) for crypto
    yahoo_symbol = f"{normalized}USD"

    # mapping.yaml overrides take priority and are returned as-is
    if asset in mapping:
        yahoo_symbol = mapping[asset]
    elif normalized in mapping:
        yahoo_symbol = mapping[normalized]

    ghost_currency = config["ghost_currency"]
    iso_date = datetime.fromtimestamp(entry_time, tz=timezone.utc).isoformat()

    if transfer_type == "deposit":
        activity_type = "BUY"
        comment = f"KRAKEN#DEP#{refid}"
        log.warning("Crypto deposit %s: %s %s - cost basis set to 0, may be inaccurate",
                    refid, amount, normalized)
    else:
        activity_type = "SELL"
        comment = f"KRAKEN#WDR#{refid}"
        log.warning("Crypto withdrawal %s: %s %s - price set to 0, may not reflect actual sale",
                    refid, amount, normalized)

    return {
        "accountId": ghost_account_id,
        "comment": comment,
        "currency": ghost_currency,
        "dataSource": "YAHOO",
        "date": iso_date,
        "fee": fee,
        "quantity": amount,
        "symbol": yahoo_symbol,
        "type": activity_type,
        "unitPrice": 0,
    }


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def main():
    """Main entry point."""
    log.info("Starting Kraken to Ghostfolio sync")

    config = load_config()
    mapping = load_mapping(config["mapping_file"])
    log.info("Loaded %d symbol mappings", len(mapping))

    # Find the Ghostfolio account
    ghost_account_id = ghost_find_account_id(config, config["ghost_account_name"])
    log.info("Found Ghostfolio account '%s' (ID: %s)", config["ghost_account_name"], ghost_account_id)

    # Fetch data from Kraken
    log.info("Fetching trades from Kraken...")
    trades = fetch_all_trades(config)

    log.info("Fetching staking ledger entries...")
    staking_entries = fetch_ledger_by_type(config, "staking")

    log.info("Fetching deposit ledger entries...")
    deposit_entries = fetch_ledger_by_type(config, "deposit")

    log.info("Fetching withdrawal ledger entries...")
    withdrawal_entries = fetch_ledger_by_type(config, "withdrawal")

    log.info("Found %d trades, %d staking rewards, %d deposits, %d withdrawals",
             len(trades), len(staking_entries), len(deposit_entries), len(withdrawal_entries))

    # Get existing orders for deduplication
    existing_comments = ghost_get_existing_comments(config)
    log.info("Found %d existing Kraken activities in Ghostfolio", len(existing_comments))

    unmapped = {}
    activities = []
    skipped_dup = 0

    # Process trades
    for trade_id, trade in trades.items():
        comment = f"KRAKEN#{trade_id}"
        if comment in existing_comments:
            skipped_dup += 1
            continue

        activity = convert_trade_to_activity(trade_id, trade, ghost_account_id, config, mapping, unmapped)
        if activity:
            activities.append(activity)

    trade_count = len(activities)
    log.info("New trade activities: %d, duplicates skipped: %d", trade_count, skipped_dup)

    # Process staking rewards
    staking_new = 0
    staking_dup = 0
    staking_skipped = 0
    for refid, entry in staking_entries.items():
        comment = f"KRAKEN#STAKE#{refid}"
        if comment in existing_comments:
            log.debug("Skipping staking entry %s: duplicate (already in Ghostfolio)", refid)
            staking_dup += 1
            continue

        activity = convert_staking_to_activity(refid, entry, ghost_account_id, config, mapping, unmapped)
        if activity:
            activities.append(activity)
            staking_new += 1
        else:
            staking_skipped += 1

    log.info("New staking activities: %d, duplicates skipped: %d, filtered: %d",
             staking_new, staking_dup, staking_skipped)

    # Process deposits and withdrawals
    transfer_new = 0
    transfer_dup = 0
    transfer_skipped_fiat = 0
    transfer_skipped_config = 0

    for refid, entry in deposit_entries.items():
        asset = entry.get("asset", "")
        normalized = normalize_kraken_asset(asset)

        # Skip fiat deposits
        if normalized in FIAT_CURRENCIES:
            transfer_skipped_fiat += 1
            continue

        # Skip crypto transfers if configured
        if config["skip_crypto_transfers"]:
            transfer_skipped_config += 1
            continue

        comment = f"KRAKEN#DEP#{refid}"
        if comment in existing_comments:
            transfer_dup += 1
            continue

        activity = convert_crypto_transfer_to_activity(
            refid, entry, "deposit", ghost_account_id, config, mapping, unmapped
        )
        if activity:
            activities.append(activity)
            transfer_new += 1

    for refid, entry in withdrawal_entries.items():
        asset = entry.get("asset", "")
        normalized = normalize_kraken_asset(asset)

        # Skip fiat withdrawals
        if normalized in FIAT_CURRENCIES:
            transfer_skipped_fiat += 1
            continue

        # Skip crypto transfers if configured
        if config["skip_crypto_transfers"]:
            transfer_skipped_config += 1
            continue

        comment = f"KRAKEN#WDR#{refid}"
        if comment in existing_comments:
            transfer_dup += 1
            continue

        activity = convert_crypto_transfer_to_activity(
            refid, entry, "withdrawal", ghost_account_id, config, mapping, unmapped
        )
        if activity:
            activities.append(activity)
            transfer_new += 1

    log.info("New transfer activities: %d, duplicates skipped: %d, fiat skipped: %d, "
             "crypto transfers skipped (SKIP_CRYPTO_TRANSFERS): %d",
             transfer_new, transfer_dup, transfer_skipped_fiat, transfer_skipped_config)

    # Import all activities
    total_new = len(activities)
    total_dup = skipped_dup + staking_dup + transfer_dup
    log.info("Importing %d new activities (%d skipped as duplicates)", total_new, total_dup)

    if activities:
        ghost_import_activities(config, activities)

    # Update cash balance from Kraken balances
    try:
        log.info("Fetching Kraken balances for cash balance update...")
        balances = fetch_balances(config)
        ghost_currency = config["ghost_currency"]

        # Find balance matching the ghost currency
        cash_balance = 0.0
        for asset, balance_str in balances.items():
            normalized = normalize_kraken_asset(asset)
            if normalized == ghost_currency:
                cash_balance += float(balance_str)

        ghost_update_cash_balance(config, ghost_account_id, cash_balance)
    except Exception as exc:
        log.error("Failed to update cash balance: %s", exc)

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

    log.info("Sync complete")


if __name__ == "__main__":
    main()
