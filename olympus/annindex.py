"""Native approximate-nearest-neighbour index — semantic recall that scales.

Olympus's semantic retrieval today is an exact O(N) cosine scan
(`recall._semantic_hits`, `docrag.retrieve`). That is correct and cheap while a
corpus is small, but it does not scale: `DEFERRED.md` #2/#3 note that
embedding-based skill retrieval and write-time dedup only become worthwhile
"when libraries outgrow" the small-N regime. This module is that scaling path —
a pure-Python HNSW graph (Malkov & Yashunin, 2016) that answers top-k in
sublinear time, wired behind the SAME cosine seams so behaviour is identical when
the corpus is small or the feature is off.

Design constraints (the Olympus house style, mirroring `dytopo`/`emem`):

  * PURE and DETERMINISTIC — no numpy, no clock, no global rng. A node's HNSW
    level is derived from a seeded hash of its OWN id, so the same vectors always
    build the same graph and the same query always returns the same neighbours.
    We never ADD non-determinism to a retrieval path, so replay stays
    byte-identical.
  * BOUNDED — hard caps on graph degree (`_M`, `_M0`) and search breadth
    (`_EF`), so a pathological corpus cannot blow up memory or latency.
  * OPT-IN and NON-REGRESSING — `OLYMPUS_ANN` gates it, default OFF. When off,
    OR when a corpus is below `EXACT_THRESHOLD` items, `nearest()` is an exact
    brute-force scan identical to the code it replaces, so small corpora and the
    default install keep exact recall for free. The graph only ever *adds*
    candidates; it can never make a small-corpus result worse.
  * PERSISTABLE — the graph serialises to JSON bytes through `store.backend()`,
    inheriting the file/Postgres switch and the atomic-write guarantee.

The public seam every caller wants is `nearest(query, items, k)`; the `HNSW`
class is for callers that want to keep a persistent, incrementally-built index.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field

from . import store

# --- bounds (a runaway index is impossible) ------------------------------
_M = 16                 # neighbours kept per node above layer 0
_M0 = 32                # neighbours kept per node at the dense base layer
_EF = 64                # search/construction breadth (candidate frontier)
_ML = 1.0 / math.log(2.0)   # level-generation normaliser (standard HNSW mL)
_MAX_LEVEL = 16         # hard ceiling on layer count

# Below this many items an exact scan is both correct and faster than building a
# graph, so `nearest()` stays exact — no behaviour change for typical per-user
# memory sets (usermem caps at 500, and semantic recall only fires as a thin
# fallback). The graph earns its keep on large corpora (document chunks, skill
# libraries) when OLYMPUS_ANN is on.
EXACT_THRESHOLD = 256


def enabled() -> bool:
    """Whether the HNSW graph is used above `EXACT_THRESHOLD`. Default OFF: the
    exact scan below is the shipped behaviour until an operator opts in."""
    return os.environ.get("OLYMPUS_ANN", "").strip().lower() in (
        "1", "on", "true", "yes")


# --- vector helpers (pure) -----------------------------------------------

def _norm(vec) -> tuple[float, ...]:
    """Unit-normalise so cosine similarity is a plain dot product. A zero (or
    empty) vector normalises to itself and scores 0 against everything."""
    s = math.sqrt(sum(x * x for x in vec))
    if s == 0.0:
        return tuple(float(x) for x in vec)
    return tuple(float(x) / s for x in vec)


def _dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _level_for(node_id: str) -> int:
    """Deterministic HNSW level for a node, derived from a seeded hash of its own
    id (NOT a global rng). Reproduces the geometric level distribution
    `floor(-ln(u) * mL)` while guaranteeing the same id always lands on the same
    level — the property that keeps index construction replay-safe."""
    h = hashlib.sha256(b"olympus.annindex.level\x00" + node_id.encode()).digest()
    # 53 bits → a uniform u in (0, 1]; avoid exactly 0 so -ln(u) is finite.
    raw = int.from_bytes(h[:7], "big") >> (56 - 53)
    u = (raw + 1) / (1 << 53)
    lvl = int(-math.log(u) * _ML)
    return min(lvl, _MAX_LEVEL)


# --- exact fallback (identical to the cosine scans this module replaces) --

def _exact(query: tuple[float, ...], items: list[tuple[str, tuple[float, ...]]],
           k: int, min_sim: float) -> list[tuple[str, float]]:
    scored = []
    for node_id, vec in items:
        sim = _dot(query, vec)
        if sim >= min_sim:
            scored.append((node_id, sim))
    # Deterministic order: similarity desc, id asc as a stable tie-break.
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:k] if k > 0 else scored


# --- HNSW graph ----------------------------------------------------------

@dataclass
class HNSW:
    """A persistent, incrementally-built HNSW index over unit vectors.

    Deterministic by construction: insertion effects depend only on vectors and
    ids, and `_level_for` is a pure function of the id, so rebuilding from the
    same (id, vector) set yields an equivalent graph and identical query
    results. `dim` is pinned by the first insert; mismatched vectors are
    rejected (best-effort — a wrong-width vector simply never matches)."""

    dim: int = 0
    _vectors: dict = field(default_factory=dict)      # id -> tuple[float,...]
    _levels: dict = field(default_factory=dict)       # id -> int
    # _links[layer][id] -> list[id]
    _links: list = field(default_factory=list)
    _entry: str | None = None
    _top: int = -1

    # -- construction --
    def _ensure_layers(self, level: int) -> None:
        while len(self._links) <= level:
            self._links.append({})

    def add(self, node_id: str, vector) -> None:
        """Insert (or replace) a node. Replacing an id re-inserts it cleanly so
        stale links never dangle."""
        vec = _norm(vector)
        if self.dim == 0:
            self.dim = len(vec)
        if len(vec) != self.dim or self.dim == 0:
            return
        if node_id in self._vectors:
            self.remove(node_id)
        level = _level_for(node_id)
        self._ensure_layers(level)
        self._vectors[node_id] = vec
        self._levels[node_id] = level
        for lyr in range(level + 1):
            self._links[lyr].setdefault(node_id, [])

        if self._entry is None:
            self._entry, self._top = node_id, level
            return

        # Greedy descent from the current top down to just above this node's
        # level, then layer-by-layer neighbour selection below that.
        ep = self._entry
        for lyr in range(self._top, level, -1):
            ep = self._greedy(vec, ep, lyr)
        for lyr in range(min(level, self._top), -1, -1):
            cap = _M0 if lyr == 0 else _M
            cands = self._search_layer(vec, [ep], lyr, _EF)
            neighbours = [nid for _, nid in cands[:cap]]
            self._links[lyr][node_id] = neighbours
            for nid in neighbours:                     # bidirectional + prune
                lst = self._links[lyr].setdefault(nid, [])
                if node_id not in lst:
                    lst.append(node_id)
                if len(lst) > cap:
                    self._links[lyr][nid] = self._prune(nid, lst, cap)
            ep = cands[0][1] if cands else ep

        if level > self._top:
            self._entry, self._top = node_id, level

    def _prune(self, node_id: str, neighbours: list, cap: int) -> list:
        base = self._vectors[node_id]
        ranked = sorted(
            ((self._sim(base, nid), nid) for nid in neighbours),
            key=lambda t: (-t[0], t[1]))
        return [nid for _, nid in ranked[:cap]]

    # -- query --
    def _sim(self, vec: tuple[float, ...], node_id: str) -> float:
        other = self._vectors.get(node_id)
        return _dot(vec, other) if other else -1.0

    def _greedy(self, vec: tuple[float, ...], entry: str, layer: int) -> str:
        """Single-best greedy walk on one layer (used in upper layers)."""
        cur = entry
        cur_sim = self._sim(vec, cur)
        while True:
            best, best_sim = cur, cur_sim
            for nid in sorted(self._links[layer].get(cur, [])):
                sim = self._sim(vec, nid)
                if sim > best_sim or (sim == best_sim and nid < best):
                    best, best_sim = nid, sim
            if best == cur:
                return cur
            cur, cur_sim = best, best_sim

    def _search_layer(self, vec: tuple[float, ...], entries: list, layer: int,
                      ef: int) -> list:
        """Best-first ef-search on one layer. Returns [(sim, id)] sorted by
        similarity desc with id tie-break — fully deterministic (no heap-order
        ambiguity)."""
        visited = set(entries)
        frontier = [(self._sim(vec, e), e) for e in entries]
        frontier.sort(key=lambda t: (-t[0], t[1]))
        best = list(frontier)
        while frontier:
            sim, cur = frontier.pop(0)
            worst = best[-1][0] if len(best) >= ef else -2.0
            if sim < worst and len(best) >= ef:
                break
            for nid in sorted(self._links[layer].get(cur, [])):
                if nid in visited:
                    continue
                visited.add(nid)
                nsim = self._sim(vec, nid)
                best.append((nsim, nid))
                best.sort(key=lambda t: (-t[0], t[1]))
                del best[ef:]
                frontier.append((nsim, nid))
                frontier.sort(key=lambda t: (-t[0], t[1]))
        return best

    def search(self, query, k: int, ef: int | None = None,
               min_sim: float = -1.0) -> list[tuple[str, float]]:
        """Top-k (id, similarity) for a query vector, similarity desc."""
        if self._entry is None or k <= 0:
            return []
        vec = _norm(query)
        if len(vec) != self.dim:
            return []
        ep = self._entry
        for lyr in range(self._top, 0, -1):
            ep = self._greedy(vec, ep, lyr)
        found = self._search_layer(vec, [ep], 0, max(ef or _EF, k))
        out = [(nid, sim) for sim, nid in found if sim >= min_sim]
        out.sort(key=lambda t: (-t[1], t[0]))
        return out[:k]

    def remove(self, node_id: str) -> None:
        """Drop a node and every link to it. O(edges); fine at our bounds."""
        if node_id not in self._vectors:
            return
        self._vectors.pop(node_id, None)
        self._levels.pop(node_id, None)
        for layer in self._links:
            layer.pop(node_id, None)
            for lst in layer.values():
                if node_id in lst:
                    lst[:] = [n for n in lst if n != node_id]
        if self._entry == node_id:
            # Re-seat the entry point on the highest-level surviving node.
            self._entry, self._top = None, -1
            for nid, lvl in self._levels.items():
                if lvl > self._top:
                    self._entry, self._top = nid, lvl

    def __len__(self) -> int:
        return len(self._vectors)

    # -- persistence (opaque JSON bytes for store.backend()) --
    def to_bytes(self) -> bytes:
        return json.dumps({
            "v": 1, "dim": self.dim, "entry": self._entry, "top": self._top,
            "vectors": {k: list(v) for k, v in self._vectors.items()},
            "levels": self._levels,
            "links": [dict(layer) for layer in self._links],
        }, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, blob: bytes | None) -> "HNSW":
        if not blob:
            return cls()
        try:
            d = json.loads(blob)
        except (ValueError, TypeError):
            return cls()
        idx = cls(dim=int(d.get("dim", 0)))
        idx._vectors = {k: tuple(v) for k, v in d.get("vectors", {}).items()}
        idx._levels = {k: int(v) for k, v in d.get("levels", {}).items()}
        idx._links = [dict(layer) for layer in d.get("links", [])]
        idx._entry = d.get("entry")
        idx._top = int(d.get("top", -1))
        return idx


def build(items) -> HNSW:
    """Build an index from an iterable of (id, vector) or a {id: vector} dict.
    Insertion order follows id sort so the graph is reproducible regardless of
    how the caller ordered the items."""
    if isinstance(items, dict):
        items = list(items.items())
    idx = HNSW()
    for node_id, vec in sorted(items, key=lambda t: t[0]):
        idx.add(node_id, vec)
    return idx


# --- the seam every caller uses ------------------------------------------

def nearest(query, items, k: int = 8, *,
            min_sim: float = 0.0) -> list[tuple[str, float]]:
    """Top-k `(id, cosine_similarity)` for `query` over `items` (a
    `{id: vector}` dict or an iterable of `(id, vector)`), similarity desc.

    This is the drop-in replacement for an O(N) cosine scan. It stays EXACT
    (brute force, identical results) when the feature is off or the corpus is
    below `EXACT_THRESHOLD`; only a large corpus with `OLYMPUS_ANN` on pays for
    (and benefits from) the HNSW graph. Vectors are normalised internally, so
    callers pass raw embeddings."""
    if isinstance(items, dict):
        items = list(items.items())
    else:
        items = list(items)
    if not items:
        return []
    q = _norm(query)
    if not enabled() or len(items) < EXACT_THRESHOLD:
        exact = [(nid, _norm(vec)) for nid, vec in items]
        return _exact(q, exact, k, min_sim)
    idx = build(items)
    return idx.search(q, k, min_sim=min_sim)


def save_index(namespace: str, key: str, idx: HNSW) -> None:
    """Persist an index through `store.backend()` — inherits the file/Postgres
    switch and the atomic-write guarantee, so a persistent index is a config
    switch, not a rewrite."""
    store.backend().put(namespace, key, idx.to_bytes())


def load_index(namespace: str, key: str) -> HNSW:
    """Load a persisted index (an empty one if absent or corrupt)."""
    return HNSW.from_bytes(store.backend().get(namespace, key))


def dedup_match(vector, items, *, threshold: float = 0.92):
    """The nearest existing item whose similarity to `vector` meets `threshold`,
    or None. The primitive behind write-time semantic dedup (DEFERRED #2): a
    caller about to store a new memory/skill can cheaply ask "do I already know
    this?" before committing a near-duplicate."""
    hits = nearest(vector, items, k=1, min_sim=threshold)
    return hits[0] if hits else None
