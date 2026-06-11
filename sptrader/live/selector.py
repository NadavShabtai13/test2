"""Automatic strategy selection.

Instead of a human running ``promote``, the bot picks the best strategy from the
saved search results using objective criteria (robustness + enough out-of-sample
trades), then freezes it as the active live strategy. Intended to run at the
start of each trading day so the choice tracks the latest search results.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..reporting import top_strategies
from .store import deactivate_all, promote


@dataclass
class SelectionCriteria:
    """How "best" is defined. Defaults favour robustness over raw win rate."""

    min_win_rate: float = 0.0
    min_trades: int = 20
    min_oos_trades: int = 10
    order_by: str = "score"  # min(IS,OOS Sharpe) -- robustness
    run_id: Optional[int] = None  # None -> latest run


def select_top(
    criteria: Optional[SelectionCriteria] = None, n: int = 1
) -> List[Dict[str, Any]]:
    """Return up to ``n`` best strategy rows matching ``criteria`` (deduped).

    May return fewer than ``n`` (or none) when not enough strategies clear the
    bar -- the caller should trade only the survivors, not pad with weak picks.
    """
    c = criteria or SelectionCriteria()
    return top_strategies(
        run_id=c.run_id,
        n=max(1, n),
        order_by=c.order_by,
        min_win_rate=c.min_win_rate,
        min_trades=c.min_trades,
        min_oos_trades=c.min_oos_trades,
        dedupe=True,
    )


def select_best(criteria: Optional[SelectionCriteria] = None) -> Optional[Dict[str, Any]]:
    """Return the single best strategy row matching ``criteria``, or None."""
    rows = select_top(criteria, n=1)
    return rows[0] if rows else None


def _metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "win_rate": row["win_rate"],
        "score": row["score"],
        "oos_sharpe": row["oos_sharpe"],
        "oos_trades": row.get("oos_trades"),
        "trades": row["trades"],
        "strategy": row["strategy"],
    }


def auto_select_and_promote(
    criteria: Optional[SelectionCriteria] = None,
) -> Optional[Dict[str, Any]]:
    """Pick the single best strategy and make it the (exclusive) live strategy."""
    best = select_best(criteria)
    if best is None:
        return None
    info = promote(result_id=best["id"], name=f"auto-{best['id']}", exclusive=True)
    info["selected_metrics"] = _metrics(best)
    return info


def auto_select_ensemble(
    criteria: Optional[SelectionCriteria] = None, n: int = 1
) -> List[Dict[str, Any]]:
    """Pick the top ``n`` strategies and make them the active ensemble.

    Replaces any previously active strategies. Each promoted strategy will run
    its own position sleeve in the live runner. Returns the promote summaries
    (possibly fewer than ``n``, or empty if none qualified).
    """
    rows = select_top(criteria, n=n)
    if not rows:
        return []
    deactivate_all()
    infos: List[Dict[str, Any]] = []
    for row in rows:
        info = promote(result_id=row["id"], name=f"auto-{row['id']}", exclusive=False)
        info["selected_metrics"] = _metrics(row)
        infos.append(info)
    return infos
