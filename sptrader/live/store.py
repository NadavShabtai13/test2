"""Promote a searched strategy to the live slot, and read it back.

Supports either a single active strategy or an *ensemble* of several active
strategies running side by side (each contributes its own position sleeve).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..db import init_db, session_scope
from ..models import LiveStrategy, OptimizationRun, StrategyResult


def promote(
    result_id: int, name: Optional[str] = None, exclusive: bool = True
) -> Dict[str, Any]:
    """Freeze a ``strategy_results`` row as an active live strategy.

    When ``exclusive`` (default) any previously active strategy is disabled, so
    exactly one strategy stays live. Pass ``exclusive=False`` to add this one to
    a running ensemble without disabling the others. Returns a summary dict.
    """
    init_db()
    with session_scope() as session:
        r = session.get(StrategyResult, result_id)
        if r is None:
            raise ValueError(f"strategy_result id={result_id} not found")
        run = session.get(OptimizationRun, r.run_id)
        symbol = run.symbol if run else "SPY"
        interval = run.interval if run else "2h"

        if exclusive:
            session.query(LiveStrategy).filter_by(status="active").update(
                {"status": "disabled"}
            )
        ls = LiveStrategy(
            result_id=result_id,
            name=name or f"result-{result_id}",
            symbol=symbol,
            interval=interval,
            spec_json=r.spec_json,
            status="active",
        )
        session.add(ls)
        session.flush()
        return {
            "live_id": ls.id,
            "result_id": result_id,
            "name": ls.name,
            "symbol": symbol,
            "interval": interval,
            "win_rate": r.win_rate,
            "score": r.score,
            "spec": json.loads(r.spec_json),
        }


def deactivate_all() -> int:
    """Disable every active live strategy. Returns how many were disabled."""
    init_db()
    with session_scope() as session:
        return (
            session.query(LiveStrategy)
            .filter_by(status="active")
            .update({"status": "disabled"})
        )


def _to_dict(ls: LiveStrategy) -> Dict[str, Any]:
    return {
        "live_id": ls.id,
        "result_id": ls.result_id,
        "name": ls.name,
        "symbol": ls.symbol,
        "interval": ls.interval,
        "spec_json": ls.spec_json,
        "spec": json.loads(ls.spec_json),
    }


def get_active() -> Optional[Dict[str, Any]]:
    """The most recently activated live strategy as a dict, or None."""
    init_db()
    with session_scope() as session:
        ls = (
            session.query(LiveStrategy)
            .filter_by(status="active")
            .order_by(LiveStrategy.id.desc())
            .first()
        )
        return _to_dict(ls) if ls is not None else None


def get_active_all() -> List[Dict[str, Any]]:
    """All currently active live strategies (the ensemble), newest first."""
    init_db()
    with session_scope() as session:
        rows = (
            session.query(LiveStrategy)
            .filter_by(status="active")
            .order_by(LiveStrategy.id.desc())
            .all()
        )
        return [_to_dict(ls) for ls in rows]
