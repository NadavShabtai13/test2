import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def ohlcv():
    """Deterministic synthetic 2h OHLCV with a trend + noise."""
    rng = np.random.default_rng(42)
    n = 600
    idx = pd.date_range("2024-01-01", periods=n, freq="2h", tz="UTC")
    drift = np.linspace(0, 0.5, n)
    noise = rng.normal(0, 0.01, n).cumsum()
    close = 100 * np.exp(drift / 5 + noise)
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    open_ = close.copy()
    open_[1:] = close[:-1]
    volume = rng.integers(1_000, 10_000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
