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


def scan_reason(parsed: dict) -> str | None:
    """Security scan for an imported skill; reason string when it must be
    refused, None when clean. A skill is durable agent *instructions*, so an
    injection payload here poisons every future run that loads it — imports
    are scanned the way live web content is, and fail closed."""
    from . import security
    body = "\n".join(str(parsed.get(k, ""))
                     for k in ("name", "description", "instructions"))
    if security.looks_like_injection(body):
        return ("it contains prompt-injection markers (e.g. 'ignore previous "
                "instructions', tool commands). Edit the file and re-import "
                "if it is legitimate")
    if security._KEYISH.search(body) or security._URL_CRED.search(body):
        return ("it contains something shaped like a credential — skills are "
                "shared instructions and must never embed secrets")
    return None


def import_file(path: str, *, provisional: bool = False) -> str:
    """Import a SKILL.md (or a directory containing one) into the Olympus
    library. Imported skills are permanent by default (they're curated); pass
    provisional=True to route them through the benchmark gate instead.
    Every import is security-scanned first (injection markers, embedded
    credentials) and refused — not sanitized — on a hit."""
    p = Path(path)
    if p.is_dir():
        p = p / "SKILL.md"
    if not p.is_file():
        return f"Error: no SKILL.md found at {path}"
    parsed = parse_skill_md(p.read_text(encoding="utf-8", errors="replace"))
    reason = scan_reason(parsed)
    # Behavioral contract at the import chokepoint (defense in depth): a skill
    # that fails the security scan is refused — formalizing the check below.
    from . import behavioral_contracts as _abc
    try:
        _abc.enforce("skill.import", {"scan_reason": reason})
    except _abc.ContractViolation as viol:
        return (f"Error: refused to import '{parsed.get('name') or p.name}' — "
                f"{'; '.join(viol.reasons)}.")
    if reason:
        return (f"Error: refused to import '{parsed.get('name') or p.name}' — "
                f"{reason}.")
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


# --- import from public GitHub URLs ---------------------------------------
# Adopted from Odysseus (import SKILL.md bundles straight from a repo URL).
# Remote skills are ALWAYS imported provisional: they're third-party
# instructions, so beyond the injection/credential scan they must also prove
# themselves through the same benchmark gate as self-written skills.

_MAX_ARCHIVE = 20 * 1024 * 1024      # tarball cap
_MAX_SKILL = 100 * 1024              # single SKILL.md cap
_MAX_SKILLS_PER_IMPORT = 50

_GH_BLOB = None                       # compiled lazily (module import stays cheap)
_GH_TREE = None


def _fetch_bytes(url: str, cap: int = _MAX_ARCHIVE) -> bytes:
    """Fetch a remote file over the same SSRF/rebinding-pinned path as web
    fetches, with a size cap. Raises ValueError when blocked or oversized."""
    from . import security, tools
    leak = security.secret_exfil_reason(url)
    if leak:
        raise ValueError(f"blocked: {leak}")
    reason = security.url_block_reason(url)
    if reason:
        raise ValueError(reason)
    req = tools._urlreq.Request(url, headers={"User-Agent": tools._UA})
    with tools._pinned_opener().open(req, timeout=60) as resp:
        blob = resp.read(cap + 1)
    if len(blob) > cap:
        raise ValueError(f"remote file exceeds the {cap // (1024 * 1024)}MB cap")
    return blob


def _import_text(text: str, origin: str, *, provisional: bool) -> str:
    parsed = parse_skill_md(text)
    reason = scan_reason(parsed)
    if reason:
        return (f"Error: refused to import '{parsed.get('name') or origin}' — "
                f"{reason}.")
    return skills.create(parsed["name"], parsed["description"],
                         parsed["instructions"],
                         specialist=parsed["specialist"],
                         provisional=provisional)


def _import_tarball(blob: bytes, subpath: str | None) -> list[str]:
    """Import every SKILL.md inside a repo tarball — read in memory, never
    extracted to disk (no path-traversal surface)."""
    import io
    import tarfile
    out: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile() or member.size > _MAX_SKILL:
                continue
            # member names look like "<repo>-<ref>/path/to/SKILL.md"
            parts = member.name.split("/", 1)
            rel = parts[1] if len(parts) == 2 else parts[0]
            if Path(rel).name != "SKILL.md":
                continue
            if subpath and not rel.startswith(subpath.rstrip("/") + "/") \
                    and rel != f"{subpath.rstrip('/')}/SKILL.md" \
                    and not rel.startswith(subpath.rstrip("/")):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            text = fh.read().decode("utf-8", errors="replace")
            out.append(_import_text(text, rel, provisional=True))
            if len(out) >= _MAX_SKILLS_PER_IMPORT:
                out.append(f"…stopped at {_MAX_SKILLS_PER_IMPORT} skills.")
                break
    return out


def _default_branch(owner: str, repo: str) -> str:
    import json
    try:
        data = json.loads(_fetch_bytes(
            f"https://api.github.com/repos/{owner}/{repo}",
            cap=1024 * 1024).decode("utf-8", "replace"))
        return str(data.get("default_branch") or "main")
    except Exception:
        return "main"


def import_url(url: str) -> list[str]:
    """Import skills from a public URL: a direct SKILL.md link, a GitHub blob
    link, or a GitHub repo/tree URL (the whole bundle). Remote imports are
    always provisional — the benchmark gate decides if they stay."""
    global _GH_BLOB, _GH_TREE
    import re
    if _GH_BLOB is None:
        _GH_BLOB = re.compile(
            r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")
        _GH_TREE = re.compile(
            r"^https://github\.com/([^/]+)/([^/]+?)"
            r"(?:\.git)?(?:/tree/([^/]+)(?:/(.*))?)?/?$")
    url = url.strip()
    m = _GH_BLOB.match(url)
    if m:                                     # blob page → raw file
        owner, repo, ref, path = m.groups()
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    if url.lower().endswith(".md"):           # direct SKILL.md (any host)
        try:
            text = _fetch_bytes(url, cap=_MAX_SKILL).decode("utf-8", "replace")
        except ValueError as err:
            return [f"Error: {err}"]
        return [_import_text(text, url, provisional=True)]
    m = _GH_TREE.match(url)
    if not m:
        return ["Error: expected a SKILL.md link or a github.com repo/tree URL."]
    owner, repo, ref, subpath = m.groups()
    ref = ref or _default_branch(owner, repo)
    try:
        blob = _fetch_bytes(
            f"https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}")
    except ValueError as err:
        return [f"Error: {err}"]
    except Exception as err:
        return [f"Error: could not download {owner}/{repo}@{ref}: "
                f"{str(err)[:120]}"]
    try:
        msgs = _import_tarball(blob, subpath)
    except Exception as err:
        return [f"Error: could not read the archive: {str(err)[:120]}"]
    return msgs or [f"No SKILL.md files found in {owner}/{repo}@{ref}."]


# --- curated starter pack ------------------------------------------------
# A tiny, opt-in set of general-purpose skills, installed PROVISIONAL so they
# go through the same benchmark gate as anything the system writes itself —
# nothing curated gets a free pass. They pair with scheduled jobs (a cron job
# can name one via `schedule_task(..., skill=...)`).

STARTER_PACK: tuple[dict, ...] = (
    {"name": "daily-brief",
     "description": "A concise morning brief: what changed, what needs a "
                    "decision today, and the single most important next action.",
     "instructions": (
         "Produce a short daily brief. Structure:\n"
         "1. **Headlines** — 2-4 bullets of what actually changed since "
         "yesterday (skip noise).\n"
         "2. **Needs a decision** — anything waiting on the user, with the "
         "options stated plainly.\n"
         "3. **One next action** — the single highest-leverage thing to do "
         "today, and why.\n"
         "Be ruthless about brevity; lead with the decision, cut the filler.")},
    {"name": "weekly-report",
     "description": "A structured weekly report: progress, blockers, metrics, "
                    "and next week's focus.",
     "instructions": (
         "Write a weekly report with these sections: **Shipped** (concrete "
         "outcomes), **In progress** (with % done), **Blockers** (and what "
         "would unblock each), **Metrics** (numbers that moved), **Next week** "
         "(top 3 priorities). Keep each bullet to one line; prefer numbers to "
         "adjectives.")},
    {"name": "inbox-triage",
     "description": "Turn a pile of messages into a prioritized, actionable "
                    "list with suggested replies.",
     "instructions": (
         "Given a set of messages, triage them: group into **Urgent / "
         "reply today**, **Can wait**, and **FYI / no action**. For each item "
         "in the first two groups, give a one-line suggested reply or next "
         "step. Never send anything — only propose. Flag anything that looks "
         "like a deadline or a commitment.")},
)


def install_starter_pack() -> list[str]:
    """Install the curated starter skills (PROVISIONAL). Returns messages."""
    out = []
    for s in STARTER_PACK:
        out.append(skills.create(s["name"], s["description"], s["instructions"],
                                 provisional=True))
    return out
