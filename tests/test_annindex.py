"""Native ANN/HNSW index — correctness, determinism, non-regression, scaling."""

import math
import random

from olympus import annindex


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _rand_vectors(n, dim, seed=0):
    rng = random.Random(seed)
    return {f"v{i:04d}": [rng.gauss(0, 1) for _ in range(dim)] for i in range(n)}


# --- gating -------------------------------------------------------------

def test_disabled_by_default():
    assert annindex.enabled() is False


def test_enabled_via_env(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ANN", "on")
    assert annindex.enabled() is True
    monkeypatch.setenv("OLYMPUS_ANN", "0")
    assert annindex.enabled() is False


# --- level assignment is deterministic + bounded ------------------------

def test_level_deterministic_and_bounded():
    lv = [annindex._level_for(f"id{i}") for i in range(200)]
    assert lv == [annindex._level_for(f"id{i}") for i in range(200)]  # stable
    assert all(0 <= x <= annindex._MAX_LEVEL for x in lv)
    assert max(lv) >= 1                      # the geometric tail produces layers


# --- exact mode (default / small corpus) matches brute force ------------

def test_nearest_exact_matches_brute_force():
    items = _rand_vectors(40, 12, seed=1)
    query = items["v0007"]
    got = annindex.nearest(query, items, k=5)
    brute = sorted(((_cos(query, v), i) for i, v in items.items()),
                   key=lambda t: (-t[0], t[1]))[:5]
    assert [i for i, _ in got] == [i for _, i in brute]
    assert got[0][1] == 1.0                  # identical vector → cosine 1
    # similarities descending
    assert all(got[j][1] >= got[j + 1][1] for j in range(len(got) - 1))


def test_nearest_min_sim_filters():
    items = {"a": [1, 0], "b": [0, 1], "c": [1, 1]}
    got = annindex.nearest([1, 0], items, k=10, min_sim=0.9)
    assert [i for i, _ in got] == ["a"]      # only the exact axis clears 0.9


def test_nearest_empty():
    assert annindex.nearest([1, 0], {}, k=5) == []
    assert annindex.nearest([1, 0], [], k=5) == []


def test_nearest_deterministic_order_on_ties():
    # Two vectors equidistant from the query — id tie-break must be stable.
    items = {"z": [1, 0], "a": [1, 0], "m": [0, 1]}
    got = annindex.nearest([1, 0], items, k=2)
    assert [i for i, _ in got] == ["a", "z"]     # ids ascending on equal sim


# --- HNSW mode (large corpus + opt-in) is high-recall + deterministic ---

def test_hnsw_finds_planted_neighbour(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ANN", "1")
    items = _rand_vectors(400, 16, seed=2)   # > EXACT_THRESHOLD → graph engages
    assert len(items) >= annindex.EXACT_THRESHOLD
    query = list(items["v0123"])             # identical to a planted vector
    got = annindex.nearest(query, items, k=3)
    assert got, "graph returned no neighbours"
    assert got[0][1] > 0.999                 # the planted vector is found first
    assert got[0][1] == max(s for _, s in got)


def test_hnsw_near_optimal_vs_exact(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ANN", "1")
    items = _rand_vectors(350, 16, seed=3)
    query = [random.Random(99).gauss(0, 1) for _ in range(16)]
    ann_top = annindex.nearest(query, items, k=1)[0]
    exact_best = max(_cos(query, v) for v in items.values())
    # Approximate, but with ef=64 on this scale it should be at/near optimal.
    assert ann_top[1] >= exact_best - 0.05


def test_hnsw_build_is_deterministic(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ANN", "1")
    items = _rand_vectors(300, 10, seed=4)
    query = items["v0050"]
    a = annindex.nearest(query, items, k=8)
    b = annindex.nearest(query, items, k=8)
    assert a == b


# --- persistence roundtrip ----------------------------------------------

def test_persistence_roundtrip():
    items = _rand_vectors(300, 8, seed=5)
    idx = annindex.build(items)
    reloaded = annindex.HNSW.from_bytes(idx.to_bytes())
    query = items["v0100"]
    assert idx.search(query, k=5) == reloaded.search(query, k=5)
    assert len(reloaded) == len(items)


def test_from_bytes_tolerates_garbage():
    assert len(annindex.HNSW.from_bytes(None)) == 0
    assert len(annindex.HNSW.from_bytes(b"not json")) == 0


# --- incremental graph ops ----------------------------------------------

def test_add_replace_and_remove():
    idx = annindex.HNSW()
    for i, v in _rand_vectors(30, 6, seed=6).items():
        idx.add(i, v)
    assert len(idx) == 30
    idx.remove("v0000")
    assert len(idx) == 29
    assert all(i != "v0000" for i, _ in idx.search([1, 0, 0, 0, 0, 0], k=29))
    # Replacing an id keeps the count stable and re-links cleanly.
    idx.add("v0001", [9, 9, 9, 9, 9, 9])
    assert len(idx) == 29


def test_build_from_dict_and_list_equivalent():
    items = _rand_vectors(20, 5, seed=7)
    q = items["v0003"]
    a = annindex.build(items).search(q, k=5)
    b = annindex.build(list(items.items())).search(q, k=5)
    assert a == b


# --- dedup primitive (DEFERRED #2) --------------------------------------

def test_dedup_match():
    items = {"known": [1, 0, 0], "other": [0, 1, 0]}
    near = annindex.dedup_match([0.99, 0.01, 0.0], items, threshold=0.9)
    assert near is not None and near[0] == "known"
    far = annindex.dedup_match([0.5, 0.5, 0.7], items, threshold=0.95)
    assert far is None
