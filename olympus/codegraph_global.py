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

from . import codegraph

GLOBAL = "global"


def _tagged(tag: str, nid: str) -> str:
    return f"{tag}:{nid}"


def add(tag: str, src_project: str = "self") -> dict:
    """Register/refresh one project's graph in the global graph under `tag`."""
    tag = codegraph._clean_label(tag).replace(" ", "-") or "repo"
    remove(tag)                                   # replace, never duplicate
    nodes_ = codegraph.nodes(src_project)
    edges_ = codegraph.edges(src_project)
    with codegraph._LOCK:
        g_nodes = codegraph._load(codegraph._NODES, GLOBAL)
        g_edges = codegraph._load(codegraph._EDGES, GLOBAL)
        have = {n["id"] for n in g_nodes}
        for n in nodes_:
            gid = _tagged(tag, n["id"])
            if gid not in have:
                g_nodes.append({**n, "id": gid, "repo": tag,
                                "path": f"{tag}/{n.get('path') or ''}"})
        have_e = {(e["src"], e["rel"], e["dst"]) for e in g_edges}
        for e in edges_:
            key = (_tagged(tag, e["src"]), e["rel"], _tagged(tag, e["dst"]))
            if key not in have_e:
                g_edges.append({**e, "src": key[0], "dst": key[2]})
        codegraph._save(codegraph._NODES, GLOBAL, g_nodes)
        codegraph._save(codegraph._EDGES, GLOBAL, g_edges)
    registry = codegraph.get_meta(GLOBAL).get("repos", {})
    registry[tag] = {"nodes": len(nodes_), "edges": len(edges_)}
    codegraph.set_meta(GLOBAL, repos=registry)
    return {"tag": tag, **registry[tag]}


def remove(tag: str) -> int:
    """Drop one repo's contribution from the global graph."""
    prefix = f"{tag}:"
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
