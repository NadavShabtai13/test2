"""Command-line interface.

Usage:
    python -m sptrader <command> [options]

Commands:
    check-db   verify the database connection
    init-db    create tables
    ingest     pull Yahoo data, resample to 2h, store (idempotent / resumable)
    optimize   run/resume the indicator-permutation search
    status     show progress of a run
    report     show the top strategies of a run
    web        launch the dashboard UI (strategies above a success-rate filter)

  Phase 2 (live / paper trading):
    promote      freeze a result row as the active live strategy
    live-signal  print the current LONG/SHORT/FLAT decision
    live-run     run the live/paper trader (dry-run by default)
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .config import get_settings


def _cmd_check_db(args) -> int:
    from sqlalchemy import text

    from .db import get_engine

    settings = get_settings()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        print(f"[ok] connected to {settings.redacted_url()}")
        return 0
    except Exception as exc:
        print(f"[error] could not connect to {settings.redacted_url()}\n  {exc}")
        print(
            "\nHint: start Postgres with `docker compose up -d` and copy "
            "`.env.example` to `.env`."
        )
        return 1


def _cmd_init_db(args) -> int:
    from .db import init_db

    init_db()
    print("[ok] tables created (if not already present).")
    return 0


def _cmd_ingest(args) -> int:
    from .data.ingest import ingest

    summary = ingest(
        symbol=args.symbol,
        lookback_days=args.lookback_days,
        base_interval=args.base_interval,
        target_interval=args.target_interval,
    )
    print("[ingest]")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


def _cmd_optimize(args) -> int:
    from .optimize.permutations import DEFAULT_CONFIG, count_strategies
    from .optimize.runner import run_optimization

    config = {}
    if args.categories:
        config["categories"] = args.categories
    if args.max_combo_size is not None:
        config["max_combo_size"] = args.max_combo_size
    if args.modes:
        config["modes"] = args.modes
    if args.adx_filters is not None:
        config["adx_filters"] = [None if a < 0 else a for a in args.adx_filters]
    if args.combines:
        config["combines"] = args.combines
    if args.dense:
        config["dense"] = True
    if args.all_combos:
        config["cross_category_only"] = False

    # --full = exhaustive: all indicators, dense grids, all combos, AND+OR.
    if args.full:
        config["dense"] = True
        config["cross_category_only"] = False
        config["combines"] = ["and", "or"]
        config["modes"] = config.get("modes") or ["long_only", "long_short"]

    if args.dry_run:
        cfg = {**DEFAULT_CONFIG, **config}
        n = count_strategies(cfg)
        w = args.workers or 2
        rate = 200.0 * w  # rough strategies/sec/worker on this engine
        secs = n / rate
        print(f"[dry-run] config: {cfg}")
        print(f"[dry-run] total strategies: {n:,}")
        print(
            f"[dry-run] rough ETA @ ~{rate:.0f}/s ({w} workers): "
            f"{secs/60:.1f} min ({secs/3600:.2f} h)"
        )
        return 0

    result = run_optimization(
        config=config or None,
        name=args.name,
        restart=args.restart,
        max_combos=args.max_combos,
        workers=args.workers,
    )
    print(f"[optimize] {result}")
    return 0


def _cmd_status(args) -> int:
    from .reporting import run_status

    st = run_status(args.run_id)
    if st is None:
        print("(no runs found)")
        return 0
    print("[status]")
    for k, v in st.items():
        print(f"  {k}: {v}")
    return 0


def _cmd_report(args) -> int:
    from .reporting import format_top_table, top_strategies

    rows = top_strategies(
        run_id=args.run_id,
        n=args.top,
        order_by=args.order_by,
        min_win_rate=args.min_win_rate,
        min_trades=args.min_trades,
        min_oos_trades=args.min_oos_trades,
        dedupe=not args.no_dedupe,
    )
    print(format_top_table(rows))
    return 0


def _cmd_web(args) -> int:
    from .web.app import run_server

    run_server(host=args.host, port=args.port, debug=args.debug)
    return 0


def _cmd_promote(args) -> int:
    from .live.store import promote

    info = promote(result_id=args.result_id, name=args.name)
    print("[promote] active live strategy:")
    for k, v in info.items():
        print(f"  {k}: {v}")
    return 0


def _cmd_live_signal(args) -> int:
    from .optimize.permutations import StrategySpec
    from .live.signal import compute_live_decision
    from .live.store import get_active

    active = get_active()
    if active is None:
        print("(no active live strategy — run `promote --result-id N` first)")
        return 1
    spec = StrategySpec.from_dict(active["spec"])
    d = compute_live_decision(spec, active["symbol"])
    print(f"[live-signal] {active['name']} on {active['symbol']}")
    for k, v in d.items():
        print(f"  {k}: {v}")
    return 0


def _criteria_from_args(args):
    from .live.selector import SelectionCriteria

    return SelectionCriteria(
        min_win_rate=args.min_win_rate,
        min_trades=args.min_trades,
        min_oos_trades=args.min_oos_trades,
        order_by=args.order_by,
        run_id=args.run_id,
    )


def _add_selection_flags(sp) -> None:
    sp.add_argument("--run-id", type=int, default=None, help="pick from this run (default: latest)")
    sp.add_argument(
        "--order-by",
        default="score",
        choices=["score", "oos_sharpe", "oos_return", "win_rate"],
        help="ranking metric; 'score' = robustness = min(IS,OOS Sharpe)",
    )
    sp.add_argument("--min-win-rate", type=float, default=0.0)
    sp.add_argument("--min-trades", type=int, default=20)
    sp.add_argument("--min-oos-trades", type=int, default=10)
    sp.add_argument(
        "--strategies",
        type=int,
        default=1,
        help="how many top strategies to run side by side as an ensemble "
        "(each gets its own position sleeve; default 1)",
    )


def _cmd_auto_select(args) -> int:
    from .live.selector import auto_select_ensemble

    infos = auto_select_ensemble(_criteria_from_args(args), n=args.strategies)
    if not infos:
        print("[auto-select] no strategy met the criteria — nothing promoted.")
        return 1
    print(f"[auto-select] active ensemble ({len(infos)} strategy(ies)):")
    for info in infos:
        m = info["selected_metrics"]
        print(
            f"  {info['name']}: {m['strategy']} "
            f"(win={m['win_rate']:.0%}, score={m['score']:.1f}, oos_trades={m['oos_trades']})"
        )
    return 0


def _cmd_live_run(args) -> int:
    if args.mode == "live" and not args.i_understand_the_risk:
        print(
            "[refused] mode=live trades REAL money. Re-run with "
            "--i-understand-the-risk after paper-trading. Defaulting to safety."
        )
        return 1
    from .live.runner import run_live

    reselect_on_flat = args.reselect_on_flat
    if reselect_on_flat is None:
        reselect_on_flat = bool(args.auto)

    run_live(
        mode=args.mode,
        poll_seconds=args.poll_seconds,
        once=args.once,
        market_hours_only=not args.ignore_market_hours,
        auto=args.auto,
        criteria=_criteria_from_args(args) if args.auto else None,
        num_strategies=args.strategies,
        combine=args.combine,
        reselect_on_flat=reselect_on_flat,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sptrader", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check-db", help="verify DB connection").set_defaults(func=_cmd_check_db)
    sub.add_parser("init-db", help="create tables").set_defaults(func=_cmd_init_db)

    pi = sub.add_parser("ingest", help="pull + resample + store candles")
    pi.add_argument("--symbol", default=None)
    pi.add_argument("--lookback-days", type=int, default=None)
    pi.add_argument("--base-interval", default=None)
    pi.add_argument("--target-interval", default=None)
    pi.set_defaults(func=_cmd_ingest)

    po = sub.add_parser("optimize", help="run/resume the permutation search")
    po.add_argument("--name", default="default")
    po.add_argument("--restart", action="store_true", help="discard prior progress")
    po.add_argument("--max-combos", type=int, default=None, help="cap number of permutations")
    po.add_argument("--categories", nargs="*", default=None)
    po.add_argument("--max-combo-size", type=int, default=None, choices=[1, 2, 3])
    po.add_argument("--modes", nargs="*", default=None, choices=["long_only", "long_short"])
    po.add_argument(
        "--adx-filters",
        nargs="*",
        type=float,
        default=None,
        help="ADX thresholds to gate entries; use a negative value to mean 'no filter'",
    )
    po.add_argument(
        "--combines",
        nargs="*",
        default=None,
        choices=["and", "or"],
        help="vote-combination logic to try (default: and)",
    )
    po.add_argument(
        "--dense", action="store_true", help="use dense parameter grids (many more values)"
    )
    po.add_argument(
        "--all-combos",
        action="store_true",
        help="allow every combination incl. same-category (not just cross-category)",
    )
    po.add_argument(
        "--full",
        action="store_true",
        help="exhaustive: all indicators, dense grids, all combos, AND+OR (very large)",
    )
    po.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resulting strategy count + ETA without running",
    )
    po.add_argument(
        "--workers",
        type=int,
        default=None,
        help="parallel worker processes (default: WORKERS env or 2)",
    )
    po.set_defaults(func=_cmd_optimize)

    ps = sub.add_parser("status", help="show run progress")
    ps.add_argument("--run-id", type=int, default=None)
    ps.set_defaults(func=_cmd_status)

    pr = sub.add_parser("report", help="show top strategies")
    pr.add_argument("--run-id", type=int, default=None)
    pr.add_argument("--top", type=int, default=15)
    pr.add_argument(
        "--order-by",
        default="score",
        choices=["score", "oos_sharpe", "oos_return", "win_rate"],
    )
    pr.add_argument(
        "--min-win-rate",
        type=float,
        default=0.0,
        help="only show strategies whose success rate >= this fraction (e.g. 0.70)",
    )
    pr.add_argument(
        "--min-trades",
        type=int,
        default=0,
        help="ignore strategies with fewer than this many trades (avoids tiny samples)",
    )
    pr.add_argument(
        "--min-oos-trades",
        type=int,
        default=0,
        help="require at least this many out-of-sample trades (kills 1-trade flukes)",
    )
    pr.add_argument(
        "--no-dedupe",
        action="store_true",
        help="keep identical-metric clone strategies (default: collapse them)",
    )
    pr.set_defaults(func=_cmd_report)

    pw = sub.add_parser("web", help="launch the strategy dashboard UI")
    pw.add_argument("--host", default="127.0.0.1")
    pw.add_argument("--port", type=int, default=8000)
    pw.add_argument("--debug", action="store_true")
    pw.set_defaults(func=_cmd_web)

    # --- Phase 2: live / paper trading -------------------------------------
    pp = sub.add_parser("promote", help="freeze a result as the active live strategy")
    pp.add_argument("--result-id", type=int, required=True)
    pp.add_argument("--name", default=None)
    pp.set_defaults(func=_cmd_promote)

    pas = sub.add_parser(
        "auto-select",
        help="let the bot pick the best strategy from the DB and make it active",
    )
    _add_selection_flags(pas)
    pas.set_defaults(func=_cmd_auto_select)

    pls = sub.add_parser("live-signal", help="print the current LONG/SHORT/FLAT decision")
    pls.set_defaults(func=_cmd_live_signal)

    plr = sub.add_parser("live-run", help="run the live/paper trader")
    plr.add_argument(
        "--mode", default="dry-run", choices=["dry-run", "paper", "live"],
        help="dry-run (no orders), paper (IBKR paper), live (real money)",
    )
    plr.add_argument("--poll-seconds", type=int, default=900)
    plr.add_argument("--once", action="store_true", help="decide once and exit")
    plr.add_argument("--ignore-market-hours", action="store_true")
    plr.add_argument(
        "--combine",
        default="priority",
        choices=["priority", "net"],
        help="with >1 strategy: 'priority' = one position, best-ranked firing "
        "strategy owns the trade (sticky); 'net' = sum of sleeves",
    )
    plr.add_argument(
        "--auto",
        action="store_true",
        help="auto-pick the best strategy from the DB at the start of each trading day "
        "(no manual promote needed)",
    )
    plr.add_argument(
        "--reselect-on-flat",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with --auto + priority: refresh the top-N pool after each closed trade "
        "(default: on when --auto is set)",
    )
    _add_selection_flags(plr)
    plr.add_argument(
        "--i-understand-the-risk",
        action="store_true",
        help="required confirmation for --mode live (real money)",
    )
    plr.set_defaults(func=_cmd_live_run)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
