"""Yahoo Finance ingestion with resampling and idempotent, resumable storage."""
from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

from ..config import get_settings
from ..db import is_postgres, session_scope
from ..models import Candle, IngestionCheckpoint

# Map our human interval -> the pandas resample rule.
_RESAMPLE_RULE = {
    "2h": "2h",
    "4h": "4h",
    "1d": "1D",
}

_OHLCV_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def fetch_ohlcv(symbol: str, lookback_days: int, interval: str) -> pd.DataFrame:
    """Fetch OHLCV from Yahoo. Returns a tz-aware DataFrame indexed by timestamp.

    Columns: open, high, low, close, volume (lower-case).
    """
    import yfinance as yf

    period = f"{lookback_days}d"
    raw = yf.download(
        tickers=symbol,
        period=period,
        interval=interval,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if raw is None or raw.empty:
        raise RuntimeError(
            f"Yahoo returned no data for symbol={symbol!r} period={period} interval={interval}. "
            "Note: Yahoo limits intraday history (e.g. 1h is ~730 days max)."
        )

    # yfinance may return a MultiIndex column frame (field, ticker). Flatten it.
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    raw = raw.rename(columns=str.lower)
    cols = ["open", "high", "low", "close", "volume"]
    missing = [c for c in cols if c not in raw.columns]
    if missing:
        raise RuntimeError(f"Unexpected Yahoo columns; missing {missing}. Got {list(raw.columns)}")

    df = raw[cols].copy()
    df.index = _ensure_utc_index(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df.index.name = "ts"
    return df


def resample_ohlcv(df: pd.DataFrame, target_interval: str, base_interval: str | None = None) -> pd.DataFrame:
    """Resample a finer-grained OHLCV frame to ``target_interval`` (e.g. 1h -> 2h).

    If the target equals the base interval (e.g. 1h -> 1h) there is nothing to
    resample; the frame is returned as-is. Empty buckets (overnight/weekend
    gaps) are dropped.
    """
    if base_interval is not None and target_interval == base_interval:
        return df.copy()
    rule = _RESAMPLE_RULE.get(target_interval)
    if rule is None:
        raise ValueError(f"Unsupported target_interval {target_interval!r}")
    out = df.resample(rule, label="left", closed="left").agg(_OHLCV_AGG)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out.index.name = "ts"
    return out


def _ensure_utc_index(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    return idx


def _upsert_candles(session, symbol: str, interval: str, df: pd.DataFrame) -> int:
    """Insert/update candles. Uses Postgres ON CONFLICT when available."""
    if df.empty:
        return 0

    rows = [
        {
            "symbol": symbol,
            "interval": interval,
            "ts": ts.to_pydatetime(),
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume) if pd.notna(r.volume) else 0.0,
        }
        for ts, r in df.iterrows()
    ]

    if is_postgres():
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(Candle).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "interval", "ts"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        session.execute(stmt)
    else:
        # Generic fallback: delete overlapping range then bulk insert.
        ts_values = [r["ts"] for r in rows]
        session.query(Candle).filter(
            Candle.symbol == symbol,
            Candle.interval == interval,
            Candle.ts.in_(ts_values),
        ).delete(synchronize_session=False)
        session.bulk_insert_mappings(Candle, rows)

    return len(rows)


def _update_checkpoint(session, symbol: str, interval: str, last_ts: dt.datetime, rows: int) -> None:
    cp = (
        session.query(IngestionCheckpoint)
        .filter_by(symbol=symbol, interval=interval)
        .one_or_none()
    )
    if cp is None:
        cp = IngestionCheckpoint(symbol=symbol, interval=interval)
        session.add(cp)
    cp.last_ts = last_ts
    cp.rows = rows
    cp.updated_at = dt.datetime.now(dt.timezone.utc)


def ingest(
    symbol: Optional[str] = None,
    lookback_days: Optional[int] = None,
    base_interval: Optional[str] = None,
    target_interval: Optional[str] = None,
) -> dict:
    """Fetch, resample, and persist candles. Idempotent and safe to re-run.

    Returns a small summary dict.
    """
    from ..db import init_db

    init_db()  # idempotent: ensures tables exist (helps fresh containers)

    s = get_settings()
    symbol = symbol or s.symbol
    lookback_days = lookback_days or s.lookback_days
    base_interval = base_interval or s.base_interval
    target_interval = target_interval or s.target_interval

    base_df = fetch_ohlcv(symbol, lookback_days, base_interval)
    target_df = resample_ohlcv(base_df, target_interval, base_interval)
    same = target_interval == base_interval

    with session_scope() as session:
        n_base = _upsert_candles(session, symbol, base_interval, base_df)
        if not base_df.empty:
            _update_checkpoint(session, symbol, base_interval, base_df.index[-1].to_pydatetime(), n_base)
        if same:
            # base == target: store once, don't duplicate the same series.
            n_target = n_base
        else:
            n_target = _upsert_candles(session, symbol, target_interval, target_df)
            if not target_df.empty:
                _update_checkpoint(
                    session, symbol, target_interval, target_df.index[-1].to_pydatetime(), n_target
                )

    return {
        "symbol": symbol,
        "base_interval": base_interval,
        "target_interval": target_interval,
        "base_rows": n_base,
        "target_rows": n_target,
        "base_start": base_df.index[0].isoformat() if not base_df.empty else None,
        "base_end": base_df.index[-1].isoformat() if not base_df.empty else None,
        "target_start": target_df.index[0].isoformat() if not target_df.empty else None,
        "target_end": target_df.index[-1].isoformat() if not target_df.empty else None,
    }


def load_candles(symbol: str, interval: str) -> pd.DataFrame:
    """Load stored candles into a tz-aware OHLCV DataFrame indexed by ts."""
    from sqlalchemy import select

    with session_scope() as session:
        stmt = (
            select(Candle.ts, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume)
            .where(Candle.symbol == symbol, Candle.interval == interval)
            .order_by(Candle.ts)
        )
        records = session.execute(stmt).all()

    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(records, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    return df
