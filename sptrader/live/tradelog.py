"""File logging for the live trader: per-decision JSONL + a human trade ledger.

Everything the runner prints to stdout is *also* persisted to ``LIVE_LOG_DIR``
(default ``logs/``, bind-mounted into the container) so trades can be
post-mortemed after the container is gone.

Two rolling files per UTC day:

* ``live-YYYY-MM-DD.jsonl`` -- one JSON object per poll decision (full snapshot:
  every sleeve's signal/price, the chosen owner, target qty, risk reason, broker
  order + fill). Machine-readable for analysis.
* ``trades-YYYY-MM-DD.log`` -- human-readable OPEN / CLOSE / FLIP / ADJUST events
  with entry/exit price, side, holding time and the owning strategy's indicators.
  This is the file to read when debugging "why did it take that trade?".
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _fmt_ts(ts_iso: Optional[str]) -> str:
    """ISO UTC -> 'UTC ... | NY ...' for readability across both clocks."""
    if not ts_iso:
        return "?"
    try:
        d = dt.datetime.fromisoformat(ts_iso)
    except ValueError:
        return ts_iso
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    ny = d.astimezone(_NY)
    return f"{d.astimezone(dt.timezone.utc):%Y-%m-%d %H:%M:%S}Z (NY {ny:%H:%M})"


def _side(qty: int) -> str:
    return "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT")


class TradeLogger:
    """Append-only logger. One instance per ``run_live`` process."""

    def __init__(self, log_dir: Optional[str] = None) -> None:
        base = log_dir or os.getenv("LIVE_LOG_DIR", "logs")
        self.dir = Path(base)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # never crash trading because logging failed
            print(f"[live][log] could not create log dir {self.dir}: {exc}")
        # Tracks the currently open position so CLOSE can report the round trip.
        self._open: Optional[Dict[str, Any]] = None

    # -- file paths (rotated per UTC day) ---------------------------------- #
    def _jsonl_path(self) -> Path:
        return self.dir / f"live-{_now_utc():%Y-%m-%d}.jsonl"

    def _ledger_path(self) -> Path:
        return self.dir / f"trades-{_now_utc():%Y-%m-%d}.log"

    def _append(self, path: Path, text: str) -> None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            print(f"[live][log] write failed ({path.name}): {exc}")

    # -- public API -------------------------------------------------------- #
    def event(self, message: str) -> None:
        """Generic non-decision line (startup, reselect, error, shutdown)."""
        line = f"{_now_utc():%Y-%m-%d %H:%M:%S}Z  {message}"
        self._append(self._ledger_path(), line)

    def decision(self, result: Dict[str, Any]) -> None:
        """Persist one poll decision and emit trade-ledger events on transitions."""
        self._append(self._jsonl_path(), json.dumps(result, default=str))

        prev = int(result.get("prev_position") or 0)
        target = int(result.get("target_qty") or 0)
        if prev == target:
            return  # no position change -> nothing for the ledger

        owner = result.get("owner")
        price = self._owner_price(result)
        order = result.get("order") or {}
        fill = order.get("avg_fill_price")
        fill_note = f", fill={fill}" if fill not in (None, 0) else ""
        status = order.get("status") or ("filled" if order.get("filled") else "?")
        ts_iso = result.get("ts")

        opened_flat = prev == 0 and target != 0
        closed_flat = prev != 0 and target == 0
        flipped = prev != 0 and target != 0 and (prev > 0) != (target > 0)

        if opened_flat:
            self._open_trade(owner, target, price, ts_iso, result)
            self._append(
                self._ledger_path(),
                f"{_fmt_ts(ts_iso)}  OPEN  {_side(target)} {abs(target)} "
                f"{result.get('symbol')} @ {price}  owner={owner}  "
                f"status={status}{fill_note}  reason={result.get('risk')}\n"
                f"    indicators: {self._owner_indicators(result)}",
            )
        elif closed_flat:
            self._close_trade(price, ts_iso, status, fill_note, result)
        elif flipped:
            self._close_trade(price, ts_iso, status, fill_note, result, suffix=" (flip)")
            self._open_trade(owner, target, price, ts_iso, result)
            self._append(
                self._ledger_path(),
                f"{_fmt_ts(ts_iso)}  OPEN  {_side(target)} {abs(target)} "
                f"{result.get('symbol')} @ {price}  owner={owner}  "
                f"status={status}{fill_note} (flip)\n"
                f"    indicators: {self._owner_indicators(result)}",
            )
        else:  # same side, size change
            self._append(
                self._ledger_path(),
                f"{_fmt_ts(ts_iso)}  ADJUST {_side(prev)} {prev} -> {target} "
                f"{result.get('symbol')} @ {price}  owner={owner}  status={status}{fill_note}",
            )

    # -- internals --------------------------------------------------------- #
    def _open_trade(self, owner, qty, price, ts_iso, result) -> None:
        self._open = {
            "owner": owner,
            "qty": qty,
            "entry_price": price,
            "entry_ts": ts_iso,
            "indicators": self._owner_indicators(result),
        }

    def _close_trade(self, price, ts_iso, status, fill_note, result, suffix="") -> None:
        o = self._open or {}
        entry = o.get("entry_price")
        side = _side(o.get("qty", 0))
        pnl_txt = "?"
        if entry not in (None, 0) and price not in (None, 0):
            move = (price - entry) / entry
            if o.get("qty", 0) < 0:
                move = -move
            pnl_txt = f"{move * 100:+.2f}%"
        held = self._holding(o.get("entry_ts"), ts_iso)
        self._append(
            self._ledger_path(),
            f"{_fmt_ts(ts_iso)}  CLOSE {side} {result.get('symbol')} @ {price}  "
            f"entry={entry}  move={pnl_txt}  held={held}  owner={o.get('owner')}  "
            f"status={status}{fill_note}{suffix}",
        )
        self._open = None

    @staticmethod
    def _holding(entry_ts: Optional[str], exit_ts: Optional[str]) -> str:
        if not entry_ts or not exit_ts:
            return "?"
        try:
            a = dt.datetime.fromisoformat(entry_ts)
            b = dt.datetime.fromisoformat(exit_ts)
        except ValueError:
            return "?"
        secs = abs((b - a).total_seconds())
        h, rem = divmod(int(secs), 3600)
        m = rem // 60
        return f"{h}h{m:02d}m"

    @staticmethod
    def _owner_price(result: Dict[str, Any]) -> Any:
        owner = result.get("owner")
        for s in result.get("sleeves", []) or []:
            if s.get("strategy") == owner:
                return s.get("price")
        # fall back to the first sleeve's price (all share one symbol/bar)
        sleeves = result.get("sleeves") or []
        return sleeves[0].get("price") if sleeves else None

    @staticmethod
    def _owner_indicators(result: Dict[str, Any]) -> str:
        owner = result.get("owner")
        for s in result.get("sleeves", []) or []:
            if s.get("strategy") == owner:
                return s.get("label") or owner or "?"
        return owner or "?"
