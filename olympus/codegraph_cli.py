"""`olympus codegraph <action>` — the graph's whole surface from the terminal.

One subcommand, many actions, so the top-level CLI stays navigable while the
graph gets a full toolbox:

  build / update / watch        construct and maintain (multi-language + docs)
  stats / show / report         inspect
  query / path / impact / verify  ask (query is token-budgeted)
  communities / label / dedup   organize
  export                        json graphml mermaid html cypher report
                                obsidian wiki (+ --neo4j / --falkordb pushes)
  benchmark                     measured tokens: subgraph vs corpus
  prs                           graph-aware PR dashboard
  global add|remove|list        the cross-repo graph
  postgres / cargo              schema introspection into the graph

Every action prints human-readable text and returns a shell exit code; the
heavy lifting lives in the codegraph_* modules, keeping this file pure
translation like the other CLI fronts.
"""

from __future__ import annotations

from . import (codegraph, codegraph_analysis, codegraph_build,
               codegraph_dedup, codegraph_export, codegraph_global)

_EXPORT_FORMATS = ("json", "graphml", "mermaid", "html", "cypher", "report",
                   "obsidian", "wiki")

ACTIONS = ("build", "update", "watch", "stats", "show", "report", "query",
           "path", "impact", "verify", "communities", "label", "dedup",
           "export", "benchmark", "prs", "global", "postgres", "cargo")


def _arg(args, n: int = 0, default: str = "") -> str:
    extra = getattr(args, "arg", None) or []
    return extra[n] if len(extra) > n else default


def run(args) -> int:
    project = args.project
    action = args.action

    if action == "build":
        r = codegraph_build.build(project, args.root,
                                  include_docs=not args.code_only)
        codegraph_analysis.compute_communities(project)
        print(f"Built {r['nodes']} nodes / {r['edges']} edges from "
              f"{r['files']} files (calls: {r['calls_resolved']} resolved, "
              f"{r['calls_inferred']} inferred, {r['calls_ambiguous']} "
              f"ambiguous).")
        return 0

    if action == "update":
        r = codegraph_build.update(project, args.root,
                                   include_docs=not args.code_only)
        codegraph_analysis.compute_communities(project)
        if r.get("mode") == "full":
            print(f"No manifest yet — full build: {r['nodes']} nodes / "
                  f"{r['edges']} edges.")
        else:
            print(f"Updated: {r['files_changed']} changed, "
                  f"{r['files_deleted']} deleted -> {r['nodes']} nodes / "
                  f"{r['edges']} edges.")
        return 0

    if action == "watch":
        from . import codegraph_watch
        print(f"Watching {args.root} (Ctrl-C to stop)…")
        n = codegraph_watch.watch(
            project, args.root, include_docs=not args.code_only,
            on_update=lambda s: print(
                f"  rebuilt: {s.get('files_changed', '?')} changed -> "
                f"{s['nodes']} nodes / {s['edges']} edges"))
        print(f"Stopped after {n} rebuild(s).")
        return 0

    if action == "stats":
        s = codegraph.stats(project)
        meta = codegraph.get_meta(project)
        comm = codegraph_analysis.communities(project) if s["nodes"] else None
        print(f"{project}: {s['nodes']} nodes, {s['edges']} edges"
              + (f", {len(set(comm['assign'].values()))} communities"
                 if comm else "")
              + (f" (root {meta['root']})" if meta.get("root") else ""))
        return 0

    if action == "show":
        text = codegraph.describe(project, _arg(args))
        print(text or "No such symbol in the graph.")
        return 0 if text else 1

    if action == "report":
        print(codegraph_analysis.render_report(project))
        return 0

    if action == "query":
        text = codegraph.subgraph_query(project, _arg(args),
                                        budget_tokens=args.budget,
                                        dfs=args.dfs)
        print(text or "Nothing in the graph matches that question.")
        return 0 if text else 1

    if action == "path":
        a = codegraph.find(project, _arg(args, 0))
        b = codegraph.find(project, _arg(args, 1))
        if not (a and b):
            print("Both endpoints must name symbols in the graph.")
            return 1
        ids = codegraph.shortest_path(project, a[0]["id"], b[0]["id"])
        if not ids:
            print("No path between those symbols.")
            return 1
        labels = {n["id"]: n["label"] for n in codegraph.nodes(project)}
        print(" -> ".join(labels.get(i, i) for i in ids))
        return 0

    if action == "impact":
        hits = codegraph.find(project, _arg(args))
        if not hits:
            print("No such symbol in the graph.")
            return 1
        deps = codegraph.impact(project, hits[0]["id"], depth=args.depth)
        if not deps:
            print("Nothing depends on it (per the graph).")
            return 0
        for d in deps:
            print(f"- {d['label']} ({d['kind']}) — {d.get('path', '?')}")
        return 0

    if action == "verify":
        v = codegraph.verify_claim(project, _arg(args))
        print(f"{v['verdict']}: {v['detail']}")
        return 0

    if action == "communities":
        comm = codegraph_analysis.communities(project, recompute=args.force)
        from collections import Counter
        sizes = Counter(comm["assign"].values())
        for c, n in sizes.most_common():
            print(f"{c:4d}  {n:5d}  {comm['labels'].get(str(c), '')}")
        return 0

    if action == "label":
        try:
            community = int(_arg(args, 0))
        except ValueError:
            print("Usage: olympus codegraph label <community-number> <name…>")
            return 1
        name = " ".join((getattr(args, "arg", None) or [])[1:])
        if not name:
            print("Give the community a name.")
            return 1
        codegraph_analysis.set_label(project, community, name)
        print(f"Community {community} is now '{name}'.")
        return 0

    if action == "dedup":
        groups = codegraph_dedup.find_duplicates(project)
        if not groups:
            print("No near-duplicate nodes found.")
            return 0
        for g in groups:
            print("- " + "  |  ".join(n["label"][:60] for n in g))
        if args.merge:
            r = codegraph_dedup.merge_duplicates(project)
            print(f"Merged {r['merged']} node(s) across {r['groups']} "
                  f"group(s).")
        else:
            print("(run with --merge to merge each group into its "
                  "highest-degree member)")
        return 0

    if action == "export":
        formats = [f.strip() for f in (args.formats or "json,report").split(",")
                   if f.strip()]
        bad = [f for f in formats if f not in _EXPORT_FORMATS]
        if bad:
            print(f"Unknown format(s): {', '.join(bad)}. "
                  f"Choose from: {', '.join(_EXPORT_FORMATS)}")
            return 1
        out = args.out or "codegraph-out"
        written = codegraph_export.export(project, out, formats)
        for fmt, path in written.items():
            print(f"{fmt:9s} -> {path}")
        if args.neo4j:
            r = codegraph_export.push_neo4j(
                project, args.neo4j, user=args.db_user,
                password=args.db_password)
            print(f"neo4j     -> pushed {r['nodes']} nodes / {r['edges']} edges")
        if args.falkordb:
            host, _, port = args.falkordb.partition(":")
            r = codegraph_export.push_falkordb(project, host or "localhost",
                                               int(port or 6379))
            print(f"falkordb  -> pushed {r['nodes']} nodes / {r['edges']} edges")
        return 0

    if action == "benchmark":
        question = _arg(args) or "what are the main components?"
        b = codegraph_analysis.token_benchmark(project, question,
                                               budget_tokens=args.budget)
        print(f"question:        {b['question']}\n"
              f"subgraph tokens: {b['subgraph_tokens']}\n"
              f"corpus tokens:   {b['corpus_tokens']}\n"
              f"reduction:       {b['reduction']:.2%}")
        return 0

    if action == "prs":
        from . import codegraph_prs
        print(codegraph_prs.dashboard(project, base=args.base or None,
                                      repo=args.repo or None))
        return 0

    if action == "global":
        sub = _arg(args, 0)
        if sub == "add":
            r = codegraph_global.add(_arg(args, 1) or "self", project)
            print(f"Registered '{r['tag']}': {r['nodes']} nodes / "
                  f"{r['edges']} edges in the global graph.")
        elif sub == "remove":
            n = codegraph_global.remove(_arg(args, 1))
            print(f"Removed {n} node(s).")
        elif sub == "list":
            reg = codegraph_global.repos()
            if not reg:
                print("The global graph is empty.")
            for tag, counts in sorted(reg.items()):
                print(f"- {tag}: {counts['nodes']} nodes / "
                      f"{counts['edges']} edges")
        else:
            print("Usage: olympus codegraph global add <tag> | remove <tag> "
                  "| list")
            return 1
        return 0

    if action == "postgres":
        from . import codegraph_introspect
        dsn = _arg(args)
        if not dsn:
            print("Usage: olympus codegraph postgres <dsn>")
            return 1
        try:
            r = codegraph_introspect.introspect_postgres(project, dsn)
        except RuntimeError as err:
            print(err)
            return 1
        print(f"Mapped {r['tables']} tables/views, {r['foreign_keys']} "
              f"foreign keys.")
        return 0

    if action == "cargo":
        from . import codegraph_introspect
        r = codegraph_introspect.introspect_cargo(project,
                                                  _arg(args) or args.root)
        print(f"Mapped {r['crates']} crates, {r['depends_on']} dependency "
              f"edges.")
        return 0

    print(f"Unknown codegraph action: {action}")
    return 1
