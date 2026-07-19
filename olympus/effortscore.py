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

LONG_PROMPT_CHARS = 2000     # a brief this long carries real breadth
MANY_TOOLS = 12              # a loadout this wide implies a complex task


def _idx(effort: str) -> int:
    return TIERS.index(effort) if effort in TIERS else 0


def at_least(minimum: str, effort: str) -> str:
    """The higher of two tiers — the floor semantics (unknown → 'low')."""
    return TIERS[max(_idx(minimum), _idx(effort))]


def score(risk_class: str = actions.TRIVIAL, prompt_chars: int = 0,
          tool_count: int = 0, retry_index: int = 0,
          needs_verification: bool = False) -> str:
    """Effort tier for one run. Monotonic: a hard signal can only raise."""
    if retry_index >= 1:
        return "high"            # the cheap attempt already failed
    if risk_class in (actions.IRREVERSIBLE, actions.FINANCIAL_LEGAL):
        return "high"            # consequence demands depth, not speed
    bumps = 0
    if needs_verification:
        bumps += 1
    if prompt_chars > LONG_PROMPT_CHARS:
        bumps += 1
    if tool_count > MANY_TOOLS:
        bumps += 1
    return TIERS[min(bumps, len(TIERS) - 1)]
