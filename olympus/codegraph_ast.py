"""Python extractor — stdlib `ast`, zero dependencies.

Walks a tree of Python files and populates `codegraph` with EXTRACTED
(confidence 1.0) structure: modules, classes, functions/methods, `defines`,
`imports` (to sibling/project modules), and best-effort `calls` edges resolved
to known symbols in the same project. Unresolved external/stdlib calls are
skipped HERE — `codegraph_build`'s shared resolution pass is where cross-file
calls gain INFERRED/AMBIGUOUS edges; Python's own unique matches stay
EXTRACTED because this parser really saw the call.

This began as the Phase 0 spike and is now the Python front of the
multi-language pipeline: codegraph_build orchestrates it alongside
codegraph_langs (regex engine, ~20 languages) and codegraph_ingest (documents).
`build()` below remains the Python-only fast path (the A/B gate measures it);
`codegraph_build.build()` is the full-corpus entry.

Trust: code from the user's own repo is trusted input (like `read_source_file`),
but docstrings/comments can carry prompt injection, so all rationale text goes
through `codegraph.add_rationale` (which sanitizes). The extractor never
executes the code it parses.
"""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path

from . import codegraph

_IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
                "build", "dist", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def _iter_py_files(root: Path, ignore_dirs: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def _module_qual(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts) if parts else rel.stem


def _callee_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _base_name(base) -> str | None:
    """Last-component name of a base-class expression: `Foo` -> 'Foo',
    `pkg.Base` -> 'Base', `Generic[T]` -> 'Generic'. Subscripts/others yield
    None (nothing resolvable)."""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    return None


class _Walk(ast.NodeVisitor):
    """One pass over a module: create class/function/method nodes + `defines`
    edges, and collect raw imports and call sites for later resolution."""

    def __init__(self, project: str, rel_path: str, module_id: str,
                 module_qual: str):
        self.project = project
        self.rel_path = rel_path
        self.module_id = module_id
        self.module_qual = module_qual
        self.scope = [(module_id, module_qual, codegraph.MODULE)]
        self.imports: list[str] = []              # imported module stems
        self.calls: list[tuple[str, str]] = []    # (caller_id, callee_name)
        self.inherits: list[tuple[str, str]] = []  # (class_id, base_name)

    # -- defs ------------------------------------------------------------
    def _add_def(self, node, kind: str):
        parent_id, parent_qual, _ = self.scope[-1]
        qual = f"{parent_qual}.{node.name}"
        n = codegraph.add_node(
            self.project, self.rel_path, qual, kind=kind,
            span=[node.lineno, getattr(node, "end_lineno", node.lineno)])
        if n is None:                    # graph at the node cap — skip cleanly
            self.generic_visit(node)
            return
        codegraph.add_edge(self.project, parent_id, "defines", n["id"])
        if kind == codegraph.CLASS:      # record explicit base classes
            for base in getattr(node, "bases", ()):
                name = _base_name(base)
                if name:
                    self.inherits.append((n["id"], name))
        doc = ast.get_docstring(node)
        if doc:
            codegraph.add_rationale(self.project, self.rel_path, n["id"], doc)
        self.scope.append((n["id"], qual, kind))
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node):
        self._add_def(node, codegraph.CLASS)

    def visit_FunctionDef(self, node):
        parent_kind = self.scope[-1][2]
        kind = (codegraph.METHOD if parent_kind == codegraph.CLASS
                else codegraph.FUNCTION)
        self._add_def(node, kind)

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- imports ---------------------------------------------------------
    # Record the FULL dotted target(s), not just the stem: `from b.util import
    # x` where both a/util.py and b/util.py exist must resolve to b.util, which
    # the stem "util" cannot disambiguate. `_resolve` matches a full dotted
    # module qualname first (EXTRACTED), then falls back to the stem. Both the
    # dotted path and its last component are recorded so either can match.
    def visit_Import(self, node):
        for alias in node.names:            # import a.b.c  /  import a.b as x
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:                     # from a.b import c, d
            self.imports.append(node.module)
            for alias in node.names:        # c/d may be submodules of a.b
                self.imports.append(f"{node.module}.{alias.name}")
        else:                               # from . import memory, store
            for alias in node.names:
                self.imports.append(alias.name)
        self.generic_visit(node)

    # -- calls -----------------------------------------------------------
    def visit_Call(self, node):
        name = _callee_name(node)
        if name:
            self.calls.append((self.scope[-1][0], name))
        self.generic_visit(node)


def extract_file(project: str, path: Path, root: Path):
    """Pass A for one file: returns its module info, or None if unparseable."""
    rel = str(path.relative_to(root))
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel)
    except (SyntaxError, ValueError, OSError):
        return None
    qual = _module_qual(path, root)
    mod = codegraph.add_node(project, rel, qual, kind=codegraph.MODULE)
    if mod is None:                          # graph at the node cap
        return None
    codegraph.add_citations(project, rel, mod["id"], src)   # ADR/RFC refs
    doc = ast.get_docstring(tree)
    if doc:
        codegraph.add_rationale(project, rel, mod["id"], doc)
    w = _Walk(project, rel, mod["id"], qual)
    w.visit(tree)
    return {"qual": qual, "stem": path.stem, "module_id": mod["id"],
            "inherits": w.inherits,
            "rel": rel, "imports": w.imports, "calls": w.calls}


def build(project: str = "self", root: str = ".",
          ignore_dirs: set[str] | None = None,
          commit_sha: str | None = None) -> dict:
    """Full rebuild: clear, walk every .py file, then resolve imports and calls
    to project-internal nodes. Returns a stats dict."""
    root_p = Path(root).resolve()
    ignore = (ignore_dirs or set()) | _IGNORE_DIRS
    codegraph.clear(project)

    with codegraph.bulk(project):
        return _build_locked(project, root_p, ignore, commit_sha)


def _build_locked(project: str, root_p: Path, ignore: set[str],
                  commit_sha: str | None) -> dict:
    collected = []
    for path in _iter_py_files(root_p, ignore):
        info = extract_file(project, path, root_p)
        if info:
            collected.append(info)

    nodes_ = codegraph.nodes(project)
    # index function/method nodes by label for call resolution
    func_index: dict[str, list[dict]] = {}
    for n in nodes_:
        if n["kind"] in (codegraph.FUNCTION, codegraph.METHOD):
            func_index.setdefault(n["label"].lower(), []).append(n)
    # index module nodes by stem (last path component) for import resolution
    module_index: dict[str, str] = {}
    for info in collected:
        module_index[info["stem"].lower()] = info["module_id"]

    imports_added = calls_resolved = calls_skipped = 0
    for info in collected:
        for stem in info["imports"]:
            tgt = module_index.get(stem.lower())
            if tgt and tgt != info["module_id"]:
                if codegraph.add_edge(project, info["module_id"], "imports", tgt):
                    imports_added += 1
        for caller_id, callee in info["calls"]:
            cands = func_index.get(callee.lower(), [])
            if not cands:
                calls_skipped += 1            # external/stdlib — skip in the spike
                continue
            same_file = [c for c in cands if c["path"] == info["rel"]]
            target = (same_file[0] if same_file
                      else cands[0] if len(cands) == 1 else None)
            if target is None:
                calls_skipped += 1            # ambiguous across files — skip
                continue
            if codegraph.add_edge(project, caller_id, "calls", target["id"]):
                calls_resolved += 1

    codegraph.set_meta(project, built_at=int(time.time()),
                       commit_sha=commit_sha, root=str(root_p))
    s = codegraph.stats(project)
    return {"files": len(collected), "nodes": s["nodes"], "edges": s["edges"],
            "imports": imports_added, "calls_resolved": calls_resolved,
            "calls_skipped": calls_skipped}
