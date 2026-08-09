# ghostfolio-kraken-sync

Sync Kraken trades, staking and Earn rewards, airdrops, and deposit/withdrawal activity to a self-hosted [Ghostfolio](https://ghostfol.io) instance.

This tool connects to the Kraken API, fetches your complete trading history and your whole ledger, classifies each ledger entry, maps Kraken's non-standard asset names to Yahoo Finance symbols, and pushes everything into Ghostfolio. It handles pagination, deduplicates activities, updates cash balances, reconciles holdings against your Kraken balances, and supports both one-off and scheduled (cron) execution.

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
| `GHOST_CURRENCY` | No | `USD` | Currency used for zero-price activities. The **cash balance** always follows the Ghostfolio account's own currency, not this; a mismatch is warned about |
| `GHOST_PLATFORM_ID` | No | | Platform ID for Kraken in Ghostfolio |
| `GHOST_ACCOUNT_NAME` | No | `Kraken` | Ghostfolio account name |
| `MAPPING_FILE` | No | `/app/mapping.yaml` | Path to symbol mapping YAML |
| `SKIP_CRYPTO_TRANSFERS` | No | `true` | Skip crypto deposit/withdrawal sync |
| `API_CALL_DELAY` | No | `1.0` | Seconds between paginated API calls |
| `SYNC_SINCE` | No | | ISO date to limit history (e.g. `2024-01-01`) |
| `LEDGER_FETCH_MODE` | No | `all` | `all` fetches the whole ledger and classifies it locally. `filtered` restores the old staking/deposit/withdrawal passes, which **cannot see Kraken Earn rewards or airdrops** |
| `SYNC_ARGS` | No | | Command line flags for containerised runs, e.g. `--reconcile` |
| `CRON` | No | | Cron expression for scheduled runs |
| `TZ` | No | | Timezone for cron scheduling (e.g. `Europe/Zurich`) |

## Command line flags

A bare run performs an ordinary sync, so cron and Docker need no changes. In a container, pass flags through `SYNC_ARGS`.

| Flag | Effect |
|---|---|
| `--reconcile` | Read-only. Compares Kraken balances against Ghostfolio positions and prints a diff table. Makes no writes at all |
| `--dry-run` | Builds the activities and prints them as JSON, but imports nothing and leaves the cash balance alone |
| `--dump-ledger-types` | Read-only. Prints a histogram of the `(type, subtype)` pairs your ledger actually contains. Needs **only** `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` — it never contacts Ghostfolio, so it works before Ghostfolio is set up or while it is down |
| `--verbose` / `-v` | Debug logging |

Exit codes: `0` on success, `1` if anything failed during the run — an API error, an activity Ghostfolio would refuse, an unrecognised ledger type, or unexplained drift found by `--reconcile`.

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

A safe first look at an existing setup, since neither writes anything:

```bash
python kraken_to_ghostfolio.py --reconcile
python kraken_to_ghostfolio.py --dump-ledger-types
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite mocks every HTTP call with `unittest.mock`, so it never contacts Kraken or Ghostfolio and needs no credentials. Test dependencies are separate from `requirements.txt` and never enter the Docker image.

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

Rewards are imported as **`BUY` activities with `unitPrice=0`**, not as `INTEREST`.

That is not a stylistic choice. In Ghostfolio, `getFactor(activityType)` returns `+1` for `BUY`, `-1` for `SELL` and **`0` for every other type**, and a position's quantity accumulates as `quantity × factor`. An `INTEREST` activity therefore contributes exactly zero quantity, whatever its `quantity` field says. `INTEREST` cannot carry the value either, because Ghostfolio computes `interest = quantity × unitPrice` and the unit price of a reward received in kind is zero. Earlier versions of this tool imported rewards as `INTEREST`, which is why holdings drifted below the real Kraken balance by the whole reward history.

There is a third reason. Ghostfolio forces every non-investment activity type — which includes `INTEREST` — onto `DataSource.MANUAL` with a UUID or `GF_`-prefixed symbol at import time. `BUY` is an investment type, so it may keep `dataSource: YAHOO` and a plain `BTCUSD` symbol, which puts the reward on the *same* asset profile as your trades instead of splitting the holding across two positions.

**The trade-off**, stated plainly: a zero `unitPrice` records the reward at a zero cost basis. Quantity and market value become correct; realised-gain figures treat the reward as pure profit, and it is no longer categorised as income. Quantity correctness wins, because a wrong holding corrupts every number downstream of it.

### What gets ingested

The whole ledger is fetched in one pass and classified locally. Fetching filtered by `type` is not enough: `earn` is not a member of Kraken's REST `type` filter enum, so no filtered request can return Kraken Earn rewards at all.

| Ledger entry | Treatment |
|---|---|
| `staking` reward (legacy Kraken Staking) | `BUY`, comment `KRAKEN#REWARD#{ledger_id}` |
| `earn` + reward subtype (Kraken Earn) | `BUY`, comment `KRAKEN#REWARD#{ledger_id}` |
| Airdrop, by type or subtype | `BUY`, comment `KRAKEN#AIRDROP#{ledger_id}` |
| `spend` / `receive` (Convert, instant buy/sell, dust sweeping) | `BUY`/`SELL`, comment `KRAKEN#CONVERT#{ledger_id}` — see below |
| `transfer` with no subtype | `BUY`/`SELL` at `unitPrice=0`, comment `KRAKEN#XFER#{ledger_id}`, warned per occurrence |
| `transfer` to/from **futures** | Same as above — **not** internal, see below |
| `earn` allocation / deallocation / migration | Skipped — moves an existing balance |
| `transfer` between spot and **staking** wallets | Skipped — both legs, so they net to zero |
| `trade` **base** leg, no fee (including subtype `tradespot`) | Skipped — already imported from TradesHistory |
| `trade` base leg with a **base-denominated fee** | `SELL` of the fee only, comment `KRAKEN#TRADEFEE#{ledger_id}` — see below |
| `trade` **quote** leg, non-fiat | `BUY`/`SELL` at `unitPrice=0`, comment `KRAKEN#TRADEQ#{ledger_id}` — see below |
| `trade` quote leg, fiat | Skipped — cash |
| `deposit` / `withdrawal` | See the next section |
| Fiat, of any type | Skipped — cash is handled by the balance update |
| Anything unrecognised | **Reported and the run exits non-zero** |

### Conversions, instant buys and dust sweeping

Kraken Convert, instant buy/sell and dust sweeping all emit a `spend` leg and one or more `receive` legs sharing a single `refid`. They are ledger-only: those refids never appear in TradesHistory, so there is no cross-source duplicate to guard against and each leg dedupes on its own `KRAKEN#CONVERT#{ledger_id}` comment.

What a leg means depends on the other legs in its group, so the group as a whole decides:

| Group shape | Result |
|---|---|
| One fiat leg + one crypto leg (instant buy or sell) | **One priced activity** on the crypto leg: `unitPrice = \|fiat amount\| / crypto quantity`, denominated in that fiat currency. The fiat leg is dropped |
| Crypto ↔ crypto | One `unitPrice=0` activity per leg, `SELL` for the outgoing side, `BUY` for the incoming |
| Dust sweeping (N spend legs, 1 receive leg) | One `unitPrice=0` activity per crypto leg |
| All legs fiat (e.g. CHF → USD) | Nothing — a currency exchange is cash, not a position |
| A lone leg with no counterpart in the fetch window | Reported as unrecognised, since it can be neither priced nor given a direction |

The priced case matters: for an instant buy, **the fiat leg is the real cost basis**. Importing it at `unitPrice=0` like the other conversions would throw away the one price Kraken actually gave us and corrupt every P&L figure that depends on it. The zero-price cases are the ones where no price exists in the ledger at all.

Bare `transfer` entries — no subtype — are the residual case. Kraken uses them for account-to-account moves and delisting sweep-outs, both of which genuinely change the holding, unlike the spot/staking/futures subtypes that only shuffle a balance between wallets. The quantity is trusted and the price is not, so each one is imported at `unitPrice=0` and logged individually as ambiguous.

Quantity is `amount − fee`, and the activity's `fee` field is left at zero. Kraken settles a ledger entry as `balance_new = balance_old + amount − fee` with the fee denominated in the entry's *own asset*, whereas Ghostfolio's `fee` field is denominated in the activity's currency — so copying it across would book a 0.0001 BTC fee as 0.0001 USD.

Suffixed assets (`.S` staked, `.M` opt-in rewards, `.B` yield-bearing, `.F` Kraken Rewards) are collapsed onto the base asset, so a reward credited to a bonded balance lands on the same position as your spot holding. That collapse is also why the internal transfers above must be skipped: both legs resolve to the same symbol and would otherwise double-count.

### Trade quote legs

A trade writes **two** ledger entries — `+base` and `−quote` — but the trade import turns the whole trade into a single activity on the base asset. Importing the base leg again would double every traded position, so it is skipped. The quote leg is different:

- **Fiat quote** (`XBTCHF` → `−CHF`) is cash and belongs to the balance update.
- **Crypto or stablecoin quote** (`XBTUSDC` → `−USDC`) is a real position that nothing else ever decreases. Left out, a USDC balance climbs indefinitely — one account showed +100.24 USDC in Ghostfolio against 0.40 actually held.

Which leg is which is decided per `refid`: a trade's ledger legs carry the trade id, so the base asset of the pair the trade import resolved identifies the covered leg. If the trade is not in the fetch window, the leg is assumed covered — guessing the other way would double a position.

### Fees charged in the base asset

Kraken sometimes takes a trade's fee out of the base asset rather than the quote. `TradesHistory` reports `vol` **gross**, and the trade import uses `vol` directly for the activity quantity, so that fee never reduces the position. On one account three such fees — 0.00000141, 0.00000501 and 0.00001579 BTC — summed to 0.00002221, which was precisely the residual the reconcile could not explain.

The correction is a separate `SELL` of the fee alone, at `unitPrice=0`, under `KRAKEN#TRADEFEE#{ledger_id}`. The base leg stays "covered" for its amount; only the fee is corrected, and only when there is one — a fee-free trade emits nothing.

This is deliberately **additive**. Netting the fee into the trade import's quantity would be tidier arithmetic, but Ghostfolio's import creates activities and never updates them, so every trade already imported under a `KRAKEN#` comment would be left at its old gross quantity with no way to revise it. Quote legs need no such treatment: they already debit `amount + fee`.

### Netting internal transfers

Spot ↔ staking moves are detected structurally rather than by name: within one `refid`, two or more entries on the same normalized asset with opposite signs. A trade also pairs entries under one `refid`, but across two *different* assets, so trades are unaffected; a reward is a single entry per `refid`, so rewards are unaffected.

Being taxonomy-free is the point — it catches Earn subtypes Kraken has not shipped yet. It also has to be structural: dropping only the negative leg, which a naive "skip non-positive amounts" rule does, keeps the positive leg and invents quantity out of nothing.

**Futures transfers are not internal**, despite looking like the staking ones. The asymmetry is worth stating:

| | Counterleg in the spot ledger? | Covered by `Balance`? | Net effect |
|---|---|---|---|
| `spottostaking` / `stakingtospot` etc. | Yes | Yes, bonded balances included | Invisible — skip both legs |
| `spottofutures` / `spotfromfutures` | **No** | **No**, futures wallet excluded | Real change — import it |

Kraken's Ledgers endpoint covers the spot wallet only, so a futures transfer is a one-legged event as far as this tool can see. Treating it as internal would drop a genuine balance change.

### Migrating from an older version

If you ran an earlier version, your Ghostfolio account still contains `KRAKEN#STAKE#...` `INTEREST` rows. Every run lists them with their Ghostfolio ids.

They are safe to leave. They contribute zero quantity (`getFactor` scores them 0) and zero value (`unitPrice` is 0), so they double-count nothing — they are only clutter. Corrected rewards arrive under the new `KRAKEN#REWARD#` namespace, which is precisely what lets them be imported without deduplication mistaking them for the old rows. Delete the listed activities in the Ghostfolio UI whenever you like; nothing else depends on it.

## How crypto deposits and withdrawals are handled

By default, crypto deposits and withdrawals are **skipped** (`SKIP_CRYPTO_TRANSFERS=true`). This is because most crypto transfers between wallets are not actual purchases or sales - they are just moving assets between your own accounts.

If you set `SKIP_CRYPTO_TRANSFERS=false`:

- **Crypto deposits** (from external wallet to Kraken) are recorded as `BUY` activities with `unitPrice=0`. A WARNING is logged because the actual cost basis is unknown.
- **Crypto withdrawals** (from Kraken to external wallet) are recorded as `SELL` activities with `unitPrice=0`. A WARNING is logged because this may not reflect an actual sale.
- **Fiat deposits and withdrawals** are always skipped regardless of this setting, as they only affect cash balance and not portfolio positions. Fiat is recognised through its state suffixes too, so `EUR.HOLD` is treated as cash exactly like `EUR`.

With `SKIP_CRYPTO_TRANSFERS=true`, `--reconcile` knows those entries are missing on purpose: it nets their quantity per asset into an `EXP.ABSENT` column and attributes any gap they close to the setting rather than reporting it as drift. This matters when a deposit funded a conversion — the conversion imports as a `SELL` while the deposit does not, driving the Ghostfolio position negative for a perfectly ordinary reason.

## Reconciling holdings

`--reconcile` is read-only. It sums your Kraken balances per asset (including the `.S`/`.M`/`.B`/`.F` variants), computes what Ghostfolio holds using the same factor rule Ghostfolio itself applies, and prints the difference:

```
====================================================================================================
Reconciliation: Kraken balances vs Ghostfolio positions

SYMBOL                 KRAKEN       GHOSTFOLIO            DELTA       IGNORED    EXP.ABSENT  LIKELY CAUSE
----------------------------------------------------------------------------------------------------
BABYUSD           37.50000000       0.00000000     +37.50000000    0.00000000   +0.00000000  missing from Ghostfolio entirely
BTCUSD             0.18350000       0.15800000      +0.02550000    0.02550000   +0.00000000  explained: 2 non-BUY/SELL row(s) hold 0.02550000 that Ghostfolio ignores
DOTUSD             0.00000000      -5.00000000      +5.00000000    0.00000000   +5.00000000  explained: 1 deposit(s)/withdrawal(s) excluded by SKIP_CRYPTO_TRANSFERS net +5.00000000

IGNORED    = quantity sitting in activities Ghostfolio does not count toward
             position size (INTEREST, DIVIDEND, FEE, ITEM, LIABILITY). Only
             BUY and SELL move quantity.
EXP.ABSENT = net quantity of deposits and withdrawals Ghostfolio was never
             sent, because SKIP_CRYPTO_TRANSFERS suppresses them. Absent by
             configuration, not by fault.
Cash (not a position): CHF 196.75 on Kraken, 196.75 on the Ghostfolio account.
3 asset(s) differ, 0 reconciled. Unexplained drift on 1 asset(s).
====================================================================================================
```

**The two right-hand columns are the diagnostic.** Several different situations produce an identical-looking shortfall, and these tell them apart:

- `DELTA == IGNORED` — the reward *was* imported, into an activity type that does not move the position. That is the `BTCUSD` row: inert `INTEREST` rows from an older version.
- `DELTA == EXP.ABSENT` — a deposit or withdrawal Ghostfolio was never sent because `SKIP_CRYPTO_TRANSFERS` is on. That is the `DOTUSD` row: a conversion imported as a `SELL` while the deposit that funded it was suppressed, so the Ghostfolio position went negative. Nothing is wrong; the configuration says so.
- `DELTA > 0` with both at zero — the entry was **never fetched at all**. That is the `BABYUSD` row: an airdrop that no filtered ledger pass would ever have returned.

The exit code keys off the *residual* (`delta − ignored − expected absent`), not the raw delta, so a fully attributed difference does not hold a scheduled run hostage. Only the genuinely unexplained remainder exits non-zero.

**Every symbol counted in the exit code appears in the table.** That invariant is enforced in one place, so the printed report and the failure list cannot drift apart — a run that fails on seventeen symbols while printing four is a bug, not a display quirk.

Leftover MANUAL asset profiles are attributed rather than failed. Ghostfolio forces non-investment types onto `DataSource.MANUAL` with a UUID or `GF_`-prefixed symbol, so every old `INTEREST` staking row sits on a synthetic profile of its own. Those profiles hold quantity that counts toward no position and have no Kraken asset behind them, so they are reported as *"legacy staking profile (superseded), zero position quantity — not drift"* and excluded from the failure set. Deleting the rows removes the profiles too.

Ledger-only activities — conversions, rewards, airdrops and bare transfers — are checked too, and reported in their own block. They never appear in TradesHistory, so without that check a conversion that failed to import would surface as unattributed drift rather than being named.

Trades present on Kraken but absent from Ghostfolio are reported separately, grouped by currency. Grouping matters because `/api/v1/import` is all-or-nothing: a handful of trades in a currency Ghostfolio rejects used to take every other trade in the batch down with them. Those trades are now held back individually and reported, so one unusable pair costs only itself.

### Inspecting your ledger taxonomy

Kraken's REST and WebSocket references enumerate different ledger types, and neither lists `earn`. The classification tables in this tool are therefore part documented, part inferred. `--dump-ledger-types` prints the `(type, subtype)` pairs your account actually contains, which is the quickest way to confirm the classifier matches reality:

```
python kraken_to_ghostfolio.py --dump-ledger-types
```

Anything the classifier does not recognise is reported and fails the run rather than being skipped quietly — a new Kraken reward type must not be able to reintroduce the drift this tool exists to remove.

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
- `XBTUSDC` = BTC/USDC (new base, stablecoin quote)

The tool handles all these patterns automatically.

### Stablecoin quotes

Ghostfolio validates the activity currency against ISO 4217, and `USDC`, `USDT` and friends are not currency codes. Pairs quoted in a stablecoin are therefore reported in the fiat currency it tracks, so `XBTUSDC` is imported as a BTC trade in `USD`. The peg is 1:1, so no conversion is applied to the price.

Pairs quoted in crypto (`ETHXBT`) are not remapped this way - the price really is denominated in BTC, and relabelling it as USD would import a wrong number. Those need a mapping file entry.

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

### "currency must be a valid ISO4217 currency code"

A pair is quoted in something Ghostfolio does not accept as a currency. Known stablecoin quotes (`USDC`, `USDT`, `DAI`, `PYUSD` and similar) are converted to their pegged fiat automatically; anything else is logged as a warning naming the pair. Add a mapping file entry pointing that pair at a symbol quoted in a real currency, for example:

```yaml
symbol_mapping:
  XBTUSDC: BTCUSD
```

### Import fails but activities were expected

The tool logs the error and continues rather than crashing. Fix the failing symbol in your mapping file and re-run - duplicate detection will skip already-imported activities.

### Portfolio values are wrong after sync

Run **Gather All Data** in Ghostfolio **Admin** - **Market Data**. This fetches historical prices for all symbols. Without this step, performance charts and current values will be missing or incorrect.

### Token expiry

The Ghostfolio auth token expires. Regenerate it using the curl command in the Ghostfolio Setup section and update your container environment variable.

### Holdings are lower in Ghostfolio than on Kraken

Run `--reconcile`. It attributes each per-asset difference to a cause and tells the two usual faults apart:

- Rewards imported by an older version as `INTEREST` contribute no quantity. They show as `DELTA == IGNORED`, and the run lists their Ghostfolio ids so you can delete them. The corrected `BUY` rows import alongside them under a different comment namespace.
- Rewards that were never fetched show as `DELTA > 0` with `IGNORED == 0`. Make sure `LEDGER_FETCH_MODE` is not set to `filtered`, which cannot see Kraken Earn rewards or airdrops.

### Reward values look wrong

Rewards are imported with `unitPrice=0`, so Ghostfolio resolves the price from Yahoo Finance. If values look wrong, check the Yahoo Finance symbol exists and has data for the relevant date, then run **Gather All Data** to refresh prices. Note that a zero unit price also means a zero cost basis, so unrealised gain on a reward is overstated by design.

### Large history causing timeouts

Use `SYNC_SINCE` to limit how far back the sync goes. For example, set `SYNC_SINCE=2024-01-01` on the first run to import only recent history, then remove it for subsequent runs (duplicate detection will prevent re-importing).

Be careful with rewards, though: a truncated reward history is a permanent quantity shortfall, not just a shorter chart. When `SYNC_SINCE` is set the tool reports how many ledger entries fall before the cutoff so the omission is visible.

## Limitations

- **No margin/futures support** - only spot trades are synced. Margin positions and futures contracts from Kraken are not handled.
- **Yahoo Finance data quality** - price data can have gaps, delays, or missing metadata for smaller or newer cryptocurrencies.
- **Crypto transfer cost basis** - when `SKIP_CRYPTO_TRANSFERS=false`, deposits and withdrawals are recorded with `unitPrice=0` because the actual cost basis or sale price is unknown. Quantities are net of the Kraken fee in the right direction: a withdrawal sells `amount + fee`, a deposit buys `amount − fee`.
- **Futures positions are not tracked** - only the spot transfer leg is visible, so a balance moved to the futures wallet leaves the portfolio as a `SELL` and returns as a `BUY`. What happens to it in between is outside this tool's reach.
- **Single account** - unlike IBKR, Kraken typically has one account per user, so multi-account support is not needed.
- **Token management** - the Ghostfolio auth token expires and requires manual renewal.
- **Rate limits** - accounts with large histories and starter-tier API rate limits may require higher `API_CALL_DELAY` values, making the sync slower. Ledgers and TradesHistory each cost 2 against Kraken's counter, so a first full backfill of a large account can take tens of minutes. Avoid scheduling runs hourly while it is in progress.
- **Reward cost basis** - rewards are imported at `unitPrice=0`, which is quantity-correct but gives them a zero cost basis, so realised-gain figures treat them as pure profit and they are not categorised as income. Pricing each reward at its market value on receipt would need a public OHLC call per reward.
- **Crypto-to-crypto conversions and dust sweeps have no cost basis** - unlike an instant buy, which is priced from its fiat leg, these carry no price anywhere in the ledger and are imported at `unitPrice=0`. Quantities stay correct; unrealised gain on those legs is overstated.
- **Bare transfers are ambiguous** - Kraken gives no subtype for account-to-account moves or delisting sweep-outs, so they are imported at `unitPrice=0` and each one is logged for you to check.
- **Delisting swaps are not handled** - a conversion such as LUNA to LUNC arrives as a pair of ledger entries on two *different* assets, which the same-asset netting rule cannot pair. Those entries are reported as unrecognised rather than guessed at, and show up as reconcile drift until you record them by hand.
- **Ledger taxonomy is partly inferred** - Kraken's REST and WebSocket references enumerate different ledger types and neither lists `earn`. Run `--dump-ledger-types` against your own account to confirm the classification matches reality.
