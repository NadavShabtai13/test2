"""Strategy permutation generation.

We follow the research consensus: prefer *cross-category* combinations (trend +
momentum + volatility/volume) rather than stacking redundant same-category
indicators. The search space is therefore:

    size 1: every individual signal instance
    size 2: every cross-category pair
    size 3: trend x momentum x (volatility | volume)

Each base combination is expanded over direction modes (long-only / long-short)
and optional ADX trend-strength filters. The whole space is bounded and every
spec has a stable ``signature`` used as the resume key.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd

from ..indicators import library as ta
from ..signals import (
    CATEGORIES,
    SignalInstance,
    build_votes,
    combine_positions,
    enumerate_instances,
)

# The default search: dense parameter grids, both AND/OR vote logic and both
# direction modes, up to 4 indicators per strategy -- but only *cross-category*
# combinations (distinct categories, one instance each: trend + momentum +
# volatility + volume). Stacking same-category indicators is redundant
# (multicollinearity) and inflates the search / overfitting risk, so it is
# excluded by default. `--max-combo-size` tunes the combo size; `--full` is kept
# as a (redundant) alias of this default.
DEFAULT_CONFIG: Dict[str, Any] = {
    "categories": list(CATEGORIES),
    "max_combo_size": 4,
    "modes": ["long_only", "long_short"],
    "adx_filters": [None],  # e.g. [None, 25] to also try ADX-gated variants
    "combines": ["and", "or"],
    "dense": True,
    "cross_category_only": True,
}


@dataclass(frozen=True)
class StrategySpec:
    instances: Tuple[SignalInstance, ...]
    mode: str
    adx_min: Optional[float] = None
    combine: str = "and"

    def signature(self) -> str:
        keys = "|".join(sorted(i.key() for i in self.instances))
        adx = f"adx{self.adx_min}" if self.adx_min is not None else "adxNone"
        return f"{keys}#mode={self.mode}#{adx}#{self.combine}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instances": [
                {"factory": i.factory, "params": i.params_dict} for i in self.instances
            ],
            "mode": self.mode,
            "adx_min": self.adx_min,
            "combine": self.combine,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "StrategySpec":
        insts = tuple(
            SignalInstance(i["factory"], tuple(sorted(i["params"].items())))
            for i in d["instances"]
        )
        return StrategySpec(insts, d["mode"], d.get("adx_min"), d.get("combine", "and"))


def _by_category(instances: List[SignalInstance]) -> Dict[str, List[SignalInstance]]:
    out: Dict[str, List[SignalInstance]] = {c: [] for c in CATEGORIES}
    for inst in instances:
        out[inst.category].append(inst)
    return out


def _base_combinations(
    config: Dict[str, Any]
) -> Iterator[Tuple[SignalInstance, ...]]:
    """Yield base instance combinations lazily (never materialized).

    Streaming matters: dense size-4 cross-category combos number in the tens of
    millions, so building a list would blow the 2GB cap. Callers iterate.
    """
    categories = config.get("categories", list(CATEGORIES))
    max_size = int(config.get("max_combo_size", 3))
    dense = bool(config.get("dense", False))
    cross_only = bool(config.get("cross_category_only", True))
    instances = enumerate_instances(categories=tuple(categories), dense=dense)

    if not cross_only:
        # Exhaustive: every combination of every instance, all sizes 1..max_size
        # (this includes same-category combos and is the largest search space).
        for k in range(1, max_size + 1):
            yield from itertools.combinations(instances, k)
        return

    grouped = _by_category(instances)
    active = [c for c in CATEGORIES if c in categories and grouped[c]]

    # Cross-category combos of size k: pick k *distinct* categories and one
    # instance from each. This keeps strategies non-redundant (the research
    # consensus) and generalizes cleanly to any size up to the number of
    # categories -- so max_size=4 yields one trend + momentum + volatility +
    # volume signal. Beyond that there are no more categories to add, so larger
    # sizes need the exhaustive (all-combos) path instead.
    for k in range(1, max_size + 1):
        if k == 1:
            for inst in instances:
                yield (inst,)
            continue
        if k > len(active):
            break
        for cat_combo in itertools.combinations(active, k):
            pools = [grouped[c] for c in cat_combo]
            yield from itertools.product(*pools)


def iter_strategies(config: Dict[str, Any] | None = None) -> Iterator[StrategySpec]:
    """Yield every StrategySpec implied by ``config`` lazily (deterministic order).

    Streaming matters: the exhaustive ``--full`` space is millions of specs, so
    we must never materialize them all in memory (keeps the 2GB cap happy).
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    modes = cfg.get("modes", ["long_only"])
    adx_filters = cfg.get("adx_filters", [None])
    # Back-compat: accept a single "combine" or a list "combines".
    combines = cfg.get("combines") or [cfg.get("combine", "and")]
    cross_only = bool(cfg.get("cross_category_only", True))

    if cross_only:
        # _base_combinations yields unique instance sets (distinct categories,
        # one instance each), so the (set, mode, adx, combine) tuples are already
        # unique -> no seen set needed, which keeps memory flat on dense grids.
        for instances in _base_combinations(cfg):
            combine_opts = ["and"] if len(instances) == 1 else combines
            for mode in modes:
                for adx_min in adx_filters:
                    for combine in combine_opts:
                        yield StrategySpec(instances, mode, adx_min, combine)
        return

    # Exhaustive: itertools.combinations yields unique instance sets, so the
    # (set, mode, adx, combine) tuples are already unique -> no seen set needed
    # (which is what keeps memory flat across millions of specs).
    dense = bool(cfg.get("dense", False))
    categories = cfg.get("categories", list(CATEGORIES))
    max_size = int(cfg.get("max_combo_size", 3))
    instances = enumerate_instances(categories=tuple(categories), dense=dense)
    for k in range(1, max_size + 1):
        combine_opts = ["and"] if k == 1 else combines
        for combo in itertools.combinations(instances, k):
            for mode in modes:
                for adx_min in adx_filters:
                    for combine in combine_opts:
                        yield StrategySpec(combo, mode, adx_min, combine)


def generate_strategies(config: Dict[str, Any] | None = None) -> List[StrategySpec]:
    """Materialize every StrategySpec implied by ``config`` (deterministic order).

    Convenience wrapper around :func:`iter_strategies`. Avoid on huge/exhaustive
    configs -- use :func:`iter_strategies` + :func:`count_strategies` instead.
    """
    return list(iter_strategies(config))


def count_strategies(config: Dict[str, Any] | None = None) -> int:
    """Exact strategy count without materializing the specs.

    Uses a closed-form for the exhaustive space (combinatorial) and a cheap
    iteration for the smaller cross-category space.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    cross_only = bool(cfg.get("cross_category_only", True))
    n_modes = len(cfg.get("modes", ["long_only"]))
    n_adx = len(cfg.get("adx_filters", [None]))
    combines = cfg.get("combines") or [cfg.get("combine", "and")]
    n_comb = len(combines)

    if cross_only:
        return sum(1 for _ in iter_strategies(cfg))

    dense = bool(cfg.get("dense", False))
    categories = cfg.get("categories", list(CATEGORIES))
    max_size = int(cfg.get("max_combo_size", 3))
    n = len(enumerate_instances(categories=tuple(categories), dense=dense))
    total = 0
    for k in range(1, max_size + 1):
        per = n_modes * n_adx * (1 if k == 1 else n_comb)
        total += math.comb(n, k) * per
    return total


def evaluate_spec(
    df: pd.DataFrame,
    spec: StrategySpec,
    cache: Dict[str, pd.Series],
    adx_series: pd.Series | None = None,
) -> pd.Series:
    """Compute the target-position series for ``spec`` over the full frame.

    ``cache`` memoizes per-instance vote series so they are computed once per run.
    """
    vote_cols = []
    for inst in spec.instances:
        key = inst.key()
        series = cache.get(key)
        if series is None:
            series = build_votes(df, inst)
            cache[key] = series
        vote_cols.append(series.rename(key))
    vote_frame = pd.concat(vote_cols, axis=1)
    return combine_positions(vote_frame, spec.mode, adx_series, spec.adx_min, spec.combine)


def needs_adx(config: Dict[str, Any]) -> bool:
    return any(a is not None for a in config.get("adx_filters", [None]))


def compute_adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return ta.adx(df["high"], df["low"], df["close"], period)["adx"]
