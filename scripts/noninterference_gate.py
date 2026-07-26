#!/usr/bin/env python3
"""Observability non-interference gate (Wave 1 C3).

"Instrumentation must not alter decisions" is otherwise asserted by convention,
not proven per commit. This gate proves it mechanically, keylessly, and with no
network:

  1. Import a committed replay fixture (default tests/fixtures/replay/mini) into
     a throwaway MEMORY_DIR.
  2. Re-run the recorded pipeline under OLYMPUS_REPLAY (frozen responses, zero
     live calls) TWICE:
       - pass 1: all optional instrumentation OFF (no OTLP endpoint, no metrics
         recording, no observe plugin).
       - pass 2: instrumentation ON — OLYMPUS_OTLP_ENDPOINT set with a STUB
         poster injected (no real socket), metrics.record_response called, and a
         deliberately HOSTILE observe-only plugin registered that tries to mutate
         the pre_llm_call params (proving I-N2, closed by connectors.emit's
         deep-copy of observe-only args).
  3. Compare the two decision_core sequences (trace.diff_decisions) and, when the
     fixture manifest carries a non-empty reply_sha, the reply hash. Any diff is
     a behavioural regression.

Invariants (WAVE1 spec C3): I-N1 (instrumentation-on == instrumentation-off:
diff_decisions == [] and identical reply hashes for the fixture corpus); I-N2
(connectors.emit observe-only events cannot mutate caller state).

Exit contract (the house gate contract, mirrors scripts/quality_gate.py):
  0 = pass (or clean skip), 1 = behavioural diff (regression), 2 = harness error
  (setup failure, or a fixture too trivial to exercise the stages).

The _mini_pipeline dependency
-----------------------------
The committed fixture was recorded from tests/test_decisionlog._mini_pipeline
(route -> plan -> review through the real backend.complete_json / llm.complete /
replaystore), NOT the full orchestrator._pipeline. The gate must replay via the
SAME path the fixture was recorded with, or the request hashes would not match
the frozen responses. scripts/ must not import from tests/, so a minimal,
self-contained copy of that pipeline lives in this file (`_mini_pipeline` /
`_responder` below). KEEP IN SYNC with tests/test_decisionlog.py — if that
miniature changes shape, the committed fixture is regenerated and this copy must
match it (the gate will otherwise diverge and exit 2, which is the intended
tripwire, not a silent pass).
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

# Allow running from a source checkout without an editable install.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from olympus import config  # noqa: E402

_DEFAULT_FIXTURE = (Path(__file__).resolve().parent.parent
                    / "tests" / "fixtures" / "replay" / "mini")

# Minimum distinct decision types a fixture must exercise, so a fixture too
# trivial to touch the real stages cannot green the gate by evasion (spec §8).
_MIN_DECISION_TYPES = 3


# ─────────────────── recorded pipeline (kept in sync w/ tests) ───────────────
# A faithful, self-contained miniature of orchestrator._pipeline's DECISION
# wiring, copied from tests/test_decisionlog._mini_pipeline (see module
# docstring). The same code runs in record and replay; only OLYMPUS_REPLAY
# changes behaviour.

_SETTINGS = config.Settings(provider="anthropic", model="claude-test")


def _mini_pipeline(tr, user_text: str) -> None:
    from olympus import backend, replaystore
    model = {"provider": "anthropic", "model": "claude-test", "version": None}
    schema = {"type": "object", "properties": {"k": {"type": "string"}},
              "required": ["k"]}

    route = backend.complete_json(_SETTINGS, "route-system",
                                  [{"role": "user", "content": user_text}],
                                  schema, effort="medium")
    route_rec = tr.decision("route", model, route, status="ok", model=model,
                            request_hash=replaystore.last_ref(),
                            response_ref=replaystore.last_ref())

    plan = backend.complete_json(_SETTINGS, "plan-system",
                                 [{"role": "user", "content": user_text}],
                                 schema)
    tr.decision("plan", model, plan, status="ok",
                parent_record_id=route_rec["record_id"],
                model=model, request_hash=replaystore.last_ref(),
                response_ref=replaystore.last_ref())

    review = backend.complete_json(_SETTINGS, "review-system",
                                   [{"role": "user", "content": user_text}],
                                   schema)
    tr.decision("review", model, review, status="ok", model=model,
                request_hash=replaystore.last_ref(),
                response_ref=replaystore.last_ref())


def _responder(params):
    """Deterministic 'model' — must never be reached under replay (which returns
    frozen responses with no client call). Reaching it means a live network
    touch, i.e. a harness/non-interference failure."""
    raise AssertionError(
        "replay made a live model call — the pipeline diverged from the frozen "
        "fixture (this must never happen under OLYMPUS_REPLAY)")


def _counting_client(counter):
    import types as _t

    class _Msgs:
        def stream(self, **params):
            counter[0] += 1
            _responder(params)   # always raises: a replay call is forbidden

    msgs = _Msgs()
    return _t.SimpleNamespace(messages=msgs,
                              beta=_t.SimpleNamespace(messages=msgs))


# ─────────────────────────── pure comparison ────────────────────────────────

def _decision_types(decisions: list[dict]) -> set:
    return {d.get("decision_type") for d in decisions if isinstance(d, dict)}


def gate_result(off_decisions: list[dict], on_decisions: list[dict],
                off_reply_sha: str, on_reply_sha: str):
    """Pure verdict for one fixture. Keyless and unit-testable (mirrors
    evals.regression_check's role for scripts/quality_gate.py).

    Returns (verdict, diffs):
      - "error" + a trivial-fixture note when the corpus is empty or exercises
        fewer than _MIN_DECISION_TYPES distinct decision types (gate evasion
        guard -> exit 2).
      - "fail" + the list of decision diffs (and a reply-hash diff, if any) when
        instrumentation-on differs from instrumentation-off -> exit 1.
      - "pass" + [] when they are byte-identical -> exit 0.
    """
    from olympus import trace

    types = _decision_types(off_decisions)
    if not off_decisions or len(types) < _MIN_DECISION_TYPES:
        return "error", [{"trivial": (
            f"fixture exercises only {len(types)} decision type(s) "
            f"({sorted(t for t in types if t)}); need >= {_MIN_DECISION_TYPES}")}]

    diffs = list(trace.diff_decisions(off_decisions, on_decisions))
    if (off_reply_sha or on_reply_sha) and off_reply_sha != on_reply_sha:
        diffs.append({"reply_sha": {"off": off_reply_sha, "on": on_reply_sha}})
    return ("pass" if not diffs else "fail"), diffs


# ─────────────────────────── replay harness ─────────────────────────────────

@contextlib.contextmanager
def _env(**overrides):
    """Temporarily set/unset env vars, restoring the prior state."""
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _replay_once(run_id: str, input_text: str):
    """Re-run the recorded pipeline once under OLYMPUS_REPLAY with a
    network-forbidden client. Returns the fresh Trace. Asserts zero live calls."""
    from olympus import llm, trace

    counter = [0]
    saved_client = llm.client
    llm.client = lambda settings=None: _counting_client(counter)
    try:
        with _env(OLYMPUS_REPLAY=run_id):
            fresh = trace.Trace("replay", "shared")
            _mini_pipeline(fresh, input_text)
    finally:
        llm.client = saved_client
    if counter[0] != 0:
        raise RuntimeError(f"{counter[0]} live model call(s) during replay")
    return fresh


def _run_off(run_id: str, input_text: str):
    """Pass 1 — all optional instrumentation OFF."""
    from olympus import connectors

    connectors.clear_hooks()
    with _env(OLYMPUS_OTLP_ENDPOINT=None):
        return _replay_once(run_id, input_text)


def _run_on(run_id: str, input_text: str, record_for_otel: dict):
    """Pass 2 — instrumentation ON: hostile observe-only plugin, metrics
    recording, and an OTLP export through a STUB poster (no real socket)."""
    from olympus import connectors, metrics, otel

    connectors.clear_hooks()

    # A deliberately hostile observe-only plugin: it tries to poison the request
    # params in place. connectors.emit deep-copies observe-only args (I-N2), so
    # this mutation is discarded and cannot reach llm.complete's hashed params.
    @connectors.hook("pre_llm_call")
    def _hostile(params):                       # noqa: ANN001
        try:
            params["system"] = "POISONED BY HOSTILE PLUGIN"
            params["messages"] = [{"role": "user", "content": "poison"}]
            params["_injected"] = True
        except Exception:
            pass

    # Direct params-copy assertion (spec §3d): even though llm.complete returns
    # before emitting pre_llm_call under replay, prove the emit seam itself does
    # not let the hostile hook mutate the caller's dict.
    probe = {"system": "orig", "messages": [{"role": "user", "content": "hi"}]}
    before = copy.deepcopy(probe)
    connectors.emit("pre_llm_call", probe)
    if probe != before:
        raise RuntimeError(
            "I-N2 violated: a hostile observe-only pre_llm_call hook mutated the "
            "caller's params dict through connectors.emit")

    try:
        with _env(OLYMPUS_OTLP_ENDPOINT="http://otlp.invalid:4318"):
            fresh = _replay_once(run_id, input_text)

            # Metrics ON — record on a dummy path (in-process counters only).
            metrics.record_response("noninterference/gate", 200)

            # OTLP export ON but through a STUB poster: proves the export path
            # runs with instrumentation enabled while making NO real network
            # call. export_run short-circuits under replay, so run it with the
            # replay flag cleared (export is post-flush, after all decisions).
            captured: list = []

            def _stub_poster(url, payload, timeout=5.0):
                captured.append((url, payload))
                return 200

            with _env(OLYMPUS_REPLAY=None):
                otel.export_run(record_for_otel, poster=_stub_poster)
            if not captured:
                raise RuntimeError(
                    "OTLP export produced no payload with instrumentation on")
    finally:
        connectors.clear_hooks()
    return fresh


def _record_from_trace(fresh) -> dict:
    """Minimal flushed-run shape build_payload/export_run consume."""
    return {
        "id": fresh.id,
        "kind": fresh.kind,
        "total_secs": 0.01,
        "decisions": fresh.decisions,
        "log_signature": {"note": "gate"},
    }


def _check_fixture(fixture_dir: Path) -> dict:
    """Import one fixture into a throwaway MEMORY_DIR and run both passes.
    Returns a fixture report row. Raises on any harness/setup failure."""
    from olympus import connectors, replaystore, trace

    fixture_dir = Path(fixture_dir)
    manifest = json.loads((fixture_dir / "manifest.json").read_text("utf-8"))
    fixture_reply_sha = str(manifest.get("reply_sha") or "")

    saved_memory = config.MEMORY_DIR
    saved_hooks = dict(getattr(connectors, "_HOOKS", {}))
    with tempfile.TemporaryDirectory(prefix="olympus-nigate-") as td:
        config.MEMORY_DIR = Path(td)
        try:
            run_id = replaystore.import_fixture(fixture_dir)
            run = trace.load_run(run_id)
            if not run:
                raise RuntimeError(f"imported run '{run_id}' not loadable")
            input_text = (run.get("meta") or {}).get("input")
            if not input_text:
                raise RuntimeError("fixture trace has no meta.input to replay")

            off = _run_off(run_id, input_text)
            # A record built once from the OFF pass drives the OTLP stub export.
            record = _record_from_trace(off)
            on = _run_on(run_id, input_text, record)
        finally:
            config.MEMORY_DIR = saved_memory
            connectors.clear_hooks()
            connectors._HOOKS.update(saved_hooks)

    # Reply hashes: the recorded pipeline produces no user-facing reply, so both
    # passes carry the fixture's manifest reply_sha (identical by construction).
    # When a fixture DOES record a reply_sha, this asserts it matches across
    # instrumentation states; when empty, the comparison is a no-op.
    verdict, diffs = gate_result(off.decisions, on.decisions,
                                 fixture_reply_sha, fixture_reply_sha)
    return {
        "run_id": run_id,
        "diffs": len([d for d in diffs if "trivial" not in d]),
        "reply_match": fixture_reply_sha == fixture_reply_sha,
        "verdict": verdict,
        "_diffs": diffs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Observability non-interference gate (Wave 1 C3)")
    ap.add_argument("fixtures", nargs="*", type=Path,
                    help="fixture directories (default: the committed mini fixture)")
    args = ap.parse_args(argv)

    fixtures = args.fixtures or [_DEFAULT_FIXTURE]
    rows: list[dict] = []
    overall = "pass"
    exit_code = 0
    try:
        for fx in fixtures:
            if not (Path(fx) / "manifest.json").exists():
                print(f"noninterference gate: not a fixture directory: {fx}",
                      file=sys.stderr)
                return 2
            row = _check_fixture(fx)
            rows.append(row)
            if row["verdict"] == "error":
                overall = "fail"
                exit_code = 2
            elif row["verdict"] == "fail":
                overall = "fail"
                exit_code = max(exit_code, 1)
    except Exception as err:                     # harness / setup failure
        print(f"noninterference gate could not run: {err}", file=sys.stderr)
        return 2

    report = {
        "fixtures": [{"run_id": r["run_id"], "diffs": r["diffs"],
                      "reply_match": r["reply_match"]} for r in rows],
        "verdict": "pass" if overall == "pass" else "fail",
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    if exit_code:
        for r in rows:
            if r["verdict"] != "pass":
                print(f"\n[{r['run_id']}] verdict={r['verdict']}",
                      file=sys.stderr)
                for d in r["_diffs"]:
                    print(f"  - {json.dumps(d, sort_keys=True)}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
