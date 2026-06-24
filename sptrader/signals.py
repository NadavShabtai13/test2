"""Signal layer: turn indicators into discrete exposure votes, then combine.

A *signal instance* = (factory name, concrete params). Each builder returns a
Series of votes in {-1, 0, +1} meaning desired exposure at that bar:

    +1  want long      -1  want short      0  want flat

Trend/breakout signals emit a persistent regime (+1/-1). Mean-reversion signals
emit a *held* long state ({0, +1}) via an entry/exit state machine.

A *strategy* combines one or more instances (AND: act only when all agree),
optionally gated by an ADX trend-strength filter, in either long-only or
long-short mode.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

import numpy as np
import pandas as pd

from .indicators import library as ta

Builder = Callable[..., pd.Series]

CATEGORIES = ("trend", "momentum", "volatility", "volume")


@dataclass(frozen=True)
class SignalFactory:
    name: str
    category: str
    param_grid: Dict[str, List[Any]]
    builder: Builder


@dataclass(frozen=True)
class SignalInstance:
    factory: str
    params: Tuple[Tuple[str, Any], ...]  # sorted, hashable

    @property
    def params_dict(self) -> Dict[str, Any]:
        return dict(self.params)

    def key(self) -> str:
        inner = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.factory}({inner})"

    @property
    def category(self) -> str:
        return REGISTRY[self.factory].category


# --------------------------------------------------------------------------- #
# State-machine helpers (vectorized)
# --------------------------------------------------------------------------- #


def _held_long(entry: pd.Series, exit_: pd.Series) -> pd.Series:
    """Enter long on ``entry``, exit on ``exit_``; carry state forward -> {0,1}."""
    state = pd.Series(np.nan, index=entry.index)
    state[entry.fillna(False)] = 1.0
    state[exit_.fillna(False) & ~entry.fillna(False)] = 0.0
    return state.ffill().fillna(0.0)


def _held_regime(up: pd.Series, down: pd.Series) -> pd.Series:
    """Hold +1 after an up event until a down event, and vice versa -> {-1,0,1}."""
    state = pd.Series(np.nan, index=up.index)
    state[up.fillna(False)] = 1.0
    state[down.fillna(False)] = -1.0
    return state.ffill().fillna(0.0)


def _sign(series: pd.Series) -> pd.Series:
    return np.sign(series).fillna(0.0)


# --------------------------------------------------------------------------- #
# Builders -- trend
# --------------------------------------------------------------------------- #


def _ema_cross(df, fast, slow):
    return _sign(ta.ema(df["close"], fast) - ta.ema(df["close"], slow))


def _sma_cross(df, fast, slow):
    return _sign(ta.sma(df["close"], fast) - ta.sma(df["close"], slow))


def _wma_cross(df, fast, slow):
    return _sign(ta.wma(df["close"], fast) - ta.wma(df["close"], slow))


def _macd_cross(df, fast, slow, signal):
    m = ta.macd(df["close"], fast, slow, signal)
    return _sign(m["macd"] - m["signal"])


def _price_vs_sma(df, period):
    return _sign(df["close"] - ta.sma(df["close"], period))


def _adx_di(df, period):
    a = ta.adx(df["high"], df["low"], df["close"], period)
    return _sign(a["plus_di"] - a["minus_di"])


def _supertrend_dir(df, period, multiplier):
    st = ta.supertrend(df["high"], df["low"], df["close"], period, multiplier)
    return st["direction"].fillna(0.0)


def _aroon_dir(df, period):
    a = ta.aroon(df["high"], df["low"], period)
    return _sign(a["aroon_up"] - a["aroon_down"])


def _psar_dir(df, step, max_step):
    sar = ta.parabolic_sar(df["high"], df["low"], step, max_step)
    return _sign(df["close"] - sar)


def _ichimoku_dir(df, conversion, base, span_b):
    ich = ta.ichimoku(df["high"], df["low"], conversion, base, span_b)
    cloud_top = ich[["senkou_a", "senkou_b"]].max(axis=1)
    cloud_bot = ich[["senkou_a", "senkou_b"]].min(axis=1)
    long = (df["close"] > cloud_top) & (ich["tenkan"] > ich["kijun"])
    short = (df["close"] < cloud_bot) & (ich["tenkan"] < ich["kijun"])
    return pd.Series(np.where(long, 1.0, np.where(short, -1.0, 0.0)), index=df.index)


# --------------------------------------------------------------------------- #
# Builders -- momentum
# --------------------------------------------------------------------------- #


def _rsi_meanrev(df, period, lower, upper):
    r = ta.rsi(df["close"], period)
    return _held_long(entry=r < lower, exit_=r > upper)


def _rsi_trend(df, period, level):
    return _sign(ta.rsi(df["close"], period) - level)


def _stoch_meanrev(df, k, d, smooth, lower, upper):
    s = ta.stochastic(df["high"], df["low"], df["close"], k, d, smooth)
    return _held_long(entry=s["k"] < lower, exit_=s["k"] > upper)


def _cci_sign(df, period):
    return _sign(ta.cci(df["high"], df["low"], df["close"], period))


def _williams_meanrev(df, period, lower, upper):
    w = ta.williams_r(df["high"], df["low"], df["close"], period)
    return _held_long(entry=w < lower, exit_=w > upper)


def _roc_sign(df, period):
    return _sign(ta.roc(df["close"], period))


def _stochrsi_meanrev(df, period, k, d, lower, upper):
    s = ta.stoch_rsi(df["close"], period, k, d)
    return _held_long(entry=s["k"] < lower, exit_=s["k"] > upper)


def _tsi_sign(df, long, short):
    return _sign(ta.tsi(df["close"], long, short))


def _momentum_sign(df, period):
    return _sign(ta.momentum(df["close"], period))


# --------------------------------------------------------------------------- #
# Builders -- volatility
# --------------------------------------------------------------------------- #


def _bollinger_meanrev(df, period, num_std):
    bb = ta.bollinger_bands(df["close"], period, num_std)
    return _held_long(entry=df["close"] < bb["lower"], exit_=df["close"] > bb["mid"])


def _bollinger_breakout(df, period, num_std):
    bb = ta.bollinger_bands(df["close"], period, num_std)
    return _held_regime(
        up=df["close"] > bb["upper"].shift(1), down=df["close"] < bb["lower"].shift(1)
    )


def _keltner_breakout(df, period, multiplier):
    kc = ta.keltner_channels(df["high"], df["low"], df["close"], period, multiplier)
    return _held_regime(
        up=df["close"] > kc["upper"].shift(1), down=df["close"] < kc["lower"].shift(1)
    )


def _donchian_breakout(df, period):
    dc = ta.donchian_channels(df["high"], df["low"], period)
    upper_prev = dc["upper"].shift(1)
    lower_prev = dc["lower"].shift(1)
    return _held_regime(up=df["close"] >= upper_prev, down=df["close"] <= lower_prev)


def _fvg_dir(df, min_gap_atr):
    """Continuation: hold long after a bullish FVG, short after a bearish one."""
    f = ta.fair_value_gap(df["high"], df["low"], df["close"], min_gap_atr)
    return _held_regime(up=f["bull"] > 0, down=f["bear"] > 0)


def _fvg_meanrev(df, min_gap_atr):
    """Mean-reversion: go long when price pulls back into the latest bullish FVG
    zone (acting as support), exit once it trades back above the gap top."""
    f = ta.fair_value_gap(df["high"], df["low"], df["close"], min_gap_atr)
    close = df["close"]
    in_zone = (close <= f["bull_top"]) & (close >= f["bull_bot"])
    above = close > f["bull_top"]
    return _held_long(entry=in_zone, exit_=above)


# --------------------------------------------------------------------------- #
# Builders -- volume
# --------------------------------------------------------------------------- #


def _obv_trend(df, period):
    o = ta.obv(df["close"], df["volume"])
    return _sign(o - ta.sma(o, period))


def _cmf_sign(df, period):
    return _sign(ta.cmf(df["high"], df["low"], df["close"], df["volume"], period))


def _mfi_meanrev(df, period, lower, upper):
    m = ta.mfi(df["high"], df["low"], df["close"], df["volume"], period)
    return _held_long(entry=m < lower, exit_=m > upper)


def _vwap_trend(df):
    v = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
    return _sign(df["close"] - v)


def _ad_trend(df, period):
    ad = ta.ad_line(df["high"], df["low"], df["close"], df["volume"])
    return _sign(ad - ta.sma(ad, period))


def _force_index_sign(df, period):
    return _sign(ta.force_index(df["close"], df["volume"], period))


def _eom_sign(df, period):
    return _sign(ta.ease_of_movement(df["high"], df["low"], df["volume"], period))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

REGISTRY: Dict[str, SignalFactory] = {}


def _register(name, category, param_grid, builder):
    REGISTRY[name] = SignalFactory(name, category, param_grid, builder)


_register("ema_cross", "trend", {"fast": [10, 20, 50], "slow": [50, 100, 200]}, _ema_cross)
_register("sma_cross", "trend", {"fast": [10, 20, 50], "slow": [50, 100, 200]}, _sma_cross)
_register("wma_cross", "trend", {"fast": [10, 20, 50], "slow": [50, 100, 200]}, _wma_cross)
_register("macd_cross", "trend", {"fast": [12, 8], "slow": [26, 21], "signal": [9, 5]}, _macd_cross)
_register("price_vs_sma", "trend", {"period": [50, 100, 200]}, _price_vs_sma)
_register("adx_di", "trend", {"period": [14]}, _adx_di)
_register("supertrend_dir", "trend", {"period": [10], "multiplier": [2.0, 3.0]}, _supertrend_dir)
_register("aroon_dir", "trend", {"period": [14, 25]}, _aroon_dir)
_register("psar_dir", "trend", {"step": [0.02], "max_step": [0.2]}, _psar_dir)
_register("ichimoku_dir", "trend", {"conversion": [9], "base": [26], "span_b": [52]}, _ichimoku_dir)

_register("rsi_meanrev", "momentum", {"period": [14], "lower": [25, 30], "upper": [60, 70]}, _rsi_meanrev)
_register("rsi_trend", "momentum", {"period": [14], "level": [50]}, _rsi_trend)
_register(
    "stoch_meanrev",
    "momentum",
    {"k": [14], "d": [3], "smooth": [3], "lower": [20], "upper": [80]},
    _stoch_meanrev,
)
_register("cci_sign", "momentum", {"period": [20]}, _cci_sign)
_register(
    "williams_meanrev", "momentum", {"period": [14], "lower": [-80], "upper": [-20]}, _williams_meanrev
)
_register("roc_sign", "momentum", {"period": [12]}, _roc_sign)
_register(
    "stochrsi_meanrev",
    "momentum",
    {"period": [14], "k": [3], "d": [3], "lower": [20], "upper": [80]},
    _stochrsi_meanrev,
)
_register("tsi_sign", "momentum", {"long": [25], "short": [13]}, _tsi_sign)
_register("momentum_sign", "momentum", {"period": [10]}, _momentum_sign)

_register("bollinger_meanrev", "volatility", {"period": [20], "num_std": [2.0, 2.5]}, _bollinger_meanrev)
_register("bollinger_breakout", "volatility", {"period": [20], "num_std": [2.0]}, _bollinger_breakout)
_register("keltner_breakout", "volatility", {"period": [20], "multiplier": [2.0]}, _keltner_breakout)
_register("donchian_breakout", "volatility", {"period": [20, 55]}, _donchian_breakout)
_register("fvg_dir", "volatility", {"min_gap_atr": [0.0, 0.5]}, _fvg_dir)
_register("fvg_meanrev", "volatility", {"min_gap_atr": [0.0, 0.5]}, _fvg_meanrev)

_register("obv_trend", "volume", {"period": [20]}, _obv_trend)
_register("cmf_sign", "volume", {"period": [20]}, _cmf_sign)
_register("mfi_meanrev", "volume", {"period": [14], "lower": [20], "upper": [80]}, _mfi_meanrev)
_register("vwap_trend", "volume", {}, _vwap_trend)
_register("ad_trend", "volume", {"period": [20]}, _ad_trend)
_register("force_index_sign", "volume", {"period": [13]}, _force_index_sign)
_register("eom_sign", "volume", {"period": [14]}, _eom_sign)


# --------------------------------------------------------------------------- #
# Dense parameter grids (used by the exhaustive "--full" search). Each entry
# replaces the sparse default grid above with many more values, so the
# permutation engine explores the full parameter space per indicator.
# --------------------------------------------------------------------------- #

DENSE_GRIDS: Dict[str, Dict[str, List[Any]]] = {
    "ema_cross": {"fast": [5, 10, 15, 20, 30, 50], "slow": [50, 100, 200, 350, 500, 1400]},
    "sma_cross": {"fast": [5, 10, 15, 20, 30, 50], "slow": [50, 100, 200, 350, 500, 1400]},
    "wma_cross": {"fast": [5, 10, 15, 20, 30, 50], "slow": [50, 100, 200, 350, 500, 1400]},
    "macd_cross": {"fast": [6, 8, 12], "slow": [21, 26, 34], "signal": [5, 9]},
    "price_vs_sma": {"period": [10, 20, 50, 100, 200, 350, 500, 1400]},
    "adx_di": {"period": [7, 10, 14, 20]},
    "supertrend_dir": {"period": [7, 10, 14], "multiplier": [2.0, 3.0, 4.0]},
    "aroon_dir": {"period": [14, 25, 50]},
    "psar_dir": {"step": [0.01, 0.02, 0.04], "max_step": [0.2]},
    "ichimoku_dir": {"conversion": [9, 12, 24], "base": [26, 36, 72], "span_b": [52, 72, 144]},
    "rsi_meanrev": {"period": [2, 4, 7, 14, 21], "lower": [10, 20, 25, 30], "upper": [60, 70, 75, 80]},
    "rsi_trend": {"period": [7, 14, 21], "level": [45, 50, 55]},
    "stoch_meanrev": {"k": [2, 4, 7, 9, 14], "d": [3], "smooth": [3], "lower": [15, 20], "upper": [80, 85]},
    "cci_sign": {"period": [10, 14, 20, 40]},
    "williams_meanrev": {"period": [10, 14, 21], "lower": [-90, -80], "upper": [-20, -10]},
    "roc_sign": {"period": [6, 9, 12, 20]},
    "stochrsi_meanrev": {"period": [14, 21], "k": [3], "d": [3], "lower": [15, 20], "upper": [80, 85]},
    "tsi_sign": {"long": [25, 30, 40], "short": [13, 15, 20]},
    "momentum_sign": {"period": [5, 10, 20, 30]},
    "bollinger_meanrev": {"period": [10, 14, 20, 30], "num_std": [2.0, 2.5, 3.0]},
    "bollinger_breakout": {"period": [10, 20, 30, 50], "num_std": [1.5, 2.0, 2.5, 3.0]},
    "keltner_breakout": {"period": [10, 20], "multiplier": [1.5, 2.0, 2.5]},
    "donchian_breakout": {"period": [10, 20, 40, 55]},
    "fvg_dir": {"min_gap_atr": [0.0, 0.25, 0.5, 1.0]},
    "fvg_meanrev": {"min_gap_atr": [0.0, 0.25, 0.5, 1.0]},
    "obv_trend": {"period": [20, 50, 100]},
    "cmf_sign": {"period": [20, 50, 100]},
    "mfi_meanrev": {"period": [10, 14, 21], "lower": [15, 20], "upper": [80, 85]},
    "ad_trend": {"period": [20, 50, 100]},
    "force_index_sign": {"period": [2, 13, 21]},
    "eom_sign": {"period": [9, 14, 25]},
}


# --------------------------------------------------------------------------- #
# Instance enumeration & evaluation
# --------------------------------------------------------------------------- #


def _is_valid(factory: str, params: Dict[str, Any]) -> bool:
    # Crossovers require fast < slow.
    if "fast" in params and "slow" in params and params["fast"] >= params["slow"]:
        return False
    return True


def enumerate_instances(categories=CATEGORIES, dense: bool = False) -> List[SignalInstance]:
    """All concrete signal instances across the requested categories.

    When ``dense`` is True, each factory uses its richer ``DENSE_GRIDS`` entry
    (if present) instead of the sparse default grid -> many more instances.
    """
    instances: List[SignalInstance] = []
    for name, factory in REGISTRY.items():
        if factory.category not in categories:
            continue
        grid = DENSE_GRIDS.get(name, factory.param_grid) if dense else factory.param_grid
        keys = list(grid.keys())
        value_lists = [grid[k] for k in keys]
        if not keys:
            instances.append(SignalInstance(name, tuple()))
            continue
        for combo in itertools.product(*value_lists):
            params = dict(zip(keys, combo))
            if not _is_valid(name, params):
                continue
            instances.append(SignalInstance(name, tuple(sorted(params.items()))))
    return instances


def build_votes(df: pd.DataFrame, instance: SignalInstance) -> pd.Series:
    """Compute the {-1,0,1} vote series for a single signal instance."""
    factory = REGISTRY[instance.factory]
    votes = factory.builder(df, **instance.params_dict)
    return votes.reindex(df.index).fillna(0.0)


def combine_positions(
    vote_frame: pd.DataFrame,
    mode: str = "long_only",
    adx_series: pd.Series | None = None,
    adx_min: float | None = None,
    combine: str = "and",
) -> pd.Series:
    """Combine per-indicator votes into a target position.

    ``and``: act only when *every* indicator agrees (unanimous).
    ``or`` : act when *any* indicator votes a direction and none opposes it
             (a conflicting +1 / -1 mix cancels out to flat).
    Optionally gated by an ADX trend-strength filter.
    """
    if combine == "or":
        has_long = (vote_frame == 1).any(axis=1)
        has_short = (vote_frame == -1).any(axis=1)
        long_ = has_long & ~has_short
        short_ = has_short & ~has_long
    else:  # "and"
        long_ = (vote_frame == 1).all(axis=1)
        short_ = (vote_frame == -1).all(axis=1)

    pos = pd.Series(0.0, index=vote_frame.index)
    pos[long_] = 1.0
    pos[short_] = -1.0

    if mode == "long_only":
        pos = pos.clip(lower=0.0)

    if adx_series is not None and adx_min is not None:
        pos = pos.where(adx_series >= adx_min, 0.0)

    return pos
