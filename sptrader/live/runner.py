"""Live runner: decide once, or poll through the trading day."""
from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from ..optimize.permutations import StrategySpec
from .broker import make_broker
from .risk import RiskConfig, clamp_position, sleeve_quantity
from .selector import SelectionCriteria, auto_select_ensemble
from .signal import compute_live_decision
from .store import get_active_all
from .tradelog import TradeLogger


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _spec_label(spec_dict: Dict[str, Any]) -> str:
    """Human description of a strategy from its spec (indicators + params)."""
    parts = []
    for inst in spec_dict.get("instances", []):
        params = ",".join(f"{k}={v}" for k, v in (inst.get("params") or {}).items())
        parts.append(f"{inst.get('factory')}({params})" if params else inst.get("factory"))
    mode = spec_dict.get("mode", "")
    combine = spec_dict.get("combine", "and")
    return f"{' + '.join(parts)} [{mode},{combine}]"


_NY = ZoneInfo("America/New_York")
_RTH_OPEN = dt.time(9, 30)
_RTH_CLOSE = dt.time(16, 0)


def is_us_market_open(now: Optional[dt.datetime] = None) -> bool:
    """US regular-hours guard: Mon-Fri 09:30-16:00 America/New_York.

    DST-safe (unlike a fixed UTC window). Exchange holidays are not handled.
    """
    now = now or _now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    ny = now.astimezone(_NY)
    if ny.weekday() >= 5:
        return False
    t = ny.time()
    return _RTH_OPEN <= t <= _RTH_CLOSE


def _evaluate_all(strategies, risk: RiskConfig):
    """Compute each active strategy's current signal + sleeve size.

    Returns a list of (strategy_dict, decision_dict, sleeve_qty), ranked best
    first (lowest live_id == highest-ranked, since the ensemble is promoted in
    score order).
    """
    ranked = sorted(strategies, key=lambda s: s["live_id"])
    out = []
    for st in ranked:
        spec = StrategySpec.from_dict(st["spec"])
        d = compute_live_decision(spec, st["symbol"])
        out.append((st, d, sleeve_quantity(d["target"], risk)))
    return out


def decide_once(
    mode: str = "dry-run",
    risk: Optional[RiskConfig] = None,
    broker=None,
    combine: str = "priority",
    state: Optional[Dict[str, Any]] = None,
    logger: Optional[TradeLogger] = None,
) -> Dict[str, Any]:
    """Evaluate every active strategy and reconcile the position once.

    Two ways to combine multiple strategies on the same symbol:

    - ``priority`` (default): only ONE position at a time. The highest-ranked
      strategy whose signal is currently non-FLAT *owns* the trade and holds it
      until it goes FLAT (sticky); only then does the field reopen and the
      best-ranked firing strategy take over. Matches "one strategy per trade".
    - ``net``: every strategy is an independent sleeve contributing
      ``order_qty`` shares; the net position is their sum, clamped to
      ``max_position``. Strategies that disagree cancel out.
    """
    risk = risk or RiskConfig.from_env()
    state = state if state is not None else {}
    strategies = get_active_all()
    if not strategies:
        raise RuntimeError(
            "no active live strategy. Run `auto-select` / `promote` (or "
            "`live-run --auto`) first."
        )

    symbol = strategies[0]["symbol"]
    evals = _evaluate_all(strategies, risk)
    sleeves = [
        {
            "strategy": st["name"],
            "label": _spec_label(st["spec"]),
            "signal": d["label"],
            "as_of": d["as_of"],
            "price": d["price"],
            "sleeve_qty": sq,
        }
        for st, d, sq in evals
    ]

    owner = None
    if combine == "net":
        net = sum(sq for _, _, sq in evals)
        target, reason = clamp_position(net, risk)
        net_target = net
    else:  # priority: single position, rank-based, sticky owner
        net_target = None
        prev_owner = state.get("owner")
        chosen = None
        # 1) if the current owner is still firing, keep following it (sticky)
        if prev_owner is not None:
            for st, d, sq in evals:
                if st["name"] == prev_owner and sq != 0:
                    chosen = (st, d, sq)
                    break
        # 2) otherwise the best-ranked firing strategy takes the trade
        if chosen is None:
            for st, d, sq in evals:
                if sq != 0:
                    chosen = (st, d, sq)
                    break
        if chosen is None:
            target, reason = 0, "no strategy firing -> FLAT"
        else:
            st, d, sq = chosen
            owner = st["name"]
            target, clamp_reason = clamp_position(sq, risk)
            if clamp_reason != "ok":
                reason = clamp_reason
            else:
                reason = "ok" if owner == prev_owner else f"{owner} took the trade"
        state["owner"] = owner

    own_broker = broker is None
    broker = broker or make_broker(mode)
    try:
        current = broker.get_position(symbol)
        order = broker.set_target(symbol, target)
    finally:
        if own_broker and hasattr(broker, "disconnect"):
            broker.disconnect()

    out = {
        "ts": _now_utc().isoformat(),
        "mode": mode,
        "combine": combine,
        "symbol": symbol,
        "n_strategies": len(strategies),
        "owner": owner,
        "sleeves": sleeves,
        "net_target": net_target,
        "target_qty": target,
        "risk": reason,
        "prev_position": current,
        "order": order,
    }
    print("[live] " + json.dumps(out, default=str))
    if logger is not None:
        logger.decision(out)
    return out


def _reselect(
    criteria: SelectionCriteria, n: int = 1, logger: Optional[TradeLogger] = None
) -> bool:
    """Auto-pick the top ``n`` strategies from the DB and make them the active
    ensemble. Returns True if at least one strategy was selected."""
    infos = auto_select_ensemble(criteria, n=n)
    if not infos:
        print("[live][auto] no strategy met the criteria -- staying flat today.")
        if logger is not None:
            logger.event("auto-select: no strategy met the criteria -- staying flat")
        return False
    print(f"[live][auto] selected {len(infos)} strategy(ies):")
    if logger is not None:
        logger.event(f"auto-select: {len(infos)} strategy(ies) promoted")
    for info in infos:
        m = info["selected_metrics"]
        line = (
            f"{info['name']}: {m['strategy']} "
            f"(win={m['win_rate']:.0%}, score={m['score']:.1f}, oos_trades={m['oos_trades']})"
        )
        print(f"[live][auto]   {line}")
        if logger is not None:
            logger.event(f"  {line}")
    return True


def run_live(
    mode: str = "dry-run",
    poll_seconds: int = 900,
    once: bool = False,
    market_hours_only: bool = True,
    auto: bool = False,
    criteria: Optional[SelectionCriteria] = None,
    num_strategies: int = 1,
    combine: str = "priority",
    reselect_on_flat: bool = False,
) -> None:
    """Loop through the trading day, deciding every ``poll_seconds``.

    When ``auto`` is set, the top ``num_strategies`` strategies are re-selected
    from the DB at the start of each trading day (objective criteria), so no
    manual ``promote`` is needed. With ``reselect_on_flat`` (recommended for
    ``combine=priority``), the pool is refreshed after each closed trade so the
    next entry is again the highest-ranked strategy that fires. Reuses one
    broker connection.
    """
    risk = RiskConfig.from_env()
    criteria = criteria or SelectionCriteria()
    state: Dict[str, Any] = {"owner": None}
    logger = TradeLogger()
    config_line = (
        f"mode={mode} poll={poll_seconds}s market_hours_only={market_hours_only} "
        f"auto={auto} strategies={num_strategies} combine={combine} "
        f"reselect_on_flat={reselect_on_flat} risk={risk}"
    )
    print(f"[live] {config_line}")
    logger.event(f"START  {config_line}")
    print(f"[live] logging to {logger.dir.resolve()}")

    if once:
        if auto and not _reselect(criteria, n=num_strategies, logger=logger):
            return
        decide_once(mode=mode, risk=risk, combine=combine, state=state, logger=logger)
        return

    broker = make_broker(mode)
    last_select_day = None
    try:
        while True:
            open_now = (not market_hours_only) or is_us_market_open()
            if open_now:
                today = _now_utc().date()
                selected = True
                if auto and today != last_select_day:
                    selected = _reselect(criteria, n=num_strategies, logger=logger)
                    state["owner"] = None  # new day -> reopen the field
                    last_select_day = today
                if selected:
                    try:
                        result = decide_once(
                            mode=mode, risk=risk, broker=broker,
                            combine=combine, state=state, logger=logger,
                        )
                        if (
                            auto
                            and reselect_on_flat
                            and combine == "priority"
                            and result.get("prev_position", 0) != 0
                            and result.get("target_qty", 0) == 0
                        ):
                            print("[live][auto] trade closed -> re-selecting top strategies")
                            logger.event("trade closed -> re-selecting top strategies")
                            if _reselect(criteria, n=num_strategies, logger=logger):
                                state["owner"] = None
                    except Exception as exc:  # keep the daemon alive across transient errors
                        print(f"[live][error] {exc}")
                        logger.event(f"ERROR  {exc}")
            else:
                print(f"[live] market closed at {_now_utc().isoformat()} -- idle")
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("\n[live] stopped.")
        logger.event("STOP  (keyboard interrupt)")
    finally:
        if hasattr(broker, "disconnect"):
            broker.disconnect()
