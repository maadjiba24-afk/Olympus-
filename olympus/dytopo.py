"""DyTopo — dynamic topology routing for the specialist council.

Olympus's council runs a FIXED pipeline (Zeus → Athena → specialists →
Aletheia). DyTopo (arXiv 2602.06039) adds an OPTIONAL, runtime-induced
*collaboration graph*: each specialist emits a natural-language `query` (what it
needs from peers) and an `offer` (what it can provide); those descriptors are
matched to induce a sparse directed graph, so on a given task the specialists
that genuinely have something for each other are wired together for a bounded
number of consultation rounds — instead of an all-to-all or fixed shape.

This module is the PURE core: descriptor matching → graph → rounds. It is
deterministic (token-overlap similarity, stable tie-breaks — no embeddings, no
clock, no rng), bounded (hard caps on nodes, out-degree, edges, rounds), and
opt-in (`OLYMPUS_DYTOPO`, off by default). Being pure and deterministic keeps it
replay-safe: the same descriptors always induce the same topology.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset((
    "the", "and", "for", "are", "with", "that", "this", "from", "into", "need",
    "needs", "provide", "provides", "offer", "offers", "want", "help", "about",
    "when", "what", "who", "how", "any", "all", "can", "will", "a", "an", "to",
    "of", "in", "on", "or", "is", "it", "i", "my", "me", "we", "you", "your",
))

# Hard caps — a runaway topology (huge council, dense graph) is impossible.
_MAX_NODES = 32
_MAX_OUT_DEGREE = 3
_MAX_EDGES = 96
_MAX_ROUNDS = 5
_DEFAULT_THRESHOLD = 0.08


def enabled() -> bool:
    """Whether dynamic topology routing is used (default OFF — the fixed DAG
    stands unless an operator opts in)."""
    return os.environ.get("OLYMPUS_DYTOPO", "").strip().lower() in (
        "1", "on", "true", "yes")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(str(text).lower())
                     if len(w) > 2 and w not in _STOP)


def _similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)      # Jaccard — deterministic


@dataclass(frozen=True)
class Descriptor:
    """A specialist's routing descriptor: what it needs (`query`) and what it can
    provide (`offer`), in natural language."""
    specialist: str
    query: str = ""
    offer: str = ""


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    score: float


@dataclass
class Topology:
    nodes: tuple[str, ...]
    edges: tuple[Edge, ...]

    def neighbors(self, node: str) -> list[str]:
        """Nodes `node` consults (its out-edges), best first."""
        outs = sorted((e for e in self.edges if e.src == node),
                      key=lambda e: (-e.score, e.dst))
        return [e.dst for e in outs]

    def as_dict(self) -> dict:
        return {"nodes": list(self.nodes),
                "edges": [{"src": e.src, "dst": e.dst, "score": round(e.score, 4)}
                          for e in self.edges]}

    def is_sparse(self) -> bool:
        """A well-formed DyTopo graph is sparse: at most O(n·k) edges."""
        return len(self.edges) <= len(self.nodes) * _MAX_OUT_DEGREE


def induce(descriptors, *, max_out_degree: int = 2,
           threshold: float = _DEFAULT_THRESHOLD) -> Topology:
    """Induce a sparse directed collaboration graph from routing descriptors.
    An edge A→B means A's `query` matches B's `offer` (A should consult B).

    Deterministic: candidate edges are scored by token-overlap and, per source,
    the top-`max_out_degree` above `threshold` are kept, ties broken by
    destination name. Self-edges are never created. All bounds are hard-clamped.
    """
    k = max(1, min(int(max_out_degree), _MAX_OUT_DEGREE))
    thr = max(0.0, min(float(threshold), 1.0))
    # de-dupe by specialist, cap node count, stable order
    seen: set[str] = set()
    descs: list[Descriptor] = []
    for d in descriptors or []:
        name = getattr(d, "specialist", None)
        if not name or name in seen:
            continue
        seen.add(name)
        descs.append(d)
        if len(descs) >= _MAX_NODES:
            break
    descs.sort(key=lambda d: d.specialist)
    nodes = tuple(d.specialist for d in descs)

    q_tokens = {d.specialist: _tokens(d.query) for d in descs}
    o_tokens = {d.specialist: _tokens(d.offer) for d in descs}

    edges: list[Edge] = []
    for src in nodes:
        scored = []
        for dst in nodes:
            if dst == src:
                continue
            s = _similarity(q_tokens[src], o_tokens[dst])
            if s >= thr and s > 0.0:
                scored.append((s, dst))
        scored.sort(key=lambda t: (-t[0], t[1]))       # score desc, name asc
        for s, dst in scored[:k]:
            edges.append(Edge(src=src, dst=dst, score=s))
            if len(edges) >= _MAX_EDGES:
                break
        if len(edges) >= _MAX_EDGES:
            break
    return Topology(nodes=nodes, edges=tuple(edges))


def rounds(topology: Topology, max_rounds: int = 3) -> list[list[tuple[str, str]]]:
    """Break the graph into bounded consultation rounds. Round r contains, for
    every node, its (r+1)-th best out-edge as a (consulter, consulted) pair —
    so a node consults its strongest match first, then the next, etc. Deterministic
    and capped at `_MAX_ROUNDS`. A node with fewer edges simply stops early."""
    r_cap = max(1, min(int(max_rounds), _MAX_ROUNDS))
    per_node = {n: topology.neighbors(n) for n in topology.nodes}
    out: list[list[tuple[str, str]]] = []
    for r in range(r_cap):
        this_round = []
        for n in topology.nodes:
            nbrs = per_node[n]
            if r < len(nbrs):
                this_round.append((n, nbrs[r]))
        if not this_round:
            break
        out.append(this_round)
    return out


@dataclass
class RouteResult:
    topology: Topology
    rounds: list = field(default_factory=list)

    def summary(self) -> dict:
        return {"nodes": len(self.topology.nodes),
                "edges": len(self.topology.edges),
                "rounds": len(self.rounds),
                "sparse": self.topology.is_sparse()}


def route(descriptors, *, max_out_degree: int = 2, max_rounds: int = 3,
          threshold: float = _DEFAULT_THRESHOLD) -> RouteResult:
    """Induce the topology and its consultation rounds in one call."""
    topo = induce(descriptors, max_out_degree=max_out_degree, threshold=threshold)
    return RouteResult(topology=topo, rounds=rounds(topo, max_rounds=max_rounds))
