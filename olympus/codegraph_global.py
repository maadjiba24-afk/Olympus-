"""The global graph — every project's graph, merged and queryable as one.

codegraph is keyed per project; this module maintains the reserved project
"global": a union of registered project graphs with each node id and path
prefixed by its repo tag, so `codegraph_path "billing.charge" "auth.login"`
can cross repository boundaries. The registry (tag -> counts) lives in the
global project's meta; add() is idempotent per tag (re-adding replaces that
repo's nodes), remove() surgically drops one repo, and nothing here ever
mutates a source project's graph.
"""

from __future__ import annotations

import hashlib

from . import codegraph

GLOBAL = "global"


def clean_tag(tag: str) -> str:
    """A tag safe to use as a node-id prefix: no ':' (the id separator) and no
    '/' (the path separator), so `remove('acme')` can never also delete
    `acme:web`'s nodes, and one tag is never a prefix of another's ids."""
    t = codegraph._clean_label(tag).replace(" ", "-")
    t = t.replace(":", "-").replace("/", "-")
    return t or "repo"


def _tagged(tag: str, nid: str) -> str:
    return f"{tag}:{nid}"


def add(tag: str, src_project: str = "self") -> dict:
    """Register/refresh one project's graph in the global graph under `tag`.
    Enforces the same node/edge caps the per-project graph uses, so the global
    graph can't grow without bound as repos are registered."""
    tag = clean_tag(tag)
    remove(tag)                                   # replace, never duplicate
    nodes_ = codegraph.nodes(src_project)
    edges_ = codegraph.edges(src_project)
    added_n = added_e = 0
    with codegraph._LOCK:
        g_nodes = codegraph._load(codegraph._NODES, GLOBAL)
        g_edges = codegraph._load(codegraph._EDGES, GLOBAL)
        have = {n["id"] for n in g_nodes}
        kept_ids: set[str] = set()
        for n in nodes_:
            if len(g_nodes) >= codegraph._MAX_NODES:
                break                             # global graph is bounded too
            gid = _tagged(tag, n["id"])
            if gid not in have:
                g_nodes.append({**n, "id": gid, "repo": tag,
                                "path": f"{tag}/{n.get('path') or ''}"})
                kept_ids.add(gid)
                added_n += 1
        have_e = {(e["src"], e["rel"], e["dst"]) for e in g_edges}
        for e in edges_:
            if len(g_edges) >= codegraph._MAX_EDGES:
                break
            src, dst = _tagged(tag, e["src"]), _tagged(tag, e["dst"])
            if src not in kept_ids or dst not in kept_ids:
                continue                          # both endpoints must exist
            key = (src, e["rel"], dst)
            if key not in have_e:
                # Regenerate the id from the tagged endpoints so global edge ids
                # are unique even when the same source project is registered
                # under two tags (subgraph_query dedups on edge id).
                eid = hashlib.sha256(
                    f"{src}{e['rel']}{dst}".encode()).hexdigest()[:12]
                g_edges.append({**e, "id": eid, "src": src, "dst": dst})
                added_e += 1
        codegraph._save(codegraph._NODES, GLOBAL, g_nodes)
        codegraph._save(codegraph._EDGES, GLOBAL, g_edges)
    registry = codegraph.get_meta(GLOBAL).get("repos", {})
    registry[tag] = {"nodes": added_n, "edges": added_e}
    codegraph.set_meta(GLOBAL, repos=registry)
    return {"tag": tag, **registry[tag]}


def remove(tag: str) -> int:
    """Drop one repo's contribution from the global graph."""
    prefix = f"{clean_tag(tag)}:"
    with codegraph._LOCK:
        g_nodes = codegraph._load(codegraph._NODES, GLOBAL)
        doomed = {n["id"] for n in g_nodes if n["id"].startswith(prefix)}
        if doomed:
            g_nodes = [n for n in g_nodes if n["id"] not in doomed]
            g_edges = [e for e in codegraph._load(codegraph._EDGES, GLOBAL)
                       if e["src"] not in doomed and e["dst"] not in doomed]
            codegraph._save(codegraph._NODES, GLOBAL, g_nodes)
            codegraph._save(codegraph._EDGES, GLOBAL, g_edges)
    registry = codegraph.get_meta(GLOBAL).get("repos", {})
    if tag in registry:
        registry.pop(tag)
        codegraph.set_meta(GLOBAL, repos=registry)
    return len(doomed)


def repos() -> dict:
    """The registry: {tag: {nodes, edges}}."""
    return codegraph.get_meta(GLOBAL).get("repos", {})
