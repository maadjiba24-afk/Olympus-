"""Calibration Record — the observation-only reliability evidence store.

Deterministic: no model calls, no network, no paid API. Every test drives the
public API against a tmp MEMORY_DIR.
"""

import json

import pytest

from olympus import calibration


@pytest.fixture(autouse=True)
def _on(monkeypatch, tmp_path):
    """Collection ON, storage isolated per test."""
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    monkeypatch.setattr(calibration.config, "MEMORY_DIR", tmp_path)
    yield


def _obs(run_id="r1", **kw):
    kw.setdefault("provider", "anthropic")
    kw.setdefault("model", "claude-sonnet-5")
    kw.setdefault("domain", "code")
    kw.setdefault("result", "ok")
    return calibration.record_observation(run_id, **kw)


# --- normal recording ----------------------------------------------------

def test_records_an_observation():
    e = _obs("run-1", latency_ms=120.5, tokens_in=10, tokens_out=20,
             cost_usd=0.002, specialist="hephaestus")
    assert e is not None
    assert e["kind"] == calibration.OBSERVATION
    assert e["seq"] == 0 and e["prev"] is None
    b = e["body"]
    assert b["run_id"] == "run-1"
    assert b["provider"] == "anthropic" and b["model"] == "claude-sonnet-5"
    assert b["model_key"] == "anthropic/claude-sonnet-5"   # never collapsed
    assert b["latency_ms"] == 120.5 and b["cost_usd"] == 0.002


def test_chain_links_entries():
    a = _obs("run-1")
    b = _obs("run-2")
    assert b["seq"] == 1
    assert b["prev"] == a["entry_hash"]        # hash chain
    assert calibration.verify()["ok"] is True


def test_provider_and_model_stay_explicit():
    _obs("r1", provider="openai", model="gpt-5")
    _obs("r2", provider="anthropic", model="claude-sonnet-5")
    keys = {e["body"]["model_key"] for e in calibration.entries()}
    assert keys == {"openai/gpt-5", "anthropic/claude-sonnet-5"}


# --- idempotency ---------------------------------------------------------

def test_duplicate_ingestion_is_idempotent():
    first = _obs("run-dup", tool="write_file")
    again = _obs("run-dup", tool="write_file")
    assert first is not None
    assert again is None                        # no-op, not a second entry
    assert len(calibration.entries(calibration.OBSERVATION)) == 1


def test_distinct_runs_are_not_deduped():
    _obs("run-a")
    _obs("run-b")
    assert len(calibration.entries(calibration.OBSERVATION)) == 2


def test_duplicate_feedback_is_idempotent():
    _obs("run-1")
    a = calibration.record_feedback("run-1", calibration.APPROVED)
    b = calibration.record_feedback("run-1", calibration.APPROVED)
    assert a is not None and b is None
    # a genuinely different verdict is a distinct event
    c = calibration.record_feedback("run-1", calibration.REJECTED)
    assert c is not None


# --- partial / invalid ---------------------------------------------------

def test_partial_record_is_sparse_not_corrupt():
    e = calibration.record_observation("run-min")       # only the required field
    assert e is not None
    assert e["body"]["run_id"] == "run-min"
    assert "latency_ms" not in e["body"]                # absent, not null
    assert "cost_usd" not in e["body"]
    assert calibration.verify()["ok"] is True


def test_zero_and_false_are_preserved():
    e = _obs("run-z", latency_ms=0, tokens_in=0, cost_usd=0.0)
    assert e["body"]["latency_ms"] == 0                 # 0 is evidence, not absence
    assert e["body"]["tokens_in"] == 0


def test_invalid_schema_inputs_raise():
    with pytest.raises(calibration.CalibrationError):
        calibration.record_observation("")               # missing run_id
    with pytest.raises(calibration.CalibrationError):
        calibration.record_observation("r", result="exploded")   # unknown result
    with pytest.raises(calibration.CalibrationError):
        calibration.record_feedback("r", "loved-it")     # unknown outcome
    with pytest.raises(calibration.CalibrationError):
        calibration._append("not-a-kind", {}, "k")


def test_malformed_lines_are_skipped_not_fatal():
    _obs("run-good")
    with open(calibration.path(), "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write(json.dumps({"schema": "someone-elses/1", "x": 1}) + "\n")
    assert len(calibration.entries(calibration.OBSERVATION)) == 1


# --- schema migration ----------------------------------------------------

def test_older_schema_entry_still_readable():
    _obs("run-new")
    with open(calibration.path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"schema": "olympus-calibration/0",
                             "seq": 99, "kind": "observation",
                             "body": {"run_id": "ancient"}}) + "\n")
    got = calibration.entries(calibration.OBSERVATION)
    assert {e["body"]["run_id"] for e in got} == {"run-new", "ancient"}
    migrated = [e for e in got if e["body"]["run_id"] == "ancient"][0]
    assert migrated["schema"] == calibration.SCHEMA
    assert migrated["_migrated_from"] == "olympus-calibration/0"


def test_migration_does_not_rewrite_disk():
    _obs("run-1")
    before = calibration.path().read_text(encoding="utf-8")
    calibration.entries()
    assert calibration.path().read_text(encoding="utf-8") == before


# --- tamper detection ----------------------------------------------------

def test_tamper_detected_on_edited_entry():
    _obs("run-1", cost_usd=0.001)
    _obs("run-2")
    lines = calibration.path().read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["body"]["cost_usd"] = 99.0                        # forge the cost
    lines[0] = json.dumps(rec, sort_keys=True)
    calibration.path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    v = calibration.verify()
    assert v["ok"] is False
    assert any("content hash mismatch" in p for p in v["problems"])


def test_tamper_detected_on_deleted_entry():
    _obs("run-1"); _obs("run-2"); _obs("run-3")
    lines = calibration.path().read_text(encoding="utf-8").splitlines()
    del lines[1]                                          # excise the middle
    calibration.path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    v = calibration.verify()
    assert v["ok"] is False
    assert any("broken chain" in p for p in v["problems"])


def test_verify_reports_signing_status_honestly():
    _obs("run-1")
    v = calibration.verify()
    assert v["entries"] == 1
    assert v["signed"] + v["unsigned"] == 1     # never silently claims signed


# --- disabled collection -------------------------------------------------

def test_disabled_collection_writes_nothing(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "0")
    assert calibration.enabled() is False
    assert _obs("run-1") is None
    assert calibration.record_feedback("run-1", calibration.APPROVED) is None
    assert calibration.record_comparison("cmp-1", chosen_model="x") is None
    assert not calibration.path().exists()      # zero writes, zero side effects
    assert calibration.entries() == []


def test_disabled_report_is_inert(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "0")
    assert "OFF" in calibration.render_report()


def test_default_is_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_CALIBRATION", raising=False)
    assert calibration.enabled() is False        # opt-in, never opt-out


# --- redaction -----------------------------------------------------------

def test_prompt_text_is_never_stored():
    secret = "wire $40,000 to account 12345 for Project Aurora"
    _obs("run-1", task=secret)
    raw = calibration.path().read_text(encoding="utf-8")
    assert secret not in raw
    assert "Aurora" not in raw and "12345" not in raw
    assert calibration.entries()[0]["body"]["task_hash"].startswith("sha256:")


def test_feedback_note_is_hashed():
    _obs("run-1")
    calibration.record_feedback("run-1", calibration.REJECTED,
                                note="client hates the Acme pricing")
    raw = calibration.path().read_text(encoding="utf-8")
    assert "Acme" not in raw and "hates" not in raw


def test_text_ref_is_stable_and_empty_stays_empty():
    assert calibration.text_ref("abc") == calibration.text_ref("abc")
    assert calibration.text_ref("abc") != calibration.text_ref("abd")
    assert calibration.text_ref("") == ""        # missing stays missing
    assert calibration.text_ref(None) == ""


def test_config_id_separates_endpoints():
    a = calibration.config_id("openai", "gpt-5", "https://a.example")
    b = calibration.config_id("openai", "gpt-5", "https://b.example")
    assert a != b                                # same model, different endpoint


# --- feedback appends, never rewrites ------------------------------------

def test_feedback_does_not_rewrite_the_observation():
    obs = _obs("run-1")
    before = calibration.path().read_text(encoding="utf-8")
    calibration.record_feedback("run-1", calibration.APPROVED_AFTER_EDIT)
    after = calibration.path().read_text(encoding="utf-8")
    assert after.startswith(before)              # strictly appended
    assert calibration.entries()[0]["entry_hash"] == obs["entry_hash"]
    assert calibration.verify()["ok"] is True


# --- blind comparison ----------------------------------------------------

def test_blind_comparison_links_to_runs():
    _obs("run-a", model="claude-sonnet-5")
    _obs("run-b", provider="openai", model="gpt-5")
    e = calibration.record_comparison(
        "cmp-1", chosen_model="anthropic/claude-sonnet-5",
        models=["anthropic/claude-sonnet-5", "openai/gpt-5"],
        run_ids=["run-a", "run-b"])
    assert e["body"]["compare_id"] == "cmp-1"
    assert e["body"]["run_ids"] == ["run-a", "run-b"]
    assert e["body"]["blind"] is True


def test_comparison_win_rate_needs_minimum_samples():
    for i in range(3):
        calibration.record_comparison(f"cmp-{i}", chosen_model="openai/gpt-5")
    cs = calibration.report()["comparison_summary"]
    assert cs["insufficient_evidence"] is True
    assert cs["win_rate"] == {}                  # refuses to publish a rate
    for i in range(3, 6):
        calibration.record_comparison(f"cmp-{i}", chosen_model="openai/gpt-5")
    cs = calibration.report()["comparison_summary"]
    assert cs["insufficient_evidence"] is False
    assert cs["win_rate"]["openai/gpt-5"] == 1.0


# --- insufficient sample handling ----------------------------------------

def test_thin_cell_reports_insufficient_and_omits_rates():
    for i in range(3):
        _obs(f"run-{i}")
    cell = calibration.report()["cells"][0]
    assert cell["insufficient_evidence"] is True
    assert "success_rate" not in cell            # no manufactured number
    assert "success_ci95" not in cell
    assert "INSUFFICIENT EVIDENCE" in calibration.render_report()


def test_rank_refuses_on_thin_evidence():
    for i in range(3):
        _obs(f"run-{i}", provider="openai", model="gpt-5")
    r = calibration.rank_models()
    assert r["ranked"] is False
    assert "insufficient evidence" in r["reason"]


def test_rank_reports_overlap_rather_than_false_precision():
    for i in range(6):
        _obs(f"a{i}", provider="openai", model="gpt-5", result="ok")
    for i in range(6):
        _obs(f"b{i}", provider="anthropic", model="claude-sonnet-5",
             result="ok" if i < 5 else "error")
    r = calibration.rank_models(domain="code")
    assert r["ranked"] is True
    assert r["separated"] is False               # CIs overlap at n=6
    assert "overlap" in r["note"]


# --- analytics correctness -----------------------------------------------

def test_success_rate_and_interval_are_correct():
    for i in range(10):
        _obs(f"run-{i}", result="ok" if i < 8 else "error")
    cell = calibration.report()["cells"][0]
    assert cell["samples"] == 10
    assert cell["success_rate"] == 0.8
    lo, hi = cell["success_ci95"]
    assert lo < 0.8 < hi                          # interval brackets the estimate
    assert 0.0 <= lo and hi <= 1.0


def test_wilson_interval_known_values():
    lo, hi = calibration.wilson_interval(0, 0)
    assert (lo, hi) == (0.0, 1.0)                 # no data → maximal uncertainty
    lo, hi = calibration.wilson_interval(5, 10)
    assert lo == pytest.approx(0.2365, abs=1e-3)
    assert hi == pytest.approx(0.7635, abs=1e-3)
    # more samples at the same rate ⇒ a strictly tighter interval
    lo2, hi2 = calibration.wilson_interval(50, 100)
    assert (hi2 - lo2) < (hi - lo)


def test_outcome_rates_join_feedback_to_observations():
    for i in range(6):
        _obs(f"run-{i}")
    calibration.record_feedback("run-0", calibration.APPROVED)
    calibration.record_feedback("run-1", calibration.APPROVED)
    calibration.record_feedback("run-2", calibration.REJECTED)
    calibration.record_feedback("run-3", calibration.RETRIED)
    cell = calibration.report()["cells"][0]
    assert cell["feedback_samples"] == 4
    assert cell["rates"][calibration.APPROVED] == 0.5
    assert cell["rates"][calibration.REJECTED] == 0.25
    assert cell["rates"][calibration.RETRIED] == 0.25


def test_cells_split_by_domain_and_model():
    for i in range(2):
        _obs(f"c{i}", domain="code")
        _obs(f"e{i}", domain="email")
    rep = calibration.report()
    assert len(rep["cells"]) == 2
    assert {c["domain"] for c in rep["cells"]} == {"code", "email"}


def test_missing_evidence_rate_is_reported():
    _obs("full", latency_ms=10, tokens_in=1, tokens_out=1, cost_usd=0.1)
    calibration.record_observation("sparse")
    rep = calibration.report()
    assert 0.0 < rep["missing_evidence_rate"] < 1.0


def test_freshness_is_reported():
    _obs("run-1")
    f = calibration.report()["freshness"]
    assert f["newest_at"] is not None
    assert f["age_seconds"] is not None and f["age_seconds"] >= 0


# --- export / import round-trip ------------------------------------------

def test_export_and_reimport_roundtrip(tmp_path):
    _obs("run-1", cost_usd=0.5)
    _obs("run-2")
    calibration.record_feedback("run-1", calibration.APPROVED)
    dest = tmp_path / "export" / "calibration.jsonl"
    n = calibration.export_jsonl(dest)
    assert n == 3
    back = calibration.import_jsonl(dest)
    assert back["count"] == 3
    assert back["verified"] is True
    assert [e["entry_hash"] for e in back["entries"]] == \
        [e["entry_hash"] for e in calibration.entries()]


def test_import_detects_a_tampered_export(tmp_path):
    _obs("run-1"); _obs("run-2")
    dest = tmp_path / "e.jsonl"
    calibration.export_jsonl(dest)
    lines = dest.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["body"]["model"] = "forged"
    lines[1] = json.dumps(rec, sort_keys=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    back = calibration.import_jsonl(dest)
    assert back["verified"] is False


def test_import_missing_file_is_graceful(tmp_path):
    back = calibration.import_jsonl(tmp_path / "nope.jsonl")
    assert back["count"] == 0 and back["verified"] is False


# --- tombstoning / retention ---------------------------------------------

def test_tombstone_hides_without_breaking_the_chain():
    a = _obs("run-1")
    _obs("run-2")
    assert calibration.tombstone(a["event_key"], "user requested") is not None
    runs = {e["body"]["run_id"] for e in calibration.entries(calibration.OBSERVATION)}
    assert runs == {"run-2"}                      # hidden from readers
    assert calibration.verify()["ok"] is True     # chain still intact
    assert any(e["body"]["run_id"] == "run-1"
               for e in calibration.entries(calibration.OBSERVATION,
                                            include_tombstoned=True))


def test_retention_prune_tombstones_old_entries(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CALIBRATION_RETENTION_DAYS", "7")
    calibration.record_observation("old", at="2020-01-01T00:00:00+00:00")
    _obs("fresh")
    assert calibration.prune() == 1
    runs = {e["body"]["run_id"] for e in calibration.entries(calibration.OBSERVATION)}
    assert runs == {"fresh"}
    assert calibration.verify()["ok"] is True


def test_retention_off_by_default():
    calibration.record_observation("old", at="2020-01-01T00:00:00+00:00")
    assert calibration.prune() == 0               # 0 days = keep everything


# --- durability ----------------------------------------------------------

def test_records_survive_restart(monkeypatch, tmp_path):
    _obs("run-1", cost_usd=0.25)
    _obs("run-2")
    # simulate a process restart: fresh module state, same MEMORY_DIR
    import importlib
    reloaded = importlib.reload(calibration)
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    monkeypatch.setattr(reloaded.config, "MEMORY_DIR", tmp_path)
    assert len(reloaded.entries(reloaded.OBSERVATION)) == 2
    assert reloaded.verify()["ok"] is True


# --- observation-only guarantee (requirement 10) -------------------------

def test_calibration_writes_no_decision():
    """The module must not import or touch any decision-carrying surface. This
    is the machine-checked form of 'observation first, automation later' — the
    same contract outcomes.py and rlscaffold.py keep."""
    import inspect
    src = inspect.getsource(calibration)
    for forbidden in ("import trust", "from . import trust",
                      "bandit_routing", "learned_routing", "modelpin",
                      "capprofile", "approvals", "specialists"):
        assert forbidden not in src, f"calibration must not touch {forbidden}"


def test_no_decision_module_touches_calibration():
    """Nothing that makes routing/permission/trust decisions may reference the
    record at all in this prototype."""
    import pathlib
    root = pathlib.Path(calibration.__file__).parent
    for name in ("trust.py", "bandit_routing.py", "learned_routing.py",
                 "modelpin.py", "capprofile.py", "backend.py", "llm.py"):
        p = root / name
        if p.exists():
            assert "calibration" not in p.read_text(encoding="utf-8"), \
                f"{name} must not consume the calibration record yet"


def test_orchestrator_only_writes_never_reads():
    """The orchestrator is the ONE integration point, and it may only RECORD.
    Reading the record back (report/rank/entries) inside a run would be the
    first step toward a decision loop — the line this prototype must not cross."""
    import pathlib
    src = (pathlib.Path(calibration.__file__).parent / "orchestrator.py").read_text(
        encoding="utf-8")
    assert "calibration.record_observation(" in src        # writes: required
    for reader in ("calibration.report(", "calibration.rank_models(",
                   "calibration.entries(", "calibration.render_report("):
        assert reader not in src, \
            f"orchestrator must not consume the record ({reader})"


# --- integration: a REAL run produces an entry (success criterion 1) -------

def _stub_client():
    """The stubbed Anthropic client used by the existing orchestrator tests —
    deterministic, no network, no paid API."""
    import json as _json
    import types
    import anthropic

    def _msg(text):
        return anthropic.types.Message.model_validate({
            "id": "m", "type": "message", "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 5,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}})

    def stream(**params):
        props = (((params.get("output_config") or {}).get("format") or {})
                 .get("schema") or {}).get("properties", {})

        class _S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                if "mode" in props:
                    return _msg(_json.dumps(
                        {"mode": "direct", "direct_reply": "hello",
                         "specialists": [], "brief": "",
                         "needs_verification": False}))
                if "verdict" in props:
                    return _msg(_json.dumps({"verdict": "approve",
                                             "feedback": "",
                                             "retry_specialists": []}))
                return _msg("hello")
        return _S()

    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


def test_real_run_produces_a_calibration_entry(monkeypatch):
    from olympus import config, llm, orchestrator, recall
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(recall, "context_block", lambda *a, **k: "")
    monkeypatch.setattr(llm, "client", lambda settings=None: _stub_client())

    bot = orchestrator.Olympus(pool=config.ModelPool.from_env())
    bot.ask("what is the capital of France?")

    obs = calibration.entries(calibration.OBSERVATION)
    assert len(obs) == 1, "a real run must produce exactly one observation"
    b = obs[0]["body"]
    assert b["run_id"] == bot.last_run_id          # joins to the trace
    assert b["provider"] == "anthropic"
    assert b["result"] == "ok"
    assert "France" not in json.dumps(obs[0])      # prompt text never stored
    assert calibration.verify()["ok"] is True


def test_real_run_records_nothing_when_disabled(monkeypatch):
    from olympus import config, llm, orchestrator, recall
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(recall, "context_block", lambda *a, **k: "")
    monkeypatch.setattr(llm, "client", lambda settings=None: _stub_client())

    bot = orchestrator.Olympus(pool=config.ModelPool.from_env())
    reply = bot.ask("what is the capital of France?")

    assert reply                                   # run still works normally
    assert not calibration.path().exists()         # and wrote nothing at all
