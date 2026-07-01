"""Relationship graph — people, organizations, and how they connect.

Flat memories answer "what do I know about Sarah?"; a graph answers "who do I
know at Acme?" — which needs edges, not rows. This is a small, per-user directed
graph of entities (people / companies) and typed relations (works_at,
cofounder_of, competitor_of, reports_to, ...), populated by the same background
extractor that builds typed memory, and traversed at retrieval time to surface
the people and orgs relevant to a turn.

Like everything else: inspectable, user-editable, on the shared store backend
(files by default, Postgres when configured), and never acted on without the
usual approval gate.
"""

from __future__ import annotations

import json
import re
import threading
import uuid

from . import memory, store

_NODES, _EDGES = "relgraph.nodes", "relgraph.edges"
_LOCK = threading.Lock()
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(("the", "and", "for", "inc", "llc", "ltd", "corp", "co"))

PERSON, COMPANY, OTHER = "person", "company", "other"
_MAX_NODES_INJECTED = 8
_MAX_NODES = 400            # per user; bound storage + injection cost
_MAX_LABEL = 80            # entity labels are names, not paragraphs


def _clean_label(label: str) -> str:
    """Labels are entity names — collapse to a single line and cap length so
    extracted content can't smuggle newlines/instructions into context."""
    return " ".join(str(label or "").split())[:_MAX_LABEL]


def _tok(text: str) -> set[str]:
    return {w for w in _WORD.findall(str(text).lower())
            if len(w) > 1 and w not in _STOP}


def _load(ns: str, user: str) -> list:
    blob = store.backend().get(ns, memory.safe_id(user))
    if not blob:
        return []
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return []


def _save(ns: str, user: str, data: list) -> None:
    store.backend().put(ns, memory.safe_id(user), json.dumps(data).encode())


# --- nodes & edges -------------------------------------------------------

def add_node(user: str, label: str, kind: str = OTHER) -> dict | None:
    """Add an entity, or return the existing one (matched case-insensitively).
    Returns None if the graph is at its node cap and the label is new — the
    caller must NOT treat that as a real node (see add_edge)."""
    label = _clean_label(label)
    if not label:
        raise ValueError("a node needs a label")
    with _LOCK:
        nodes = _load(_NODES, user)
        for n in nodes:
            if n["label"].lower() == label.lower():
                if kind != OTHER and n["kind"] == OTHER:
                    n["kind"] = kind          # upgrade an unknown kind
                    _save(_NODES, user, nodes)
                return dict(n)
        if len(nodes) >= _MAX_NODES:
            # At cap: refuse the new entity rather than aliasing it to nodes[0].
            # Returning an arbitrary existing node here would fabricate false
            # edges to it in add_edge; the existing graph is preserved instead.
            return None
        node = {"id": uuid.uuid4().hex[:12], "label": label, "kind": kind}
        nodes.append(node)
        _save(_NODES, user, nodes)
    return dict(node)


def add_edge(user: str, src_label: str, rel: str, dst_label: str,
             confidence: float = 0.7, src_kind: str = OTHER,
             dst_kind: str = OTHER) -> dict | None:
    """Connect two entities by a relation (creating the nodes as needed).
    De-duplicates on (src, rel, dst)."""
    rel = (rel or "").strip().lower().replace(" ", "_")
    if not rel:
        return None
    src = add_node(user, src_label, src_kind)
    dst = add_node(user, dst_label, dst_kind)
    if src is None or dst is None:      # graph at cap — don't invent an edge
        return None
    if src["id"] == dst["id"]:
        return None
    with _LOCK:
        edges = _load(_EDGES, user)
        for e in edges:
            if e["src"] == src["id"] and e["dst"] == dst["id"] and e["rel"] == rel:
                return dict(e)
        edge = {"id": uuid.uuid4().hex[:12], "src": src["id"], "dst": dst["id"],
                "rel": rel, "confidence": round(float(confidence), 3)}
        edges.append(edge)
        _save(_EDGES, user, edges)
    return dict(edge)


def nodes(user: str) -> list:
    return _load(_NODES, user)


def edges(user: str) -> list:
    return _load(_EDGES, user)


def _node_by_id(nodes_: list, nid: str) -> dict | None:
    return next((n for n in nodes_ if n["id"] == nid), None)


def forget(user: str, label: str) -> bool:
    """Remove an entity and every edge touching it."""
    with _LOCK:
        ns_ = _load(_NODES, user)
        node = next((n for n in ns_ if n["label"].lower() == label.strip().lower()),
                    None)
        if not node:
            return False
        ns_.remove(node)
        es = [e for e in _load(_EDGES, user)
              if e["src"] != node["id"] and e["dst"] != node["id"]]
        _save(_NODES, user, ns_)
        _save(_EDGES, user, es)
    return True


# --- traversal & retrieval ----------------------------------------------

def find_entities(user: str, text: str) -> list[dict]:
    """Nodes whose label words all appear in the text (so 'Acme' or 'Acme
    Corp' match, but a stray short word doesn't)."""
    toks = _tok(text)
    if not toks:
        return []
    out = []
    for n in _load(_NODES, user):
        label_toks = _tok(n["label"])
        if label_toks and label_toks <= toks:
            out.append(n)
    return out


def neighbors(user: str, node_id: str) -> list[tuple[str, dict]]:
    """One hop in BOTH directions: returns (phrase, node) for each connection,
    so 'who works at Acme' reaches people via incoming works_at edges."""
    ns_, es = _load(_NODES, user), _load(_EDGES, user)
    out = []
    for e in es:
        if e["src"] == node_id:
            other = _node_by_id(ns_, e["dst"])
            if other:
                out.append((f"{e['rel'].replace('_', ' ')} {other['label']}", other))
        elif e["dst"] == node_id:
            other = _node_by_id(ns_, e["src"])
            if other:
                out.append((f"{other['label']} {e['rel'].replace('_', ' ')} this",
                            other))
    return out


def describe(user: str, label: str) -> str:
    """Human-readable one-entity view (for `olympus graph <entity>`)."""
    ents = find_entities(user, label)
    if not ents:
        return ""
    node = ents[0]
    lines = [f"{node['label']} ({node['kind']})"]
    for phrase, _ in neighbors(user, node["id"]):
        lines.append(f"  - {phrase}")
    return "\n".join(lines)


def context_block(user: str, message: str) -> str:
    """Surface the people/orgs connected to entities named in this turn.
    Returns '' when the turn names no known entity, so it costs no tokens."""
    ents = find_entities(user, message)
    if not ents:
        return ""
    ns_, es = _load(_NODES, user), _load(_EDGES, user)
    seen: set[str] = set()
    lines: list[str] = []
    for ent in ents:
        for e in es:
            if e["src"] == ent["id"]:
                other = _node_by_id(ns_, e["dst"])
                if other:
                    key = f"{ent['id']}|{e['rel']}|{other['id']}"
                    if key not in seen:
                        seen.add(key)
                        lines.append(f"- {ent['label']} {e['rel'].replace('_',' ')} "
                                     f"{other['label']}")
            elif e["dst"] == ent["id"]:
                other = _node_by_id(ns_, e["src"])
                if other:
                    key = f"{other['id']}|{e['rel']}|{ent['id']}"
                    if key not in seen:
                        seen.add(key)
                        lines.append(f"- {other['label']} {e['rel'].replace('_',' ')} "
                                     f"{ent['label']}")
            if len(lines) >= _MAX_NODES_INJECTED:
                break
    if not lines:
        return ""
    return ("\n\n## People & organizations relevant here (from what you've told "
            "me):\n" + "\n".join(lines[:_MAX_NODES_INJECTED]))


def ingest(user: str, relationships: list[dict]) -> int:
    """Add extracted {subject, relation, object, subject_kind, object_kind}
    triples to the graph. Best-effort; returns how many edges were added."""
    added = 0
    for r in relationships or []:
        try:
            subj = str(r.get("subject", "")).strip()
            obj = str(r.get("object", "")).strip()
            rel = str(r.get("relation", "")).strip()
            if not (subj and obj and rel):
                continue
            if add_edge(user, subj, rel, obj,
                        confidence=float(r.get("confidence", 0.7)),
                        src_kind=r.get("subject_kind", OTHER),
                        dst_kind=r.get("object_kind", OTHER)):
                added += 1
        except (ValueError, TypeError):
            continue
    return added
