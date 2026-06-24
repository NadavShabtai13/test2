"""sptrader: an S&P 500 indicator-permutation backtesting & strategy-search toolkit.

Pipeline:
    1. ingest   -> pull 1h OHLCV from Yahoo, store in Postgres
    2. optimize -> permute indicator-based strategies, backtest each, store results
    3. report   -> rank strategies by out-of-sample robustness

Everything is checkpointed in Postgres so any stage can stop and resume.
"""

__version__ = "0.1.0"
