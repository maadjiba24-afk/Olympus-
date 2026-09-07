"""P2T: companion working-model evidence is exact-owner and fail-closed.

The model is injected into every answer as trusted collaboration context.  A
lossy owner key, malformed state interpreted as first use, or a torn RMW can
therefore leak or silently replace another principal's private instructions.
"""

from __future__ import annotations

import contextlib
import hashlib
import json

import pytest

from olympus import atomicio, cli, companion, gateway, memory, tui, usermem


A = "email-a.b@example.test"
B = "email-a-b-example-test"
LONG_A = "email-" + "x" * 80 + ".alpha"
LONG_B = "email-" + "x" * 80 + "-alpha"
PAIRS = ((A, B), (LONG_A, LONG_B))


def _state(model: str = "- private model", interactions: int = 1) -> dict:
    return {
        "interactions": interactions,
        "evolutions": 1,
        "model": model,
        "updated": 1.0,
    }


def _corrupt(user: str, raw: bytes = b'{"interactions":'):
    path = companion._path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


@pytest.mark.parametrize("first,second", PAIRS)
def test_exact_owner_paths_do_not_collapse_safe_id_collisions(first, second):
    assert memory.safe_id(first) == memory.safe_id(second)
    assert companion._path(first) != companion._path(second)

    companion.save(first, _state("- first private model"))
    companion.save(second, _state("- second private model"))

    assert companion.load(first)["model"] == "- first private model"
    assert companion.load(second)["model"] == "- second private model"
    assert "first private model" not in companion.model_block(second)


def test_legacy_lossy_state_is_preserved_and_claimed_by_nobody():
    assert memory.safe_id(A) == memory.safe_id(B)
    legacy = companion._legacy_path(A)
    legacy.write_text(json.dumps(_state("- legacy secret")), encoding="utf-8")
    before = legacy.read_bytes()

    for owner in (A, B, memory.safe_id(A)):
        assert companion.load(owner) == companion._default_state()
        assert "legacy secret" not in companion.model_block(owner)
        status = companion.state_status(owner)
        assert status["state"] == "missing"
        assert status["legacy_quarantined"] is True
        assert "legacy secret" not in json.dumps(status)

    companion.note_interaction(A)
    assert companion.load(A)["interactions"] == 1
    assert companion.load(B)["interactions"] == 0
    assert legacy.read_bytes() == before


@pytest.mark.parametrize("raw,reason", [
    (b"{not-json", "malformed JSON"),
    (b"\xff\xfe", "valid UTF-8"),
    (b"[]", "root is not an object"),
    (b'{"interactions":0}', "state keys"),
    (b'{"interactions":true,"evolutions":0,"model":"","updated":0}',
     "non-negative integer"),
    (b'{"interactions":0,"interactions":1,"evolutions":0,'
     b'"model":"","updated":0}', "malformed JSON"),
    (b'{"interactions":0,"evolutions":0,"model":"","updated":NaN}',
     "finite timestamp"),
    (b'{"interactions":0,"evolutions":0,"model":"","updated":1'
     + b"0" * 400 + b'}', "finite timestamp"),
    (b" " * (companion._MAX_STATE_BYTES + 1), "size bound"),
], ids=[
    "malformed-json",
    "invalid-utf8",
    "non-object-root",
    "schema-mismatch",
    "boolean-counter",
    "duplicate-key",
    "non-finite-timestamp",
    "unrepresentable-timestamp",
    "oversized-file",
])
def test_existing_invalid_state_is_never_first_use(raw, reason):
    user = "corrupt-owner"
    path = _corrupt(user, raw)

    with pytest.raises(companion.CompanionStateError, match=reason):
        companion.load(user)
    with pytest.raises(companion.CompanionStateError, match=reason):
        companion.model_block(user)
    with pytest.raises(companion.CompanionStateError, match=reason):
        companion.note_interaction(user)

    assert path.read_bytes() == raw


def test_save_refuses_to_overwrite_corrupt_current_evidence():
    user = "write-guard"
    raw = b'{"model":"private restriction"'
    path = _corrupt(user, raw)

    with pytest.raises(companion.CompanionStateError,
                       match="evidence is unavailable"):
        companion.save(user, _state("- replacement"))

    assert path.read_bytes() == raw


def test_state_status_exposes_no_model_content():
    user = "status-owner"
    companion.save(user, _state("- do not disclose this model"))

    status = companion.state_status(user)

    assert status == {
        "owner": user,
        "state": "valid",
        "reason": None,
        "model_present": True,
        "legacy_quarantined": False,
        "legacy_file": None,
        "repair_command": None,
    }
    assert "do not disclose" not in json.dumps(status)


def test_repair_preserves_corrupt_bytes_before_reset():
    user = "repair-owner"
    raw = b'{"model":"private restriction"'
    path = _corrupt(user, raw)

    result = companion.repair(user)

    digest = hashlib.sha256(raw).hexdigest()
    archive = path.with_name(
        f"companion.corrupt.{digest[:companion._QUARANTINE_DIGEST_HEX]}.json")
    assert result["repaired"] is True
    assert result["quarantined_sha256"] == digest
    assert result["quarantine_file"] == archive.name
    assert archive.read_bytes() == raw
    assert companion.load(user) == companion._default_state()


def test_repair_does_not_rewrite_missing_valid_or_legacy_state():
    valid_user = "valid-owner"
    companion.save(valid_user, _state("- keep me"))
    valid_path = companion._path(valid_user)
    valid_before = valid_path.read_bytes()

    legacy_user = "legacy.owner"
    legacy = companion._legacy_path(legacy_user)
    legacy.write_text(json.dumps(_state("- unattributed")), encoding="utf-8")
    legacy_before = legacy.read_bytes()

    valid = companion.repair(valid_user)
    missing = companion.repair("missing-owner")
    legacy_only = companion.repair(legacy_user)

    assert valid["repaired"] is False and valid["state"] == "valid"
    assert missing["repaired"] is False and missing["state"] == "missing"
    assert legacy_only["repaired"] is False
    assert legacy_only["legacy_quarantined"] is True
    assert valid_path.read_bytes() == valid_before
    assert legacy.read_bytes() == legacy_before


def test_interrupted_repair_keeps_live_and_quarantine_bytes(monkeypatch):
    user = "interrupted-repair"
    raw = b"broken companion evidence"
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
        companion.repair(user)

    digest = hashlib.sha256(raw).hexdigest()
    archive = path.with_name(
        f"companion.corrupt.{digest[:companion._QUARANTINE_DIGEST_HEX]}.json")
    assert path.read_bytes() == raw
    assert archive.read_bytes() == raw


def test_updates_use_distinct_full_digest_process_locks(monkeypatch):
    names = []

    @contextlib.contextmanager
    def fake_lock(name, *args, **kwargs):
        names.append(name)
        yield

    monkeypatch.setattr(companion.proclock, "lock", fake_lock)

    companion.note_interaction(A)
    companion.note_interaction(B)

    assert len(names) == 2 and names[0] != names[1]
    assert all(name.startswith("companion-") and len(name) == 74
               for name in names)
    assert names[0] == "companion-" + hashlib.sha256(A.encode()).hexdigest()


def test_atomic_publish_failure_does_not_replace_valid_state(monkeypatch):
    user = "atomic-owner"
    companion.save(user, _state("- original", interactions=4))
    path = companion._path(user)
    before = path.read_bytes()

    def fail_publish(*args, **kwargs):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(atomicio, "publish", fail_publish)

    with pytest.raises(OSError, match="simulated publish failure"):
        companion.note_interaction(user)

    assert path.read_bytes() == before


def test_corrupt_model_blocks_prompt_assembly_before_provider_call(monkeypatch):
    user = "prompt-owner"
    raw = b'{"model":"truncated"'
    path = _corrupt(user, raw)
    called = False

    def provider_must_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider boundary reached")

    from olympus import backend, orchestrator
    monkeypatch.setattr(backend, "complete_json", provider_must_not_run)

    bot = orchestrator.Olympus(user=user)
    with pytest.raises(companion.CompanionStateError):
        bot._route("local fixture request")

    assert called is False
    assert path.read_bytes() == raw


def test_gateway_growth_surfaces_unavailable_evidence_without_content():
    user = "gateway-owner"
    principal = gateway._resolved_principal(user, "ol")
    raw = b'{"model":"gateway private secret"'
    path = _corrupt(principal, raw)

    reply = "\n".join(gateway.reply_for({}, user, "/growth"))

    assert "evidence is unavailable" in reply.lower()
    assert "gateway private secret" not in reply
    assert path.read_bytes() == raw


def test_cli_exposes_status_and_explicit_repair(capsys):
    user = "cli-status-owner"
    raw = b'{"model":"cli private secret"'
    path = _corrupt(user, raw)

    assert cli.main(["growth", "--owner", user, "--evidence"]) == 1
    status_output = capsys.readouterr().out
    assert '"state": "unavailable"' in status_output
    assert "cli private secret" not in status_output
    assert path.read_bytes() == raw

    assert cli.main(["growth", "--owner", user, "--repair"]) == 0
    repair_output = capsys.readouterr().out
    assert '"repaired": true' in repair_output.lower()
    assert companion.load(user) == companion._default_state()


def test_terminal_growth_surfaces_unavailable_evidence_without_content():
    raw = b'{"model":"terminal private secret"'
    path = _corrupt("cli", raw)

    handled, output, should_exit = tui.dispatch_command(object(), "/growth")

    assert handled is True and should_exit is False
    assert "evidence is unavailable" in output.lower()
    assert "terminal private secret" not in output
    assert path.read_bytes() == raw


def test_memory_card_marks_companion_evidence_unavailable():
    user = "memory-card-owner"
    raw = b'{"model":"memory-card private secret"'
    path = _corrupt(user, raw)

    card = usermem.render_card(user)

    assert "how i've adapted to you" in card.lower()
    assert "evidence is unavailable" in card.lower()
    assert "memory-card private secret" not in card
    assert path.read_bytes() == raw


def test_source_never_keys_companion_state_on_safe_id():
    source = __import__("inspect").getsource(companion)
    path_body = source.split("def _path", 1)[1].split("def _legacy_path", 1)[0]
    assert "storage_key" in path_body
    assert "safe_id" not in path_body
