"""Personal-document RAG — ground the council in the user's own documents.

Retrieves the most relevant chunks of the user's workspace documents (see
`documents.py`) for the current request, so answers can be grounded in what the
user has written/saved. Lexical retrieval always works; semantic retrieval adds
on top when an embeddings endpoint is configured (`OLYMPUS_EMBED_MODEL`) —
best-effort, degrading cleanly to lexical when embeddings are unavailable.

The chunk index (chunks + their embeddings) is cached per user on disk and
re-embeds only documents whose file changed, so retrieval stays cheap turn to
turn. Document content is the user's OWN first-party material, so — like
`recall` (user memory) — it is injected as trusted context, not wrapped as
untrusted (a document the user wrote is not an external attacker surface).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import config, documents, embed, memory

_CHUNK_CHARS = 1200
_CHUNK_OVERLAP = 200
_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "you",
    "are", "was", "were", "has", "have", "had", "not", "but", "all", "any",
    "can", "use", "how", "what", "when", "where", "which", "will", "would",
})
_TOKEN = re.compile(r"[a-z0-9]+")
_FLOOR = 0.05                     # minimum relevance to inject a chunk


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower())
            if len(t) >= 3 and t not in _STOP}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def chunk(text: str, size: int = _CHUNK_CHARS,
          overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split a document into overlapping chunks on paragraph-ish boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = start + size
        # prefer to break on a blank line or newline near the window edge
        cut = text.rfind("\n\n", start, end)
        if cut <= start:
            cut = text.rfind("\n", start, end)
        if cut <= start:
            cut = end
        out.append(text[start:cut].strip())
        if cut >= len(text):
            break
        start = max(cut - overlap, start + 1)
    return [c for c in out if c]


def _index_path(user: str) -> Path:
    safe = memory.safe_id(user)
    base = (config.MEMORY_DIR / "users" / safe) if safe != "shared" \
        else config.MEMORY_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / "doc_index.json"


def _load_index(user: str) -> dict:
    p = _index_path(user)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def build_index(user: str) -> dict:
    """(Re)build the per-document chunk index, re-embedding only documents whose
    file changed since last time. Returns {slug: {mtime, name, chunks,
    embeddings|None}}. Best-effort — never raises."""
    cache = _load_index(user)
    fresh: dict = {}
    for meta in documents.listing(user):
        slug = meta["slug"]
        path = documents.path_for(user, meta["name"])
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cached = cache.get(slug)
        if cached and cached.get("mtime") == mtime:
            fresh[slug] = cached                 # unchanged — reuse
            continue
        body = documents.read(user, meta["name"]) or ""
        chunks = chunk(body)
        embeddings = None
        if embed.available() and chunks:
            embeddings = embed.embed(chunks)     # None on failure → lexical
        fresh[slug] = {"mtime": mtime, "name": meta["name"],
                       "chunks": chunks, "embeddings": embeddings}
    try:
        _index_path(user).write_text(json.dumps(fresh), encoding="utf-8")
    except OSError:
        pass
    return fresh


def retrieve(user: str, query: str, k: int = 4) -> list[dict]:
    """Top-k relevant document chunks: [{document, text, score}]. Semantic when
    embeddings are configured, lexical otherwise (and lexical always contributes
    a floor so an exact keyword hit is never lost)."""
    index = build_index(user)
    if not index:
        return []
    qtokens = _tokens(query)
    qvec = embed.embed_one(query) if embed.available() else None
    scored = []
    for slug, entry in index.items():
        chunks = entry.get("chunks") or []
        embs = entry.get("embeddings")
        for i, ch in enumerate(chunks):
            lex = _overlap(qtokens, _tokens(ch))
            vec = 0.0
            if qvec and embs and i < len(embs) and embs[i]:
                vec = max(0.0, embed.cosine(qvec, embs[i]))
            score = max(vec, lex) if qvec else lex
            if score >= _FLOOR:
                scored.append({"document": entry.get("name", slug),
                               "text": ch, "score": round(score, 4)})
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:k]


def context_block(user: str, query: str, k: int = 4,
                  char_budget: int = 3000) -> str:
    """Grounding block for the pipeline: relevant excerpts from the user's own
    documents, or '' when there are none. First-party (trusted) content."""
    if not config.MEMORY_ENABLED:
        return ""
    try:
        hits = retrieve(user, query, k)
    except Exception:
        return ""
    if not hits:
        return ""
    lines, used = [], 0
    for h in hits:
        excerpt = h["text"].strip()
        if used + len(excerpt) > char_budget:
            excerpt = excerpt[:max(0, char_budget - used)]
        if not excerpt:
            break
        lines.append(f"[from your document: {h['document']}]\n{excerpt}")
        used += len(excerpt)
        if used >= char_budget:
            break
    if not lines:
        return ""
    return ("\n\n## From your documents\n"
            "Relevant excerpts from documents you've saved. Treat them as "
            "authoritative context for this request and cite the document name "
            "when you use one.\n\n" + "\n\n---\n\n".join(lines))


def render_search(user: str, query: str, k: int = 5) -> str:
    """Plain-text results for the search_documents tool."""
    hits = retrieve(user, query, k)
    if not hits:
        return "No relevant documents found."
    out = []
    for h in hits:
        snippet = " ".join(h["text"].split())[:300]
        out.append(f"[{h['document']}] (score {h['score']})\n{snippet}")
    return "\n\n".join(out)
