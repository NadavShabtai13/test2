import numpy as np

from sptrader.indicators import library as ta


def test_sma_matches_rolling_mean(ohlcv):
    out = ta.sma(ohlcv["close"], 10)
    expected = ohlcv["close"].rolling(10).mean()
    assert np.allclose(out.dropna(), expected.dropna())


def test_rsi_bounds(ohlcv):
    r = ta.rsi(ohlcv["close"], 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_macd_components(ohlcv):
    m = ta.macd(ohlcv["close"])
    assert set(m.columns) == {"macd", "signal", "hist"}
    assert np.allclose((m["macd"] - m["signal"]).dropna(), m["hist"].dropna())


def test_atr_positive(ohlcv):
    a = ta.atr(ohlcv["high"], ohlcv["low"], ohlcv["close"], 14).dropna()
    assert (a >= 0).all()


def test_bollinger_order(ohlcv):
    bb = ta.bollinger_bands(ohlcv["close"], 20, 2.0).dropna()
    assert (bb["upper"] >= bb["mid"]).all()
    assert (bb["mid"] >= bb["lower"]).all()


def test_supertrend_direction_values(ohlcv):
    st = ta.supertrend(ohlcv["high"], ohlcv["low"], ohlcv["close"]).dropna()
    assert set(st["direction"].unique()).issubset({-1.0, 1.0})


def test_vwap_resets_each_trading_day():
    import pandas as pd

    idx = pd.DatetimeIndex(
        [
            "2024-06-03 13:30:00",
            "2024-06-03 14:30:00",
            "2024-06-04 13:30:00",
        ],
        tz="UTC",
    )
    high = pd.Series([110.0, 112.0, 200.0], index=idx)
    low = pd.Series([100.0, 102.0, 190.0], index=idx)
    close = pd.Series([105.0, 107.0, 195.0], index=idx)
    volume = pd.Series([1000.0, 2000.0, 500.0], index=idx)

    v = ta.vwap(high, low, close, volume)
    assert np.isclose(v.iloc[0], 105.0)
    assert np.isclose(v.iloc[2], 195.0)

    tp = (high + low + close) / 3
    continuous = (tp * volume).cumsum() / volume.cumsum().replace(0, np.nan)
    assert not np.isclose(v.iloc[2], continuous.iloc[2])


def test_no_lookahead_sma():
    # SMA at t must not depend on t+1: changing a future value leaves past intact.
    import pandas as pd

    s = pd.Series(np.arange(50, dtype=float))
    base = ta.sma(s, 5)
    s2 = s.copy()
    s2.iloc[40] = 999
    mod = ta.sma(s2, 5)
    assert np.allclose(base.iloc[:36].dropna(), mod.iloc[:36].dropna())
