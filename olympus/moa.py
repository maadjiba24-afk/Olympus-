"""Mixture-of-Agents — an ensemble usable anywhere a single model is.

Set `OLYMPUS_PROVIDER=moa` (or put a member with provider "moa" in the pool)
and every completion fans out to the operator's configured pool members
(OLYMPUS_MODELS) as *reference* models, then a strongest-member *aggregator*
synthesizes their answers into one. The idea (Hermes v0.18, and the MoA
paper before it): independent drafts from diverse models surface facts and
framings any single model misses; the aggregator keeps what survives
comparison.

Scope: text and JSON completions ensemble; tool-using agent runs route to
the aggregator member directly (a tool loop can't be meaningfully averaged
across models — each would issue different calls).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import config

MAX_REFERENCE = 4          # fan-out ceiling: cost grows linearly per member

_AGGREGATE_NOTE = (
    "You are the aggregator of a mixture-of-agents ensemble. Independent "
    "reference models each answered the same request; their drafts follow, "
    "labelled. Synthesize ONE best answer: keep claims that multiple drafts "
    "agree on or that come with clear support, drop claims that conflict "
    "without support, and preserve any caveats. Do not mention the ensemble "
    "or the drafts — just answer the request as well as it can be answered."
)


def _members() -> list[config.Settings]:
    """Reference members: every usable non-moa pool member (bounded)."""
    pool = config.ModelPool.from_env()
    refs = [m for m in pool.members if m.provider != "moa"][:MAX_REFERENCE]
    if not refs:
        raise RuntimeError(
            "moa provider needs at least one non-moa member in the pool — "
            "configure OLYMPUS_MODELS (or a primary provider) first.")
    return refs


def _aggregator(refs: list[config.Settings]) -> config.Settings:
    """The strongest reference member synthesizes."""
    return max(refs, key=lambda m: config.capability_score(m.model, "reasoning"))


def _drafts(refs: list[config.Settings], system: str,
            messages: list[dict[str, Any]], effort: str) -> list[str]:
    from . import backend

    def one(member: config.Settings) -> str | None:
        try:
            return backend.complete_text(member, system, messages,
                                         effort=effort)
        except Exception:
            return None       # a dead reference shrinks the ensemble, only

    with ThreadPoolExecutor(max_workers=len(refs)) as ex:
        results = list(ex.map(one, refs))
    drafts = [r for r in results if r]
    if not drafts:
        raise RuntimeError("every moa reference model failed")
    return drafts


def _aggregate_messages(messages: list[dict[str, Any]],
                        refs: list[config.Settings],
                        drafts: list[str]) -> list[dict[str, Any]]:
    labelled = "\n\n".join(
        f"### Draft {i + 1} ({m.provider}/{m.model or 'default'})\n{d}"
        for i, (m, d) in enumerate(zip(refs, drafts)))
    tail = {"role": "user", "content":
            f"Reference drafts for the request above:\n\n{labelled}\n\n"
            "Now give the single best final answer."}
    return list(messages) + [tail]


def complete_text(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], effort: str = "high") -> str:
    from . import backend
    refs = _members()
    if len(refs) == 1:                    # ensemble of one = just that model
        return backend.complete_text(refs[0], system, messages, effort=effort)
    drafts = _drafts(refs, system, messages, effort)
    return backend.complete_text(
        _aggregator(refs), system + "\n\n" + _AGGREGATE_NOTE,
        _aggregate_messages(messages, refs, drafts), effort=effort)


def complete_json(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], schema: dict[str, Any],
                  effort: str = "high") -> dict[str, Any]:
    from . import backend
    refs = _members()
    if len(refs) == 1:
        return backend.complete_json(refs[0], system, messages, schema,
                                     effort=effort)
    drafts = _drafts(refs, system, messages, effort)
    return backend.complete_json(
        _aggregator(refs), system + "\n\n" + _AGGREGATE_NOTE,
        _aggregate_messages(messages, refs, drafts), schema, effort=effort)


def agent_settings() -> config.Settings:
    """Tool-using runs execute on the aggregator member (see module doc)."""
    refs = _members()
    return _aggregator(refs)
