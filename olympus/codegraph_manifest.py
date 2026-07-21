"""Package manifests into the graph — one canonical node per package.

Graphify's manifest-ingest insight, absorbed: a `pyproject.toml`/`package.json`/
`go.mod`/`pom.xml` declares a package and its dependencies, and the SAME package
named from ten different manifests should be ONE hub node, not ten file-anchored
copies. So package nodes are keyed by NAME (synthetic path `@packages`), giving a
stable id per package: "what depends on `requests`?" becomes a graph query, and a
shared dependency shows up as the hub it is.

Declared package and each dependency become `entity` nodes; the manifest's
package gets a `depends_on` edge to each dependency. Everything is parsed with
the stdlib (json + line regexes; `tomllib` when present, a `tomli` or pure-regex
fallback on Python 3.10 where `tomllib` does not exist) — no new dependency, and
`pom.xml` is read with a small regex rather than an XML parser (only artifact
ids are needed).
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
    except ModuleNotFoundError:
        try:                                     # Python 3.10 has no tomllib
            import tomli
            return tomli.loads(text)
        except Exception:
            return {}
    except Exception:
        return {}


def _dep_name(spec: str) -> str:
    """Package name out of a requirement string: 'requests>=2,<3' -> 'requests',
    'foo[extra]==1' -> 'foo'."""
    m = re.match(r"[A-Za-z0-9._-]+", str(spec).strip())
    return m.group(0) if m else ""


def _pyproject_fields(text: str) -> tuple[str, list[str]]:
    """(package name, dependency names) from a pyproject.toml. Uses a real TOML
    parser when one is importable (tomllib on 3.11+, tomli on 3.10); otherwise
    a zero-dependency regex fallback, so the manifest path never silently loses
    dependencies on a Python without tomllib (the bug that slipped past a
    3.11-only local run and failed CI on 3.10)."""
    data = _parse_toml(text)
    if data:
        proj = data.get("project") or {}
        name = str(proj.get("name") or "")
        deps = [_dep_name(d) for d in (proj.get("dependencies") or [])]
        return name, [d for d in deps if d][:_MAX_DEPS]
    # Fallback: [project].name and its dependencies array, by regex.
    name = ""
    nm = re.search(r'(?m)^\s*name\s*=\s*["\']([^"\']+)["\']', text)
    if nm:
        name = nm.group(1)
    deps: list[str] = []
    dm = re.search(r'(?m)^\s*dependencies\s*=\s*\[', text)
    if dm:
        # Walk to the matching ']' by bracket depth — a dependency's own extras
        # (`foo[extra]`) contain balanced brackets, so a non-greedy `.*?]` would
        # stop early and drop that dependency.
        start = dm.end() - 1                     # the '['
        depth, arr = 0, ""
        for j in range(start, min(len(text), start + 20000)):
            c = text[j]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    arr = text[start + 1:j]
                    break
        else:
            arr = text[start + 1:start + 20000]
        for s in re.findall(r'["\']([^"\']+)["\']', arr):
            d = _dep_name(s)
            if d:
                deps.append(d)
    return name, deps[:_MAX_DEPS]


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
        pkg_name, deps = _pyproject_fields(text)
        pkg_name = pkg_name or path.parent.name
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
