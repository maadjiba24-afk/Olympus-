"""The skill library Olympus builds for itself.

A skill is a reusable how-to document distilled from experience: lessons,
corrections, feedback, and watched videos get synthesized into named skills
by Metis (daily) and any specialist who learns something procedural. Every
specialist sees the skill index and loads relevant skills on demand — so
knowledge gained once is applied by the whole council forever.

Autonomously-created skills are written **provisional** and proven by a
benchmark before they become permanent (see orchestrator.gate_skills): if a
new/changed skill doesn't measurably raise the affected specialist's score, it is
reverted. That makes the autonomous self-improvement path safe enough to run
without a human in the loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import config

# Near-duplicate cosine threshold for write-time skill dedup. Tunable; high by
# default so only genuinely-overlapping skills are flagged.
_DUP_THRESHOLD = float(os.environ.get("OLYMPUS_SKILL_DUP_THRESHOLD", "0.90"))


def _dir() -> Path:
    d = config.MEMORY_DIR / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backup_dir() -> Path:
    d = config.MEMORY_DIR / "skill_backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "skill"


def _meta(text: str, key: str) -> str | None:
    m = re.search(rf"<!--\s*{key}:\s*(.*?)\s*-->", text)
    return m.group(1) if m else None


def _title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def create(name: str, description: str, instructions: str, *,
           specialist: str | None = None, provisional: bool = False) -> str:
    """Create or overwrite a skill. Returns a confirmation message.

    `specialist` tags which specialist the skill primarily serves (used to
    pick which benchmark gates it). `provisional` marks it unproven until a
    benchmark promotes it.
    """
    path = _dir() / f"{_slug(name)}.md"
    existed = path.exists()
    if existed:  # back up the prior version so a revert can restore it
        (_backup_dir() / path.name).write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8")
    elif (_backup_dir() / path.name).exists():
        # brand-new skill: clear any stale backup so revert means "delete"
        (_backup_dir() / path.name).unlink()

    header = ""
    if specialist:
        header += f"<!-- specialist: {specialist} -->\n"
    if provisional:
        header += "<!-- provisional -->\n"
    path.write_text(
        f"{header}# {name.strip()}\n\n"
        f"> {description.strip()}\n\n"
        f"{instructions.strip()}\n\n"
        f"_Last updated: {time.strftime('%Y-%m-%d %H:%M')}_\n",
        encoding="utf-8",
    )
    verb = "updated" if existed else "created"
    tag = " (provisional — pending benchmark)" if provisional else ""
    msg = f"Skill '{name}' {verb}{tag}. It is visible to every specialist."

    # Best-effort semantic layer: warm this skill's embedding, and — only for a
    # genuinely NEW skill — flag near-duplicates so the library doesn't accrete
    # overlapping skills. No-op (and no cost) when embeddings aren't configured.
    dups = [] if existed else near_duplicates(name, description, instructions)
    _skill_embedding(_slug(name), _emb_text(name, description, instructions))
    if dups:
        listed = ", ".join(f"'{d['name']}' ({d['score']:.2f})" for d in dups[:3])
        msg += (f"\n⚠ Near-duplicate of existing skill(s): {listed}. "
                "Consider consolidating instead of adding overlap.")
    return msg


def read(name: str) -> str:
    path = _dir() / f"{_slug(name)}.md"
    if not path.exists():
        available = ", ".join(p.stem for p in sorted(_dir().glob("*.md"))) or "none"
        return f"No skill named '{name}'. Available skills: {available}"
    return path.read_text(encoding="utf-8", errors="replace")[:20_000]


# --- semantic layer (best-effort; only active when an embeddings endpoint is
# configured, exactly like recall.py — the hot path stays free otherwise) -----

def _emb_text(name: str, description: str, instructions: str) -> str:
    """The text a skill is embedded from: its name + description + a bounded
    slice of the instructions (enough to capture intent without huge payloads)."""
    return f"{name}\n{description}\n{instructions}".strip()[:4000]


def _emb_cache_path() -> Path:
    return _dir() / ".embeddings.json"


def _load_emb_cache() -> dict:
    p = _emb_cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_emb_cache(cache: dict) -> None:
    p = _emb_cache_path()
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _skill_embedding(slug: str, emb_text: str) -> list[float] | None:
    """Embedding for a skill, cached by content hash so re-embedding only
    happens when the skill text changes. Best-effort: None when embeddings are
    unavailable or the call fails."""
    from . import embed
    if not embed.available():
        return None
    h = _text_hash(emb_text)
    model = embed._model() or ""
    cache = _load_emb_cache()
    ent = cache.get(slug)
    # Invalidate on a model change too: vectors from a different embedding model
    # live in a different space (often a different dimension), so a stale entry
    # would score 0 against a freshly-embedded query — silently breaking search
    # until every skill was re-saved. Re-embed when the model or content changes.
    if (isinstance(ent, dict) and ent.get("hash") == h
            and ent.get("model") == model and ent.get("vec")):
        return ent["vec"]
    vec = embed.embed_one(emb_text)
    if vec:
        cache[slug] = {"hash": h, "model": model, "vec": vec}
        _save_emb_cache(cache)
    return vec


def _skill_emb_text_from_file(text: str) -> str:
    """Reconstruct the embedding text from a stored skill file."""
    name = _title(text)
    desc = ""
    body_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("> ") and not desc:
            desc = s[2:].strip()
        elif s and not s.startswith(("<!--", "# ", "> ", "_Last updated:")):
            body_lines.append(s)
    return _emb_text(name, desc, "\n".join(body_lines))


def search(query: str, limit: int = 5,
           specialist: str | None = None) -> list[dict]:
    """Semantic (cosine-ranked) skill search. Best-effort: returns [] when no
    embeddings endpoint is configured, so callers fall back to the prompt
    `index`. Each hit is {name, slug, score}."""
    from . import embed
    if not embed.available():
        return []
    qvec = embed.embed_one(query)
    if not qvec:
        return []
    scored: list[tuple[float, str, str]] = []
    for path in sorted(_dir().glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _visible_to(_meta(text, "specialist"), specialist):
            continue
        vec = _skill_embedding(path.stem, _skill_emb_text_from_file(text))
        if not vec:
            continue
        scored.append((embed.cosine(qvec, vec), _title(text) or path.stem,
                       path.stem))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [{"name": n, "slug": s, "score": round(c, 3)}
            for c, n, s in scored[:limit] if c > 0]


def near_duplicates(name: str, description: str, instructions: str,
                    threshold: float | None = None) -> list[dict]:
    """Existing skills semantically near a proposed one (cosine ≥ threshold),
    excluding the same slug. Best-effort: [] when embeddings are unavailable —
    so dedup never blocks a save, it only informs. Each hit is {name, score}."""
    from . import embed
    if not embed.available():
        return []
    thr = _DUP_THRESHOLD if threshold is None else threshold
    nvec = embed.embed_one(_emb_text(name, description, instructions))
    if not nvec:
        return []
    self_slug = _slug(name)
    out: list[dict] = []
    for path in sorted(_dir().glob("*.md")):
        if path.stem == self_slug:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        vec = _skill_embedding(path.stem, _skill_emb_text_from_file(text))
        if not vec:
            continue
        c = embed.cosine(nvec, vec)
        if c >= thr:
            out.append({"name": _title(text) or path.stem, "score": round(c, 3)})
    out.sort(key=lambda d: d["score"], reverse=True)
    return out


GLOBAL_SPECIALIST = "all"   # tag a skill `specialist: all` to share it with everyone


def _visible_to(owner: str | None, specialist: str | None) -> bool:
    """Whether a skill owned by `owner` should appear for `specialist`.

    `specialist=None` means "show everything" (the human `olympus skills` view).
    A skill with no owner, or owner 'all', is global. A skill tagged for one
    specialist is shown ONLY to that specialist — so a benchmark-gated skill
    can't silently degrade other specialists that merely see it in a shared
    index (the cross-contamination the gate couldn't catch)."""
    if specialist is None:
        return True
    if not owner or owner == GLOBAL_SPECIALIST:
        return True
    return owner == specialist


def _index_line(path: Path, text: str) -> str:
    """The one-line index entry for a skill file: `- Name: description (prov)`."""
    name = _title(text) or path.stem
    desc = ""
    for line in text.splitlines():
        if line.startswith("> "):
            desc = line[2:].strip()
            break
    prov = " (provisional)" if _meta(text, "provisional") is not None \
        or "<!-- provisional -->" in text else ""
    return f"- {name}: {desc}{prov}"


def index(specialist: str | None = None) -> str:
    """One line per skill. With `specialist`, scope to the skills that
    specialist should actually see (its own + global); without it, list all."""
    lines = []
    for path in sorted(_dir().glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _visible_to(_meta(text, "specialist"), specialist):
            continue
        lines.append(_index_line(path, text))
    return "\n".join(lines) if lines else "(no skills built yet)"


def scoped_index(specialist: str | None, task: str,
                 limit: int = 12) -> str | None:
    """The top-`limit` skills most relevant to `task` (semantic cosine),
    formatted exactly like `index` so `read_skill` still resolves each name.

    Returns None when embeddings are unavailable or nothing scores > 0, so a
    caller falls back to the full `index`. Visibility (own + global) is enforced
    by `search`, so a task-scoped block never leaks another specialist's skills.
    Every listed skill remains loadable by name, and any unlisted skill is still
    reachable via `read_skill` — scoping trims the *prompt*, not the library."""
    hits = search(task, limit=limit, specialist=specialist)
    if not hits:
        return None
    lines = []
    for h in hits:
        path = _dir() / f"{h['slug']}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines.append(_index_line(path, text))
    return "\n".join(lines) if lines else None


def count() -> int:
    return len(list(_dir().glob("*.md")))


def list_all() -> list[dict]:
    """Metadata for every visible skill — the curator's working set."""
    out = []
    for path in sorted(_dir().glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        desc = next((ln[2:].strip() for ln in text.splitlines()
                     if ln.startswith("> ")), "")
        m = re.search(r"_Last updated: (.*?)_", text)
        out.append({"name": _title(text) or path.stem,
                    "specialist": _meta(text, "specialist"),
                    "provisional": "<!-- provisional -->" in text,
                    "description": desc,
                    "updated": m.group(1) if m else "",
                    "chars": len(text)})
    return out


def archive(name: str, reason: str = "") -> str:
    """Retire a skill: move it into skill_backups/ as pruned-<date>-<slug>.md
    (recoverable by hand) instead of deleting outright."""
    path = _dir() / f"{_slug(name)}.md"
    if not path.exists():
        return f"no skill named '{name}'"
    dest = _backup_dir() / f"pruned-{time.strftime('%Y%m%d')}-{path.name}"
    body = path.read_text(encoding="utf-8", errors="replace")
    if reason:
        body = f"<!-- pruned: {reason} -->\n" + body
    dest.write_text(body, encoding="utf-8")
    path.unlink()
    return f"archived '{name}' → skill_backups/{dest.name}"


# --- provisional lifecycle (benchmark gating) ----------------------------

def list_provisional() -> list[tuple[str, str | None]]:
    """Return (skill_name, specialist) for every provisional skill."""
    out = []
    for path in _dir().glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "<!-- provisional -->" in text:
            out.append((_title(text) or path.stem, _meta(text, "specialist")))
    return out


def set_hidden(name: str, hidden: bool) -> None:
    """Hide/unhide a skill from the index so before/after benchmarks isolate
    its effect. Hidden skills are renamed .md -> .hidden."""
    base = _dir() / f"{_slug(name)}.md"
    hide = _dir() / f"{_slug(name)}.hidden"
    if hidden and base.exists():
        base.rename(hide)
    elif not hidden and hide.exists():
        hide.rename(base)


def promote(name: str) -> None:
    """Clear the provisional flag — the skill has proven itself."""
    path = _dir() / f"{_slug(name)}.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r"<!--\s*provisional\s*-->\n?", "", text),
                    encoding="utf-8")


def revert(name: str) -> str:
    """Undo a provisional skill: restore its prior version, or delete it if it
    was brand new."""
    path = _dir() / f"{_slug(name)}.md"
    backup = _backup_dir() / f"{_slug(name)}.md"
    if backup.exists():
        path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        backup.unlink()
        return f"reverted '{name}' to previous version"
    if path.exists():
        path.unlink()
        return f"removed new skill '{name}'"
    return f"nothing to revert for '{name}'"
