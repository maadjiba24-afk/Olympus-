"""Multi-language extraction — the graph learns every language natively.

Graphify covers ~40 languages by shipping ~25 tree-sitter grammar wheels;
Olympus's defining property is three pure-Python dependencies, so the same
capability is absorbed the other way: one regex extraction ENGINE plus a
per-language table of definition/import/comment shapes (the approach Graphify
itself uses for Apex and as its Pascal fallback). Python keeps the stdlib-`ast`
extractor (codegraph_ast) — it is strictly better; everything else goes
through here.

Honesty over coverage: a regex parser knows *that* `foo(` was called but not
reliably *who* called it (brace nesting is invisible to it), so call edges from
this engine are INFERRED — never EXTRACTED — and the hallucination oracle,
which trusts EXTRACTED only, stays sound. Definitions and imports ARE explicit
single-line statements the regexes match exactly, so `defines`/`imports` edges
keep EXTRACTED. Ambiguous cross-file call candidates become AMBIGUOUS edges
(capped) instead of being dropped, so "maybe" is visible in the graph rather
than silently missing.

NOTE/WHY/HACK comments and doc-comments become sanitized rationale nodes, same
as the Python path — an injection-shaped comment never reaches a model clean.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import codegraph

_MAX_FILE_BYTES = 1_000_000        # skip generated monsters; graphs need shape, not bulk
_MAX_AMBIGUOUS = 3                 # cap AMBIGUOUS fan-out per call site

# Words that look like calls but are control flow / builtins in most languages.
_NOT_CALLS = frozenset({
    "if", "for", "while", "switch", "return", "catch", "throw", "new",
    "function", "def", "fn", "func", "sizeof", "typeof", "super", "this",
    "print", "println", "printf", "len", "require", "import", "include",
    "assert", "panic", "defer", "go", "await", "yield", "match", "case",
    "public", "private", "static", "void", "int", "string", "bool", "float",
    "double", "char", "long", "let", "var", "const", "in", "of", "do", "else",
    "try", "raise", "echo", "exit", "until", "unless", "elif", "then",
})

_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_NOTE_RE = re.compile(r"(?:NOTE|WHY|HACK|TODO\(why\))[:\s]\s*(.+)", re.I)


class Lang:
    """One language's shapes. `fn`/`cls` regexes expose a `name` group; `imp`
    exposes `mod` (a module/path string whose stem resolves to a file)."""

    def __init__(self, name: str, suffixes: tuple[str, ...], fn: str,
                 cls: str | None = None, imp: str | None = None,
                 comment: str = "//"):
        self.name = name
        self.suffixes = suffixes
        self.fn = re.compile(fn, re.M)
        self.cls = re.compile(cls, re.M) if cls else None
        self.imp = re.compile(imp, re.M) if imp else None
        self.comment = comment


_ID = r"[A-Za-z_][A-Za-z0-9_]*"

LANGS: list[Lang] = [
    Lang("javascript", (".js", ".jsx", ".mjs", ".cjs"),
         fn=rf"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>{_ID})"
            rf"|^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name2>{_ID})\s*=\s*"
            rf"(?:async\s*)?(?:\([^)]*\)|{_ID})\s*=>",
         cls=rf"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>{_ID})",
         imp=r"""(?:import\s.*?from\s+|require\s*\(\s*)['"](?P<mod>[^'"]+)['"]"""),
    Lang("typescript", (".ts", ".tsx", ".mts", ".cts"),
         fn=rf"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>{_ID})"
            rf"|^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name2>{_ID})\s*=\s*"
            rf"(?:async\s*)?(?:\([^)]*\)|{_ID})\s*=>",
         cls=rf"^\s*(?:export\s+)?(?:abstract\s+)?"
             rf"(?:class|interface|enum)\s+(?P<name>{_ID})",
         imp=r"""import\s.*?from\s+['"](?P<mod>[^'"]+)['"]"""),
    Lang("go", (".go",),
         fn=rf"^func\s+(?:\([^)]*\)\s+)?(?P<name>{_ID})\s*[(\[]",
         cls=rf"^type\s+(?P<name>{_ID})\s+(?:struct|interface)\b",
         imp=r"""^\s*(?:import\s+)?(?:\w+\s+)?"(?P<mod>[^"]+)"$"""),
    Lang("rust", (".rs",),
         fn=rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
            rf"fn\s+(?P<name>{_ID})",
         cls=rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(?P<name>{_ID})",
         imp=rf"^\s*(?:pub\s+)?use\s+(?:crate|super|self)?::?(?P<mod>{_ID})"),
    Lang("java", (".java",),
         fn=rf"^\s*(?:public|protected|private|static|final|abstract|synchronized"
            rf"|native|\s)+[\w<>\[\],.\s]+\s+(?P<name>{_ID})\s*\([^;]*$",
         cls=rf"^\s*(?:public\s+|abstract\s+|final\s+)*"
             rf"(?:class|interface|enum|record)\s+(?P<name>{_ID})",
         imp=rf"^import\s+(?:static\s+)?[\w.]*?(?P<mod>{_ID})\s*;"),
    Lang("c", (".c", ".h"),
         fn=rf"^(?:static\s+|inline\s+|extern\s+)*[\w*]+[\s*]+(?P<name>{_ID})"
            rf"\s*\([^;]*$",
         cls=rf"^\s*(?:typedef\s+)?struct\s+(?P<name>{_ID})",
         imp=r"""^\s*#\s*include\s+["<](?P<mod>[^">]+)[">]"""),
    Lang("cpp", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".cu", ".cuh", ".metal"),
         fn=rf"^(?:static\s+|inline\s+|virtual\s+|constexpr\s+|template\s*<[^>]*>\s*)*"
            rf"[\w:<>&*~]+[\s&*]+(?P<name>{_ID})\s*\([^;]*$",
         cls=rf"^\s*(?:class|struct|enum\s+class)\s+(?P<name>{_ID})",
         imp=r"""^\s*#\s*include\s+["<](?P<mod>[^">]+)[">]"""),
    Lang("csharp", (".cs",),
         fn=rf"^\s*(?:public|protected|private|internal|static|virtual|override"
            rf"|async|sealed|\s)+[\w<>\[\],.?\s]+\s+(?P<name>{_ID})\s*\([^;]*$",
         cls=rf"^\s*(?:public\s+|internal\s+|abstract\s+|sealed\s+|partial\s+|static\s+)*"
             rf"(?:class|interface|enum|struct|record)\s+(?P<name>{_ID})",
         imp=rf"^\s*using\s+(?:static\s+)?[\w.]*?(?P<mod>{_ID})\s*;"),
    Lang("ruby", (".rb",), comment="#",
         fn=rf"^\s*def\s+(?:self\.)?(?P<name>{_ID}[?!]?)",
         cls=rf"^\s*(?:class|module)\s+(?P<name>{_ID})",
         imp=r"""^\s*require(?:_relative)?\s+['"](?P<mod>[^'"]+)['"]"""),
    Lang("php", (".php",),
         fn=rf"^\s*(?:public\s+|protected\s+|private\s+|static\s+|abstract\s+|final\s+)*"
            rf"function\s+(?P<name>{_ID})",
         cls=rf"^\s*(?:abstract\s+|final\s+)*(?:class|interface|trait|enum)\s+(?P<name>{_ID})",
         imp=rf"^\s*use\s+[\w\\]*?(?P<mod>{_ID})\s*;"),
    Lang("kotlin", (".kt", ".kts"),
         fn=rf"^\s*(?:public\s+|private\s+|internal\s+|protected\s+|open\s+"
            rf"|override\s+|suspend\s+|inline\s+)*fun\s+(?:<[^>]*>\s*)?"
            rf"(?:[\w.<>?]+\.)?(?P<name>{_ID})",
         cls=rf"^\s*(?:public\s+|private\s+|internal\s+|open\s+|abstract\s+|sealed\s+"
             rf"|data\s+|enum\s+|annotation\s+)*(?:class|interface|object)\s+(?P<name>{_ID})",
         imp=rf"^import\s+[\w.]*?(?P<mod>{_ID})$"),
    Lang("swift", (".swift",),
         fn=rf"^\s*(?:public\s+|private\s+|internal\s+|open\s+|static\s+|override\s+"
            rf"|mutating\s+)*func\s+(?P<name>{_ID})",
         cls=rf"^\s*(?:public\s+|open\s+|final\s+)*"
             rf"(?:class|struct|enum|protocol|extension)\s+(?P<name>{_ID})",
         imp=rf"^\s*import\s+(?P<mod>{_ID})"),
    Lang("scala", (".scala",),
         fn=rf"^\s*(?:override\s+|private\s+|protected\s+|implicit\s+|final\s+)*"
            rf"def\s+(?P<name>{_ID})",
         cls=rf"^\s*(?:sealed\s+|abstract\s+|final\s+|case\s+)*"
             rf"(?:class|trait|object)\s+(?P<name>{_ID})",
         imp=rf"^import\s+[\w.]*?(?P<mod>{_ID})"),
    Lang("lua", (".lua", ".luau"), comment="--",
         fn=rf"^\s*(?:local\s+)?function\s+(?:[\w.:]+[.:])?(?P<name>{_ID})",
         imp=r"""require\s*\(?\s*['"](?P<mod>[^'"]+)['"]"""),
    Lang("bash", (".sh", ".bash"), comment="#",
         fn=rf"^\s*(?:function\s+)?(?P<name>{_ID})\s*\(\)\s*\{{",
         imp=rf"^\s*(?:source|\.)\s+(?P<mod>\S+)"),
    Lang("powershell", (".ps1", ".psm1"), comment="#",
         fn=rf"^\s*function\s+(?P<name>[\w-]+)",
         imp=rf"^\s*Import-Module\s+(?P<mod>[\w.-]+)"),
    Lang("elixir", (".ex", ".exs"), comment="#",
         fn=rf"^\s*defp?\s+(?P<name>{_ID}[?!]?)",
         cls=rf"^\s*defmodule\s+(?P<name>[\w.]+)",
         imp=rf"^\s*(?:import|alias|use)\s+[\w.]*?(?P<mod>{_ID})\s*$"),
    Lang("dart", (".dart",),
         fn=rf"^\s*(?:static\s+|final\s+)*[\w<>?,\s]+\s+(?P<name>{_ID})\s*\([^;]*$"
            rf"|^\s*(?:void|Future[\w<>]*)\s+(?P<name2>{_ID})\s*\(",
         cls=rf"^\s*(?:abstract\s+)?(?:class|mixin|enum)\s+(?P<name>{_ID})",
         imp=r"""^import\s+['"](?P<mod>[^'"]+)['"]"""),
    Lang("zig", (".zig",),
         fn=rf"^\s*(?:pub\s+)?(?:export\s+)?fn\s+(?P<name>{_ID})",
         imp=r"""@import\s*\(\s*"(?P<mod>[^"]+)"\s*\)"""),
    Lang("julia", (".jl",), comment="#",
         fn=rf"^\s*function\s+(?:[\w.]+\.)?(?P<name>{_ID}[!]?)"
            rf"|^\s*(?P<name2>{_ID})\s*\([^)]*\)\s*=(?!=)",
         cls=rf"^\s*(?:mutable\s+)?struct\s+(?P<name>{_ID})",
         imp=rf"^\s*(?:using|import)\s+\.?(?P<mod>{_ID})"),
    Lang("fortran", (".f", ".f90", ".f95", ".f03", ".f08"), comment="!",
         fn=rf"^\s*(?:pure\s+|elemental\s+|recursive\s+)*"
            rf"(?:subroutine|function)\s+(?P<name>{_ID})",
         cls=rf"^\s*module\s+(?P<name>{_ID})",
         imp=rf"^\s*use\s+(?P<mod>{_ID})"),
]

SUFFIXES: dict[str, Lang] = {s: lang for lang in LANGS for s in lang.suffixes}


def _first_group(m: re.Match) -> str | None:
    for g in ("name", "name2"):
        try:
            if m.group(g):
                return m.group(g)
        except (IndexError, re.error):
            continue
    return None


def _mod_stem(mod: str) -> str:
    """'./lib/util.js' / 'a/b/util' / 'util.h' -> 'util'."""
    stem = mod.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return stem.rsplit(".", 1)[0] if "." in stem else stem


def extract_file(project: str, path: Path, root: Path) -> dict | None:
    """One file through the engine: module + class/function nodes with
    `defines` edges (EXTRACTED — the definitions are explicit), rationale from
    NOTE/WHY/HACK comments, and raw imports/call-sites for build()'s
    resolution pass. Same info-dict contract as codegraph_ast.extract_file."""
    lang = SUFFIXES.get(path.suffix.lower())
    if lang is None:
        return None
    rel = str(path.relative_to(root))
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    qual = path.stem
    mod = codegraph.add_node(project, rel, qual, kind=codegraph.MODULE)
    module_id = mod["id"]

    # Definitions: one pass per shape, keyed by line for call attribution.
    defs: list[tuple[int, str, str]] = []          # (lineno, node_id, name)
    lines = text.splitlines()
    for rx, kind in ((lang.fn, codegraph.FUNCTION),
                     (lang.cls, codegraph.CLASS)):
        if rx is None:
            continue
        for m in rx.finditer(text):
            name = _first_group(m)
            if not name or name in _NOT_CALLS:
                continue
            lineno = text.count("\n", 0, m.start()) + 1
            n = codegraph.add_node(project, rel, f"{qual}.{name}", kind=kind,
                                   span=[lineno, lineno])
            codegraph.add_edge(project, module_id, "defines", n["id"])
            defs.append((lineno, n["id"], name))
    defs.sort()

    # Rationale: NOTE/WHY/HACK comment lines attach to the nearest preceding
    # definition (or the module). Sanitized inside add_rationale.
    cm = re.escape(lang.comment)
    for m in re.finditer(rf"{cm}\s*(.+)$", text, re.M):
        note = _NOTE_RE.search(m.group(1))
        if not note:
            continue
        lineno = text.count("\n", 0, m.start()) + 1
        owner = module_id
        for dline, did, _name in defs:
            if dline <= lineno:
                owner = did
            else:
                break
        codegraph.add_rationale(project, rel, owner, note.group(1))

    # Imports (module stems) and call sites attributed to the nearest
    # preceding definition — approximate for brace languages, which is exactly
    # why build() marks these calls INFERRED.
    imports = ([_mod_stem(m.group("mod")) for m in lang.imp.finditer(text)]
               if lang.imp else [])
    def_names = {name for _, _, name in defs}
    calls: list[tuple[str, str]] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith(lang.comment):
            continue
        owner = module_id
        for dline, did, _name in defs:
            if dline <= i:
                owner = did
            else:
                break
        for cm_ in _CALL_RE.finditer(line):
            callee = cm_.group(1)
            if callee in _NOT_CALLS:
                continue
            # a def on this very line is a definition, not a call of itself
            if callee in def_names and any(d[0] == i and d[2] == callee
                                           for d in defs):
                continue
            calls.append((owner, callee))

    return {"qual": qual, "stem": path.stem, "module_id": module_id,
            "rel": rel, "imports": imports, "calls": calls,
            "lang": lang.name}
