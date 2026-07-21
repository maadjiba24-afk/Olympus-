"""Graph exports — the same graph, in every shape people actually use.

Everything here is stdlib string generation writing UNDER a caller-chosen
output directory; the two database pushes lazy-import their drivers (extras),
matching how store.py treats psycopg. Formats:

  to_node_link()  the canonical dict (graph.json shape; d3/networkx-compatible)
  to_graphml()    Gephi / yEd
  to_mermaid()    architecture diagram, communities as subgraphs (markdown-able)
  to_html()       ONE self-contained file: embedded data + a small canvas
                  force layout, community colors, pan/zoom/hover. No CDN, no
                  external requests — the CSP Graphify needs, we get by
                  construction.
  to_obsidian()   a vault folder: one note per node, [[wikilinks]] for edges
  to_wiki()       agent-crawlable markdown wiki: index + per-community pages
                  + god-node pages
  to_cypher()     MERGE statements (works for Neo4j and FalkorDB)
  push_neo4j() / push_falkordb()  live pushes via optional drivers

Labels in every format pass through codegraph's sanitizer plus per-format
escaping — an injection-shaped docstring must not become executable Cypher or
live HTML any more than it may become a prompt.
"""

from __future__ import annotations

import html as _html
import json
import re
from collections import Counter
from pathlib import Path

from . import codegraph, codegraph_analysis

_MAX_VIZ_NODES = 4000        # beyond this a force layout is soup anyway
_MAX_MERMAID_NODES = 150
_MAX_VAULT_NOTES = 2000


def _graph(project: str) -> tuple[list[dict], list[dict]]:
    return codegraph.nodes(project), codegraph.edges(project)


def to_node_link(project: str) -> dict:
    """The canonical interchange dict (graph.json)."""
    nodes_, edges_ = _graph(project)
    comm = codegraph_analysis.communities(project)
    return {
        "directed": True,
        "meta": codegraph.get_meta(project),
        "communities": comm["labels"],
        "nodes": [{**n, "community": comm["assign"].get(n["id"])}
                  for n in nodes_],
        "edges": edges_,
    }


# --- graphml --------------------------------------------------------------

def _x(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)


def to_graphml(project: str) -> str:
    nodes_, edges_ = _graph(project)
    comm = codegraph_analysis.communities(project)["assign"]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
           '  <key id="label" for="node" attr.name="label" attr.type="string"/>',
           '  <key id="kind" for="node" attr.name="kind" attr.type="string"/>',
           '  <key id="path" for="node" attr.name="path" attr.type="string"/>',
           '  <key id="community" for="node" attr.name="community" attr.type="int"/>',
           '  <key id="rel" for="edge" attr.name="relation" attr.type="string"/>',
           '  <key id="tier" for="edge" attr.name="tier" attr.type="string"/>',
           '  <graph edgedefault="directed">']
    for n in nodes_:
        out.append(f'    <node id="{_x(n["id"])}">'
                   f'<data key="label">{_x(n["label"])}</data>'
                   f'<data key="kind">{_x(n["kind"])}</data>'
                   f'<data key="path">{_x(n.get("path"))}</data>'
                   f'<data key="community">{comm.get(n["id"], -1)}</data>'
                   f'</node>')
    for e in edges_:
        out.append(f'    <edge source="{_x(e["src"])}" target="{_x(e["dst"])}">'
                   f'<data key="rel">{_x(e["rel"])}</data>'
                   f'<data key="tier">{_x(e.get("tier"))}</data></edge>')
    out += ["  </graph>", "</graphml>", ""]
    return "\n".join(out)


# --- mermaid --------------------------------------------------------------

def _mid(nid: str) -> str:
    return "n" + re.sub(r"[^A-Za-z0-9]", "", nid)[:12]


def to_mermaid(project: str, max_nodes: int = _MAX_MERMAID_NODES) -> str:
    """Top-degree slice of the graph as a mermaid flowchart, communities as
    subgraphs — small enough to read, real enough to be true."""
    nodes_, edges_ = _graph(project)
    comm = codegraph_analysis.communities(project)
    degree = codegraph.degree(project)
    keep = sorted((n for n in nodes_ if n["kind"] != codegraph.RATIONALE),
                  key=lambda n: -degree.get(n["id"], 0))[:max_nodes]
    keep_ids = {n["id"] for n in keep}

    by_comm: dict[int, list[dict]] = {}
    for n in keep:
        by_comm.setdefault(comm["assign"].get(n["id"], -1), []).append(n)

    out = ["flowchart LR"]
    for c in sorted(by_comm):
        label = comm["labels"].get(str(c), f"community {c}")
        safe = re.sub(r"[^\w /.-]", "", label)[:40] or f"community {c}"
        out.append(f'  subgraph c{c}["{safe}"]')
        for n in by_comm[c]:
            lab = re.sub(r"[^\w .:-]", "", n["label"])[:30]
            out.append(f'    {_mid(n["id"])}["{lab}"]')
        out.append("  end")
    seen = set()
    for e in edges_:
        if e["src"] in keep_ids and e["dst"] in keep_ids:
            key = (e["src"], e["dst"])
            if key in seen or e["rel"] == "defines":
                continue
            seen.add(key)
            arrow = "-->" if e.get("tier") == codegraph.EXTRACTED else "-.->"
            out.append(f'  {_mid(e["src"])} {arrow}|{e["rel"]}| {_mid(e["dst"])}')
    return "\n".join(out) + "\n"


# --- html (self-contained) ------------------------------------------------

_HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__TITLE__ — code graph</title>
<style>
 body{margin:0;font:13px system-ui,sans-serif;background:#111;color:#ddd}
 #hud{position:fixed;top:8px;left:8px;background:#000a;padding:8px 12px;
      border-radius:6px;max-width:44ch}
 #hud b{color:#fff} canvas{display:block}
</style></head><body>
<div id="hud"><b>__TITLE__</b> — drag to pan, wheel to zoom, hover for names.
<div id="who"></div></div>
<canvas id="c"></canvas>
<script id="data" type="application/json">__DATA__</script>
<script>
const G=JSON.parse(document.getElementById('data').textContent);
const cv=document.getElementById('c'),cx=cv.getContext('2d');
let W,H;function rs(){W=cv.width=innerWidth;H=cv.height=innerHeight}rs();
onresize=rs;
const N=G.nodes,E=G.edges,idx={};N.forEach((n,i)=>idx[n.id]=i);
const deg=new Float32Array(N.length);
const EL=[];for(const e of E){const a=idx[e.src],b=idx[e.dst];
 if(a===undefined||b===undefined)continue;EL.push([a,b,e.tier]);deg[a]++;deg[b]++}
const px=new Float32Array(N.length),py=new Float32Array(N.length),
      vx=new Float32Array(N.length),vy=new Float32Array(N.length);
for(let i=0;i<N.length;i++){const c=(N[i].community||0);
 const a=(i*2.399963)+c,r=80+40*Math.sqrt(i%97)+120*(c%7);
 px[i]=Math.cos(a)*r;py[i]=Math.sin(a)*r}
const PAL=['#e6a23c','#5bc0de','#7bd88f','#d87bb2','#b0a1e6','#e66e6e',
 '#6ee6d8','#e6dd6e','#9fe66e','#6e9fe6','#e69f6e','#c2c2c2'];
let tx=0,ty=0,scale=1,drag=null,hover=-1,ticks=0;
function step(){
 if(ticks++>400)return;
 for(let i=0;i<N.length;i++){vx[i]*=.85;vy[i]*=.85;
  vx[i]-=px[i]*8e-4;vy[i]-=py[i]*8e-4}
 // grid-bucketed repulsion (cheap approximation)
 const cell=90,map=new Map();
 for(let i=0;i<N.length;i++){const k=((px[i]/cell)|0)+':'+((py[i]/cell)|0);
  (map.get(k)||map.set(k,[]).get(k)).push(i)}
 for(const arr of map.values())for(let a=0;a<arr.length;a++)
  for(let b=a+1;b<Math.min(arr.length,a+12);b++){const i=arr[a],j=arr[b];
   let dx=px[i]-px[j],dy=py[i]-py[j],d2=dx*dx+dy*dy+1,f=240/d2;
   dx*=f;dy*=f;vx[i]+=dx;vy[i]+=dy;vx[j]-=dx;vy[j]-=dy}
 for(const[a,b]of EL){let dx=px[b]-px[a],dy=py[b]-py[a];
  const d=Math.sqrt(dx*dx+dy*dy)+.1,f=(d-60)*.004/d;
  dx*=f;dy*=f;vx[a]+=dx;vy[a]+=dy;vx[b]-=dx;vy[b]-=dy}
 for(let i=0;i<N.length;i++){px[i]+=vx[i];py[i]+=vy[i]}
}
function draw(){
 cx.setTransform(1,0,0,1,0,0);cx.clearRect(0,0,W,H);
 cx.setTransform(scale,0,0,scale,W/2+tx,H/2+ty);
 cx.lineWidth=.4/scale;
 for(const[a,b,t]of EL){cx.strokeStyle=t==='EXTRACTED'?'#666':'#3a3a55';
  cx.beginPath();cx.moveTo(px[a],py[a]);cx.lineTo(px[b],py[b]);cx.stroke()}
 for(let i=0;i<N.length;i++){const r=2+Math.sqrt(deg[i]);
  cx.fillStyle=PAL[(N[i].community||0)%PAL.length];
  cx.beginPath();cx.arc(px[i],py[i],r,0,7);cx.fill()}
 if(hover>=0){const n=N[hover];cx.fillStyle='#fff';
  cx.font=(12/scale)+'px sans-serif';
  cx.fillText(n.label+' ('+n.kind+')',px[hover]+6/scale,py[hover])}
}
function loop(){step();draw();requestAnimationFrame(loop)}loop();
cv.onmousedown=e=>drag=[e.clientX,e.clientY];
onmouseup=()=>drag=null;
cv.onmousemove=e=>{if(drag){tx+=e.clientX-drag[0];ty+=e.clientY-drag[1];
  drag=[e.clientX,e.clientY];return}
 const mx=(e.clientX-W/2-tx)/scale,my=(e.clientY-H/2-ty)/scale;hover=-1;
 for(let i=0;i<N.length;i++){const dx=px[i]-mx,dy=py[i]-my;
  if(dx*dx+dy*dy<36){hover=i;break}}
 document.getElementById('who').textContent=
  hover>=0?N[hover].label+' — '+(N[hover].path||''):''};
cv.onwheel=e=>{e.preventDefault();scale*=e.deltaY<0?1.1:.9};
</script></body></html>
"""


def to_html(project: str) -> str:
    data = to_node_link(project)
    nodes_ = [n for n in data["nodes"] if n["kind"] != codegraph.RATIONALE]
    if len(nodes_) > _MAX_VIZ_NODES:
        degree = codegraph.degree(project)
        nodes_ = sorted(nodes_, key=lambda n: -degree.get(n["id"], 0))
        nodes_ = nodes_[:_MAX_VIZ_NODES]
    keep = {n["id"] for n in nodes_}
    slim = {
        "nodes": [{"id": n["id"], "label": n["label"], "kind": n["kind"],
                   "path": n.get("path"), "community": n.get("community")}
                  for n in nodes_],
        "edges": [{"src": e["src"], "dst": e["dst"], "tier": e.get("tier")}
                  for e in data["edges"]
                  if e["src"] in keep and e["dst"] in keep],
    }
    # Neutralize every way the JSON payload could break out of its <script>
    # container: `</script>` (via `</`), an HTML comment open, and a nested
    # `<script`. Labels are attacker-influenced (function names, doc titles).
    payload = (json.dumps(slim).replace("</", "<\\/")
               .replace("<!--", "<\\!--").replace("<script", "<\\script"))
    return (_HTML_PAGE
            .replace("__TITLE__", _x(project))
            .replace("__DATA__", payload))


# --- obsidian vault -------------------------------------------------------

def _note_name(n: dict) -> str:
    base = re.sub(r"[^\w.-]+", " ", f"{n['label']} {n['id'][:6]}").strip()
    return base[:80] or n["id"]


def to_obsidian(project: str, vault_dir: str | Path) -> dict:
    """One markdown note per node (bounded), wikilinked along the edges, plus
    an index. Never touches files it did not write: everything goes under
    <vault>/codegraph-<project>/ — pointing this at a live vault is safe."""
    vault = Path(vault_dir) / f"codegraph-{codegraph._key(project)}"
    vault.mkdir(parents=True, exist_ok=True)
    nodes_, edges_ = _graph(project)
    comm = codegraph_analysis.communities(project)
    degree = codegraph.degree(project)
    keep = sorted((n for n in nodes_ if n["kind"] != codegraph.RATIONALE),
                  key=lambda n: -degree.get(n["id"], 0))[:_MAX_VAULT_NOTES]
    names = {n["id"]: _note_name(n) for n in keep}

    adj: dict[str, list[tuple[str, str, bool]]] = {}
    for e in edges_:
        if e["src"] in names and e["dst"] in names:
            adj.setdefault(e["src"], []).append((e["rel"], e["dst"], True))
            adj.setdefault(e["dst"], []).append((e["rel"], e["src"], False))

    for n in keep:
        c = comm["assign"].get(n["id"])
        lines = ["---", f"kind: {n['kind']}",
                 f"path: {json.dumps(n.get('path') or '')}",
                 f"community: {json.dumps(comm['labels'].get(str(c), ''))}",
                 "---", "", f"# {n['label']}", ""]
        for rel, other, outgoing in sorted(adj.get(n["id"], []))[:40]:
            arrow = rel.replace("_", " ")
            link = f"[[{names[other]}]]"
            lines.append(f"- {arrow} {link}" if outgoing
                         else f"- {link} {arrow} this")
        (vault / f"{names[n['id']]}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    index = ["# Code graph index", ""]
    for g in codegraph_analysis.god_nodes(project):
        if g["id"] in names:
            index.append(f"- [[{names[g['id']]}]] (degree {g['degree']})")
    (vault / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return {"notes": len(keep) + 1, "dir": str(vault)}


# --- wiki -----------------------------------------------------------------

def to_wiki(project: str, out_dir: str | Path) -> dict:
    """Agent-crawlable wiki: index.md, one page per community, one page per
    god node. Pages cross-link with plain relative markdown links, so any
    agent (or human) can navigate without the graph tooling."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    nodes_, edges_ = _graph(project)
    by_id = {n["id"]: n for n in nodes_}
    comm = codegraph_analysis.communities(project)
    degree = codegraph.degree(project)
    gods = codegraph_analysis.god_nodes(project)

    sizes = Counter(comm["assign"].values())
    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "x"

    comm_pages = {}
    for c, size in sizes.most_common():
        label = comm["labels"].get(str(c), f"community {c}")
        slug = f"community-{_slug(label)}-{c}"
        comm_pages[c] = (slug, label, size)

    index = [f"# {project} — knowledge wiki", "",
             "Generated from the code graph. Start here.", "",
             "## Subsystems", ""]
    for c, (slug, label, size) in comm_pages.items():
        index.append(f"- [{label}]({slug}.md) — {size} nodes")
    index += ["", "## Key symbols", ""]
    for g in gods:
        index.append(f"- [{g['label']}](symbol-{_slug(g['label'])}.md) "
                     f"(degree {g['degree']})")
    (out / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    members: dict[int, list[dict]] = {}
    for nid, c in comm["assign"].items():
        n = by_id.get(nid)
        if n and n["kind"] != codegraph.RATIONALE:
            members.setdefault(c, []).append(n)
    for c, (slug, label, _size) in comm_pages.items():
        mems = sorted(members.get(c, []),
                      key=lambda n: -degree.get(n["id"], 0))
        lines = [f"# {label}", "", f"[index](index.md)", "",
                 f"{len(mems)} symbols. Top members:", ""]
        for m in mems[:30]:
            lines.append(f"- **{m['label']}** ({m['kind']}) — "
                         f"`{m.get('path', '?')}`")
        (out / f"{slug}.md").write_text("\n".join(lines) + "\n",
                                        encoding="utf-8")

    for g in gods:
        lines = [f"# {g['label']}", "", f"[index](index.md)", "",
                 f"{g['kind']} in `{g.get('path', '?')}`, degree "
                 f"{g['degree']}.", "", "## Connections", ""]
        for e in edges_:
            if e["src"] == g["id"] or e["dst"] == g["id"]:
                other = by_id.get(e["dst"] if e["src"] == g["id"] else e["src"])
                if other and other["kind"] != codegraph.RATIONALE:
                    d = ("-> " + other["label"] if e["src"] == g["id"]
                         else other["label"] + " ->")
                    lines.append(f"- {d} ({e['rel']}, {e.get('tier', '?')})")
            if len(lines) > 80:
                break
        (out / f"symbol-{_slug(g['label'])}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
    return {"pages": 1 + len(comm_pages) + len(gods), "dir": str(out)}


# --- cypher / graph databases --------------------------------------------

def _cy(s) -> str:
    return str(s if s is not None else "").replace("\\", "\\\\").replace(
        "'", "\\'")


def to_cypher(project: str) -> str:
    nodes_, edges_ = _graph(project)
    comm = codegraph_analysis.communities(project)["assign"]
    out = []
    for n in nodes_:
        kind = re.sub(r"[^A-Za-z]", "", n["kind"]).capitalize() or "Node"
        out.append(
            f"MERGE (n:{kind} {{id: '{_cy(n['id'])}'}}) "
            f"SET n.label = '{_cy(n['label'])}', "
            f"n.path = '{_cy(n.get('path'))}', "
            f"n.community = {comm.get(n['id'], -1)};")
    for e in edges_:
        rel = re.sub(r"[^A-Za-z_]", "", e["rel"]).upper() or "RELATES"
        out.append(
            f"MATCH (a {{id: '{_cy(e['src'])}'}}), (b {{id: '{_cy(e['dst'])}'}}) "
            f"MERGE (a)-[r:{rel}]->(b) "
            f"SET r.tier = '{_cy(e.get('tier'))}', "
            f"r.confidence = {float(e.get('confidence', 0.0))};")
    return "\n".join(out) + "\n"


def push_neo4j(project: str, uri: str, user: str = "neo4j",
               password: str = "") -> dict:
    """Live push via the optional neo4j driver (pip install neo4j)."""
    try:
        from neo4j import GraphDatabase
    except ImportError as err:
        raise RuntimeError("the neo4j driver is not installed — "
                           "pip install neo4j") from err
    nodes_, edges_ = _graph(project)
    comm = codegraph_analysis.communities(project)["assign"]
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        with driver.session() as session:
            for n in nodes_:
                session.run(
                    "MERGE (x:CodeNode {id: $id}) SET x.label = $label, "
                    "x.kind = $kind, x.path = $path, x.community = $comm",
                    id=n["id"], label=n["label"], kind=n["kind"],
                    path=n.get("path") or "", comm=comm.get(n["id"], -1))
            for e in edges_:
                rel = re.sub(r"[^A-Za-z_]", "", e["rel"]).upper() or "RELATES"
                session.run(
                    f"MATCH (a:CodeNode {{id: $src}}), (b:CodeNode {{id: $dst}}) "
                    f"MERGE (a)-[r:{rel}]->(b) SET r.tier = $tier",
                    src=e["src"], dst=e["dst"], tier=e.get("tier") or "")
    return {"nodes": len(nodes_), "edges": len(edges_)}


def push_falkordb(project: str, host: str = "localhost", port: int = 6379,
                  graph_name: str | None = None) -> dict:
    """Live push via the optional falkordb driver (pip install falkordb)."""
    try:
        from falkordb import FalkorDB
    except ImportError as err:
        raise RuntimeError("the falkordb driver is not installed — "
                           "pip install falkordb") from err
    nodes_, edges_ = _graph(project)
    comm = codegraph_analysis.communities(project)["assign"]
    g = FalkorDB(host=host, port=port).select_graph(
        graph_name or f"codegraph_{codegraph._key(project)}")
    for n in nodes_:
        g.query("MERGE (x:CodeNode {id: $id}) SET x.label = $label, "
                "x.kind = $kind, x.path = $path, x.community = $comm",
                {"id": n["id"], "label": n["label"], "kind": n["kind"],
                 "path": n.get("path") or "", "comm": comm.get(n["id"], -1)})
    for e in edges_:
        rel = re.sub(r"[^A-Za-z_]", "", e["rel"]).upper() or "RELATES"
        g.query(f"MATCH (a:CodeNode {{id: $src}}), (b:CodeNode {{id: $dst}}) "
                f"MERGE (a)-[r:{rel}]->(b) SET r.tier = $tier",
                {"src": e["src"], "dst": e["dst"], "tier": e.get("tier") or ""})
    return {"nodes": len(nodes_), "edges": len(edges_)}


# --- one-call export ------------------------------------------------------

def export(project: str, out_dir: str | Path, formats: list[str]) -> dict:
    """Write the requested formats under out_dir; returns {format: path}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for fmt in formats:
        if fmt == "json":
            p = out / "graph.json"
            p.write_text(json.dumps(to_node_link(project), indent=1),
                         encoding="utf-8")
        elif fmt == "graphml":
            p = out / "graph.graphml"
            p.write_text(to_graphml(project), encoding="utf-8")
        elif fmt == "mermaid":
            p = out / "graph.mmd"
            p.write_text(to_mermaid(project), encoding="utf-8")
        elif fmt == "html":
            p = out / "graph.html"
            p.write_text(to_html(project), encoding="utf-8")
        elif fmt == "cypher":
            p = out / "cypher.txt"
            p.write_text(to_cypher(project), encoding="utf-8")
        elif fmt == "report":
            p = out / "CODEGRAPH_REPORT.md"
            p.write_text(codegraph_analysis.render_report(project),
                         encoding="utf-8")
        elif fmt == "obsidian":
            p = Path(to_obsidian(project, out)["dir"])
        elif fmt == "wiki":
            p = Path(to_wiki(project, out / "wiki")["dir"])
        else:
            raise ValueError(f"unknown export format: {fmt}")
        written[fmt] = str(p)
    return written
