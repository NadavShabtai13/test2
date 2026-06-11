"""Broker adapters.

``DryRunBroker``  -- logs decisions, places no orders, tracks an in-memory
                    position. Works with no external dependencies.
``IBKRBroker``    -- Interactive Brokers via ib_insync (lazy import). Connects
                    to a running TWS / IB Gateway. Defaults to the *paper*
                    trading port; real money requires an explicit opt-in.

All adapters expose the same tiny interface:
    get_position(symbol) -> int
    set_target(symbol, qty) -> dict   # reconcile account to qty
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict


class DryRunBroker:
    """Simulated broker: no orders, in-memory net position."""

    name = "dry-run"

    def __init__(self) -> None:
        self._pos: Dict[str, int] = {}

    def get_position(self, symbol: str) -> int:
        return self._pos.get(symbol, 0)

    def set_target(self, symbol: str, qty: int) -> Dict[str, Any]:
        cur = self.get_position(symbol)
        delta = qty - cur
        self._pos[symbol] = qty
        action = "HOLD" if delta == 0 else ("BUY" if delta > 0 else "SELL")
        return {"action": action, "delta": delta, "from": cur, "to": qty, "filled": True}


class IBKRBroker:
    """Interactive Brokers adapter (paper by default).

    Ports: TWS paper 7497 / live 7496; IB Gateway paper 4002 / live 4001.
    """

    name = "ibkr"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 17,
        allow_live: bool = False,
    ) -> None:
        live_ports = {7496, 4001}
        if port in live_ports and not allow_live:
            raise RuntimeError(
                f"port {port} is a LIVE (real-money) IBKR port. Refusing without "
                "explicit opt-in. Use a paper port (7497/4002) or pass allow_live."
            )
        try:
            from ib_insync import IB  # lazy: only needed for real trading
        except Exception as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "ib_insync is required for IBKR trading. It is in requirements.txt; "
                f"rebuild the image. Import error: {exc}"
            )
        self._IB = IB
        self.ib = IB()
        self.ib.connect(host, port, clientId=client_id)

    def _contract(self, symbol: str):
        from ib_insync import Stock

        return Stock(symbol, "SMART", "USD")

    def get_position(self, symbol: str) -> int:
        for p in self.ib.positions():
            if getattr(p.contract, "symbol", None) == symbol:
                return int(p.position)
        return 0

    def set_target(self, symbol: str, qty: int) -> Dict[str, Any]:
        from ib_insync import MarketOrder

        cur = self.get_position(symbol)
        delta = qty - cur
        if delta == 0:
            return {"action": "HOLD", "delta": 0, "from": cur, "to": qty, "filled": True}
        order = MarketOrder("BUY" if delta > 0 else "SELL", abs(delta))
        trade = self.ib.placeOrder(self._contract(symbol), order)
        timeout_s = float(os.getenv("IBKR_FILL_TIMEOUT", "30"))
        deadline = time.monotonic() + timeout_s
        while not trade.isDone():
            if time.monotonic() >= deadline:
                break
            self.ib.sleep(0.1)
        status = trade.orderStatus.status
        avg_fill = getattr(trade.orderStatus, "avgFillPrice", None)
        return {
            "action": "BUY" if delta > 0 else "SELL",
            "delta": delta,
            "from": cur,
            "to": qty,
            "status": status,
            "filled": status == "Filled",
            "avg_fill_price": float(avg_fill) if avg_fill else None,
        }

    def disconnect(self) -> None:
        try:
            self.ib.disconnect()
        except Exception:
            pass


def make_broker(mode: str):
    """Factory. ``mode`` in {dry-run, paper, live}."""
    if mode == "dry-run":
        return DryRunBroker()
    host = os.getenv("IBKR_HOST", "127.0.0.1")
    client_id = int(os.getenv("IBKR_CLIENT_ID", "17"))
    if mode == "paper":
        port = int(os.getenv("IBKR_PORT", "7497"))
        return IBKRBroker(host, port, client_id, allow_live=False)
    if mode == "live":
        port = int(os.getenv("IBKR_PORT", "7496"))
        return IBKRBroker(host, port, client_id, allow_live=True)
    raise ValueError(f"unknown mode {mode!r} (use dry-run|paper|live)")
