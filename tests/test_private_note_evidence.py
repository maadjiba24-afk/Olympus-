"""M04 adversarial storage + real consumer/approval integration.

Owned local fixtures only. Process tests require POSIX; native Windows exercises
its supported threaded topology and extended-path file APIs separately.
"""
import concurrent.futures
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from olympus import actions, builtin_actions, config, journey, memory
from olympus import note_archive as archives, note_evidence as notes
from olympus.owner_evidence import OwnerEvidenceStateError


@pytest.fixture(autouse=True)
def own_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "state")
    monkeypatch.delenv("OLYMPUS_VAULT_DIR", raising=False)
    with memory.user_context("shared"):
        yield


def save(owner, title="marker", content="private", category="lessons"):
    with memory.user_context(owner):
        return memory.save(category, title, content)


def pair():
    return save("a.b", "first"), save("a.b", "second")


@pytest.mark.parametrize("category", ["lessons", "corrections", "feedback", "action_notes"])
@pytest.mark.parametrize("owners", [("a.b", "a@b"), ("Alice", "alice"),
                                   ("é", "e\u0301"), ("x"*100+"a", "x"*100+"b")])
def test_exact_owner_collision_pairs(category, owners):
    paths = [notes.create(owner, category, "secret" + str(i), "content")
             for i, owner in enumerate(owners)]
    assert paths[0] != paths[1]
    for i, owner in enumerate(owners):
        rows = notes.notes(owner, category)
        assert len(rows) == 1
        assert rows[0]["path"] == notes.logical(paths[i])
        assert rows[0]["owner"] == owner
        assert "secret" + str(i) in rows[0]["body"]
        assert "secret" + str(1-i) not in rows[0]["body"]


@pytest.mark.parametrize("owner", ["", None, "   ", "shared"])
def test_canonical_blank_boundary_and_shared_reading(owner):
    save(owner, "sharedmark")
    assert "sharedmark" in memory.search_for("other", "sharedmark")
    assert len(notes.notes("shared", "lessons")) == 1


@pytest.mark.parametrize("category", ["lessons", "corrections", "feedback", "action_notes"])
def test_legacy_never_claimed_by_either_collider(category):
    old = (config.MEMORY_DIR / "notes" / "a-b" if category == "action_notes" else
           config.MEMORY_DIR / "users" / "a-b" / category)
    old.mkdir(parents=True)
    original = b"# mixed old history\n\nNeither owner may claim this.\n"
    (old / "old.md").write_bytes(original)
    for owner in ("a.b", "a@b", "a-b"):
        with pytest.raises(OwnerEvidenceStateError, match="unclaimed legacy"):
            notes.create(owner, category, "new", "data")
        with pytest.raises(ValueError, match="acknowledgement"):
            notes.initialize(owner, category)
        result = notes.initialize(owner, category, acknowledge_legacy=True)
        assert result["legacy_unclaimed"][0]["sha256"] == notes.digest(original)
        assert notes.notes(owner, category) == []
        notes.create(owner, category, owner, "new")
    assert (old / "old.md").read_bytes() == original


@pytest.mark.parametrize("damage", ["utf8", "digest", "owner", "duplicate", "version", "empty", "oversize"])
def test_bad_notes_are_unavailable_to_all_read_surfaces(damage):
    path = save("a.b")
    raw = path.read_bytes()
    replacements = {
        "utf8": b"\xff", "digest": raw.replace(b"private", b"changed"),
        "owner": raw.replace(b'owner_json: "a.b"', b'owner_json: "a@b"'),
        "duplicate": raw.replace(b"schema_version: 2", b"schema_version: 2\nschema_version: 2"),
        "version": raw.replace(b"schema_version: 2", b"schema_version: 900"),
        "empty": b"", "oversize": b"a" * (notes.MAX_NOTE + 1),
    }
    path.write_bytes(replacements[damage])
    with memory.user_context("a.b"):
        for reader in (lambda: memory.recent("lessons"), lambda: memory.search("private"),
                       lambda: memory.category_count("lessons"), lambda: memory.recent_titles("lessons"),
                       lambda: journey.entries("a.b")):
            with pytest.raises(OwnerEvidenceStateError):
                reader()
    assert path.read_bytes() == replacements[damage]


@pytest.mark.parametrize("kind", ["file", "parent", "fifo"])
def test_nonregular_note_paths_preserve_external_target(tmp_path, kind):
    external = tmp_path / "outside"
    external.mkdir()
    victim = external / "victim.md"
    victim.write_bytes(b"outside bytes")
    root = notes.directory("a.b", "lessons")
    root.parent.mkdir(parents=True)
    try:
        if kind == "parent":
            root.symlink_to(external, target_is_directory=True)
        else:
            root.mkdir()
            if kind == "fifo":
                if not hasattr(os, "mkfifo"):
                    pytest.skip("FIFO requires POSIX")
                os.mkfifo(root / "fifo.md")
            else:
                (root / "victim.md").symlink_to(victim)
    except OSError as error:
        pytest.skip("symlink creation unavailable: " + str(error))
    with pytest.raises(OwnerEvidenceStateError):
        notes.notes("a.b", "lessons")
    assert victim.read_bytes() == b"outside bytes"


def test_unique_concurrent_writes_and_context_restoration():
    def worker(i):
        owner = ("a.b", "a@b")[i % 2]
        with memory.user_context("outer"):
            path = save(owner, "same title", "item" + str(i))
            assert memory.current_owner() == "outer"
            return str(path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(worker, range(24)))
    assert len(set(paths)) == 24
    for owner in ("a.b", "a@b"):
        assert len(notes.notes(owner, "lessons")) == 12


@pytest.mark.skipif(os.name != "posix", reason="cross-process note locking requires POSIX; Windows single-process topology remains")
def test_separate_process_writers_preserve_every_note():
    code = '''from pathlib import Path
import sys
from olympus import config, memory
config.MEMORY_DIR = Path(sys.argv[1])
with memory.user_context("a.b"):
    for i in range(5):
        memory.save("lessons", "same time", sys.argv[2] + str(i))
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    children = [subprocess.Popen([sys.executable, "-B", "-c", code,
                                 str(config.MEMORY_DIR), str(i)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for i in range(3)]
    for child in children:
        out, err = child.communicate(timeout=30)
        assert child.returncode == 0, (out, err)
    assert len(notes.notes("a.b", "lessons")) == 15


@pytest.mark.parametrize("decision", ["resume", "rollback"])
@pytest.mark.parametrize("fail_at", ["plan", "active", "first", "second", "terminal", "unlink"])
def test_interrupted_mutation_all_publication_phases(monkeypatch, fail_at, decision):
    a, b = pair()
    originals = {notes.relative(p): p.read_bytes() for p in (a, b)}
    changes = {name: raw + b"\n" for name, raw in originals.items()}
    expected = {name: notes.digest(raw) for name, raw in originals.items()}
    publish, remove = notes.publish, notes.remove
    hit = []
    names = sorted(changes)
    def breaking_publish(path, raw):
        rel = notes.relative(path)
        selected = ((fail_at == "plan" and path.name == "plan.json") or
                    (fail_at == "active" and path == notes._active()) or
                    (fail_at == "terminal" and path.name == "result.json") or
                    (fail_at == "first" and rel == names[0]) or
                    (fail_at == "second" and rel == names[1]))
        publish(path, raw)
        if selected and not hit:
            hit.append(rel)
            raise OSError("injected after durable publication")
    def breaking_remove(path):
        if fail_at == "unlink" and path == notes._active() and not hit:
            hit.append("unlink")
            # Fail before unlink; completed terminal still requires acknowledgement.
            raise OSError("injected unlink barrier")
        remove(path)
    with monkeypatch.context() as m:
        m.setattr(notes, "publish", breaking_publish)
        m.setattr(notes, "remove", breaking_remove)
        with pytest.raises(OwnerEvidenceStateError):
            notes.transact(changes, expected)
    assert hit
    state = notes.recovery_status()
    if fail_at == "plan":
        assert state["state"] == "available"
        assert {name: notes.checked_relative(name).read_bytes() for name in originals} == originals
        return
    assert state["state"] == "unavailable"
    with pytest.raises(OwnerEvidenceStateError, match="interrupted"):
        memory.search("anything")
    identity = state["active_transaction"]
    if fail_at in ("terminal", "unlink") and decision == "rollback":
        with pytest.raises(OwnerEvidenceStateError, match="terminal"):
            notes.recover(identity, decision)
        decision = "resume"
    receipt = notes.recover(identity, decision)
    assert receipt["verified"]
    target = changes if decision == "resume" else originals
    assert {name: notes.checked_relative(name).read_bytes() for name in target} == target
    assert notes.recover(identity, decision)["already_completed"]
    notes.checked_relative(names[0]).write_bytes(b"new independent work")
    with pytest.raises(OwnerEvidenceStateError, match="since changed"):
        notes.recover(identity, decision)


def test_recovery_checks_every_target_before_overwriting_any(monkeypatch):
    a, b = pair()
    changes = {notes.relative(p): p.read_bytes() + b"\n" for p in (a, b)}
    expected = {notes.relative(p): notes.digest(p.read_bytes()) for p in (a, b)}
    original_finish = notes._finish
    monkeypatch.setattr(notes, "_finish", lambda *a: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OwnerEvidenceStateError):
        notes.transact(changes, expected)
    monkeypatch.setattr(notes, "_finish", original_finish)
    a_before = a.read_bytes()
    b.write_bytes(b"other operator work")
    tx = notes.recovery_status()["active_transaction"]
    with pytest.raises(OwnerEvidenceStateError, match="target changed"):
        notes.recover(tx, "resume")
    assert a.read_bytes() == a_before
    assert b.read_bytes() == b"other operator work"


def _tar(path, rows, *, scope=None, mutate=None, extra=(), version=2):
    manifest = {"schema_version": version, "scope": scope or {"user": "a.b"},
                "files": [{"path": name, "bytes": len(raw), "sha256": notes.digest(raw)}
                          for name, raw in rows]}
    if mutate:
        mutate(manifest)
    with tarfile.open(path, "w:gz") as tar:
        for name, raw in [("manifest.json", json.dumps(manifest).encode()),
                          *(("data/"+name, raw) for name, raw in rows), *extra]:
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return path


@pytest.mark.parametrize("bad", ["checksum", "size", "duplicate", "extra", "owner", "scope", "version"])
def test_invalid_second_archive_member_changes_nothing(tmp_path, bad):
    a, b = pair()
    rows = [(notes.relative(p), p.read_bytes()) for p in (a, b)]
    original = {p: p.read_bytes() for p in (a, b)}
    def mutate(manifest):
        if bad == "checksum": manifest["files"][1]["sha256"] = "0" * 64
        if bad == "size": manifest["files"][1]["bytes"] += 1
        if bad == "owner": manifest["scope"] = {"user": "a@b"}
        if bad == "scope": manifest["scope"] = {"all": False}
        if bad == "version": manifest["schema_version"] = True
        if bad == "duplicate": manifest["files"].append(manifest["files"][1])
    archive = _tar(tmp_path / "bad.tgz", rows, mutate=mutate,
                   extra=[("unlisted", b"data")] if bad == "extra" else [])
    before_journals = set(notes.inventory(notes._journal_root(), recursive=True))
    with pytest.raises((ValueError, OwnerEvidenceStateError)):
        archives.restore(archive)
    assert {p: p.read_bytes() for p in (a, b)} == original
    assert set(notes.inventory(notes._journal_root(), recursive=True)) == before_journals


@pytest.mark.parametrize("rel", ["../outside.md", "/abs.md", "owners/../a.md", "users/a\\b/x.md",
                                "lessons/CON.md", "lessons/a./x.md", "lessons/a:stream.md"])
def test_unsafe_archive_paths_refused_before_mutation(tmp_path, rel):
    archive = _tar(tmp_path / "bad.tgz", [(rel, b"bad")], scope={"all": True})
    with pytest.raises(ValueError):
        archives.restore(archive)
    assert not notes._journal_root().exists()


@pytest.mark.parametrize("version", [1, 2])
def test_archive_exact_roundtrip_and_legacy_remains_unclaimed(tmp_path, version):
    old = "users/a-b/lessons/old.md"
    raw = b"# legacy\n\nambiguous\n"
    archive = _tar(tmp_path / "old.tgz", [(old, raw)], version=version)
    result = archives.restore(archive, user="a.b", all_users=False)
    assert result["legacy_unclaimed"] == [old]
    assert notes.checked_relative(old).read_bytes() == raw
    with pytest.raises(OwnerEvidenceStateError, match="unclaimed"):
        memory.search_for("a-b", "legacy")


def test_scope_and_ingest_gate_cannot_be_bypassed(tmp_path, monkeypatch):
    p = save("a.b")
    archive = tmp_path / "own.tgz"
    archives.export(archive, user="a.b")
    with pytest.raises(ValueError, match="selected owner"):
        archives.restore(archive, user="a@b")
    monkeypatch.setattr(memory, "_gate_import", lambda *a: (_ for _ in ()).throw(ValueError("ingest denied")))
    raw = p.read_bytes()
    with pytest.raises(ValueError, match="ingest denied"):
        archives.restore(archive, user="a.b")
    assert p.read_bytes() == raw


def test_delete_uses_exact_preview_and_preserves_recovery(tmp_path):
    p = save("a.b")
    other = save("a@b")
    preview = archives.delete_preview("a.b", category="lessons")
    before = p.read_bytes()
    p.write_bytes(notes.render("a.b", "lessons", "new work", "changed"))
    with pytest.raises(OwnerEvidenceStateError, match="stale"):
        archives.delete("a.b", category="lessons", preview=preview)
    current = p.read_bytes()
    assert current != before and other.exists()
    actual = archives.delete("a.b", category="lessons")
    assert actual == [notes.relative(p)] and not p.exists() and other.exists()
    plans = [p.read_bytes() for p in notes.inventory(notes._journal_root(), recursive=True) if p.name == "plan.json"]
    assert any(notes._pack(current).encode() in plan for plan in plans)


def test_journey_ref_binds_content_and_exact_owner():
    p = save("a.b")
    ref = journey.entries("a.b")[0]["ref"]
    assert "No journey" in journey.show(ref, "a@b")
    p.write_bytes(notes.render("a.b", "lessons", "revised", "new"))
    assert "No journey" in journey.remove(ref, "a.b")
    assert p.exists()
    new_ref = journey.entries("a.b")[0]["ref"]
    assert ref != new_ref
    assert journey.remove(new_ref, "a.b").startswith("Removed")
    assert not p.exists()


def _approved_note(owner="a.b", body="saved once"):
    actions.grant_scope(owner, "notes")
    return actions.prepare(owner, "save_note", {"title": "note", "body": body})


def test_real_action_approval_and_undo_with_owner_and_action_binding():
    first, second = _approved_note(), _approved_note(body="different note")
    a, b = actions.approve("a.b", first.id), actions.approve("a.b", second.id)
    assert a.status == b.status == actions.EXECUTED
    assert actions.get("a@b", a.id) is None
    with memory.user_context("a.b"):
        with pytest.raises(OwnerEvidenceStateError, match="another action"):
            builtin_actions._note_undo({**a.result, "_action_id": b.id})
    assert len(notes.notes("a.b", "action_notes")) == 2
    assert actions.undo("a.b", a.id).status == actions.UNDONE
    assert actions.undo("a.b", a.id).status == actions.UNDONE
    assert actions.retry_note("a.b", b.id).status == actions.EXECUTED
    assert len(notes.notes("a.b", "action_notes")) == 1


@pytest.mark.parametrize("operation", ["execute", "undo"])
@pytest.mark.parametrize("decision", ["resume", "rollback"])
def test_note_and_action_state_recover_in_one_transaction(monkeypatch, operation, decision):
    a = _approved_note()
    if operation == "undo":
        a = actions.approve("a.b", a.id)
    original_publish = notes.publish
    hit = []
    def publish(path, raw):
        original_publish(path, raw)
        # For execute, the action record sorts before the note. For undo the
        # same callback fires after the UNDONE record, before deletion.
        if path.name == a.id + ".json" and json.loads(raw)["status"] == (
                actions.EXECUTED if operation == "execute" else actions.UNDONE):
            hit.append(str(path))
            raise OSError("lost acknowledgement after action record publication")
    with monkeypatch.context() as m:
        m.setattr(notes, "publish", publish)
        with pytest.raises(OwnerEvidenceStateError):
            (actions.approve if operation == "execute" else actions.undo)("a.b", a.id)
    assert hit
    with pytest.raises(OwnerEvidenceStateError):
        actions.get("a.b", a.id)
    tx = notes.recovery_status()["active_transaction"]
    notes.recover(tx, decision)
    record = actions.get("a.b", a.id)
    if operation == "execute":
        assert record.status == (actions.EXECUTED if decision == "resume" else actions.APPROVED)
        assert len(notes.notes("a.b", "action_notes")) == (1 if decision == "resume" else 0)
        record = actions.retry_note("a.b", a.id)
        assert record.status == actions.EXECUTED
        assert len(notes.notes("a.b", "action_notes")) == 1
    else:
        assert record.status == (actions.UNDONE if decision == "resume" else actions.EXECUTED)
        assert len(notes.notes("a.b", "action_notes")) == (0 if decision == "resume" else 1)
        assert actions.undo("a.b", a.id).status == actions.UNDONE
        assert notes.notes("a.b", "action_notes") == []


def test_stale_action_and_stale_undo_cannot_overwrite_work():
    original = _approved_note()
    executed = actions.approve("a.b", original.id)
    with pytest.raises(OwnerEvidenceStateError, match="stale action"):
        actions.auto_or_hold(original, level=3)
    path = Path(executed.result["path"])
    changed = notes.render("a.b", "action_notes", "independent edit", "keep",
                           operation=executed.result["operation"])
    path.write_bytes(changed)
    with pytest.raises(OwnerEvidenceStateError, match="changed since"):
        actions.undo("a.b", original.id)
    assert path.read_bytes() == changed
    assert actions.get("a.b", original.id).status == actions.EXECUTED


def test_concurrent_approvals_execute_once():
    a = _approved_note()
    def approve(_):
        try:
            return actions.approve("a.b", a.id).status
        except ValueError as error:
            return str(error)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(approve, range(4)))
    assert statuses.count(actions.EXECUTED) == 1
    assert len(notes.notes("a.b", "action_notes")) == 1
    assert sum("not awaiting approval" in status for status in statuses) == 3


def test_mirror_failure_and_retry_never_repeat_canonical_save(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    monkeypatch.setenv("OLYMPUS_VAULT_DIR", str(vault))
    external = archives.external_publish
    with monkeypatch.context() as m:
        m.setattr(archives, "external_publish", lambda *a: (_ for _ in ()).throw(OSError("mirror down")))
        with memory.user_context("a.b"), pytest.warns(RuntimeWarning, match="Canonical"):
            result = memory.save_with_status("lessons", "test", "keep")
    assert result["canonical_saved"] is True
    assert result["mirror"]["state"] == "unavailable"
    assert len(notes.status("a.b")["optional_mirror_unavailable"]) == 1
    retry = memory.retry_note_mirror("a.b", "lessons", Path(result["path"]).name)
    assert retry["state"] == "available"
    assert len(notes.notes("a.b", "lessons")) == 1
    assert notes.status("a.b")["optional_mirror_unavailable"] == []
    other = save("a@b")
    assert len(list(vault.glob("olympus-notes-v2/*/lessons/*.md"))) == 2
    assert Path(result["path"]).read_bytes() != other.read_bytes()


def test_mcp_context_restored_even_when_notes_unavailable(monkeypatch):
    from olympus import mcp_server
    p = save("a.b")
    p.write_bytes(b"\xff")
    monkeypatch.setenv("OLYMPUS_MCP_USER", "a.b")
    with memory.user_context("outer"):
        with pytest.raises(OwnerEvidenceStateError):
            mcp_server._workspace_tool("olympus_recall_memory", {"query": "x"})
        assert memory.current_owner() == "outer"


def test_cli_recovery_route_reports_real_state(capsys):
    from olympus import cli
    assert cli.main(["memory", "notes-status", "--user", "a.b"]) == 0
    assert json.loads(capsys.readouterr().out)["owner"] == "a.b"
    assert cli.main(["memory", "notes-recover", "missing", "--decision", "resume"]) == 1
    assert "invalid recovery" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="native Windows extended-path behavior")
def test_native_windows_long_note_and_recovery_paths(tmp_path, monkeypatch):
    root = tmp_path / ("a" * 90) / ("b" * 90) / "state"
    monkeypatch.setattr(config, "MEMORY_DIR", root)
    p = save("long-owner" * 20)
    assert len(str(p)) > 260
    assert notes.notes("long-owner" * 20, "lessons")[0]["raw"] == p.read_bytes()
    archives.delete("long-owner" * 20, category="lessons")
    assert not p.exists()

    # The directory API must keep ordinary Path containment working, while
    # its filesystem operations still support paths beyond legacy MAX_PATH.
    owner = "long-owner" * 20
    directory = memory._dir("job_reports", owner)
    assert directory == root / "owners" / memory.owner_key(owner) / "job_reports"
    assert root in directory.parents
    assert notes.io(directory).is_dir()
    report = memory.save_for(owner, "job_reports", "long report", "owned report")
    assert len(str(report)) > 260
    assert report.read_bytes()
    assert "owned report" in memory.search_for(owner, "owned report")
    assert "owned report" not in memory.search_for("other-owner", "owned report")
    assert memory.prune_for(owner, "job_reports", keep=0) == 1
    assert not report.exists()


@pytest.mark.parametrize("category", memory.CATEGORIES)
def test_directory_api_preserves_root_path_containment(category):
    owner = "owner.with-punctuation"
    directory = memory._dir(category, owner)
    private = category in memory.USER_SCOPED | memory.PRIVATE_CATEGORIES
    expected = (config.MEMORY_DIR / "owners" / memory.owner_key(owner) / category
                if private else config.MEMORY_DIR / category)
    assert directory == expected
    assert config.MEMORY_DIR in directory.parents
    assert notes.io(directory).is_dir()


@pytest.mark.parametrize("consumer", ["companion", "wiki", "digest"])
def test_note_damage_reaches_learning_consumers_before_models(monkeypatch, consumer):
    from olympus import backend, companion, digest, wiki
    p = save("a.b", category="feedback")
    p.write_bytes(b"\xff damaged evidence")
    calls = []
    monkeypatch.setattr(backend, "complete_text", lambda *a, **k: calls.append(a) or "wrong")
    with memory.user_context("caller"):
        if consumer == "companion":
            with pytest.raises(OwnerEvidenceStateError):
                companion.evolve("a.b", settings=config.Settings(provider="openai", model="owned", api_key="owned-fixture"))
        elif consumer == "wiki":
            with pytest.raises(OwnerEvidenceStateError):
                wiki._recent_material("a.b", 0)
        else:
            with memory.user_context("a.b"):
                # Digest includes system lessons/corrections rather than feedback.
                q = save("a.b", category="lessons")
                q.write_bytes(b"\xff")
                assert "unavailable" in digest.learned_recently()
        assert memory.current_owner() == "caller"
    assert calls == []


def test_tool_reports_canonical_success_and_mirror_failure(tmp_path, monkeypatch):
    from olympus import tools
    monkeypatch.setenv("OLYMPUS_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setattr(archives, "external_publish", lambda *a: (_ for _ in ()).throw(OSError("down")))
    with memory.user_context("a.b"), pytest.warns(RuntimeWarning):
        result = json.loads(tools.HANDLERS["save_lesson"]("owned", "body"))
    assert result["canonical_saved"] and result["mirror"]["state"] == "unavailable"
    assert len(notes.notes("a.b", "lessons")) == 1


def test_gateway_and_tui_journey_resolve_real_owner_not_ambient():
    from olympus import gateway, tui
    from types import SimpleNamespace
    exact = gateway.principal_id("a.b", "sl")
    other = gateway.principal_id("a@b", "sl")
    save(exact, "owner-only marker")
    save(other, "collider-only marker")
    with memory.user_context("wrong ambient"):
        result = "\n".join(gateway.reply_for({}, "a.b", "/journey", prefix="sl"))
        assert "owner-only marker" in result and "collider-only" not in result
        _, result, _ = tui.dispatch_command(SimpleNamespace(user=exact), "/journey")
        assert "owner-only marker" in result and "collider-only" not in result


def test_streaming_owner_context_restored_at_yield_and_close():
    from olympus.orchestrator import _bind_note_owner
    from types import SimpleNamespace
    observed = []
    @_bind_note_owner(instance=True)
    def stream(self):
        try:
            observed.append(memory.current_owner())
            yield "one"
            observed.append(memory.current_owner())
            yield "two"
        finally:
            observed.append(memory.current_owner())
    with memory.user_context("caller"):
        iterator = stream(SimpleNamespace(user="a.b"))
        assert next(iterator) == "one" and memory.current_owner() == "caller"
        assert next(iterator) == "two" and memory.current_owner() == "caller"
        iterator.close()
        assert memory.current_owner() == "caller"
    assert observed == ["a.b", "a.b", "a.b"]


def test_webreflection_never_acknowledges_a_lost_lesson(monkeypatch):
    from olympus import webreflect
    monkeypatch.setenv("OLYMPUS_WEB_REFLECT", "1")
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    found = [{"key": "owned", "kind": "capability", "title": "owned", "detail": "mock source"}]
    monkeypatch.setattr(webreflect, "discoveries", lambda: found)
    notices = []
    original = notes.publish
    def fail(path, raw):
        if path.parent.name == "lessons":
            raise OSError("note storage unavailable")
        original(path, raw)
    with monkeypatch.context() as m:
        m.setattr(notes, "publish", fail)
        with pytest.raises(OwnerEvidenceStateError):
            webreflect.run_due(10**9, notify=notices.append)
    assert notices == []
    tx = notes.recovery_status()["active_transaction"]
    notes.recover(tx, "resume")
    assert len(notes.notes("shared", "lessons")) == 1
    assert webreflect._load_state()["seen"] == ["owned"]
    assert webreflect.run_due(10**9+1, notify=notices.append) == []
    assert len(notes.notes("shared", "lessons")) == 1


@pytest.mark.skipif(os.name != "posix", reason="directory fsync requires POSIX")
def test_real_directory_barrier_failure_is_not_success(monkeypatch):
    p = save("a.b")
    before = p.read_bytes()
    original = notes.sync_dir
    def fail(path):
        if notes.logical(path) == notes.logical(p.parent):
            raise OSError("directory fsync denied")
        return original(path)
    with monkeypatch.context() as m:
        m.setattr(notes, "sync_dir", fail)
        with pytest.raises(OwnerEvidenceStateError):
            notes.delete_rows(notes.notes("a.b", "lessons"))
    state = notes.recovery_status()
    assert state["state"] == "unavailable"
    notes.recover(state["active_transaction"], "rollback")
    assert p.read_bytes() == before


def test_archive_rejects_links_and_case_duplicate_members(tmp_path):
    for kind in (tarfile.SYMTYPE, tarfile.FIFOTYPE, tarfile.DIRTYPE):
        path = tmp_path / (str(kind) + ".tgz")
        with tarfile.open(path, "w:gz") as tar:
            entry = tarfile.TarInfo("data/lessons/poison.md")
            entry.type = kind
            entry.linkname = "../../outside"
            tar.addfile(entry)
        with pytest.raises(ValueError, match="archive member"):
            archives.restore(path)
    raw = notes.render("a.b", "lessons", "a", "b")
    rel = notes.relative(notes.directory("a.b", "lessons") / "same.md")
    path = _tar(tmp_path / "dup.tgz", [(rel, raw)], extra=[("data/"+rel.upper(), raw)])
    with pytest.raises(ValueError, match="duplicate"):
        archives.restore(path)
    assert not notes._journal_root().exists()


def test_empty_archive_requires_valid_scope(tmp_path):
    path = _tar(tmp_path / "empty.tgz", [], scope={"all": False})
    with pytest.raises(ValueError, match="scope"):
        archives.restore(path)


def test_real_http_action_parser_returns_503_for_damaged_note_state(monkeypatch):
    from olympus import web
    owner = "a.b"
    monkeypatch.setenv("OLYMPUS_REQUIRE_LOGIN", "0")
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "owned-token")
    monkeypatch.setattr(web, "_user_for", lambda sid: owner)
    monkeypatch.setattr(web, "_HITS", {})
    a = _approved_note(owner)
    path = actions._path(owner, a.id)
    path.write_bytes(b"{damaged action")
    payload = json.dumps({"op": "approve", "action_id": a.id}).encode()
    class Connection:
        response = bytearray()
        def makefile(self, *args, **kwargs):
            return io.BytesIO(
                b"POST /api/action HTTP/1.0\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nX-Olympus-Token: owned-token\r\n"
                + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        def sendall(self, data):
            self.response.extend(data)
    connection = Connection()
    web.Handler(connection, ("127.0.0.1", 12345), object())
    headers, raw = bytes(connection.response).split(b"\r\n\r\n", 1)
    assert int(headers.split()[1]) == 503
    assert json.loads(raw)["evidence_state"] == "unavailable"
    assert notes.notes(owner, "action_notes") == []
    assert path.read_bytes() == b"{damaged action"


def test_archive_expansion_is_bounded_before_tar_header_parsing(tmp_path, monkeypatch):
    import gzip
    monkeypatch.setattr(archives, "MAX_ARCHIVE", 4096)
    archive = tmp_path / "bomb.tgz"
    archive.write_bytes(gzip.compress(b"\0" * 8192))
    with pytest.raises(ValueError, match="expanded"):
        archives.restore(archive)
    assert not notes._journal_root().exists()


def test_owner_delete_does_not_resolve_to_shared_system_reports():
    p = save("shared", category="reports")
    raw = p.read_bytes()
    with pytest.raises(ValueError, match="shared scope"):
        memory.delete_memory("a.b", category="reports")
    assert p.read_bytes() == raw
    assert memory.delete_memory("shared", category="reports") == [notes.relative(p)]


def test_initialization_retry_preserves_new_notes_and_legacy():
    original = config.MEMORY_DIR / "users" / "a-b" / "lessons" / "old.md"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"unclaimed")
    notes.initialize("a.b", "lessons", acknowledge_legacy=True)
    p = save("a.b")
    before = p.read_bytes()
    result = notes.initialize("a.b", "lessons", acknowledge_legacy=True)
    assert result["already_initialized"] and not result["initialized_empty"]
    assert p.read_bytes() == before and original.read_bytes() == b"unclaimed"
