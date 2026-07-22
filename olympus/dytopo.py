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


def _tuned_out_degree() -> int:
    """Self-tuned default out-degree (narrowed within [1,3] by the evolve
    reviewer when routing degrades). Falls back to 2 on any read error."""
    try:
        from . import evolve
        return int(evolve.current("dytopo", "max_out_degree"))
    except (KeyError, ValueError, TypeError, ImportError, OSError):
        return 2


def route(descriptors, *, max_out_degree: int | None = None, max_rounds: int = 3,
          threshold: float = _DEFAULT_THRESHOLD) -> RouteResult:
    """Induce the topology and its consultation rounds in one call. When
    `max_out_degree` is None it takes the self-tuned default."""
    k = _tuned_out_degree() if max_out_degree is None else max_out_degree
    topo = induce(descriptors, max_out_degree=k, threshold=threshold)
    res = RouteResult(topology=topo, rounds=rounds(topo, max_rounds=max_rounds))
    try:
        from . import evolve
        evolve.log_event("dytopo", "route", {
            "nodes": len(topo.nodes), "edges": len(topo.edges),
            "rounds": len(res.rounds), "sparse": topo.is_sparse()})
        evolve.record("dytopo",
                      evolve.OK if topo.is_sparse() else evolve.DEGRADED,
                      f"nodes={len(topo.nodes)} edges={len(topo.edges)}")
    except Exception:
        pass
    return res


# --- explicit named topologies (the swarm-shape vocabulary) --------------
#
# `induce` above builds the topology *dynamically* from what each specialist
# says it needs/offers. Sometimes you instead want a *fixed* collaboration
# shape — every specialist cross-checks every other (mesh), workers report to a
# coordinator (star), or a review chain (hierarchical). These constructors emit
# the SAME `Topology`/`Edge` type, so `rounds()`, `RouteResult`, and the
# consultation driver below treat an explicit shape and an induced one
# identically. All are pure, deterministic, and clamped to the same hard caps.

def _clean_nodes(nodes) -> tuple[str, ...]:
    """De-dupe, drop falsy, sort for a stable order, cap at `_MAX_NODES`."""
    seen: set[str] = set()
    out: list[str] = []
    for n in nodes or []:
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(str(n))
    out.sort()
    return tuple(out[:_MAX_NODES])


def _capped(edges: list[Edge]) -> tuple[Edge, ...]:
    return tuple(edges[:_MAX_EDGES])


def mesh(nodes) -> Topology:
    """All-to-all: every specialist consults every other. Dense by design, but
    still hard-capped at `_MAX_EDGES` — and the cap is applied FAIRLY: each
    node's out-degree is bounded to `_MAX_EDGES // n` so a large council degrades
    to an evenly-thinned mesh where every node keeps some peers, rather than a
    lopsided one where the first few sorted nodes consume the whole budget."""
    ns = _clean_nodes(nodes)
    n = len(ns)
    per_source = max(1, _MAX_EDGES // n) if n else _MAX_EDGES
    edges: list[Edge] = []
    for a in ns:
        kept = 0
        for b in ns:
            if a == b:
                continue
            edges.append(Edge(src=a, dst=b, score=1.0))
            kept += 1
            if kept >= per_source:
                break
    return Topology(nodes=ns, edges=_capped(edges))


def star(hub: str, nodes, *, mode: str = "in") -> Topology:
    """Coordinator shape. `mode="in"` (default): every worker consults the hub
    (workers→hub, the centralized/queen pattern). `"out"`: the hub consults every
    worker (hub→workers). `"both"`: bidirectional. The hub is always a node even
    if absent from `nodes`."""
    ns = list(_clean_nodes(list(nodes) + ([hub] if hub else [])))
    edges: list[Edge] = []
    for n in ns:
        if n == hub or not hub:
            continue
        if mode in ("in", "both"):
            edges.append(Edge(src=n, dst=hub, score=1.0))
        if mode in ("out", "both"):
            edges.append(Edge(src=hub, dst=n, score=1.0))
    edges.sort(key=lambda e: (e.src, e.dst))
    return Topology(nodes=tuple(ns), edges=_capped(edges))


def hierarchical(layers) -> Topology:
    """A review tree: `layers` is an ordered list of node-lists, top to bottom;
    every node in layer i consults every node in layer i+1 (parent→child). Models
    a plan→implement→review escalation. Clamped like the rest."""
    clean_layers = [list(_clean_nodes(layer)) for layer in (layers or [])]
    all_nodes = _clean_nodes([n for layer in clean_layers for n in layer])
    edges: list[Edge] = []
    for i in range(len(clean_layers) - 1):
        for parent in clean_layers[i]:
            for child in clean_layers[i + 1]:
                if parent != child:
                    edges.append(Edge(src=parent, dst=child, score=1.0))
    edges.sort(key=lambda e: (e.src, e.dst))
    return Topology(nodes=all_nodes, edges=_capped(edges))


def ring(nodes) -> Topology:
    """Each specialist consults the next, closing the loop — a cheap
    round-robin cross-check (n edges for n nodes)."""
    ns = _clean_nodes(nodes)
    if len(ns) < 2:
        return Topology(nodes=ns, edges=())
    edges = [Edge(src=ns[i], dst=ns[(i + 1) % len(ns)], score=1.0)
             for i in range(len(ns))]
    return Topology(nodes=ns, edges=_capped(edges))


_NAMED = {"mesh": mesh, "ring": ring}


def named_topology(kind: str, nodes, *, hub: str | None = None,
                   layers=None) -> Topology:
    """Dispatch a topology by name over a flat node list. `star` needs `hub`;
    `hierarchical` needs `layers`; `mesh`/`ring` need only `nodes`. Unknown
    kinds fall back to `mesh` (the safest cross-check shape)."""
    kind = (kind or "").strip().lower()
    if kind == "star":
        return star(hub or (sorted(_clean_nodes(nodes))[:1] or [""])[0], nodes)
    if kind == "hierarchical":
        return hierarchical(layers or [list(_clean_nodes(nodes))])
    return _NAMED.get(kind, mesh)(nodes)


# --- swarm consultation driver (dependency-injected → testable) ----------

def swarm_enabled() -> bool:
    """Whether a topology-driven consultation pass runs after the DAG dispatch.
    Default OFF: when unset the council pipeline is byte-identical to today."""
    return os.environ.get("OLYMPUS_SWARM", "").strip().lower() in (
        "1", "on", "true", "yes")


def swarm_topology_kind() -> str:
    """Which explicit shape the consultation pass uses (`OLYMPUS_SWARM_TOPOLOGY`,
    default `star` — the cheapest useful cross-check)."""
    return (os.environ.get("OLYMPUS_SWARM_TOPOLOGY", "star").strip().lower()
            or "star")


def run_consultation(topology: Topology, outputs, runner, *,
                     max_rounds: int = 1) -> list[tuple[str, str]]:
    """Refine first-pass specialist outputs by letting each consulter revisit its
    answer in light of the peers it consults.

    `outputs` is the DAG's `[(specialist_key, text), ...]`. `runner(consulter,
    peer_context, own_output) -> str` is INJECTED — the orchestrator passes a
    real specialist runner; tests pass a deterministic fake — so this scheduler
    stays pure and unit-testable with no model. Returns outputs in the SAME shape
    and order; a runner that raises or returns falsy leaves that output
    untouched. Bounded by `rounds()` (≤ `_MAX_ROUNDS`) and the topology caps, so
    the number of extra model calls is provably limited.

    Duplicate specialist keys are preserved: Athena can assign one specialist to
    two DAG steps, so `outputs` may carry the same key twice with different text.
    Refinement is POSITIONAL (each slot keeps its own answer) — a key is never
    collapsed to a single value, which would silently drop one step's output."""
    # Positional list, not a dict, so repeated keys keep their distinct answers.
    current: list[list] = [[k, v] for k, v in outputs]
    present = {k for k, _ in current}

    def _peer_ctx(key: str) -> str:
        for k, v in current:            # first occurrence — deterministic
            if k == key:
                return v
        return ""

    for consultations in rounds(topology, max_rounds=max_rounds):
        for consulter, consulted in consultations:
            if consulter not in present or consulted not in present:
                continue
            peer_ctx = _peer_ctx(consulted)
            if not peer_ctx:
                continue
            for pair in current:        # refine every slot for this consulter
                if pair[0] != consulter:
                    continue
                try:
                    refined = runner(consulter, peer_ctx, pair[1])
                except Exception:
                    refined = None
                if refined:
                    pair[1] = refined
    return [(k, v) for k, v in current]
