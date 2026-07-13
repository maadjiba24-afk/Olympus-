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

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

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
    from . import firstrun
    if not firstrun.configured():
        raise RuntimeError(
            "no model is configured — run `olympus setup` or set "
            "ANTHROPIC_API_KEY / OLYMPUS_API_KEY before using the ensemble.")
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


def _save_trace(question: str, refs: list[config.Settings],
                drafts: list[str], final: str) -> None:
    """Optional full-turn trace (OLYMPUS_MOA_SAVE_TRACES=1): every reference
    draft plus the aggregate, saved to memory/reports for later comparison of
    what each member contributed."""
    if os.environ.get("OLYMPUS_MOA_SAVE_TRACES", "").lower() not in (
            "1", "true", "yes", "on"):
        return
    try:
        from . import memory
        body = [f"## Question\n{question[:2000]}", ""]
        for m, d in zip(refs, drafts):
            body += [f"## Draft — {m.provider}/{m.model or 'default'}",
                     d[:4000], ""]
        body += ["## Aggregate", final[:4000]]
        memory.save("reports", "moa trace", "\n".join(body))
    except Exception:
        pass                               # tracing must never break a reply


def complete_text(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], effort: str = "high") -> str:
    from . import backend
    refs = _members()
    if len(refs) == 1:                    # ensemble of one = just that model
        return backend.complete_text(refs[0], system, messages, effort=effort)
    drafts = _drafts(refs, system, messages, effort)
    final = backend.complete_text(
        _aggregator(refs), system + "\n\n" + _AGGREGATE_NOTE,
        _aggregate_messages(messages, refs, drafts), effort=effort)
    question = str(messages[-1].get("content", "")) if messages else ""
    _save_trace(question, refs, drafts, final)
    return final


def one_shot(prompt: str, *, show_drafts: bool = True,
             runner: Callable[..., str] | None = None) -> str:
    """`/moa <prompt>`: run ONE prompt through the ensemble regardless of the
    configured provider (the session's model is untouched). With
    `show_drafts`, each reference model's answer appears as a labelled block
    before the aggregate — the Hermes-style visible ensemble."""
    from . import backend
    runner = runner or backend.complete_text
    try:
        refs = _members()
    except RuntimeError as err:
        return str(err)
    system = "Answer the user's question well. Be concrete."
    messages = [{"role": "user", "content": prompt}]
    if len(refs) == 1:
        only = runner(refs[0], system, messages)
        return (f"(ensemble of one — {refs[0].provider}/{refs[0].model})\n\n"
                + only)
    labelled_drafts, drafts = [], []
    with ThreadPoolExecutor(max_workers=len(refs)) as ex:
        results = list(ex.map(
            lambda m: _safe(runner, m, system, messages), refs))
    for m, d in zip(refs, results):
        if d is None:
            labelled_drafts.append(
                f"── {m.provider}/{m.model or 'default'} ── (failed)")
            continue
        drafts.append(d)
        labelled_drafts.append(
            f"── {m.provider}/{m.model or 'default'} ──\n{d}")
    live = [m for m, d in zip(refs, results) if d is not None]
    if not drafts:
        return "Every moa reference model failed."
    final = runner(_aggregator(live), system + "\n\n" + _AGGREGATE_NOTE,
                   _aggregate_messages(messages, live, drafts))
    _save_trace(prompt, live, drafts, final)
    out = ""
    if show_drafts:
        out = "\n\n".join(labelled_drafts) + "\n\n══ aggregate ══\n"
    return out + final


def _safe(runner, member, system, messages) -> str | None:
    try:
        return runner(member, system, messages)
    except Exception:
        return None


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
