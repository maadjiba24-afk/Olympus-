"""Memory Wiki — durable concept pages, self-maintained by nightly dreaming.

Olympus's other memory layers are streams: dated notes (memory.py), typed
rows with decay (usermem.py), verified facts with TTL (facts.py). None of
them holds a *current best understanding* of a topic — the project you're
building, the people you work with, how your deploys work. That is what a
wiki page is: one canonical, updated-in-place document per concept.

Pages are written and refreshed by **dreaming**: a nightly consolidation job
(run from the heartbeat) that reads what accumulated since the last dream —
typed memories, lessons, corrections — and merges it into the page set:
new concepts get pages, existing pages get rewritten with the new
information folded in, stale pages get refreshed or flagged.

Freshness is linted, not assumed: every page carries a review horizon;
`lint()` reports pages past it, so the dream (and `olympus wiki lint`) can
see what's rotting. Pages marked durable (identity-grade facts) are exempt.

Storage: plain markdown files with a small frontmatter block under
memory/users/<user>/wiki/ — greppable, exportable, no new dependencies.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import config, memory

REVIEW_DEFAULT_DAYS = 90
MAX_PAGES = 200                  # per user — a wiki, not a landfill
MAX_PAGE_CHARS = 8000
DREAM_BATCH_CHARS = 12000        # how much new material one dream may read

DREAM_SYSTEM = (
    "You are Olympus's memory consolidation (its dreaming phase). You read "
    "what was learned recently and maintain a small wiki of durable concept "
    "pages — one page per lasting concept (a project, a person, a workflow, "
    "a standing preference). You rewrite pages to reflect the current best "
    "understanding: fold new facts in, drop what was superseded, keep each "
    "page short and factual. Do NOT create pages for one-off events or "
    "chit-chat. Return only the pages that need creating or updating."
)

DREAM_SCHEMA = {
    "type": "object",
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "review_after_days": {"type": "integer"},
                    "durable": {"type": "boolean"},
                },
                "required": ["title", "content"],
            },
        },
    },
    "required": ["pages"],
}


def _dir(user: str) -> Path:
    safe = memory.safe_id(user)
    base = (config.MEMORY_DIR / "users" / safe / "wiki") if safe != "shared" \
        else (config.MEMORY_DIR / "wiki")
    base.mkdir(parents=True, exist_ok=True)
    return base


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:64] or "untitled"


def _render(meta: dict, body: str) -> str:
    lines = ["---"]
    for key in ("title", "updated", "created", "review_after_days",
                "durable", "sources"):
        if key in meta and meta[key] not in (None, ""):
            lines.append(f"{key}: {meta[key]}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.strip() + "\n"


def _parse(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict = {}
    for line in parts[1].splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key or not value:
            continue
        if key == "review_after_days":
            try:
                meta[key] = int(value)
            except ValueError:
                pass
        elif key == "durable":
            meta[key] = value.lower() in ("true", "1", "yes")
        elif key in ("updated", "created"):
            try:
                meta[key] = float(value)
            except ValueError:
                pass
        else:
            meta[key] = value
    return meta, parts[2].lstrip("\n")


def upsert(user: str, title: str, content: str, *,
           review_after_days: int = REVIEW_DEFAULT_DAYS,
           durable: bool = False, sources: str = "",
           now: float | None = None) -> str:
    """Create or rewrite the page for `title`; returns its slug. Content is
    the page's full new body (pages are canonical, not append-only)."""
    now = now if now is not None else time.time()
    slug = slugify(title)
    path = _dir(user) / f"{slug}.md"
    created = now
    if path.exists():
        old_meta, _ = _parse(path.read_text(encoding="utf-8"))
        created = old_meta.get("created", now)
    meta = {"title": title.strip(), "updated": now, "created": created,
            "review_after_days": max(1, int(review_after_days)),
            "durable": durable, "sources": sources}
    path.write_text(_render(meta, content[:MAX_PAGE_CHARS]), encoding="utf-8")
    _enforce_cap(user)
    return slug


def _enforce_cap(user: str) -> None:
    files = sorted(_dir(user).glob("*.md"),
                   key=lambda p: p.stat().st_mtime)
    for path in files[:-MAX_PAGES] if len(files) > MAX_PAGES else []:
        path.unlink(missing_ok=True)


def read(user: str, slug: str) -> str:
    path = _dir(user) / f"{slugify(slug)}.md"
    if not path.exists():
        return f"No wiki page '{slug}'."
    meta, body = _parse(path.read_text(encoding="utf-8"))
    return f"# {meta.get('title', slug)}\n\n{body}"


def remove(user: str, slug: str) -> bool:
    path = _dir(user) / f"{slugify(slug)}.md"
    if not path.exists():
        return False
    path.unlink()
    return True


def pages(user: str) -> list[dict]:
    out = []
    for path in sorted(_dir(user).glob("*.md")):
        meta, body = _parse(path.read_text(encoding="utf-8"))
        out.append({"slug": path.stem, "title": meta.get("title", path.stem),
                    "updated": meta.get("updated", 0.0),
                    "review_after_days": meta.get("review_after_days",
                                                  REVIEW_DEFAULT_DAYS),
                    "durable": meta.get("durable", False),
                    "chars": len(body)})
    return out


def lint(user: str, now: float | None = None) -> list[str]:
    """Freshness/health report: stale pages (past their review horizon —
    durable pages exempt), empty pages, near-duplicate titles."""
    now = now if now is not None else time.time()
    issues: list[str] = []
    seen_tokens: list[tuple[str, set[str]]] = []
    for page in pages(user):
        age_days = (now - page["updated"]) / 86400
        if not page["durable"] and age_days > page["review_after_days"]:
            issues.append(f"stale: '{page['title']}' ({page['slug']}) — "
                          f"last updated {int(age_days)}d ago, review horizon "
                          f"{page['review_after_days']}d")
        if page["chars"] < 20:
            issues.append(f"empty: '{page['title']}' ({page['slug']})")
        tokens = set(re.findall(r"[a-z0-9]+", page["title"].lower()))
        for other_slug, other_tokens in seen_tokens:
            if tokens and other_tokens:
                overlap = len(tokens & other_tokens) / len(tokens | other_tokens)
                if overlap >= 0.6:
                    issues.append(f"near-duplicate: {page['slug']} vs "
                                  f"{other_slug}")
        seen_tokens.append((page["slug"], tokens))
    return issues


def summary(user: str) -> str:
    ps = pages(user)
    if not ps:
        return ("The wiki is empty — pages appear as Olympus consolidates "
                "what it learns (nightly dreaming).")
    lines = [f"Wiki ({len(ps)} pages):"]
    for p in ps:
        age = int((time.time() - p["updated"]) / 86400)
        tag = " [durable]" if p["durable"] else ""
        lines.append(f"  {p['slug']} — {p['title']}{tag} (updated {age}d ago)")
    issues = lint(user)
    if issues:
        lines.append(f"{len(issues)} freshness issue(s) — see `olympus wiki lint`.")
    return "\n".join(lines)


# --- retrieval into the pipeline ---------------------------------------------

def context_block(user: str, query: str, limit: int = 2,
                  budget_chars: int = 1600) -> str:
    """The most relevant wiki pages for `query`, as a compact prompt block.
    Keyword-scored like the rest of the lexical hot path — cheap and offline."""
    q_tokens = set(re.findall(r"[a-z0-9]+", (query or "").lower()))
    if not q_tokens:
        return ""
    scored = []
    for path in _dir(user).glob("*.md"):
        meta, body = _parse(path.read_text(encoding="utf-8"))
        hay = f"{meta.get('title', '')} {body}".lower()
        tokens = set(re.findall(r"[a-z0-9]+", hay))
        score = len(q_tokens & tokens) / max(1, len(q_tokens))
        if score >= 0.3:
            scored.append((score, meta.get("title", path.stem), body))
    if not scored:
        return ""
    scored.sort(key=lambda t: -t[0])
    parts = ["Concept pages (Olympus's consolidated understanding — may be "
             "refreshed by newer conversation):"]
    used = 0
    for _score, title, body in scored[:limit]:
        snippet = body.strip()[: max(200, budget_chars // limit)]
        parts.append(f"## {title}\n{snippet}")
        used += len(snippet)
        if used >= budget_chars:
            break
    return "\n\n" + "\n\n".join(parts) + "\n"


# --- dreaming (nightly consolidation) ----------------------------------------

def _recent_material(user: str, since: float) -> str:
    """What accumulated since the last dream: active typed memories and the
    latest lessons/corrections/feedback notes. Bounded — a dream reads a
    digest, not the archive."""
    parts: list[str] = []
    try:
        from . import usermem
        fresh = [m for m in usermem.active_memories(user)
                 if m.get("updated_at", m.get("created_at", 0)) >= since]
        if fresh:
            parts.append("Typed memories:\n" + "\n".join(
                f"- [{m.get('type')}] {m.get('content', '')[:300]}"
                for m in fresh[:40]))
    except Exception:
        pass
    try:
        memory.set_user(user)
        for category in ("lessons", "corrections", "feedback"):
            notes = memory.recent(category, n=6)
            if notes and not notes.startswith("(no "):
                parts.append(f"{category.title()}:\n{notes[:2500]}")
    except Exception:
        pass
    text = "\n\n".join(parts)
    return text[:DREAM_BATCH_CHARS]


def _dream_state_path(user: str) -> Path:
    return _dir(user) / ".dream_state.json"


def last_dream(user: str) -> float:
    try:
        return float(json.loads(_dream_state_path(user).read_text(
            encoding="utf-8")).get("last_dream", 0.0))
    except (OSError, ValueError):
        return 0.0


def _mark_dreamed(user: str, now: float) -> None:
    _dream_state_path(user).write_text(
        json.dumps({"last_dream": now}), encoding="utf-8")


def dream(user: str, runner=None, now: float | None = None) -> str:
    """One consolidation pass for `user`: read what's new since the last
    dream, merge it into the concept pages, refresh what lint flagged.
    `runner(system, prompt, schema) -> dict` is injectable for tests."""
    now = now if now is not None else time.time()
    since = last_dream(user)
    material = _recent_material(user, since)
    issues = lint(user, now)
    if not material.strip() and not issues:
        _mark_dreamed(user, now)
        return "nothing new to consolidate"

    index = "\n".join(f"- {p['slug']}: {p['title']} "
                      f"(updated {int((now - p['updated']) / 86400)}d ago)"
                      for p in pages(user)) or "(no pages yet)"
    prompt = (
        f"Existing wiki pages:\n{index}\n\n"
        + (f"Freshness warnings:\n" + "\n".join(f"- {i}" for i in issues)
           + "\n\n" if issues else "")
        + (f"New material since the last consolidation:\n{material}\n\n"
           if material.strip() else "")
        + "Maintain the wiki: return the pages to create or rewrite (full "
          "new content for each). A page must be a lasting concept. If a "
          "stale page is still correct, return it with its content "
          "unchanged to refresh it; if it's obsolete, return it with "
          "content 'OBSOLETE' to retire it."
    )

    if runner is None:
        def runner(system: str, prompt: str, schema: dict) -> dict:
            from . import backend
            settings = config.ModelPool.from_env().for_role("reasoning")
            return backend.complete_json(
                settings, system, [{"role": "user", "content": prompt}],
                schema, effort="low")

    try:
        result = runner(DREAM_SYSTEM, prompt, DREAM_SCHEMA)
    except Exception as err:
        return f"dream failed: {err}"

    written, retired = 0, 0
    for page in (result or {}).get("pages", [])[:25]:
        title = (page.get("title") or "").strip()
        content = (page.get("content") or "").strip()
        if not title or not content:
            continue
        if content.upper() == "OBSOLETE":
            if remove(user, slugify(title)):
                retired += 1
            continue
        upsert(user, title, content,
               review_after_days=int(page.get("review_after_days")
                                     or REVIEW_DEFAULT_DAYS),
               durable=bool(page.get("durable", False)), now=now)
        written += 1
    _mark_dreamed(user, now)
    return f"consolidated: {written} page(s) written, {retired} retired"


def users_with_material() -> list[str]:
    """Users whose memory has anything to consolidate — the shared namespace,
    every per-user notes directory, and every user with typed memories in the
    kv store."""
    out = {"shared"}
    users_dir = config.MEMORY_DIR / "users"
    if users_dir.exists():
        out |= {p.name for p in users_dir.iterdir() if p.is_dir()}
    try:
        from . import store
        out |= set(store.backend().keys("usermem.memories"))
    except Exception:
        pass
    return sorted(out)


def dream_all(now: float | None = None, runner=None) -> list[str]:
    """The heartbeat entrypoint: one dream per user with material."""
    log = []
    for user in users_with_material():
        try:
            result = dream(user, runner=runner, now=now)
            if "nothing new" not in result:
                log.append(f"{user}: {result}")
        except Exception as err:
            log.append(f"{user}: dream errored: {err}")
    return log
