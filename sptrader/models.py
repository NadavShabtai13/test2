"""SQLAlchemy ORM models.

These tables are the persistence layer that makes every stage resumable:

* ``candles``                -> raw + resampled OHLCV (idempotent upsert)
* ``ingestion_checkpoints``  -> last bar stored per (symbol, interval)
* ``optimization_runs``      -> one row per search; tracks progress counters
* ``strategy_results``       -> one row per tested permutation (the resume key)
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "ts", name="uq_candle_symbol_interval_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    interval: Mapped[str] = mapped_column(String(8), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)


class IngestionCheckpoint(Base):
    __tablename__ = "ingestion_checkpoints"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", name="uq_ingest_symbol_interval"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32))
    interval: Mapped[str] = mapped_column(String(8))
    last_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class OptimizationRun(Base):
    __tablename__ = "optimization_runs"
    __table_args__ = (UniqueConstraint("config_hash", name="uq_run_config_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    config_json: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(String(32))
    interval: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done
    total_combos: Mapped[int] = mapped_column(Integer, default=0)
    completed_combos: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    results: Mapped[list["StrategyResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class StrategyResult(Base):
    __tablename__ = "strategy_results"
    __table_args__ = (
        UniqueConstraint("run_id", "signature", name="uq_result_run_signature"),
        # Collapses behaviorally-identical strategies (same metrics) so the DB
        # never stores duplicates -- enforced at insert via ON CONFLICT.
        UniqueConstraint("run_id", "dedup_key", name="uq_result_run_dedup"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("optimization_runs.id"), index=True)
    signature: Mapped[str] = mapped_column(String(255), index=True)
    # Hash of the rounded headline metrics; identical-behavior specs share it.
    dedup_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    spec_json: Mapped[str] = mapped_column(Text)

    # Selection score (higher is better); derived from IS+OOS robustness.
    score: Mapped[float] = mapped_column(Float, default=float("-inf"), index=True)

    # Headline metrics duplicated as columns for fast sorting/filtering.
    is_sharpe: Mapped[float] = mapped_column(Float, default=0.0)
    oos_sharpe: Mapped[float] = mapped_column(Float, default=0.0)
    oos_return: Mapped[float] = mapped_column(Float, default=0.0)
    oos_max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    num_trades: Mapped[int] = mapped_column(Integer, default=0)

    # Trade-level success ("win") rate, the metric the UI threshold filters on.
    # ``win_rate`` is the trades-weighted combination of IS + OOS, in [0, 1].
    win_rate: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    is_win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    oos_win_rate: Mapped[float] = mapped_column(Float, default=0.0)

    metrics_json: Mapped[str] = mapped_column(Text)  # full IS + OOS metric dicts
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped[OptimizationRun] = relationship(back_populates="results")


class LiveStrategy(Base):
    """A strategy promoted from the search to be traded live (Phase 2).

    Stores the frozen spec so the live runner doesn't depend on a specific
    search run still existing. Only one row is ``active`` at a time.
    """

    __tablename__ = "live_strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    result_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_results.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(32))
    interval: Mapped[str] = mapped_column(String(8))
    spec_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|disabled
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
