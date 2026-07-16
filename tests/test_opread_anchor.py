"""Operational readiness A — external head anchor (closes the truncation gap).

Every append to a signed log (checkpoint chain, speculation record, delta
snapshot) publishes the new head — witness-signed — to a sink OUTSIDE the
host's write domain. `verify_anchor()` recomputes local heads and reports
divergence, so a truncated/rolled-back log no longer passes silently.

Done bar: a truncated ledger is DETECTED; anchor writes are FAIL-LOUD
(unreachable sink alerts, never a silent skip); anchor updates are signed.
"""

import json

import pytest

from olympus import anchor, config, deltas, ledger, witness

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "opread-a-seed")
    monkeypatch.setattr(ledger, "_locks", {})
    monkeypatch.setattr(deltas, "_locks", {})
    # The anchor sink lives OUTSIDE the memory dir (the host's write domain).
    monkeypatch.setenv("OLYMPUS_ANCHOR", "dir")
    monkeypatch.setenv("OLYMPUS_ANCHOR_DIR", str(tmp_path / "external-anchor"))


_PLAN = [{"action": "add", "n": i} for i in range(1, 4)]


def _exec(prev, step, i):
    return {"total": (prev or {}).get("total", 0) + step["n"]}


# --- the Done bar: truncation is detected ----------------------------------

@_signing
def test_truncated_checkpoint_ledger_is_detected():
    ledger.drive("run-t", _PLAN, _exec)
    assert anchor.verify_anchor()["ok"]                 # intact → PASS
    # Truncate: drop the newest committed record — the classic silent rollback.
    path = ledger._path("run-t")
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")
    # The chain still self-verifies (this is exactly the gap)...
    assert ledger.verify_ledger("run-t")["ok"]
    # ...but the external anchor catches it.
    rep = anchor.verify_anchor()
    assert not rep["ok"]
    kinds = {d["kind"] for d in rep["divergences"]}
    assert "truncated" in kinds
    assert any("run-t" in d["target"] for d in rep["divergences"])


@_signing
def test_truncated_delta_history_is_detected():
    for v in range(3):
        deltas.record_snapshot("playbook:c1", kind="playbook",
                               state={"v": v},
                               provenance=deltas.Provenance("ace"))
    assert anchor.verify_anchor()["ok"]
    path = deltas._path("playbook:c1")
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")
    assert deltas.verify_history("playbook:c1")["ok"]   # self-check can't see it
    rep = anchor.verify_anchor()
    assert not rep["ok"]
    assert any(d["kind"] == "truncated" and "playbook:c1" in d["target"]
               for d in rep["divergences"])


@_signing
def test_rewritten_head_is_detected_as_mismatch():
    ledger.drive("run-m", _PLAN, _exec)
    path = ledger._path("run-m")
    lines = path.read_text().splitlines()
    node = json.loads(lines[-1])
    node["node_hash"] = "00" * 32                       # rewrite the head
    path.write_text("\n".join(lines[:-1] + [json.dumps(node)]) + "\n")
    rep = anchor.verify_anchor()
    assert any(d["kind"] == "head-mismatch" for d in rep["divergences"])


@_signing
def test_deleted_local_log_is_detected():
    ledger.drive("run-d", _PLAN, _exec)
    ledger._path("run-d").unlink()
    rep = anchor.verify_anchor()
    assert any(d["kind"] == "missing-local" for d in rep["divergences"])


@_signing
def test_speculation_heads_are_anchored():
    ledger.record_speculation("run-s", "prune", {"reason": "node_cap"})
    sink = anchor._sink()
    rec = sink.read("speculation", "run-s")
    assert rec and rec["seq"] == 0 and anchor.record_ok(rec)


# --- fail-loud: unreachable sink alerts, never silent -----------------------

@_signing
def test_unreachable_sink_alerts_and_append_still_commits(monkeypatch, capsys):
    monkeypatch.setenv("OLYMPUS_ANCHOR_DIR", "")        # sink misconfigured
    alerts = []
    from olympus import gateway
    monkeypatch.setattr(gateway, "notify_all",
                        lambda text: alerts.append(text) or [])
    node = ledger.open_run("run-u").append({"a": 1}, {"s": 1})
    # The local append still committed (execution is not held hostage)...
    assert node["seq"] == 0 and ledger.verify_ledger("run-u")["ok"]
    # ...and the failure was LOUD: gateway alert + stderr, naming the target.
    assert alerts and "ANCHOR WRITE FAILED" in alerts[0] and "run-u" in alerts[0]
    assert "ANCHOR WRITE FAILED" in capsys.readouterr().err


@_signing
def test_stale_anchor_is_reported(monkeypatch):
    # An anchor write missed (anchoring off for one append) → local runs ahead;
    # verify-anchor flags the gap instead of pretending all is well.
    led = ledger.open_run("run-st")
    led.append({"a": 1}, {})
    monkeypatch.setenv("OLYMPUS_ANCHOR", "")            # miss one anchor write
    led.append({"a": 2}, {})
    monkeypatch.setenv("OLYMPUS_ANCHOR", "dir")
    rep = anchor.verify_anchor()
    assert any(d["kind"] == "anchor-stale" for d in rep["divergences"])


# --- anchor records are witness-signed --------------------------------------

@_signing
def test_anchor_records_are_signed_and_tamper_evident(tmp_path):
    ledger.open_run("run-sig").append({"a": 1}, {})
    sink = anchor._sink()
    rec = sink.read("checkpoint", "run-sig")
    assert anchor.record_ok(rec)
    rec["seq"] = 5                                       # tamper the anchor
    assert not anchor.record_ok(rec)
    # A tampered anchor in the sink is itself reported.
    path = sink.root / anchor._fname("checkpoint", "run-sig")
    path.write_text(json.dumps(rec), encoding="utf-8")
    rep = anchor.verify_anchor()
    assert any(d["kind"] == "bad-anchor" for d in rep["divergences"])


# --- off by default ----------------------------------------------------------

def test_disabled_anchoring_is_inert(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_ANCHOR", "")
    ledger.open_run("run-off").append({"a": 1}, {})     # must not raise
    assert not (tmp_path / "external-anchor").exists()  # nothing written
    rep = anchor.verify_anchor()
    assert not rep["ok"] and not rep["enabled"]


# --- the git sink -------------------------------------------------------------

def _git(repo, *args):
    import subprocess
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


@_signing
def test_git_sink_commits_and_pushes_to_the_anchor_remote(tmp_path, monkeypatch):
    # A dedicated anchor repo with a (local, bare) remote standing in for the
    # out-of-host machine: publish writes the head, commits, and PUSHES.
    remote = tmp_path / "anchor-remote.git"
    import subprocess
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo = tmp_path / "anchor-repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "anchor")
    _git(repo, "remote", "add", "origin", str(remote))
    monkeypatch.setenv("OLYMPUS_ANCHOR", "git")
    monkeypatch.setenv("OLYMPUS_ANCHOR_REPO", str(repo))
    monkeypatch.setenv("OLYMPUS_ANCHOR_REMOTE", "origin")
    monkeypatch.setenv("OLYMPUS_ANCHOR_BRANCH", "anchors")

    ledger.open_run("run-git").append({"a": 1}, {})
    # The head file exists, is committed, and reached the remote.
    assert _git(repo, "status", "--porcelain") == ""    # committed, clean
    names = _git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    assert any(n.startswith("checkpoint__run-git") for n in names.splitlines())
    remote_log = subprocess.run(
        ["git", "-C", str(remote), "log", "--oneline", "anchors"],
        capture_output=True, text=True)
    assert remote_log.returncode == 0 and "anchor checkpoint/run-git" \
        in remote_log.stdout


@_signing
def test_git_sink_unreachable_remote_alerts(tmp_path, monkeypatch):
    repo = tmp_path / "anchor-repo2"
    import subprocess
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "anchor")
    _git(repo, "remote", "add", "origin", str(tmp_path / "no-such-remote.git"))
    monkeypatch.setenv("OLYMPUS_ANCHOR", "git")
    monkeypatch.setenv("OLYMPUS_ANCHOR_REPO", str(repo))
    monkeypatch.setenv("OLYMPUS_ANCHOR_REMOTE", "origin")
    alerts = []
    from olympus import gateway
    monkeypatch.setattr(gateway, "notify_all",
                        lambda text: alerts.append(text) or [])
    node = ledger.open_run("run-git2").append({"a": 1}, {})
    assert node["seq"] == 0                              # append still commits
    assert alerts and "ANCHOR WRITE FAILED" in alerts[0]  # loud, never silent
