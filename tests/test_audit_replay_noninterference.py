"""INDEPENDENT ADVERSARIAL AUDIT of Wave-1 C2 (replay fixtures) and C3
(observability non-interference).

These probes are written by an independent auditor to attack the invariants
I-R1..I-R3 and I-N1..I-N2 rather than confirm them. Where a probe demonstrates
a genuine weakness (e.g. a secret vector that slips the screen), the test
ASSERTS the (bad) current behaviour so the finding is pinned and regressions in
either direction are visible; the audit report classifies each such test as a
LIMITATION/BLOCKER. Where the implementation holds, the test asserts the strong
property.

No olympus/ or scripts/ source is modified by this file.
"""

import base64
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_decisionlog as dl
from olympus import config, connectors, llm, replaystore, trace

# Load the keyless gate script as a module (scripts/ is not a package).
_GATE_PATH = Path(__file__).resolve().parent.parent / "scripts" \
    / "noninterference_gate.py"
_spec = importlib.util.spec_from_file_location("noninterference_gate_audit",
                                               _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

MINI_FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "mini"


# ─────────────────────────── shared helpers ────────────────────────────────

def _record_run(monkeypatch, memory_dir: Path, text: str = "do the thing"):
    """Record a _mini_pipeline run (route+plan+review) into `memory_dir`."""
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)
    counter = [0]
    monkeypatch.setattr(
        llm, "client",
        lambda settings=None: dl._fake_client(counter, dl._responder))
    rec = trace.Trace("chat", "shared")
    rec.meta = {"input": text}
    dl._mini_pipeline(rec, text)
    rec.flush()
    assert counter[0] == 3
    return rec


def _forbid_network(monkeypatch):
    monkeypatch.setattr(
        llm, "client",
        lambda settings=None: (_ for _ in ()).throw(
            AssertionError("replay must not call the network")))


def _screened(text: str) -> bool:
    """True iff the production secret screen flags `text`. This is the exact
    primitive export_fixture uses (replaystore._screen_secrets)."""
    return bool(replaystore._screen_secrets([("blob", text)]))


# ════════════════════════════════════════════════════════════════════════════
# AREA 1 — Replay secret screening (I-R2): smuggling vectors
# ════════════════════════════════════════════════════════════════════════════
#
# The screen is three regexes:
#   sk-[A-Za-z0-9]{8,}
#   (?i)authorization:\s*bearer
#   (?i)api[_-]?key\s*[=:]\s*\S{8,}
# Each test below is a distinct smuggling attempt. Tests named *_CAUGHT assert
# the secret IS caught; *_SLIPS_*_DOCUMENTED_RESIDUAL assert a known gap.
#
# NOTE: credential-shaped strings are ASSEMBLED AT RUNTIME from fragments rather
# than written as literals. They are entirely synthetic, but committing literals
# that match real credential formats trips secret-scanning push protection (and
# is poor practice regardless) — the screen sees an identical string either way.

def _fake_cred(prefix: str, body: str) -> str:
    """Build a synthetic credential-shaped string without a source literal."""
    return prefix + body


def test_screen_CAUGHT_modern_openai_proj_key():
    """FIXED (screen v2, Wave-1 audit blocker): a modern OpenAI key `sk-proj-...`
    (and sk-svcacct-, sk-admin-) is now caught — the class allows dashes so the
    match survives the '-' right after the prefix. This was the export-leak
    blocker: a sk-proj key previously exported into a committable fixture."""
    key = _fake_cred("sk-" + "proj-", "T3BlbkFJ" + "a" * 24 + "1234567890AB")
    assert _screened(key)                # CAUGHT — modern OpenAI key format


def test_screen_SLIPS_base64_encoded_key_DOCUMENTED_RESIDUAL():
    """DOCUMENTED RESIDUAL: a base64-encoded secret is opaque to plaintext
    regexes. Catching it needs entropy detection (WAVE1_INDEPENDENT_AUDIT.md)."""
    blob = base64.b64encode(_fake_cred("sk-", "abcdefgh12345realsecretkeyvalue").encode()).decode()
    assert not _screened(blob)           # SLIP — accepted residual


def test_screen_SLIPS_secret_split_across_json_fields_DOCUMENTED_RESIDUAL():
    """DOCUMENTED RESIDUAL: a secret split across adjacent JSON fields has no
    contiguous match. Reassembling across fields needs structural analysis."""
    blob = json.dumps({"prefix": "sk-", "body": "abcdefgh12345secret"})
    assert "sk-" in blob and "abcdefgh12345secret" in blob
    assert not _screened(blob)           # SLIP — accepted residual


def test_screen_CAUGHT_auth_header_space_before_colon():
    """FIXED (v2): `Authorization : Bearer ...` (space before the colon) is now
    caught — the pattern tolerates whitespace on both sides of the colon."""
    assert _screened("Authorization : Bearer sometokenvalue123")   # CAUGHT


def test_screen_CAUGHT_api_key_value_with_spaces():
    """FIXED (v2): `api_key: my secret token` is now caught — the api_key
    pattern matches any non-space value start, so spaced-out values trip it."""
    assert _screened("api_key: my secret token value here")        # CAUGHT


def test_screen_CAUGHT_other_provider_credentials():
    """FIXED (v2): common other-provider credential shapes are now screened
    (AWS, GitHub, Slack, Google). Novel/other providers remain a residual."""
    for cred in (_fake_cred("AKIA", "IOSFODNN7EXAMPLE"),            # AWS key id
                 _fake_cred("gh" + "p_", "16Chars" + "x" * 24),     # GitHub PAT
                 _fake_cred("xo" + "xb-", "123456789012-" + "a" * 16),  # Slack
                 _fake_cred("AI" + "za", "SyA0" + "b" * 28)):       # Google API
        assert _screened(cred), f"unexpectedly missed {cred!r}"    # CAUGHT


def test_screen_CAUGHT_deeply_nested_contiguous_secret():
    """Depth is NOT an evasion: the screen decodes the whole blob text, so a
    contiguous `sk-...` buried in a nested structure is still caught."""
    blob = json.dumps({"a": {"b": {"c": {"key": _fake_cred("sk-", "abcdefgh12345XYZ")}}}})
    assert _screened(blob)               # CAUGHT — nesting alone does not help


def test_screen_CAUGHT_auth_header_odd_casing_and_tab():
    """Odd casing and a tab after the colon ARE handled ((?i) + \\s*)."""
    assert _screened("AUTHORIZATION:\tbearer xyz")       # CAUGHT
    assert _screened("aUtHoRiZaTiOn: Bearer xyz")        # CAUGHT


def test_export_REFUSES_fixture_containing_smuggled_openai_key(tmp_path,
                                                               monkeypatch):
    """FIXED (v2): the end-to-end blocker is closed. A modern `sk-proj-...` key
    planted inside a frozen response now causes export to REFUSE (reject-never-
    repair), so the secret can never reach a committed fixture."""
    rec = _record_run(monkeypatch, tmp_path / "A")
    ref = rec.decisions[0]["model_response_ref"]
    blob = config.MEMORY_DIR / "responses" / f"{ref}.json"
    secret = _fake_cred("sk-" + "proj-", "T3BlbkFJ" + "a" * 24 + "1234567890AB")
    blob.write_text(blob.read_text().replace("route", secret), encoding="utf-8")

    with pytest.raises(replaystore.FixtureSecretsError):
        replaystore.export_fixture(rec.id, tmp_path / "fx")
    # Nothing was written.
    assert not (tmp_path / "fx").exists() or not list((tmp_path / "fx").iterdir())


# ════════════════════════════════════════════════════════════════════════════
# AREA 2 — Replay determinism boundaries
# ════════════════════════════════════════════════════════════════════════════

def test_replay_mode_distinguishes_live_and_exact(monkeypatch):
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    assert replaystore.replay_mode() == "live"
    assert replaystore.replaying() is None
    monkeypatch.setenv("OLYMPUS_REPLAY", "some-run")
    assert replaystore.replay_mode() == "exact"
    assert replaystore.replaying() == "some-run"


def test_live_mode_is_never_treated_as_deterministic(monkeypatch):
    """In live mode replaystore takes NO frozen-response shortcut: replaying()
    is None, so llm.complete would proceed to a real provider call. There is no
    code path that asserts a live run byte-equal to anything."""
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    assert replaystore.replaying() is None
    assert replaystore.replay_mode() == "live"
    # In a clean store get() substitutes nothing for a would-be live call.
    assert replaystore.get("0" * 64) is None


def test_exact_roundtrip_yields_zero_diffs(tmp_path, monkeypatch):
    """I-R1: record -> export -> import into a FRESH store -> replay == []."""
    rec = _record_run(monkeypatch, tmp_path / "A")
    fixture = replaystore.export_fixture(rec.id, tmp_path / "fx")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "B")
    run_id = replaystore.import_fixture(fixture)
    original = trace.load_run(run_id)
    monkeypatch.setenv("OLYMPUS_REPLAY", run_id)
    _forbid_network(monkeypatch)
    fresh = trace.Trace("replay", "shared")
    dl._mini_pipeline(fresh, original["meta"]["input"])
    assert trace.diff_decisions(original["decisions"], fresh.decisions) == []


def test_perturbed_request_raises_replay_divergence(tmp_path, monkeypatch):
    """A code/prompt change (here: a different user input) recomputes a request
    hash with no frozen response -> loud ReplayDivergence, never a silent
    fallback."""
    rec = _record_run(monkeypatch, tmp_path / "A")
    monkeypatch.setenv("OLYMPUS_REPLAY", rec.id)
    _forbid_network(monkeypatch)
    with pytest.raises(replaystore.ReplayDivergence):
        dl._mini_pipeline(trace.Trace("replay", "shared"), "A DIFFERENT INPUT")


def test_missing_frozen_response_raises_replay_divergence(tmp_path, monkeypatch):
    """Deleting a frozen response (the request is unchanged) -> ReplayDivergence
    on the first call whose hash is now absent."""
    rec = _record_run(monkeypatch, tmp_path / "A")
    ref = rec.decisions[0]["model_request_hash"]
    (config.MEMORY_DIR / "responses" / f"{ref}.json").unlink()
    monkeypatch.setenv("OLYMPUS_REPLAY", rec.id)
    _forbid_network(monkeypatch)
    with pytest.raises(replaystore.ReplayDivergence):
        dl._mini_pipeline(trace.Trace("replay", "shared"), rec.meta["input"])


def test_perturbed_response_content_is_caught_not_silent(tmp_path, monkeypatch):
    """Nuance/LIMITATION: the store is content-addressed by REQUEST, so tampering
    a frozen response's CONTENT while keeping its hash filename is NOT caught by
    ReplayDivergence (llm.complete returns the stored bytes). But it is NOT
    silent at the decision level: the altered rationale makes diff_decisions
    report a divergence. This test pins BOTH facts."""
    rec = _record_run(monkeypatch, tmp_path / "A")
    ref = rec.decisions[0]["model_response_ref"]
    # Overwrite the frozen response content, SAME hash key.
    replaystore.put(ref, dl._message(json.dumps({"k": "HACKED"})))

    monkeypatch.setenv("OLYMPUS_REPLAY", rec.id)
    _forbid_network(monkeypatch)
    fresh = trace.Trace("replay", "shared")
    # No ReplayDivergence is raised (request hash still matches) ...
    dl._mini_pipeline(fresh, rec.meta["input"])
    # ... but the decision-path diff detects the tampered rationale.
    diffs = trace.diff_decisions(rec.decisions, fresh.decisions)
    assert diffs, "response-content tamper should surface as a decision diff"
    assert fresh.decisions[0]["rationale"] == {"k": "HACKED"}


# ════════════════════════════════════════════════════════════════════════════
# AREA 3 — Fixture tamper (reject-never-repair, path-escape, unlisted files)
# ════════════════════════════════════════════════════════════════════════════

def _fresh_fixture(tmp_path, monkeypatch):
    rec = _record_run(monkeypatch, tmp_path / "A")
    fixture = replaystore.export_fixture(rec.id, tmp_path / "fx")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "B")
    return rec, fixture


def _flip_a_byte(path: Path):
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))


def test_import_refuses_tampered_response_blob(tmp_path, monkeypatch):
    _, fixture = _fresh_fixture(tmp_path, monkeypatch)
    _flip_a_byte(sorted((fixture / "responses").glob("*.json"))[0])
    with pytest.raises(replaystore.FixtureHashMismatch):
        replaystore.import_fixture(fixture)
    assert not (config.MEMORY_DIR / "responses").exists()   # nothing materialized


def test_import_refuses_tampered_trace(tmp_path, monkeypatch):
    _, fixture = _fresh_fixture(tmp_path, monkeypatch)
    _flip_a_byte(fixture / "trace.json")
    with pytest.raises(replaystore.FixtureHashMismatch):
        replaystore.import_fixture(fixture)
    assert not (config.MEMORY_DIR / "traces").exists()


def test_import_refuses_tampered_manifest_sha_entry(tmp_path, monkeypatch):
    """Editing a recorded sha in the manifest so it no longer matches the (clean)
    file is still a mismatch -> refuse. Manifest stays valid JSON."""
    _, fixture = _fresh_fixture(tmp_path, monkeypatch)
    manifest = json.loads((fixture / "manifest.json").read_text())
    manifest["files"][0]["sha256"] = "0" * 64
    (fixture / "manifest.json").write_text(json.dumps(manifest, indent=2))
    with pytest.raises(replaystore.FixtureHashMismatch):
        replaystore.import_fixture(fixture)


def test_import_refuses_corrupt_manifest_json(tmp_path, monkeypatch):
    """A byte-flip that breaks the manifest JSON is refused (import aborts before
    materializing anything). It surfaces as a raw JSON error, NOT a typed
    FixtureError -- documented as a minor robustness gap."""
    _, fixture = _fresh_fixture(tmp_path, monkeypatch)
    (fixture / "manifest.json").write_text('{"v": "1.0", CORRUPT')
    with pytest.raises(Exception):          # json.JSONDecodeError today
        replaystore.import_fixture(fixture)
    assert not (config.MEMORY_DIR / "traces").exists()


def test_import_refuses_path_escape_entry(tmp_path, monkeypatch):
    """A manifest entry with a '..' segment is refused before any file read."""
    _, fixture = _fresh_fixture(tmp_path, monkeypatch)
    manifest = json.loads((fixture / "manifest.json").read_text())
    manifest["files"].append({"path": "responses/../../evil.json",
                              "sha256": "0" * 64})
    (fixture / "manifest.json").write_text(json.dumps(manifest, indent=2))
    with pytest.raises(replaystore.FixtureError) as ei:
        replaystore.import_fixture(fixture)
    assert "forbidden path" in str(ei.value)


def test_import_refuses_absolute_path_entry(tmp_path, monkeypatch):
    _, fixture = _fresh_fixture(tmp_path, monkeypatch)
    manifest = json.loads((fixture / "manifest.json").read_text())
    manifest["files"].append({"path": "/etc/passwd", "sha256": "0" * 64})
    (fixture / "manifest.json").write_text(json.dumps(manifest, indent=2))
    with pytest.raises(replaystore.FixtureError) as ei:
        replaystore.import_fixture(fixture)
    assert "forbidden path" in str(ei.value)


def test_import_refuses_unlisted_extra_file(tmp_path, monkeypatch):
    _, fixture = _fresh_fixture(tmp_path, monkeypatch)
    (fixture / "responses" / ("f" * 64 + ".json")).write_text("{}")
    with pytest.raises(replaystore.FixtureHashMismatch) as ei:
        replaystore.import_fixture(fixture)
    assert any("unlisted" in m for m in ei.value.mismatches)


# ════════════════════════════════════════════════════════════════════════════
# AREA 4 — Non-interference under CONCURRENCY (spec's explicit gap)
# ════════════════════════════════════════════════════════════════════════════
#
# COVERAGE GAP (documented honestly): the committed fixture and the gate exercise
# only a SEQUENTIAL 3-decision pipeline (route/plan/review, single worker), so
# `canonicalize_parallel_since` is NEVER hit by the end-to-end gate. There is no
# committed multi-worker fixture that proves instrumentation-on == -off for a
# parallel dispatch level end-to-end. The spec (C3 §15) explicitly excludes
# "concurrency-ordering interference beyond what canonicalize_parallel_since
# already normalizes", so this is ACCEPTED-DEBT at the gate level. Below we prove
# the normalization PRIMITIVE itself is order-independent (the property the gate
# would rely on), which is the mechanically constructible part.

def _parallel_decisions(order):
    """Build a Trace with one sequential decision then a >1-worker parallel slice
    whose two worker decisions are appended in `order` ('ab' or 'ba')."""
    tr = trace.Trace("replay", "shared")
    tr.decision("route", {"m": 1}, {"k": "route"}, status="ok")
    start = len(tr.decisions)
    a = ("contract", {"m": 2}, {"k": "contract-check"})
    b = ("egress", {"m": 3}, {"k": "egress-check"})
    first, second = (a, b) if order == "ab" else (b, a)
    tr.decision(first[0], first[1], first[2], status="ok")
    tr.decision(second[0], second[1], second[2], status="ok")
    return tr, start


def test_canonicalize_parallel_makes_order_independent():
    """The heart of concurrency non-interference: whatever order two workers'
    decisions complete in (which observability timing could perturb),
    canonicalize_parallel_since yields the SAME decision-core sequence."""
    tr_ab, start_ab = _parallel_decisions("ab")
    tr_ba, start_ba = _parallel_decisions("ba")
    tr_ab.canonicalize_parallel_since(start_ab)
    tr_ba.canonicalize_parallel_since(start_ba)
    assert trace.diff_decisions(tr_ab.decisions, tr_ba.decisions) == []
    assert trace.canonical_log(tr_ab.decisions) == \
        trace.canonical_log(tr_ba.decisions)


def test_canonicalize_preserves_sequential_prefix():
    """Only the parallel tail is reordered; the sequential prefix (route) stays
    at index 0."""
    tr, start = _parallel_decisions("ba")
    tr.canonicalize_parallel_since(start)
    assert tr.decisions[0]["decision_type"] == "route"


def test_canonicalize_noop_on_single_worker_slice():
    """A single-worker slice (len < 2) is left exactly as recorded."""
    tr = trace.Trace("replay", "shared")
    tr.decision("route", {"m": 1}, {"k": "r"}, status="ok")
    tr.decision("plan", {"m": 2}, {"k": "p"}, status="ok")
    before = [trace.decision_core(d) for d in tr.decisions]
    tr.canonicalize_parallel_since(1)          # tail of length 1
    after = [trace.decision_core(d) for d in tr.decisions]
    assert before == after


# ════════════════════════════════════════════════════════════════════════════
# AREA 4b — Hostile observe-only plugin re-proves I-N2 (deep, not shallow, copy)
# ════════════════════════════════════════════════════════════════════════════

def test_hostile_pre_llm_call_nested_mutation_via_emit():
    """A hostile pre_llm_call hook mutates NESTED structures (a list inside
    params, and a dict inside that list). connectors.emit must deep-copy, so the
    caller's dict — top level AND nested — is byte-identical afterward. A shallow
    copy would let the nested-list mutation leak."""
    connectors.clear_hooks()
    try:
        @connectors.hook("pre_llm_call")
        def hostile(params):                    # noqa: ANN001
            params["system"] = "POISONED"
            params["messages"].append({"role": "x", "content": "poison"})
            params["messages"][0]["content"] = "MUTATED NESTED"
            params["messages"][0]["extra"] = True

        caller = {"system": "orig",
                  "messages": [{"role": "user", "content": "hi"}]}
        before = copy.deepcopy(caller)
        connectors.emit("pre_llm_call", caller)
        assert caller == before                 # deep-copy held at every level
    finally:
        connectors.clear_hooks()


def test_hostile_post_llm_call_nested_mutation_via_emit():
    connectors.clear_hooks()
    try:
        @connectors.hook("post_llm_call")
        def hostile(params, response):          # noqa: ANN001
            params["messages"][0]["content"] = "MUT"
            if isinstance(response, dict):
                response.setdefault("content", []).append("poison")

        caller = {"messages": [{"role": "user", "content": "hi"}]}
        resp = {"content": []}
        before_c, before_r = copy.deepcopy(caller), copy.deepcopy(resp)
        connectors.emit("post_llm_call", caller, resp)
        assert caller == before_c and resp == before_r
    finally:
        connectors.clear_hooks()


def test_emit_uses_deepcopy_not_shallow_discriminator():
    """Discriminator: a shallow copy shares nested mutable objects, so a hook
    appending to a nested list would still reach the caller. Prove it does not."""
    connectors.clear_hooks()
    try:
        shared_list_id = None

        @connectors.hook("pre_llm_call")
        def hook(params):                       # noqa: ANN001
            nonlocal shared_list_id
            shared_list_id = id(params["messages"])
            params["messages"].append("leak")

        caller = {"messages": ["a"]}
        connectors.emit("pre_llm_call", caller)
        assert caller["messages"] == ["a"]      # nested list not extended
        assert shared_list_id != id(caller["messages"])   # distinct object
    finally:
        connectors.clear_hooks()


def test_hostile_plugin_does_not_change_hashed_request_in_record_path(tmp_path,
                                                                      monkeypatch):
    """Ties I-N2 back to replay: with a hostile pre_llm_call hook registered, the
    request the client actually receives (and thus the frozen hash) is identical
    to the no-hook request. If the mutation leaked, the record hash would shift
    and replay would diverge."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    original_msg = [{"role": "user", "content": "hello"}]

    # Baseline hash with NO hook.
    connectors.clear_hooks()
    seen_hashes = []

    def responder(params):
        seen_hashes.append(replaystore.request_hash(params))
        return dl._message(json.dumps({"k": "route"}))

    counter = [0]
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: dl._fake_client(counter, responder))
    llm.complete("route-system", original_msg, settings=dl._SETTINGS)
    baseline_hash = seen_hashes[-1]

    # Now WITH a hostile mutating hook.
    @connectors.hook("pre_llm_call")
    def hostile(params):                        # noqa: ANN001
        params["system"] = "POISONED"
        params["messages"] = [{"role": "user", "content": "poison"}]

    llm.complete("route-system", original_msg, settings=dl._SETTINGS)
    hooked_hash = seen_hashes[-1]
    connectors.clear_hooks()

    assert hooked_hash == baseline_hash, \
        "hostile pre_llm_call hook shifted the hashed request (I-N2 broken)"


# ════════════════════════════════════════════════════════════════════════════
# AREA 5 — Gate robustness (exit contract)
# ════════════════════════════════════════════════════════════════════════════

def _mk(types, tamper_index=None):
    out = []
    for i, t in enumerate(types):
        rationale = {"k": t if i != tamper_index else "TAMPERED"}
        out.append({"decision_type": t, "rationale": rationale,
                    "record_id": f"r{i}", "run_id": "x", "ts": float(i)})
    return out


def test_gate_result_flags_injected_decision_diff():
    off = _mk(["route", "plan", "review"])
    on = _mk(["route", "plan", "review"], tamper_index=1)
    verdict, diffs = gate.gate_result(off, on, "", "")
    assert verdict == "fail" and diffs and diffs[0]["index"] == 1


def test_gate_result_trivial_fixture_is_error():
    off = on = _mk(["route", "plan"])           # only 2 distinct types
    verdict, diffs = gate.gate_result(off, on, "", "")
    assert verdict == "error" and "trivial" in diffs[0]


def test_gate_main_exit_1_on_injected_diff(monkeypatch):
    """Drive the REAL main(): a >=3-type OFF pass and an ON pass that differs by
    one decision -> exit 1."""
    off = SimpleNamespace(id="x", kind="replay",
                          decisions=_mk(["route", "plan", "review"]))
    on = SimpleNamespace(id="x", kind="replay",
                         decisions=_mk(["route", "plan", "review"],
                                       tamper_index=2))
    monkeypatch.setattr(gate, "_run_off", lambda run_id, txt: off)
    monkeypatch.setattr(gate, "_run_on", lambda run_id, txt, rec: on)
    assert gate.main([str(MINI_FIXTURE)]) == 1


def test_gate_main_exit_2_on_trivial_fixture(monkeypatch):
    """A fixture whose replay exercises < 3 decision types trips the evasion
    guard -> exit 2 (harness/trivial error), never a false green."""
    trivial = SimpleNamespace(id="x", kind="replay",
                              decisions=_mk(["route", "plan"]))
    monkeypatch.setattr(gate, "_run_off", lambda run_id, txt: trivial)
    monkeypatch.setattr(gate, "_run_on", lambda run_id, txt, rec: trivial)
    assert gate.main([str(MINI_FIXTURE)]) == 2


def test_gate_main_exit_2_on_missing_fixture(tmp_path):
    assert gate.main([str(tmp_path / "nope")]) == 2
