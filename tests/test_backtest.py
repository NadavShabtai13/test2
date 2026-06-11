import numpy as np
import pandas as pd

from sptrader.backtest.engine import backtest, periods_per_year, train_test_split


def test_periods_per_year_2h(ohlcv):
    ppy = periods_per_year(ohlcv.index)
    # 2h bars -> 12 per day -> ~4380 per calendar year
    assert 4000 < ppy < 4800


def test_buy_and_hold_matches_asset(ohlcv):
    close = ohlcv["close"]
    pos = pd.Series(1.0, index=close.index)
    m = backtest(close, pos, cost_bps=0.0)
    # With zero cost and full exposure, return ~ close-to-close compounded.
    bh = (1 + close.pct_change().fillna(0)).prod() - 1
    # one-bar entry delay drops the first return
    assert abs(m["total_return"] - bh) < 0.05
    # the same one-bar delay leaves the first bar flat, so exposure is (n-1)/n
    assert m["exposure"] == (len(close) - 1) / len(close)


def test_flat_position_is_zero(ohlcv):
    close = ohlcv["close"]
    pos = pd.Series(0.0, index=close.index)
    m = backtest(close, pos)
    assert m["total_return"] == 0.0
    assert m["num_trades"] == 0
    assert m["sharpe"] == 0.0


def test_costs_reduce_return(ohlcv):
    close = ohlcv["close"]
    # alternate in/out every bar to incur turnover
    pos = pd.Series(np.tile([1.0, 0.0], len(close) // 2 + 1)[: len(close)], index=close.index)
    no_cost = backtest(close, pos, cost_bps=0.0)["total_return"]
    with_cost = backtest(close, pos, cost_bps=10.0)["total_return"]
    assert with_cost < no_cost


def test_train_test_split_chronological(ohlcv):
    tr, te = train_test_split(ohlcv, 0.7)
    assert len(tr) + len(te) == len(ohlcv)
    assert tr.index.max() <= te.index.min()
