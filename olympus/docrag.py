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

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import annindex, config, documents, embed, memory, store

# Persistent HNSW index over the user's document chunks — the one place with a
# stable, cached, repeatedly-queried corpus, so the graph's amortised speedup
# actually pays off (unlike a one-shot `nearest` scan). Keyed to a corpus
# signature so it rebuilds ONLY when documents change.
_ANN_NS = "docrag.ann"
_ANN_SIG_NS = "docrag.ann.sig"
_ANN_CANDIDATES = 128        # vector-candidate pool; the lexical floor covers the rest

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
    """(Re)build the per-document chunk index, re-embedding only documents
    whose content changed. Returns {slug: {sha256, name, chunks,
    embeddings|None}}. Best-effort; never raises."""
    cache = _load_index(user)
    fresh: dict = {}

    for meta in documents.listing(user):
        slug = meta["slug"]
        body = documents.read(user, meta["name"])
        if body is None:
            continue

        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        cached = cache.get(slug)

        if cached and cached.get("sha256") == digest:
            fresh[slug] = cached
            continue

        chunks = chunk(body)
        embeddings = None
        if embed.available() and chunks:
            embeddings = embed.embed(chunks)

        fresh[slug] = {
            "sha256": digest,
            "name": meta["name"],
            "chunks": chunks,
            "embeddings": embeddings,
        }

    try:
        _index_path(user).write_text(
            json.dumps(fresh),
            encoding="utf-8",
        )
    except OSError:
        pass

    return fresh


def _corpus_signature(index: dict) -> bytes:
    """Fingerprint the embedded corpus using content, legacy mtime, and chunks."""
    parts = [
        f"{slug}:{entry.get('sha256')}:{entry.get('mtime')}:"
        f"{len(entry.get('chunks') or [])}"
        for slug, entry in sorted(index.items())
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest().encode()


def _persistent_index(user: str, index: dict, items: dict) -> annindex.HNSW:
    """Build-or-load the user's persistent HNSW. Loads the cached graph when the
    corpus signature matches (the common case turn-to-turn); rebuilds and
    re-persists only when documents changed. Best-effort — a store hiccup just
    falls back to an in-memory build."""
    key = memory.safe_id(user)
    sig = _corpus_signature(index)
    try:
        if store.backend().get(_ANN_SIG_NS, key) == sig:
            idx = annindex.load_index(_ANN_NS, key)
            if len(idx) == len(items):
                return idx
    except Exception:
        pass
    idx = annindex.build(items)
    try:
        annindex.save_index(_ANN_NS, key, idx)
        store.backend().put(_ANN_SIG_NS, key, sig)
    except Exception:
        pass
    return idx


def retrieve(user: str, query: str, k: int = 4) -> list[dict]:
    """Top-k relevant document chunks: [{document, text, score}]. Semantic when
    embeddings are configured, lexical otherwise (and lexical always contributes
    a floor so an exact keyword hit is never lost)."""
    index = build_index(user)
    if not index:
        return []
    qtokens = _tokens(query)
    qvec = embed.embed_one(query) if embed.available() else None
    # Vector similarities for the embedded chunks. Two regimes, same result shape:
    #  * small corpus / OLYMPUS_ANN off / replay → an exact cosine scan over all
    #    chunks (identical to before), so the default path is unchanged.
    #  * large corpus + OLYMPUS_ANN on → query a PERSISTENT HNSW graph (built
    #    once, reused turn-to-turn), so retrieval scales sublinearly on the
    #    high-dimension cosine that dominates per-chunk cost. The graph is skipped
    #    during replay to keep that path side-effect-free.
    vec_sims: dict[str, float] = {}
    if qvec:
        items: dict[str, list] = {}
        for slug, entry in index.items():
            embs = entry.get("embeddings") or []
            chunks = entry.get("chunks") or []
            for i in range(min(len(chunks), len(embs))):
                if embs[i]:
                    items[f"{slug}\x00{i}"] = embs[i]
        if items:
            use_graph = (annindex.enabled()
                         and len(items) >= annindex.EXACT_THRESHOLD
                         and not os.environ.get("OLYMPUS_REPLAY"))
            if use_graph:
                idx = _persistent_index(user, index, items)
                hits = annindex.query_index(
                    idx, qvec, k=min(len(items), _ANN_CANDIDATES), min_sim=-1.0)
            else:
                hits = annindex.nearest(qvec, items, k=len(items), min_sim=-1.0)
            for key, sim in hits:
                vec_sims[key] = max(0.0, sim)
    scored = []
    for slug, entry in index.items():
        chunks = entry.get("chunks") or []
        for i, ch in enumerate(chunks):
            lex = _overlap(qtokens, _tokens(ch))
            vec = vec_sims.get(f"{slug}\x00{i}", 0.0)
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
