"""Tests for optional embeddings, hybrid retrieval, and the web memory surface."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import embed, recall, usermem, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    for v in ("OLYMPUS_EMBED_MODEL", "OLYMPUS_EMBED_BASE_URL",
              "OLYMPUS_EMBED_API_KEY", "OLYMPUS_API_KEY", "OPENAI_API_KEY",
              "OLYMPUS_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    yield
    store.reset()


# --- embeddings client (graceful) ----------------------------------------

def test_unavailable_by_default():
    assert embed.available() is False
    assert embed.embed(["hi"]) is None       # never calls out when off


def test_available_when_configured(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("OLYMPUS_EMBED_API_KEY", "sk-x")
    assert embed.available() is True


def test_embed_parses_openai_response(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMBED_MODEL", "m")
    monkeypatch.setenv("OLYMPUS_EMBED_API_KEY", "sk-x")
    class _R:
        def read(self): return json.dumps(
            {"data": [{"index": 0, "embedding": [0.1, 0.2]},
                      {"index": 1, "embedding": [0.3, 0.4]}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(embed.urllib.request, "urlopen", lambda *a, **k: _R())
    assert embed.embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_returns_none_on_error(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMBED_MODEL", "m")
    monkeypatch.setenv("OLYMPUS_EMBED_API_KEY", "sk-x")
    def boom(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(embed.urllib.request, "urlopen", boom)
    assert embed.embed(["x"]) is None        # best-effort, swallows the error


def test_cosine():
    assert embed.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert embed.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert embed.cosine([], [1]) == 0.0


# --- hybrid retrieval -----------------------------------------------------

def test_lexical_only_when_embeddings_off():
    usermem.add_memory("u", type="project", content="the bakery app Crumb",
                       confidence=0.9, importance=0.7)
    # paraphrase with no shared keywords → lexical misses, no embeddings → []
    assert recall.retrieve("u", "how is the pastry shop software") == []


def test_semantic_fallback_when_lexical_thin(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMBED_MODEL", "m")
    monkeypatch.setenv("OLYMPUS_EMBED_API_KEY", "sk-x")
    m = usermem.add_memory("u", type="project", content="the bakery app Crumb",
                           confidence=0.9, importance=0.8)
    usermem.set_embedding("u", m["id"], [1.0, 0.0, 0.0])
    # query embedding close to the stored one → semantic hit despite no keywords
    monkeypatch.setattr(embed, "embed_one", lambda text: [0.96, 0.1, 0.0])
    hits = recall.retrieve("u", "how is the pastry shop software")
    assert len(hits) == 1 and hits[0]["id"] == m["id"]


def test_semantic_skipped_when_lexical_sufficient(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMBED_MODEL", "m")
    monkeypatch.setenv("OLYMPUS_EMBED_API_KEY", "sk-x")
    for i in range(3):
        usermem.add_memory("u", type="project",
                           content=f"bakery project note {i}", confidence=0.9,
                           importance=0.7)
    called = {"n": 0}
    monkeypatch.setattr(embed, "embed_one",
                        lambda t: called.__setitem__("n", called["n"] + 1) or [1.0])
    recall.retrieve("u", "bakery project note")    # plenty of lexical hits
    assert called["n"] == 0                         # never paid for embeddings


def test_commit_embeds_when_available(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMBED_MODEL", "m")
    monkeypatch.setenv("OLYMPUS_EMBED_API_KEY", "sk-x")
    monkeypatch.setattr(embed, "embed_one", lambda t: [0.5, 0.5])
    recall._gate("u", {"type": "preference", "content": "likes dark mode",
                       "confidence": 0.9, "sensitivity": "normal"}, "e1")
    m = usermem.active_memories("u")[0]
    assert m.get("embedding") == [0.5, 0.5]


# --- web memory surface ---------------------------------------------------

@pytest.fixture()
def server(monkeypatch):
    from olympus import web
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", web
    srv.shutdown()


def _post(url, path, payload):
    req = urllib.request.Request(
        url + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())


def test_web_lists_memories_and_candidates(server):
    base, web = server
    user = web._user_for("ms")
    usermem.add_memory(user, type="preference", content="concise please",
                       confidence=0.9)
    usermem.add_candidate(user, {"type": "identity", "content": "has condition X",
                                 "reason": "sensitive"})
    out = json.loads(urllib.request.urlopen(base + "/api/memory?session=ms").read())
    assert len(out["memories"]) == 1 and len(out["candidates"]) == 1


def test_web_approve_candidate(server):
    base, web = server
    user = web._user_for("ms2")
    c = usermem.add_candidate(user, {"type": "identity", "content": "vegan",
                                     "confidence": 0.8})
    out = _post(base, "/api/memory", {"session": "ms2", "id": c["id"], "op": "approve"})
    assert out["ok"] is True
    assert any(m["content"] == "vegan" for m in usermem.active_memories(user))
    assert usermem.candidates(user) == []


def test_web_forget_memory(server):
    base, web = server
    user = web._user_for("ms3")
    m = usermem.add_memory(user, type="project", content="x", confidence=0.9)
    _post(base, "/api/memory", {"session": "ms3", "id": m["id"], "op": "forget"})
    assert usermem.active_memories(user) == []
