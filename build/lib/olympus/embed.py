"""Optional embeddings — semantic recall when an embeddings endpoint exists.

Anthropic has no first-party embeddings API, and Olympus must run anywhere with
zero setup, so embeddings are strictly OPTIONAL and OFF by default. When the
user points OLYMPUS_EMBED_MODEL at any OpenAI-compatible /embeddings endpoint
(OpenAI, a local Ollama/LM Studio server, OpenRouter, ...), retrieval gains a
semantic fallback; when they don't, retrieval stays purely lexical with zero
change in behavior. Everything here is best-effort: any failure returns None and
the caller falls back to lexical, so embeddings can never break a conversation.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _model() -> str | None:
    return os.environ.get("OLYMPUS_EMBED_MODEL")


def _base_url() -> str:
    return (os.environ.get("OLYMPUS_EMBED_BASE_URL")
            or os.environ.get("OLYMPUS_BASE_URL")
            or DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str | None:
    return (os.environ.get("OLYMPUS_EMBED_API_KEY")
            or os.environ.get("OLYMPUS_API_KEY")
            or os.environ.get("OPENAI_API_KEY"))


def available() -> bool:
    """True only if a model is configured and we can reach an endpoint (a key,
    or a base URL for a keyless local server)."""
    if not _model():
        return False
    return bool(_api_key() or os.environ.get("OLYMPUS_EMBED_BASE_URL")
                or os.environ.get("OLYMPUS_BASE_URL"))


def embed(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts via an OpenAI-compatible endpoint. Best-effort:
    returns None on any problem so callers fall back to lexical retrieval."""
    if not available() or not texts:
        return None
    payload = {"model": _model(), "input": texts}
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        f"{_base_url()}/embeddings", data=json.dumps(payload).encode(),
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        rows = sorted(data["data"], key=lambda r: r.get("index", 0))
        return [r["embedding"] for r in rows]
    except (urllib.error.URLError, TimeoutError, OSError, KeyError,
            ValueError, json.JSONDecodeError):
        return None


def embed_one(text: str) -> list[float] | None:
    vecs = embed([text])
    return vecs[0] if vecs else None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
