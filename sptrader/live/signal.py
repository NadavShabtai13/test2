"""Compute the *current* desired exposure (LONG / SHORT / FLAT) for a spec.

Reuses the exact backtest signal code on the most recent bars, then reads the
last bar's combined position. This is the live decision for "right now".

Data source here is Yahoo (same as the backtest) for a self-contained paper
setup. For real intraday execution you would swap in the broker's live bars
(Yahoo intraday is delayed ~15 min) -- see ``broker.py``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..config import get_settings
from ..data.ingest import fetch_ohlcv
from ..optimize.permutations import StrategySpec, compute_adx_series, evaluate_spec


def compute_live_decision(
    spec: StrategySpec,
    symbol: str,
    interval: Optional[str] = None,
    lookback_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Return the latest target exposure and context for ``spec`` on ``symbol``.

    ``target`` is in {-1.0, 0.0, +1.0}: short / flat / long.
    """
    s = get_settings()
    interval = interval or s.interval
    # Enough history for slow indicators (e.g. 200-period MA) regardless of the
    # backtest's default lookback.
    lookback_days = lookback_days or max(s.lookback_days, 60)

    df = fetch_ohlcv(symbol, lookback_days, interval)
    if df.empty:
        raise RuntimeError(f"no live bars for {symbol} @ {interval}")

    adx = compute_adx_series(df) if spec.adx_min is not None else None
    position = evaluate_spec(df, spec, {}, adx)

    target = float(position.iloc[-1])
    prev = float(position.iloc[-2]) if len(position) > 1 else 0.0
    label = {1.0: "LONG", -1.0: "SHORT", 0.0: "FLAT"}.get(target, str(target))
    return {
        "target": target,
        "label": label,
        "changed": target != prev,
        "as_of": df.index[-1].isoformat(),
        "price": float(df["close"].iloc[-1]),
        "bars": int(len(df)),
        "symbol": symbol,
        "interval": interval,
    }
