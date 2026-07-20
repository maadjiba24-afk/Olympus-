"""Graph-aware PR dashboard — what does each open PR actually touch?

`olympus codegraph prs` lists open pull requests and maps each one's changed
files onto the code graph: which communities it lands in, which god nodes it
touches, and — the part a diff can't show — which OTHER open PRs share those
communities (merge-order risk: two PRs editing one subsystem collide long
before git says so).

Read-only over github.py's endpoints; unconfigured (no GITHUB_TOKEN/REPO)
renders a one-line pointer instead of an error. Impact comes from the graph
alone — no LLM call, no diff download.
"""

from __future__ import annotations

from collections import Counter

from . import codegraph, codegraph_analysis, github


def pr_impact(number: int, project: str = "self",
              repo: str | None = None) -> dict:
    """One PR's graph footprint: communities and god nodes its files touch."""
    files = github.pull_files(number, repo)
    comm = codegraph_analysis.communities(project)
    nodes_ = codegraph.nodes(project)
    gods = {g["id"]: g["label"] for g in codegraph_analysis.god_nodes(project)}

    touched = [n for n in nodes_ if n.get("path") in set(files)]
    comm_counts = Counter(comm["assign"].get(n["id"]) for n in touched
                          if comm["assign"].get(n["id"]) is not None)
    return {
        "number": number,
        "files": len(files),
        "known_files": len({n["path"] for n in touched}),
        "communities": {comm["labels"].get(str(c), f"community {c}"): cnt
                        for c, cnt in comm_counts.most_common(6)},
        "community_ids": [c for c, _ in comm_counts.most_common()],
        "god_nodes": sorted({gods[n["id"]] for n in touched if n["id"] in gods}),
    }


def dashboard(project: str = "self", base: str | None = None,
              repo: str | None = None) -> str:
    """The rendered dashboard. Communities shared between PRs are flagged."""
    if not github.configured():
        return ("PR dashboard needs GITHUB_TOKEN and GITHUB_REPO set "
                "(read access is enough).")
    pulls = github.list_pulls(base, repo)
    if not pulls:
        return "No open pull requests."

    impacts = {p["number"]: pr_impact(p["number"], project, repo)
               for p in pulls}
    comm_users: dict[int, list[int]] = {}
    for num, imp in impacts.items():
        for c in imp["community_ids"]:
            comm_users.setdefault(c, []).append(num)

    lines = [f"Open pull requests ({len(pulls)}):", ""]
    for p in pulls:
        imp = impacts[p["number"]]
        flag = " [draft]" if p["draft"] else ""
        lines.append(f"#{p['number']}{flag} {p['title']} — {p['author']} "
                     f"({p['head']} -> {p['base']})")
        if imp["communities"]:
            lines.append("  touches: " + ", ".join(
                f"{label} ({cnt})" for label, cnt in imp["communities"].items()))
        if imp["god_nodes"]:
            lines.append("  god nodes: " + ", ".join(imp["god_nodes"][:6]))
        rivals = sorted({n for c in imp["community_ids"]
                         for n in comm_users.get(c, []) if n != p["number"]})
        if rivals:
            lines.append("  shares subsystems with: "
                         + ", ".join(f"#{r}" for r in rivals)
                         + "  <- merge-order risk")
        if not imp["files"]:
            lines.append("  (no file list — API limit or empty PR)")
        lines.append("")
    return "\n".join(lines).rstrip()
