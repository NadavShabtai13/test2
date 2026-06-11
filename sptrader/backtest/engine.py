"""Vectorized, cost-aware backtester.

Convention: ``position`` is the *target* exposure decided using information up
to and including bar ``t``. We trade it on bar ``t+1`` (``position.shift(1)``),
which removes look-ahead. P&L is close-to-close. Transaction cost is charged on
turnover (absolute change in position) in basis points per unit traded.
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

TRADING_SECONDS_PER_YEAR = 365.25 * 24 * 3600


def periods_per_year(index: pd.DatetimeIndex) -> float:
    """Estimate return observations per calendar year from the bar spacing.

    Uses the median spacing between bars so overnight/weekend gaps don't distort
    the annualization factor.
    """
    if len(index) < 3:
        return 252.0
    # Resolution-independent: pandas may store datetime64 as ns / us / ms, so
    # divide the timedeltas by a 1-second unit rather than assuming nanoseconds.
    deltas = np.diff(np.asarray(index.values)) / np.timedelta64(1, "s")  # seconds
    median_dt = float(np.median(deltas))
    if median_dt <= 0:
        return 252.0
    return TRADING_SECONDS_PER_YEAR / median_dt


def train_test_split(
    df: pd.DataFrame, train_fraction: float = 0.7
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological split (no shuffling) into in-sample / out-of-sample."""
    n = len(df)
    cut = int(n * train_fraction)
    return df.iloc[:cut], df.iloc[cut:]


def backtest(
    close: pd.Series,
    position: pd.Series,
    cost_bps: float = 5.0,
    ppy: float | None = None,
) -> Dict[str, float]:
    """Run the backtest and return a metrics dict.

    Metrics: total_return, cagr, ann_vol, sharpe, sortino, max_drawdown, calmar,
    win_rate, profit_factor, exposure, num_trades, n_bars.
    """
    close = close.astype(float)
    position = position.reindex(close.index).fillna(0.0).astype(float)

    if ppy is None:
        ppy = periods_per_year(close.index)

    asset_ret = close.pct_change().fillna(0.0)
    held = position.shift(1).fillna(0.0)  # act next bar

    turnover = held.diff().abs().fillna(held.abs())
    cost = turnover * (cost_bps / 1e4)
    strat_ret = held * asset_ret - cost

    n = len(strat_ret)
    empty = {
        "total_return": 0.0, "cagr": 0.0, "ann_vol": 0.0, "sharpe": 0.0,
        "sortino": 0.0, "max_drawdown": 0.0, "calmar": 0.0, "win_rate": 0.0,
        "trade_win_rate": 0.0, "trade_wins": 0, "trade_count": 0,
        "profit_factor": 0.0, "exposure": 0.0, "num_trades": 0, "n_bars": n,
    }
    if n == 0 or strat_ret.abs().sum() == 0:
        return empty

    equity = (1.0 + strat_ret).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)

    years = n / ppy if ppy else np.nan
    final_eq = float(equity.iloc[-1])
    # Compute via logs to avoid float overflow when ``years`` is tiny (short
    # windows make 1/years large, and eq**large can overflow). On degenerate
    # overfit equity the result can still overflow -> keep it finite (and out of
    # the stored JSON) rather than emitting inf/NaN.
    cagr = 0.0
    if years and years > 0 and final_eq > 0:
        with np.errstate(over="ignore"):
            cagr_val = np.expm1(np.log(final_eq) / years)
        cagr = float(cagr_val) if np.isfinite(cagr_val) else 0.0

    mean = strat_ret.mean()
    std = strat_ret.std(ddof=0)
    ann_vol = float(std * np.sqrt(ppy))
    sharpe = float(mean / std * np.sqrt(ppy)) if std > 0 else 0.0

    downside = strat_ret[strat_ret < 0].std(ddof=0)
    sortino = float(mean / downside * np.sqrt(ppy)) if downside and downside > 0 else 0.0

    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = float(drawdown.min())
    calmar = float(cagr / abs(max_dd)) if max_dd < 0 else 0.0

    # Per-trade stats: a trade spans entry to the next change in position.
    trade_change = held.diff().fillna(0.0) != 0.0
    num_trades = int(trade_change.sum())

    active = strat_ret[held != 0.0]
    wins = active[active > 0].sum()
    losses = active[active < 0].sum()
    # Bar-level hit rate (kept for backward compatibility).
    win_rate = float((active > 0).mean()) if len(active) else 0.0
    profit_factor = float(wins / abs(losses)) if losses < 0 else 0.0
    exposure = float((held != 0.0).mean())

    # Trade-level success rate: group consecutive bars sharing the same held
    # exposure into segments, keep only the in-market ones, and compound each
    # segment's bar returns into a single per-trade P&L. "Success rate" = the
    # fraction of those trades that closed in profit.
    trade_win_rate, trade_wins, trade_count = _trade_win_rate(strat_ret, held)

    return {
        "total_return": total_return,
        "cagr": cagr,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "win_rate": win_rate,
        "trade_win_rate": trade_win_rate,
        "trade_wins": trade_wins,
        "trade_count": trade_count,
        "profit_factor": profit_factor,
        "exposure": exposure,
        "num_trades": num_trades,
        "n_bars": n,
    }


def _trade_win_rate(strat_ret: pd.Series, held: pd.Series) -> Tuple[float, int, int]:
    """Compute (success_rate, winning_trades, total_trades) at the trade level.

    A trade is a maximal run of bars holding the same non-zero exposure. The
    trade's return is the compounded product of its per-bar strategy returns.
    Vectorized via a segment id + log-return groupby sum (fast for the search).
    """
    in_market = held != 0.0
    if not bool(in_market.any()):
        return 0.0, 0, 0
    # New segment whenever the held exposure changes value.
    seg_id = (held != held.shift(1)).cumsum()
    log_ret = np.log1p(strat_ret.clip(lower=-0.999999))
    frame = pd.DataFrame({"lr": log_ret, "seg": seg_id})[in_market.to_numpy()]
    if frame.empty:
        return 0.0, 0, 0
    trade_log = frame.groupby("seg")["lr"].sum()
    trade_ret = np.expm1(trade_log)
    count = int(trade_ret.size)
    wins = int((trade_ret > 0).sum())
    rate = float(wins / count) if count else 0.0
    return rate, wins, count
