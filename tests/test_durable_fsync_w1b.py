"""W1-1b: the remaining durable publish sites.

Same contract and same tracing helpers as `test_durable_fsync.py`, applied to
the 19 sites W1-1 did not cover. Split into a second file only because the two
PRs are reviewed separately; the shape is identical.

Two directions are asserted here, and the split is the point of W1-1b:

* **Group C (16 sites)** — fsync before replace, like every W1-1 site.
* **Group B (2 sites)** — atomic publish and NO fsync, because each sits on a
  per-model-call / per-tool-call locked read-modify-write. W1-1 shipped a
  per-call fsync on exactly that shape (`usage.record`) and broke the
  observability wall-clock contract on windows-py3.12. These tests are the
  inverse guard for those telemetry-only sites. M07 moves context heat into a
  durable unified authority with pin/gate receipts; its barrier is now required.

Group A retains the three structural quarantine-move guards below. M11
restore now publishes flushed copies and retains immutable recovery images;
its positive and fault controls replace the obsolete backup move exemption.
"""

from __future__ import annotations

import json
import os

import pytest

from olympus import (agentbeat, backup, connectors, ctxbudget, ctxheat, facts,
                     modelgate, modelgrade, pairing, pluginstore, scheduler,
                     skills, toolcall_repair, watchdog, webmonitor,
                     webproposals, webreflect)
from test_durable_fsync import assert_data_synced_before_replace, trace


def assert_published_without_fsync(events: list, *, label: str) -> None:
    """The Group B contract: atomic, and deliberately NOT durable.

    Atomicity is still required — a reader in another process must never see a
    torn file. Only the sync is given up, and only where the write sits on a
    per-call locked path.
    """
    kinds = [e[0] for e in events]
    assert "replace" in kinds, f"{label}: never published atomically"
    first_replace = kinds.index("replace")
    synced = [e for e in events[:first_replace] if e[0] == "fsync"]
    assert not synced, (
        f"{label}: fsynced before replace, but this site is on a per-call "
        f"locked read-modify-write and must NOT sync — that is the W1-1a "
        f"regression. events={events}")


# =========================================================================
# Group B — hot paths. Atomic, no fsync, no knob.
# =========================================================================

def test_ctxbudget_save_does_not_fsync(monkeypatch):
    """`observe()` reaches `_save` on EVERY model call (llm.py:389/483,
    openai_compat.py:187) under a lock. It is the `ctx` component of the very
    benchmark W1-1 broke."""
    events = trace(monkeypatch)
    ctxbudget._save({"ratios": {}})
    assert_published_without_fsync(events, label="ctxbudget._save")


def test_toolcall_repair_record_does_not_fsync(monkeypatch):
    """Runs per repaired tool call inside `proclock.lock("repair-stats")`.
    `usage.py` groups this with `ctxbudget.observe` as one contract."""
    events = trace(monkeypatch)
    toolcall_repair.record_repair("anthropic", "claude-opus-5", "web_fetch",
                                  1, "recovered")
    assert_published_without_fsync(events, label="record_repair")


def test_ctxheat_authority_syncs_before_atomic_publication(monkeypatch, tmp_path):
    """M07 receipts share authority with heat; durable publication is required."""
    events = trace(monkeypatch)
    monkeypatch.setenv('OLYMPUS_CTXHEAT','shadow')
    if os.name == 'nt':
        from olympus import gallery_windows
        real=gallery_windows.rename_same_directory
        def rename(handle,name):
            events.append(('replace','held-staging-handle',name))
            return real(handle,name)
        monkeypatch.setattr(gallery_windows,'rename_same_directory',rename)
    assert ctxheat.record('owned-item','wiki',retrieved=True)
    assert_data_synced_before_replace(events,label='ctxheat unified authority')


def test_group_b_still_publishes_atomically(monkeypatch, tmp_path):
    """Giving up the sync must not give up atomicity: the file still lands
    whole, via a replace, never a partial in-place write."""
    ctxbudget._save({"ratios": {"x": 1}})
    assert json.loads(ctxbudget._cal_path().read_text(encoding="utf-8")) \
        == {"ratios": {"x": 1}}


# =========================================================================
# Group C — everything else. fsync before replace.
# =========================================================================

def test_agentbeat_save_syncs(monkeypatch):
    """Per agent-beat mutation (add/prune/tick) — heartbeat cadence."""
    events = trace(monkeypatch)
    agentbeat._save([])
    assert_data_synced_before_replace(events, label="agentbeat._save")


def test_connectors_write_syncs(monkeypatch, tmp_path):
    """Per operator action (`add_mcp_server`)."""
    events = trace(monkeypatch)
    connectors._atomic_write(tmp_path / "connectors.json", '{"servers": []}')
    assert_data_synced_before_replace(events, label="connectors._atomic_write")


def test_facts_trim_syncs(monkeypatch):
    """Amortised compaction: fires only when the append log passes
    2 x MAX_FACTS (5000), i.e. ~1 write per 5000 appends."""
    path = facts._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps({"claim": str(i), "verdict": "y", "source": "",
                    "ts": 0}) + "\n" for i in range(5)), encoding="utf-8")
    events = trace(monkeypatch)
    facts._trim(path)
    assert_data_synced_before_replace(events, label="facts._trim")


def test_modelgate_write_baseline_syncs(monkeypatch):
    """Per gate run (heartbeat cadence / operator)."""
    events = trace(monkeypatch)
    modelgate._write_baseline("anthropic/claude-opus-5", {"score": 8.0})
    assert_data_synced_before_replace(events, label="modelgate._write_baseline")


def test_modelgate_write_freeze_syncs(monkeypatch):
    """A FREEZE is a safety control: a lost write silently un-freezes a member,
    exactly like `identity.revoke`. Unconditional fsync."""
    events = trace(monkeypatch)
    modelgate._write_freeze("aletheia", "high", "drift detected", "ref-1")
    assert_data_synced_before_replace(events, label="modelgate._write_freeze")


def test_modelgate_unfreeze_syncs(monkeypatch):
    """Operator action; the same safety-control argument in reverse."""
    modelgate._write_freeze("aletheia", "high", "drift", "ref-1")
    events = trace(monkeypatch)
    assert modelgate.unfreeze("aletheia") is True
    assert_data_synced_before_replace(events, label="modelgate.unfreeze")


def test_modelgrade_rebuild_syncs(monkeypatch):
    """Only on a missing / stale-schema / corrupt cards.json — a cache repair,
    not a steady-state write."""
    monkeypatch.setenv("OLYMPUS_MODELGRADE", "on")
    events = trace(monkeypatch)
    modelgrade.rebuild()
    assert_data_synced_before_replace(events, label="modelgrade.rebuild")


def test_pairing_save_syncs(monkeypatch):
    """Per pairing action (issue / claim / expire) — operator-paced."""
    events = trace(monkeypatch)
    pairing._save({"codes": {}})
    assert_data_synced_before_replace(events, label="pairing._save")


def test_pluginstore_save_manifest_syncs(monkeypatch):
    """Per plugin install / uninstall."""
    events = trace(monkeypatch)
    pluginstore._save_manifest({"plugins": {}})
    assert_data_synced_before_replace(events, label="pluginstore._save_manifest")


def test_scheduler_save_syncs(monkeypatch):
    """Per scheduled-job mutation (add / remove / run)."""
    events = trace(monkeypatch)
    scheduler._save([])
    assert_data_synced_before_replace(events, label="scheduler._save")


def test_skills_save_emb_cache_syncs(monkeypatch):
    """Only on an embedding cache MISS — a skill's text or the embedding model
    changed. A warm cache returns before this write."""
    events = trace(monkeypatch)
    skills._save_emb_cache({"slug": {"hash": "h", "model": "m", "vec": [0.1]}})
    assert_data_synced_before_replace(events, label="skills._save_emb_cache")


def test_webmonitor_save_syncs(monkeypatch):
    """Per monitor mutation — operator-paced."""
    events = trace(monkeypatch)
    webmonitor._save([])
    assert_data_synced_before_replace(events, label="webmonitor._save")


def test_webproposals_save_syncs(monkeypatch):
    """Per discovery cycle (`draft_from_discoveries`) — heartbeat cadence."""
    events = trace(monkeypatch)
    webproposals._save({})
    assert_data_synced_before_replace(events, label="webproposals._save")


def test_webreflect_save_state_syncs(monkeypatch):
    """Per reflection cycle (`run_due`) — heartbeat cadence."""
    events = trace(monkeypatch)
    webreflect._save_state({"seen": [], "last_run": 0.0})
    assert_data_synced_before_replace(events, label="webreflect._save_state")


def test_watchdog_write_forensics_syncs(monkeypatch):
    """Per watchdog anomaly (observe / cancel). Rare by construction, and the
    one write whose whole purpose is surviving whatever just went wrong."""
    state = watchdog.LeaseState(lease_id="L1", now=100.0, created=0.0)
    verdict = watchdog.Verdict(kind="stall", reason="no beat for 412s",
                               operator_action="check the worker")
    events = trace(monkeypatch)
    written = watchdog.write_forensics(state, verdict, outcome="observed")
    assert written is not None, "forensics did not write"
    assert_data_synced_before_replace(events, label="watchdog.write_forensics")


def _durable_backup_fixture(tmp_path, monkeypatch):
    """One existing owned memory root, owned signer and no uploader dispatch."""
    from durable_native_probe import native_path
    from olympus import config, witness, backup_process, backup_signing
    live = tmp_path / 'memory'
    native_path(live).mkdir(parents=True, exist_ok=True)
    payload = b'owned native durability payload\n'
    native_path(live / 'data.txt').write_bytes(payload)
    package = tmp_path / 'owned-package'
    native_path(package).mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', live)
    monkeypatch.setattr(witness, '_package_dir', lambda: package)
    for name in (*backup_signing._NAMES, 'OLYMPUS_SECRET_KEY',
                 'OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_BACKUP_CMD',
                 'OLYMPUS_BACKUP_ALLOW_PLAINTEXT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setenv('OLYMPUS_SIGNING_SEED', 'owned durability signing fixture')
    monkeypatch.setenv('OLYMPUS_BACKUP_ALLOW_PLAINTEXT', '1')
    monkeypatch.setattr(backup_process, 'execute',
                        lambda *a, **k: pytest.fail('durability test must not dispatch an uploader'))
    return live, payload


def _durable_pair_paths(live, operation_id):
    from olympus import backup_publication
    archive = live / 'backups' / backup_publication.archive_name(operation_id, False)
    signature = archive.with_name(archive.name + '.sig.json')
    operation = archive.parent / backup_publication.CONTROL / operation_id
    return archive, signature, operation


def _durable_retained_files(directory):
    """Read only small owned test evidence, never a caller-selected tree."""
    from durable_native_probe import native_path, read_image
    result = {}
    root = native_path(directory)
    for count, path in enumerate(root.rglob('*'), 1):
        assert count <= 128
        if path.is_file():
            result[path.relative_to(root).as_posix()] = read_image(path)
    return result


def _assert_durable_pair(created, archive, signature, operation, payload):
    import hashlib
    from durable_native_probe import read_image
    from olympus import backup_archive
    archive_raw, archive_id = read_image(archive)
    signature_raw, signature_id = read_image(signature)
    assert created['publication_complete'] is True
    assert created['archive_sha256'] == created['sha256'] == hashlib.sha256(archive_raw).hexdigest()
    assert created['signature_sha256'] == hashlib.sha256(signature_raw).hexdigest()
    assert created['signature_ok'] is True and created['signer_authenticated'] is True
    assert created['signer_default'] is False
    retained_archive = read_image(operation / 'archive.image')
    retained_signature = read_image(operation / 'signature.image')
    assert retained_archive[0] == archive_raw and retained_archive[1] != archive_id
    assert retained_signature[0] == signature_raw and retained_signature[1] != signature_id
    verified = backup.verify_archive(str(archive))
    assert verified['signature_ok'] is True and verified['signer_authenticated'] is True
    assert verified['sha256'] == created['sha256']
    manifest, images = backup_archive.decode(archive_raw)
    assert images == {'data.txt': payload} and manifest['file_count'] == 1
    complete = json.loads(read_image(operation / 'complete.json')[0])
    assert complete['operation_id'] == created['operation_id']
    assert complete['publication_complete'] is True


def test_backup_create_syncs(monkeypatch, tmp_path):
    """Both final images use their own successful flush/native rename sequence."""
    import uuid
    from durable_native_probe import NativePublicationProbe
    live, payload = _durable_backup_fixture(tmp_path, monkeypatch)
    operation_id = uuid.uuid4().hex
    archive, signature, operation = _durable_pair_paths(live, operation_id)
    complete = operation / 'complete.json'
    with monkeypatch.context() as patch:
        probe = NativePublicationProbe(patch, 'archive',
                                       {archive: None, signature: None, complete: None})
        created = backup.create(operation_id=operation_id)
    assert created['operation_id'] == operation_id
    probe.assert_final_barrier_before([archive, signature], complete)
    _assert_durable_pair(created, archive, signature, operation, payload)


@pytest.mark.parametrize('component', ['archive', 'signature'])
@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit],
                         ids=['io-error', 'keyboard-interrupt', 'system-exit'])
def test_backup_failed_staging_flush_retains_exact_recovery_images(
        monkeypatch, tmp_path, component, fault_type):
    import uuid
    from durable_native_probe import NativePublicationProbe, native_path, read_image
    from olympus import backup_publication
    live, payload = _durable_backup_fixture(tmp_path, monkeypatch)
    operation_id = uuid.uuid4().hex
    archive, signature, operation = _durable_pair_paths(live, operation_id)
    complete = operation / 'complete.json'
    target = archive if component == 'archive' else signature
    fault = fault_type('owned backup staging flush fault')
    category = backup.BackupError if fault_type is OSError else fault_type
    with monkeypatch.context() as patch:
        probe = NativePublicationProbe(patch, 'archive',
            {archive: None, signature: None, complete: None}, fail_target=target, failure=fault)
        with pytest.raises(category) as caught:
            backup.create(operation_id=operation_id)
    probe.assert_failed_flush(target)
    assert caught.value.operation_id == operation_id
    if fault_type is not OSError:
        assert caught.value is fault
    assert not native_path(target).exists() and not native_path(complete).exists()
    assert all(row['path'] != complete for row in probe.frames)
    if component == 'archive':
        assert not native_path(signature).exists()
    else:
        probe.assert_published(archive)
    retained = {name: read_image(operation / name)
                for name in ('plan.json', 'archive.image', 'signature.image')}
    assert retained['archive.image'][0] and retained['signature.image'][0]
    # This exact-ID settlement uses retained images. It does not create again.
    with monkeypatch.context() as patch:
        resumed_probe = NativePublicationProbe(patch, 'archive',
            {archive: retained['archive.image'][0], signature: retained['signature.image'][0], complete: None})
        resumed = backup_publication.resume(archive.parent, operation_id=operation_id)
    assert resumed['operation_id'] == operation_id and resumed['publication_complete'] is True
    resumed_probe.assert_final_barrier_before([archive, signature], complete)
    assert {name: read_image(operation / name) for name in retained} == retained
    assert read_image(archive)[0] == retained['archive.image'][0]
    assert read_image(signature)[0] == retained['signature.image'][0]
    assert read_image(live / 'data.txt')[0] == payload


def _durable_restore_fixture(tmp_path, monkeypatch):
    import uuid
    from durable_native_probe import native_path
    from olympus import backup_restore
    live, payload = _durable_backup_fixture(tmp_path, monkeypatch)
    created = backup.create(operation_id=uuid.uuid4().hex)
    destination = tmp_path / 'offline'
    first_id = uuid.uuid4().hex
    first = backup.restore(created['path'], into=destination, operation_id=first_id)
    assert first['restore_complete'] is True and first['restored'] == 1
    earlier = destination / backup_restore.CONTROL / first_id
    earlier_images = _durable_retained_files(earlier)
    before = b'owned previous destination bytes\n'
    native_path(destination / 'data.txt').write_bytes(before)
    native_path(destination / 'unrelated.txt').write_bytes(b'owned unrelated bytes')
    operation_id = uuid.uuid4().hex
    operation = destination / backup_restore.CONTROL / operation_id
    return live, payload, created, destination, operation_id, operation, before, earlier, earlier_images


def _assert_restore_images(operation, target, before, after):
    import hashlib
    from durable_native_probe import read_image
    plan = json.loads(read_image(operation / 'plan.json')[0])
    assert len(plan['files']) == 1
    row = plan['files'][0]
    assert row['path'] == 'data.txt'
    assert row['before_sha256'] == hashlib.sha256(before).hexdigest()
    assert row['after_sha256'] == hashlib.sha256(after).hexdigest()
    original = read_image(operation / 'images-000' / '00000.before')
    retained = read_image(operation / 'images-000' / '00000.after')
    final = read_image(target)
    assert original[0] == before and retained[0] == final[0] == after
    assert len({original[1], retained[1], final[1]}) == 3


def test_restore_publishes_flushed_copies_and_preserves_retained_images(monkeypatch, tmp_path):
    """Current restore writes copies; immutable recovery images are never moved."""
    from durable_native_probe import NativePublicationProbe, read_image
    (live, payload, created, destination, operation_id, operation, before,
     earlier, earlier_images) = _durable_restore_fixture(tmp_path, monkeypatch)
    target, complete = destination / 'data.txt', operation / 'complete.json'
    with monkeypatch.context() as patch:
        probe = NativePublicationProbe(patch, 'archive', {target: payload, complete: None})
        result = backup.restore(created['path'], into=destination, force=True, operation_id=operation_id)
    assert result['restore_complete'] is True and result['restored'] == 1
    assert result['operation_id'] == operation_id and result['signature_ok'] is True
    probe.assert_final_barrier_before([target], complete)
    _assert_restore_images(operation, target, before, payload)
    assert _durable_retained_files(earlier) == earlier_images
    assert read_image(destination / 'unrelated.txt')[0] == b'owned unrelated bytes'
    assert read_image(live / 'data.txt')[0] == payload
    assert json.loads(read_image(complete)[0])['operation_id'] == operation_id


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit],
                         ids=['io-error', 'keyboard-interrupt', 'system-exit'])
def test_restore_failed_staging_flush_keeps_before_and_resumes_copies(monkeypatch, tmp_path, fault_type):
    from durable_native_probe import NativePublicationProbe, native_path, read_image
    (live, payload, created, destination, operation_id, operation, before,
     earlier, earlier_images) = _durable_restore_fixture(tmp_path, monkeypatch)
    target, complete = destination / 'data.txt', operation / 'complete.json'
    before_image = read_image(target)
    fault = fault_type('owned restore staging flush fault')
    category = backup.BackupError if fault_type is OSError else fault_type
    with monkeypatch.context() as patch:
        probe = NativePublicationProbe(patch, 'archive', {target: payload, complete: None},
                                       fail_target=target, failure=fault)
        with pytest.raises(category) as caught:
            backup.restore(created['path'], into=destination, force=True, operation_id=operation_id)
    probe.assert_failed_flush(target)
    assert caught.value.operation_id == operation_id
    if fault_type is not OSError:
        assert caught.value is fault
    assert read_image(target) == before_image and not native_path(complete).exists()
    assert all(row['path'] != complete for row in probe.frames)
    retained = {name: read_image(operation / name) for name in
                ('plan.json', 'images-000/00000.before', 'images-000/00000.after')}
    assert retained['images-000/00000.before'][0] == before
    assert retained['images-000/00000.after'][0] == payload
    assert _durable_retained_files(earlier) == earlier_images
    with monkeypatch.context() as patch:
        resumed_probe = NativePublicationProbe(patch, 'archive', {target: payload, complete: None})
        resumed = backup.resume_restore(destination, operation_id=operation_id)
    assert resumed['restore_complete'] is True and resumed['operation_id'] == operation_id
    resumed_probe.assert_final_barrier_before([target], complete)
    assert {name: read_image(operation / name) for name in retained} == retained
    _assert_restore_images(operation, target, before, payload)
    assert _durable_retained_files(earlier) == earlier_images
    assert read_image(destination / 'unrelated.txt')[0] == b'owned unrelated bytes'
    assert read_image(live / 'data.txt')[0] == payload


# =========================================================================
# Group A — deliberately unconverted. Pinned so the exemption stays visible.
# =========================================================================

GROUP_A = [
    ("olympus/ctxbudget.py", "_quarantine a corrupt calibration file"),
    ("olympus/modelgrade.py", "quarantine corrupt promotion evidence"),
    ("olympus/toolcall_repair.py", "move corrupt repair telemetry aside"),
]


def test_group_a_sites_remain_plain_os_replace():
    """These are `os.replace(existing, dest)` — a MOVE of a file that already
    exists, with no data to write. `atomicio.publish(tmp, path, data)` has no
    meaning for them and would corrupt them, so they keep the bare call.

    Pinned as a test so the exemption is a recorded decision rather than an
    oversight the next sweep silently "fixes".

    Two assertions, because either alone has a hole:

    * **Exactly one** `os.replace(` per file, not merely one present. A
      presence check passes on any number >= 1, so a new UNCONVERTED publish
      landing in one of these files would sail through.
    * **The survivor is the MOVE form**, i.e. its first argument is not a temp
      file. Count alone does not close the case this test exists for: convert
      the Group A quarantine AND add an unconverted publish to the same file
      and the count is still 1. The form check is what catches that, because
      every publish site in W1-1/W1-1b passed `tmp` (or `tmp_final`) first
      while every Group A move passes an already-existing path.
    """
    import re
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    call = re.compile(r"os\.replace\(\s*([A-Za-z_][A-Za-z0-9_]*)")
    for rel, why in GROUP_A:
        text = (repo / rel).read_text(encoding="utf-8")
        hits = [(i, ln.strip())
                for i, ln in enumerate(text.splitlines(), 1)
                if "os.replace(" in ln]
        assert len(hits) == 1, (
            f"{rel}: expected exactly 1 os.replace( — the Group A move "
            f"({why}) — but found {len(hits)}. "
            f"Lines: {[f'{i}: {s}' for i, s in hits]}. "
            f"A NEW publish site here must use atomicio.publish; a Group A "
            f"move must stay a bare os.replace.")
        line_no, src = hits[0]
        first_arg = call.search(src)
        assert first_arg is not None, (
            f"{rel}:{line_no}: could not parse the os.replace call: {src!r}")
        assert not first_arg.group(1).startswith("tmp"), (
            f"{rel}:{line_no}: the surviving os.replace publishes a TEMP file "
            f"({src!r}) — that is an unconverted publish, not the Group A "
            f"move ({why}). Convert it with atomicio.publish.")
