"""P2U: assessment authorization evidence is exact-owner and fail-closed.

These grants are the code-level predicate permitting target-touching security
assessment I/O.  Ambiguous ownership, malformed state interpreted as no grants,
or a lost read-modify-write can therefore invalidate the authorization boundary.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from olympus import (assess, atomicio, builtin_actions, cli, config, memory,
                     selfassess, tools)


A = "email-a.b@example.test"
B = "email-a-b-example-test"
LONG_A = "email-" + "x" * 80 + ".alpha"
LONG_B = "email-" + "x" * 80 + "-alpha"
PAIRS = ((A, B), (LONG_A, LONG_B))


@pytest.mark.skipif(os.name != "nt", reason="native Windows authorization long-path contract")
def test_native_windows_authorization_long_paths_keep_grants_and_repair_bytes(tmp_path, monkeypatch):
    root = tmp_path / ("a" * 80) / ("b" * 80) / ("c" * 80)
    assert len(str(root)) > 260
    monkeypatch.setattr(config, "MEMORY_DIR", root)
    legacy = assess._io(assess._legacy_auth_path(LONG_A))
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"unclaimed legacy authorization")
    for owner, target in ((LONG_A, "first.example"), (LONG_B, "second.example")):
        assess.grant([target], user=owner)
        assert assess.authorization_status(owner)["state"] == "valid"
        assert assess.in_scope(target, owner)
        assert memory.storage_key(owner).endswith(hashlib.sha256(owner.encode()).hexdigest())
    assert not assess.in_scope("first.example", LONG_B)
    assert not assess.in_scope("second.example", LONG_A)
    target = assess._io(assess._auth_path(LONG_A))
    raw = b'{"unavailable":"preserve owned evidence"'
    target.write_bytes(raw)
    with pytest.raises(assess.AssessAuthorizationStateError):
        assess.grant(["must-not-run.example"], user=LONG_A)
    publish = atomicio.publish
    calls = []
    def fail_reset(tmp, destination, data, **kwargs):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError("owned interrupted reset")
        return publish(tmp, destination, data, **kwargs)
    monkeypatch.setattr(atomicio, "publish", fail_reset)
    with pytest.raises(OSError, match="owned interrupted reset"):
        assess.repair_authorizations(LONG_A)
    assert target.read_bytes() == raw
    archives = list(target.parent.glob("authorizations.corrupt.*.json"))
    assert len(archives) == 1 and archives[0].read_bytes() == raw
    monkeypatch.setattr(atomicio, "publish", publish)
    repaired = assess.repair_authorizations(LONG_A)
    assert repaired["repaired"] and repaired["quarantined_sha256"] == hashlib.sha256(raw).hexdigest()
    assert assess.active_authorizations(LONG_A) == []
    assert assess.in_scope("second.example", LONG_B)
    assert legacy.read_bytes() == b"unclaimed legacy authorization"
    assert archives[0].read_bytes() == raw


def _record(target: str = "example.test", *, auth_id: str = "auth-1-abcdef"):
    return {
        "id": auth_id,
        "targets": [target],
        "created": 1.0,
        "expires": 4_000_000_000.0,
        "note": "operator-approved fixture",
        "approved_by": "operator",
    }


def _corrupt(user: str, raw: bytes = b'[{"id":'):
    path = assess._io(assess._auth_path(user))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


@pytest.mark.parametrize("first,second", PAIRS)
def test_exact_owner_assessment_directories_do_not_collapse(first, second):
    assert memory.safe_id(first) == memory.safe_id(second)
    assert assess._store_dir(first) != assess._store_dir(second)
    assert assess._auth_path(first) != assess._auth_path(second)

    assess.grant(["first.example"], user=first)
    assess.grant(["second.example"], user=second)

    assert assess.in_scope("first.example", user=first) is True
    assert assess.in_scope("first.example", user=second) is False
    assert assess.in_scope("second.example", user=second) is True
    assert assess.in_scope("second.example", user=first) is False


def test_ambient_owner_uses_exact_context_not_lossy_current_user():
    assert memory.safe_id(A) == memory.safe_id(B)

    memory.set_user(A)
    assess.grant(["private.example"])
    assert assess.in_scope("private.example") is True

    memory.set_user(B)
    assert assess.in_scope("private.example") is False
    assert assess.active_authorizations() == []


def test_legacy_lossy_authorization_is_preserved_and_claimed_by_nobody():
    legacy = assess._io(assess._legacy_auth_path(A))
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps([_record("legacy-secret.example")]),
                      encoding="utf-8")
    before = legacy.read_bytes()

    for owner in (A, B, memory.safe_id(A)):
        assert assess.active_authorizations(owner) == []
        assert assess.in_scope("legacy-secret.example", owner) is False
        status = assess.authorization_status(owner)
        assert status["state"] == "missing"
        assert status["legacy_quarantined"] is True
        assert "legacy-secret.example" not in json.dumps(status)

    assess.grant(["new.example"], user=A)
    assert assess.in_scope("new.example", A) is True
    assert assess.in_scope("new.example", B) is False
    assert legacy.read_bytes() == before


@pytest.mark.parametrize("raw,reason", [
    (b"{not-json", "malformed JSON"),
    (b"\xff\xfe", "valid UTF-8"),
    (b"{}", "root is not an array"),
    (json.dumps([{"id": "only"}]).encode(), "keys do not match"),
    (json.dumps([_record() | {"extra": True}]).encode(), "keys do not match"),
    (json.dumps([_record() | {"created": True}]).encode(), "finite timestamp"),
    (json.dumps([_record() | {"created": 10**400}]).encode(),
     "finite timestamp"),
    (json.dumps([_record() | {"expires": float("nan")}]).encode(),
     "finite timestamp"),
    (json.dumps([_record() | {"targets": ["z.example", "a.example"]}]).encode(),
     "targets are not canonical"),
    (json.dumps([_record() | {"approved_by": 7}]).encode(), "approved_by"),
    (json.dumps([_record(), _record(target="other.example")]).encode(),
     "invalid or duplicated"),
    (b'[{"id":"one","id":"two"}]', "malformed JSON"),
    (b" " * (assess._MAX_AUTH_BYTES + 1), "size bound"),
], ids=[
    "malformed-json",
    "invalid-utf8",
    "non-array-root",
    "missing-fields",
    "unknown-field",
    "boolean-timestamp",
    "unrepresentable-timestamp",
    "non-finite-timestamp",
    "non-canonical-targets",
    "invalid-approver",
    "duplicate-authorization-id",
    "duplicate-json-key",
    "oversized-file",
])
def test_invalid_existing_authorization_is_never_no_grants(raw, reason):
    user = "corrupt-owner"
    path = _corrupt(user, raw)

    with pytest.raises(assess.AssessAuthorizationStateError, match=reason):
        assess.active_authorizations(user)
    with pytest.raises(assess.AssessAuthorizationStateError, match=reason):
        assess.in_scope("example.test", user)
    with pytest.raises(assess.AssessAuthorizationStateError, match=reason):
        assess.require_scope("example.test", user)

    assert path.read_bytes() == raw


def test_corrupt_authorization_blocks_target_io_before_probe(monkeypatch):
    user = "io-boundary-owner"
    raw = b'[{"targets":["example.test"]'
    path = _corrupt(user, raw)
    called = False

    def probe_must_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("target I/O boundary reached")

    monkeypatch.setattr(assess, "_probe", probe_must_not_run)

    with pytest.raises(assess.AssessAuthorizationStateError):
        assess.recon("example.test", user)

    assert called is False
    assert path.read_bytes() == raw


def test_explicit_selfassess_owner_refuses_before_crawl(monkeypatch):
    user = "explicit-selfassess-owner"
    raw = b'[{"targets":["127.0.0.1"]'
    path = _corrupt(user, raw)
    memory.set_user("different-ambient-owner")

    def crawl_must_not_run(*args, **kwargs):
        raise AssertionError("self-assessment crawl boundary reached")

    monkeypatch.setattr(selfassess, "_discover", crawl_must_not_run)

    with pytest.raises(assess.AssessAuthorizationStateError):
        selfassess.selfassess(
            "http://127.0.0.1:8000/", user=user)

    assert path.read_bytes() == raw
    assert assess.active_authorizations("different-ambient-owner") == []


def test_grant_and_revoke_refuse_to_overwrite_corrupt_evidence():
    user = "write-guard-owner"
    raw = b'[{"approved_by":"private operator evidence"'
    path = _corrupt(user, raw)

    with pytest.raises(assess.AssessAuthorizationStateError):
        assess.grant(["replacement.example"], user=user)
    assert path.read_bytes() == raw

    with pytest.raises(assess.AssessAuthorizationStateError):
        assess.revoke("anything", user=user)
    assert path.read_bytes() == raw


def test_signed_authorization_action_refuses_corrupt_owner_evidence():
    user = "action-spine-owner"
    raw = b'[{"approved_by":"signed private approval evidence"'
    path = _corrupt(user, raw)

    with pytest.raises(assess.AssessAuthorizationStateError):
        builtin_actions._authorize_assessment_execute({
            "targets": ["replacement.example"],
            "expires_in": 3600,
            "note": "replacement",
            "_user": user,
        })

    assert path.read_bytes() == raw


def test_status_is_non_sensitive_and_reports_legacy_quarantine():
    user = "status.owner"
    assess.grant(["private-target.example"], note="private note", user=user)

    status = assess.authorization_status(user)

    assert status == {
        "owner": user,
        "state": "valid",
        "reason": None,
        "active_count": 1,
        "legacy_quarantined": False,
        "legacy_file": None,
        "repair_command": None,
    }
    rendered = json.dumps(status)
    assert "private-target.example" not in rendered
    assert "private note" not in rendered


@pytest.mark.parametrize("size", [37, assess._MAX_AUTH_BYTES + 1],
                         ids=["malformed", "oversized"])
def test_repair_preserves_corrupt_bytes_before_reset(size):
    user = f"repair-owner-{size}"
    raw = (b"broken authorization evidence" * ((size // 29) + 1))[:size]
    path = _corrupt(user, raw)

    result = assess.repair_authorizations(user)

    digest = hashlib.sha256(raw).hexdigest()
    archive = path.with_name(
        "authorizations.corrupt."
        f"{digest[:assess._QUARANTINE_DIGEST_HEX]}.json")
    assert result["repaired"] is True
    assert result["quarantined_sha256"] == digest
    assert result["quarantine_file"] == archive.name
    assert archive.read_bytes() == raw
    assert assess.active_authorizations(user) == []
    assert path.read_text(encoding="utf-8").strip() == "[]"


def test_repair_refuses_evidence_above_quarantine_bound():
    user = "unquarantinable-owner"
    raw = b"x" * (assess._MAX_AUTH_QUARANTINE_BYTES + 1)
    path = _corrupt(user, raw)

    with pytest.raises(
            assess.AssessAuthorizationStateError,
            match="repair quarantine bound"):
        assess.repair_authorizations(user)

    assert path.stat().st_size == len(raw)
    assert path.read_bytes() == raw
    assert not list(path.parent.glob("authorizations.corrupt.*.json"))


def test_repair_does_not_rewrite_missing_valid_or_legacy_state():
    valid_user = "valid-owner"
    assess.grant(["valid.example"], user=valid_user)
    valid_path = assess._io(assess._auth_path(valid_user))
    valid_before = valid_path.read_bytes()

    legacy_user = "legacy.owner"
    legacy = assess._io(assess._legacy_auth_path(legacy_user))
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps([_record("legacy.example")]), encoding="utf-8")
    legacy_before = legacy.read_bytes()

    valid = assess.repair_authorizations(valid_user)
    missing = assess.repair_authorizations("missing-owner")
    legacy_only = assess.repair_authorizations(legacy_user)

    assert valid["repaired"] is False and valid["state"] == "valid"
    assert missing["repaired"] is False and missing["state"] == "missing"
    assert legacy_only["repaired"] is False
    assert legacy_only["legacy_quarantined"] is True
    assert valid_path.read_bytes() == valid_before
    assert legacy.read_bytes() == legacy_before


def test_interrupted_repair_preserves_live_and_quarantine_bytes(monkeypatch):
    user = "interrupted-repair"
    raw = b"broken private authorization evidence"
    path = _corrupt(user, raw)
    original_publish = atomicio.publish
    calls = 0

    def fail_reset(tmp, destination, data, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated reset failure")
        return original_publish(tmp, destination, data, **kwargs)

    monkeypatch.setattr(atomicio, "publish", fail_reset)

    with pytest.raises(OSError, match="simulated reset failure"):
        assess.repair_authorizations(user)

    digest = hashlib.sha256(raw).hexdigest()
    archive = path.with_name(
        "authorizations.corrupt."
        f"{digest[:assess._QUARANTINE_DIGEST_HEX]}.json")
    assert path.read_bytes() == raw
    assert archive.read_bytes() == raw


def test_updates_use_distinct_full_digest_process_locks(monkeypatch):
    names = []

    @contextlib.contextmanager
    def fake_lock(name, *args, **kwargs):
        names.append(name)
        yield

    monkeypatch.setattr(assess.proclock, "lock", fake_lock)

    assess.grant(["a.example"], user=A)
    assess.grant(["b.example"], user=B)

    assert len(names) == 2 and names[0] != names[1]
    assert all(name.startswith("assess-auth-") and len(name) == 76
               for name in names)
    assert names[0] == "assess-auth-" + hashlib.sha256(A.encode()).hexdigest()


def test_atomic_publish_failure_keeps_previous_authorizations(monkeypatch):
    user = "atomic-owner"
    assess.grant(["original.example"], user=user)
    path = assess._io(assess._auth_path(user))
    before = path.read_bytes()

    def fail_publish(*args, **kwargs):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(atomicio, "publish", fail_publish)

    with pytest.raises(OSError, match="simulated publish failure"):
        assess.grant(["replacement.example"], user=user)

    assert path.read_bytes() == before
    assert assess.in_scope("original.example", user) is True
    assert assess.in_scope("replacement.example", user) is False


def test_bounded_writer_never_publishes_an_unreadable_store(monkeypatch):
    user = "bounded-writer-owner"
    assess.grant(["original.example"], user=user)
    path = assess._io(assess._auth_path(user))
    before = path.read_bytes()
    monkeypatch.setattr(assess, "_MAX_AUTH_BYTES", len(before) + 32)

    with pytest.raises(
            assess.AssessAuthorizationStateError,
            match="serialized authorization state exceeds"):
        assess.grant(["replacement.example"], user=user)

    assert path.read_bytes() == before
    assert assess.in_scope("original.example", user) is True


def test_concurrent_grants_do_not_lose_updates():
    user = "concurrent-owner"
    targets = [f"target-{index}.example" for index in range(16)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda target: assess.grant([target], user=user), targets))

    auths = assess.active_authorizations(user)
    assert len(auths) == len(targets)
    assert {a["targets"][0] for a in auths} == set(targets)


def test_tool_and_cli_surface_unavailable_evidence_without_content(capsys):
    user = "cli-owner"
    raw = b'[{"approved_by":"private approver secret"'
    path = _corrupt(user, raw)

    memory.set_user(user)
    tool_output = tools._assess_scope()
    assert "assessment refused" in tool_output.lower()
    assert "evidence is unavailable" in tool_output.lower()
    assert "private approver secret" not in tool_output

    assert cli.main([
        "assess", "scope", "--owner", user, "--evidence"]) == 1
    status_output = capsys.readouterr().out
    assert '"state": "unavailable"' in status_output
    assert "private approver secret" not in status_output
    assert path.read_bytes() == raw

    assert cli.main([
        "assess", "scope", "--owner", user, "--repair"]) == 0
    repair_output = capsys.readouterr().out
    assert '"repaired": true' in repair_output.lower()
    assert assess.active_authorizations(user) == []


def test_selfassess_tool_refusal_is_sanitized(monkeypatch):
    user = "selfassess-tool-owner"
    raw = b'[{"approved_by":"private self-assess approval"'
    path = _corrupt(user, raw)
    memory.set_user(user)

    def network_must_not_run(*args, **kwargs):
        raise AssertionError("self-assessment network boundary reached")

    monkeypatch.setattr(assess, "_probe", network_must_not_run)

    output = tools._assess_selfassess("http://127.0.0.1:8000/")

    assert "assessment refused" in output.lower()
    assert "evidence is unavailable" in output.lower()
    assert "private self-assess approval" not in output
    assert path.read_bytes() == raw


def test_source_uses_safe_id_only_for_explicit_legacy_quarantine():
    source = __import__("inspect").getsource(assess)
    store_body = source.split("def _store_dir", 1)[1].split(
        "def _legacy_store_dir", 1)[0]
    assert "storage_key" in store_body
    assert "safe_id" not in store_body
