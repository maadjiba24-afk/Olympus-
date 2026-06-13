"""Tests for the web knowledge panel (profile/playbooks/graph/outcomes)."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import profile, playbooks, relgraph, usermem, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    yield
    store.reset()


@pytest.fixture()
def server(monkeypatch):
    from olympus import web
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", web
    srv.shutdown()


def _get(url, path):
    return json.loads(urllib.request.urlopen(url + path).read())


def _post(url, payload):
    req = urllib.request.Request(
        url + "/api/memory", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())


def test_view_returns_all_sections(server):
    base, web = server
    user = web._user_for("k1")
    profile.set_about(user, "runs a bakery")
    playbooks.save(user, "weekly update", ["pull metrics"])
    relgraph.add_edge(user, "Sarah", "works_at", "Acme",
                      src_kind="person", dst_kind="company")
    usermem.add_memory(user, type="preference", content="concise", confidence=0.9)
    d = _get(base, "/api/memory?session=k1")
    assert d["profile"]["about"] == "runs a bakery"
    assert any(p["name"] == "weekly update" for p in d["playbooks"])
    assert any(n["label"] == "Acme" for n in d["graph"])
    assert any(m["content"] == "concise" for m in d["memories"])
    assert "outcomes" in d and "insights" in d


def test_set_profile_via_web(server):
    base, web = server
    user = web._user_for("k2")
    out = _post(base, {"session": "k2", "kind": "profile", "op": "set",
                       "value": "loves dark mode"})
    assert out["profile"]["about"] == "loves dark mode"
    assert profile.get(user)["about"] == "loves dark mode"


def test_approve_proposed_playbook_via_web(server):
    base, web = server
    user = web._user_for("k3")
    p = playbooks.propose(user, "onboarding", ["create", "welcome"])
    assert playbooks.match(user, "run onboarding") is None      # inert while proposed
    _post(base, {"session": "k3", "kind": "playbook", "op": "approve", "id": p["id"]})
    assert playbooks.match(user, "run onboarding")["name"] == "onboarding"


def test_forget_graph_entity_via_web(server):
    base, web = server
    user = web._user_for("k4")
    relgraph.add_edge(user, "Sarah", "works_at", "Acme")
    _post(base, {"session": "k4", "kind": "graph", "op": "forget", "label": "Acme"})
    assert relgraph.nodes(user) == [] or all(
        n["label"] != "Acme" for n in relgraph.nodes(user))


def test_page_has_memory_panel(server):
    base, _ = server
    html = urllib.request.urlopen(base + "/").read().decode()
    assert "memory" in html and "renderMemoryData" in html
