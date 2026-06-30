"""The specialist benchmark is the signal Prometheus's measured-training loop
optimizes against (and the basis for `olympus scores`). Thin coverage = weak
signal, so this guards depth and well-formedness.
"""

from olympus import evals
from olympus.specialists import SPECIALISTS

MIN_PER_SPECIALIST = 5


def test_benchmarks_load_and_are_wellformed():
    items = evals.load_benchmarks()
    assert items, "benchmarks.json must not be empty"
    for b in items:
        assert set(b) >= {"id", "specialist", "task", "criteria"}
        assert b["task"].strip() and b["criteria"].strip()
        assert len(b["task"]) >= 30 and len(b["criteria"]) >= 30


def test_ids_are_unique():
    ids = [b["id"] for b in evals.load_benchmarks()]
    assert len(ids) == len(set(ids))


def test_every_specialist_key_is_real():
    for b in evals.load_benchmarks():
        assert b["specialist"] in SPECIALISTS, b["specialist"]


def test_every_user_facing_specialist_has_depth():
    cov = evals.coverage()
    thin = {s: n for s in evals.USER_FACING if (n := cov.get(s, 0)) < MIN_PER_SPECIALIST}
    assert not thin, f"specialists with too few benchmark items: {thin}"
