"""Phase 5 P5-10/P5-11 — retention policy surface and legacy namespace.

Gates proved here: P5-A11 (retention dry run is accurate), P5-A12 (deletion
removes or tombstones all required derived data), P5-A13 (legacy shared
namespace is not auto-assigned).

The theme throughout: this module ships a MECHANISM and refuses to invent a
POLICY. Tests assert both halves — that the mechanism works, and that the
absence of a policy is reported rather than papered over with a default.
"""

from __future__ import annotations

import json
import os
import shutil
import time

import pytest

from olympus import config, memory, retention, sessionlog


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    monkeypatch.delenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", raising=False)
    monkeypatch.delenv("OLYMPUS_LEGAL_HOLD", raising=False)
    return tmp_path / "mem"


def _make_principal(uid: str, *, turns: int = 3) -> None:
    """A principal with the full derived footprint: notes, snapshot, journal."""
    memory.set_user(uid)
    memory.save("lessons", f"{uid}-note", f"content for {uid}")
    history = []
    for i in range(turns):
        history += [{"role": "user", "content": f"q{i} for {uid}"},
                    {"role": "assistant", "content": f"a{i} for {uid}"}]
    memory.save_conversation(uid, history)
    memory.set_user("shared")


# ══════════════════════════════════════════════════════════════════════════
# policy: unset is a REPORTED state, not a silent default
# ══════════════════════════════════════════════════════════════════════════

def test_unset_policy_is_distinct_from_zero(store, monkeypatch):
    """Returning a default would invent a legal position; returning 0 would
    silently start deleting user content. Neither is ours to choose."""
    assert retention.conversation_retain_days() is None
    assert retention.policy_status() == "unset"

    monkeypatch.setenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", "forever")
    assert retention.conversation_retain_days() == 0
    assert retention.policy_status() == "forever"

    monkeypatch.setenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", "30")
    assert retention.conversation_retain_days() == 30
    assert retention.policy_status() == "30 days"


def test_an_unparseable_policy_reads_as_unset_not_as_a_guess(store,
                                                             monkeypatch):
    for bad in ("thirty", "-1", "3.5", ""):
        monkeypatch.setenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", bad)
        days = retention.conversation_retain_days()
        assert days in (None, 0), f"{bad!r} was interpreted as {days}"
        if days is None:
            assert retention.deployment_blocked_reason()


def test_no_policy_blocks_regulated_use_and_says_why(store):
    reason = retention.deployment_blocked_reason()
    assert reason
    assert "OLYMPUS_CONVERSATION_RETAIN_DAYS" in reason
    assert "regulated" in reason
    # …and setting it either way clears the block, because both are decisions
    for value in ("30", "forever"):
        os.environ["OLYMPUS_CONVERSATION_RETAIN_DAYS"] = value
        try:
            assert retention.deployment_blocked_reason() == ""
        finally:
            del os.environ["OLYMPUS_CONVERSATION_RETAIN_DAYS"]


def test_the_sweep_does_nothing_without_a_policy(store):
    """The most important negative: no policy must never mean 'delete'."""
    _make_principal("alice")
    out = retention.sweep_conversations(dry_run=False)
    assert out["removed"] == 0
    assert out["skipped_reason"]
    assert retention.inspect_principal("alice")["exists"]


def test_forever_is_an_explicit_no_op(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", "forever")
    _make_principal("alice")
    out = retention.sweep_conversations(dry_run=False)
    assert out["removed"] == 0 and "forever" in out["skipped_reason"]
    assert retention.inspect_principal("alice")["exists"]


# ══════════════════════════════════════════════════════════════════════════
# P5-A11 — the dry run is accurate
# ══════════════════════════════════════════════════════════════════════════

def test_dry_run_is_the_default_for_deletion(store):
    """A destructive default is one mis-click from an incident."""
    import inspect
    sig = inspect.signature(retention.delete_principal)
    assert sig.parameters["dry_run"].default is True
    sig2 = inspect.signature(retention.sweep_conversations)
    assert sig2.parameters["dry_run"].default is True


def test_dry_run_changes_nothing(store):
    _make_principal("alice")
    before = retention.inspect_principal("alice")
    plan = retention.delete_principal("alice")           # dry run
    after = retention.inspect_principal("alice")
    assert plan["dry_run"] is True
    assert after["files"] == before["files"] > 0
    assert after["bytes"] == before["bytes"]
    assert plan["deleted"] == []


def test_the_dry_run_predicts_exactly_what_deletion_removes(store):
    """P5-A11 stated precisely: the plan and the outcome must agree, or the
    dry run is decoration."""
    _make_principal("alice")
    predicted = set(retention.delete_principal("alice")["paths"])
    assert predicted
    actual = set(retention.delete_principal(
        "alice", dry_run=False, reason="test")["deleted"])
    assert actual == predicted, (
        f"dry run promised {predicted}, deletion removed {actual}")


def test_the_sweep_dry_run_lists_candidates_without_deleting(store,
                                                             monkeypatch):
    monkeypatch.setenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", "1")
    _make_principal("stale")
    conv = config.MEMORY_DIR / "conversations" / "stale.json"
    old = time.time() - 10 * 86400
    os.utime(conv, (old, old))

    out = retention.sweep_conversations(dry_run=True)
    assert [c["principal"] for c in out["candidates"]] == ["stale"]
    assert out["removed"] == 0
    assert retention.inspect_principal("stale")["exists"]

    out2 = retention.sweep_conversations(dry_run=False)
    assert out2["removed"] == 1
    assert not retention.inspect_principal("stale")["exists"]


# ══════════════════════════════════════════════════════════════════════════
# P5-A12 — deletion removes ALL derived data
# ══════════════════════════════════════════════════════════════════════════

def test_deletion_removes_every_derived_store(store):
    """Deleting the snapshot but leaving the sealed journal is not deletion —
    the journal replays the whole conversation back."""
    _make_principal("alice", turns=4)
    uid = memory.safe_id("alice")
    assert (config.MEMORY_DIR / "conversations" / f"{uid}.json").exists()
    assert (config.MEMORY_DIR / "sessions" / f"{uid}.journal.jsonl").exists()
    assert (config.MEMORY_DIR / "users" / uid).exists()

    out = retention.delete_principal("alice", dry_run=False, reason="rtbf")
    assert out["errors"] == [], out["errors"]
    assert out["verified"] is True
    assert not (config.MEMORY_DIR / "conversations" / f"{uid}.json").exists()
    assert not (config.MEMORY_DIR / "sessions" / f"{uid}.journal.jsonl").exists()
    assert not (config.MEMORY_DIR / "users" / uid).exists()
    # and the content is genuinely unrecoverable from the journal
    assert sessionlog.recover_history("alice") in (None, [])


def test_deletion_is_verified_not_assumed(store):
    """Step 13's rule applied to deletion. `verified` must be computed from the
    filesystem after the fact, not set optimistically."""
    _make_principal("bob")
    assert retention.verify_deleted("bob") is False
    out = retention.delete_principal("bob", dry_run=False)
    assert out["verified"] is True
    assert retention.verify_deleted("bob") is True


def test_deletion_tombstones_the_journal_before_removing_it(store):
    """If the process dies between tombstone and unlink, the journal must say
    the deletion was INTENDED rather than looking corrupted."""
    import inspect
    src = inspect.getsource(retention.delete_principal)
    i_tomb = src.find("append_tombstone")
    i_unlink = src.find("shutil.rmtree")
    assert i_tomb != -1 and i_unlink != -1 and i_tomb < i_unlink, (
        "the journal is removed before it is tombstoned")


def test_deleting_one_principal_leaves_another_intact(store):
    """P5-A14 in the deletion path: a right-to-be-forgotten request must not
    take a neighbour's data with it."""
    _make_principal("alice")
    _make_principal("bob")
    retention.delete_principal("alice", dry_run=False)
    assert not retention.inspect_principal("alice")["exists"]
    assert retention.inspect_principal("bob")["exists"]
    memory.set_user("bob")
    assert "content for bob" in memory.search("content", limit=5)
    memory.set_user("shared")


def test_legal_hold_outranks_deletion(store, monkeypatch):
    _make_principal("held")
    monkeypatch.setenv("OLYMPUS_LEGAL_HOLD", "held")
    out = retention.delete_principal("held", dry_run=False, reason="rtbf")
    assert out["refused"], "a legal hold did not stop the deletion"
    assert "legal hold" in out["refused"]
    assert retention.inspect_principal("held")["exists"]


def test_legal_hold_also_exempts_the_sweep(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", "1")
    monkeypatch.setenv("OLYMPUS_LEGAL_HOLD", "held")
    _make_principal("held")
    conv = config.MEMORY_DIR / "conversations" / "held.json"
    old = time.time() - 30 * 86400
    os.utime(conv, (old, old))
    out = retention.sweep_conversations(dry_run=False)
    assert out["held"] == ["held"]
    assert retention.inspect_principal("held")["exists"]


def test_every_deletion_is_audited(store):
    _make_principal("alice")
    retention.delete_principal("alice")                       # dry run
    retention.delete_principal("alice", dry_run=False, reason="rtbf request")
    events = [r["event"] for r in retention.audit_log()]
    assert "delete_dry_run" in events and "delete" in events
    rec = [r for r in retention.audit_log() if r["event"] == "delete"][0]
    assert rec["reason"] == "rtbf request" and rec["verified"] is True


def test_the_audit_log_is_append_only_and_skips_corruption(store):
    _make_principal("alice")
    retention.delete_principal("alice")
    p = config.MEMORY_DIR / "retention" / "audit.jsonl"
    first = p.read_text(encoding="utf-8")
    with open(p, "a", encoding="utf-8") as f:
        f.write("{corrupt\n")
    retention.delete_principal("alice")
    assert p.read_text(encoding="utf-8").startswith(first)
    assert len(retention.audit_log()) == 2       # the corrupt line is skipped


# ══════════════════════════════════════════════════════════════════════════
# P5-A13 — the legacy namespace is never auto-assigned
# ══════════════════════════════════════════════════════════════════════════

def test_legacy_inspection_explains_why_it_cannot_be_attributed(store):
    _make_principal(retention.LEGACY_PRINCIPAL)
    info = retention.inspect_legacy()
    assert info["exists"] is True
    assert "COMMINGLED" in info["why_it_exists"]
    assert "adopt" in info["unsafe_operation"]
    assert set(info["safe_operations"]) == {"inspect", "export", "quarantine",
                                            "delete"}


def test_adoption_without_the_acknowledgement_is_refused(store):
    _make_principal(retention.LEGACY_PRINCIPAL)
    for ack in ("", "yes", "I agree", retention.ADOPTION_ACK[:-5]):
        with pytest.raises(retention.LegacyAdoptionRefused) as exc:
            retention.adopt_legacy("alice", acknowledgement=ack,
                                   dry_run=False)
        assert "acknowledgement" in str(exc.value)
    # the data is untouched
    assert retention.inspect_principal(retention.LEGACY_PRINCIPAL)["exists"]
    assert not retention.inspect_principal("alice")["exists"]


def test_a_refused_adoption_is_audited(store):
    _make_principal(retention.LEGACY_PRINCIPAL)
    with pytest.raises(retention.LegacyAdoptionRefused):
        retention.adopt_legacy("alice", acknowledgement="nope")
    assert any(r["event"] == "legacy_adopt_refused"
               for r in retention.audit_log())


def test_adoption_with_the_acknowledgement_works_and_is_audited(store):
    _make_principal(retention.LEGACY_PRINCIPAL)
    out = retention.adopt_legacy("alice",
                                 acknowledgement=retention.ADOPTION_ACK,
                                 dry_run=False)
    assert out["adopted"] is True
    assert retention.inspect_principal("alice")["exists"]
    assert any(r["event"] == "legacy_adopt" for r in retention.audit_log())


def test_no_code_path_adopts_the_legacy_namespace_automatically():
    """P5-A13 structurally: nothing outside the retention module and its tests
    may call adopt_legacy at all, and nothing may embed the acknowledgement."""
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    for py in (repo / "olympus").glob("*.py"):
        if py.name == "retention.py":
            continue
        text = py.read_text(encoding="utf-8")
        assert "adopt_legacy" not in text, (
            f"{py.name} calls adopt_legacy — adoption must be an operator "
            f"action, never a code path")
        assert retention.ADOPTION_ACK not in text, (
            f"{py.name} embeds the adoption acknowledgement, which defeats it")


def test_quarantine_is_reversible(store):
    """The recommended default action: it removes the cross-principal exposure
    immediately and keeps the data for a considered decision."""
    _make_principal(retention.LEGACY_PRINCIPAL)
    dry = retention.quarantine_legacy()
    assert dry["moved"] is False
    assert retention.inspect_principal(retention.LEGACY_PRINCIPAL)["exists"]

    out = retention.quarantine_legacy(dry_run=False)
    assert out["moved"] is True
    assert not (config.MEMORY_DIR / "users" /
                retention.LEGACY_PRINCIPAL).exists()

    back = retention.restore_legacy_quarantine()
    assert back["restored"] is True
    assert (config.MEMORY_DIR / "users" / retention.LEGACY_PRINCIPAL).exists()


def test_legacy_export_is_non_destructive(store, tmp_path):
    _make_principal(retention.LEGACY_PRINCIPAL)
    out = retention.export_legacy(tmp_path / "out")
    assert out["exported"] is True and out["bytes"] > 0
    assert retention.inspect_principal(retention.LEGACY_PRINCIPAL)["exists"]


def test_legacy_can_simply_be_deleted(store):
    _make_principal(retention.LEGACY_PRINCIPAL)
    out = retention.delete_principal(retention.LEGACY_PRINCIPAL,
                                     dry_run=False, reason="phase5 cleanup")
    assert out["verified"] is True


# ══════════════════════════════════════════════════════════════════════════
# the report
# ══════════════════════════════════════════════════════════════════════════

def test_the_report_is_read_only_by_default(store):
    _make_principal("alice")
    rep = retention.report()
    assert rep["conversation_policy"] == "unset"
    assert rep["deployment_blocked"]
    assert rep["conversation_sweep"]["dry_run"] is True
    assert retention.inspect_principal("alice")["exists"]
    assert json.dumps(rep, default=str)          # serialisable for the report


# ══════════════════════════════════════════════════════════════════════════
# Wave-0 hardening: deletion completeness across ALL substrates
#
# The original fixture seeded only notes + snapshot + journal, so the suite
# could not see that `delete_principal` walked filesystem paths only and left
# every KV-backed store behind while still reporting verified=True. These tests
# seed the substrates that were actually being missed.
# ══════════════════════════════════════════════════════════════════════════

def _seed_kv_footprint(uid: str) -> None:
    """Typed memories, relationship graph, embeddings and routing rows —
    everything that lives behind `kvstore.backend()` rather than in a file."""
    from olympus import store as kvstore
    safe = memory.safe_id(uid)
    for ns in retention._DERIVED_KV_NAMESPACES:
        kvstore.backend().put(ns, safe, b'[{"text": "sensitive-canary"}]')


def test_inspect_reports_kv_and_index_footprint(store, monkeypatch):
    from olympus import search
    _make_principal("kvuser")
    _seed_kv_footprint("kvuser")
    search.index_conversation(memory.safe_id("kvuser"),
                              [{"role": "user", "content": "findable canary"}])
    info = retention.inspect_principal("kvuser")
    assert info["exists"]
    assert set(info["kv_namespaces"]) == set(retention._DERIVED_KV_NAMESPACES)
    assert info["indexed_turns"] > 0


def test_delete_principal_removes_kv_backed_stores(store):
    from olympus import store as kvstore
    _make_principal("kvuser")
    _seed_kv_footprint("kvuser")
    safe = memory.safe_id("kvuser")

    plan = retention.delete_principal("kvuser", dry_run=False, reason="rtbf")

    assert plan["verified"] is True, plan
    for ns in retention._DERIVED_KV_NAMESPACES:
        assert kvstore.backend().get(ns, safe) is None, f"{ns} survived deletion"


def test_delete_principal_purges_the_search_index(store):
    from olympus import search
    _make_principal("kvuser")
    safe = memory.safe_id("kvuser")
    search.index_conversation(safe, [{"role": "user", "content": "canary-text"}])
    assert search.indexed_turns(safe) > 0

    retention.delete_principal("kvuser", dry_run=False, reason="rtbf")

    assert search.indexed_turns(safe) == 0


def test_verify_deleted_is_false_while_typed_memories_remain(store):
    """The exact false-assurance from the audit, asserted directly."""
    from olympus import store as kvstore
    _make_principal("kvuser")
    safe = memory.safe_id("kvuser")
    kvstore.backend().put("usermem.memories", safe, b'[{"text": "still here"}]')

    # Remove only the filesystem footprint, as the old implementation did.
    for path in retention._paths_for(safe):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    assert retention.verify_deleted("kvuser") is False


def test_dry_run_still_touches_nothing_in_the_kv_store(store):
    from olympus import store as kvstore
    _make_principal("kvuser")
    _seed_kv_footprint("kvuser")
    safe = memory.safe_id("kvuser")

    plan = retention.delete_principal("kvuser", dry_run=True)

    assert plan["dry_run"] is True
    for ns in retention._DERIVED_KV_NAMESPACES:
        assert kvstore.backend().get(ns, safe) is not None


def test_deletion_leaves_other_principals_kv_data_intact(store):
    from olympus import store as kvstore
    _make_principal("victim")
    _make_principal("bystander")
    _seed_kv_footprint("victim")
    _seed_kv_footprint("bystander")

    retention.delete_principal("victim", dry_run=False, reason="rtbf")

    other = memory.safe_id("bystander")
    for ns in retention._DERIVED_KV_NAMESPACES:
        assert kvstore.backend().get(ns, other) is not None, f"{ns} lost for bystander"
