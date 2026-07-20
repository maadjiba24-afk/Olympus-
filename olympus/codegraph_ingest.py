"""Documents into the code graph — READMEs, ADRs, notes, configs.

Graphify's "beyond code" insight, absorbed: design docs explain the code they
sit next to, so they belong in the SAME graph, not a separate RAG silo (docrag
keeps doing retrieval; this gives docs *structure*). A markdown file becomes a
DOCUMENT node; its `[[wikilinks]]` and `[text](./local.md)` links become
`references` edges — to other docs or to code modules, resolved by stem in the
shared build pass. Headings become searchable rationale so a doc's claims are
findable by symbol name.

Only local links create edges (a URL is not a node in your codebase). All
extracted text goes through the rationale sanitizer — a doc that says "ignore
previous instructions" is data here, never an instruction.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import codegraph

SUFFIXES = (".md", ".mdx", ".rst", ".txt", ".yaml", ".yml")

_MAX_FILE_BYTES = 500_000
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#]+)[^)]*\)")
_HEADING_RE = re.compile(r"^#{1,4}\s+(.+)$", re.M)
_MAX_HEADINGS = 8


def _stem(target: str) -> str | None:
    """Local link target -> file stem; None for URLs/anchors/mailto."""
    t = target.strip()
    if not t or "://" in t or t.startswith(("mailto:", "#", "data:")):
        return None
    t = t.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return (t.rsplit(".", 1)[0] if "." in t else t) or None


def extract_file(project: str, path: Path, root: Path) -> dict | None:
    """One document: a DOCUMENT node labeled by its first heading (else the
    filename), heading rationale, and link stems for the shared resolution
    pass. Same info-dict contract as the code extractors; `lang` is "doc" so
    build() turns its "imports" into `references` edges."""
    if path.suffix.lower() not in SUFFIXES:
        return None
    rel = str(path.relative_to(root))
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    node = codegraph.add_node(project, rel, path.stem, kind=codegraph.DOCUMENT)
    headings = _HEADING_RE.findall(text)
    for i, h in enumerate(headings[:_MAX_HEADINGS]):
        codegraph.add_rationale(project, rel, node["id"], h, slot=f"h{i}")

    links: list[str] = []
    for rx in (_WIKILINK_RE, _MDLINK_RE):
        for m in rx.finditer(text):
            stem = _stem(m.group(1))
            if stem and stem.lower() != path.stem.lower():
                links.append(stem)

    return {"qual": path.stem, "stem": path.stem, "module_id": node["id"],
            "rel": rel, "imports": links, "calls": [], "lang": "doc"}
