# sptrader

Indicator-permutation backtesting & strategy-search toolkit for the S&P 500
(and any other Yahoo Finance symbol). It pulls OHLCV data as **1h candles** by
default (2h optional, via resampling),
permutes technical-indicator strategies across categories, backtests each with
transaction costs and an in-sample / out-of-sample split, and surfaces the
**winning strategies in a web dashboard** filtered by a success-rate threshold
(70% by default).

Everything runs in **Docker** — no Python or packages on your host. Copy the
repo to any server with Docker and the same commands work.

## Architecture


| Stage      | What it does                                                                   | Resumable?                            |
| ---------- | ------------------------------------------------------------------------------ | ------------------------------------- |
| `ingest`   | Pull Yahoo OHLCV (default 60 days, 1h candles; 2h optional), store in Postgres | yes (idempotent upsert + checkpoints) |
| `optimize` | Permute cross-category indicator strategies, backtest IS/OOS, store results    | yes (skips already-tested signatures) |
| `web`      | Dashboard: strategies above a success-rate threshold, with their indicators    | live (reads the DB)                   |


Postgres persists candles, run progress, and every tested permutation, so an
interrupted build or run continues exactly where it stopped (`Ctrl-C` safe).

## How the flow works (end to end)

```
 env / .env ─▶ ingest ─▶ Postgres(candles) ─▶ optimize ─▶ Postgres(results) ─▶ web / report
                                                  │
                                       2 worker processes (parallel)
```

**0. Config.** Everything is driven by env vars (`.env` or `-e` on the command).
Key knobs: `SYMBOL`, `LOOKBACK_DAYS`, `BASE_INTERVAL`/`TARGET_INTERVAL`,
`COST_BPS`, `TRAIN_FRACTION`, `WORKERS`. See `sptrader/config.py`.

**1. Ingest** (`sptrader/data/ingest.py`)

1. Download OHLCV from Yahoo for `SYMBOL` over `LOOKBACK_DAYS` at `BASE_INTERVAL`
  (`1h`).
2. If `TARGET_INTERVAL` ≠ `BASE_INTERVAL` (e.g. `2h`), resample: open=first,
  high=max, low=min, close=last, volume=sum; empty buckets dropped. By default
   `TARGET_INTERVAL=1h`, so no resampling — the 1h candles are used directly.
3. Upsert both intervals into `candles` (idempotent: `ON CONFLICT` on
  `symbol,interval,ts`), and record an `ingestion_checkpoint` (last bar stored).
   Re-running only adds new bars.

**2. Optimize** (`sptrader/optimize/`)

1. **Generate** strategy specs *lazily* (`permutations.iter_strategies`) in a
  deterministic order. A spec = a set of indicator instances + a direction mode
   (`long_only`/`long_short`) + combine logic (`and`/`or`) + optional ADX gate.
2. **Distribute**: specs are streamed in batches to `WORKERS` worker processes
  (default 2). Each worker holds the candle frame once and caches each
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

# 3. pull market data into Postgres
docker compose run --rm app ingest

# 4. run (or resume) the strategy search
docker compose run --rm app optimize

# 5. serve the dashboard, then open http://localhost:8000
docker compose up -d app
```

Re-running `optimize` after a stop **resumes**; it never re-tests a permutation
already in the database.

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

Other useful env vars (see `.env.example`): `LOOKBACK_DAYS` (use `720` for the
full Yahoo intraday history — recommended for serious results), `COST_BPS`
(per-side transaction cost), `TRAIN_FRACTION` (IS/OOS split).

## Search size & exhaustive mode

The search runs in parallel worker processes (default **2**, via `WORKERS`) and
the container is capped at **2 GB RAM** (`mem_limit` in compose). Specs are
streamed lazily, so even the exhaustive space stays within the cap.

Preview the size and ETA before committing with `--dry-run`:

```bash
docker compose run --rm app optimize --full --dry-run
```


| Flags                                                | Strategies | ~ETA (2 workers) |
| ---------------------------------------------------- | ---------- | ---------------- |
| *(default)*                                          | ~16k       | < 1 min          |
| `--dense` (rich param grids, cross-category)         | ~1.25M     | ~50 min          |
| `--dense --all-combos` (incl. same-category)         | ~6.3M      | ~4.4 h           |
| `--full` (all indicators, dense, all combos, AND+OR) | ~12.7M     | ~8.8 h           |
| `--full --max-combo-size 2`                          | ~143k      | ~6 min           |


Exhaustive flags:

- `--full` — everything: all indicators, dense grids, all combination sizes
(incl. same-category), and both AND/OR vote logic.
- `--dense` — rich parameter grids per indicator.
- `--all-combos` — allow every combination, not just cross-category.
- `--combines and or` — vote logic to try.
- `--workers N` — parallel processes (or set `WORKERS` env).

`--full` is checkpointed and resumable: stop it (`Ctrl-C` / container restart)
and re-run the same command to continue from the cursor.

## Other commands

```bash
docker compose run --rm app status                     # search progress
docker compose run --rm app report --min-win-rate 0.7  # top strategies in the terminal
docker compose run --rm app optimize --max-combo-size 2 --modes long_only
docker compose run --rm app optimize --adx-filters 25  # ADX-gated variants

# exhaustive overnight run on 2 years of data, 2 workers, 2 GB cap:
docker compose run --rm -e LOOKBACK_DAYS=720 app ingest
docker compose run --rm -e LOOKBACK_DAYS=720 app optimize --full
```

In the dashboard, click any row to expand the **trade detail**: number of
trades, wins/losses, success rate, return, Sharpe/Sortino, max drawdown, profit
factor and exposure — shown separately for in-sample and out-of-sample — plus
the full list of indicators (with parameters and category) and the combine logic.

Run the test suite inside the container:

```bash
docker compose run --rm --entrypoint pytest app -q
```

## Indicator catalog

The search uses **30 indicator signals** across the four classic categories.
Each emits a per-bar vote in `{-1, 0, +1}`. Implementations are pure
pandas/numpy in `sptrader/indicators/library.py`; the signal wrappers and their
default (sparse) parameter grids are in `sptrader/signals.py`. The dense grids
used by `--full` / `--dense` live in `signals.DENSE_GRIDS`.

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


### Volatility (4) — how big are the swings / channels?


| Signal               | Indicator         | Type     | What it votes                      | Default params             |
| -------------------- | ----------------- | -------- | ---------------------------------- | -------------------------- |
| `bollinger_meanrev`  | Bollinger Bands   | mean-rev | long below lower band, exit at mid | period 20, num_std {2,2.5} |
| `bollinger_breakout` | Bollinger Bands   | breakout | +1 above upper, −1 below lower     | period 20, num_std 2       |
| `keltner_breakout`   | Keltner Channels  | breakout | +1 above upper, −1 below lower     | period 20, multiplier 2    |
| `donchian_breakout`  | Donchian Channels | breakout | +1 on new high break, −1 on low    | period {20,55}             |


### Volume (7) — is participation confirming the move?


| Signal             | Indicator                 | Type     | What it votes                       | Default params   |
| ------------------ | ------------------------- | -------- | ----------------------------------- | ---------------- |
| `obv_trend`        | On-Balance Volume         | sign     | +1 when OBV > its SMA               | period 20        |
| `cmf_sign`         | Chaikin Money Flow        | sign     | sign of CMF                         | period 20        |
| `mfi_meanrev`      | Money Flow Index          | mean-rev | long when MFI < lower, exit > upper | period 14, 20/80 |
| `vwap_trend`       | VWAP                      | sign     | +1 when close > VWAP                | —                |
| `ad_trend`         | Accumulation/Distribution | sign     | +1 when A/D > its SMA               | period 20        |
| `force_index_sign` | Force Index               | sign     | sign of Force Index                 | period 13        |
| `eom_sign`         | Ease of Movement          | sign     | sign of EoM                         | period 14        |


> **Why cross-category?** The default (non-`--full`) search only combines
> indicators from *different* categories (e.g. trend + momentum + volume),
> because stacking same-category indicators is redundant. `--full` drops that
> restriction and tries every combination. See `RESEARCH.md` for the reasoning.

## Phase 2: live / paper trading (Interactive Brokers)

The same engine that backtests can compute the **current** LONG / SHORT / FLAT
decision from the latest bars and send it to a broker. This is scaffolded and
**paper/dry-run-first** — real money is gated behind an explicit flag.

There are two ways to choose which strategy trades:

- **Manual** — you inspect the dashboard and freeze one row with `promote`.
- **Automatic** — the bot picks the best strategy from the DB itself, by
objective criteria (robustness + enough out-of-sample trades). No human pick.

Flow (manual):

```bash
# 1. Pick a strategy from the search and freeze it as the live strategy
docker compose run --rm app promote --result-id <ID>

# 2. See the current decision (no broker, Yahoo bars)
docker compose run --rm app live-signal

# 3a. Dry-run: decide + log, place NO orders (no IBKR needed)
docker compose run --rm app live-run --mode dry-run --once --ignore-market-hours

# 3b. Paper: route to an IBKR *paper* account (needs TWS/IB Gateway running)
docker compose run --rm app live-run --mode paper --poll-seconds 900
```

Flow (automatic — bot selects, no manual promote):

```bash
# See which strategy the bot WOULD pick, and freeze it now
docker compose run --rm app auto-select --order-by score --min-oos-trades 10 --min-win-rate 0.55

# Run the trader so it re-picks the best strategy at the START OF EACH trading day
docker compose run --rm app live-run --auto --mode dry-run \
  --order-by score --min-oos-trades 10 --min-trades 20 --min-win-rate 0.55
```

`--auto` makes `live-run` re-run the selection once per trading day, then follow
that strategy's indicator signals for the day. If no strategy clears the
criteria, the bot stays **flat** rather than trading a weak pick. Selection
flags (shared by `auto-select` and `live-run --auto`):

- `--order-by` — `score` (robustness = min(IS,OOS Sharpe)), `oos_sharpe`,
`oos_return`, or `win_rate`.
- `--min-win-rate` — e.g. `0.55` requires ≥55% winning trades.
- `--min-trades` / `--min-oos-trades` — reject tiny, fluke samples.
- `--run-id` — pick from a specific run (default: the latest).
- `--strategies N` — run the **top N strategies as an ensemble** (default 1).

### Several strategies at once (`--strategies N`, `--combine`)

With `--strategies N > 1` the bot considers the top N strategies together. On a
single account a symbol has only ONE net position, so there are two honest ways
to combine them (`--combine`):

- `**priority` (default, recommended)** — **one position at a time**. Every poll
each strategy computes its own LONG/SHORT/FLAT. The highest-ranked strategy
(by `score`) that is currently firing *owns* the trade and holds it until it
goes FLAT (sticky); only then does the field reopen and the next best-ranked
firing strategy take over. This is "one strategy per trade": if strategy X's
indicators hold it trades X; once that trade closes, all N are re-checked and
whichever fires (by rank) takes the next trade. No double-counting, no
cancellation.
- `**net`** — each strategy is an independent *sleeve* contributing
`LIVE_ORDER_QTY` shares; the position is their **sum**, clamped to
`LIVE_MAX_POSITION`. Agreeing strategies stack; disagreeing ones cancel. More
capital-efficient but less intuitive on one account.

```bash
# Top 3 strategies, one-position priority arbitration, re-picked each day
docker compose run --rm app live-run --auto --strategies 3 --combine priority \
  --mode dry-run --order-by score --min-oos-trades 10 --min-trades 20 --min-win-rate 0.55
```

Note on ranking: "first to fire" is decided by **rank (score), not wall-clock
time** — all strategies are evaluated on the same bar, so a deterministic
rank-based tie-break avoids thrashing. In `net` mode, N sleeves can push up to
`N * LIVE_ORDER_QTY`, so raise `LIVE_MAX_POSITION` if you want them to express
fully (e.g. 3 × 10 → `LIVE_MAX_POSITION=30`).

Modes: `dry-run` (default, no orders), `paper` (IBKR paper port 7497/4002),
`live` (real money — refused unless you add `--i-understand-the-risk`).

Risk controls (env / `.env`): `LIVE_ORDER_QTY`, `LIVE_MAX_POSITION`,
`LIVE_ALLOW_SHORT`, `LIVE_KILL_SWITCH`. IBKR connection: `IBKR_HOST`,
`IBKR_PORT`, `IBKR_CLIENT_ID`.

**Before real money** (mandatory): validate on long history + walk-forward,
then paper-trade for weeks. Strategies found on 2 months of data are research
artifacts, not trade plans. `live-signal` uses Yahoo (delayed ~15 min); swap in
the broker's live bars for real intraday execution.

## Notes

- Default candle size is 1h (native on Yahoo). For 2h set `TARGET_INTERVAL=2h`
(resampled from the 1h base, since Yahoo has no native 2h).
- Behaviorally-identical strategies are de-duplicated at insert (a `dedup_key`
unique constraint), so the DB never stores duplicate strategies.
- Yahoo caps intraday history (~730 days for 1h). The default 60-day window is a
small sample — fine for a demo, weak for selection. Use `LOOKBACK_DAYS=720`.
- Backtests are close-to-close with a one-bar entry delay and per-side cost.
Paper-trade before risking capital. No strategy here is a guaranteed winner.
