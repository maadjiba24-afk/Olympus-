"""Code knowledge graph — modules, classes, functions, and how they connect.

`relgraph` answers "who do I know at Acme?"; `codegraph` answers "what calls
`resolve_handler`?" or "what breaks if I change `add_edge`?" — questions about a
*codebase*, not a contact network. It shares relgraph's storage, locking, and
injection-aware conventions, but is keyed per-PROJECT (default "self" =
Olympus's own source tree) rather than per-user, and is populated
deterministically — stdlib-`ast` for Python (`codegraph_ast`), a regex engine
for ~20 other languages (`codegraph_langs`), documents (`codegraph_ingest`),
and schema introspection (`codegraph_introspect`) — never by an LLM extractor.

Like everything else: inspectable, on the shared store backend (files by
default, Postgres when configured), bounded, and the context block costs zero
tokens when the turn names nothing distinctive in the graph.

This module is the graph CORE: nodes/edges/meta, traversal (neighbors, impact,
shortest_path, token-budgeted subgraph_query), the context block, and the
hallucination oracle. The rest of the absorbed Graphify surface lives beside
it: codegraph_build (multi-language + incremental), codegraph_analysis
(communities/god nodes/surprises/report/benchmark), codegraph_dedup,
codegraph_export (graphml/mermaid/html/obsidian/wiki/cypher/neo4j/falkordb),
codegraph_watch, codegraph_global (cross-repo), codegraph_prs (PR dashboard).

Edges carry a confidence *tier* (Graphify's pattern, the one part worth taking
verbatim): EXTRACTED edges come straight from a real parser and are ground
truth (confidence 1.0); INFERRED/AMBIGUOUS edges are guesses. The
hallucination oracle (`verify_claim`) trusts EXTRACTED only, and refuses to
refute where only regex-level evidence exists — it never asserts a claim false
on the strength of a guess. This verification is the thing Graphify itself
does not have.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
from collections import deque

from . import memory, security, store

_NODES, _EDGES, _META = "codegraph.nodes", "codegraph.edges", "codegraph.meta"
_LOCK = threading.Lock()
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")

# confidence tiers
EXTRACTED, INFERRED, AMBIGUOUS = "EXTRACTED", "INFERRED", "AMBIGUOUS"
# node kinds
MODULE, CLASS, FUNCTION, METHOD, RATIONALE = (
    "module", "class", "function", "method", "rationale")
# non-code kinds: documents ingested by codegraph_ingest, and schema/manifest
# entities from codegraph_introspect (tables, crates). Same graph, same rules.
DOCUMENT, ENTITY = "document", "entity"
# relations that mean "B depends on A" when traversed from A (for impact).
# `inherits`/`implements` count: change a base class and every subclass is
# affected, so reverse-traversal for impact must follow them.
_DEP_RELS = frozenset({"calls", "imports", "references", "inherits",
                       "implements"})
# cross-file relations rebuilt from scratch on every incremental update (they
# depend on the whole graph, not one file), kept in one place so build/update
# and the resolver never drift.
_CROSS_FILE_RELS = frozenset({"calls", "imports", "references", "inherits",
                              "implements"})

_MAX_NODES = 20000          # per project; bound storage + injection cost
_MAX_EDGES = 80000
_MAX_INJECTED = 12          # nodes surfaced into a single turn
_MAX_LABEL = 200

# Common English words that are also code symbol names. The auto-injected
# context block must NOT fire just because a user said "save my report" — that
# would leak code structure into unrelated turns and waste tokens. Only
# *distinctive* identifiers (snake_case, camelCase, or long & uncommon) trigger
# injection; explicit tool queries (the agent typed the symbol) stay permissive.
_COMMON = frozenset({
    "run", "build", "main", "save", "load", "get", "set", "add", "find",
    "report", "record", "update", "delete", "create", "list", "show", "send",
    "read", "write", "check", "start", "stop", "close", "open", "call", "make",
    "name", "value", "data", "text", "path", "file", "line", "code", "node",
    "edge", "user", "test", "type", "kind", "item", "task", "step", "work",
})

# A/B toggle for the Phase 0 gate. When off, the context block is empty AND the
# specialists strip the codegraph tools from their loadout (see specialists.py)
# — a real switch, so the gate measures the feature, not just a test hook.
_ENABLED = True


def set_enabled(value: bool) -> None:
    global _ENABLED
    _ENABLED = bool(value)


def enabled() -> bool:
    return _ENABLED


# --- helpers -------------------------------------------------------------

def _key(project: str) -> str:
    return memory.safe_id(project or "self")


def _clean_label(label: str) -> str:
    """Collapse to a single line and cap length so extracted text (docstrings,
    comments) can't smuggle newlines/instructions into an injected context
    block — the same defense relgraph applies to entity names."""
    return " ".join(str(label or "").split())[:_MAX_LABEL]


def _distinctive(label: str) -> bool:
    """True if `label` is specific enough to auto-surface. snake_case/camelCase
    identifiers and long uncommon words qualify; bare common words do not."""
    if "_" in label:                                   # snake_case
        return True
    if any(c.isupper() for c in label[1:]):            # camelCase
        return True
    low = label.lower()
    return len(low) >= 6 and low not in _COMMON


def _node_id(path: str, qual: str) -> str:
    """Deterministic id from (path, qualified-name). Stable across rebuilds and
    line-number shifts, so incremental re-extraction keeps node identity (this
    is why `span` is a mutable field, NOT part of the id)."""
    return hashlib.sha256(f"{path}::{qual}".encode()).hexdigest()[:12]


def _toks(text: str) -> set[str]:
    return {w.lower() for w in _IDENT.findall(str(text))}


# A build session: while active for a project, node/edge reads and writes hit
# an in-memory cache with id/edge-key indexes, flushed to the store ONCE at
# exit. Without it, a whole-repo build pays a full JSON round-trip per node —
# O(n²) in graph size — which turns "seconds" into "minutes" on a real repo.
# Single-writer by design (guarded by _LOCK on entry/exit like everything else);
# concurrent readers in other processes see the pre-build graph until the
# atomic store.put publishes the new one.
_BULK: dict | None = None


def _bulk_for(project: str) -> dict | None:
    b = _BULK
    return b if b and b["key"] == _key(project) else None


@contextlib.contextmanager
def bulk(project: str):
    """Batch all graph writes for `project`; flush once on exit."""
    global _BULK
    with _LOCK:
        if _BULK is not None:
            raise RuntimeError("a codegraph bulk session is already active")
        nodes_ = _load_disk(_NODES, project)
        edges_ = _load_disk(_EDGES, project)
        _BULK = {"key": _key(project), "nodes": nodes_, "edges": edges_,
                 "node_idx": {n["id"]: n for n in nodes_},
                 "edge_keys": {(e["src"], e["rel"], e["dst"]): e
                               for e in edges_}}
    try:
        yield
    finally:
        with _LOCK:
            b, _BULK = _BULK, None
        _save(_NODES, project, b["nodes"])
        _save(_EDGES, project, b["edges"])


def _load_disk(ns: str, project: str) -> list:
    blob = store.backend().get(ns, _key(project))
    if not blob:
        return []
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return []


def _load(ns: str, project: str) -> list:
    b = _bulk_for(project)
    if b is not None:
        return b["nodes"] if ns == _NODES else b["edges"]
    return _load_disk(ns, project)


def _save(ns: str, project: str, data) -> None:
    b = _bulk_for(project)
    if b is not None:
        which = "nodes" if ns == _NODES else "edges"
        if b[which] is not data:               # replaced list (e.g. remove_paths)
            b[which] = data
            if which == "nodes":
                b["node_idx"] = {n["id"]: n for n in data}
            else:
                b["edge_keys"] = {(e["src"], e["rel"], e["dst"]): e
                                  for e in data}
        return
    store.backend().put(ns, _key(project), json.dumps(data).encode())


def _by_id(nodes_: list, nid: str) -> dict | None:
    b = _BULK
    if b is not None and b["nodes"] is nodes_:
        return b["node_idx"].get(nid)
    return next((n for n in nodes_ if n["id"] == nid), None)


def _by_label(project: str, name: str) -> list[dict]:
    """Nodes whose symbol name matches `name` exactly (case-insensitive).
    Code identifiers are distinctive, so exact match is the right precision."""
    name = name.strip().lower()
    return [n for n in _load(_NODES, project) if n["label"].lower() == name]


# --- nodes & edges -------------------------------------------------------

def add_node(project: str, path: str, qual: str, kind: str = FUNCTION,
             span: list | None = None) -> dict | None:
    """Upsert a code entity, identified by (path, qualified-name). Returns the
    node, or ``None`` when the graph is at the node cap and this is a NEW node —
    callers must treat a ``None`` as "not in the graph" and skip any edge they
    would have attached. (Returning an unrelated node here, as the spike did,
    silently welded every over-cap file's structure onto node[0] as false
    EXTRACTED edges — the exact failure the oracle must never manufacture.)
    Re-extraction updates a mutable `span` without changing the id."""
    qual = _clean_label(qual)
    if not qual:
        raise ValueError("a node needs a qualified name")
    label = _clean_label(qual.rsplit(".", 1)[-1]) or qual
    nid = _node_id(path, qual)
    with _LOCK:
        nodes_ = _load(_NODES, project)
        existing = _by_id(nodes_, nid)
        if existing:
            changed = False
            if span and existing.get("span") != span:
                existing["span"] = span
                changed = True
            if kind and existing.get("kind") != kind and kind != FUNCTION:
                existing["kind"] = kind
                changed = True
            if changed:
                _save(_NODES, project, nodes_)
            return dict(existing)
        if len(nodes_) >= _MAX_NODES:          # bounded: refuse new nodes
            return None
        node = {"id": nid, "label": label, "qual": qual, "kind": kind,
                "path": path, "span": span}
        nodes_.append(node)
        b = _bulk_for(project)
        if b is not None and b["nodes"] is nodes_:
            b["node_idx"][nid] = node
        _save(_NODES, project, nodes_)
    return dict(node)


def add_rationale(project: str, path: str, owner_id: str, text: str,
                  slot: str = "doc") -> dict | None:
    """Store a design-intent note (docstring / NOTE/WHY/HACK comment) as a node
    linked to the code it explains. SANITIZED twice — `_clean_label` strips
    newlines and `security.sanitize_for_memory` defangs injection-shaped lines —
    because a comment like '# ignore all previous instructions' must never reach
    a model as a clean instruction (the existing injection model). `slot` keys
    the node within its owner, so one owner can hold several notes (a document's
    headings) without them collapsing into one."""
    first = next((ln for ln in str(text or "").splitlines() if ln.strip()), "")
    safe = _clean_label(security.sanitize_for_memory(first))
    if not safe:
        return None
    qual = f"{owner_id}#{_clean_label(slot) or 'doc'}"
    node = add_node(project, path, qual, kind=RATIONALE)
    if node is None:                              # graph at the node cap
        return None
    with _LOCK:
        nodes_ = _load(_NODES, project)
        n = _by_id(nodes_, node["id"])
        if n:
            n["label"] = safe
            _save(_NODES, project, nodes_)
            node = dict(n)
    add_edge(project, node["id"], "explains", owner_id)
    return node


def add_edge(project: str, src_id: str, rel: str, dst_id: str,
             tier: str = EXTRACTED, confidence: float = 1.0) -> dict | None:
    """Connect two existing nodes by a relation. De-duplicates on
    (src, rel, dst). Skips self-edges and dangling references."""
    rel = (rel or "").strip().lower().replace(" ", "_")
    if not rel or src_id == dst_id:
        return None
    with _LOCK:
        nodes_ = _load(_NODES, project)
        if not (_by_id(nodes_, src_id) and _by_id(nodes_, dst_id)):
            return None
        edges_ = _load(_EDGES, project)
        b = _bulk_for(project)
        if b is not None and b["edges"] is edges_:
            hit = b["edge_keys"].get((src_id, rel, dst_id))
            if hit is not None:
                return dict(hit)
        else:
            for e in edges_:
                if e["src"] == src_id and e["dst"] == dst_id and e["rel"] == rel:
                    return dict(e)
        if len(edges_) >= _MAX_EDGES:
            return None
        edge = {"id": hashlib.sha256(f"{src_id}{rel}{dst_id}".encode())
                .hexdigest()[:12], "src": src_id, "dst": dst_id, "rel": rel,
                "tier": tier, "confidence": round(float(confidence), 3)}
        edges_.append(edge)
        if b is not None and b["edges"] is edges_:
            b["edge_keys"][(src_id, rel, dst_id)] = edge
        _save(_EDGES, project, edges_)
    return dict(edge)


def nodes(project: str) -> list:
    return _load(_NODES, project)


def edges(project: str) -> list:
    return _load(_EDGES, project)


def stats(project: str) -> dict:
    return {"nodes": len(_load(_NODES, project)),
            "edges": len(_load(_EDGES, project))}


def clear(project: str) -> None:
    """Wipe the graph for a project (a full rebuild starts here)."""
    _save(_NODES, project, [])
    _save(_EDGES, project, [])


def remove_paths(project: str, rel_paths: set[str]) -> int:
    """Drop every node extracted from the given files (plus their rationale
    nodes) and any edge touching them. The incremental-update primitive: a
    changed or deleted file removes its old contribution before re-extraction.
    Returns the number of nodes removed."""
    if not rel_paths:
        return 0
    with _LOCK:
        nodes_ = _load(_NODES, project)
        doomed = {n["id"] for n in nodes_ if n.get("path") in rel_paths}
        if not doomed:
            return 0
        nodes_ = [n for n in nodes_ if n["id"] not in doomed]
        edges_ = [e for e in _load(_EDGES, project)
                  if e["src"] not in doomed and e["dst"] not in doomed]
        _save(_NODES, project, nodes_)
        _save(_EDGES, project, edges_)
    return len(doomed)


def remove_edges_by_relation(project: str, rels: set[str]) -> int:
    """Drop every edge with a relation in `rels`. Incremental update clears the
    cross-file edges (`calls`/`imports`/`references`) and lets `_resolve`
    rebuild them from the full manifest, so an incremental graph is byte-for-
    byte the resolution a full build would produce — no stale EXTRACTED edge
    survives after new code makes a call ambiguous."""
    with _LOCK:
        edges_ = _load(_EDGES, project)
        kept = [e for e in edges_ if e["rel"] not in rels]
        removed = len(edges_) - len(kept)
        if removed:
            _save(_EDGES, project, kept)
    return removed


def degree(project: str) -> dict[str, int]:
    """Node id -> number of touching edges (undirected degree)."""
    counts: dict[str, int] = {}
    for e in _load(_EDGES, project):
        counts[e["src"]] = counts.get(e["src"], 0) + 1
        counts[e["dst"]] = counts.get(e["dst"], 0) + 1
    return counts


def set_meta(project: str, **kv) -> None:
    blob = store.backend().get(_META, _key(project))
    meta = {}
    if blob:
        try:
            meta = json.loads(blob)
        except (ValueError, json.JSONDecodeError):
            meta = {}
    meta.update({k: v for k, v in kv.items() if v is not None})
    store.backend().put(_META, _key(project), json.dumps(meta).encode())


def get_meta(project: str) -> dict:
    blob = store.backend().get(_META, _key(project))
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return {}


# --- traversal & retrieval ----------------------------------------------

def find(project: str, name: str) -> list[dict]:
    """Nodes whose label appears as an identifier in `name`. Permissive — used
    by the explicit query tools where the agent typed the symbol."""
    toks = _toks(name)
    if not toks:
        return []
    return [n for n in _load(_NODES, project) if n["label"].lower() in toks]


def neighbors(project: str, node_id: str) -> list[tuple[str, dict]]:
    """One hop in BOTH directions: (phrase, node) per connection, so 'what calls
    X' reaches callers via incoming `calls` edges."""
    nodes_, edges_ = _load(_NODES, project), _load(_EDGES, project)
    out = []
    for e in edges_:
        if e["src"] == node_id:
            other = _by_id(nodes_, e["dst"])
            if other:
                out.append((f"{e['rel'].replace('_', ' ')} {other['label']}",
                            other))
        elif e["dst"] == node_id:
            other = _by_id(nodes_, e["src"])
            if other:
                out.append((f"{other['label']} {e['rel'].replace('_', ' ')} this",
                            other))
    return out


def impact(project: str, node_id: str, depth: int = 2) -> list[dict]:
    """Reverse-dependency closure: everything that (transitively) calls /
    imports / references this node — 'what breaks if I change it'."""
    nodes_, edges_ = _load(_NODES, project), _load(_EDGES, project)
    seen, frontier, out = {node_id}, deque([(node_id, 0)]), []
    while frontier:
        nid, d = frontier.popleft()
        if d >= depth:
            continue
        for e in edges_:
            if e["dst"] == nid and e["rel"] in _DEP_RELS and e["src"] not in seen:
                seen.add(e["src"])
                dep = _by_id(nodes_, e["src"])
                if dep:
                    out.append(dep)
                    frontier.append((e["src"], d + 1))
    return out


def shortest_path(project: str, a_id: str, b_id: str) -> list[str]:
    """Undirected BFS shortest path of node ids between two nodes ([] if none)."""
    if a_id == b_id:
        return [a_id]
    edges_ = _load(_EDGES, project)
    adj: dict[str, set[str]] = {}
    for e in edges_:
        adj.setdefault(e["src"], set()).add(e["dst"])
        adj.setdefault(e["dst"], set()).add(e["src"])
    prev, q = {a_id: None}, deque([a_id])
    while q:
        cur = q.popleft()
        if cur == b_id:
            path = []
            while cur is not None:
                path.append(cur)
                cur = prev[cur]
            return list(reversed(path))
        for nb in adj.get(cur, ()):
            if nb not in prev:
                prev[nb] = cur
                q.append(nb)
    return []


def describe(project: str, name: str) -> str:
    """Human-readable one-symbol view (for `olympus codegraph show <name>`)."""
    ents = find(project, name)
    if not ents:
        return ""
    node = ents[0]
    where = f" [{node['path']}]" if node.get("path") else ""
    lines = [f"{node['label']} ({node['kind']}){where}"]
    for phrase, _ in neighbors(project, node["id"]):
        lines.append(f"  - {phrase}")
    return "\n".join(lines)


# Prose-labeled node kinds: their labels are free text pulled from the analyzed
# repo (docstrings, comments, doc headings, doc titles). Code/entity labels are
# identifier-constrained by extraction; prose is not. The trusted, UNWRAPPED
# paths (the auto-injected context block, the TRUSTED_TOOLS graph tools) surface
# STRUCTURE ONLY and never a prose label, so a hostile comment in a cloned repo
# cannot reach the model as clean text through them. Prose stays available in
# the explicit CLI views, whose output is not auto-injected.
_PROSE_KINDS = frozenset({RATIONALE, DOCUMENT})


def _is_prose(node: dict | None) -> bool:
    return bool(node) and node.get("kind") in _PROSE_KINDS


def context_block(project: str, text: str) -> str:
    """A compact, injectable view of code structure relevant to this turn.
    Returns '' when the toggle is off or the turn names no *distinctive* symbol
    in the graph, so it costs no tokens and never fires on incidental common
    words — the relgraph discipline, tightened for code identifiers.

    STRUCTURE ONLY: prose-labeled nodes (rationale/documents) are excluded, so
    attacker-influenced comment/doc text from an analyzed repo is never injected
    unwrapped into a turn (see `_PROSE_KINDS`)."""
    if not _ENABLED:
        return ""
    ents = [n for n in find(project, text)
            if _distinctive(n["label"]) and not _is_prose(n)]
    if not ents:
        return ""
    nodes_, edges_ = _load(_NODES, project), _load(_EDGES, project)
    seen: set[str] = set()
    lines: list[str] = []
    for ent in ents:
        for e in edges_:
            if e["src"] == ent["id"]:
                other = _by_id(nodes_, e["dst"])
                phrase = (f"{ent['label']} {e['rel'].replace('_', ' ')} "
                          f"{other['label']}") if other else None
            elif e["dst"] == ent["id"]:
                other = _by_id(nodes_, e["src"])
                phrase = (f"{other['label']} {e['rel'].replace('_', ' ')} "
                          f"{ent['label']}") if other else None
            else:
                continue
            if _is_prose(other):                  # never inject prose unwrapped
                continue
            if phrase and phrase not in seen:
                seen.add(phrase)
                lines.append(f"- {phrase}")
            if len(lines) >= _MAX_INJECTED:
                break
        if len(lines) >= _MAX_INJECTED:
            break
    if not lines:
        return ""
    return ("\n\n## Code structure relevant here (from the graph; EXTRACTED "
            "edges are ground truth):\n" + "\n".join(lines[:_MAX_INJECTED]))


def subgraph_query(project: str, question: str, budget_tokens: int = 2000,
                   dfs: bool = False, include_prose: bool = False) -> str:
    """Answer-shaped subgraph retrieval: seed on the symbols the question
    names, walk outward (BFS by default, DFS on request), and stop when the
    rendered text would exceed `budget_tokens` (~4 chars/token). The graph
    analog of grep-then-read: everything relevant, nothing else, within a
    predictable context cost. (Graphify's query pattern, absorbed.)

    `include_prose` defaults to FALSE — prose-labeled nodes (rationale/
    documents, whose text comes from the analyzed repo) are excluded. The
    agent tools and MCP surface call it this way so hostile comment/doc text is
    never returned unwrapped; the explicit CLI passes True for a full view."""
    budget_chars = max(200, int(budget_tokens) * 4)
    seeds = [s for s in find(project, question)
             if include_prose or not _is_prose(s)]
    if not seeds:
        return ""
    nodes_, edges_ = _load(_NODES, project), _load(_EDGES, project)
    by_id = {n["id"]: n for n in nodes_}
    adj: dict[str, list[dict]] = {}
    for e in edges_:
        adj.setdefault(e["src"], []).append(e)
        adj.setdefault(e["dst"], []).append(e)

    lines: list[str] = []
    used = 0
    for s in seeds[:8]:
        where = f" [{s.get('path', '?')}" + (
            f":{s['span'][0]}]" if s.get("span") else "]")
        line = f"{s['label']} ({s['kind']}){where}"
        lines.append(line)
        used += len(line) + 1

    seen_edges: set[str] = set()
    seen_nodes = {s["id"] for s in seeds[:8]}
    frontier: deque = deque((s["id"], 0) for s in seeds[:8])
    while frontier and used < budget_chars:
        nid, d = frontier.pop() if dfs else frontier.popleft()
        for e in adj.get(nid, ()):
            if e["id"] in seen_edges:
                continue
            seen_edges.add(e["id"])
            a, b = by_id.get(e["src"]), by_id.get(e["dst"])
            if not (a and b):
                continue
            if not include_prose and (_is_prose(a) or _is_prose(b)):
                continue
            mark = "" if e.get("tier") == EXTRACTED else f" ({e.get('tier', '?')})"
            line = f"- {a['label']} {e['rel'].replace('_', ' ')} {b['label']}{mark}"
            if used + len(line) > budget_chars:
                frontier.clear()
                break
            lines.append(line)
            used += len(line) + 1
            for nxt in (e["src"], e["dst"]):
                if nxt not in seen_nodes:
                    seen_nodes.add(nxt)
                    frontier.append((nxt, d + 1))
    return "\n".join(lines)


# --- the hallucination oracle -------------------------------------------

_CALLS_RE = re.compile(
    r"\b([A-Za-z_][\w.]+)\b\s+(?:calls|invokes|uses)\s+\b([A-Za-z_][\w.]+)\b",
    re.I)
_IMPORTS_RE = re.compile(
    r"\b([A-Za-z_][\w.]+)\b\s+imports\s+\b([A-Za-z_][\w.]+)\b", re.I)
_UNUSED_RE = re.compile(
    r"\b([A-Za-z_][\w.]+)\b\s+(?:is\s+unused|is\s+dead\s+code|"
    r"is\s+never\s+called|has\s+no\s+callers)\b", re.I)
_NOTHING_RE = re.compile(
    r"\bnothing\s+(?:calls|references|uses)\s+\b([A-Za-z_][\w.]+)\b", re.I)


def _last(sym: str) -> str:
    return sym.rsplit(".", 1)[-1]


def _has_extracted_edge(project: str, src_nodes, rel, dst_nodes) -> bool:
    src_ids = {n["id"] for n in src_nodes}
    dst_ids = {n["id"] for n in dst_nodes}
    return any(e["src"] in src_ids and e["dst"] in dst_ids and e["rel"] == rel
               and e["tier"] == EXTRACTED for e in _load(_EDGES, project))


def _has_any_edge(project: str, src_nodes, rel, dst_nodes) -> bool:
    src_ids = {n["id"] for n in src_nodes}
    dst_ids = {n["id"] for n in dst_nodes}
    return any(e["src"] in src_ids and e["dst"] in dst_ids and e["rel"] == rel
               for e in _load(_EDGES, project))


def _ast_backed(nodes_: list[dict]) -> bool:
    """True when every node comes from a file the AST extractor covers (.py).
    Regex-extracted languages never get EXTRACTED call edges, so the ABSENCE
    of one there is not evidence of absence — refuting on it would make the
    oracle unsound the moment the graph went multi-language."""
    return all(str(n.get("path", "")).endswith(".py") for n in nodes_)


def _incoming_tiers(project: str, dst_nodes, rels) -> set[str]:
    """Confidence tiers of edges pointing INTO these nodes via `rels`. The
    oracle distinguishes 'has an EXTRACTED caller' (proof it is used) from
    'has only an AMBIGUOUS/INFERRED caller' (a guess that must not settle an
    is-unused claim either way)."""
    dst_ids = {n["id"] for n in dst_nodes}
    return {e.get("tier") for e in _load(_EDGES, project)
            if e["dst"] in dst_ids and e["rel"] in rels}


def verify_claim(project: str, claim: str) -> dict:
    """Check a structural claim about the code against EXTRACTED edges.

    Returns {"verdict": CONFIRMED|REFUTED|UNKNOWN, "detail": str}. UNKNOWN when a
    referenced symbol isn't in the graph — never assert a claim false just
    because we can't see the symbol (the honesty rule Aletheia needs)."""
    claim = claim or ""
    for rx, rel in ((_CALLS_RE, "calls"), (_IMPORTS_RE, "imports")):
        m = rx.search(claim)
        if m:
            a, b = _by_label(project, _last(m.group(1))), \
                   _by_label(project, _last(m.group(2)))
            if not a or not b:
                return {"verdict": "UNKNOWN",
                        "detail": "a referenced symbol is not in the graph"}
            if _has_extracted_edge(project, a, rel, b):
                return {"verdict": "CONFIRMED",
                        "detail": f"EXTRACTED {rel} edge present"}
            if _has_any_edge(project, a, rel, b):
                return {"verdict": "UNKNOWN",
                        "detail": f"only an inferred {rel} edge exists "
                                  f"(regex-level evidence, not proof)"}
            if not _ast_backed(a) or not _ast_backed(b):
                return {"verdict": "UNKNOWN",
                        "detail": "symbols come from regex-extracted files — "
                                  "absence of an edge is not proof there"}
            return {"verdict": "REFUTED",
                    "detail": f"no EXTRACTED {rel} edge between known symbols"}
    m = _UNUSED_RE.search(claim) or _NOTHING_RE.search(claim)
    if m:
        target = _by_label(project, _last(m.group(1)))
        if not target:
            return {"verdict": "UNKNOWN", "detail": "symbol not in the graph"}
        tiers = _incoming_tiers(project, target, {"calls", "references"})
        if EXTRACTED in tiers:
            return {"verdict": "REFUTED",
                    "detail": "it has an EXTRACTED caller"}
        if tiers:                                 # only INFERRED/AMBIGUOUS in
            return {"verdict": "UNKNOWN",
                    "detail": "its only incoming callers are inferred "
                              "(regex-level evidence, not proof)"}
        if not _ast_backed(target):
            return {"verdict": "UNKNOWN",
                    "detail": "symbol comes from a regex-extracted file — "
                              "missing callers there prove nothing"}
        return {"verdict": "CONFIRMED", "detail": "no incoming callers found"}
    return {"verdict": "UNKNOWN", "detail": "claim shape not recognized"}
