"""SPEC-03: production-real audit stack — unified run verification, key custody,
default-seed honesty, tamper-evidence, and API posture.

Recorded runs are produced by the REAL orchestrator pipeline against a mocked
Anthropic client (no network), reusing the freeze/replay machinery the existing
decision-log / replay tests use. Verification goes through the real CLI surface
(`olympus verify --run`) so we assert true process exit codes.
"""

import json
import threading
import types
import urllib.request
from http.server import ThreadingHTTPServer

import anthropic
import pytest

from olympus import cli, config, llm, recall, trace, witness, web


# A real (non-default) signing seed for the production cases.
REAL_SEED = "spec03-test-seed-please-do-not-use-in-prod-0123456789abcdef"


# --- a mocked council model (no network), faithful to the pipeline -----------
def _msg(text):
    return anthropic.types.Message.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _pipeline_client():
    def stream(**params):
        schema = ((params.get("output_config") or {}).get("format") or {}).get("schema") or {}
        props = schema.get("properties", {})

        class _S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                if "mode" in props:
                    return _msg(json.dumps(
                        {"mode": "delegate", "direct_reply": None,
                         "specialists": ["argus"], "brief": "research",
                         "needs_verification": False}))
                if "steps" in props:
                    return _msg(json.dumps(
                        {"steps": [{"id": "s1", "specialist": "argus",
                                    "task": "research", "depends_on": []}]}))
                if "verdict" in props:
                    return _msg(json.dumps(
                        {"verdict": "approve", "feedback": "",
                         "retry_specialists": []}))
                return _msg("findings")
        return _S()
    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


def _record_run(monkeypatch):
    """Run the real pipeline once (mocked model) and return the recorded run id.
    The decision log is signed on flush with whatever seed is configured."""
    monkeypatch.setattr(recall, "context_block", lambda user, msg: "")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(llm, "client", lambda settings=None: _pipeline_client())
    from olympus import orchestrator
    bot = orchestrator.Olympus(pool=config.ModelPool.from_env())
    bot.ask("research the current state of X")
    rid = bot.last_run_id
    # After recording, replay must never touch the network.
    monkeypatch.setattr(llm, "client", lambda settings=None: (_ for _ in ()).throw(
        AssertionError("verification/replay must not call the model API")))
    return rid


def _verify(argv):
    """Invoke the real CLI; return (exit_code:int, stdout:str)."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return (rc or 0), buf.getvalue()


# --- 1. production PASS -------------------------------------------------------
@pytest.mark.requires_crypto
def test_production_run_verifies_and_exits_zero(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    monkeypatch.setenv("OLYMPUS_PINNED_PUBKEY", witness.public_key_hex())
    rid = _record_run(monkeypatch)

    rc, out = _verify(["verify", "--run", rid])
    assert rc == 0, out
    assert "replay    : PASS" in out
    assert "signature : PASS" in out
    assert "RESULT    : PASS" in out
    assert "DEV / UNVERIFIED" not in out

    rc2, out2 = _verify(["verify", "--run", rid, "--require-production"])
    assert rc2 == 0, out2
    assert "RESULT    : PASS" in out2


# --- 2. default-seed labeling + --require-production teeth --------------------
@pytest.mark.requires_crypto
def test_default_seed_is_labeled_and_require_production_fails(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    monkeypatch.delenv("OLYMPUS_PINNED_PUBKEY", raising=False)
    rid = _record_run(monkeypatch)

    rc, out = _verify(["verify", "--run", rid])
    assert rc == 0, out
    assert "RESULT    : PASS" in out
    assert "DEV / UNVERIFIED" in out          # loud honesty label

    rc2, out2 = _verify(["verify", "--run", rid, "--require-production"])
    assert rc2 == 1
    assert "FAIL" in out2 and "default" in out2.lower()


@pytest.mark.requires_crypto
def test_default_seed_log_path_also_labeled(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    rid = _record_run(monkeypatch)
    rc, out = _verify(["verify", "--log", rid])
    assert rc == 0 and "DEV / UNVERIFIED" in out
    rc2, _ = _verify(["verify", "--log", rid, "--require-production"])
    assert rc2 == 1


# --- 3. tamper a decision record ---------------------------------------------
def _trace_file():
    base = config.MEMORY_DIR / "traces"
    return sorted(base.glob("*.jsonl"))[0]


def _rewrite_run(run_id, mutate):
    """Apply `mutate(run_dict)` to the stored run line and write it back."""
    path = _trace_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        rec = json.loads(line)
        if rec.get("id") == run_id:
            mutate(rec)
            out.append(json.dumps(rec))
        else:
            out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


@pytest.mark.requires_crypto
def test_tampered_decision_record_fails(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    monkeypatch.setenv("OLYMPUS_PINNED_PUBKEY", witness.public_key_hex())
    rid = _record_run(monkeypatch)

    def mutate(rec):
        rec["decisions"][0]["rationale"] = {"tampered": True}
    _rewrite_run(rid, mutate)

    rc, out = _verify(["verify", "--run", rid])
    assert rc == 1, out
    assert "FAIL" in out
    # A tampered decision breaks the signature (and/or replay) — named, not silent.
    assert ("signature : FAIL" in out) or ("replay    : FAIL" in out)


# --- 4. tamper a frozen response ---------------------------------------------
@pytest.mark.requires_crypto
def test_tampered_frozen_response_fails(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    monkeypatch.setenv("OLYMPUS_PINNED_PUBKEY", witness.public_key_hex())
    rid = _record_run(monkeypatch)

    responses = sorted((config.MEMORY_DIR / "responses").glob("*.json"))
    assert responses, "expected frozen responses on disk"
    # Mutate a CONSUMED response's text — replay must diverge from the recording.
    # Tamper a response the signed decision log actually references (the first
    # orchestration decision — routing), NOT an arbitrary file by name-sort:
    # a recorded run also freezes responses replay never re-reads (a fail-open
    # quality review, a speculative call), and tampering one of those correctly
    # does NOT diverge. Targeting a logged decision's response keeps this test
    # deterministic regardless of response-hash ordering (which shifts whenever a
    # specialist's tool loadout changes).
    run = trace.load_run(rid) or {}
    consumed = [d.get("model_response_ref") for d in run.get("decisions", [])
                if d.get("model_response_ref")]
    assert consumed, "expected the decision log to reference frozen responses"
    victim = config.MEMORY_DIR / "responses" / f"{consumed[0]}.json"
    assert victim.exists(), f"referenced response {consumed[0]} not frozen on disk"
    obj = json.loads(victim.read_text(encoding="utf-8"))
    obj["content"] = [{"type": "text", "text": "TAMPERED-RESPONSE"}]
    victim.write_text(json.dumps(obj), encoding="utf-8")

    rc, out = _verify(["verify", "--run", rid])
    assert rc == 1, out
    assert "replay    : FAIL" in out


# --- 5. wrong key (signature under a key other than the pinned one) ----------
@pytest.mark.requires_crypto
def test_wrong_key_is_rejected(monkeypatch):
    # Record signed by seed A.
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    rid = _record_run(monkeypatch)

    # Verify under a DIFFERENT seed B, pinning B's key.
    other_seed = REAL_SEED + "-DIFFERENT"
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", other_seed)
    monkeypatch.setenv("OLYMPUS_PINNED_PUBKEY", witness.public_key_hex())

    rc, out = _verify(["verify", "--run", rid])
    assert rc == 1, out
    assert "signature : FAIL" in out
    # Rejected as untrusted/altered — not merely reported "internally consistent".
    assert "PASS — replay-identical and signed by the trusted production" not in out


# --- 6. API posture ----------------------------------------------------------
@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(web, "_SESSIONS", {}, raising=False)
    monkeypatch.setattr(web, "_HITS", {}, raising=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _status(base):
    with urllib.request.urlopen(base + "/api/status?session=posture") as r:
        return json.loads(r.read())


@pytest.mark.requires_crypto
def test_api_posture_dev(server, monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    monkeypatch.delenv("OLYMPUS_PINNED_PUBKEY", raising=False)
    # Neutralize the repo's committed witness_pubkey.txt so we exercise the
    # unpinned branch cleanly (otherwise `pinned` reflects the shipped key).
    monkeypatch.setattr(witness, "pinned_pubkey", lambda: None)
    sig = _status(server)["signing"]
    assert sig["posture"] == "dev"
    assert sig["pinned"] is False
    assert sig["public_key"]                       # the public verifying key
    assert "verify --run" in sig["verify_hint"]


@pytest.mark.requires_crypto
def test_api_posture_production(server, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    monkeypatch.setenv("OLYMPUS_PINNED_PUBKEY", witness.public_key_hex())
    sig = _status(server)["signing"]
    assert sig["posture"] == "production"
    assert sig["pinned"] is True
    assert sig["public_key"] == witness.public_key_hex()


# --- 7. custody surface ------------------------------------------------------
@pytest.mark.requires_crypto
def test_pubkey_command_prints_key(monkeypatch, capsys):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    rc = cli.main(["pubkey"])
    out = capsys.readouterr().out.strip()
    assert (rc or 0) == 0
    assert out == witness.public_key_hex()       # bare key on stdout (pipeable)
