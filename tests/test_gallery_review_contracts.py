"""Independent M06 recovery review regressions, using only owned local fixtures.

Run only through the approved denied-network launcher in cloud validation.
These tests do not establish native Windows filesystem/handle semantics.
"""
import copy
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from olympus import gallery
from olympus import gallery_state as gs


def _fixture_path(path):
    """Keep full owned paths, using extended Windows syntax for fixture I/O."""
    if os.name != "nt":
        return path
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def test_fixture_path_preserves_deep_owned_io(tmp_path):
    logical = tmp_path
    for letter in "abc":
        logical /= "owned-" + letter * 80
    logical /= "fixture.blob"
    assert len(str(logical)) > 260
    actual = _fixture_path(logical)
    assert _fixture_path(actual) == actual
    actual.parent.mkdir(parents=True)
    actual.write_bytes(b"owned original bytes")
    assert actual.read_bytes() == b"owned original bytes"
    retained = _fixture_path(logical.with_suffix(".retained-evidence"))
    actual.rename(retained)
    assert not actual.exists()
    assert retained.exists()
    assert retained.read_bytes() == b"owned original bytes"


@pytest.fixture
def owned(monkeypatch, tmp_path):
    from PIL import Image
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 30, 50)).save(stream, format="PNG")
    return tmp_path, gs.Store("review:exact-owner"), stream.getvalue()


def completed(store, data, operation="review-generate", name="owned.png"):
    store.reserve(operation, "generate", {"fixture": "owned"}, name)
    store.start(operation)
    return store.finalize(operation, data)


def read_state(store):
    return json.loads((store.root / "state.json").read_text())


def write_state(store, state):
    (store.root / "state.json").write_text(json.dumps(state))


def rebound(review):
    review = copy.deepcopy(review)
    review.pop("digest", None)
    review["digest"] = gs._digest(gs._json(review))
    return review


def test_missing_started_manifest_never_reopens_admission(owned):
    _, store, _ = owned
    store.reserve("same", "generate", {}, "owned.png")
    store.start("same")
    (store.root / "state.json").rename(store.root / "lost-state.evidence")
    with pytest.raises(gs.GalleryError) as error:
        store.reserve("same", "generate", {}, "owned.png")
    assert error.value.code == "unavailable"
    assert (store.root / "lost-state.evidence").exists()


@pytest.mark.parametrize("change", [
    "complete_without_image", "generate_with_source", "failed_without_error",
    "failed_with_live_image", "generate_deleted", "unbound_tombstone",
    "source_is_list", "duplicate_output_identity",
])
def test_malformed_operation_relations_refuse_authority(owned, change):
    _, store, data = owned
    receipt = completed(store, data)
    state = read_state(store)
    op = state["operations"]["review-generate"]
    image = receipt["image"]
    source = {k: image[k] for k in ("name", "id", "revision")}
    if change == "complete_without_image":
        op.pop("image")
    elif change == "generate_with_source":
        op["source"] = source
    elif change == "failed_without_error":
        op["status"] = "failed"
        op.pop("image")
        op.pop("metadata", None)
        state["images"] = {}
    elif change == "failed_with_live_image":
        op["status"] = "failed"
        op["error"] = {"code": "fixture", "message": "owned failure"}
    elif change == "generate_deleted":
        op["status"] = "deleted"
        state["images"] = {}
        state["tombstones"][image["id"]] = image
    elif change == "unbound_tombstone":
        forged = {**image, "id": "f" * 32, "name": "orphan.png"}
        state["tombstones"][forged["id"]] = forged
    elif change == "source_is_list":
        op["kind"] = "edit"
        op["source"] = ["owned.png"]
    else:
        duplicate = {**op, "operation_id": "duplicate"}
        state["operations"]["duplicate"] = duplicate
    write_state(store, state)
    original = (store.root / "state.json").read_bytes()
    with pytest.raises(gs.GalleryError) as error:
        store.status("review-generate")
    assert error.value.code == "unavailable"
    assert (store.root / "state.json").read_bytes() == original


@pytest.mark.parametrize("terminal", ["complete", "deleted"])
@pytest.mark.parametrize("method", ["status", "recover"])
@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_terminal_receipts_verify_retained_bytes(owned, terminal, method, damage):
    _, store, data = owned
    image = completed(store, data)["image"]
    operation = "review-generate"
    if terminal == "deleted":
        operation = "review-delete"
        store.delete(image["name"], image["id"], image["revision"], operation)
    blob = _fixture_path(store.root / "objects" / (image["id"] + ".blob"))
    if damage == "missing":
        retained = blob.with_suffix(".retained-evidence")
        blob.rename(retained)
        assert not blob.exists()
        assert retained.exists()
    else:
        blob.write_bytes(b"owned changed bytes")
        assert blob.read_bytes() == b"owned changed bytes"
    with pytest.raises(gs.GalleryError) as error:
        getattr(store, method)(operation)
    assert error.value.code == "unavailable"


@pytest.mark.parametrize("method", ["status", "recover", "reserve"])
def test_failed_manifest_barrier_never_becomes_confirmed_by_reread(owned, monkeypatch, method):
    _, store, data = owned
    store.reserve("barrier", "generate", {}, "owned.png")
    store.start("barrier")
    original_sync = gs.Directory.sync

    def refuse_committed_manifest(directory):
        path = directory.path / "state.json"
        if directory.path == store.root and path.exists():
            state = json.loads(path.read_text())
            if state["operations"]["barrier"]["status"] == "complete":
                raise OSError("owned injected manifest directory barrier failure")
        return original_sync(directory)

    monkeypatch.setattr(gs.Directory, "sync", refuse_committed_manifest)
    with pytest.raises(gs.GalleryError):
        store.finalize("barrier", data)
    assert read_state(store)["operations"]["barrier"]["status"] == "complete"
    with pytest.raises(gs.GalleryError) as error:
        if method == "reserve":
            store.reserve("barrier", "generate", {}, "owned.png")
        else:
            getattr(store, method)("barrier")
    assert error.value.code == "unavailable"
    monkeypatch.setattr(gs.Directory, "sync", original_sync)
    assert store.recover("barrier")["status"] == "complete"


def test_output_barrier_retry_required_before_adoption(owned, monkeypatch):
    _, store, data = owned
    store.reserve("output-barrier", "generate", {}, "owned.png")
    store.start("output-barrier")
    original_sync = gs.Directory.sync

    def refuse_output_sync(directory):
        if directory.path == store.root / "outputs":
            raise OSError("owned injected output barrier failure")
        return original_sync(directory)

    monkeypatch.setattr(gs.Directory, "sync", refuse_output_sync)
    with pytest.raises(gs.GalleryError):
        store.finalize("output-barrier", data)
    assert list((store.root / "objects").glob("*.blob"))
    assert list((store.root / "outputs").glob("*.json"))
    with pytest.raises(gs.GalleryError):
        store.recover("output-barrier")
    assert read_state(store)["images"] == {}
    monkeypatch.setattr(gs.Directory, "sync", original_sync)
    assert store.recover("output-barrier")["status"] == "complete"


def test_renamed_legacy_inode_cannot_be_claimed_by_second_owner(owned):
    workspace, store, data = owned
    source = workspace / "legacy.png"
    source.write_bytes(data)
    review = gallery.legacy_review(store.owner)
    assert gallery.claim_legacy(store.owner, review=review, review_digest=review["digest"])["status"] == "complete"
    source.rename(workspace / "renamed.png")
    other_review = gallery.legacy_review("another-owner")
    result = gallery.claim_legacy("another-owner", review=other_review, review_digest=other_review["digest"])
    assert result["status"] == "partial"
    assert result["results"][0]["code"] == "legacy_conflict"
    assert (workspace / "renamed.png").read_bytes() == data
    assert gs.Store("another-owner").list_images() == []


def test_missing_claim_ledger_cannot_reset_attribution(owned):
    workspace, store, data = owned
    (workspace / "legacy.png").write_bytes(data)
    review = gallery.legacy_review(store.owner)
    gallery.claim_legacy(store.owner, review=review, review_digest=review["digest"])
    ledger = workspace / "gallery-v2" / "claims.json"
    ledger.rename(ledger.with_suffix(".retained-evidence"))
    other_review = gallery.legacy_review("another-owner")
    with pytest.raises(gs.GalleryError) as error:
        gallery.claim_legacy("another-owner", review=other_review, review_digest=other_review["digest"])
    assert error.value.code == "unavailable"


@pytest.mark.parametrize("damage", ["missing", "corrupt", "foreign"])
def test_existing_claim_ledger_requires_valid_initialization(owned, damage):
    workspace, store, data = owned
    (workspace / "legacy.png").write_bytes(data)
    review = gallery.legacy_review(store.owner)
    gallery.claim_legacy(store.owner, review=review, review_digest=review["digest"])
    marker = workspace / "gallery-v2" / "claims.initialized"
    if damage == "missing":
        marker.rename(marker.with_suffix(".retained-evidence"))
    elif damage == "corrupt":
        marker.write_bytes(b"{")
    else:
        marker.write_text(json.dumps({"version": 2, "workspace": "foreign"}))
    with pytest.raises(gs.GalleryError) as error:
        gallery.claim_legacy(store.owner, review=review, review_digest=review["digest"])
    assert error.value.code == "unavailable"


def test_duplicate_review_refused_before_any_publication(owned, monkeypatch):
    workspace, store, data = owned
    (workspace / "legacy.png").write_bytes(data)
    review = gallery.legacy_review(store.owner)
    review["items"].append(copy.deepcopy(review["items"][0]))
    review = rebound(review)
    calls = []

    def unexpected_publish(*args):
        calls.append(args)
        raise OSError("must not reach publication for duplicate source")

    monkeypatch.setattr(gs.Directory, "publish", unexpected_publish)
    with pytest.raises(gs.GalleryError) as error:
        gallery.claim_legacy(store.owner, review=review, review_digest=review["digest"])
    assert error.value.code == "review_conflict"
    assert calls == []
    assert (workspace / "legacy.png").read_bytes() == data


def test_failed_ledger_publication_aborts_claim_copy(owned, monkeypatch):
    workspace, store, data = owned
    (workspace / "legacy.png").write_bytes(data)
    review = gallery.legacy_review(store.owner)
    original_publish = gs.Directory.publish

    def refuse_ledger(directory, name, content):
        if name == "claims.json":
            raise OSError("owned failed ledger publication")
        return original_publish(directory, name, content)

    monkeypatch.setattr(gs.Directory, "publish", refuse_ledger)
    with pytest.raises(gs.GalleryError):
        gallery.claim_legacy(store.owner, review=review, review_digest=review["digest"])
    assert not (store.root / "state.json").exists()
    assert (workspace / "legacy.png").read_bytes() == data


def test_reserved_local_claim_can_resume_without_provider(owned, monkeypatch):
    workspace, store, data = owned
    (workspace / "legacy.png").write_bytes(data)
    review = gallery.legacy_review(store.owner)
    original_start = gs.Store.start

    def interrupt_before_start(*args):
        raise gs.GalleryError("unavailable", "owned interruption before local claim start")

    monkeypatch.setattr(gs.Store, "start", interrupt_before_start)
    assert gallery.claim_legacy(store.owner, review=review, review_digest=review["digest"])["status"] == "partial"
    monkeypatch.setattr(gs.Store, "start", original_start)
    assert gallery.claim_legacy(store.owner, review=review, review_digest=review["digest"])["status"] == "complete"
    assert (workspace / "legacy.png").read_bytes() == data


@pytest.mark.parametrize("source", [None, 42, [], {}])
def test_bad_review_source_is_typed_refusal(owned, source):
    workspace, store, data = owned
    (workspace / "legacy.png").write_bytes(data)
    review = gallery.legacy_review(store.owner)
    review["items"][0]["source"] = source
    review = rebound(review)
    try:
        result = gallery.claim_legacy(store.owner, review=review, review_digest=review["digest"])
    except gs.GalleryError as error:
        assert error.code in {"invalid", "review_conflict"}
    else:
        assert result["status"] == "partial"
        assert result["results"][0]["status"] == "error"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract; Windows has explicit limitation")
@pytest.mark.parametrize("parent_kind", ["workspace", "gallery"])
def test_existing_directory_retries_failed_parent_barrier(owned, monkeypatch, parent_kind):
    workspace, store, _ = owned
    original_fsync = gs.os.fsync
    calls = []
    parent = workspace if parent_kind == "workspace" else workspace / "gallery-v2"
    child = workspace / "gallery-v2" if parent_kind == "workspace" else store.root

    def fail_created_parent(fd):
        if parent.exists() and child.exists():
            actual, expected = os.fstat(fd), parent.stat()
            if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                calls.append(parent_kind)
                raise OSError("owned parent directory barrier failure")
        return original_fsync(fd)

    monkeypatch.setattr(gs.os, "fsync", fail_created_parent)
    with pytest.raises(gs.GalleryError):
        store.reserve("parent-barrier", "generate", {}, "owned.png")
    assert child.exists()
    with pytest.raises(gs.GalleryError):
        store.reserve("parent-barrier", "generate", {}, "owned.png")
    assert len(calls) == 2
    monkeypatch.setattr(gs.os, "fsync", original_fsync)
    assert store.reserve("parent-barrier", "generate", {}, "owned.png")["execute"]


def test_delete_manifest_barrier_retries_without_losing_original(owned, monkeypatch):
    _, store, data = owned
    image = completed(store, data)["image"]
    original_sync = gs.Directory.sync

    def refuse_deleted_manifest(directory):
        path = directory.path / "state.json"
        if directory.path == store.root and path.exists():
            state = json.loads(path.read_text())
            if "delete-barrier" in state["operations"]:
                raise OSError("owned deletion directory barrier failure")
        return original_sync(directory)

    monkeypatch.setattr(gs.Directory, "sync", refuse_deleted_manifest)
    for _ in range(2):
        with pytest.raises(gs.GalleryError):
            store.delete(image["name"], image["id"], image["revision"], "delete-barrier")
    assert _fixture_path(store.root / "objects" / (image["id"] + ".blob")).read_bytes() == data
    monkeypatch.setattr(gs.Directory, "sync", original_sync)
    assert store.delete(image["name"], image["id"], image["revision"], "delete-barrier")["status"] == "deleted"


def test_legacy_data_and_identity_use_one_handle(owned, monkeypatch):
    workspace, store, data = owned
    source = workspace / "legacy.png"
    source.write_bytes(data)
    original_open = gs.Directory._open
    calls = []

    def record_open(directory, name, flags, *args):
        if directory.path == workspace and name == source.name:
            calls.append(name)
        return original_open(directory, name, flags, *args)

    monkeypatch.setattr(gs.Directory, "_open", record_open)
    actual, identity = gallery._legacy_read(store, source.name)
    assert actual == data
    assert identity["inode"] == source.stat().st_ino
    assert calls == ["legacy.png"]


@pytest.mark.parametrize("race", ["name", "capacity"])
def test_competing_reservations_have_one_winner(owned, monkeypatch, race):
    _, store, _ = owned
    if race == "capacity":
        monkeypatch.setattr(gs, "MAX_IMAGES", 1)
    barrier = threading.Barrier(2)

    def attempt(index):
        barrier.wait(timeout=5)
        name = "same.png" if race == "name" else f"name-{index}.png"
        try:
            return store.reserve(f"race-{index}", "generate", {}, name)["execute"]
        except gs.GalleryError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert results.count(True) == 1
    assert results.count("name_conflict" if race == "name" else "capacity") == 1


def test_edit_output_after_source_delete_stays_pending(owned):
    _, store, data = owned
    image = completed(store, data)["image"]
    source = {key: image[key] for key in ("name", "id", "revision")}
    store.reserve("edit-race", "edit", {}, "edited.png", source)
    store.start("edit-race")
    store.delete(image["name"], image["id"], image["revision"], "delete-source")
    for method in (lambda: store.finalize("edit-race", data), lambda: store.recover("edit-race")):
        with pytest.raises(gs.GalleryError) as error:
            method()
        assert error.value.code == "stale"
    assert store.list_images() == []
    assert len(list((store.root / "objects").glob("*.blob"))) == 2


def test_delete_receipt_source_must_match_tombstone(owned):
    _, store, data = owned
    image = completed(store, data)["image"]
    store.delete(image["name"], image["id"], image["revision"], "deleted")
    state = read_state(store)
    state["operations"]["deleted"]["source"]["revision"] = "0" * 64
    write_state(store, state)
    with pytest.raises(gs.GalleryError) as error:
        store.status("deleted")
    assert error.value.code == "unavailable"


def test_json_float_is_not_gallery_schema_version(owned):
    _, store, _ = owned
    store.reserve("version", "generate", {}, "owned.png")
    state = read_state(store)
    state["version"] = 2.0
    write_state(store, state)
    with pytest.raises(gs.GalleryError) as error:
        store.status("version")
    assert error.value.code == "unavailable"


@pytest.mark.parametrize("bound", ["images", "bytes"])
def test_pending_reservations_are_authority_capacity_invariants(owned, monkeypatch, bound):
    _, store, _ = owned
    store.reserve("capacity-a", "generate", {}, "a.png")
    store.reserve("capacity-b", "generate", {}, "b.png")
    if bound == "images":
        monkeypatch.setattr(gs, "MAX_IMAGES", 1)
    else:
        monkeypatch.setattr(gs, "MAX_LIVE_BYTES", gs.MAX_IMAGE_BYTES)
    with pytest.raises(gs.GalleryError) as error:
        store.status("capacity-a")
    assert error.value.code == "unavailable"


def test_unclaimed_scan_is_bounded_even_without_images(owned, monkeypatch):
    workspace, store, _ = owned
    for index in range(4):
        (workspace / f"ordinary-{index}.txt").write_text("owned fixture")
    monkeypatch.setattr(gallery, "MAX_LEGACY_SCAN", 3)
    with pytest.raises(gs.GalleryError) as error:
        gallery.list_result(store.owner)
    assert error.value.code == "capacity"


@pytest.mark.parametrize("name", ["CON .png", "COM¹.png", "LPT².png"])
def test_windows_device_display_aliases_refused(owned, name):
    _, store, _ = owned
    with pytest.raises(gs.GalleryError) as error:
        store.reserve("reserved-name", "generate", {}, name)
    assert error.value.code == "invalid_name"


def test_metadata_completion_headroom_is_reserved_before_execution(owned, monkeypatch):
    _, store, _ = owned
    # Current reservation JSON fits, but the allowed output metadata alone
    # exceeds this bound. Admitting provider execution cannot be safe.
    monkeypatch.setattr(gs, "MAX_STATE_BYTES", 1024)
    with pytest.raises(gs.GalleryError) as error:
        store.reserve("metadata-headroom", "generate", {}, "owned.png")
    assert error.value.code == "capacity"


def test_workspace_denial_is_typed_unavailable(owned, monkeypatch):
    from olympus import sandbox

    def deny_workspace():
        raise PermissionError("owned workspace denial")

    monkeypatch.setattr(sandbox, "workdir", deny_workspace)
    with pytest.raises(gs.GalleryError) as error:
        gallery.list_result("review:exact-owner")
    assert error.value.code == "unavailable"


@pytest.mark.parametrize("evidence", ["state.json", "initialized.json", "blob"])
def test_evidence_disappearing_before_confirmation_is_unavailable(owned, monkeypatch, evidence):
    _, store, data = owned
    image = completed(store, data)["image"]
    target = (store.root / "objects" / (image["id"] + ".blob")
              if evidence == "blob" else store.root / evidence)
    original_confirm = gs.Directory.confirm
    moved = []

    def disappear_before_confirmation(directory, name):
        path = directory.path / name
        if path == target and not moved:
            retained = _fixture_path(path.with_name(path.name + ".retained-evidence"))
            _fixture_path(path).rename(retained)
            assert not _fixture_path(path).exists()
            assert retained.exists()
            moved.append(retained)
        return original_confirm(directory, name)

    monkeypatch.setattr(gs.Directory, "confirm", disappear_before_confirmation)
    with pytest.raises(gs.GalleryError) as error:
        store.status("review-generate")
    assert error.value.code == "unavailable"
    assert moved and moved[0].exists()
