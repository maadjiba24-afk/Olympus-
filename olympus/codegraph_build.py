"""Build and incrementally update the code graph — every language, one pass.

This grew out of codegraph_ast.build() (the Python-only Phase 0 spike) when the
graph absorbed Graphify's pipeline wholesale: multi-language extraction
(codegraph_langs), documents (codegraph_ingest), `.gitignore`/`.olympusignore`
handling, and — the part that makes a graph livable — INCREMENTAL update. Every
extraction records a per-file manifest entry (content hash + the file's raw
imports/call-sites) in the store; `update()` re-extracts only files whose hash
changed, removes the contribution of deleted files, and re-runs resolution from
the manifest without re-parsing anything unchanged. Node ids are sha(path::qual)
precisely so identity survives this.

Resolution honesty (the oracle depends on it):
  - `defines`/`imports`/`references` edges are EXTRACTED — explicit statements.
  - calls resolved from Python's AST walk are EXTRACTED (unique target) —
    the parser really saw the call.
  - calls from the regex engine are INFERRED even when unique — the engine
    attributes call sites approximately.
  - a call matching several same-named symbols becomes AMBIGUOUS edges to each
    candidate (capped), visible in the graph instead of silently dropped.

Ignore rules are a deliberate SUBSET of gitignore: root-level `.gitignore` and
`.olympusignore` (last wins, `!` negation honored), `*` globs, dir/ suffixes —
enough for real repos without reimplementing git. `.olympusignore` only ever
narrows further, mirroring Graphify's merge rule.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from pathlib import Path

from . import codegraph, codegraph_ast, codegraph_ingest, codegraph_langs, store

_MANIFEST = "codegraph.manifest"

_IGNORE_DIRS = codegraph_ast._IGNORE_DIRS | {
    ".idea", ".vscode", "target", "vendor", ".tox", "__snapshots__", ".next",
    "coverage", ".cache"}

_AMBIG_CONF = 0.3
_INFER_CONF = 0.7

# Names that shadow stdlib/builtin methods: a call site named `append` is
# almost always list.append, not your `append`. Cross-file resolution for
# these would flood the graph with false edges (and crown `append` a god
# node), so they only resolve within the same file, where the local
# definition plausibly IS the target.
_SHADOWED = frozenset({
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "index",
    "count", "get", "set", "add", "update", "keys", "values", "items", "copy",
    "join", "split", "strip", "replace", "format", "encode", "decode",
    "lower", "upper", "startswith", "endswith", "read", "write", "close",
    "open", "exists", "mkdir", "unlink", "resolve", "flush", "seek", "next",
    "send", "put", "delete", "list", "filter", "map", "type", "str", "int",
    "float", "bool", "dict", "tuple", "len", "min", "max", "sum", "abs",
    "all", "any", "sorted", "reversed", "enumerate", "zip", "range", "repr",
    "hash", "id", "isinstance", "issubclass", "getattr", "setattr", "hasattr",
})


# --- ignore rules ---------------------------------------------------------

class IgnoreRules:
    """Root-level `.gitignore` + `.olympusignore` patterns (subset — see module
    docstring). Later patterns win, so `.olympusignore` is loaded last."""

    def __init__(self, root: Path):
        self.rules: list[tuple[bool, str]] = []       # (negated, pattern)
        for name in (".gitignore", ".olympusignore"):
            p = root / name
            if not p.exists():
                continue
            try:
                lines = p.read_text(encoding="utf-8",
                                    errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                neg = line.startswith("!")
                self.rules.append((neg, line[1:] if neg else line))

    def ignored(self, rel: str, is_dir: bool = False) -> bool:
        rel = rel.replace("\\", "/")
        verdict = False
        for neg, pat in self.rules:
            dir_only = pat.endswith("/")
            pat_ = pat.rstrip("/")
            anchored = pat_.startswith("/")
            pat_ = pat_.lstrip("/")
            if dir_only and not is_dir and not any(
                    fnmatch.fnmatch(part, pat_) for part in rel.split("/")[:-1]):
                continue
            hit = (fnmatch.fnmatch(rel, pat_) if anchored or "/" in pat_
                   else any(fnmatch.fnmatch(part, pat_)
                            for part in rel.split("/")))
            if hit:
                verdict = not neg
        return verdict


# --- file collection ------------------------------------------------------

def collect_files(root: Path, include_docs: bool = True,
                  respect_ignores: bool = True) -> list[Path]:
    """Every extractable file under root, honoring ignore dirs + ignore files.
    Sorted for deterministic builds."""
    rules = IgnoreRules(root) if respect_ignores else None
    suffixes = set(codegraph_langs.SUFFIXES) | {".py"}
    if include_docs:
        suffixes |= set(codegraph_ingest.SUFFIXES)
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in _IGNORE_DIRS for part in rel_parts[:-1]):
            continue
        rel = "/".join(rel_parts)
        if rules and rules.ignored(rel):
            continue
        out.append(path)
    return out


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _extract(project: str, path: Path, root: Path) -> dict | None:
    if path.suffix.lower() == ".py":
        info = codegraph_ast.extract_file(project, path, root)
        if info is not None:
            info.setdefault("lang", "python")
        return info
    if path.suffix.lower() in codegraph_ingest.SUFFIXES:
        return codegraph_ingest.extract_file(project, path, root)
    return codegraph_langs.extract_file(project, path, root)


# --- manifest -------------------------------------------------------------

def _load_manifest(project: str) -> dict:
    blob = store.backend().get(_MANIFEST, codegraph._key(project))
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return {}


def _save_manifest(project: str, manifest: dict) -> None:
    store.backend().put(_MANIFEST, codegraph._key(project),
                        json.dumps(manifest).encode())


# --- resolution -----------------------------------------------------------

def _resolve(project: str, infos: list[dict]) -> dict:
    """Cross-file pass over ALL manifest infos: imports/references by module
    stem, calls by symbol name with the tier rules from the module docstring.
    add_edge de-duplicates, so re-running after an incremental update only adds
    what is missing."""
    nodes_ = codegraph.nodes(project)
    func_index: dict[str, list[dict]] = {}
    for n in nodes_:
        if n["kind"] in (codegraph.FUNCTION, codegraph.METHOD):
            func_index.setdefault(n["label"].lower(), []).append(n)
    # Two module indexes so a code import can never resolve to a same-stem
    # document (olympus/capabilities.py, not docs/CAPABILITIES.md). Docs look
    # doc-first, then code — a wikilink usually names another doc.
    code_index: dict[str, str] = {}
    doc_index: dict[str, str] = {}
    for info in infos:
        idx = doc_index if info.get("lang") == "doc" else code_index
        idx.setdefault(info["stem"].lower(), info["module_id"])

    stats = {"imports": 0, "calls_resolved": 0, "calls_inferred": 0,
             "calls_ambiguous": 0, "calls_skipped": 0}
    for info in infos:
        is_doc = info.get("lang") == "doc"
        rel_name = "references" if is_doc else "imports"
        for stem in info["imports"]:
            stem_l = str(stem).lower()
            tgt = (doc_index.get(stem_l) or code_index.get(stem_l) if is_doc
                   else code_index.get(stem_l))
            if tgt and tgt != info["module_id"]:
                if codegraph.add_edge(project, info["module_id"], rel_name, tgt):
                    stats["imports"] += 1
        certain = info.get("lang") == "python"
        for caller_id, callee in info["calls"]:
            cands = func_index.get(str(callee).lower(), [])
            same_file = [c for c in cands if c["path"] == info["rel"]]
            if str(callee).lower() in _SHADOWED:
                cands = same_file            # cross-file would be noise
            elif same_file:
                cands = same_file
            if not cands:
                stats["calls_skipped"] += 1
                continue
            if len(cands) == 1:
                tier = (codegraph.EXTRACTED if certain else codegraph.INFERRED)
                conf = 1.0 if certain else _INFER_CONF
                if codegraph.add_edge(project, caller_id, "calls",
                                      cands[0]["id"], tier=tier,
                                      confidence=conf):
                    key = "calls_resolved" if certain else "calls_inferred"
                    stats[key] += 1
            elif len(cands) <= codegraph_langs._MAX_AMBIGUOUS:
                for c in cands:
                    if codegraph.add_edge(project, caller_id, "calls", c["id"],
                                          tier=codegraph.AMBIGUOUS,
                                          confidence=_AMBIG_CONF):
                        stats["calls_ambiguous"] += 1
            else:
                # a name defined in many places is a common word, not a lead —
                # edges to all of them would be noise, so none are made
                stats["calls_skipped"] += 1
    return stats


# --- build & update -------------------------------------------------------

def build(project: str = "self", root: str = ".",
          include_docs: bool = True, respect_ignores: bool = True,
          commit_sha: str | None = None) -> dict:
    """Full rebuild: clear, extract every file, resolve, write the manifest."""
    root_p = Path(root).resolve()
    codegraph.clear(project)
    manifest: dict = {}
    infos: list[dict] = []
    with codegraph.bulk(project):
        for path in collect_files(root_p, include_docs, respect_ignores):
            info = _extract(project, path, root_p)
            if info is None:
                continue
            infos.append(info)
            manifest[info["rel"]] = {"sha": _sha(path), "info": info}
        stats = _resolve(project, infos)
    _save_manifest(project, manifest)
    codegraph.set_meta(project, built_at=int(time.time()),
                       commit_sha=commit_sha, root=str(root_p))
    s = codegraph.stats(project)
    return {"files": len(infos), "nodes": s["nodes"], "edges": s["edges"],
            **stats}


def update(project: str = "self", root: str = ".",
           include_docs: bool = True, respect_ignores: bool = True) -> dict:
    """Incremental update: hash the tree, re-extract only changed files, drop
    deleted ones, re-resolve from the manifest. Falls back to a full build when
    no manifest exists (a pre-incremental graph)."""
    manifest = _load_manifest(project)
    if not manifest:
        result = build(project, root, include_docs, respect_ignores)
        result["mode"] = "full"
        return result

    root_p = Path(root).resolve()
    # keys are os-native relative paths — the exact form extraction stored
    current = {str(p.relative_to(root_p)): p
               for p in collect_files(root_p, include_docs, respect_ignores)}

    changed = [rel for rel, p in current.items()
               if manifest.get(rel, {}).get("sha") != _sha(p)]
    deleted = [rel for rel in manifest if rel not in current]

    with codegraph.bulk(project):
        codegraph.remove_paths(project, set(changed) | set(deleted))
        for rel in deleted:
            manifest.pop(rel, None)
        for rel in changed:
            info = _extract(project, current[rel], root_p)
            if info is None:
                manifest.pop(rel, None)
                continue
            manifest[rel] = {"sha": _sha(current[rel]), "info": info}

        # Unchanged files keep their nodes; resolution over the WHOLE manifest
        # re-adds any cross-file edge that remove_paths took with it.
        infos = [m["info"] for m in manifest.values()]
        stats = _resolve(project, infos)
    _save_manifest(project, manifest)
    codegraph.set_meta(project, built_at=int(time.time()), root=str(root_p))
    s = codegraph.stats(project)
    return {"files_changed": len(changed), "files_deleted": len(deleted),
            "nodes": s["nodes"], "edges": s["edges"], "mode": "incremental",
            **stats}
