"""Pre-trade risk controls. Non-negotiable before any real-money trading."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple


@dataclass
class RiskConfig:
    """Hard limits applied to every live decision."""

    order_qty: int = 10          # units (shares/contracts) per full long/short
    max_position: int = 10       # absolute cap on net position
    allow_short: bool = True     # if False, short signals become FLAT
    kill_switch: bool = False    # if True, force FLAT and place no new exposure

    @staticmethod
    def from_env() -> "RiskConfig":
        def _i(name, default):
            v = os.getenv(name)
            return int(v) if v not in (None, "") else default

        def _b(name, default):
            v = os.getenv(name)
            return v.lower() in ("1", "true", "yes") if v not in (None, "") else default

        return RiskConfig(
            order_qty=_i("LIVE_ORDER_QTY", 10),
            max_position=_i("LIVE_MAX_POSITION", 10),
            allow_short=_b("LIVE_ALLOW_SHORT", True),
            kill_switch=_b("LIVE_KILL_SWITCH", False),
        )


def sleeve_quantity(signal_target: float, risk: RiskConfig) -> int:
    """Per-strategy size BEFORE the net cap. {-1,0,+1} -> shares.

    Applies the kill switch and short ban (which zero out exposure) but not the
    net-position clamp -- that is applied once on the combined target.
    """
    if risk.kill_switch:
        return 0
    if signal_target < 0 and not risk.allow_short:
        return 0
    return int(round(signal_target)) * risk.order_qty


def clamp_position(qty: int, risk: RiskConfig) -> Tuple[int, str]:
    """Clamp a target quantity to +/- ``max_position``.

    Returns (quantity, reason) where reason explains any clamp applied.
    """
    capped = max(-risk.max_position, min(risk.max_position, qty))
    if capped != qty:
        return capped, f"capped to max_position {risk.max_position}"
    return capped, "ok"


def target_position(signal_target: float, risk: RiskConfig) -> Tuple[int, str]:
    """Translate a single {-1,0,+1} signal into a risk-clamped target quantity.

    Returns (quantity, reason). Combines :func:`sleeve_quantity` (kill switch /
    short ban) with :func:`clamp_position` (net cap) so all risk rules live here.
    """
    if risk.kill_switch:
        return 0, "kill_switch active -> FLAT"
    if signal_target < 0 and not risk.allow_short:
        return 0, "short disabled -> FLAT"
    return clamp_position(sleeve_quantity(signal_target, risk), risk)
