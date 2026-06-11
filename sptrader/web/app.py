"""Flask dashboard: winning strategies above a success-rate threshold.

Routes
------
``GET /``                 the dashboard page
``GET /api/runs``         JSON list of optimization runs (for the selector)
``GET /api/strategies``   JSON of strategies for a run, filtered/sorted server-side
                          query params: run_id, min_win_rate, min_trades,
                          order_by, top
"""
from __future__ import annotations

from typing import Optional

from flask import Flask, jsonify, render_template, request

from ..reporting import list_runs, run_status, top_strategies


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/runs")
    def api_runs():
        return jsonify({"runs": list_runs()})

    @app.get("/api/strategies")
    def api_strategies():
        run_id = _maybe_int(request.args.get("run_id"))
        # Default threshold = 0.70 (the "70% success rate" requirement).
        min_win_rate = _maybe_float(request.args.get("min_win_rate"), 0.70)
        min_trades = int(_maybe_float(request.args.get("min_trades"), 0))
        min_oos_trades = int(_maybe_float(request.args.get("min_oos_trades"), 0))
        dedupe = request.args.get("dedupe", "1") not in ("0", "false", "no")
        order_by = request.args.get("order_by", "score")
        top = int(_maybe_float(request.args.get("top"), 200))

        rows = top_strategies(
            run_id=run_id,
            n=top,
            order_by=order_by,
            min_win_rate=min_win_rate,
            min_trades=min_trades,
            min_oos_trades=min_oos_trades,
            dedupe=dedupe,
        )
        status = run_status(run_id)
        return jsonify(
            {
                "status": status,
                "count": len(rows),
                "min_win_rate": min_win_rate,
                "strategies": rows,
            }
        )

    return app


def _maybe_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "" or value == "null":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_float(value: Optional[str], default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run_server(host: str = "127.0.0.1", port: int = 8000, debug: bool = False) -> None:
    from ..db import init_db

    init_db()  # idempotent: ensures tables exist so the page never 500s on a fresh DB
    print(f"[web] dashboard on http://{host}:{port}  (Ctrl-C to stop)")
    create_app().run(host=host, port=port, debug=debug)
