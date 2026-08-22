"""E-mem — episodic memory reconstruction (arXiv 2601.21714).

Where `recall` retrieves typed memories and compaction folds turns into a lossy
summary, E-mem does something different and complementary: on demand, it
RECONSTRUCTS a past episode by gathering the raw fragments related to a query —
event-log entries, typed memories (with their provenance), and conversation
snippets from the FTS5 index — and assembling the most relevant ones into a
chronologically-ordered, provenance-tagged reconstruction. Nothing is
summarized or rewritten: fragments are selected and ordered, never compressed,
so the reconstruction is faithful and attributable rather than a de-contextualized
digest.

This is added ALONGSIDE the existing memory paths, not in place of them: it is
opt-in (`OLYMPUS_EMEM`, off by default) and reads the same substrate everything
else does. The reconstruction core is pure (scoring + chronological assembly
over injected fragments) so it is deterministic and testable without I/O; the
gatherer is a thin, best-effort layer over `usermem` + `search`.

Security: a reconstruction may include conversation snippets that originated from
tools/web, so the rendered episode leaves this module only through
`security.wrap_untrusted(source="episodic-memory")` — recollection re-entering a
prompt is DATA, not instructions.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset((
    "the", "and", "for", "are", "with", "that", "this", "from", "into", "what",
    "when", "who", "how", "any", "all", "can", "you", "your", "was", "were",
    "did", "does", "about", "have", "has", "had", "our", "their", "they",
))

_MAX_FRAGMENTS = 12
_BUDGET_CHARS = 4000
_HALF_LIFE_S = 30 * 86400        # recency half-life for scoring (30 days)


def enabled() -> bool:
    """Whether episodic reconstruction is offered (default OFF; the existing
    retrieval + compaction paths are unchanged)."""
    return os.environ.get("OLYMPUS_EMEM", "").strip().lower() in (
        "1", "on", "true", "yes")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(str(text).lower())
                     if len(w) > 2 and w not in _STOP)


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class Fragment:
    """One attributable piece of the past — kept VERBATIM (non-destructive)."""
    source: str          # "event" | "memory" | "conversation"
    ref: str             # id / provenance handle (attribution)
    ts: float            # for chronological assembly
    text: str
    trust: str = "user"  # provenance/trust label, preserved


@dataclass
class Episode:
    query: str
    fragments: tuple[Fragment, ...]

    def provenance(self) -> list[str]:
        return [f"{f.source}:{f.ref}" for f in self.fragments]

    def has_external(self) -> bool:
        return any(f.source == "conversation" for f in self.fragments)

    def render(self) -> str:
        """A chronological, source-tagged reconstruction. Wrapped in the
        untrusted envelope (recollection is DATA). '' when empty."""
        if not self.fragments:
            return ""
        lines = [f"[{f.source}] {f.text}" for f in self.fragments]
        body = ("## Reconstructed episode for: " + self.query + "\n"
                + "\n".join(lines))
        from . import security
        return security.wrap_untrusted(body, source="episodic-memory")


# --- pure reconstruction core ---------------------------------------------

def _score(q: frozenset[str], text: str, ts: float, now: float) -> float:
    ov = _overlap(q, _tokens(text))
    if ov <= 0:
        return 0.0
    age = max(0.0, now - float(ts or 0))
    recency = 0.5 ** (age / _HALF_LIFE_S)
    return ov * (0.5 + 0.5 * recency)     # relevance dominates; recency breaks ties


def reconstruct(query: str, fragments, *, now: float,
                max_fragments: int = _MAX_FRAGMENTS,
                budget_chars: int = _BUDGET_CHARS) -> Episode:
    """Select the fragments relevant to `query`, then assemble them in
    CHRONOLOGICAL order (episodic reconstruction). Pure + deterministic given
    `now`. Non-destructive: fragment text is never rewritten. `now` is injected
    (no clock) so replay/tests are exact."""
    q = _tokens(query)
    if not q:
        return Episode(query=str(query), fragments=())
    cap = max(1, min(int(max_fragments), _MAX_FRAGMENTS))
    scored = []
    for f in fragments or []:
        if not isinstance(f, Fragment) or not f.text:
            continue
        s = _score(q, f.text, f.ts, now)
        if s > 0.0:
            scored.append((s, f))
    # rank by relevance; ties broken deterministically by (ts, ref)
    scored.sort(key=lambda t: (-t[0], -float(t[1].ts or 0), t[1].ref))
    chosen, used = [], 0
    for _, f in scored[:cap]:
        cost = len(f.text)
        if used + cost > budget_chars and chosen:
            break
        chosen.append(f)
        used += cost
    # reconstruct the EPISODE: order the selected fragments in time
    chosen.sort(key=lambda f: (float(f.ts or 0), f.ref))
    return Episode(query=str(query), fragments=tuple(chosen))


# --- gatherer (thin, best-effort I/O over the existing substrate) ---------

def gather(user: str, query: str, *, limit: int = 40) -> list[Fragment]:
    """Collect candidate fragments from the event log, typed memories, and the
    conversation FTS5 index. Best-effort: any source that errors is skipped, and
    the whole thing never raises into the caller."""
    frags: list[Fragment] = []
    from . import usermem
    # typed memories (carry provenance + trust)
    try:
        for m in usermem.active_memories(user)[:limit]:
            frags.append(Fragment(
                source="memory", ref=str(m.get("id", "")),
                ts=float(m.get("last_used_at") or m.get("created_at") or 0),
                text=str(m.get("content", "")),
                trust=str(m.get("sensitivity", "normal"))))
    except Exception as err:
        _capture(err)
    # raw event log (the provenance source of truth)
    try:
        for e in usermem.events(user)[-limit:]:
            payload = e.get("payload") or {}
            text = payload.get("user") or payload.get("text") or ""
            if text:
                frags.append(Fragment(
                    source="event", ref=str(e.get("id", "")),
                    ts=float(e.get("ts") or 0), text=str(text),
                    trust=str(e.get("source", "user"))))
    except Exception as err:
        _capture(err)
    # conversation snippets from the FTS5 index (may include external content)
    try:
        from . import search
        for h in search.search(query, limit=limit, owner=user):
            content = getattr(h, "content", "") or ""
            if content:
                frags.append(Fragment(
                    source="conversation",
                    ref=str(getattr(h, "conversation", "")),
                    ts=0.0, text=str(content), trust="external"))
    except Exception as err:
        _capture(err)
    return frags


def _capture(err: Exception) -> None:
    from . import errors
    errors.capture("emem.gather", err)


def _tuned_max_fragments() -> int:
    """Self-tuned fragment cap (narrowed within [4,12] by the evolve reviewer
    when reconstruction degrades). Falls back to the hard default on read error."""
    try:
        from . import evolve
        return int(evolve.current("emem", "max_fragments"))
    except (KeyError, ValueError, TypeError, ImportError, OSError):
        return _MAX_FRAGMENTS


def context_block(user: str, query: str, *, now: float | None = None) -> str:
    """The alongside variant of `recall.context_block`: reconstruct an episode
    for this turn and return it as an enveloped block. '' when disabled, when the
    turn shares no salient term with the past, or on any error."""
    if not enabled():
        return ""
    import time
    now = time.time() if now is None else now
    try:
        episode = reconstruct(query, gather(user, query), now=now,
                              max_fragments=_tuned_max_fragments())
    except Exception as err:
        _capture(err)
        return ""
    try:
        from . import evolve
        evolve.log_event("emem", "reconstruct",
                         {"fragments": len(episode.fragments),
                          "external": episode.has_external()})
    except Exception:
        pass
    return episode.render()
