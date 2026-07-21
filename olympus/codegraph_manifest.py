"""Package manifests into the graph — one canonical node per package.

Graphify's manifest-ingest insight, absorbed: a `pyproject.toml`/`package.json`/
`go.mod`/`pom.xml` declares a package and its dependencies, and the SAME package
named from ten different manifests should be ONE hub node, not ten file-anchored
copies. So package nodes are keyed by NAME (synthetic path `@packages`), giving a
stable id per package: "what depends on `requests`?" becomes a graph query, and a
shared dependency shows up as the hub it is.

Declared package and each dependency become `entity` nodes; the manifest's
package gets a `depends_on` edge to each dependency. Everything is parsed with
the stdlib (tomllib/json + line regexes) — no new dependency, and `pom.xml` is
read with a small regex rather than an XML parser (we only need artifact ids).
Dependency names are the manifest's own declared strings — first-partyish, but
still routed through the same node-label sanitizer as everything else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import codegraph

MANIFEST_NAMES = frozenset({"pyproject.toml", "package.json", "go.mod",
                            "pom.xml"})

_PKG_PATH = "@packages"          # synthetic path → node id is stable per name
_MAX_DEPS = 500                  # bound a hostile manifest


def pkg_node(project: str, name: str) -> dict | None:
    """Canonical package node, keyed by NAME (synthetic `@packages` path) so
    every mention of a package collapses onto one hub."""
    name = codegraph._clean_label(name)
    if not name:
        return None
    return codegraph.add_node(project, _PKG_PATH, name, kind=codegraph.ENTITY)


def _parse_toml(text: str) -> dict:
    try:
        import tomllib
        return tomllib.loads(text)
    except Exception:
        return {}


def extract_file(project: str, path: Path, root: Path) -> dict | None:
    """One manifest → its canonical package node here, and the dependency NAMES
    returned in `manifest_deps` for the shared resolver to turn into
    `depends_on` edges. Routing edges through the resolver keeps manifests on
    the same incremental-update contract as imports: on `update()` all
    `depends_on` edges are cleared and rebuilt from the full manifest, so a
    dependency removed from a manifest doesn't linger."""
    name = path.name.lower()
    rel = str(path.relative_to(root))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    pkg_name = ""
    deps: list[str] = []
    if name == "pyproject.toml":
        data = _parse_toml(text)
        proj = data.get("project") or {}
        pkg_name = str(proj.get("name") or path.parent.name)
        for dep in (proj.get("dependencies") or [])[:_MAX_DEPS]:
            m = re.match(r"[A-Za-z0-9._-]+", str(dep))
            if m:
                deps.append(m.group(0))
    elif name == "package.json":
        try:
            data = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            data = {}
        pkg_name = str(data.get("name") or path.parent.name)
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            block = data.get(key)
            if isinstance(block, dict):
                deps.extend(list(block)[:_MAX_DEPS])
    elif name == "go.mod":
        mod = re.search(r"^\s*module\s+(\S+)", text, re.M)
        pkg_name = (mod.group(1).rsplit("/", 1)[-1] if mod else path.parent.name)
        for m in re.finditer(r"^\s*(?:require\s+)?([\w./-]+)\s+v[\w.\-+]+",
                             text, re.M):
            dep = m.group(1)
            if dep in ("require", "module", "go", "toolchain"):
                continue
            deps.append(dep.rsplit("/", 1)[-1])
            if len(deps) >= _MAX_DEPS:
                break
    elif name == "pom.xml":
        # First <artifactId> is the project's; the rest are dependencies.
        arts = re.findall(r"<artifactId>\s*([\w.\-]+)\s*</artifactId>", text)
        if arts:
            pkg_name = arts[0]
            deps = arts[1:_MAX_DEPS + 1]

    owner = pkg_node(project, pkg_name or path.parent.name)
    if owner is None:
        return None
    clean_deps: list[str] = []
    seen: set[str] = set()
    for dep in deps:
        c = codegraph._clean_label(dep)
        if c and c.lower() not in seen:
            seen.add(c.lower())
            clean_deps.append(c)

    return {"qual": owner["label"], "stem": path.stem, "module_id": owner["id"],
            "rel": rel, "imports": [], "calls": [], "inherits": [],
            "manifest_deps": clean_deps, "lang": "manifest"}
