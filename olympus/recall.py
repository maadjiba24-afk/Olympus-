"""Memory write + read policy: extract durable facts, gate them, retrieve them.

Write path (background, cheap model): a turn → candidate typed memories with
confidence + sensitivity → dedup / conflict / gate → commit, reinforce, or hold
for the user's approval. Nothing sensitive is ever auto-committed, and nothing
is written from raw text without passing the extractor — so memory stays honest.

Read path (pure Python, no model call on the hot path): lexically match the
active memories to the turn, rank by importance × decayed-confidence × recency ×
overlap, fill a token budget, and drop anything below the relevance floor so
irrelevant memory never pollutes the prompt.
"""

from __future__ import annotations

import re

from . import backend, config, embed, relgraph, security, usermem

_WORD = re.compile(r"[a-z0-9]+")
# Common words carry no relevance signal and cause false lexical matches.
_STOP = frozenset((
    "the", "and", "for", "are", "was", "but", "not", "you", "your", "his",
    "her", "its", "our", "their", "this", "that", "these", "those", "with",
    "what", "how", "why", "who", "when", "where", "which", "from", "into",
    "about", "have", "has", "had", "will", "would", "can", "could", "should",
    "they", "them", "she", "him", "use", "using", "any", "all", "out", "now",
    "get", "got", "let", "please", "tell", "give", "make", "want", "need",
))


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(str(text).lower())
            if len(w) > 2 and w not in _STOP}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)         # Jaccard


# --- write path ----------------------------------------------------------

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(usermem.TYPES)},
                    "key": {"type": ["string", "null"]},
                    "content": {"type": "string"},
                    "confidence": {"type": "number"},
                    "importance": {"type": "number"},
                    "sensitivity": {"type": "string",
                                    "enum": ["normal", "high"]},
                },
                "required": ["type", "content", "confidence", "sensitivity"],
                "additionalProperties": False,
            },
        },
        "relationships": {
            "type": "array",
            "description": "Connections between people/companies the user "
                           "mentioned, as subject-relation-object triples.",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "relation": {"type": "string",
                                 "description": "e.g. works_at, cofounder_of, "
                                 "competitor_of, reports_to, client_of"},
                    "object": {"type": "string"},
                    "subject_kind": {"type": "string",
                                     "enum": ["person", "company", "other"]},
                    "object_kind": {"type": "string",
                                    "enum": ["person", "company", "other"]},
                    "confidence": {"type": "number"},
                },
                "required": ["subject", "relation", "object"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["memories"],
    "additionalProperties": False,
}

_EXTRACT_SYSTEM = (
    "You extract durable, reusable facts about THIS user that are worth "
    "remembering across future sessions: identity, stated preferences, "
    "decisions, projects, recurring tasks, important people/companies, and how "
    "they like recurring tasks done. Return ONLY such facts as structured "
    "memories. Also capture connections between people and organizations the "
    "user mentions as subject-relation-object 'relationships' (e.g. Sarah "
    "cofounder_of Acme).\n\n"
    "Rules:\n"
    "- Do NOT store secrets, passwords, one-off trivia, or your own speculation.\n"
    "- If nothing is worth remembering, return an empty list.\n"
    "- Score confidence honestly (1.0 = the user stated it plainly; lower if "
    "inferred).\n"
    "- Mark anything sensitive (health, finances, legal, personal identifiers) "
    "as sensitivity='high' — it will require the user's approval before it is "
    "saved.\n"
    "- Phrase each memory as a short, self-contained statement."
)


def _dedupe_or_conflict(user: str, cand: dict):
    """Returns ('duplicate', mem) | ('conflict', mem) | ('new', None)."""
    cand_tokens = _tokens(cand["content"])
    for m in usermem.active_memories(user):
        if m["type"] != cand["type"]:
            continue
        if cand.get("key") and m.get("key") == cand["key"]:
            # same slot, different content → conflict (unless identical text)
            if _tokens(m["content"]) == cand_tokens:
                return "duplicate", m
            return "conflict", m
        if _overlap(_tokens(m["content"]), cand_tokens) >= 0.8:
            return "duplicate", m
    return "new", None


def _maybe_embed(user: str, mem: dict) -> None:
    """Attach a semantic embedding if an embeddings endpoint is configured.
    Best-effort and only relevant to hybrid retrieval; no-op otherwise."""
    if not embed.available():
        return
    vec = embed.embed_one(mem["content"])
    if vec:
        usermem.set_embedding(user, mem["id"], vec)


def _gate(user: str, cand: dict, event_id: str) -> str:
    """Apply the write policy to one candidate. Returns the action taken."""
    floor = config.MEMORY_CONFIDENCE_FLOOR
    if float(cand.get("confidence", 0)) < floor:
        return "discarded_low_confidence"

    verdict, existing = _dedupe_or_conflict(user, cand)
    if verdict == "duplicate":
        usermem.reinforce(user, existing["id"])
        return "reinforced"

    # sensitive memories are never auto-committed — they go to the user.
    if cand.get("sensitivity") == "high":
        usermem.add_candidate(user, {**cand, "reason": "sensitive",
                                     "provenance": [event_id]})
        return "held_sensitive"

    if verdict == "conflict":
        # only auto-supersede when clearly confident; else ask the user.
        if float(cand["confidence"]) >= max(floor, existing["confidence"]):
            new = usermem.add_memory(
                user, type=cand["type"], content=cand["content"],
                confidence=cand["confidence"], key=cand.get("key"),
                importance=cand.get("importance", 0.5),
                provenance=[event_id])
            usermem.supersede(user, existing["id"], new)
            _maybe_embed(user, new)
            return "superseded"
        usermem.add_candidate(user, {**cand, "reason": "conflict",
                                     "conflicts_with": existing["id"],
                                     "provenance": [event_id]})
        return "held_conflict"

    mem = usermem.add_memory(user, type=cand["type"], content=cand["content"],
                             confidence=cand["confidence"], key=cand.get("key"),
                             importance=cand.get("importance", 0.5),
                             provenance=[event_id])
    _maybe_embed(user, mem)
    return "committed"


def extract(user: str, user_msg: str, reply: str,
            settings: config.Settings) -> dict:
    """Run the extractor over one turn and apply the write policy. Best-effort:
    never raises into the caller. Returns a small summary of actions taken."""
    summary: dict[str, int] = {}
    try:
        if not config.MEMORY_ENABLED:
            return summary
        if len((user_msg or "").strip()) < config.MEMORY_MIN_CHARS:
            return summary            # trivial turn — not worth a model call
        from . import usage
        try:
            usage.check_budget()       # don't extract over the spend cap
        except usage.BudgetExceeded:
            return summary

        convo = (f"User said:\n{user_msg[:2000]}\n\n"
                 f"Assistant replied:\n{reply[:1500]}")
        try:
            out = backend.complete_json(
                settings, _EXTRACT_SYSTEM,
                [{"role": "user", "content": convo}], EXTRACT_SCHEMA,
                effort="low")
        except Exception:
            return summary

        eid = usermem.record_event(
            user, "turn", {"user": user_msg[:500]}, source="user")
        for cand in (out.get("memories") or []):
            content = security.sanitize_for_memory(str(cand.get("content", "")))
            if not content.strip():
                continue
            cand["content"] = content
            action = _gate(user, cand, eid)
            summary[action] = summary.get(action, 0) + 1
        rels = relgraph.ingest(user, out.get("relationships") or [])
        if rels:
            summary["relationships"] = rels
    except Exception:
        pass                           # memory must never break a conversation
    return summary


# --- read path -----------------------------------------------------------

def _semantic_hits(user: str, query: str, already: set[str]) -> list[tuple]:
    """Cosine-ranked memories with embeddings, excluding ids already found
    lexically. Best-effort: empty if embeddings are unavailable or fail."""
    qvec = embed.embed_one(query)
    if not qvec:
        return []
    out = []
    for m in usermem.active_memories(user):
        if m["id"] in already or not m.get("embedding"):
            continue
        eff = usermem.effective_confidence(m)
        if eff < config.MEMORY_RETRIEVAL_FLOOR_CONF:
            continue
        c = embed.cosine(qvec, m["embedding"])
        if c >= config.MEMORY_SEMANTIC_THRESHOLD:
            out.append((m.get("importance", 0.5) * eff * c, m))
    return out


def retrieve(user: str, query: str,
             budget_tokens: int | None = None) -> list[dict]:
    """Rank active memories against the query and return those that clear the
    relevance floor, up to the token budget. Lexical first; when it comes back
    thin AND an embeddings endpoint is configured, add a semantic fallback —
    so the hot path stays free in the common case and only pays for embeddings
    when keyword matching misses a paraphrase."""
    budget = budget_tokens or config.MEMORY_RETRIEVAL_BUDGET_TOKENS
    q = _tokens(query)
    if not q:
        return []
    scored = []
    for m in usermem.active_memories(user):
        ov = _overlap(q, _tokens(m["content"]))
        if ov <= 0:
            continue                   # relevance floor: must actually relate
        eff = usermem.effective_confidence(m)
        if eff < config.MEMORY_RETRIEVAL_FLOOR_CONF:
            continue
        score = m.get("importance", 0.5) * eff * (0.3 + ov)
        scored.append((score, m))

    if (len(scored) < config.MEMORY_SEMANTIC_FALLBACK_MIN and embed.available()):
        found = {m["id"] for _, m in scored}
        scored.extend(_semantic_hits(user, query, found))

    scored.sort(key=lambda s: s[0], reverse=True)

    chosen, used = [], 0
    for _, m in scored:
        cost = len(m["content"]) // 4 + 4
        if used + cost > budget:
            break
        chosen.append(m)
        used += cost
    return chosen


def context_block(user: str, query: str) -> str:
    """A compact, injectable block of the memories relevant to this turn.
    Returns '' when nothing clears the floor, so it costs no tokens."""
    mems = retrieve(user, query)
    if not mems:
        return ""
    for m in mems:
        usermem.touch(user, m["id"])   # recency, not confidence
    lines = [f"- ({m['type']}) {m['content']}" for m in mems]
    return ("\n\n## Relevant things you remember about this user (verify if "
            "acting on them):\n" + "\n".join(lines))
