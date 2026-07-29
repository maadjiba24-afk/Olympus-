"""Promotion must fail closed: no durable record, no permission.

The pre-merge audit found `GateLedger._log` wrapping its audit write in a bare
`except Exception: pass`. For a rejection that is the right way to fail —
refusing to stop a deteriorating model because the audit disk is full leaves it
running. For a promotion it is inverted: it meant a challenger could go live
with nothing on record saying who approved it, and afterwards that state is
indistinguishable from a promotion nobody authorised.

Every test here breaks one part of the durable path and asserts the same thing:
**the challenger is not promoted.** Not "promoted with a warning", not
"promoted and the audit is missing" — unpromoted, with the exception naming
what failed.

The nine failure modes, and what each one is really about:

| test                      | the failure it stands for                        |
|---------------------------|--------------------------------------------------|
| audit sink unavailable    | the log path cannot be written at all             |
| signature failure         | the chain does not verify before the write        |
| disk full                 | `write()` succeeds and stores fewer bytes         |
| partial audit write       | a crash mid-line leaves a truncated record        |
| partial state write       | the state file is truncated or unparseable        |
| crash between writes      | audit landed, state did not                       |
| duplicate request         | the same promotion submitted twice                |
| restart and reconstruction| a fresh process reads the durable records         |
| tampered promotion record | somebody edited the state or the log by hand      |
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.governance import Actor
from olympus.trading.errors import GovernanceViolation
from olympus.trading.native import durable as DU
from olympus.trading.native import promotion as PR

START = datetime(2028, 5, 1, tzinfo=timezone.utc)
KEY = "SIM:BTCUSDT"

REVIEW_EVIDENCE = {
    "model_version": "native-0.1.0+test",
    "evaluation_report": "docs/OLYMPUS_VS_KRONOS.md#matched",
    "reviewed_by": "alice",
    "review_notes": "read the matched evaluation and the robustness suite",
    "rollback_plan": "revert to the incumbent and rerun shadow mode",
}


def passing_checks():
    return {stage: (lambda s=stage: (True, {"stage": s.value, "measured": 1.0}))
            for stage in PR.STAGE_ORDER if stage in PR.AUTONOMOUS_STAGES}


def ledger_at(tmp_path, **kwargs) -> PR.GateLedger:
    return PR.GateLedger(audit_log=DU.AuditLog(tmp_path / "audit.jsonl"),
                         state=DU.StateStore(tmp_path / "state.json"),
                         **kwargs)


def ready_for_review(ledger: PR.GateLedger, challenger_id: str = "c1"):
    """Take a challenger through stages 1–10. Autonomous, no operator."""
    ledger.enter(challenger_id=challenger_id, proposal_id="p1",
                 created_at=START)
    PR.run_autonomous_stages(ledger, challenger_id, checks=passing_checks(),
                             at=START)
    return ledger[challenger_id]


def reviewed(ledger: PR.GateLedger, operator, challenger_id: str = "c1"):
    ready_for_review(ledger, challenger_id)
    ledger.advance(challenger_id, PR.StageResult(
        stage=PR.GateStage.HUMAN_REVIEW, passed=True, at=START,
        evidence={"reviewer": operator.name}, actor=operator.name),
        actor=operator)
    return ledger[challenger_id]


def restriction_at(when=START):
    return PR.default_restriction(instruments=(KEY,), max_notional=1000.0,
                                  at=when)


@pytest.fixture
def operator():
    return Actor.operator("alice", token="tok")


# ---------------------------------------------------------------------------
# the happy path, so the failures below mean something
# ---------------------------------------------------------------------------

def test_a_promotion_writes_both_durable_records_before_it_returns(
        tmp_path, operator):
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    run = ledger.promote("c1", actor=operator, at=START,
                         restriction=restriction_at(),
                         evidence=REVIEW_EVIDENCE)
    assert run.outcome is PR.GateOutcome.PROMOTED

    # One chain for everything: the ten autonomous stages are recorded by
    # "olympus" through the best-effort path, and the two human decisions are
    # recorded by the operator through the fail-closed one.
    events = ledger.audit.for_subject("c1")
    assert events[-1].event == "native.promotion.promote"
    assert [e.actor for e in events[-2:]] == ["alice", "alice"]
    assert {e.actor for e in events[:-2]} == {"olympus"}
    assert ledger.audit.verify() == ()

    state = ledger.durable_state
    assert "c1" in state
    assert state["c1"]["outcome"] == "promoted"
    assert state["c1"]["audit_digest"] == events[-1].digest, (
        "the state must name the audit event that authorised it, or the two "
        "records cannot be checked against each other after a restart")
    assert (tmp_path / "audit.jsonl").is_file()
    assert (tmp_path / "state.json").is_file()


def test_the_order_is_operator_evidence_audit_state(tmp_path, operator):
    """Each step must fail before the next one has written anything."""
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    before = len(ledger.audit)

    # 1. a non-operator never reaches the evidence check.
    with pytest.raises(GovernanceViolation):
        ledger.promote("c1", actor=Actor.autonomous("engine"), at=START,
                       restriction=restriction_at(), evidence=REVIEW_EVIDENCE)
    assert len(ledger.audit) == before

    # 2. incomplete evidence never reaches the audit write.
    with pytest.raises(PR.PromotionRefused) as raised:
        ledger.promote("c1", actor=operator, at=START,
                       restriction=restriction_at(),
                       evidence={"model_version": "x"})
    assert set(raised.value.details["missing"]) == {
        "evaluation_report", "reviewed_by", "review_notes", "rollback_plan"}
    assert len(ledger.audit) == before
    assert ledger.durable_state["c1"]["outcome"] != "promoted"
    assert ledger["c1"].outcome is not PR.GateOutcome.PROMOTED


# ---------------------------------------------------------------------------
# 1. audit sink unavailable
# ---------------------------------------------------------------------------

def test_promotion_fails_closed_when_the_audit_sink_is_unavailable(
        tmp_path, operator):
    """The log path cannot be written. Nothing else is attempted."""
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)

    # A directory where the log file should be. Chosen over a mode-bit trick
    # because these tests run as root in CI, and root ignores mode bits — the
    # same reason the isolation module uses a read-only bind mount rather than
    # `chmod 0444`.
    unwritable = tmp_path / "blocked" / "audit.jsonl"
    unwritable.mkdir(parents=True)

    # Opening it is already a failure, and that is the right moment for it —
    # a log that cannot be read cannot be appended to intact either.
    with pytest.raises(DU.DurabilityError):
        DU.AuditLog(unwritable)

    class _Unavailable(DU.AuditLog):
        """Constructs, and then cannot write. The sink that dies at runtime."""

        def commit(self, **kwargs):
            raise DU.DurabilityError("the audit sink is unavailable",
                                     path=str(self.path))

    ledger._log_store = _Unavailable(tmp_path / "audit.jsonl")   # noqa: SLF001
    with pytest.raises(DU.DurabilityError):
        ledger.promote("c1", actor=operator, at=START,
                       restriction=restriction_at(),
                       evidence=REVIEW_EVIDENCE)

    assert unwritable.is_dir()
    assert ledger.durable_state["c1"]["outcome"] != "promoted"
    assert ledger["c1"].outcome is not PR.GateOutcome.PROMOTED


def test_an_in_memory_audit_log_refuses_to_promote_by_default(operator):
    """A gate with nowhere durable to write does not quietly promote into RAM."""
    ledger = PR.GateLedger()
    reviewed(ledger, operator) if False else ready_for_review(ledger)
    with pytest.raises(DU.DurabilityError) as raised:
        ledger.advance("c1", PR.StageResult(
            stage=PR.GateStage.HUMAN_REVIEW, passed=True, at=START,
            evidence={"reviewer": "alice"}, actor="alice"), actor=operator)
    assert raised.value.details["audit_durable"] is False
    assert ledger["c1"].furthest is PR.GateStage.SHADOW_MODE

    # The volatility has to be asked for by name.
    volatile = PR.GateLedger(allow_volatile_promotion=True)
    ready_for_review(volatile)
    volatile.advance("c1", PR.StageResult(
        stage=PR.GateStage.HUMAN_REVIEW, passed=True, at=START,
        evidence={"reviewer": "alice"}, actor="alice"), actor=operator)
    assert volatile["c1"].furthest is PR.GateStage.HUMAN_REVIEW


# ---------------------------------------------------------------------------
# 2. signature / chain failure
# ---------------------------------------------------------------------------

def test_promotion_fails_closed_when_the_audit_chain_does_not_verify(
        tmp_path, operator):
    """A broken chain is not a place to append a promotion to.

    Appending would produce a log whose earlier records cannot be trusted and
    whose newest one looks fine, which is worse than refusing: it launders the
    tampering behind a valid-looking head.
    """
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)

    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    record = json.loads(lines[0])
    record["detail"]["passed"] = "the failure that was never recorded"
    record["digest"] = DU.digest_of({k: v for k, v in record.items()
                                     if k != "digest"})  # restamp it
    path.write_text("\n".join([json.dumps(record, sort_keys=True)] + lines[1:])
                    + "\n")

    fresh = PR.GateLedger(audit_log=DU.AuditLog(path),
                          state=DU.StateStore(tmp_path / "state.json"))
    fresh._runs = dict(ledger._runs)                     # noqa: SLF001
    with pytest.raises(DU.TamperedRecord) as raised:
        fresh.promote("c1", actor=operator, at=START,
                      restriction=restriction_at(), evidence=REVIEW_EVIDENCE)
    assert raised.value.details["findings"]
    assert fresh.durable_state["c1"]["outcome"] != "promoted"


def test_a_record_whose_digest_does_not_match_its_body_is_rejected(tmp_path):
    """Editing the content and leaving the digest is caught on read."""
    path = tmp_path / "audit.jsonl"
    log = DU.AuditLog(path)
    log.commit(event="native.promotion.promote", subject="c1", at=START,
               actor="alice", detail={"restriction": {"max_notional": 1000.0}})
    record = json.loads(path.read_text().splitlines()[0])
    record["detail"]["restriction"]["max_notional"] = 10_000_000.0
    path.write_text(json.dumps(record, sort_keys=True) + "\n")

    with pytest.raises(DU.TamperedRecord):
        DU.AuditLog(path).events


# ---------------------------------------------------------------------------
# 3. disk full
# ---------------------------------------------------------------------------

class _FullDisk(DU.AuditLog):
    """A log whose device is full: the write raises `ENOSPC`."""

    def commit(self, **kwargs):
        raise OSError(28, "No space left on device", str(self.path))


class _ShortWrite(DU.AuditLog):
    """Worse than a full disk: the write *succeeds* and stores fewer bytes.

    This is the case a `try/except OSError` around the write would miss
    entirely, and it is why the commit reads back and compares digests instead
    of trusting the return of `write()`.
    """

    def commit(self, **kwargs):
        record = DU.AuditEvent(event=kwargs["event"], subject=kwargs["subject"],
                               at=kwargs["at"], actor=kwargs["actor"],
                               detail=dict(kwargs.get("detail") or {}),
                               previous=self.head)
        line = json.dumps(record.to_dict(), sort_keys=True, default=str)
        DU.append_durably(self.path, line[:len(line) // 2])
        stored = self.events
        if not stored or stored[-1].digest != record.digest:
            raise DU.DurabilityError("the audit event did not read back",
                                     path=str(self.path))
        return record                                    # pragma: no cover


def test_promotion_fails_closed_when_the_disk_is_full(tmp_path, operator):
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    ledger._log_store = _FullDisk(tmp_path / "audit.jsonl")   # noqa: SLF001

    with pytest.raises(OSError) as raised:
        ledger.promote("c1", actor=operator, at=START,
                       restriction=restriction_at(), evidence=REVIEW_EVIDENCE)
    assert raised.value.errno == 28
    assert ledger.durable_state["c1"]["outcome"] != "promoted"
    assert ledger["c1"].outcome is not PR.GateOutcome.PROMOTED


def test_promotion_fails_closed_when_the_audit_write_is_short(
        tmp_path, operator):
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    ledger._log_store = _ShortWrite(tmp_path / "audit.jsonl")  # noqa: SLF001

    with pytest.raises(DU.DurabilityError):
        ledger.promote("c1", actor=operator, at=START,
                       restriction=restriction_at(), evidence=REVIEW_EVIDENCE)
    assert ledger.durable_state["c1"]["outcome"] != "promoted"
    assert ledger["c1"].outcome is not PR.GateOutcome.PROMOTED


# ---------------------------------------------------------------------------
# 4. partial audit write
# ---------------------------------------------------------------------------

def test_a_partial_final_line_is_dropped_rather_than_half_believed(tmp_path):
    """A crash mid-append leaves a truncated line. It never completed."""
    path = tmp_path / "audit.jsonl"
    log = DU.AuditLog(path)
    log.commit(event="native.promotion.stage", subject="c1", at=START,
               actor="alice", detail={"stage": "human_review"})
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"event": "native.promotion.promote", "subject": "c1"')

    reopened = DU.AuditLog(path)
    assert len(reopened) == 1
    assert reopened.verify() == ()
    assert [e.event for e in reopened] == ["native.promotion.stage"]


def test_corruption_in_the_middle_of_the_log_is_not_forgiven(tmp_path):
    """Only the *last* line may be partial. Anything earlier is tampering."""
    path = tmp_path / "audit.jsonl"
    log = DU.AuditLog(path)
    for index in range(3):
        log.commit(event="native.promotion.stage", subject=f"c{index}",
                   at=START, actor="alice", detail={"n": index})
    lines = path.read_text().splitlines()
    lines[1] = lines[1][:20]
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(DU.TamperedRecord):
        DU.AuditLog(path).events


# ---------------------------------------------------------------------------
# 5. partial state write
# ---------------------------------------------------------------------------

class _TruncatingState(DU.StateStore):
    """A state store whose file lands truncated."""

    def save(self, state):
        payload = json.dumps(dict(state), sort_keys=True, default=str)
        DU.replace_durably(self.path, payload[:len(payload) // 2])
        raise DU.DurabilityError("the state did not read back as written",
                                 path=str(self.path))


def test_promotion_fails_closed_when_the_state_write_is_partial(
        tmp_path, operator):
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    ledger._state = _TruncatingState(tmp_path / "state.json")   # noqa: SLF001

    with pytest.raises(DU.DurabilityError):
        ledger.promote("c1", actor=operator, at=START,
                       restriction=restriction_at(), evidence=REVIEW_EVIDENCE)
    assert ledger["c1"].outcome is not PR.GateOutcome.PROMOTED

    # And the truncated file is not readable as a promotion either.
    with pytest.raises(DU.TamperedRecord):
        DU.StateStore(tmp_path / "state.json").load()


def test_an_atomic_replace_never_leaves_a_reader_a_half_written_state(tmp_path):
    """`os.replace` swaps the inode; a reader sees the old file or the new one."""
    path = tmp_path / "state.json"
    store = DU.StateStore(path)
    store.save({"c1": {"outcome": "promoted"}})
    first = path.read_text()

    with pytest.raises(ValueError):
        DU.replace_durably(path, _raise_midway())
    assert path.read_text() == first, "a failed write replaced the good state"
    assert list(DU.StateStore(path).load()) == ["c1"]
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")], \
        "a failed atomic write left its temporary file behind"


def _raise_midway() -> str:
    raise ValueError("the payload could not be produced")


# ---------------------------------------------------------------------------
# 6. crash between the two writes
# ---------------------------------------------------------------------------

class _CrashAfterAudit(DU.StateStore):
    """Simulates the process dying between the audit write and the state write."""

    def save(self, state):
        raise DU.DurabilityError("the process died before the state landed",
                                 path=str(self.path))


def test_a_crash_between_the_two_writes_leaves_the_challenger_unpromoted(
        tmp_path, operator):
    """The window that cannot be made atomic, and the direction it fails in.

    The audit event is written first on purpose. A crash here leaves evidence
    that a promotion was attempted and no state saying it was granted, and
    `reconstruct()` reads the state. The other order would come back promoted
    with nothing on record saying who approved it — which is the failure this
    whole module exists to prevent.
    """
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    ledger._state = _CrashAfterAudit(tmp_path / "state.json")   # noqa: SLF001

    with pytest.raises(DU.DurabilityError):
        ledger.promote("c1", actor=operator, at=START,
                       restriction=restriction_at(), evidence=REVIEW_EVIDENCE)

    assert ledger["c1"].outcome is not PR.GateOutcome.PROMOTED
    # The attempt is on record.
    events = [e.event for e in DU.AuditLog(tmp_path / "audit.jsonl")]
    assert "native.promotion.promote" in events

    restarted = ledger_at(tmp_path)
    findings = restarted.reconstruct()
    assert any("no durable state naming it" in f for f in findings), findings
    assert restarted.durable_state["c1"]["outcome"] != "promoted", (
        "the surviving half of the crash window granted a permission")


# ---------------------------------------------------------------------------
# 7. duplicate request
# ---------------------------------------------------------------------------

def test_a_duplicate_promotion_request_is_refused(tmp_path, operator):
    """Promoting twice would append a second authorisation for one decision."""
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    ledger.promote("c1", actor=operator, at=START,
                   restriction=restriction_at(), evidence=REVIEW_EVIDENCE)
    audit_before = len(ledger.audit)

    with pytest.raises(PR.PromotionRefused):
        ledger.promote("c1", actor=operator, at=START,
                       restriction=restriction_at(), evidence=REVIEW_EVIDENCE)
    assert len(ledger.audit) == audit_before, \
        "a refused duplicate still wrote an audit event"
    assert len(ledger.durable_state["c1"]["results"]) == 12


def test_re_entering_a_challenger_id_is_refused(tmp_path):
    ledger = ledger_at(tmp_path)
    ready_for_review(ledger)
    with pytest.raises(PR.PromotionRefused):
        ledger.enter(challenger_id="c1", proposal_id="p2", created_at=START)


# ---------------------------------------------------------------------------
# 8. restart and reconstruction
# ---------------------------------------------------------------------------

def test_a_restart_reconstructs_the_promotion_from_the_durable_records(
        tmp_path, operator):
    """A fresh ledger over the same files sees the same promotion."""
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    ledger.promote("c1", actor=operator, at=START,
                   restriction=restriction_at(), evidence=REVIEW_EVIDENCE)

    restarted = ledger_at(tmp_path)
    assert restarted.reconstruct() == ()
    state = restarted.durable_state
    assert state["c1"]["outcome"] == "promoted"
    assert state["c1"]["restriction"]["instruments"] == [KEY]
    assert state["c1"]["restriction"]["max_notional"] == 1000.0
    events = restarted.audit.for_subject("c1")
    assert events[-1].event == "native.promotion.promote"
    assert events[-1].digest == state["c1"]["audit_digest"]
    assert restarted.audit.verify() == ()


def test_reconstruction_reports_a_state_naming_an_audit_event_that_is_gone(
        tmp_path, operator):
    """Deleting the authorising event does not leave a quietly valid promotion."""
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    ledger.promote("c1", actor=operator, at=START,
                   restriction=restriction_at(), evidence=REVIEW_EVIDENCE)

    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join(path.read_text().splitlines()[:-1]) + "\n")

    restarted = ledger_at(tmp_path)
    findings = restarted.reconstruct()
    assert any("is not in the log" in f for f in findings), findings


# ---------------------------------------------------------------------------
# 9. tampered promotion record
# ---------------------------------------------------------------------------

def test_a_tampered_state_file_is_refused_rather_than_read(tmp_path, operator):
    """Widening the size cap by editing the file does not widen anything."""
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    ledger.promote("c1", actor=operator, at=START,
                   restriction=restriction_at(), evidence=REVIEW_EVIDENCE)

    path = tmp_path / "state.json"
    data = json.loads(path.read_text())
    data["c1"]["restriction"]["max_notional"] = 100_000_000.0
    path.write_text(json.dumps(data, sort_keys=True))

    with pytest.raises(DU.TamperedRecord) as raised:
        DU.StateStore(path).load()
    assert "does not match its own digest" in raised.value.message

    with pytest.raises(DU.TamperedRecord):
        ledger_at(tmp_path).reconstruct()


def test_inserting_a_promotion_into_the_state_without_an_audit_event_is_seen(
        tmp_path, operator):
    """The two records are checked against each other, not each on its own."""
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    ledger.promote("c1", actor=operator, at=START,
                   restriction=restriction_at(), evidence=REVIEW_EVIDENCE)

    store = DU.StateStore(tmp_path / "state.json")
    state = store.load()
    state["c2"] = {**state["c1"], "challenger_id": "c2",
                   "audit_digest": "f" * 64}
    store.save(state)                     # restamped, so the digest check passes

    findings = ledger_at(tmp_path).reconstruct()
    assert any(f.startswith("c2:") and "is not in the log" in f
               for f in findings), findings


# ---------------------------------------------------------------------------
# the other direction: safety actions still fail safe
# ---------------------------------------------------------------------------

class _BrokenSink:
    def record(self, **kwargs):
        raise RuntimeError("the audit service is down")


def test_a_rejection_still_happens_when_the_audit_is_unavailable(tmp_path):
    """The asymmetry, stated as a test.

    Refusing to reject a challenger because the audit disk is full would leave
    a failing model in the gate. So rejection proceeds — and the missing record
    is reported rather than swallowed, which is the part that was wrong before.
    """
    ledger = PR.GateLedger(audit=_BrokenSink(),
                           audit_log=_FullDisk(tmp_path / "audit.jsonl"),
                           state=DU.StateStore(tmp_path / "state.json"))
    ready_for_review(ledger)
    run = ledger.reject("c1", reason="deteriorating on the held-out split",
                        at=START)

    assert run.outcome is PR.GateOutcome.REJECTED
    unrecorded = ledger.unrecorded_safety_actions
    assert len(unrecorded) >= 1
    assert unrecorded[-1]["kind"] == "reject"
    assert any("audit sink" in f for f in unrecorded[-1]["failures"])
    assert any("durable log" in f for f in unrecorded[-1]["failures"])


def test_no_positive_decision_uses_the_best_effort_log():
    """Read as a structural claim, not as behaviour.

    `_log` is the swallow-and-continue path. If a human stage or a promotion
    ever reaches it, a permission expansion can succeed without a record again,
    and the whole of this file goes back to passing while the guarantee is
    gone.
    """
    import inspect

    source = inspect.getsource(PR.GateLedger)
    positive = source.split("def _commit_positive")[1].split("\n    def ")[0]
    assert "self._log(" not in positive, (
        "the fail-closed commit calls the best-effort logger")

    promote = source.split("def promote(self")[1].split("\n    def ")[0]
    assert "self._log(" not in promote
    assert "_commit_positive" in promote

    advance = source.split("def advance")[1].split("\n    def ")[0]
    assert "HUMAN_STAGES" in advance and "_commit_positive" in advance
    # The best-effort call in `advance` is on the autonomous branch, after the
    # human branch has already returned.
    human_branch = advance.split("if result.stage in HUMAN_STAGES:")[1]
    assert "return self._commit_positive" in human_branch.split("self._log(")[0]


def test_the_evidence_requirement_cannot_be_satisfied_with_blanks(
        tmp_path, operator):
    """Whitespace is not a review note."""
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    blank = {name: "   " for name in PR.REQUIRED_PROMOTION_EVIDENCE}
    with pytest.raises(PR.PromotionRefused) as raised:
        ledger.promote("c1", actor=operator, at=START,
                       restriction=restriction_at(), evidence=blank)
    assert set(raised.value.details["missing"]) == set(
        PR.REQUIRED_PROMOTION_EVIDENCE)


def test_an_expired_restriction_cannot_be_promoted_into(tmp_path, operator):
    """A promotion that is already over is not a promotion worth recording."""
    ledger = ledger_at(tmp_path)
    reviewed(ledger, operator)
    stale = PR.default_restriction(instruments=(KEY,), max_notional=10.0,
                                   at=START - timedelta(days=90))
    with pytest.raises(PR.PromotionRefused):
        ledger.promote("c1", actor=operator, at=START, restriction=stale,
                       evidence=REVIEW_EVIDENCE)
    assert ledger.durable_state["c1"]["outcome"] != "promoted"
