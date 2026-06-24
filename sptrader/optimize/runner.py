"""Resumable, parallel optimization driver.

The whole search is checkpointed in Postgres:

* One ``optimization_runs`` row identifies the search (by a config+data hash).
* Specs are generated **lazily** in a deterministic order and streamed in
  batches to a small pool of worker processes (default 2) that run the
  CPU-bound backtests in parallel.
* ``completed_combos`` is a resume **cursor**: the number of specs already
  processed in order. On resume we ``islice`` past them, so an interrupted run
  (or a stopped container) continues exactly where it left off without
  recomputing or holding millions of signatures in memory.

Selection score = ``min(in_sample_sharpe, out_of_sample_sharpe)``.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Dict, List, Optional

import pandas as pd

from ..backtest.engine import periods_per_year
from ..config import get_settings
from ..data.ingest import load_candles
from ..db import is_postgres, session_scope
from ..models import OptimizationRun, StrategyResult
from .permutations import (
    DEFAULT_CONFIG,
    compute_adx_series,
    count_strategies,
    iter_strategies,
    needs_adx,
)
from .worker import eval_batch, init_worker


def _config_hash(config: Dict[str, Any], symbol: str, interval: str, fingerprint: Dict) -> str:
    payload = {
        "config": config,
        "symbol": symbol,
        "interval": interval,
        "fingerprint": fingerprint,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _chunked(seq: List[Any], n: int) -> List[List[Any]]:
    """Split ``seq`` into ``n`` roughly equal contiguous chunks (drop empties)."""
    if n <= 1:
        return [seq]
    size = max(1, (len(seq) + n - 1) // n)
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _insert_results(session, run_id: int, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    # Drop intra-batch dedup_key collisions up front (keep first); the DB unique
    # constraint then handles collisions across batches / workers.
    seen_keys = set()
    mappings = []
    for r in rows:
        k = r.get("dedup_key")
        if k and k in seen_keys:
            continue
        seen_keys.add(k)
        mappings.append({**r, "run_id": run_id})
    if is_postgres():
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        # No conflict target -> skip on ANY unique violation: re-inserted
        # signatures AND behaviorally-identical (dedup_key) duplicates.
        stmt = pg_insert(StrategyResult).values(mappings).on_conflict_do_nothing()
        session.execute(stmt)
    else:
        session.bulk_insert_mappings(StrategyResult, mappings)


def run_optimization(
    config: Optional[Dict[str, Any]] = None,
    name: str = "default",
    restart: bool = False,
    max_combos: Optional[int] = None,
    workers: Optional[int] = None,
    batch_size: int = 2000,
    progress_every_batches: int = 1,
) -> Dict[str, Any]:
    """Run (or resume) the permutation search in parallel. Returns a summary.

    ``Ctrl-C`` is graceful: the last committed cursor stays, status stays
    ``running``, and re-running resumes from there.
    """
    from ..db import init_db

    init_db()  # idempotent: ensures tables exist (helps fresh containers)

    settings = get_settings()
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    symbol, interval = settings.symbol, settings.interval

    df = load_candles(symbol, interval)
    if df.empty:
        raise RuntimeError(
            f"No candles for {symbol} @ {interval}. Run ingestion first (`ingest`)."
        )
    if len(df) < 30:
        print(
            f"[warn] only {len(df)} bars available. Results will be statistically "
            "weak -- consider a larger LOOKBACK_DAYS (e.g. 720)."
        )

    fingerprint = {
        "n": int(len(df)),
        "start": df.index[0].isoformat(),
        "end": df.index[-1].isoformat(),
    }
    cfg_hash = _config_hash(cfg, symbol, interval, fingerprint)

    total = count_strategies(cfg)
    if max_combos is not None:
        total = min(total, max_combos)

    ppy = periods_per_year(df.index)
    adx_series = compute_adx_series(df) if needs_adx(cfg) else None
    workers = workers or int(os.getenv("WORKERS", "3"))

    # --- get or create the run; completed_combos is the resume cursor --------
    with session_scope() as session:
        run = session.query(OptimizationRun).filter_by(config_hash=cfg_hash).one_or_none()
        if run is not None and restart:
            session.delete(run)  # cascades to results
            session.flush()
            run = None
        if run is None:
            run = OptimizationRun(
                name=name,
                config_hash=cfg_hash,
                config_json=json.dumps(cfg, default=str),
                symbol=symbol,
                interval=interval,
                status="running",
                total_combos=total,
                completed_combos=0,
            )
            session.add(run)
            session.flush()
        else:
            run.total_combos = total
            run.status = "running"
        run_id = run.id
        cursor = int(run.completed_combos or 0)

    print(
        f"[run #{run_id}] {symbol}@{interval} bars={len(df)} workers={workers} "
        f"total={total:,} resume_from={cursor:,} remaining={max(total - cursor, 0):,}"
    )

    # Stream specs, skipping the already-done prefix (cursor) and any cap.
    gen = iter_strategies(cfg)
    if max_combos is not None:
        gen = itertools.islice(gen, max_combos)
    if cursor:
        gen = itertools.islice(gen, cursor, None)

    processed = cursor
    started = time.time()
    batch_no = 0
    status = "done"

    try:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=init_worker,
            initargs=(df, adx_series, ppy, settings.cost_bps, settings.train_fraction),
        ) as pool:
            while True:
                batch = list(itertools.islice(gen, batch_size))
                if not batch:
                    break
                spec_dicts = [s.to_dict() for s in batch]
                chunks = _chunked(spec_dicts, workers)
                rows: List[Dict[str, Any]] = []
                for part in pool.map(eval_batch, chunks):
                    rows.extend(part)

                with session_scope() as session:
                    _insert_results(session, run_id, rows)
                    processed += len(batch)
                    run_obj = session.get(OptimizationRun, run_id)
                    run_obj.completed_combos = processed

                batch_no += 1
                if batch_no % progress_every_batches == 0:
                    rate = (processed - cursor) / max(time.time() - started, 1e-9)
                    eta = (total - processed) / rate if rate > 0 else float("inf")
                    print(
                        f"  {processed:,}/{total:,} "
                        f"[{rate:.0f}/s, ETA {eta/60:.1f}m]"
                    )
    except KeyboardInterrupt:
        status = "interrupted"
        with session_scope() as session:
            run_obj = session.get(OptimizationRun, run_id)
            run_obj.status = "running"  # leave resumable
        print(
            f"\n[interrupted] cursor at {processed:,}. Re-run `optimize` to resume run #{run_id}."
        )
        return {"run_id": run_id, "status": status, "processed": processed, "total": total}

    with session_scope() as session:
        run_obj = session.get(OptimizationRun, run_id)
        run_obj.status = "done"
        run_obj.completed_combos = processed

    elapsed = time.time() - started
    print(f"[run #{run_id}] done. processed {processed - cursor:,} new in {elapsed:.1f}s.")
    return {"run_id": run_id, "status": status, "processed": processed, "total": total}
