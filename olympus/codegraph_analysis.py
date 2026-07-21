"""Graph analysis — communities, god nodes, surprises, and the report.

The structural half of Graphify, absorbed natively: no networkx, no
graspologic — a pure-Python Louvain (greedy modularity, deterministic order)
over the store-backed graph, plus the derived views that make a graph worth
having:

  god_nodes()      the most-connected symbols — what everything flows through
  communities()    Louvain partition, cached in the store with heuristic labels
  surprises()      cross-community edges ranked by how unexpected they are
  questions()      questions the graph is uniquely positioned to answer
  render_report()  CODEGRAPH_REPORT.md — the whole picture in one document
  token_benchmark() measured context cost: subgraph query vs. reading the corpus

Community labels are heuristic by default (dominant path prefix + top symbol),
so labeling costs zero tokens; the council can rename them, and `set_label`
persists it. Determinism matters here for the same reason it matters in
capabilities.py: a rebuild on unchanged code must produce the same partition,
or every diff shows phantom churn.
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from . import codegraph, store

_COMM = "codegraph.communities"

_MAX_COMMUNITIES_SHOWN = 12
_MAX_GOD_NODES = 10
_MAX_SURPRISES = 10


# --- Louvain (pure Python, deterministic) ---------------------------------

def _louvain(adj: dict[str, dict[str, float]], seed: int = 0,
             max_passes: int = 10) -> dict[str, int]:
    """Greedy modularity optimization with community aggregation. `adj` is an
    undirected weighted adjacency map. Node order is sorted and the RNG is
    seeded, so the partition is reproducible for a given graph."""
    if not adj:
        return {}
    # current partition maps original node -> community of the CURRENT level;
    # levels aggregate communities into super-nodes until modularity stalls.
    node_to_orig: dict[str, list[str]] = {n: [n] for n in adj}
    result: dict[str, int] = {}
    rng = random.Random(seed)

    for _level in range(max_passes):
        m2 = sum(w for nbrs in adj.values() for w in nbrs.values())  # = 2m
        if m2 == 0:
            break
        degree = {n: sum(nbrs.values()) for n, nbrs in adj.items()}
        comm = {n: i for i, n in enumerate(sorted(adj))}
        comm_degree = defaultdict(float)
        for n, c in comm.items():
            comm_degree[c] += degree[n]

        improved = False
        for _sweep in range(10):
            moved = 0
            order = sorted(adj)
            rng.shuffle(order)
            for n in order:
                c_old = comm[n]
                # weights from n into each neighboring community
                w_to: dict[int, float] = defaultdict(float)
                for nb, w in adj[n].items():
                    if nb != n:
                        w_to[comm[nb]] += w
                comm_degree[c_old] -= degree[n]
                best_c, best_gain = c_old, 0.0
                for c_new, w in w_to.items():
                    gain = w - comm_degree[c_new] * degree[n] / m2
                    base = w_to.get(c_old, 0.0) - comm_degree[c_old] * degree[n] / m2
                    if gain - base > best_gain + 1e-12:
                        best_gain, best_c = gain - base, c_new
                comm_degree[best_c] += degree[n]
                if best_c != c_old:
                    comm[n] = best_c
                    moved += 1
            if moved == 0:
                break
            improved = True

        # write result in terms of original nodes
        for n, c in comm.items():
            for orig in node_to_orig[n]:
                result[orig] = c
        if not improved:
            break

        # aggregate: communities become super-nodes for the next level
        new_adj: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        new_orig: dict[str, list[str]] = defaultdict(list)
        for n, c in comm.items():
            new_orig[f"c{c}"].extend(node_to_orig[n])
        for n, nbrs in adj.items():
            for nb, w in nbrs.items():
                a, b = f"c{comm[n]}", f"c{comm[nb]}"
                new_adj[a][b] += w
        if len(new_adj) == len(adj):
            break
        adj = {k: dict(v) for k, v in new_adj.items()}
        node_to_orig = dict(new_orig)

    # renumber densely, ordered by community size (0 = largest)
    sizes = Counter(result.values())
    dense = {c: i for i, (c, _cnt) in
             enumerate(sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0])))}
    return {n: dense[c] for n, c in result.items()}


def _adjacency(project: str) -> dict[str, dict[str, float]]:
    """Undirected weighted adjacency from the stored graph. AMBIGUOUS edges
    weigh less so guesses don't shape the partition as much as facts."""
    adj: dict[str, dict[str, float]] = defaultdict(dict)
    weight = {codegraph.EXTRACTED: 1.0, codegraph.INFERRED: 0.7,
              codegraph.AMBIGUOUS: 0.25}
    for e in codegraph.edges(project):
        w = weight.get(e.get("tier"), 0.5)
        for a, b in ((e["src"], e["dst"]), (e["dst"], e["src"])):
            adj[a][b] = max(adj[a].get(b, 0.0), w)
    for n in codegraph.nodes(project):
        adj.setdefault(n["id"], {})
    return dict(adj)


# --- communities ----------------------------------------------------------

def compute_communities(project: str) -> dict:
    """Partition the graph, label heuristically, persist. Returns the stored
    structure: {"assign": {node_id: comm}, "labels": {comm: label}}."""
    assign = _louvain(_adjacency(project))
    nodes_by_id = {n["id"]: n for n in codegraph.nodes(project)}
    members: dict[int, list[dict]] = defaultdict(list)
    for nid, c in assign.items():
        node = nodes_by_id.get(nid)
        if node:
            members[c].append(node)

    degree = codegraph.degree(project)
    labels: dict[str, str] = {}
    for c, mems in members.items():
        paths = Counter(Path(m["path"]).parts[0] for m in mems if m.get("path"))
        top_path = paths.most_common(1)[0][0] if paths else ""
        top_sym = max(
            (m for m in mems if m["kind"] != codegraph.RATIONALE),
            key=lambda m: degree.get(m["id"], 0), default=None)
        label = " / ".join(x for x in (top_path, top_sym["label"] if top_sym
                                       else "") if x) or f"community {c}"
        labels[str(c)] = label

    data = {"assign": assign, "labels": labels}
    store.backend().put(_COMM, codegraph._key(project),
                        json.dumps(data).encode())
    return data


def communities(project: str, recompute: bool = False) -> dict:
    """Cached partition (computed on first ask or when `recompute`)."""
    if not recompute:
        blob = store.backend().get(_COMM, codegraph._key(project))
        if blob:
            try:
                return json.loads(blob)
            except (ValueError, json.JSONDecodeError):
                pass
    return compute_communities(project)


def set_label(project: str, community: int, label: str) -> None:
    """Persist a (council- or operator-chosen) community name."""
    data = communities(project)
    data["labels"][str(int(community))] = codegraph._clean_label(label)
    store.backend().put(_COMM, codegraph._key(project),
                        json.dumps(data).encode())


# --- god nodes & surprises ------------------------------------------------

def god_nodes(project: str, top_n: int = _MAX_GOD_NODES) -> list[dict]:
    """Most-connected real symbols (rationale/document nodes excluded — a
    README linked from everywhere is not an abstraction, and stdlib-shadowing
    names like `append` are call-resolution noise, not hubs)."""
    from . import codegraph_build
    degree = codegraph.degree(project)
    out = []
    for n in codegraph.nodes(project):
        if n["kind"] in (codegraph.RATIONALE, codegraph.DOCUMENT,
                         codegraph.CITATION):
            continue
        if n["label"].lower() in codegraph_build._SHADOWED:
            continue
        d = degree.get(n["id"], 0)
        if d:
            out.append({**n, "degree": d})
    out.sort(key=lambda n: (-n["degree"], n["label"]))
    return out[:top_n]


def surprises(project: str, top_n: int = _MAX_SURPRISES) -> list[dict]:
    """Cross-community edges ranked by how unexpected they are: different
    top-level directories score, code<->doc score, a peripheral node reaching a
    hub scores. Guessed edges rank below facts at equal surprise."""
    comm = communities(project)["assign"]
    nodes_by_id = {n["id"]: n for n in codegraph.nodes(project)}
    degree = codegraph.degree(project)
    god_cut = {g["id"] for g in god_nodes(project)}
    tier_rank = {codegraph.EXTRACTED: 0, codegraph.INFERRED: 1,
                 codegraph.AMBIGUOUS: 2}

    scored = []
    for e in codegraph.edges(project):
        a, b = nodes_by_id.get(e["src"]), nodes_by_id.get(e["dst"])
        if not (a and b) or codegraph.RATIONALE in (a["kind"], b["kind"]):
            continue
        if comm.get(e["src"]) == comm.get(e["dst"]):
            continue
        score, why = 1.0, ["bridges communities"]
        da = Path(a.get("path", "")).parts[:1]
        db = Path(b.get("path", "")).parts[:1]
        if da and db and da != db:
            score += 1.0
            why.append("crosses top-level directories")
        if (a["kind"] == codegraph.DOCUMENT) != (b["kind"] == codegraph.DOCUMENT):
            score += 1.0
            why.append("links docs to code")
        if (e["src"] in god_cut) != (e["dst"] in god_cut):
            small = min(degree.get(e["src"], 0), degree.get(e["dst"], 0))
            if small <= 2:
                score += 0.5
                why.append("a peripheral node reaches a hub")
        scored.append((score, tier_rank.get(e.get("tier"), 3), {
            "a": a["label"], "b": b["label"], "rel": e["rel"],
            "tier": e.get("tier", "?"), "score": round(score, 2),
            "why": ", ".join(why),
            "where": f"{a.get('path', '?')} <-> {b.get('path', '?')}"}))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]["a"]))
    # one surprise per node pair region: don't let a single hub dominate
    out, seen = [], set()
    for _s, _t, item in scored:
        k = item["a"]
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
        if len(out) >= top_n:
            break
    return out


def questions(project: str, top_n: int = 5) -> list[str]:
    """Questions this graph can answer better than a file listing can."""
    gods = god_nodes(project, 4)
    comm = communities(project)
    out = []
    if gods:
        out.append(f"What breaks if `{gods[0]['label']}` changes? "
                   f"(codegraph_impact)")
    if len(gods) >= 2:
        out.append(f"How does `{gods[0]['label']}` reach `{gods[1]['label']}`? "
                   f"(codegraph_path)")
    sizes = Counter(comm["assign"].values())
    if len(sizes) >= 2:
        big2 = [comm["labels"].get(str(c), f"community {c}")
                for c, _n in sizes.most_common(2)]
        out.append(f"Where do '{big2[0]}' and '{big2[1]}' touch? "
                   f"(the surprises list)")
    for s in surprises(project, 2):
        out.append(f"Why does {s['a']} {s['rel'].replace('_', ' ')} {s['b']}?")
    return out[:top_n]


# --- the report -----------------------------------------------------------

def render_report(project: str = "self") -> str:
    """CODEGRAPH_REPORT.md — Graphify's GRAPH_REPORT, in Olympus's voice."""
    s = codegraph.stats(project)
    if not s["nodes"]:
        return "The code graph is empty — run `olympus codegraph build` first."
    meta = codegraph.get_meta(project)
    comm = communities(project)
    tier_counts = Counter(e.get("tier", "?") for e in codegraph.edges(project))
    sizes = Counter(comm["assign"].values())

    lines = [f"# Code graph report — {project}", ""]
    if meta.get("root"):
        lines.append(f"Root: `{meta['root']}`")
    lines += [f"Nodes: {s['nodes']}  Edges: {s['edges']}  "
              f"Communities: {len(sizes)}", "",
              "Edge confidence: " + ", ".join(
                  f"{t} {tier_counts.get(t, 0)}"
                  for t in (codegraph.EXTRACTED, codegraph.INFERRED,
                            codegraph.AMBIGUOUS)),
              ""]

    lines += ["## God nodes", "",
              "The most-connected symbols — everything flows through these:", ""]
    for g in god_nodes(project):
        lines.append(f"- **{g['label']}** ({g['kind']}, degree {g['degree']}) — "
                     f"`{g.get('path', '?')}`")

    lines += ["", "## Communities", ""]
    for c, n in sizes.most_common(_MAX_COMMUNITIES_SHOWN):
        label = comm["labels"].get(str(c), f"community {c}")
        lines.append(f"- **{label}** — {n} nodes")
    if len(sizes) > _MAX_COMMUNITIES_SHOWN:
        lines.append(f"- … and {len(sizes) - _MAX_COMMUNITIES_SHOWN} more")

    surp = surprises(project)
    if surp:
        lines += ["", "## Surprising connections", ""]
        for x in surp:
            lines.append(f"- {x['a']} **{x['rel'].replace('_', ' ')}** {x['b']} "
                         f"[{x['tier']}] — {x['why']} ({x['where']})")

    qs = questions(project)
    if qs:
        lines += ["", "## Questions this graph can answer", ""]
        lines += [f"- {q}" for q in qs]

    lines += ["", "## Reading the confidence tags", "",
              "- `EXTRACTED` — parsed straight from the source; ground truth.",
              "- `INFERRED` — best single match from the regex engine; likely "
              "but not proven.",
              "- `AMBIGUOUS` — several same-named candidates; all shown, none "
              "trusted.", ""]
    return "\n".join(lines)


# --- benchmark ------------------------------------------------------------

def token_benchmark(project: str, question: str,
                    budget_tokens: int = 2000) -> dict:
    """Measured context cost of answering from the graph vs. pasting the
    corpus (chars/4 ~ tokens — Graphify's benchmark, absorbed). Keeps the
    honesty rule: numbers are measured, never asserted."""
    meta = codegraph.get_meta(project)
    root = Path(meta.get("root", "."))
    corpus_chars = 0
    if root.is_dir():
        from . import codegraph_build
        for p in codegraph_build.collect_files(root):
            try:
                corpus_chars += p.stat().st_size
            except OSError:
                pass
    sub = codegraph.subgraph_query(project, question, budget_tokens)
    sub_tokens = max(1, len(sub) // 4)
    corpus_tokens = max(1, corpus_chars // 4)
    return {"question": question, "subgraph_tokens": sub_tokens,
            "corpus_tokens": corpus_tokens,
            "reduction": round(1 - sub_tokens / corpus_tokens, 4),
            "answerable": bool(sub)}
