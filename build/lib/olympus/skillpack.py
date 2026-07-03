"""agentskills.io interop — import and export skills in the open standard.

Olympus builds its own skill library (see skills.py), but those files use an
Olympus-specific layout. The agentskills.io standard (also used by Hermes and
others) packages a skill as a directory containing a ``SKILL.md`` with YAML
frontmatter (``name``, ``description``, …) followed by the instructions. This
module bridges the two so skills are portable in both directions — Olympus can
consume community skill packs and publish its own.

Frontmatter is parsed/emitted with a tiny hand-rolled reader (a flat
``key: value`` block) so there's no YAML dependency, matching the project's
stdlib-only stance.
"""

from __future__ import annotations

from pathlib import Path

from . import skills


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md into ({frontmatter}, body). Tolerates a missing block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        key, sep, val = lines[i].partition(":")
        if sep:
            meta[key.strip().lower()] = val.strip().strip('"\'')
        i += 1
    body = "\n".join(lines[i + 1:]).strip() if i < len(lines) else ""
    return meta, body


def parse_skill_md(text: str) -> dict:
    """Normalize a SKILL.md into {name, description, instructions, specialist}."""
    meta, body = _parse_frontmatter(text)
    name = meta.get("name", "")
    description = meta.get("description", "")
    specialist = meta.get("specialist") or None
    instructions = body
    if not name:                       # fall back to a leading '# Heading'
        for line in body.splitlines():
            if line.startswith("# "):
                name = line[2:].strip()
                break
    return {"name": name or "imported-skill", "description": description,
            "instructions": instructions, "specialist": specialist}


def to_skill_md(name: str) -> str:
    """Render an existing Olympus skill as agentskills.io SKILL.md text."""
    raw = skills.read(name)
    if raw.startswith("No skill named"):
        raise ValueError(raw)
    title = skills._title(raw) or name
    description = ""
    instr_lines, in_body = [], False
    for line in raw.splitlines():
        if line.startswith("> ") and not description:
            description = line[2:].strip()
            in_body = True
            continue
        if line.startswith("#") or line.startswith("<!--"):
            continue
        if line.startswith("_Last updated"):
            continue
        if in_body:
            instr_lines.append(line)
    specialist = skills._meta(raw, "specialist")
    front = ["---", f"name: {title}", f"description: {description}"]
    if specialist:
        front.append(f"specialist: {specialist}")
    front.append("---")
    body = "\n".join(instr_lines).strip()
    return "\n".join(front) + "\n\n" + body + "\n"


def export(name: str, dest_dir: str) -> str:
    """Write `<dest_dir>/<slug>/SKILL.md`. Returns the file path."""
    slug = skills._slug(name)
    out = Path(dest_dir) / slug
    out.mkdir(parents=True, exist_ok=True)
    path = out / "SKILL.md"
    path.write_text(to_skill_md(name), encoding="utf-8")
    return str(path)


def import_file(path: str, *, provisional: bool = False) -> str:
    """Import a SKILL.md (or a directory containing one) into the Olympus
    library. Imported skills are permanent by default (they're curated); pass
    provisional=True to route them through the benchmark gate instead."""
    p = Path(path)
    if p.is_dir():
        p = p / "SKILL.md"
    if not p.is_file():
        return f"Error: no SKILL.md found at {path}"
    parsed = parse_skill_md(p.read_text(encoding="utf-8", errors="replace"))
    return skills.create(parsed["name"], parsed["description"],
                         parsed["instructions"],
                         specialist=parsed["specialist"],
                         provisional=provisional)


def import_dir(root: str, *, provisional: bool = False) -> list[str]:
    """Import every SKILL.md found under `root`. Returns confirmation messages."""
    base = Path(root)
    out = []
    for skill_md in sorted(base.rglob("SKILL.md")):
        out.append(import_file(str(skill_md), provisional=provisional))
    return out
