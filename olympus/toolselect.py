"""Per-turn dynamic tool selection — a slimmer loadout for the actual request.

Adopted from the surveyed framework's per-request tool index (their answer to "tool schemas
eat the context before the user request starts"): instead of always sending a
specialist's whole catalog, rank the loadout against the current task and keep
only the most relevant tail of big loadouts. Every schema not sent is prompt
tokens saved on every round of the agent loop — which is real money on a
pricing-aware pool and real headroom on a small-context local model.

Guarantees, in order of importance:

* **Selection only ever drops.** It runs on the loadout AFTER capability
  separation (security.filter_tools) and the conversation capability profile,
  and returns a subset — so it can never re-add a tool a security layer
  stripped, no matter how relevant it scores.
* **Essentials are never dropped**: the BASE tools every specialist's prompt
  assumes (memory/facts/lessons/skills/time/sessions), server-side tools
  (web_search / web_fetch / code_execution ride a `type`, not a client
  schema), and the generic actuator `prepare_action` (the model is told
  throughout its prompts that preparing an action is always available).
* **Small loadouts pass through untouched.** Trimming a 9-tool loadout saves
  nothing and risks a miss; only loadouts whose droppable tail exceeds the
  cap are ranked at all.
* **Deterministic and dependency-free.** Pure lexical scoring (name parts
  weighted over description words, prefix-stemmed), stable order, no I/O, no
  model call — the same task always yields the same loadout, which keeps
  replay/witness runs reproducible.

Config:
  OLYMPUS_TOOL_SELECT      "0" disables selection entirely (default on).
  OLYMPUS_TOOL_SELECT_MAX  cap on non-essential tools kept (default 16 —
                           conservative; drop it to 6-8 for 4k/8k-context
                           local models).
"""

from __future__ import annotations

import os
import re

# Never dropped: the base loadout every specialist prompt assumes is present,
# plus the generic actuator. (Server-side defs are protected structurally —
# they carry a "type" instead of an input_schema — see _protected.)
_ESSENTIAL = frozenset({
    "recall_memory", "recall_fact", "save_lesson", "read_skill",
    "current_time", "search_sessions", "prepare_action", "ask_user",
})

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "your",
    "you", "are", "was", "were", "has", "have", "had", "not", "but", "all",
    "any", "can", "use", "using", "used", "about", "what", "which", "when",
    "where", "how", "why", "please", "then", "than", "them", "they", "its",
    "it's", "will", "would", "should", "could", "need", "want", "make",
    "give", "get", "one", "two", "new", "our", "out", "now",
})

_TOKEN = re.compile(r"[a-z0-9]+")
_STEM = 6          # prefix length for crude stemming ("email"~"emails")


def enabled() -> bool:
    return os.environ.get("OLYMPUS_TOOL_SELECT", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def max_kept() -> int:
    try:
        return max(1, int(os.environ.get("OLYMPUS_TOOL_SELECT_MAX", "16")))
    except ValueError:
        return 16


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower())
            if len(t) >= 3 and t not in _STOPWORDS}


def _stems(tokens: set[str]) -> set[str]:
    return {t[:_STEM] for t in tokens}


def score(task_tokens: set[str], d: dict) -> int:
    """Lexical relevance of one tool def to the task. Name-part hits weigh 3
    (a task that says 'email' almost certainly wants the email tools);
    description hits weigh 1."""
    task_stems = _stems(task_tokens)
    name_parts = _tokens((d.get("name") or "").replace("_", " "))
    desc_parts = _tokens(str(d.get("description") or ""))
    s = 3 * len(task_stems & _stems(name_parts))
    s += len(task_stems & _stems(desc_parts))
    return s


def _protected(d: dict) -> bool:
    name = d.get("name")
    if name in _ESSENTIAL:
        return True
    # Server-side tools (web_search/web_fetch/code_execution on Anthropic)
    # carry a `type`; their schemas live server-side and cost ~nothing here,
    # and web access is a capability the prompt reasons about, not a pick.
    if d.get("type"):
        return True
    return False


def select(task: str, defs: list[dict]) -> list[dict]:
    """Return the loadout trimmed for `task` — a subset of `defs`, original
    order preserved. No-op when disabled, or when the droppable tail already
    fits under the cap."""
    if not enabled() or not task:
        return defs
    droppable = [d for d in defs if not _protected(d)]
    cap = max_kept()
    if len(droppable) <= cap:
        return defs
    task_tokens = _tokens(task)
    ranked = sorted(droppable, key=lambda d: score(task_tokens, d),
                    reverse=True)          # sort is stable → ties keep order
    kept = {id(d) for d in ranked[:cap]}
    return [d for d in defs if _protected(d) or id(d) in kept]
