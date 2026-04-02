# ghostfolio-kraken-sync

Sync Kraken trades, staking rewards, and deposit/withdrawal activity to a self-hosted [Ghostfolio](https://ghostfol.io) instance.

This tool connects to the Kraken API, fetches your complete trading history, staking rewards, and deposit/withdrawal ledger entries, maps Kraken's non-standard asset names to Yahoo Finance symbols, and pushes everything into Ghostfolio. It handles pagination, deduplicates activities, updates cash balances, and supports both one-off and scheduled (cron) execution.

## Docker image

```
ghcr.io/obol89/ghostfolio-kraken-sync:latest
```

Multi-arch image (linux/amd64 and linux/arm64). New images are published automatically on every push to main.

## Prerequisites

- A running self-hosted Ghostfolio instance
- A Kraken account with API keys
- Docker (for containerised runs) or Python 3.10+ with `requests` and `pyyaml`

## Kraken API Key Setup

1. Log in to [Kraken](https://www.kraken.com)
2. Go to **Security** - **API**
3. Click **Create API Key**
4. Give it a descriptive name like `Ghostfolio Sync`
5. Enable the following permissions:
   - **Query funds** - needed for balance retrieval
   - **Query closed orders & trades** - needed for trade history
   - **Query ledger entries** - needed for staking rewards, deposits, and withdrawals
   - **Export data** - needed for full history access
6. Do **not** enable trading, withdrawal, or funding permissions
7. Save the **API Key** and **Private Key** (base64-encoded secret)
8. Use the API Key as `KRAKEN_API_KEY` and the Private Key as `KRAKEN_API_SECRET`

## Ghostfolio Setup

### 1. Get an auth token

```bash
curl -X POST http://localhost:3333/api/v1/auth/anonymous \
  -H 'Content-Type: application/json' \
  -d '{"accessToken": "YOUR_GHOSTFOLIO_ACCESS_TOKEN"}'
```

The response contains an `authToken` field. Use this as `GHOST_TOKEN`. This token expires and will need to be regenerated periodically.

### 2. Create a Kraken Platform

1. Go to Ghostfolio **Admin** - **Platform**
2. Click **Add Platform**
3. Enter a name like `Kraken` and a URL like `https://www.kraken.com`
4. Save it

To find the Platform ID, query the API:

```bash
curl http://localhost:3333/api/v1/platform \
  -H 'Authorization: Bearer YOUR_AUTH_TOKEN'
```

Look for the `id` field of the Kraken platform entry. Use this as `GHOST_PLATFORM_ID`.

### 3. Create an account

1. Go to **Accounts** and click **Add Account**
2. Set the name to match `GHOST_ACCOUNT_NAME` (default: `Kraken`)
3. Select the Kraken platform you created
4. Set the currency to your base currency (matching `GHOST_CURRENCY`)

### 4. Add currencies

If your Kraken trades involve currencies that are not yet in Ghostfolio:

1. Go to **Admin** - **Market Data**
2. Search for and add any missing currency pairs (for example `USDEUR`, `USDCHF`)
3. Ghostfolio needs these to convert values to your base currency

### 5. After your first import

After the first sync, go to **Admin** - **Market Data** and click **Gather All Data**. This fetches historical prices from Yahoo Finance for all newly imported symbols. Without this step, portfolio values and performance charts will be incorrect. Run it again after adding symbols from a new import.

## Configuration

All configuration is done via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `KRAKEN_API_KEY` | Yes | | Kraken API key |
| `KRAKEN_API_SECRET` | Yes | | Kraken API secret (base64-encoded by Kraken) |
| `GHOST_TOKEN` | Yes | | Ghostfolio auth bearer token |
| `GHOST_HOST` | Yes | | Ghostfolio base URL (e.g. `http://ghostfolio:3333`) |
| `GHOST_CURRENCY` | No | `USD` | Base currency for the Ghostfolio account |
| `GHOST_PLATFORM_ID` | No | | Platform ID for Kraken in Ghostfolio |
| `GHOST_ACCOUNT_NAME` | No | `Kraken` | Ghostfolio account name |
| `MAPPING_FILE` | No | `/app/mapping.yaml` | Path to symbol mapping YAML |
| `SKIP_CRYPTO_TRANSFERS` | No | `true` | Skip crypto deposit/withdrawal sync |
| `API_CALL_DELAY` | No | `1.0` | Seconds between paginated API calls |
| `SYNC_SINCE` | No | | ISO date to limit history (e.g. `2024-01-01`) |
| `CRON` | No | | Cron expression for scheduled runs |
| `TZ` | No | | Timezone for cron scheduling (e.g. `Europe/Zurich`) |

## Mapping File

The mapping file overrides the automatic Kraken-to-Yahoo-Finance symbol resolution. Most common pairs are resolved automatically, but edge cases may need manual mapping.

### Format

The file must have a `symbol_mapping` key at the top level. Keys can be either Kraken trading pairs or asset names:

```yaml
symbol_mapping:
  XXBTZUSD: BTC-USD       # Override pair resolution
  DOTEUR: DOT-EUR         # Override pair resolution
  SOL: SOL-USD            # Override for staking reward asset
  MATIC: MATIC-USD        # Override for staking reward asset
```

### When mapping is needed

The automatic resolution handles most cases:

- Standard prefixed pairs like `XXBTZUSD` are split into `BTC` and `USD` automatically
- Newer pairs like `SOLUSD` or `DOTEUR` are split by detecting the fiat suffix
- Staking rewards use the asset name with your `GHOST_CURRENCY` as the quote

You may need manual mapping when:

- A pair cannot be split automatically (unusual naming)
- The Yahoo Finance symbol differs from the expected `BASE-QUOTE` format
- You want to use a different data source or symbol variant

### Unmapped symbols

When the script encounters a pair it resolves automatically (without a mapping entry), it prints all such pairs at the end of the run in a format you can review:

```
Unmapped Kraken pairs found. Add to your mapping file under symbol_mapping:

  XXBTZUSD: BTC-USD  # BTC/USD
  XETHZEUR: ETH-EUR  # ETH/EUR
```

If these look correct, no action is needed. If any are wrong, add the correct mapping to your file.

## Running

### One-off run

```bash
docker run --rm \
  -e KRAKEN_API_KEY=your_key \
  -e KRAKEN_API_SECRET=your_secret \
  -e GHOST_TOKEN=your_ghost_token \
  -e GHOST_HOST=http://ghostfolio:3333 \
  -e GHOST_ACCOUNT_NAME=Kraken \
  -e GHOST_CURRENCY=USD \
  -v ./mapping.yaml:/app/mapping.yaml \
  ghcr.io/obol89/ghostfolio-kraken-sync:latest
```

### First run with history limit

If you have a long trading history and want to start from a specific date:

```bash
docker run --rm \
  -e KRAKEN_API_KEY=your_key \
  -e KRAKEN_API_SECRET=your_secret \
  -e GHOST_TOKEN=your_ghost_token \
  -e GHOST_HOST=http://ghostfolio:3333 \
  -e GHOST_ACCOUNT_NAME=Kraken \
  -e GHOST_CURRENCY=USD \
  -e SYNC_SINCE=2024-01-01 \
  -v ./mapping.yaml:/app/mapping.yaml \
  ghcr.io/obol89/ghostfolio-kraken-sync:latest
```

### Without Docker

```bash
pip install -r requirements.txt
export KRAKEN_API_KEY=your_key
export KRAKEN_API_SECRET=your_secret
export GHOST_TOKEN=your_ghost_token
export GHOST_HOST=http://localhost:3333
export GHOST_ACCOUNT_NAME=Kraken
export GHOST_CURRENCY=USD
python kraken_to_ghostfolio.py
```

## Docker Compose / Portainer

```yaml
services:
  kraken-sync:
    image: ghcr.io/obol89/ghostfolio-kraken-sync:latest
    container_name: ghostfolio-kraken-sync
    restart: unless-stopped
    depends_on:
      - ghostfolio
    environment:
      TZ: Europe/Zurich
      KRAKEN_API_KEY: your_key
      KRAKEN_API_SECRET: your_secret
      GHOST_TOKEN: your_ghost_token
      GHOST_HOST: http://ghostfolio:3333
      GHOST_ACCOUNT_NAME: Kraken
      GHOST_CURRENCY: CHF
      GHOST_PLATFORM_ID: your_platform_id
      MAPPING_FILE: /app/mapping.yaml
      SKIP_CRYPTO_TRANSFERS: "true"
      API_CALL_DELAY: "1.0"
      CRON: "0 */6 * * *"
    volumes:
      - ./mapping.yaml:/app/mapping.yaml
    networks:
      - ghostfolio

networks:
  ghostfolio:
    external: true
```

Use `http://ghostfolio:3333` (internal Docker network hostname) rather than an external IP or localhost. Make sure the `ghostfolio` network name matches the network your Ghostfolio instance is on.

In Portainer, paste this as a stack definition and deploy it directly.

## How staking rewards are handled

Kraken staking rewards appear in the Ledgers API with `type=staking`. Each reward entry contains:

- The staked asset (e.g. `DOT.S`, `ETH.S`, `SOL.S`)
- The reward amount
- A timestamp

The tool:

1. Strips the staking suffix (`.S`, `.M`, `.B`, `.F`) to get the base asset
2. Creates a Ghostfolio `INTEREST` activity with the reward quantity
3. Uses your `GHOST_CURRENCY` as the quote currency for the Yahoo Finance symbol
4. Sets `unitPrice` to 0, letting Ghostfolio resolve the price from Yahoo Finance

Each staking reward is deduplicated using the comment `KRAKEN#STAKE#{ledger_refid}`.

## How crypto deposits and withdrawals are handled

By default, crypto deposits and withdrawals are **skipped** (`SKIP_CRYPTO_TRANSFERS=true`). This is because most crypto transfers between wallets are not actual purchases or sales - they are just moving assets between your own accounts.

If you set `SKIP_CRYPTO_TRANSFERS=false`:

- **Crypto deposits** (from external wallet to Kraken) are recorded as `BUY` activities with `unitPrice=0`. A WARNING is logged because the actual cost basis is unknown.
- **Crypto withdrawals** (from Kraken to external wallet) are recorded as `SELL` activities with `unitPrice=0`. A WARNING is logged because this may not reflect an actual sale.
- **Fiat deposits and withdrawals** are always skipped regardless of this setting, as they only affect cash balance and not portfolio positions.

## Kraken symbol quirks

Kraken uses non-standard asset naming that predates modern cryptocurrency conventions:

### Prefixed names

Assets listed early on Kraken have `X` or `Z` prefixes:

| Kraken name | Standard name | Type |
|---|---|---|
| `XXBT` | `BTC` | Crypto |
| `XETH` | `ETH` | Crypto |
| `XXRP` | `XRP` | Crypto |
| `XLTC` | `LTC` | Crypto |
| `XMLN` | `MLN` | Crypto |
| `ZUSD` | `USD` | Fiat |
| `ZEUR` | `EUR` | Fiat |
| `ZGBP` | `GBP` | Fiat |
| `ZCAD` | `CAD` | Fiat |
| `ZJPY` | `JPY` | Fiat |

Newer assets use standard names: `DOT`, `SOL`, `ADA`, `MATIC`, etc.

### Staking variants

Staked assets have suffixes indicating the staking type:

| Suffix | Meaning |
|---|---|
| `.S` | Staked |
| `.M` | Opt-in rewards |
| `.B` | Yield-bearing |
| `.F` | Kraken Rewards |

These are stripped during normalization - `DOT.S` and `DOT` are treated as the same asset.

### Trading pairs

Pairs combine both naming conventions:

- `XXBTZUSD` = BTC/USD (old style, both prefixed)
- `XETHZEUR` = ETH/EUR (old style, both prefixed)
- `DOTEUR` = DOT/EUR (new base, old quote without prefix)
- `SOLUSD` = SOL/USD (new style, both standard)

The tool handles all these patterns automatically.

## Troubleshooting

### "Kraken API error: EAPI:Invalid key"

Your API key is incorrect or has been revoked. Generate a new key in Kraken's API settings.

### "Kraken API error: EAPI:Invalid nonce"

The nonce (timestamp-based) was lower than the previous request's nonce. This can happen if your system clock is inaccurate or if you run multiple instances simultaneously. Wait a few seconds and try again. Only run one instance at a time.

### "Kraken API error: EGeneral:Permission denied"

Your API key does not have the required permissions. Ensure **Query funds**, **Query closed orders & trades**, **Query ledger entries**, and **Export data** are all enabled.

### Rate limiting

Kraken uses a call counter that increases by 1-2 per API call and decays at 0.33/second for starter tier accounts. The default `API_CALL_DELAY` of 1 second between paginated requests should avoid rate limits. If you hit rate limits, increase the delay:

```
API_CALL_DELAY=2.0
```

### "not valid for the specified data source YAHOO"

An imported symbol is not recognised by Yahoo Finance. Check the unmapped symbols output at the end of the run and add the correct Yahoo Finance symbol to your mapping file.

### Import fails but activities were expected

The tool logs the error and continues rather than crashing. Fix the failing symbol in your mapping file and re-run - duplicate detection will skip already-imported activities.

### Portfolio values are wrong after sync

Run **Gather All Data** in Ghostfolio **Admin** - **Market Data**. This fetches historical prices for all symbols. Without this step, performance charts and current values will be missing or incorrect.

### Token expiry

The Ghostfolio auth token expires. Regenerate it using the curl command in the Ghostfolio Setup section and update your container environment variable.

### Staking rewards showing wrong values

Staking rewards are imported with `unitPrice=0`. Ghostfolio should resolve the price from Yahoo Finance. If values look wrong, check that the Yahoo Finance symbol exists and has data for the relevant date. Run **Gather All Data** to refresh prices.

### Large history causing timeouts

Use `SYNC_SINCE` to limit how far back the sync goes. For example, set `SYNC_SINCE=2024-01-01` on the first run to import only recent history, then remove it for subsequent runs (duplicate detection will prevent re-importing).

## Limitations

- **No margin/futures support** - only spot trades are synced. Margin positions and futures contracts from Kraken are not handled.
- **Yahoo Finance data quality** - price data can have gaps, delays, or missing metadata for smaller or newer cryptocurrencies.
- **Crypto transfer cost basis** - when `SKIP_CRYPTO_TRANSFERS=false`, deposits and withdrawals are recorded with `unitPrice=0` because the actual cost basis or sale price is unknown.
- **Single account** - unlike IBKR, Kraken typically has one account per user, so multi-account support is not needed.
- **Token management** - the Ghostfolio auth token expires and requires manual renewal.
- **Rate limits** - accounts with large histories and starter-tier API rate limits may require higher `API_CALL_DELAY` values, making the sync slower.
- **Staking reward pricing** - staking rewards are imported with `unitPrice=0`. Ghostfolio resolves the price from Yahoo Finance, which may not have exact pricing for the reward timestamp.
