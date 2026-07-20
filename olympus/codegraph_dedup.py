"""Near-duplicate node merging — one concept, one node.

Code symbols never need this (their identity is exact: sha(path::qual)), but
prose-labeled nodes do: two docs titled "Threat model" and "The threat model",
a docstring rationale repeated across five overloads. Left unmerged they
fragment the graph — five weak nodes where one hub should be.

Graphify's dedup pipeline, absorbed in pure Python and right-sized for the
node counts a project graph actually has:

  candidate blocking   MinHash over token shingles (own 64-permutation
                       implementation, ~30 lines — no datasketch)
  verification         Jaro-Winkler similarity over the full labels
  merge                union-find; the survivor is the highest-degree member,
                       every edge is rewritten onto it, losers are dropped

Only RATIONALE / DOCUMENT / ENTITY nodes participate, and only within the same
kind — a doc never merges into a comment. Deterministic: fixed hash seeds,
sorted iteration, no RNG at merge time.
"""

from __future__ import annotations

import hashlib
import struct
import zlib

from . import codegraph

_PERMS = 64
_SHINGLE = 3
_MINHASH_BAND = 8           # bands of 8 rows -> candidates share >=1 identical band
_JW_THRESHOLD = 0.90
_DEDUP_KINDS = (codegraph.RATIONALE, codegraph.DOCUMENT, codegraph.ENTITY)

# 64 fixed 32-bit hash seeds (sha256 of the index — stable across runs/machines)
_SEEDS = [struct.unpack("<I", hashlib.sha256(str(i).encode()).digest()[:4])[0]
          for i in range(_PERMS)]
_MOD = (1 << 32) - 5        # large prime < 2^32


def _stable32(s: str) -> int:
    return zlib.crc32(s.encode("utf-8"))     # process-independent, unlike hash()


def _shingles(text: str) -> set[int]:
    """Token trigrams PLUS unigrams. Unigrams keep recall on short labels
    (two 3-word titles differing in one word share no trigram at all);
    Jaro-Winkler verification downstream keeps the precision."""
    toks = "".join(c if c.isalnum() else " " for c in text.lower()).split()
    out = {_stable32(t) for t in toks}
    out |= {_stable32(" ".join(toks[i:i + _SHINGLE]))
            for i in range(max(0, len(toks) - _SHINGLE + 1))}
    return out


def _minhash(shingles: set[int]) -> tuple[int, ...]:
    if not shingles:
        return (0,) * _PERMS
    return tuple(min((seed * s + 1) % _MOD for s in shingles)
                 for seed in _SEEDS)


def jaro_winkler(a: str, b: str) -> float:
    """Plain Jaro-Winkler (prefix boost 0.1, max prefix 4). Pure Python."""
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    window = max(la, lb) // 2 - 1
    match_a, match_b = [False] * la, [False] * lb
    matches = 0
    for i, ca in enumerate(a):
        lo, hi = max(0, i - window), min(lb, i + window + 1)
        for j in range(lo, hi):
            if not match_b[j] and b[j] == ca:
                match_a[i] = match_b[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    trans = 0
    k = 0
    for i in range(la):
        if match_a[i]:
            while not match_b[k]:
                k += 1
            if a[i] != b[k]:
                trans += 1
            k += 1
    jaro = (matches / la + matches / lb
            + (matches - trans / 2) / matches) / 3
    prefix = 0
    for ca, cb in zip(a[:4], b[:4]):
        if ca != cb:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic: smaller id becomes the root
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def find_duplicates(project: str) -> list[list[dict]]:
    """Groups of near-duplicate nodes (same kind, banded MinHash candidate,
    Jaro-Winkler >= threshold). Each group is sorted highest-degree first."""
    nodes_ = [n for n in codegraph.nodes(project)
              if n["kind"] in _DEDUP_KINDS and n["label"]]
    if len(nodes_) < 2:
        return []
    by_id = {n["id"]: n for n in nodes_}
    uf = _UnionFind()

    buckets: dict[tuple, list[str]] = {}
    for n in sorted(nodes_, key=lambda x: x["id"]):
        sig = _minhash(_shingles(n["label"]))
        for band in range(0, _PERMS, _MINHASH_BAND):
            key = (n["kind"], band, sig[band:band + _MINHASH_BAND])
            buckets.setdefault(key, []).append(n["id"])
        # Token blocking backs the bands up: banded MinHash needs high Jaccard,
        # which two 3-word titles differing by one word never reach. Any shared
        # token makes a pair a candidate; the cap keeps 'the' from exploding.
        toks = "".join(c if c.isalnum() else " "
                       for c in n["label"].lower()).split()
        for t in set(toks):
            buckets.setdefault((n["kind"], "tok", t), []).append(n["id"])

    _MAX_BUCKET = 50
    checked: set[tuple[str, str]] = set()
    for ids in buckets.values():
        if len(ids) < 2 or len(ids) > _MAX_BUCKET:
            continue
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                pair = (a, b) if a < b else (b, a)
                if pair in checked:
                    continue
                checked.add(pair)
                la = by_id[a]["label"].lower()
                lb = by_id[b]["label"].lower()
                if jaro_winkler(la, lb) >= _JW_THRESHOLD:
                    uf.union(a, b)

    groups: dict[str, list[dict]] = {}
    for n in nodes_:
        groups.setdefault(uf.find(n["id"]), []).append(n)
    degree = codegraph.degree(project)
    out = []
    for members in groups.values():
        if len(members) > 1:
            members.sort(key=lambda n: (-degree.get(n["id"], 0), n["id"]))
            out.append(members)
    return out


def merge_duplicates(project: str) -> dict:
    """Rewrite every duplicate group onto its highest-degree survivor. Edge
    rewrites de-duplicate naturally (add_edge's (src, rel, dst) key); dangling
    duplicates and their old edges are dropped in one save."""
    groups = find_duplicates(project)
    if not groups:
        return {"groups": 0, "merged": 0}
    remap: dict[str, str] = {}
    for members in groups:
        survivor = members[0]["id"]
        for loser in members[1:]:
            remap[loser["id"]] = survivor

    with codegraph._LOCK:
        nodes_ = codegraph._load(codegraph._NODES, project)
        edges_ = codegraph._load(codegraph._EDGES, project)
        nodes_ = [n for n in nodes_ if n["id"] not in remap]
        seen: set[tuple] = set()
        new_edges = []
        for e in edges_:
            src = remap.get(e["src"], e["src"])
            dst = remap.get(e["dst"], e["dst"])
            if src == dst:
                continue
            key = (src, e["rel"], dst)
            if key in seen:
                continue
            seen.add(key)
            # Regenerate the id from the rewritten endpoints so it stays
            # sha(src+rel+dst) — the invariant subgraph_query's edge dedup and
            # add_edge's identity both rely on.
            eid = hashlib.sha256(
                f"{src}{e['rel']}{dst}".encode()).hexdigest()[:12]
            new_edges.append({**e, "id": eid, "src": src, "dst": dst})
        codegraph._save(codegraph._NODES, project, nodes_)
        codegraph._save(codegraph._EDGES, project, new_edges)
    return {"groups": len(groups), "merged": len(remap)}
