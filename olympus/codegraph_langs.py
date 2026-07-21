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
_MAX_LINE_LEN = 800                # per-line regex input cap — ReDoS backstop on
                                   # hostile minified/space-padded lines
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
    # `with (obj) {` (JS), `using (x) {` (C#), `lock (x) {` (C#), `foreach`,
    # `synchronized`, `fixed` — control statements that take `(...) {` and would
    # otherwise be welded in as phantom methods by the shorthand pattern.
    "with", "using", "lock", "foreach", "synchronized", "fixed", "when",
})

_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_NOTE_RE = re.compile(r"(?:NOTE|WHY|HACK|TODO\(why\))[:\s]\s*(.+)", re.I)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Modifier/keyword tokens that appear inside an inheritance clause but are not
# base types (e.g. `class C : public Base` in C++, `where T : new()` in C#).
_INH_KEYWORDS = frozenset({
    "public", "private", "protected", "internal", "virtual", "abstract",
    "override", "sealed", "final", "open", "static", "readonly", "partial",
    "where", "out", "in", "ref", "new", "class", "struct", "interface",
    "enum", "record", "object", "trait", "extends", "implements", "with",
    "by", "data", "sealed", "companion",
})


def _bases_from(clause: str) -> list[str]:
    """Base-type identifiers from an inheritance clause, keywords/generics
    stripped. `public Base, IThing<T>` -> ['Base', 'IThing']."""
    out: list[str] = []
    for ident in _IDENT_RE.findall(clause):
        low = ident.lower()
        if low in _INH_KEYWORDS or low in _NOT_CALLS or ident in out:
            continue
        out.append(ident)
    return out


class Lang:
    """One language's shapes. `fn`/`cls` regexes expose a `name` group; `imp`
    exposes `mod` (a module/path string whose stem resolves to a file)."""

    def __init__(self, name: str, suffixes: tuple[str, ...], fn: str,
                 cls: str | None = None, imp: str | None = None,
                 comment: str = "//", inh: str | None = None):
        self.name = name
        self.suffixes = suffixes
        self.fn = re.compile(fn, re.M)
        self.cls = re.compile(cls, re.M) if cls else None
        self.imp = re.compile(imp, re.M) if imp else None
        self.comment = comment
        # `inh` captures a class line's inheritance CLAUSE in group "bases"
        # (the text after extends/implements/:/<). Base identifiers are pulled
        # from that clause in code, not by a second regex, so there is no
        # nested-quantifier ReDoS surface. Keep the clause class simple and
        # bounded; the per-line length cap is the backstop.
        self.inh = re.compile(inh, re.M) if inh else None


_ID = r"[A-Za-z_][A-Za-z0-9_]*"

# Class/object method shorthand: `name(params) {` — a definition, not a call.
# Guards against false positives: params must contain NO parens (`[^()]*`), which
# rejects `it("x", function(){…})` and arrow-callback lines like
# `describe("x", () => {`; an optional TS return-type annotation is allowed
# before the body brace. Control keywords (if/for/while/with/…) are filtered by
# `_NOT_CALLS` in the extractor.
#
# The single leading `[ \t]*` is the ONLY unbounded whitespace matcher — the
# generator `*` carries its own trailing space and each modifier keyword
# consumes its `[ \t]+`, so a whitespace run can be partitioned exactly one way
# and there is nothing for the engine to backtrack over (an earlier draft had a
# second `[ \t]*` before the name, which made a non-matching space-padded line
# O(n²)). The per-line `_MAX_LINE_LEN` cap remains as defense in depth.
_METHOD_SHORTHAND = (
    r"^[ \t]*(?:(?:public|private|protected|readonly|static|async|get|set|"
    r"override)[ \t]+)*(?:\*[ \t]*)?(?P<name3>[A-Za-z_$][A-Za-z0-9_$]*)[ \t]*"
    r"\([^()]*\)(?:[ \t]*:[ \t]*[^\n{;=]+)?[ \t]*\{")

LANGS: list[Lang] = [
    Lang("javascript", (".js", ".jsx", ".mjs", ".cjs"),
         fn=rf"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>{_ID})"
            rf"|^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name2>{_ID})\s*=\s*"
            rf"(?:async\s*)?(?:\([^)]*\)|{_ID})\s*=>"
            rf"|{_METHOD_SHORTHAND}",
         cls=rf"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>{_ID})",
         imp=r"""(?:import\s.*?from\s+|require\s*\(\s*)['"](?P<mod>[^'"]+)['"]"""),
    Lang("typescript", (".ts", ".tsx", ".mts", ".cts"),
         fn=rf"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>{_ID})"
            rf"|^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name2>{_ID})\s*=\s*"
            rf"(?:async\s*)?(?:\([^)]*\)|{_ID})\s*=>"
            rf"|{_METHOD_SHORTHAND}",
         cls=rf"^\s*(?:export\s+)?(?:abstract\s+)?"
             rf"(?:class|interface|enum)\s+(?P<name>{_ID})",
         imp=r"""import\s.*?from\s+['"](?P<mod>[^'"]+)['"]""",
         inh=r"(?:extends|implements)\s+(?P<bases>[^\n{]+)"),
    Lang("go", (".go",),
         fn=rf"^func\s+(?:\([^)]*\)\s+)?(?P<name>{_ID})\s*[(\[]",
         cls=rf"^type\s+(?P<name>{_ID})\s+(?:struct|interface)\b",
         # Require the `import` keyword: a bare `"handler"` on its own line (a
         # string-literal continuation, not an import) must not become a false
         # import edge. Block imports — `import ( "a"; "b" )` — are missed; that
         # is the safe direction (a missed edge, never an invented one).
         imp=r"""^\s*import\s+(?:\w+\s+)?"(?P<mod>[^"]+)"$"""),
    Lang("rust", (".rs",),
         fn=rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
            rf"fn\s+(?P<name>{_ID})",
         cls=rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(?P<name>{_ID})",
         imp=rf"^\s*(?:pub\s+)?use\s+(?:crate|super|self)?::?(?P<mod>{_ID})"),
    Lang("java", (".java",),
         # LINEAR by construction: `(?:TOKEN[ \t]+)+ NAME (` where TOKEN is a
         # run of non-space type/modifier chars and the separator is
         # whitespace. The two character classes are disjoint, so a space run
         # and a word run each belong to exactly one place — there is no
         # ambiguous partition for the engine to backtrack over (the old
         # `(?:…|\s)+[\w…\s]+\s+` form had three overlapping whitespace
         # quantifiers and was catastrophically backtracking / ReDoS-prone).
         fn=rf"^[ \t]*(?:[\w<>\[\],.?@]+[ \t]+)+(?P<name>{_ID})[ \t]*\(",
         cls=rf"^\s*(?:public\s+|abstract\s+|final\s+)*"
             rf"(?:class|interface|enum|record)\s+(?P<name>{_ID})",
         imp=rf"^import\s+(?:static\s+)?[\w.]*?(?P<mod>{_ID})\s*;",
         inh=r"(?:extends|implements)\s+(?P<bases>[^\n{]+)"),
    Lang("c", (".c", ".h"),
         fn=rf"^(?:static\s+|inline\s+|extern\s+)*[\w*]+[\s*]+(?P<name>{_ID})"
            rf"\s*\([^;]*$",
         cls=rf"^\s*(?:typedef\s+)?struct\s+(?P<name>{_ID})",
         imp=r"""^\s*#\s*include\s+["<](?P<mod>[^">]+)[">]"""),
    Lang("cpp", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".cu", ".cuh", ".metal"),
         fn=rf"^(?:static\s+|inline\s+|virtual\s+|constexpr\s+|template\s*<[^>]*>\s*)*"
            rf"[\w:<>&*~]+[\s&*]+(?P<name>{_ID})\s*\([^;]*$",
         cls=rf"^\s*(?:class|struct|enum\s+class)\s+(?P<name>{_ID})",
         imp=r"""^\s*#\s*include\s+["<](?P<mod>[^">]+)[">]""",
         inh=rf"(?:class|struct)\s+{_ID}\s*:\s*(?P<bases>[^\n{{]+)"),
    Lang("csharp", (".cs",),
         # Linear token form — see the java note above for why this shape can't
         # backtrack catastrophically.
         fn=rf"^[ \t]*(?:[\w<>\[\],.?@]+[ \t]+)+(?P<name>{_ID})[ \t]*\(",
         cls=rf"^\s*(?:public\s+|internal\s+|abstract\s+|sealed\s+|partial\s+|static\s+)*"
             rf"(?:class|interface|enum|struct|record)\s+(?P<name>{_ID})",
         imp=rf"^\s*using\s+(?:static\s+)?[\w.]*?(?P<mod>{_ID})\s*;",
         inh=rf"(?:class|interface|struct|record)\s+{_ID}(?:<[^>]*>)?\s*:\s*"
             rf"(?P<bases>[^\n{{]+)"),
    Lang("ruby", (".rb",), comment="#",
         fn=rf"^\s*def\s+(?:self\.)?(?P<name>{_ID}[?!]?)",
         cls=rf"^\s*(?:class|module)\s+(?P<name>{_ID})",
         imp=r"""^\s*require(?:_relative)?\s+['"](?P<mod>[^'"]+)['"]""",
         inh=rf"^\s*class\s+{_ID}\s*<\s*(?P<bases>[\w:]+)"),
    Lang("php", (".php",),
         fn=rf"^\s*(?:public\s+|protected\s+|private\s+|static\s+|abstract\s+|final\s+)*"
            rf"function\s+(?P<name>{_ID})",
         cls=rf"^\s*(?:abstract\s+|final\s+)*(?:class|interface|trait|enum)\s+(?P<name>{_ID})",
         imp=rf"^\s*use\s+[\w\\]*?(?P<mod>{_ID})\s*;",
         inh=r"(?:extends|implements)\s+(?P<bases>[^\n{]+)"),
    Lang("kotlin", (".kt", ".kts"),
         fn=rf"^\s*(?:public\s+|private\s+|internal\s+|protected\s+|open\s+"
            rf"|override\s+|suspend\s+|inline\s+)*fun\s+(?:<[^>]*>\s*)?"
            rf"(?:[\w.<>?]+\.)?(?P<name>{_ID})",
         cls=rf"^\s*(?:public\s+|private\s+|internal\s+|open\s+|abstract\s+|sealed\s+"
             rf"|data\s+|enum\s+|annotation\s+)*(?:class|interface|object)\s+(?P<name>{_ID})",
         imp=rf"^import\s+[\w.]*?(?P<mod>{_ID})$",
         inh=rf"(?:class|interface|object)\s+{_ID}(?:\s*\([^)]*\))?\s*:\s*"
             rf"(?P<bases>[^\n{{]+)"),
    Lang("swift", (".swift",),
         fn=rf"^\s*(?:public\s+|private\s+|internal\s+|open\s+|static\s+|override\s+"
            rf"|mutating\s+)*func\s+(?P<name>{_ID})",
         cls=rf"^\s*(?:public\s+|open\s+|final\s+)*"
             rf"(?:class|struct|enum|protocol|extension)\s+(?P<name>{_ID})",
         imp=rf"^\s*import\s+(?P<mod>{_ID})",
         inh=rf"(?:class|struct|enum|protocol|extension)\s+{_ID}\s*:\s*"
             rf"(?P<bases>[^\n{{]+)"),
    Lang("scala", (".scala",),
         fn=rf"^\s*(?:override\s+|private\s+|protected\s+|implicit\s+|final\s+)*"
            rf"def\s+(?P<name>{_ID})",
         cls=rf"^\s*(?:sealed\s+|abstract\s+|final\s+|case\s+)*"
             rf"(?:class|trait|object)\s+(?P<name>{_ID})",
         imp=rf"^import\s+[\w.]*?(?P<mod>{_ID})",
         inh=r"(?:extends|with)\s+(?P<bases>[^\n{]+)"),
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
    Lang("objc", (".m", ".mm"),
         # Objective-C methods: `-(ret)name` / `+(ret)name`, one per line.
         fn=rf"^[ \t]*[-+][ \t]*\([^)]*\)[ \t]*(?P<name>{_ID})",
         cls=rf"^\s*@(?:interface|implementation|protocol)\s+(?P<name>{_ID})",
         imp=r"""^\s*#\s*(?:import|include)\s+["<](?P<mod>[^">]+)[">]""",
         inh=rf"@interface\s+{_ID}\s*:\s*(?P<bases>{_ID})"),
    Lang("groovy", (".groovy", ".gradle"),
         # `def name(` OR `Type name(` — explicit alternation so the return
         # type can't greedily eat into the method name, and linear (disjoint
         # token/space classes) so it can't backtrack catastrophically.
         fn=rf"^[ \t]*(?:(?:public|private|protected|static|final)[ \t]+)*"
            rf"(?:def[ \t]+(?P<name>{_ID})|[\w.<>\[\]]+[ \t]+(?P<name2>{_ID}))"
            rf"[ \t]*\(",
         cls=rf"^\s*(?:@\w+\s+)*(?:public\s+|abstract\s+|final\s+)*"
             rf"(?:class|interface|trait|enum)\s+(?P<name>{_ID})",
         imp=rf"^import\s+(?:static\s+)?[\w.]*?(?P<mod>{_ID})",
         inh=r"(?:extends|implements)\s+(?P<bases>[^\n{]+)"),
    Lang("sql", (".sql",), comment="--",
         fn=rf"(?i:create)\s+(?:or\s+replace\s+)?(?i:function|procedure)\s+"
            rf"(?:if\s+not\s+exists\s+)?(?P<name>[\w.\"]+)",
         cls=rf"(?i:create)\s+(?:or\s+replace\s+)?"
             rf"(?i:table|view|materialized\s+view)\s+"
             rf"(?:if\s+not\s+exists\s+)?(?P<name>[\w.\"]+)"),
    Lang("terraform", (".tf", ".tfvars", ".hcl"), comment="#",
         fn=r"""^\s*(?:resource|data)\s+"(?P<name>[\w.-]+)\"""",
         cls=r"""^\s*(?:module|provider|variable|output)\s+"(?P<name>[\w.-]+)\"""",
         imp=r"""source\s*=\s*"(?P<mod>[^"]+)\""""),
    Lang("perl", (".pl", ".pm"), comment="#",
         fn=rf"^\s*sub\s+(?P<name>{_ID})",
         cls=rf"^\s*package\s+(?P<name>[\w:]+)",
         imp=rf"^\s*use\s+(?P<mod>[\w:]+)"),
    Lang("r", (".r",), comment="#",
         fn=rf"^\s*(?P<name>[\w.]+)\s*(?:<-|=)\s*function",
         imp=r"^\s*(?:library|require)\s*\(\s*[\"']?(?P<mod>[\w.]+)"),
    Lang("haskell", (".hs",), comment="--",
         fn=r"^(?P<name>[a-z_][\w']*)\s*::",
         cls=r"^(?:data|newtype|class|type)\s+(?P<name>[A-Z][\w']*)",
         imp=r"^import\s+(?:qualified\s+)?(?P<mod>[\w.]+)"),
    Lang("ocaml", (".ml", ".mli"), comment="(*",
         fn=rf"^\s*let\s+(?:rec\s+)?(?P<name>{_ID})",
         cls=rf"^\s*module\s+(?:type\s+)?(?P<name>{_ID})",
         imp=rf"^\s*open\s+(?P<mod>{_ID})"),
    Lang("clojure", (".clj", ".cljs", ".cljc"), comment=";",
         fn=r"\(defn-?\s+(?P<name>[\w.?!*+<>=/-]+)",
         cls=r"\(ns\s+(?P<name>[\w.]+)"),
    Lang("erlang", (".erl", ".hrl"), comment="%",
         fn=r"^(?P<name>[a-z]\w*)\s*\(",
         cls=r"^\s*-module\(\s*(?P<name>\w+)",
         imp=r"^\s*-import\(\s*(?P<mod>\w+)"),
    Lang("solidity", (".sol",),
         fn=rf"^\s*function\s+(?P<name>{_ID})",
         cls=rf"^\s*(?:abstract\s+)?(?:contract|interface|library)\s+(?P<name>{_ID})",
         imp=r"""^\s*import\s+.*?["'](?P<mod>[^"']+)["']""",
         inh=rf"(?:contract|interface)\s+{_ID}\s+is\s+(?P<bases>[^\n{{]+)"),
    Lang("nim", (".nim",), comment="#",
         fn=rf"^\s*(?:proc|func|method|template|macro|iterator|converter)\s+"
            rf"(?P<name>{_ID})",
         cls=rf"^\s*type\s+(?P<name>{_ID})",
         imp=r"^\s*(?:import|include)\s+(?P<mod>[\w./]+)"),
    Lang("pascal", (".pas", ".pp", ".dpr", ".dpk", ".lpr"),
         # Pascal/Delphi keywords are case-insensitive.
         fn=rf"(?i:^\s*(?:procedure|function|constructor|destructor))\s+"
            rf"(?:{_ID}\.)?(?P<name>{_ID})",
         cls=rf"^\s*(?:(?i:type)\s+)?(?P<name>{_ID})\s*=\s*"
             rf"(?i:class|object|record|interface)",
         imp=rf"(?i:^\s*uses)\s+(?P<mod>{_ID})",
         inh=rf"{_ID}\s*=\s*(?i:class|interface)\s*\(\s*(?P<bases>[\w, ]+)"),
    Lang("verilog", (".v", ".sv", ".svh"),
         fn=rf"^\s*(?:module|task|function)\s+(?:\w+\s+)?(?P<name>{_ID})"
            rf"|^\s*(?:endmodule)?\s*module\s+(?P<name2>{_ID})",
         cls=rf"^\s*(?:class|interface|package)\s+(?P<name>{_ID})",
         imp=rf"^\s*(?:import|`include)\s+[\"<]?(?P<mod>[\w./]+)"),
    Lang("apex", (".cls", ".trigger"),
         # Salesforce Apex — Java-like; linear token form (see java note).
         fn=rf"^[ \t]*(?:[\w<>\[\],.?@]+[ \t]+)+(?P<name>{_ID})[ \t]*\(",
         cls=rf"^\s*(?:(?:public|private|global|protected|virtual|abstract"
             rf"|with sharing|without sharing|static)\s+)*"
             rf"(?:class|interface|enum)\s+(?P<name>{_ID})"
             rf"|^\s*trigger\s+(?P<name2>{_ID})",
         inh=r"(?:extends|implements)\s+(?P<bases>[^\n{]+)"),
    Lang("webscript", (".vue", ".svelte", ".astro"),
         # Single-file components: match the JS/TS in their <script> block.
         fn=rf"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>{_ID})"
            rf"|^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name2>{_ID})\s*=\s*"
            rf"(?:async\s*)?(?:\([^)]*\)|{_ID})\s*=>"
            rf"|{_METHOD_SHORTHAND}",
         cls=rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?"
             rf"class\s+(?P<name>{_ID})",
         imp=r"""import\s.*?from\s+['"](?P<mod>[^'"]+)['"]"""),
]

SUFFIXES: dict[str, Lang] = {s: lang for lang in LANGS for s in lang.suffixes}


def _first_group(m: re.Match) -> str | None:
    for g in ("name", "name2", "name3"):
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
    if mod is None:                              # graph at the node cap
        return None
    module_id = mod["id"]
    codegraph.add_citations(project, rel, module_id, text)   # ADR/RFC refs
    lines = text.splitlines()

    def _owner_at(lineno: int, defs_: list) -> str:
        owner = module_id
        for dline, did, _name in defs_:
            if dline <= lineno:
                owner = did
            else:
                break
        return owner

    # Definitions — matched LINE BY LINE, and only on lines within
    # `_MAX_LINE_LEN`. Every def regex is single-line (`^…$`), so per-line
    # `search` is equivalent to the old whole-file `finditer` — but it bounds
    # the input each regex ever sees, which is what defuses catastrophic
    # backtracking (ReDoS) on a hostile minified/space-padded line in a cloned
    # repo. A pathological line is skipped, not chewed on.
    # Class/module patterns are tried BEFORE function patterns: on a line that
    # could look like both (e.g. Apex `trigger X on Y (...)`, which the linear
    # method form would otherwise misread as a call to `Y`), the structural
    # declaration wins. A real function line has no class keyword, so this is
    # harmless everywhere else.
    def_rxs = [(rx, kind) for rx, kind in
               ((lang.cls, codegraph.CLASS), (lang.fn, codegraph.FUNCTION))
               if rx is not None]
    defs: list[tuple[int, str, str]] = []          # (lineno, node_id, name)
    class_lines: dict[int, str] = {}               # lineno -> class node id
    for i, line in enumerate(lines, start=1):
        if len(line) > _MAX_LINE_LEN:
            continue
        for rx, kind in def_rxs:
            m = rx.search(line)
            if not m:
                continue
            name = _first_group(m)
            if not name or name in _NOT_CALLS:
                continue
            n = codegraph.add_node(project, rel, f"{qual}.{name}", kind=kind,
                                   span=[i, i])
            if n is None:
                break
            codegraph.add_edge(project, module_id, "defines", n["id"])
            defs.append((i, n["id"], name))
            if kind == codegraph.CLASS:
                class_lines[i] = n["id"]
            break                                  # a line is one def, not two

    # Inheritance: on each class line, pull base identifiers out of the
    # captured clause (in code — no second regex, no ReDoS surface).
    inherits: list[tuple[str, str]] = []
    if lang.inh and class_lines:
        for i, cid in class_lines.items():
            line = lines[i - 1]
            if len(line) > _MAX_LINE_LEN:
                continue
            m = lang.inh.search(line)
            if not m:
                continue
            for base in _bases_from(m.group("bases")):
                inherits.append((cid, base))

    # Rationale: NOTE/WHY/HACK comment lines, each in its own slot so several
    # notes on one owner don't overwrite each other. Sanitized inside
    # add_rationale.
    note_slot = 0
    for i, line in enumerate(lines, start=1):
        if len(line) > _MAX_LINE_LEN:
            continue
        idx = line.find(lang.comment)
        if idx < 0:
            continue
        note = _NOTE_RE.search(line[idx + len(lang.comment):])
        if not note:
            continue
        codegraph.add_rationale(project, rel, _owner_at(i, defs),
                                note.group(1), slot=f"n{note_slot}")
        note_slot += 1

    # Imports (module stems) and call sites attributed to the nearest preceding
    # definition — approximate for brace languages, which is exactly why
    # build() marks these edges INFERRED, never ground truth.
    imports: list[str] = []
    if lang.imp:
        for line in lines:
            if len(line) > _MAX_LINE_LEN:
                continue
            m = lang.imp.search(line)
            if m:
                imports.append(_mod_stem(m.group("mod")))
    def_names = {name for _, _, name in defs}
    calls: list[tuple[str, str]] = []
    for i, line in enumerate(lines, start=1):
        if len(line) > _MAX_LINE_LEN:
            continue
        if line.lstrip().startswith(lang.comment):
            continue
        owner = _owner_at(i, defs)
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
            "inherits": inherits, "lang": lang.name}
