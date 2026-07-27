"""Experiments & quarantine registry — Wave-2 capability C4 (gate W2-A6).

What these prove:

1. **W2-I4.1 — no experimental feature bypasses the registry.** The shipped
   registry passes `check_registry()` today, and the gate genuinely FAILS on a
   declared flag with no entry, a missing/empty required field, an invalid
   status, an unparseable date, a duplicate id, and an undeclared
   `OLYMPUS_*EXPERIMENTAL*` flag. A gate that cannot fail is not a gate.
2. **W2-I4.2 — expired evidence auto-disables.** `active()` is False once
   `next_review` is past, True before it, and False for every status that is
   not `active`/`shadow` — including `accepted_debt`.
3. **The retest sink works and never raises**, even with a broken proclock or
   an unwritable memory dir, and a recorded retest extends the review window.
4. **The real Wave-1 negatives are present by id** — asserted explicitly so a
   future edit cannot quietly drop an inconvenient one.
"""

from __future__ import annotations

import copy
import datetime as dt
import json

import pytest

from olympus import experiments


# --- helpers -----------------------------------------------------------------

def _entry(**over) -> dict:
    e = {f: f"{f} text" for f in experiments.REQUIRED_FIELDS}
    e.update({
        "id": "demo",
        "status": experiments.ACTIVE,
        "created": "2026-01-01",
        "last_tested": "2026-01-01",
        "next_review": "2026-12-31",
    })
    e.update(over)
    return e


def _registry(*entries) -> dict:
    return {"version": experiments.SCHEMA_VERSION, "entries": list(entries)}


def _fake_load(monkeypatch, registry: dict):
    monkeypatch.setattr(experiments, "load", lambda: copy.deepcopy(registry))


# --- 1. the shipped registry is valid data -----------------------------------

def test_committed_registry_is_valid_json_with_the_declared_schema():
    data = json.loads(experiments.registry_path().read_text(encoding="utf-8"))
    assert data["version"] == experiments.SCHEMA_VERSION
    assert isinstance(data["entries"], list) and data["entries"]
    ids = [e["id"] for e in data["entries"]]
    assert len(ids) == len(set(ids)), "entry ids must be unique"


def test_shipped_registry_passes_the_ci_gate():
    """W2-I4.1: the repo is consistent right now."""
    assert experiments.check_registry() == []


def test_every_declared_flag_resolves_to_an_entry():
    ids = {e["id"] for e in experiments.all_entries()}
    unmapped = {f: t for f, t in experiments.known_flags().items()
                if t not in ids}
    assert not unmapped, f"flags without an entry: {unmapped}"


def test_known_flags_is_a_copy():
    flags = experiments.known_flags()
    flags["OLYMPUS_BOGUS"] = "nope"
    assert "OLYMPUS_BOGUS" not in experiments.known_flags()


def test_every_entry_status_is_in_the_vocabulary():
    bad = [(e["id"], e["status"]) for e in experiments.all_entries()
           if e["status"] not in experiments.STATUSES]
    assert not bad


# --- 2. the seeded Wave-1 negatives can never be quietly dropped -------------

WAVE1_NEGATIVES = [
    # the false claim re-declared (audit §3)
    "ctxbudget-calibrated-estimator",
    # documented residuals / accepted debt (audit §5)
    "replay-secret-screen-residual",
    "sessionlog-seal-key-rotation",
    "sessionlog-suffix-rollback",
    "modelgate-missing-domain",
    "modelgate-budget-output-bound",
    "toolcall-validate-off-legacy",
    "noninterference-parallel-dispatch",
]

WAVE2_PROPOSALS = [
    "ctxheat-provisional-constants",
    "modelgrade-ladder",
    "routesub-substitution-bands",
    "watchdog-progress-leases",
]

WAVE3_CANDIDATES = [
    "wave3-speculation-draftverify",
    "wave3-prefetch-activation",
    "wave3-local-tier",
    "wave3-provider-mirror",
]


@pytest.mark.parametrize("entry_id", WAVE1_NEGATIVES)
def test_wave1_negative_is_registered(entry_id):
    e = experiments.entry(entry_id)
    assert e is not None, f"Wave-1 negative '{entry_id}' vanished from the registry"
    assert e["status"] == experiments.ACCEPTED_DEBT
    assert e["owner"], "a negative result needs a real owner"
    assert len(e["evidence"]) > 80, "the eulogy must carry the actual evidence"


def test_ic4_entry_carries_the_measured_numbers():
    """The re-declared estimator claim keeps its distributional numbers, not a
    vague 'known to be inaccurate'."""
    e = experiments.entry("ctxbudget-calibrated-estimator")
    for token in ("43.9%", "29.2%", "11.5%", "58.0%", "held-out"):
        assert token in e["evidence"], token


def test_im1_entry_names_the_output_bound_residual():
    e = experiments.entry("modelgate-budget-output-bound")
    text = e["evidence"] + e["reason"]
    assert "_DRIFT_MAX_OUTPUT" in text
    assert "$5.20" in e["evidence"], "the measured overrun stays on the record"


@pytest.mark.parametrize("entry_id", WAVE2_PROPOSALS + WAVE3_CANDIDATES)
def test_unbuilt_capability_is_proposed_with_an_activation_floor(entry_id):
    e = experiments.entry(entry_id)
    assert e is not None, entry_id
    assert e["status"] == experiments.PROPOSED
    assert len(e["activation_condition"]) > 40, \
        "a proposed capability must name the evidence floor that unlocks it"
    assert not experiments.active(entry_id), \
        "a proposed capability is never active"


def test_wave3_candidates_are_gated_on_named_floors():
    prefetch = experiments.entry("wave3-prefetch-activation")
    assert "recall@2" in prefetch["activation_condition"]
    assert "n >= 200" in prefetch["activation_condition"]
    for entry_id in ("wave3-speculation-draftverify", "wave3-local-tier"):
        assert "modelgrade" in experiments.entry(entry_id)["activation_condition"]


# --- 3. W2-I4.1: the gate fails on a real problem ----------------------------

def test_gate_fails_when_a_flag_has_no_entry(monkeypatch):
    flags = dict(experiments.EXPERIMENTAL_FLAGS)
    flags["OLYMPUS_BOGUS_FEATURE"] = "bogus-unregistered-experiment"
    monkeypatch.setattr(experiments, "EXPERIMENTAL_FLAGS", flags)
    problems = experiments.check_registry()
    assert any("OLYMPUS_BOGUS_FEATURE" in p and "W2-I4.1" in p
               for p in problems), problems


def test_gate_fails_on_a_missing_required_field(monkeypatch):
    for field in experiments.REQUIRED_FIELDS:
        broken = _entry()
        broken.pop(field)
        _fake_load(monkeypatch, _registry(broken))
        problems = experiments.check(flags={}, namespace=set())
        assert any(f"'{field}'" in p and "missing" in p for p in problems), \
            f"dropping '{field}' must be caught: {problems}"


def test_gate_fails_on_an_empty_required_field(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(hypothesis="   ")))
    problems = experiments.check(flags={}, namespace=set())
    assert any("empty 'hypothesis'" in p for p in problems), problems


def test_gate_allows_empty_last_tested_and_outcome(monkeypatch):
    """An untested experiment has no honest date to put in last_tested."""
    _fake_load(monkeypatch, _registry(_entry(last_tested="", outcome="")))
    assert experiments.check(flags={}, namespace=set()) == []


def test_gate_fails_on_an_invalid_status(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(status="probably_fine")))
    problems = experiments.check(flags={}, namespace=set())
    assert any("invalid status 'probably_fine'" in p for p in problems), problems


@pytest.mark.parametrize("field", ["created", "last_tested", "next_review"])
def test_gate_fails_on_an_unparseable_date(monkeypatch, field):
    _fake_load(monkeypatch, _registry(_entry(**{field: "someday"})))
    problems = experiments.check(flags={}, namespace=set())
    assert any(f"unparseable {field}" in p for p in problems), problems


def test_gate_fails_on_review_before_creation(monkeypatch):
    _fake_load(monkeypatch,
               _registry(_entry(created="2026-05-01", next_review="2026-01-01")))
    problems = experiments.check(flags={}, namespace=set())
    assert any("before created" in p for p in problems), problems


def test_gate_fails_on_duplicate_ids(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(), _entry()))
    problems = experiments.check(flags={}, namespace=set())
    assert any("duplicate experiment id 'demo'" in p for p in problems), problems


def test_gate_fails_on_a_non_string_field(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(cost=3)))
    problems = experiments.check(flags={}, namespace=set())
    assert any("must be a string" in p for p in problems), problems


def test_gate_fails_on_an_undeclared_namespace_flag(monkeypatch):
    """Ruling R2's OLYMPUS_*EXPERIMENTAL* namespace is policed too."""
    monkeypatch.setenv("OLYMPUS_EXPERIMENTAL_TELEPORT", "1")
    problems = experiments.check_registry()
    assert any("OLYMPUS_EXPERIMENTAL_TELEPORT" in p for p in problems), problems
    # ...and declaring it (with an entry) clears the problem.
    flags = dict(experiments.EXPERIMENTAL_FLAGS)
    flags["OLYMPUS_EXPERIMENTAL_TELEPORT"] = "modelgrade-ladder"
    monkeypatch.setattr(experiments, "EXPERIMENTAL_FLAGS", flags)
    assert experiments.check_registry() == []


def test_namespace_scan_ignores_this_module_itself():
    """experiments.py names the namespace in order to police it; it must not
    report its own regex as an undeclared flag."""
    assert not any(n not in experiments.EXPERIMENTAL_FLAGS
                   for n in experiments.namespace_flags())


def test_gate_reports_a_wrong_schema_version(monkeypatch):
    reg = _registry(_entry())
    reg["version"] = 99
    _fake_load(monkeypatch, reg)
    problems = experiments.check(flags={}, namespace=set())
    assert any("version is 99" in p for p in problems), problems


# --- 4. W2-I4.2: expiry auto-disable -----------------------------------------

def test_active_true_before_next_review(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(next_review="2026-12-31")))
    assert experiments.active("demo", now="2026-06-01") is True
    assert experiments.active("demo", now="2026-12-31") is True, \
        "the review date itself is still fresh"


def test_active_false_past_next_review(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(next_review="2026-12-31")))
    assert experiments.active("demo", now="2027-01-01") is False
    assert experiments.active("demo", now=dt.date(2030, 1, 1)) is False


def test_active_accepts_date_and_datetime_for_now(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(next_review="2026-12-31")))
    assert experiments.active("demo", now=dt.date(2026, 6, 1)) is True
    assert experiments.active("demo", now=dt.datetime(2027, 6, 1, 12)) is False


@pytest.mark.parametrize("status", [
    experiments.PROPOSED, experiments.QUARANTINED, experiments.REJECTED,
    experiments.ACCEPTED_DEBT, experiments.INCIDENT,
])
def test_active_false_for_non_running_statuses(monkeypatch, status):
    _fake_load(monkeypatch, _registry(_entry(status=status)))
    assert experiments.active("demo", now="2026-06-01") is False


def test_shadow_counts_as_active(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(status=experiments.SHADOW)))
    assert experiments.active("demo", now="2026-06-01") is True


def test_active_false_for_unregistered_id():
    assert experiments.active("no-such-experiment") is False


def test_active_false_when_the_review_date_is_unusable(monkeypatch):
    """Evidence that cannot prove its own freshness does not count."""
    _fake_load(monkeypatch, _registry(_entry(next_review="")))
    assert experiments.active("demo", now="2026-06-01") is False


def test_expired_lists_only_past_entries(monkeypatch):
    _fake_load(monkeypatch, _registry(
        _entry(id="stale", next_review="2026-01-01"),
        _entry(id="fresh", next_review="2026-12-31"),
        _entry(id="undated", next_review=""),
    ))
    assert experiments.expired(now="2026-06-01") == ["stale", "undated"]
    assert experiments.expired(now="2025-01-01") == ["undated"]


def test_shipped_registry_has_no_already_expired_entry():
    """Every seeded entry was given a future review window at commit time."""
    assert experiments.expired(now="2026-07-26") == []


def test_by_status_partitions_the_registry():
    entries = experiments.all_entries()
    covered = []
    for status in experiments.STATUSES:
        covered.extend(e["id"] for e in experiments.by_status(status))
    assert sorted(covered) == sorted(e["id"] for e in entries)
    assert {e["id"] for e in experiments.by_status(experiments.ACCEPTED_DEBT)} \
        >= set(WAVE1_NEGATIVES)
    assert experiments.by_status("not-a-status") == []


# --- 5. the retest sink ------------------------------------------------------

def test_record_test_appends_to_the_ledger():
    experiments.record_test("demo", outcome="pass", evidence="n=200, 0 diffs")
    experiments.record_test("other", outcome="fail", evidence="regressed")
    rows = experiments.state_records()
    assert [r["id"] for r in rows] == ["demo", "other"]
    assert rows[0]["outcome"] == "pass" and rows[0]["evidence"].startswith("n=200")
    assert rows[0]["ts"].endswith("Z")
    assert experiments.state_records("other") == [rows[1]]


def test_record_test_extends_the_review_window(monkeypatch):
    """W2-I4.2: expired until RE-TESTED — a retest genuinely re-enables it."""
    _fake_load(monkeypatch, _registry(_entry(next_review="2026-01-01")))
    assert experiments.active("demo", now="2026-06-01") is False
    experiments.record_test("demo", outcome="pass", evidence="re-measured",
                            next_review="2026-12-31")
    assert experiments.active("demo", now="2026-06-01") is True
    assert experiments.expired(now="2026-06-01") == []


def test_record_test_never_shortens_the_window(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(next_review="2026-12-31")))
    experiments.record_test("demo", outcome="pass", evidence="e",
                            next_review="2026-02-01")
    assert experiments.active("demo", now="2026-06-01") is True


def test_record_test_ignores_an_unparseable_review_date(monkeypatch):
    _fake_load(monkeypatch, _registry(_entry(next_review="2026-01-01")))
    rec = experiments.record_test("demo", outcome="pass", evidence="e",
                                  next_review="whenever")
    assert "next_review" not in rec
    assert experiments.active("demo", now="2026-06-01") is False


def test_record_test_never_raises_with_a_broken_proclock(monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def boom(*a, **kw):
        raise RuntimeError("lock wedged")
        yield                                        # pragma: no cover

    monkeypatch.setattr(experiments.proclock, "lock", boom)
    rec = experiments.record_test("demo", outcome="pass", evidence="e")
    assert rec["id"] == "demo"
    assert experiments.state_records() == []          # nothing written, no raise


def test_record_test_never_raises_when_the_ledger_is_unwritable(monkeypatch,
                                                                tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    from olympus import config
    monkeypatch.setattr(config, "MEMORY_DIR", blocker)
    rec = experiments.record_test("demo", outcome="pass", evidence="e")
    assert rec["outcome"] == "pass"
    assert experiments.state_records() == []


def test_state_records_skips_corrupt_lines():
    experiments.record_test("demo", outcome="pass", evidence="e")
    path = experiments.state_path()
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n\n[1,2,3]\n")
    experiments.record_test("demo2", outcome="pass", evidence="e")
    assert [r["id"] for r in experiments.state_records()] == ["demo", "demo2"]


def test_state_records_empty_without_a_ledger():
    assert experiments.state_records() == []


# --- 6. corrupt / missing registry: empty, never a raise ---------------------

def test_corrupt_registry_reads_empty_and_never_raises(monkeypatch, tmp_path):
    bad = tmp_path / "experiments.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(experiments, "registry_path", lambda: bad)
    captured = []
    from olympus import errors
    monkeypatch.setattr(errors, "capture",
                        lambda where, exc, context="": captured.append(where))
    assert experiments.load() == {"version": experiments.SCHEMA_VERSION,
                                  "entries": []}
    assert experiments.all_entries() == []
    assert experiments.entry("anything") is None
    assert experiments.active("anything") is False     # fail-closed
    assert experiments.expired() == []
    assert set(captured) == {"experiments.load"}, \
        "corruption is reported, and only as a load problem"


def test_missing_registry_reads_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(experiments, "registry_path",
                        lambda: tmp_path / "gone.json")
    assert experiments.load()["entries"] == []
    problems = experiments.check(flags={"OLYMPUS_X": "y"}, namespace=set())
    assert any("W2-I4.1" in p for p in problems), problems


@pytest.mark.parametrize("payload", ["[]", '{"entries": {}}', '"nope"', "12"])
def test_wrongly_shaped_registry_reads_empty(monkeypatch, tmp_path, payload):
    bad = tmp_path / "experiments.json"
    bad.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(experiments, "registry_path", lambda: bad)
    assert experiments.load()["entries"] == []


def test_non_object_entries_are_dropped(monkeypatch, tmp_path):
    path = tmp_path / "experiments.json"
    path.write_text(json.dumps({"version": 1, "entries": [_entry(), "junk", 7]}),
                    encoding="utf-8")
    monkeypatch.setattr(experiments, "registry_path", lambda: path)
    assert [e["id"] for e in experiments.all_entries()] == ["demo"]
