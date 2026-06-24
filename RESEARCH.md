# Research notes: indicators, strategies, and honest validation

This document summarizes what trading-education sites and practitioner forums
actually agree on, and how those lessons are encoded in this project. Sources
were gathered from trading academies, swing-trading guides, walk-forward /
overfitting literature (López de Prado-style), and r/algotrading retrospectives.

> **Reality check.** No indicator or permutation search produces a guaranteed
> "winning" strategy. Markets are adaptive and largely efficient at this
> timeframe. The honest goal is to find rules that are *robust* (work in- and
> out-of-sample, survive costs) and to size risk accordingly. Treat every
> backtest number as optimistic.

## 1. Indicators come in four categories

| Category   | Measures              | Examples implemented                                   |
|------------|-----------------------|--------------------------------------------------------|
| Trend      | Direction & slope     | SMA/EMA/WMA cross, MACD, ADX/DI, Aroon, PSAR, Ichimoku, Supertrend |
| Momentum   | Speed of the move     | RSI, Stochastic, StochRSI, CCI, Williams %R, ROC, Momentum, TSI |
| Volatility | Magnitude of the move | Bollinger Bands, ATR, Keltner, Donchian                |
| Volume     | Participation         | OBV, VWAP, CMF, MFI, A/D line, Force Index, EoM        |

## 2. The strongest consensus: combine *across* categories, not within

Multiple independent sources (TradeOlogy Academy, TradingZenith, TradeAlgo,
StratBase, arxum) converge on the same rule:

- A good setup answers three questions: **what is the trend? is momentum timing
  favorable? is volatility/volume supportive?** — one indicator per question.
- **Stacking same-category indicators (RSI + Stochastic + CCI) is redundant** and
  produces "decision paralysis" without adding information.
- A **trend-strength filter (ADX > ~25)** before taking trend/MA-cross or MACD
  signals materially cuts losing trades in range-bound markets (cited win-rate
  improvements of ~22–28%).

**Encoded here:** the permutation engine combines up to 4 indicators per
strategy, one from each category (trend + momentum + volatility + volume). Only
cross-category combinations are formed; same-category stacks are excluded as
redundant (multicollinearity). ADX gating is an optional filter (`--adx-filters
25`).

## 3. What practitioners say actually matters (r/algotrading)

- **If a strategy only works after heavy parameter tuning, it probably doesn't
  work.** Walk-forward / out-of-sample validation is "the only honest signal."
- **Transaction costs are not optional.** A "20% annual" backtest can become 6%
  or negative once spread + slippage + commission are included. Costs commonly
  eat 0.2–1.5% per round trip; even 0.05%/side can kill high-turnover ideas.
- Trend-following vs mean-reversion both work *in the right regime*: trend-
  following needs trends; mean-reversion gets run over in strong trends.

**Encoded here:** every backtest charges a configurable per-side cost on
turnover (`COST_BPS`, default 5 bps). Both trend-following (crossovers,
breakouts) and mean-reversion (RSI/Bollinger/Stochastic) signal families are
included so the search can discover which regime the data favors.

## 4. Overfitting defenses (the part that makes results trustworthy)

From the walk-forward / DSR / PBO literature:

1. **Never score a strategy on the data used to pick it.** Split chronologically
   into in-sample (IS) and out-of-sample (OOS). (No random shuffling — markets
   evolve and shuffling leaks the future.)
2. **Compare IS vs OOS side by side; you want *stability*, not OOS beating IS.**
   Healthy: IS profit factor 2.0 → OOS 1.6. Red flag: OOS collapses below ~1.0.
3. **Multiple testing inflates the best result.** Trying thousands of variants
   guarantees some look great by luck. Deflated Sharpe Ratio (DSR) and
   Probability of Backtest Overfitting (PBO) quantify this.
4. **Prefer parameter *plateaus* over sharp peaks** — robustness beats a single
   lucky setting.

**Encoded here:** the selection score is `min(IS Sharpe, OOS Sharpe)`. This
deliberately punishes the classic overfit signature (great IS, poor OOS): a
strategy only scores well if it works in *both* halves. The report shows IS and
OOS Sharpe side by side so you can eyeball stability.

### Known limitations / honest caveats

- `min(IS, OOS)` is a pragmatic robustness proxy, **not** a full walk-forward +
  DSR/PBO pipeline. A rolling walk-forward and a deflated-Sharpe adjustment are
  the natural next upgrades (see README → Roadmap).
- A **short lookback window is small** in bar count. That is fine for a
  plumbing demo but statistically weak for strategy selection. Use
  `LOOKBACK_DAYS=720` (Yahoo's ~730-day intraday cap) for anything you intend to
  take seriously.
- Backtest fills are close-to-close with a one-bar delay. Real execution adds
  slippage, partial fills, and latency. Paper-trade before risking capital.

## 5. Sensible starting kit (per the sources)

If you want a hand-picked baseline rather than the full search:
**50/200 EMA trend + RSI(14) for timing + ATR(14) for stop sizing**, with an
**ADX(14) > 25** gate. The permutation engine includes all of these so it can
confirm (or refute) that baseline on the actual S&P 500 data.
