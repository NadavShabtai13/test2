"""A broad, dependency-free technical-indicator library.

Everything is implemented in pure pandas/numpy so there are no native build
headaches. Functions accept OHLCV Series/DataFrames and return Series (or a
DataFrame of named components). All are causal (no look-ahead).

Categories covered:
    trend       SMA, EMA, WMA, MACD, ADX(+DI/-DI), Aroon, Parabolic SAR,
                Ichimoku, Supertrend
    momentum    RSI, Stochastic, StochRSI, CCI, Williams %R, ROC, Momentum, TSI
    volatility  Bollinger Bands, ATR, Keltner Channels, Donchian Channels,
                Fair Value Gap (ICT 3-bar imbalance)
    volume      OBV, VWAP, CMF, MFI, A/D line, Force Index, Ease of Movement
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(period, min_periods=period).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )


# --------------------------------------------------------------------------- #
# Trend
# --------------------------------------------------------------------------- #


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    # Wilder's smoothing
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.DataFrame:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    tr = true_range(high, low, close)
    atr_ = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_ = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return pd.DataFrame({"adx": adx_, "plus_di": plus_di, "minus_di": minus_di})


def aroon(high: pd.Series, low: pd.Series, period: int = 25) -> pd.DataFrame:
    def _since_extreme(x, fn):
        idx = fn(x)
        return period - (len(x) - 1 - idx)

    up = high.rolling(period + 1, min_periods=period + 1).apply(
        lambda x: 100 * _since_extreme(x, np.argmax) / period, raw=True
    )
    down = low.rolling(period + 1, min_periods=period + 1).apply(
        lambda x: 100 * _since_extreme(x, np.argmin) / period, raw=True
    )
    return pd.DataFrame({"aroon_up": up, "aroon_down": down, "aroon_osc": up - down})


def parabolic_sar(
    high: pd.Series, low: pd.Series, step: float = 0.02, max_step: float = 0.2
) -> pd.Series:
    """Classic Wilder Parabolic SAR. Iterative by nature."""
    n = len(high)
    if n == 0:
        return pd.Series(dtype=float, index=high.index)

    sar = np.zeros(n)
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)

    bull = True
    af = step
    ep = h[0]
    sar[0] = l[0]

    for i in range(1, n):
        prev = sar[i - 1]
        sar[i] = prev + af * (ep - prev)
        if bull:
            sar[i] = min(sar[i], l[i - 1], l[max(i - 2, 0)])
            if l[i] < sar[i]:
                bull = False
                sar[i] = ep
                ep = l[i]
                af = step
            elif h[i] > ep:
                ep = h[i]
                af = min(af + step, max_step)
        else:
            sar[i] = max(sar[i], h[i - 1], h[max(i - 2, 0)])
            if h[i] > sar[i]:
                bull = True
                sar[i] = ep
                ep = h[i]
                af = step
            elif l[i] < ep:
                ep = l[i]
                af = min(af + step, max_step)
    return pd.Series(sar, index=high.index)


def ichimoku(
    high: pd.Series,
    low: pd.Series,
    conversion: int = 9,
    base: int = 26,
    span_b: int = 52,
) -> pd.DataFrame:
    conv = (high.rolling(conversion).max() + low.rolling(conversion).min()) / 2
    base_line = (high.rolling(base).max() + low.rolling(base).min()) / 2
    span_a = ((conv + base_line) / 2).shift(base)
    span_b_line = ((high.rolling(span_b).max() + low.rolling(span_b).min()) / 2).shift(base)
    return pd.DataFrame(
        {"tenkan": conv, "kijun": base_line, "senkou_a": span_a, "senkou_b": span_b_line}
    )


def supertrend(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10, multiplier: float = 3.0
) -> pd.DataFrame:
    """Supertrend: returns the line and a direction (+1 up / -1 down)."""
    atr_ = atr(high, low, close, period)
    hl2 = (high + low) / 2
    upper = hl2 + multiplier * atr_
    lower = hl2 - multiplier * atr_

    n = len(close)
    final_upper = upper.to_numpy(dtype=float).copy()
    final_lower = lower.to_numpy(dtype=float).copy()
    c = close.to_numpy(dtype=float)
    direction = np.ones(n)
    st = np.full(n, np.nan)

    for i in range(1, n):
        if np.isnan(final_upper[i - 1]):
            continue
        final_upper[i] = (
            upper.iloc[i]
            if (upper.iloc[i] < final_upper[i - 1] or c[i - 1] > final_upper[i - 1])
            else final_upper[i - 1]
        )
        final_lower[i] = (
            lower.iloc[i]
            if (lower.iloc[i] > final_lower[i - 1] or c[i - 1] < final_lower[i - 1])
            else final_lower[i - 1]
        )
        if c[i] > final_upper[i - 1]:
            direction[i] = 1
        elif c[i] < final_lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
        st[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return pd.DataFrame(
        {"supertrend": pd.Series(st, index=close.index), "direction": pd.Series(direction, index=close.index)}
    )


# --------------------------------------------------------------------------- #
# Momentum
# --------------------------------------------------------------------------- #


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series, k: int = 14, d: int = 3, smooth: int = 3
) -> pd.DataFrame:
    lowest = low.rolling(k, min_periods=k).min()
    highest = high.rolling(k, min_periods=k).max()
    raw_k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    k_line = raw_k.rolling(smooth, min_periods=smooth).mean()
    d_line = k_line.rolling(d, min_periods=d).mean()
    return pd.DataFrame({"k": k_line, "d": d_line})


def stoch_rsi(close: pd.Series, period: int = 14, k: int = 3, d: int = 3) -> pd.DataFrame:
    r = rsi(close, period)
    lowest = r.rolling(period, min_periods=period).min()
    highest = r.rolling(period, min_periods=period).max()
    stoch = (r - lowest) / (highest - lowest).replace(0, np.nan)
    k_line = (100 * stoch).rolling(k, min_periods=k).mean()
    d_line = k_line.rolling(d, min_periods=d).mean()
    return pd.DataFrame({"k": k_line, "d": d_line})


def cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
    tp = (high + low + close) / 3
    ma = tp.rolling(period, min_periods=period).mean()
    md = (tp - ma).abs().rolling(period, min_periods=period).mean()
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def williams_r(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    highest = high.rolling(period, min_periods=period).max()
    lowest = low.rolling(period, min_periods=period).min()
    return -100 * (highest - close) / (highest - lowest).replace(0, np.nan)


def roc(close: pd.Series, period: int = 12) -> pd.Series:
    return 100 * (close / close.shift(period) - 1)


def momentum(close: pd.Series, period: int = 10) -> pd.Series:
    return close - close.shift(period)


def tsi(close: pd.Series, long: int = 25, short: int = 13) -> pd.Series:
    diff = close.diff()
    abs_diff = diff.abs()
    ema1 = diff.ewm(span=long, adjust=False).mean().ewm(span=short, adjust=False).mean()
    ema2 = abs_diff.ewm(span=long, adjust=False).mean().ewm(span=short, adjust=False).mean()
    return 100 * ema1 / ema2.replace(0, np.nan)


# --------------------------------------------------------------------------- #
# Volatility
# --------------------------------------------------------------------------- #


def bollinger_bands(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid.replace(0, np.nan)
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": width, "pct_b": pct_b})


def keltner_channels(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20, multiplier: float = 2.0
) -> pd.DataFrame:
    mid = ema(close, period)
    rng = atr(high, low, close, period)
    return pd.DataFrame(
        {"mid": mid, "upper": mid + multiplier * rng, "lower": mid - multiplier * rng}
    )


def donchian_channels(high: pd.Series, low: pd.Series, period: int = 20) -> pd.DataFrame:
    upper = high.rolling(period, min_periods=period).max()
    lower = low.rolling(period, min_periods=period).min()
    return pd.DataFrame({"upper": upper, "lower": lower, "mid": (upper + lower) / 2})


# --------------------------------------------------------------------------- #
# Price action -- Fair Value Gap (ICT 3-bar imbalance)
# --------------------------------------------------------------------------- #


def fair_value_gap(
    high: pd.Series, low: pd.Series, close: pd.Series, min_gap_atr: float = 0.0
) -> pd.DataFrame:
    """Detect 3-bar Fair Value Gaps (FVG / imbalances). Causal, no look-ahead.

    A *bullish* FVG forms at bar ``i`` when ``low[i] > high[i-2]`` -- the middle
    bar's impulse left an untraded gap between the high of two bars ago and the
    current low. A *bearish* FVG forms when ``high[i] < low[i-2]``.

    ``min_gap_atr`` ignores gaps thinner than ``min_gap_atr × ATR(14)`` so tiny
    noise imbalances are filtered out (0.0 keeps every gap).

    Returns per bar:
        bull, bear           1.0 on the bar a gap is detected, else 0.0
        bull_top, bull_bot   boundaries of the most-recent bullish gap (ffill)
        bear_top, bear_bot   boundaries of the most-recent bearish gap (ffill)
    """
    high_2 = high.shift(2)
    low_2 = low.shift(2)
    atr_ = atr(high, low, close, 14)
    threshold = (min_gap_atr * atr_).fillna(0.0)

    gap_up = low - high_2  # > 0 => bullish imbalance
    gap_dn = low_2 - high  # > 0 => bearish imbalance
    bull = (gap_up > 0) & (gap_up >= threshold)
    bear = (gap_dn > 0) & (gap_dn >= threshold)

    return pd.DataFrame(
        {
            "bull": bull.astype(float),
            "bear": bear.astype(float),
            "bull_top": low.where(bull).ffill(),
            "bull_bot": high_2.where(bull).ffill(),
            "bear_top": low_2.where(bear).ffill(),
            "bear_bot": high.where(bear).ffill(),
        }
    )


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Session VWAP: cumulative typical-price × volume, reset each US trading day."""
    tp = (high + low + close) / 3
    pv = tp * volume
    idx = high.index
    if isinstance(idx, pd.DatetimeIndex):
        if idx.tz is None:
            session = idx.normalize()
        else:
            session = idx.tz_convert("America/New_York").normalize()
        cum_pv = pv.groupby(session, sort=False).cumsum()
        cum_vol = volume.groupby(session, sort=False).cumsum().replace(0, np.nan)
    else:
        cum_pv = pv.cumsum()
        cum_vol = volume.cumsum().replace(0, np.nan)
    return cum_pv / cum_vol


def cmf(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 20
) -> pd.Series:
    mfm = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    mfv = mfm * volume
    return mfv.rolling(period, min_periods=period).sum() / volume.rolling(
        period, min_periods=period
    ).sum().replace(0, np.nan)


def mfi(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 14
) -> pd.Series:
    tp = (high + low + close) / 3
    raw_flow = tp * volume
    pos = raw_flow.where(tp > tp.shift(1), 0.0)
    neg = raw_flow.where(tp < tp.shift(1), 0.0)
    pos_sum = pos.rolling(period, min_periods=period).sum()
    neg_sum = neg.rolling(period, min_periods=period).sum()
    ratio = pos_sum / neg_sum.replace(0, np.nan)
    return 100 - (100 / (1 + ratio))


def ad_line(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    mfm = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    return (mfm * volume).cumsum()


def force_index(close: pd.Series, volume: pd.Series, period: int = 13) -> pd.Series:
    fi = close.diff() * volume
    return fi.ewm(span=period, adjust=False, min_periods=period).mean()


def ease_of_movement(
    high: pd.Series, low: pd.Series, volume: pd.Series, period: int = 14
) -> pd.Series:
    distance = ((high + low) / 2).diff()
    box = (volume / 1e8) / (high - low).replace(0, np.nan)
    emv = distance / box
    return emv.rolling(period, min_periods=period).mean()
