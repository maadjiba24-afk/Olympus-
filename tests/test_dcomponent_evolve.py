"""Self-evolution wiring for the integration-depth components (D1-D8): each has
a registered, non-security, hard-bounded tunable that its I/O boundary reads."""

import pytest

from olympus import evolve


_TUNABLES = {
    "treesearch.max_nodes": (10, 100, 50),
    "dytopo.max_out_degree": (1, 3, 2),
    "emem.max_fragments": (4, 12, 12),
    "liveeval.sample_size": (10, 50, 20),
    "scaffold_evolve.max_archive": (50, 200, 200),
}


def test_tunables_registered_with_bounds():
    for key, (lo, hi, default) in _TUNABLES.items():
        t = evolve._TUNABLES.get(key)
        assert t is not None, f"{key} not registered"
        assert (t.lo, t.hi, t.default) == (lo, hi, default)
        assert t.tighten_only is False        # non-security → freely tunable


def test_current_returns_default_within_bounds():
    for key, (lo, hi, default) in _TUNABLES.items():
        feature, name = key.split(".")
        val = evolve.current(feature, name)
        assert lo <= val <= hi and val == default


def test_helpers_read_the_tunable():
    from olympus import treesearch, dytopo, emem, liveeval, scaffold_evolve
    assert treesearch._tuned_node_default() == 50
    assert dytopo._tuned_out_degree() == 2
    assert emem._tuned_max_fragments() == 12
    assert liveeval._tuned_sample() == 20
    assert scaffold_evolve._archive_cap() == 200


def test_helpers_fall_back_safely(monkeypatch):
    # a broken evolve.current must not crash the feature — it falls back
    from olympus import treesearch, dytopo, emem, liveeval, scaffold_evolve

    def boom(*a, **k):
        raise KeyError("missing")
    monkeypatch.setattr(evolve, "current", boom)
    assert treesearch._tuned_node_default() == 50
    assert dytopo._tuned_out_degree() == 2
    assert emem._tuned_max_fragments() == emem._MAX_FRAGMENTS
    assert liveeval._tuned_sample() == liveeval._DEFAULT_SAMPLE
    assert scaffold_evolve._archive_cap() == scaffold_evolve._MAX_ARCHIVE


def test_no_new_security_tunables():
    # none of the D-component tunables may be tighten_only-security knobs; the
    # security-relevant ones (operator.*) remain the only tighten_only entries.
    for key in _TUNABLES:
        assert evolve._TUNABLES[key].tighten_only is False
