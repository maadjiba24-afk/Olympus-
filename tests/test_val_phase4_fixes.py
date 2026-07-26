"""Evidence for the Phase-4 defect fixes (Stage A D1/D2/D3, Stage B F1, Stage C D1/D2/D3).

Each test fails if its defect returns. Written against the fixed behaviour, not
the symptom, so they stay meaningful after refactors.
"""
from __future__ import annotations

import json

import pytest

from olympus import config, memory, orchestrator, trace as trace_mod, usage


# ── Stage A D1: replay must not report a false divergence ───────────────────

def test_post_pipeline_decisions_are_excluded_from_the_replay_diff():
    """`synthesis_check` is emitted by _compute_reply, AFTER _pipeline. Replay
    re-executes _pipeline only, so diffing it reported a divergence on every
    ordinary verified run — and replaygate turns a 'divergence' into a GitHub
    issue. Only what replay re-executes may be compared."""
    src = open(orchestrator.__file__, encoding="utf-8").read()
    assert "_POST_PIPELINE" in src, "the post-pipeline exclusion is gone"
    i_filter = src.find("_POST_PIPELINE = (")
    i_diff = src.find("diffs = trace_mod.diff_decisions(recorded")
    assert i_filter != -1 and i_diff != -1 and i_filter < i_diff

    recorded = [
        {"decision_type": "route", "agent": {"name": "zeus"}, "rationale": {}},
        {"decision_type": "synthesis_check", "agent": {"name": "zeus"},
         "rationale": {}},
    ]
    fresh = [recorded[0]]
    kept = [d for d in recorded if d["decision_type"] not in ("synthesis_check",)]
    assert trace_mod.diff_decisions(kept, fresh) == []
    # ...and the unfiltered comparison is what used to fail:
    assert trace_mod.diff_decisions(recorded, fresh) != []


# ── Stage A D3: a dropped slice always leaves durable evidence ──────────────

def test_compaction_failure_is_recorded_even_with_ctxbudget_off(monkeypatch):
    """The trace event alone was not a durable record: _maybe_compact runs from
    _finish where trace.current() is always None, and ask() flushes the trace
    first. A truncation must still leave evidence in the errors ledger."""
    from olympus import ctxbudget, errors

    assert not ctxbudget.enabled()          # shipped default
    src = open(orchestrator.__file__, encoding="utf-8").read()
    block = src[src.find("reason=\"compaction_failed\""):][:1400]
    # the unconditional capture must precede the flag-gated one
    i_uncond = block.find('errors.capture(\n                "orchestrator.compress_history"')
    i_flag = block.find("if ctxbudget.enabled():")
    assert i_uncond != -1, "no unconditional capture on the truncation path"
    assert i_uncond < i_flag, (
        "the truncation record is still gated behind OLYMPUS_CTX_BUDGET")
    assert callable(errors.capture)


# ── Stage A D2: a cancelled run's forensics say 'cancelled' ────────────────

def test_finish_closes_the_lease_with_its_actual_outcome():
    """Closing unconditionally with 'ok' re-wrote a cancelled run's forensics,
    so the preserved record contradicted the refusal the user was shown."""
    src = open(orchestrator.__file__, encoding="utf-8").read()
    i = src.find("def _finish(")
    block = src[i:i + 900]
    assert 'self._watch.close("ok")' not in block, (
        "_finish still hard-codes an 'ok' outcome")
    assert 'outcome = "cancelled" if' in block
    assert "self._watch.close(outcome)" in block


# ── Stage C D1: accounting never re-issues a billed call ───────────────────

def test_usage_record_captures_disk_failure_instead_of_raising(monkeypatch):
    """An ENOSPC on the ledger used to escape into openai_compat's retry
    handler, which treats OSError as a provider fault — 4 billed POSTs for one
    logical call. Accounting must never escape."""
    def full(_path, _obj):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(usage, "_atomic_write_json", full)
    usage.record("claude-opus-4-8", 100, 50)        # must not raise


def test_usage_record_distinguishes_a_wedged_lock_from_a_full_disk(monkeypatch):
    """The two need different operator actions, so they must not collapse into
    one message."""
    from olympus import errors, proclock

    def wedged(*_a, **_kw):
        raise TimeoutError("proclock 'usage-ledger' not acquired")

    monkeypatch.setattr(proclock, "lock", wedged)
    usage.record("claude-opus-4-8", 1, 1)
    log = config.MEMORY_DIR / "errors" / "errors.jsonl"
    assert log.exists(), "no error was captured"
    assert "ledger lock wedged" in log.read_text(encoding="utf-8")


# ── Stage C D2: a trace I/O failure never destroys a computed reply ────────

def test_trace_flush_captures_io_failure(monkeypatch):
    """flush() runs from ask()'s `finally:` — an OSError there replaced an
    already-computed answer with a traceback."""
    src = open(trace_mod.__file__, encoding="utf-8").read()
    i = src.find("def flush(")
    block = src[i:i + 2500]
    assert "except OSError as err:" in block, "flush still lets OSError escape"
    assert 'errors.capture("trace.flush"' in block


# ── Stage C D3: concurrent snapshot writers use distinct temp files ────────

def test_snapshot_temp_name_is_unique_per_writer():
    """Two threads of one process shared `.{name}.{pid}.tmp`: one write was
    silently clobbered and the loser raised FileNotFoundError into its reply."""
    src = open(memory.__file__, encoding="utf-8").read()
    i = src.find("def save_conversation(")
    block = src[i:i + 1200]
    assert "threading.get_ident()" in block, (
        "the snapshot temp path is not writer-unique")


# ── Stage B F1: a hostile scalar cannot crash the /v1 surface ──────────────

def test_infinite_max_tokens_is_refused_not_crashed():
    """json.loads accepts Infinity; int(float('inf')) raises OverflowError,
    which is not a ValueError — it escaped and dropped the connection."""
    from olympus import web

    for bad in (float("inf"), float("-inf")):
        with pytest.raises(web._AnthropicRefusal):
            web._anthropic_to_internal(
                {"model": "m", "max_tokens": bad,
                 "messages": [{"role": "user", "content": "hi"}]})


def test_json_infinity_literal_reaches_the_refusal_path():
    payload = json.loads('{"max_tokens": Infinity}')
    assert payload["max_tokens"] == float("inf")
    from olympus import web
    with pytest.raises(web._AnthropicRefusal):
        web._anthropic_to_internal(
            {"model": "m", "max_tokens": payload["max_tokens"],
             "messages": [{"role": "user", "content": "hi"}]})
