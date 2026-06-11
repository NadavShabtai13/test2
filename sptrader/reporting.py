"""Read-side helpers: rank strategies and summarize run progress."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import desc

from .db import session_scope
from .models import OptimizationRun, StrategyResult


def _category_of(factory: str) -> str:
    """True indicator category from the signal registry (not a name guess)."""
    from .signals import REGISTRY

    f = REGISTRY.get(factory)
    return f.category if f else "unknown"


def latest_run_id() -> Optional[int]:
    with session_scope() as session:
        run = session.query(OptimizationRun).order_by(desc(OptimizationRun.id)).first()
        return run.id if run else None


def list_runs() -> List[Dict[str, Any]]:
    """All optimization runs, newest first (for the UI run selector)."""
    with session_scope() as session:
        runs = session.query(OptimizationRun).order_by(desc(OptimizationRun.id)).all()
        return [
            {
                "run_id": run.id,
                "name": run.name,
                "symbol": run.symbol,
                "interval": run.interval,
                "status": run.status,
                "total": run.total_combos,
                "completed": run.completed_combos,
            }
            for run in runs
        ]


def run_status(run_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    with session_scope() as session:
        run = (
            session.get(OptimizationRun, run_id)
            if run_id is not None
            else session.query(OptimizationRun).order_by(desc(OptimizationRun.id)).first()
        )
        if run is None:
            return None
        done = session.query(StrategyResult).filter_by(run_id=run.id).count()
        return {
            "run_id": run.id,
            "name": run.name,
            "symbol": run.symbol,
            "interval": run.interval,
            "status": run.status,
            "total": run.total_combos,
            "completed": done,
            "pct": (100.0 * done / run.total_combos) if run.total_combos else 0.0,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        }


def top_strategies(
    run_id: Optional[int] = None,
    n: int = 10,
    order_by: str = "score",
    min_win_rate: float = 0.0,
    min_trades: int = 0,
    min_oos_trades: int = 0,
    dedupe: bool = True,
) -> List[Dict[str, Any]]:
    sort_col = {
        "score": StrategyResult.score,
        "oos_sharpe": StrategyResult.oos_sharpe,
        "oos_return": StrategyResult.oos_return,
        "win_rate": StrategyResult.win_rate,
    }.get(order_by, StrategyResult.score)

    with session_scope() as session:
        if run_id is None:
            run = session.query(OptimizationRun).order_by(desc(OptimizationRun.id)).first()
            if run is None:
                return []
            run_id = run.id

        query = session.query(StrategyResult).filter_by(run_id=run_id)
        if min_win_rate > 0:
            query = query.filter(StrategyResult.win_rate >= min_win_rate)
        if min_trades > 0:
            query = query.filter(StrategyResult.num_trades >= min_trades)

        # OOS-trade count and clone-dedupe need the per-row metrics, which live in
        # metrics_json (not an indexed column). So when either is requested we
        # pull a larger candidate pool ordered by the sort key, then post-filter
        # in Python and stop once we have ``n`` survivors.
        need_post = min_oos_trades > 0 or dedupe
        fetch_n = max(n * 50, 3000) if need_post else n
        rows = query.order_by(desc(sort_col)).limit(fetch_n).all()

        out: List[Dict[str, Any]] = []
        seen_metrics = set()
        for r in rows:
            detail = _trade_detail(r.metrics_json)
            oos_trades = int(detail["oos"]["trades"])
            is_trades = int(detail["is"]["trades"])
            if min_oos_trades > 0 and oos_trades < min_oos_trades:
                continue
            if dedupe:
                mkey = (
                    round(float(r.is_sharpe), 4),
                    round(float(r.oos_sharpe), 4),
                    round(float(r.oos_return), 6),
                    round(float(r.oos_max_drawdown), 6),
                    int(r.num_trades),
                    round(float(r.win_rate), 6),
                )
                if mkey in seen_metrics:
                    continue
                seen_metrics.add(mkey)

            spec = json.loads(r.spec_json)
            indicators = [
                {
                    "factory": i["factory"],
                    "params": i["params"],
                    "category": _category_of(i["factory"]),
                    "label": f"{i['factory']}({','.join(f'{k}={v}' for k, v in i['params'].items())})",
                }
                for i in spec["instances"]
            ]
            instances = ", ".join(ind["label"] for ind in indicators)
            out.append(
                {
                    "id": r.id,
                    "score": r.score,
                    "is_sharpe": r.is_sharpe,
                    "oos_sharpe": r.oos_sharpe,
                    "oos_return": r.oos_return,
                    "oos_mdd": r.oos_max_drawdown,
                    "win_rate": r.win_rate,
                    "is_win_rate": r.is_win_rate,
                    "oos_win_rate": r.oos_win_rate,
                    "trades": r.num_trades,
                    "is_trades": is_trades,
                    "oos_trades": oos_trades,
                    "mode": spec["mode"],
                    "adx_min": spec["adx_min"],
                    "combine": spec.get("combine", "and"),
                    "strategy": instances,
                    "indicators": indicators,
                    "detail": detail,
                }
            )
            if len(out) >= n:
                break
        return out


def _trade_detail(metrics_json: Optional[str]) -> Dict[str, Any]:
    """Break the stored IS/OOS metric dicts into the per-strategy trade view
    the dashboard expands (trades, wins, profit factor, exposure, returns)."""
    try:
        m = json.loads(metrics_json) if metrics_json else {}
    except (TypeError, ValueError):
        m = {}

    def _half(d: Dict[str, Any]) -> Dict[str, Any]:
        d = d or {}
        trades = int(d.get("trade_count", 0))
        wins = int(d.get("trade_wins", 0))
        return {
            "trades": trades,
            "wins": wins,
            "losses": max(trades - wins, 0),
            "win_rate": d.get("trade_win_rate", 0.0),
            "total_return": d.get("total_return", 0.0),
            "sharpe": d.get("sharpe", 0.0),
            "sortino": d.get("sortino", 0.0),
            "max_drawdown": d.get("max_drawdown", 0.0),
            "profit_factor": d.get("profit_factor", 0.0),
            "exposure": d.get("exposure", 0.0),
            "n_bars": int(d.get("n_bars", 0)),
        }

    return {"is": _half(m.get("is", {})), "oos": _half(m.get("oos", {}))}


def format_top_table(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(no results yet)"
    try:
        from tabulate import tabulate
    except Exception:  # pragma: no cover
        return "\n".join(str(r) for r in rows)

    table = [
        [
            f"{100*r.get('win_rate', 0.0):.0f}%",
            f"{r['score']:.3f}",
            f"{r['is_sharpe']:.2f}",
            f"{r['oos_sharpe']:.2f}",
            f"{100*r['oos_return']:.1f}%",
            f"{100*r['oos_mdd']:.1f}%",
            r["trades"],
            r["mode"],
            r["adx_min"] if r["adx_min"] is not None else "-",
            r["strategy"],
        ]
        for r in rows
    ]
    headers = [
        "win%",
        "score(min IS/OOS Sharpe)",
        "IS Sh",
        "OOS Sh",
        "OOS ret",
        "OOS maxDD",
        "trades",
        "mode",
        "adx",
        "strategy",
    ]
    return tabulate(table, headers=headers, tablefmt="github")
