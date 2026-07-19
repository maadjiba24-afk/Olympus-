"""Deterministic difficulty pre-scorer (ADR 0005 decision c).

Maps observable, deterministic signals — action risk class, prompt length,
tool count, retry index, the router's needs_verification flag — to a
reasoning-effort tier, with ZERO model calls and zero I/O. Before this,
every `effort=` in the pipeline was a static literal tied to the call site,
so "thinks harder on hard calls" only existed as reactive model swapping on
multi-model pools.

Semantics:
- The per-specialist `effort` field is a FLOOR — `at_least` may raise a run
  above it, never lower it below.
- A rework (retry_index >= 1) is itself the difficulty signal: the pipeline
  already observed the cheap attempt fail, so the retry runs at the top
  tier. On a single-model pool this IS the escalation path — same model,
  more compute — where the teacher path has no stronger member to swap in.
- The thresholds are plain constants, deliberately NOT evolve-tunable: the
  scorer's behavior must stay deterministic and auditable.
"""

from __future__ import annotations

from . import actions

TIERS = ("low", "medium", "high")

LONG_PROMPT_CHARS = 2000        # a brief this long carries real breadth
VERY_LONG_PROMPT_CHARS = 8000   # ... and this long can demand depth alone
# Counts the specialist's EXTRA tools only — the 7 BASE tools are shared by
# everyone, so including them made the signal fire for most of the roster
# and structurally defeated the cost dial (adversarial review, ADR 0005).
MANY_TOOLS = 10


def _idx(effort) -> int:
    return TIERS.index(effort) if effort in TIERS else 0


def _nat(value) -> int:
    """Coerce anything to a non-negative int — the scorer is a TOTAL
    function (ADR 0005 amendment 7): garbage in (None, strings, negatives,
    NaN) degrades to 0, never an exception."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if n > 0 else 0


def at_least(minimum, effort) -> str:
    """The higher of two tiers — the floor semantics (unknown → 'low')."""
    return TIERS[max(_idx(minimum), _idx(effort))]


def score(risk_class=actions.TRIVIAL, prompt_chars=0,
          tool_count=0, retry_index=0,
          needs_verification=False) -> str:
    """Effort tier for one run. Monotonic (a hard signal can only raise),
    deterministic, and TOTAL — any input degrades to its harmless default
    rather than raising."""
    if _nat(retry_index) >= 1:
        return "high"            # the cheap attempt already failed
    if risk_class in (actions.IRREVERSIBLE, actions.FINANCIAL_LEGAL):
        return "high"            # consequence demands depth, not speed
    bumps = 0
    if bool(needs_verification):
        bumps += 1
    prompt_chars = _nat(prompt_chars)
    if prompt_chars > LONG_PROMPT_CHARS:
        bumps += 1
    if prompt_chars > VERY_LONG_PROMPT_CHARS:
        bumps += 1                # a second bump: length alone can reach high
    if _nat(tool_count) > MANY_TOOLS:
        bumps += 1
    return TIERS[min(bumps, len(TIERS) - 1)]
