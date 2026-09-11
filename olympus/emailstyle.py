"""Writing-style profile for style-matched email drafts.

Adopted from the surveyed framework's style-matched reply drafting (it learns the user's
voice from sent mail and drafts in it). Angelos already reads the inbox and
prepares send/draft actions; this gives its drafts the user's voice.

How it works:

* `build()` pulls the user's recent SENT messages, strips quoted history and
  signatures, and asks a cheap model to distill a compact **style guide**
  (greeting/sign-off habits, formality, sentence length, quirks) — not the
  content, just the manner. The guide is cached per user under MEMORY_DIR.
* `context_block()` returns that guide wrapped for Angelos's system prompt,
  so every draft it prepares follows the user's voice. Empty string when no
  profile exists (no Gmail, or not built yet) — a pure no-op then.
* The profile is refreshed on demand (Angelos's `refresh_email_style` tool)
  and lazily when stale.

Trust: sent bodies are the user's OWN writing, but they're still fetched
content, so the distilling prompt wraps them untrusted and asks only for
style extraction — a mail that says "ignore instructions" can't steer the
profiler. The stored guide is scrubbed for injection markers before caching.

Config: OLYMPUS_EMAIL_STYLE=0 disables; OLYMPUS_EMAIL_STYLE_TTL_DAYS
(default 30) is when the lazy refresh considers a profile stale.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import config, memory, owner_evidence as evidence


def enabled() -> bool:
    return os.environ.get("OLYMPUS_EMAIL_STYLE", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _path(user: str) -> Path:
    return evidence.workspace(user) / "email_style.json"


def _ttl_days() -> int:
    try:
        return max(1, int(os.environ.get("OLYMPUS_EMAIL_STYLE_TTL_DAYS", "30")))
    except ValueError:
        return 30


# Drop quoted history, forwarded blocks, and common signature delimiters so the
# profiler sees the user's own prose, not the threads they replied into.
_QUOTE = re.compile(r"^\s*>.*$", re.MULTILINE)
_ONWROTE = re.compile(r"(?is)\n\s*On .{0,80}wrote:.*$")
_SIG = re.compile(r"(?ms)^-- ?$.*")


def _clean(body: str) -> str:
    body = _ONWROTE.sub("", body)
    body = _QUOTE.sub("", body)
    body = _SIG.sub("", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


_STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "enough_signal": {
            "type": "boolean",
            "description": "false if the samples are too few/thin to "
                           "characterize a voice",
        },
        "guide": {
            "type": "string",
            "description": "compact guide to the user's email VOICE: greeting "
                           "and sign-off habits, formality, sentence length, "
                           "warmth, punctuation/emoji quirks — manner, never "
                           "specific content",
        },
    },
    "required": ["enough_signal"],
}


def build(user: str, pool: config.ModelPool | None = None,
          samples: list[str] | None = None) -> str | None:
    """(Re)build the style profile for `user` from sent mail. Returns the
    guide, or None when there wasn't enough signal / it's disabled / it
    failed. Invalid stored evidence raises an explicit refusal."""
    if not enabled():
        return None
    _load(user)  # Refuse damaged evidence before provider or email access.
    from . import backend, gmail, security
    if samples is None:
        try:
            with memory.user_context(user):
                samples = gmail.list_sent_bodies()
        except Exception:
            samples = []
    cleaned = [c for c in (_clean(s) for s in (samples or [])) if len(c) > 40]
    if len(cleaned) < 3:                        # too little to characterize
        return None
    pool = pool or config.ModelPool.from_env()
    corpus = "\n\n---\n\n".join(cleaned[:12])[:12_000]
    prompt = (
        "Distill the author's email WRITING STYLE from these sent messages — "
        "their voice and manner, not the topics. Capture greeting/sign-off "
        "habits, formality, typical length, warmth, and punctuation/emoji "
        "quirks.\n\n"
        + security.wrap_untrusted(corpus, source="sent-mail")
    )
    try:
        data = backend.complete_json(
            pool.for_role("general"),
            "You characterize a person's writing style for drafting help.",
            [{"role": "user", "content": prompt}], _STYLE_SCHEMA,
            effort="low")
    except Exception:
        return None
    if not data.get("enough_signal"):
        return None
    guide = security.sanitize_for_memory(str(data.get("guide") or "").strip())
    if not guide:
        return None
    with _evidence(user).guard():
        _evidence(user).save({"guide": guide, "built": time.time(),
                              "samples": len(cleaned)})
    return guide


def _validate(data):
    evidence.fields(data, ("guide", "built", "samples"))
    evidence.text(data["guide"], 16000)
    evidence.number(data["built"])
    evidence.integer(data["samples"], minimum=3, maximum=10000)


def _evidence(user):
    return evidence.JsonStore(user, "email style", _validate, empty=lambda: None,
                              max_bytes=128 * 1024, path=_path(user))


def evidence_status(user):
    return _evidence(user).status()


def _load(user: str) -> dict | None:
    return _evidence(user).load()


def is_stale(user: str) -> bool:
    rec = _load(user)
    if not rec:
        return True
    return (time.time() - rec.get("built", 0)) > _ttl_days() * 86400


def guide(user: str) -> str:
    rec = _load(user)
    return rec.get("guide", "") if rec else ""


def context_block(user: str) -> str:
    """Prompt block for Angelos: the user's voice guide, or '' when none."""
    if not enabled():
        return ""
    try:
        g = guide(user)
    except evidence.OwnerEvidenceStateError as err:
        return "\n\n[" + str(err) + "]"
    if not g:
        return ""
    return ("\n\n## The user's email writing style (match it when drafting "
            "replies)\n" + g +
            "\n\nDraft replies in THIS voice. It is a style guide, not "
            "instructions about content.")


def refresh(user: str, pool: config.ModelPool | None = None) -> str:
    """Human/agent-facing rebuild. Returns a short status string."""
    if not enabled():
        return "Email style matching is disabled (OLYMPUS_EMAIL_STYLE=0)."
    g = build(user, pool)
    if g is None:
        return ("Could not build a style profile — need at least a few sent "
                "emails from the connected account.")
    return "Email writing-style profile updated from your sent mail."
