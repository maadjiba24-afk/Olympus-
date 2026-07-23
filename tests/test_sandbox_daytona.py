"""Daytona remote-sandbox backend (DEFERRED #11).

A thin remote backend: `sandbox.run` with OLYMPUS_EXEC_BACKEND=daytona submits
the (already cmdguard-checked) command to a Daytona workspace API over the
SSRF-gated POST choke (`tools._http_post_json`, which returns parsed JSON) and
maps the reply into a Result. The security gate still runs BEFORE submission —
the remote box is protected by the same fail-closed rule as local/docker.
"""

import pytest

from olympus import sandbox, tools


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "daytona")
    monkeypatch.setenv("OLYMPUS_DAYTONA_URL", "https://daytona.example")
    monkeypatch.setenv("OLYMPUS_DAYTONA_WORKSPACE", "ws1")
    monkeypatch.delenv("OLYMPUS_DAYTONA_TOKEN", raising=False)
    return tmp_path


# --- the security gate runs BEFORE any remote submission --------------------

def test_cmdguard_still_refuses_catastrophic(ws, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(sandbox, "_daytona_submit",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    r = sandbox.run("rm -rf /")
    assert r.code == 126 and "security gate" in r.output
    assert called["n"] == 0                        # never reached the remote


# --- unconfigured / broken setup fails closed, never raises -----------------

def test_unconfigured_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "daytona")
    monkeypatch.delenv("OLYMPUS_DAYTONA_URL", raising=False)
    r = sandbox.run("echo hi")
    assert r.ok is False and r.code == 127 and "daytona" in r.output.lower()


# --- response mapping (submit returns a parsed dict) ------------------------

def test_maps_success_and_watch():
    res = sandbox._daytona_run(
        "echo hi", 30, watch="ERROR",
        submit=lambda c, t: {"exitCode": 0, "result": "hi\nERROR x\n"})
    assert res.ok and res.code == 0 and res.output == "hi\nERROR x"
    assert res.watched == ("ERROR x",)


def test_maps_nonzero_exit():
    res = sandbox._daytona_run(
        "false", 30, submit=lambda c, t: {"code": 3, "output": "nope"})
    assert res.ok is False and res.code == 3 and res.output == "nope"


def test_non_dict_reply_fails_closed():
    res = sandbox._daytona_run("x", 30, submit=lambda c, t: "not a dict")
    assert res.ok is False and res.code == 127


def test_degraded_error_reply_fails_closed():
    # tools._http_post_json returns {'_error': ...} on a network/parse failure
    res = sandbox._daytona_run("x", 30,
                               submit=lambda c, t: {"_error": "network down"})
    assert res.ok is False and res.code == 127 and "network down" in res.output


def test_egress_refusal_is_126():
    def blocked(c, t):
        raise ValueError("blocked: internal host")
    res = sandbox._daytona_run("x", 30, submit=blocked)
    assert res.code == 126 and "egress refused" in res.output


def test_output_capped():
    big = "A" * (sandbox.OUTPUT_CAP + 500)
    res = sandbox._daytona_run(
        "x", 30, submit=lambda c, t: {"exitCode": 0, "result": big})
    assert len(res.output) <= sandbox.OUTPUT_CAP + 60 and "truncated" in res.output


# --- the submit routes through the gated POST with the bearer ----------------

def test_submit_uses_gated_post_with_bearer(ws, monkeypatch):
    monkeypatch.setenv("OLYMPUS_DAYTONA_TOKEN", "tok123")
    seen = {}

    def fake_post(url, payload, timeout=20, headers=None):
        seen.update(url=url, payload=payload, headers=headers)
        return {"exitCode": 0, "result": "done"}
    monkeypatch.setattr(tools, "_http_post_json", fake_post)
    out = sandbox._daytona_submit("echo hi", 30)
    assert out["result"] == "done"
    assert seen["url"].endswith("/workspace/ws1/toolbox/process/execute")
    assert seen["payload"]["command"] == "echo hi"
    assert seen["headers"]["Authorization"] == "Bearer tok123"


def test_run_routes_to_daytona(ws, monkeypatch):
    monkeypatch.setattr(sandbox, "_daytona_submit",
                        lambda c, t: {"exitCode": 0, "result": "remote-ran"})
    r = sandbox.run("echo hi")
    assert r.ok and r.output == "remote-ran"
