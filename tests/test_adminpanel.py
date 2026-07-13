"""The read-only operator admin panel: /admin shell + /api/admin data.

Covers: full snapshot assembly, zero-secret-leak guarantee, section fault
isolation, and the auth posture (token when configured, loopback-only when
not, never open behind a proxy or on a public bind)."""

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import adminpanel, goals, scheduler, skills, web

SECTIONS = ("instance", "flags", "models", "budget", "channels", "heartbeat",
            "goals", "approvals", "skills", "schedules", "connectors",
            "config", "evolution", "security", "health", "errors")


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch):
    real_connect = socket.socket.connect

    def guard(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"unexpected outbound socket to {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guard)
    yield


# --- snapshot assembly -------------------------------------------------------

def test_snapshot_has_every_section(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    snap = adminpanel.snapshot()
    for section in SECTIONS:
        assert section in snap, f"missing section {section}"
        v = snap[section]
        assert not (isinstance(v, dict) and set(v) == {"error"}), \
            f"section {section} failed to collect: {v}"
    assert snap["generated_at"] > 0


def test_snapshot_reflects_live_state(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    goals.add("cli", "keep CI green", "badge green for a week")
    scheduler.add("digest", "daily", "summarize the news", user="cli")
    skills.create("Panel Skill", "demo", "1. Do.", provisional=True)
    snap = adminpanel.snapshot()
    assert any(g["text"].startswith("keep CI green") for g in snap["goals"])
    assert any(j["name"] == "digest" for j in snap["schedules"])
    assert snap["skills"]["count"] == 1 and snap["skills"]["provisional"] == 1
    names = {c["name"] for c in snap["heartbeat"]["cycles"]}
    assert {"daily_learning", "skill_curation"} <= names


def test_snapshot_never_contains_secret_values(monkeypatch):
    secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-supersecret-000111",
        "TELEGRAM_BOT_TOKEN": "12345:telegramsecrettoken",
        "SLACK_BOT_TOKEN": "xoxb-slacksecret-999",
        "WHATSAPP_ACCESS_TOKEN": "whatsappsecret-888",
        "OLYMPUS_ACCESS_TOKEN": "operator-token-777",
        "OLYMPUS_API_KEYS": "v1-key-666",
    }
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    blob = json.dumps(adminpanel.snapshot())
    for v in secrets.values():
        assert v not in blob, f"secret value leaked into snapshot: {v[:8]}…"
    # ...but their presence IS reported.
    snap = adminpanel.snapshot()
    assert snap["channels"]["telegram"]["configured"] is True
    assert snap["flags"]["access_token_set"] is True


def test_one_broken_section_does_not_kill_the_panel(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(adminpanel, "_channels",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    snap = adminpanel.snapshot()
    assert "error" in snap["channels"]          # visible failure...
    assert snap["goals"] == []                   # ...everything else intact


def test_base_urls_reduced_to_host(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PROVIDER", "openai")
    monkeypatch.setenv("OLYMPUS_MODEL", "gpt-5")
    monkeypatch.setenv("OLYMPUS_API_KEY", "visitorkey-123456")
    monkeypatch.setenv("OLYMPUS_BASE_URL", "https://user:pass@llm.example.com/v1")
    snap = adminpanel.snapshot()
    member = snap["models"]["members"][0]
    assert member["base_url"] == "https://llm.example.com"
    assert "pass" not in json.dumps(snap["models"])


def test_shell_page_is_data_free():
    page = adminpanel.page()
    assert "operator panel" in page
    assert "/api/admin" in page                  # fetches, doesn't embed
    assert "__OLYMPUS" not in page               # no server-side injection


# --- routes + auth posture ---------------------------------------------------

@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req)


def test_admin_shell_served(server, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-secret-in-env")
    resp = _get(server + "/admin")
    body = resp.read().decode()
    assert resp.status == 200
    assert "operator panel" in body
    assert "tg-secret-in-env" not in body        # the shell carries no data


def test_api_admin_loopback_allowed_without_token(server, monkeypatch):
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)
    body = json.loads(_get(server + "/api/admin").read())
    for section in SECTIONS:
        assert section in body


def test_api_admin_requires_token_when_configured(server, monkeypatch):
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "op-tok")
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(server + "/api/admin")
    assert err.value.code == 401                 # no header -> refused
    body = json.loads(_get(server + "/api/admin",
                           headers={"X-Olympus-Token": "op-tok"}).read())
    assert "instance" in body


def test_api_admin_refuses_proxied_request_without_token(server, monkeypatch):
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(server + "/api/admin",
             headers={"X-Forwarded-For": "203.0.113.9"})
    assert err.value.code == 401                 # relayed off-box -> refused


def test_admin_auth_refuses_remote_peer_without_token(monkeypatch):
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)

    class _H(web.Handler):
        def __init__(self):                      # bypass socket setup
            self.client_address = ("203.0.113.7", 5555)
            self.headers = {}

    ok, code, _msg = _H()._admin_authorized()
    assert not ok and code == 403


def test_admin_auth_refuses_public_bind_without_token(monkeypatch):
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)

    class _Srv:
        server_address = ("0.0.0.0", 8000)

    class _H(web.Handler):
        def __init__(self):
            self.client_address = ("127.0.0.1", 5555)
            self.headers = {}
            self.server = _Srv()

    ok, code, _msg = _H()._admin_authorized()
    assert not ok and code == 401


# --- Phase 2: act on running state -------------------------------------------

def test_act_unknown_op_is_in_band_error():
    r = adminpanel.act("teleport", {})
    assert not r["ok"] and "unknown op" in r["message"]


def test_act_goal_lifecycle():
    r = adminpanel.act("goal_add", {"text": "ship the deck",
                                    "contract": "deck emailed"})
    assert r["ok"]
    gid = goals.active("admin")[0].id
    assert adminpanel.act("goal_done", {"id": gid})["ok"]
    assert goals.get(gid).status == "done"
    r = adminpanel.act("goal_drop", {"id": "nope"})
    assert r["ok"] and "No goal" in r["message"]     # message passthrough


def test_act_schedule_lifecycle():
    r = adminpanel.act("schedule_add", {"name": "digest", "interval": "daily",
                                        "prompt": "summarize the news"})
    assert r["ok"]
    assert adminpanel.act("schedule_disable", {"name": "digest"})["ok"]
    assert not [j for j in scheduler._load() if j.enabled]
    assert adminpanel.act("schedule_enable", {"name": "digest"})["ok"]
    assert adminpanel.act("schedule_remove", {"name": "digest"})["ok"]
    assert scheduler._load() == []
    r = adminpanel.act("schedule_remove", {"name": "digest"})
    assert not r["ok"]                                # gone -> in-band error
    r = adminpanel.act("schedule_add", {"name": "", "prompt": ""})
    assert not r["ok"] and "needs" in r["message"]


def test_act_approve_and_reject_held_actions(monkeypatch):
    from olympus import actions
    monkeypatch.setenv("OLYMPUS_MEMORY_DIR", "")     # isolated_memory rules
    a1 = actions.prepare("alice", "save_note", {"title": "t", "content": "c"},
                         why="test")
    a2 = actions.prepare("alice", "save_note", {"title": "t2", "content": "c"})
    r = adminpanel.act("approve_action", {"user": "alice", "id": a1.id})
    assert r["ok"], r
    assert actions.get("alice", a1.id).status in ("executed", "failed")
    r = adminpanel.act("reject_action", {"user": "alice", "id": a2.id})
    assert r["ok"]
    assert actions.get("alice", a2.id).status == "rejected"
    # Double-approve is an in-band error, not a crash.
    r = adminpanel.act("approve_action", {"user": "alice", "id": a1.id})
    assert not r["ok"]


def test_act_set_autonomy():
    from olympus import actions
    r = adminpanel.act("set_autonomy", {"user": "alice", "level": 3})
    assert r["ok"] and "L3" in r["message"]
    assert actions.autonomy_level("alice") == 3
    assert not adminpanel.act("set_autonomy", {"level": 2})["ok"]  # no user


def test_act_background_triggers(monkeypatch):
    import threading
    ran = threading.Event()
    from olympus import curator
    monkeypatch.setattr(curator, "curate", lambda: ran.set())
    r = adminpanel.act("run_curate", {})
    assert r["ok"] and "background" in r["message"]
    assert ran.wait(5)                               # actually executed


# --- Phase 2 route + CSRF/auth posture ----------------------------------------

def _post(url, payload, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    return urllib.request.urlopen(req)


def test_admin_act_route_requires_csrf_header(server, monkeypatch):
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server + "/api/admin/act", {"op": "goal_add",
                                          "params": {"text": "x"}})
    assert err.value.code == 403                     # custom header missing


def test_admin_act_route_works_with_header(server, monkeypatch):
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)
    resp = _post(server + "/api/admin/act",
                 {"op": "goal_add", "params": {"text": "from the panel"}},
                 headers={"X-Olympus-Admin": "1"})
    body = json.loads(resp.read())
    assert body["ok"]
    assert any(g.text == "from the panel" for g in goals.active("admin"))


def test_admin_act_route_bad_op_is_400(server, monkeypatch):
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server + "/api/admin/act", {"op": "nope"},
              headers={"X-Olympus-Admin": "1"})
    assert err.value.code == 400


def test_admin_act_route_requires_token_when_configured(server, monkeypatch):
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "op-tok")
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server + "/api/admin/act", {"op": "goal_add",
                                          "params": {"text": "x"}},
              headers={"X-Olympus-Admin": "1"})
    assert err.value.code == 401
    resp = _post(server + "/api/admin/act",
                 {"op": "goal_add", "params": {"text": "with token"}},
                 headers={"X-Olympus-Admin": "1", "X-Olympus-Token": "op-tok"})
    assert json.loads(resp.read())["ok"]
