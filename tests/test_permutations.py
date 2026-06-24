from sptrader.optimize.permutations import (
    StrategySpec,
    evaluate_spec,
    generate_strategies,
)
from sptrader.signals import build_votes, enumerate_instances


def test_enumerate_instances_nonempty():
    insts = enumerate_instances()
    assert len(insts) > 30
    # crossovers must satisfy fast < slow
    for inst in insts:
        p = inst.params_dict
        if "fast" in p and "slow" in p:
            assert p["fast"] < p["slow"]


def test_votes_are_discrete(ohlcv):
    for inst in enumerate_instances():
        v = build_votes(ohlcv, inst)
        assert set(v.unique()).issubset({-1.0, 0.0, 1.0}), inst.key()


# A small, structured config so these tests stay fast (the default search is the
# exhaustive dense/all-combos space).
_SMALL = {"cross_category_only": True, "dense": False}


def test_generate_strategies_unique_signatures():
    specs = generate_strategies({**_SMALL, "max_combo_size": 2, "modes": ["long_only"]})
    sigs = [s.signature() for s in specs]
    assert len(sigs) == len(set(sigs))
    assert len(specs) > 100


def test_spec_roundtrip():
    specs = generate_strategies({**_SMALL, "max_combo_size": 3, "modes": ["long_only"]})
    for spec in specs[:50]:
        d = spec.to_dict()
        back = StrategySpec.from_dict(d)
        assert back.signature() == spec.signature()


def test_evaluate_spec_positions(ohlcv):
    specs = generate_strategies({**_SMALL, "max_combo_size": 2, "modes": ["long_only"]})
    cache = {}
    pos = evaluate_spec(ohlcv, specs[0], cache)
    assert set(pos.unique()).issubset({0.0, 1.0})
    assert len(pos) == len(ohlcv)
