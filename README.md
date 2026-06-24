# sptrader

Indicator-permutation backtesting & strategy-search toolkit for the S&P 500
(and any other Yahoo Finance symbol). It pulls OHLCV data as **1h candles**,
permutes technical-indicator strategies across categories, backtests each with
transaction costs and an in-sample / out-of-sample split, and surfaces the
**winning strategies in a web dashboard** filtered by a success-rate threshold
(70% by default).

Everything runs in **Docker** — no Python or packages on your host. Copy the
repo to any server with Docker and the same commands work.

## Architecture


| Stage      | What it does                                                                              | Resumable?                            |
| ---------- | ----------------------------------------------------------------------------------------- | ------------------------------------- |
| `ingest`   | Pull Yahoo OHLCV (default 720 days, 1h candles), store in Postgres                        | yes (idempotent upsert + checkpoints) |
| `optimize` | Permute cross-category indicator strategies (dense, up to 4), backtest IS/OOS, store results | yes (skips already-tested signatures) |
| `web`      | Dashboard: strategies above a success-rate threshold, with their indicators               | live (reads the DB)                   |


Postgres persists candles, run progress, and every tested permutation, so an
interrupted build or run continues exactly where it stopped (`Ctrl-C` safe).

## How the flow works (end to end)

```
 env / .env ─▶ ingest ─▶ Postgres(candles) ─▶ optimize ─▶ Postgres(results) ─▶ web / report
                                                  │
                                       3 worker processes (parallel)
```

**0. Config.** Everything is driven by env vars (`.env` or `-e` on the command).
Key knobs: `SYMBOL`, `LOOKBACK_DAYS` (default **720** ≈ 2 years),
`COST_BPS`, `TRAIN_FRACTION`, `WORKERS` (default **3**). Candles are always 1h.
See `sptrader/config.py`.

**1. Ingest** (`sptrader/data/ingest.py`)

1. Download 1h OHLCV from Yahoo for `SYMBOL` over `LOOKBACK_DAYS`.
2. Upsert into `candles` (idempotent: `ON CONFLICT` on `symbol,interval,ts`),
  and record an `ingestion_checkpoint` (last bar stored). Re-running only adds
   new bars.

**2. Optimize** (`sptrader/optimize/`)

1. **Generate** strategy specs *lazily* (`permutations.iter_strategies`) in a
  deterministic order. A spec = a set of indicator instances + a direction mode
   (`long_only`/`long_short`) + combine logic (`and`/`or`) + optional ADX gate.
2. **Distribute**: specs are streamed in batches to `WORKERS` worker processes
  (default 3). Each worker holds the candle frame once and caches each
   indicator's vote series, so indicators are computed once, not per strategy.
3. **Backtest each** (`backtest/engine.py`): build a target position per bar,
  trade it on the *next* bar (`shift(1)`, no look-ahead), P&L close-to-close,
   charge `COST_BPS` per side on turnover. Split chronologically into in-sample
   (first `TRAIN_FRACTION`) and out-of-sample.
4. **Score**: `score = min(IS Sharpe, OOS Sharpe)` — rewards strategies that work
  in *both* halves and punishes the classic overfit (great IS, dead OOS). Also
   computes the trade-level **success rate** (winning trades / total trades).
5. **Store** each result in `strategy_results` (`ON CONFLICT DO NOTHING`).
  `optimization_runs.completed_combos` is a **resume cursor**: on restart the
   run `islice`s past the already-done prefix — no recompute, no millions of
   signatures held in memory.

**3. Read** (`reporting.py` + `web/`)

- `report` / the dashboard rank and filter strategies by success rate (≥ 70% by
default), Sharpe, or return, and expand the per-strategy trade detail.

How a strategy turns indicators into a position:

- Each indicator instance emits a per-bar **vote** in `{-1, 0, +1}` (short / flat
/ long). Trend & breakout signals hold a regime; mean-reversion signals hold a
long until an exit level.
- **combine = and**: act only when *all* indicators agree (unanimous).
**combine = or**: act when *any* votes a side and none opposes it.
- **mode = long_only** clips shorts to flat; **long_short** keeps both.
- An optional **ADX gate** (`--adx-filters 25`) forces flat when trend strength
is below the threshold.

## Quick start (Docker only)

```bash
# 1. (optional) configure — defaults are fine for a first run
cp .env.example .env

# 2. start the database
docker compose up -d postgres

# 3. pull market data into Postgres (~2 years of 1h bars)
docker compose run --rm app ingest

# 4. run (or resume) the strategy search
docker compose run --rm app optimize

# 5. serve the dashboard, then open http://localhost:8000
docker compose up -d app
```

Re-running `optimize` after a stop **resumes**; it never re-tests a permutation
already in the database.

## What to run right now

Use this sequence after pulling the latest code (or upgrading from an older
60-day / smaller-grid setup). **You do not need to wipe the database.**

```bash
# 1. Stop any optimize that is still running (Ctrl-C in that terminal).

# 2. Rebuild the app image so it picks up code changes
docker compose build app

# 3. Start Postgres if it is not already up
docker compose up -d postgres

# 4. Re-ingest — upserts ~720 days of 1h candles (adds history, updates overlap)
docker compose run --rm app ingest

# 5. Search from scratch on the new data + new signal logic. Plain `optimize` is
#    already the full search (dense grids, AND/OR, both modes, up to 4
#    cross-category indicators); --restart discards any half-finished run.
docker compose run --rm app optimize --restart

# 6. Dashboard (separate terminal)
docker compose up -d app
# open http://localhost:8000
```

**Why `--restart`?** Each optimization run is keyed by a hash of the search
config *and* the candle fingerprint (bar count + date range). Re-ingesting 720
days creates a **new** run automatically. `--restart` is still required if you
already started a 720-day run **before** the latest signal/grid fixes — without
it, the cursor would resume mid-run and mix old and new backtest logic.

**What happens to old data?**


| What                                       | Action                                                                           |
| ------------------------------------------ | -------------------------------------------------------------------------------- |
| `candles` table                            | Kept. `ingest` upserts; no volume delete needed.                                 |
| Old optimization runs (e.g. 60-day window) | Kept in the DB for reference; ignored by a new run with a different fingerprint. |
| In-progress run on the *same* fingerprint  | `--restart` deletes that run + its results and starts at combo 0.                |


Check progress anytime:

```bash
docker compose run --rm app status
```

The plain `optimize` command **is** the full search now (dense grids, AND/OR,
both modes, up to 4 cross-category indicators — a multi-day run). For a quick
smoke test first, shrink the combo size:

```bash
docker compose run --rm app optimize --max-combo-size 2 --dry-run  # preview size/ETA
docker compose run --rm app optimize --max-combo-size 2            # small, fast run
```

## The dashboard

`http://localhost:8000` shows a sortable table. Each row is one strategy:

- **Success rate** — fraction of trades that closed in profit (with IS / OOS
split shown beneath). The slider filters to strategies at or above a threshold
(defaults to **70%**).
- **Indicators that succeeded** — the indicator(s) and their parameters that make
up the strategy, colour-coded by category (trend / momentum / volatility / volume).
- **Score** — `min(IS Sharpe, OOS Sharpe)`, a robustness measure that punishes
strategies that only look good in-sample.
- OOS Sharpe, OOS return, OOS max drawdown, trade count, mode, ADX gate.

> A high in-sample win rate alone is **not** proof. Prefer rows where the
> out-of-sample numbers hold up too. See `RESEARCH.md` for the methodology and
> honest caveats.

## Changing the instrument

The search is generic. Override the symbol per command or in `.env`:

```bash
# Nasdaq-100 ETF instead of S&P 500
docker compose run --rm -e SYMBOL=QQQ app ingest
docker compose run --rm -e SYMBOL=QQQ app optimize
```

Other useful env vars (see `.env.example`): `LOOKBACK_DAYS` (default `720`, Yahoo
1h cap ~730 days), `WORKERS` (default `3`), `COST_BPS` (per-side transaction
cost), `TRAIN_FRACTION` (IS/OOS split).

## Search size

The search runs in parallel worker processes (default **3**, via `WORKERS`) and
the container is capped at **2 GB RAM** (`mem_limit` in compose). Specs are
streamed lazily, so the space stays within the cap.

Preview the size and ETA before committing with `--dry-run`:

```bash
docker compose run --rm app optimize --dry-run
```

Plain `optimize` is the default search: dense grids, AND/OR vote logic, both
direction modes, up to **4 cross-category** indicators per strategy (one trend +
momentum + volatility + volume — ~147M strategies, a multi-day run). Shrink it
with `--max-combo-size` for much faster runs.


| Command                        | Strategies | ~ETA (3 workers) |
| ------------------------------ | ---------- | ---------------- |
| `optimize --max-combo-size 2`  | ~209k      | ~10 min          |
| `optimize --max-combo-size 3`  | ~9.8M      | ~4–5h            |
| `optimize` *(default, size 4)* | ~147M      | ~3 days          |

Exact totals: `optimize --dry-run`. Counts include the FVG signals and the
1h-tuned `DENSE_GRIDS`. The search is resumable, so a multi-day run survives
Ctrl-C / restarts (re-run the same command to continue).

Flags:

- `--max-combo-size N` — max indicators per strategy (default **4**, one per
category). Lower it (2-3) for much faster runs; cross-category tops out at 4.
- `--adx-filters 25` — ADX trend-strength gate(s); negative value = no gate.
- `--workers N` — parallel processes (or set `WORKERS` env).
- `--name` — label the run. `--restart` — discard prior progress.
- `--dry-run` — print strategy count + ETA without running.
- `--full` — no-op alias kept for backwards compatibility.

### How many indicators per strategy?

By default each strategy holds up to **4** indicators (`--max-combo-size 4`) —
one from **each category** (trend / momentum / volatility / volume). Same-
category stacking (e.g. three EMA crosses) is **excluded by design**: those
indicators are derived from the same price data, so they are redundant
(multicollinearity), give false confidence, and bloat the search → worse
out-of-sample generalization. See `RESEARCH.md`. The size-4 default is a
multi-day run; use `--max-combo-size 3` (~4-5h) or `2` (~10 min) when iterating.

More indicators with `combine=and` means **fewer trades and a higher chance of
curve-fitting the last ~2 years**. The defence is built in: every strategy is
scored on its *out-of-sample* slice (`score = min(IS Sharpe, OOS Sharpe)`), so
strategies that only shine in-sample sink to the bottom. When hunting big
combos, also filter for enough out-of-sample evidence:

```bash
docker compose run --rm app report --min-oos-trades 20 --min-win-rate 0.6
```

The search is checkpointed and resumable: stop it (`Ctrl-C` / container restart)
and re-run the **same** command to continue from the cursor. After **code or
grid changes**, or when switching lookback windows on an in-progress run, add
`--restart` so results are not mixed.

## Other commands

```bash
docker compose run --rm app status                      # search progress
docker compose run --rm app report --min-win-rate 0.7   # top strategies in the terminal
docker compose run --rm app optimize --max-combo-size 2 # quick, smaller run
docker compose run --rm app optimize --adx-filters 25   # add ADX-gated variants

# full default run (720-day default, 3 workers, 2 GB cap, ~3 days; resumable):
docker compose run --rm app ingest
docker compose run --rm app optimize --restart
```

In the dashboard, click any row to expand the **trade detail**: number of
trades, wins/losses, success rate, return, Sharpe/Sortino, max drawdown, profit
factor and exposure — shown separately for in-sample and out-of-sample — plus
the full list of indicators (with parameters and category) and the combine logic.

Run the test suite inside the container:

```bash
docker compose run --rm --entrypoint python app -m pytest tests/ -q
```

## Indicator catalog

The search uses **32 indicator signals** across the four classic categories.
Each emits a per-bar vote in `{-1, 0, +1}`. Implementations are pure
pandas/numpy in `sptrader/indicators/library.py`; the signal wrappers and their
default (sparse) parameter grids are in `sptrader/signals.py`. The dense grids
used by `--full` live in `signals.DENSE_GRIDS`.

**1h / SPY tuning (dense grids).** The default target is 1h candles on a US
equity ETF (~7 RTH bars per day). Dense grids extend beyond daily-chart
defaults where it matters:

- **Trend** — slow MA periods include institutional dailies mapped to hours
(e.g. 350 ≈ 50 days, 1400 ≈ 200 days).
- **Momentum** — short RSI / Stochastic periods (2, 4, 7) for intraday
mean-reversion; `lower` down to 10 for RSI-2-style setups.
- **Volume** — CMF / OBV / A/D smoothing at 50–100 bars to reduce open/close
volume spikes.
- **Volatility** — Bollinger `num_std` up to 3.0; Ichimoku / TSI grids
expanded for hourly bars.
- **Fair Value Gap** (`fvg_dir`, `fvg_meanrev`) — 3-bar ICT imbalance; the
`min_gap_atr` grid filters out gaps thinner than a fraction of ATR.

**Session VWAP.** `vwap_trend` uses a **daily-reset** VWAP (cumulative within
each US trading day in `America/New_York`), not a rolling lifetime average.

**Breakout causality.** `bollinger_breakout`, `keltner_breakout`, and
`donchian_breakout` compare the current close to the **previous bar's**
channel edge (`shift(1)`), so the band is not contaminated by the same bar's
close.

Signal *type* legend: **regime** = holds +1/−1 with the trend; **mean-rev** =
buys oversold, exits at the mid/overbought; **breakout** = goes with a channel
break; **sign** = sign of the indicator vs its zero/centre line.

### Trend (10) — what direction is the market going?


| Signal           | Indicator                   | Type   | What it votes                 | Default params                          |
| ---------------- | --------------------------- | ------ | ----------------------------- | --------------------------------------- |
| `ema_cross`      | Exponential MA cross        | regime | +1 when fast EMA > slow EMA   | fast {10,20,50}, slow {50,100,200}      |
| `sma_cross`      | Simple MA cross             | regime | +1 when fast SMA > slow SMA   | fast {10,20,50}, slow {50,100,200}      |
| `wma_cross`      | Weighted MA cross           | regime | +1 when fast WMA > slow WMA   | fast {10,20,50}, slow {50,100,200}      |
| `macd_cross`     | MACD line vs signal         | regime | +1 when MACD > signal line    | fast {12,8}, slow {26,21}, signal {9,5} |
| `price_vs_sma`   | Price vs SMA                | regime | +1 when close > SMA           | period {50,100,200}                     |
| `adx_di`         | Directional Index (+DI/−DI) | regime | +1 when +DI > −DI             | period {14}                             |
| `supertrend_dir` | Supertrend                  | regime | follows Supertrend direction  | period {10}, multiplier {2,3}           |
| `aroon_dir`      | Aroon up/down               | regime | +1 when Aroon-Up > Aroon-Down | period {14,25}                          |
| `psar_dir`       | Parabolic SAR               | regime | +1 when close > SAR           | step {0.02}, max_step {0.2}             |
| `ichimoku_dir`   | Ichimoku cloud              | regime | +1 above cloud & Tenkan>Kijun | conv 9, base 26, spanB 52               |


### Momentum (9) — is the move fast / over-extended?


| Signal             | Indicator                   | Type     | What it votes                       | Default params                          |
| ------------------ | --------------------------- | -------- | ----------------------------------- | --------------------------------------- |
| `rsi_meanrev`      | RSI                         | mean-rev | long when RSI < lower, exit > upper | period 14, lower {25,30}, upper {60,70} |
| `rsi_trend`        | RSI vs 50                   | sign     | +1 when RSI > level                 | period 14, level 50                     |
| `stoch_meanrev`    | Stochastic %K               | mean-rev | long when %K < lower, exit > upper  | k 14, d 3, smooth 3, 20/80              |
| `cci_sign`         | Commodity Channel Index     | sign     | sign of CCI                         | period 20                               |
| `williams_meanrev` | Williams %R                 | mean-rev | long when %R < lower, exit > upper  | period 14, −80/−20                      |
| `roc_sign`         | Rate of Change              | sign     | sign of ROC                         | period 12                               |
| `stochrsi_meanrev` | Stochastic RSI              | mean-rev | long when low, exit when high       | period 14, k 3, d 3, 20/80              |
| `tsi_sign`         | True Strength Index         | sign     | sign of TSI                         | long 25, short 13                       |
| `momentum_sign`    | Momentum (close − close[n]) | sign     | sign of momentum                    | period 10                               |


### Volatility (6) — how big are the swings / channels?


| Signal               | Indicator            | Type     | What it votes                          | Default params             |
| -------------------- | -------------------- | -------- | -------------------------------------- | -------------------------- |
| `bollinger_meanrev`  | Bollinger Bands      | mean-rev | long below lower band, exit at mid     | period 20, num_std {2,2.5} |
| `bollinger_breakout` | Bollinger Bands      | breakout | +1 above upper, −1 below lower         | period 20, num_std 2       |
| `keltner_breakout`   | Keltner Channels     | breakout | +1 above upper, −1 below lower         | period 20, multiplier 2    |
| `donchian_breakout`  | Donchian Channels    | breakout | +1 on new high break, −1 on low        | period {20,55}             |
| `fvg_dir`            | Fair Value Gap (ICT) | regime   | +1 after bullish gap, −1 after bearish | min_gap_atr {0,0.5}        |
| `fvg_meanrev`        | Fair Value Gap (ICT) | mean-rev | long on pullback into a bullish gap    | min_gap_atr {0,0.5}        |


### Volume (7) — is participation confirming the move?


| Signal             | Indicator                   | Type     | What it votes                       | Default params   |
| ------------------ | --------------------------- | -------- | ----------------------------------- | ---------------- |
| `obv_trend`        | On-Balance Volume           | sign     | +1 when OBV > its SMA               | period 20        |
| `cmf_sign`         | Chaikin Money Flow          | sign     | sign of CMF                         | period 20        |
| `mfi_meanrev`      | Money Flow Index            | mean-rev | long when MFI < lower, exit > upper | period 14, 20/80 |
| `vwap_trend`       | VWAP (session, daily reset) | sign     | +1 when close > session VWAP        | —                |
| `ad_trend`         | Accumulation/Distribution   | sign     | +1 when A/D > its SMA               | period 20        |
| `force_index_sign` | Force Index                 | sign     | sign of Force Index                 | period 13        |
| `eom_sign`         | Ease of Movement            | sign     | sign of EoM                         | period 14        |


> **Why cross-category only?** Combining indicators from *different* categories
> (e.g. trend + momentum + volume) gives genuinely independent perspectives.
> Stacking same-category indicators is redundant (multicollinearity) — it is the
> same signal counted twice, inflates the search, and overfits. So the default
> only combines across categories. See `RESEARCH.md`.

> **OR + short momentum on 1h.** With `combine = or`, a single fast RSI /
> Stochastic flip can force entries while slower trend votes stay flat — high
> trade count and `COST_BPS` drag. When filtering the dashboard, prefer
> strategies with reasonable `num_trades` and solid OOS stats, not just IS win
> rate.

## Phase 2: live / paper trading

The live engine is a **bar-close bot** (not tick/HFT): it polls every N seconds
during US regular hours, pulls the latest 1h bars, computes LONG / SHORT /
FLAT, and optionally sends orders. Data for decisions comes from **Yahoo** in
both `dry-run` and `paper` today (~15 min delay — fine for plumbing tests, not
for real-money execution).

US market open is **09:30 America/New_York** ≈ **16:30 Israel** (summer IDT).
The bot idles outside RTH when `market_hours_only` is on (default).

### Recommended rollout (two steps)


| Step                 | When                         | Mode      | Data                           | Orders                |
| -------------------- | ---------------------------- | --------- | ------------------------------ | --------------------- |
| **1. Yahoo session** | A few hours on a trading day | `dry-run` | Yahoo                          | None — JSON logs only |
| **2. IBKR demo**     | After step 1 looks sane      | `paper`   | Yahoo (signals) + IBKR (fills) | Paper account         |


Helper script (wraps the commands below): `scripts/start-live.sh`

```bash
chmod +x scripts/start-live.sh   # once
```

### Before the session (any mode)

Run ~5 minutes before **16:30 Israel** (or whenever you want to start). This
checks the DB, promotes the current top-3 pool, and prints one live decision:

```bash
./scripts/start-live.sh preflight
```

Default selection (tunable via env `RUN_ID`):

- `--run-id 3` — current optimize run
- `--strategies 3` — top 3 candidates in the pool
- `--combine priority` — **one strategy owns each trade** (sticky until FLAT)
- `--order-by score` — max robustness = `min(IS Sharpe, OOS Sharpe)`
- `--min-oos-trades 25` `--min-trades 50` `--min-win-rate 0.78`

After each **closed trade**, `--reselect-on-flat` (default with `--auto`) refreshes
the top-3 pool; the highest-ranked strategy that fires next takes the trade.

### Step 1 — Yahoo dry-run (a few hours today)

No broker needed. Decisions are logged; no orders placed.

```bash
# Start in the background (waits for US RTH if started early)
./scripts/start-live.sh dry-run

# Watch decisions
docker logs -f sptrader-live

# Stop when done
./scripts/start-live.sh stop
```

Equivalent manual command:

```bash
docker compose run -d --name sptrader-live app \
  live-run --auto --reselect-on-flat --mode dry-run \
  --poll-seconds 600 --combine priority \
  --run-id 3 --strategies 3 --order-by score \
  --min-oos-trades 25 --min-trades 50 --min-win-rate 0.78
```

One-shot smoke test (no loop):

```bash
docker compose run --rm app live-run --auto --mode dry-run --once \
  --run-id 3 --strategies 3 --combine priority \
  --order-by score --min-oos-trades 25 --min-trades 50 --min-win-rate 0.78 \
  --ignore-market-hours
```

### Step 2 — Interactive Brokers paper (demo)

**Prerequisites:** TWS or IB Gateway running on the host in **paper** mode
(port **7497** for TWS paper / **4002** for Gateway paper). Docker reaches the
host via `host.docker.internal` (already set in `docker-compose.yml`).

```bash
# Optional: confirm IBKR env in .env
# IBKR_HOST=host.docker.internal
# IBKR_PORT=7497
# IBKR_CLIENT_ID=17

./scripts/start-live.sh paper
docker logs -f sptrader-live

# Stop
./scripts/start-live.sh stop
```

Equivalent manual command:

```bash
docker compose run -d --name sptrader-live app \
  live-run --auto --reselect-on-flat --mode paper \
  --poll-seconds 600 --combine priority \
  --run-id 3 --strategies 3 --order-by score \
  --min-oos-trades 25 --min-trades 50 --min-win-rate 0.78
```

Signals still use Yahoo bars; only **order routing** goes to IBKR paper.

### How priority arbitration works

With `--strategies 3 --combine priority` (recommended):

1. Three strategies are ranked by `score`.
2. On each poll, the **highest-ranked strategy that is firing** (LONG/SHORT)
  *owns* the trade and keeps control until it goes FLAT (sticky owner).
3. When the position closes, the top-3 pool is **re-selected** from the DB and
  the field reopens — again, first by rank that fires wins.
4. Only **one position at a time** on SPY.

Alternative: `--combine net` sums independent sleeves (not recommended for a
single account).

### Manual strategy pick (optional)

Skip `--auto` and freeze one dashboard row yourself:

```bash
docker compose run --rm app promote --result-id <DB_ID>   # not dashboard row #
docker compose run --rm app live-signal
docker compose run --rm app live-run --mode dry-run --poll-seconds 600
```

### Selection flags (`auto-select` / `live-run --auto`)

- `--order-by` — `score` (default), `oos_sharpe`, `oos_return`, `win_rate`
- `--min-win-rate` `--min-trades` `--min-oos-trades` — sample-size filters
- `--run-id` — pick from a specific optimize run (default: latest)
- `--strategies N` — top N in the pool (default 1; we use 3)
- `--reselect-on-flat` / `--no-reselect-on-flat` — refresh pool after each trade

### Trade logs (post-mortem)

Everything the live runner prints is **also** written to files under `./logs`
(bind-mounted into the container via `docker-compose.yml`), so trades can be
reviewed after the container is gone. Two files roll per UTC day:


| File                         | Format                   | Use                                                                                                                          |
| ---------------------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `logs/live-YYYY-MM-DD.jsonl` | one JSON object per poll | full snapshot: every sleeve's signal/price, chosen owner, target qty, risk reason, broker order + fill                       |
| `logs/trades-YYYY-MM-DD.log` | human-readable           | `OPEN` / `CLOSE` / `FLIP` / `ADJUST` events with entry/exit price, side, `move%`, holding time, owning strategy's indicators |


```bash
tail -f logs/trades-$(date -u +%F).log      # follow trades as they happen
cat  logs/trades-$(date -u +%F).log         # ledger for post-mortem
jq . logs/live-$(date -u +%F).jsonl         # full per-poll detail
```

Each `CLOSE` line reports the round trip, e.g.:

```
2026-06-10 16:30:00Z (NY 12:30)  CLOSE LONG SPY @ 105.0  entry=100.0  move=+5.00%  held=2h00m  owner=auto-123  status=Filled, fill=105.01
```

Override the directory with `LIVE_LOG_DIR` (default `logs`). Files inside the
container are written by root; remove them from the host with
`docker run --rm -v "$PWD/logs:/l" alpine rm -f /l/live-*.jsonl /l/trades-*.log`
if needed.

### Modes & risk


| Mode      | Orders     | Notes                              |
| --------- | ---------- | ---------------------------------- |
| `dry-run` | None       | Safe default for Yahoo sessions    |
| `paper`   | IBKR paper | Needs TWS/Gateway                  |
| `live`    | Real money | Requires `--i-understand-the-risk` |


Env (see `.env.example` / `docker-compose.yml`): `LIVE_ORDER_QTY`,
`LIVE_MAX_POSITION`, `LIVE_ALLOW_SHORT`, `LIVE_KILL_SWITCH`, `IBKR_HOST`,
`IBKR_PORT`, `IBKR_CLIENT_ID`, `IBKR_FILL_TIMEOUT`, `LIVE_LOG_DIR`.

**Before real money:** weeks of paper, walk-forward validation, and replacing
Yahoo with IBKR historical/real-time bars in `live/signal.py`.

## Notes

- Candle size is fixed at 1h (native on Yahoo).
- Behaviorally-identical strategies are de-duplicated at insert (a `dedup_key`
unique constraint), so the DB never stores duplicate strategies.
- Yahoo caps intraday history (~730 days for 1h). Default `LOOKBACK_DAYS=720`
uses nearly all of it — needed for institutional-length hourly MAs (e.g. 1400
bars ≈ 200 trading days).
- Backtests are close-to-close with a one-bar entry delay and per-side cost.
Paper-trade before risking capital. No strategy here is a guaranteed winner.

