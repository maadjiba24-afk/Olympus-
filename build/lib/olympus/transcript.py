"""Optional in-run compaction of a single agent's tool transcript.

The chat layer already folds old *conversation* turns into a state block
(orchestrator._compress_history). This is the deeper analog: within ONE
specialist run, a tool-heavy loop (many/large `web_fetch`/`read_file`/codegraph
results) accumulates all those results in `messages` and re-sends them every
iteration. When enabled, this shrinks the contents of the OLDER `tool_result`
blocks in place — keeping the most recent ones verbatim — so the run stays
bounded. Off by default (`OLYMPUS_INRUN_COMPACT`).

Replay-safety (the whole reason this is careful):
  * It NEVER removes or reorders messages and never separates a `tool_use` from
    its `tool_result` — it only rewrites the *content string* of old
    tool_result blocks. So the API's pairing/alternation invariants hold.
  * It is a PURE function of the message stream. Under replay the tool results
    are frozen (identical to record), so this produces the identical messages,
    hence identical downstream request hashes — replay stays byte-identical.
  * The optional LLM summarizer is routed through `backend.complete_text` →
    `llm.complete`, which is frozen/replayed by request hash like every other
    model call. So even "summarize" mode reproduces exactly under replay.

The compaction settings are recorded into the run's `tr.meta`
(`inrun_compact`/`inrun_budget`/`inrun_keep_recent`) and restored by
`orchestrator.replay_run` — exactly like `contracts_enabled`/`egress_guard` — so
replay reproduces the recorded message stream automatically, with no manual
config matching. The settings are read LIVE from the env (via `config.inrun_*`)
so that restore takes effect. Default is off; existing runs are unaffected.
"""

from __future__ import annotations

from typing import Any, Callable

from . import config


def enabled() -> bool:
    return config.inrun_compact() in ("1", "true", "yes", "on", "elide",
                                      "summarize")


def mode() -> str:
    return "summarize" if config.inrun_compact() == "summarize" else "elide"


_HEAD = 400   # chars of an elided result kept as a breadcrumb


def _text_len(messages: list[dict[str, Any]]) -> int:
    """Cheap size estimate over message + tool_result content (chars)."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    total += len(str(b.get("content", "")))
    return total


def _elide(content: str) -> str:
    if len(content) <= _HEAD:
        return content
    return (content[:_HEAD]
            + f"\n…[{len(content) - _HEAD} chars elided in-run to save context]")


def _tool_result_positions(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for i, m in enumerate(messages):
        c = m.get("content")
        if isinstance(c, list):
            for j, b in enumerate(c):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out.append((i, j))
    return out


def compact(messages: list[dict[str, Any]], *, budget: int,
            keep_recent: int = 2,
            summarizer: Callable[[str], str] | None = None
            ) -> list[dict[str, Any]]:
    """Return `messages` with old tool_result contents shrunk, IFF the transcript
    exceeds `budget`. The most recent `keep_recent` tool results are left
    verbatim. Pure: does not mutate the input. `summarizer(content)->str`, when
    given, produces the replacement (falling back to deterministic elision on
    any error); without it, deterministic elision is used."""
    if _text_len(messages) <= budget:
        return messages
    positions = _tool_result_positions(messages)
    to_shrink = positions[:-keep_recent] if keep_recent > 0 else positions
    if not to_shrink:
        return messages

    # Copy only what we touch (shallow elsewhere) so the input is unchanged.
    new = list(messages)
    touched_msgs = {i for i, _ in to_shrink}
    for i in touched_msgs:
        new[i] = dict(new[i])
        new[i]["content"] = [dict(b) if isinstance(b, dict) else b
                             for b in new[i]["content"]]
    for i, j in to_shrink:
        block = new[i]["content"][j]
        content = str(block.get("content", ""))
        if len(content) <= _HEAD:
            continue                       # already small — nothing to gain
        if summarizer is not None:
            try:
                block["content"] = summarizer(content)
            except Exception:
                block["content"] = _elide(content)
        else:
            block["content"] = _elide(content)
    return new


def _summarizer(settings) -> Callable[[str], str]:
    """A model-based summarizer routed through the frozen `complete_text`, so it
    replays deterministically. Used only in 'summarize' mode."""
    from . import backend
    s = settings or config.Settings.from_env()

    def summarize(content: str) -> str:
        text = backend.complete_text(
            s,
            "Summarize this tool result compactly for an agent still working "
            "the task: keep facts, figures, names, URLs, and anything it may "
            "still need; drop boilerplate. A few lines.",
            [{"role": "user", "content": content[:8000]}], effort="low")
        return f"[earlier tool result, summarized in-run]\n{text}"

    return summarize


def maybe_compact(messages: list[dict[str, Any]], settings=None
                  ) -> list[dict[str, Any]]:
    """The one entry the agent loop calls. No-op unless enabled."""
    if not enabled():
        return messages
    summ = _summarizer(settings) if mode() == "summarize" else None
    return compact(messages, budget=config.inrun_budget(),
                   keep_recent=config.inrun_keep_recent(), summarizer=summ)
