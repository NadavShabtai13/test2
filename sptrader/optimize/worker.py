"""Worker-process helpers for the parallel optimization run.

Each worker process loads the candle frame once (via ``init_worker``) and keeps
its own indicator-vote cache, so the CPU-bound backtests run in parallel without
shipping data per task. Tasks are plain dicts (picklable); results are plain
dicts the main process turns into ORM rows.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

import pandas as pd

from ..backtest.engine import backtest, train_test_split
from .permutations import StrategySpec, evaluate_spec

# Per-worker globals, populated by init_worker.
_DF: Optional[pd.DataFrame] = None
_CLOSE: Optional[pd.Series] = None
_ADX: Optional[pd.Series] = None
_PPY: Optional[float] = None
_COST: float = 5.0
_TRAIN_FRACTION: float = 0.7
_CACHE: Dict[str, pd.Series] = {}


def init_worker(df, adx_series, ppy, cost_bps, train_fraction) -> None:
    global _DF, _CLOSE, _ADX, _PPY, _COST, _TRAIN_FRACTION, _CACHE
    _DF = df
    _CLOSE = df["close"]
    _ADX = adx_series
    _PPY = ppy
    _COST = cost_bps
    _TRAIN_FRACTION = train_fraction
    _CACHE = {}


def robust_score(is_m: Dict[str, float], oos_m: Dict[str, float]) -> float:
    """Selection score: min(IS Sharpe, OOS Sharpe) -- punishes overfit."""
    return float(min(is_m.get("sharpe", 0.0), oos_m.get("sharpe", 0.0)))


def combined_win_rate(is_m: Dict[str, float], oos_m: Dict[str, float]) -> float:
    """Trades-weighted success rate across both halves."""
    wins = int(is_m.get("trade_wins", 0)) + int(oos_m.get("trade_wins", 0))
    total = int(is_m.get("trade_count", 0)) + int(oos_m.get("trade_count", 0))
    return float(wins / total) if total else 0.0


def _dedup_key(is_m: Dict[str, float], oos_m: Dict[str, float], num_trades: int, win_rate: float) -> str:
    """Stable hash of rounded headline metrics. Identical behavior -> same key,
    which the DB unique constraint uses to drop duplicate strategies."""
    key = (
        round(float(is_m.get("sharpe", 0.0)), 4),
        round(float(oos_m.get("sharpe", 0.0)), 4),
        round(float(oos_m.get("total_return", 0.0)), 6),
        round(float(oos_m.get("max_drawdown", 0.0)), 6),
        int(num_trades),
        round(float(win_rate), 6),
    )
    return hashlib.sha1(repr(key).encode()).hexdigest()


def _evaluate_one(spec: StrategySpec) -> Dict[str, Any]:
    position = evaluate_spec(_DF, spec, _CACHE, _ADX)
    tr_pos, te_pos = train_test_split(position, _TRAIN_FRACTION)
    tr_close, te_close = train_test_split(_CLOSE, _TRAIN_FRACTION)
    is_m = backtest(tr_close, tr_pos, _COST, _PPY)
    oos_m = backtest(te_close, te_pos, _COST, _PPY)
    num_trades = int(is_m["num_trades"] + oos_m["num_trades"])
    win_rate = combined_win_rate(is_m, oos_m)
    return {
        "signature": spec.signature(),
        "dedup_key": _dedup_key(is_m, oos_m, num_trades, win_rate),
        "spec_json": json.dumps(spec.to_dict(), default=str),
        "score": robust_score(is_m, oos_m),
        "is_sharpe": is_m["sharpe"],
        "oos_sharpe": oos_m["sharpe"],
        "oos_return": oos_m["total_return"],
        "oos_max_drawdown": oos_m["max_drawdown"],
        "num_trades": num_trades,
        "win_rate": win_rate,
        "is_win_rate": is_m.get("trade_win_rate", 0.0),
        "oos_win_rate": oos_m.get("trade_win_rate", 0.0),
        "metrics_json": json.dumps({"is": is_m, "oos": oos_m}),
    }


def eval_batch(spec_dicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Backtest a chunk of specs (given as dicts); return result-row dicts."""
    out: List[Dict[str, Any]] = []
    for d in spec_dicts:
        out.append(_evaluate_one(StrategySpec.from_dict(d)))
    return out
