"""Runtime configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

try:  # optional dependency; .env is convenient but not required
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is in requirements but stay defensive
    pass


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass
class Settings:
    """All tunables in one place. Override any value via environment variables."""

    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg2://sptrader:sptrader@localhost:5432/sptrader",
        )
    )
    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", "SPY"))
    lookback_days: int = field(default_factory=lambda: _get_int("LOOKBACK_DAYS", 720))
    interval: str = "1h"
    cost_bps: float = field(default_factory=lambda: _get_float("COST_BPS", 5.0))
    train_fraction: float = field(default_factory=lambda: _get_float("TRAIN_FRACTION", 0.7))

    def redacted_url(self) -> str:
        """Database URL with the password masked, safe for logs."""
        url = self.database_url
        if "@" in url and ":" in url.split("@")[0]:
            head, tail = url.split("@", 1)
            scheme_user = head.rsplit(":", 1)[0]
            return f"{scheme_user}:***@{tail}"
        return url


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Process-wide cached settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
