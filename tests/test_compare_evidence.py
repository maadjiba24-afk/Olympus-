"""M05 owned fixtures: exact ownership, decisions, interruption and consumers.

These tests never supply genuine quality samples or authorize model traffic.
The cloud leg uses an external kernel IP-denial launcher; native HTTP/browser
and platform durability contracts remain separate required delivery evidence.
"""
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

import pytest

from olympus import backend, calibration, compare, compare_execution as execution
from olympus import compare_state as state, config, note_evidence as files


@pytest.fixture()
def owned(monkeypatch):
    models = [config.Settings(provider="anthropic", model="requested-a", api_key="secret-a"),
              config.Settings(provider="anthropic", model="requested-b", api_key="secret-b")]
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "0")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "m05-owned-fixture-only")
    monkeypatch.setattr(compare, "available_models", lambda: models)
    monkeypatch.setattr(compare.random, "shuffle", lambda order: None)
    calls = []

    def dispatch(settings, system, messages, effort):
        calls.append(settings.model)
        execution.observe(provider=settings.provider, model=settings.model + "-served",
                          response_id="owned-response-" + str(len(calls)), revision="owned-revision",
                          endpoint="https://owned.invalid/v1")
        return "Owned answer " + str(len(calls))

    monkeypatch.setattr(backend, "_dispatch_text", dispatch)
    return models, calls


def test_exact_owners_and_literal_shared_remain_distinct(owned):
    owners = ["tg-a.b", "tg-a-b", "Alice", "alice", "é", "é", "shared", "Shared", "x" * 80 + "a", "x" * 80 + "b"]
    ids = {owner: compare.run(owner, "owned question")["id"] for owner in owners}
    assert len({state.path(o) for o in owners}) == len(owners)
    for owner in owners:
        for other in owners:
            assert (compare.get(owner, ids[other]) is not None) == (owner == other)
    assert all(len(compare.export(owner)["records"]) == 1 for owner in owners)


@pytest.mark.parametrize("owner", [None, "", "   ", 0, [], "\ud800"], ids=["none", "empty", "blank", "number", "list", "surrogate"])
def test_missing_owner_never_means_shared(owned, owner):
    with pytest.raises(compare.CompareError):
        compare.run(owner, "q")
    assert owned[1] == []


def test_missing_reads_do_not_create_comparison_directory(owned):
    assert compare.get("first", "0" * 32) is None
    assert compare.tally("first") == {}
    assert not state.path("first").parent.exists()


def test_legacy_preserved_and_explicit_empty_initialization(owned):
    legacy = config.MEMORY_DIR / "users" / "tg-a-b" / "compares"
    legacy.mkdir(parents=True)
    (legacy / "old.json").write_bytes(b'{"private": "unattributed"}')
    for owner in ("tg-a.b", "tg-a-b"):
        with pytest.raises(compare.CompareError, match="Legacy"):
            compare.run(owner, "q")
        with pytest.raises(compare.CompareError):
            compare.initialize_empty(owner)
        compare.initialize_empty(owner, acknowledge_legacy=True)
        assert compare.tally(owner) == {}
    assert (legacy / "old.json").read_bytes() == b'{"private": "unattributed"}'
    assert owned[1] == []


@pytest.mark.parametrize("raw", [b'{', b'{"version":2,"version":2}', b'[]', b'null', b'{"x":NaN}', b'\xff'],
                         ids=["truncated", "duplicate", "list", "null", "nan", "utf8"])
def test_corrupt_store_refuses_all_consumers_without_repair(owned, raw):
    path = state.path("u")
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    for operation in (lambda: compare.tally("u"), lambda: compare.run("u", "q"),
                      lambda: compare.recover("u", "0" * 32), lambda: compare.initialize_empty("u")):
        with pytest.raises(compare.CompareError):
            operation()
        assert path.read_bytes() == raw
    assert owned[1] == []


@pytest.mark.parametrize("damage", ["owner", "bool_version", "choice", "run_id", "label", "identity", "created", "extra"], ids=str)
def test_typed_record_damage_is_unavailable(owned, damage):
    cid = compare.run("u", "q")["id"]
    value = compare.export("u")
    rec = value["records"][cid]
    if damage == "owner": value["owner"] = "other"
    if damage == "bool_version": value["version"] = True
    if damage == "choice": rec["choice"] = "A"
    if damage == "run_id": rec["members"][1]["run_id"] = rec["members"][0]["run_id"]
    if damage == "label": rec["members"][1]["label"] = "A"
    if damage == "identity": rec["members"][0]["identity"] = "0" * 64
    if damage == "created": rec["created"] = 10 ** 400
    if damage == "extra": rec["tally"] = {"made-up": 42}
    raw = json.dumps(value).encode()
    state.path("u").write_bytes(raw)
    with pytest.raises(compare.CompareError):
        compare.reveal("u", cid, "A")
    assert state.path("u").read_bytes() == raw


def test_state_size_bound_before_calls(owned, monkeypatch):
    path = state.path("u")
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * 4097)
    monkeypatch.setattr(state, "MAX_BYTES", 4096)
    with pytest.raises(compare.CompareError):
        compare.run("u", "q")
    assert owned[1] == []


@pytest.mark.parametrize("which", ["file", "parent", "directory", "fifo"])
def test_nonregular_evidence_refused(owned, tmp_path, which):
    path = state.path("u")
    path.parent.mkdir(parents=True)
    target = tmp_path / "outside"
    target.write_bytes(b"preserve")
    if which in ("file", "parent"):
        try:
            if which == "file": path.symlink_to(target)
            else:
                path.parent.rmdir()
                path.parent.symlink_to(tmp_path, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable; native Windows junction contract is separate")
    elif which == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("POSIX FIFO contract")
        os.mkfifo(path)
    else:
        path.mkdir()
    with pytest.raises(compare.CompareError):
        compare.run("u", "q")
    assert target.read_bytes() == b"preserve"
    assert owned[1] == []


def test_idempotent_run_and_conflicting_request(owned):
    cid = uuid.uuid4().hex
    first = compare.run("u", "q", cid=cid)
    assert compare.run("u", "q", cid=cid) == first
    assert len(owned[1]) == 2
    with pytest.raises(compare.CompareError) as raised:
        compare.run("u", "different", cid=cid)
    assert raised.value.status == 409
    assert len(owned[1]) == 2


def test_first_reveal_is_final_and_retry_does_not_vote_twice(owned):
    cid = compare.run("u", "q")["id"]
    first = compare.reveal("u", cid, "a")
    assert compare.reveal("u", cid, "A") == first
    assert compare.reveal("u", cid) == first
    with pytest.raises(compare.CompareError) as raised:
        compare.reveal("u", cid, "B")
    assert raised.value.code == "conflict"
    assert compare.tally("u") == {first["chosen_identity"]: 1}


def test_no_vote_after_unblinded_reveal(owned):
    cid = compare.run("u", "q")["id"]
    compare.reveal("u", cid)
    with pytest.raises(compare.CompareError):
        compare.reveal("u", cid, "A")
    assert compare.tally("u") == {}


def test_failure_is_blind_and_ineligible(owned, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("requested-b API token SECRET_LEAK")
    monkeypatch.setattr(backend, "_dispatch_text", fail)
    out = compare.run("u", "q")
    assert "SECRET_LEAK" not in json.dumps(out) and "requested-b" not in json.dumps(out)
    assert not any(a["eligible"] for a in out["answers"])
    with pytest.raises(compare.CompareError):
        compare.reveal("u", out["id"], "A")
    assert compare.reveal("u", out["id"])["choice"] is None


@pytest.mark.parametrize("answer", [None, {}, "", "x" * (state.MAX_TEXT + 1)], ids=["none", "object", "empty", "oversize"])
def test_malformed_provider_answer_is_bounded_and_ineligible(owned, monkeypatch, answer):
    monkeypatch.setattr(backend, "_dispatch_text", lambda *a, **k: answer)
    out = compare.run("u", "q")
    assert all(a["state"] == "failed" for a in out["answers"])
    assert state.path("u").stat().st_size < 10000


@pytest.mark.parametrize("step", range(1, 7))
@pytest.mark.parametrize("after", [False, True], ids=["before_publish", "lost_ack"])
def test_every_run_publication_recovers_without_repeating_calls(owned, monkeypatch, step, after):
    publish = files.publish
    count = 0
    def interrupted(path, raw):
        nonlocal count
        count += 1
        if count == step and not after:
            raise OSError("owned publication fault")
        publish(path, raw)
        if count == step and after:
            raise OSError("owned lost acknowledgement")
    monkeypatch.setattr(files, "publish", interrupted)
    cid = uuid.uuid4().hex
    with pytest.raises(compare.CompareError) as raised:
        compare.run("u", "q", cid=cid)
    assert raised.value.cid == cid
    calls = list(owned[1])
    monkeypatch.setattr(files, "publish", publish)
    if step == 1 and not after:
        assert compare.get("u", cid) is None  # No preparation or provider work.
        assert calls == []
        return
    prior = compare.run("u", "q", cid=cid)
    recovered = compare.recover("u", cid)
    assert recovered["phase"] == "complete"
    assert owned[1] == calls
    assert compare.recover("u", cid) == recovered
    assert sum(a["state"] == "answered" for a in recovered["answers"]) == sum(a["state"] == "answered" for a in prior["answers"])


@pytest.mark.parametrize("after", [False, True], ids=["before_publish", "lost_ack"])
def test_reveal_commit_point_keeps_vote_atomic(owned, monkeypatch, after):
    cid = compare.run("u", "q")["id"]
    publish = files.publish
    def interrupted(path, raw):
        if after: publish(path, raw)
        raise OSError("owned reveal fault")
    monkeypatch.setattr(files, "publish", interrupted)
    with pytest.raises(compare.CompareError):
        compare.reveal("u", cid, "A")
    monkeypatch.setattr(files, "publish", publish)
    result = compare.reveal("u", cid, "A")
    compare.recover("u", cid)
    assert compare.tally("u") == {result["chosen_identity"]: 1}
    assert len(owned[1]) == 2


def test_concurrent_reveals_only_one_decision(owned):
    cid = compare.run("u", "q")["id"]
    def vote(label):
        try: return compare.reveal("u", cid, label)["choice"]
        except compare.CompareError as err: return err.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(vote, ["A", "B"] * 8))
    assert ("A" in outcomes) != ("B" in outcomes)
    assert "conflict" in outcomes and sum(compare.tally("u").values()) == 1


def test_pruning_retains_votes_and_request_tombstones(owned, monkeypatch):
    monkeypatch.setattr(compare, "_MAX_STORED", 2)
    ids = []
    for i in range(5):
        cid = compare.run("u", str(i))["id"]
        compare.reveal("u", cid, "A")
        ids.append(cid)
    saved = compare.export("u")
    assert len(saved["records"]) == 2 and len(saved["archive"]) == 3
    assert sum(compare.tally("u").values()) == 5
    for operation in (lambda: compare.reveal("u", ids[0], "A"),
                      lambda: compare.run("u", "0", cid=ids[0])):
        with pytest.raises(compare.CompareError) as raised: operation()
        assert raised.value.status == 410
    assert len(owned[1]) == 10


def test_receipt_capacity_refuses_before_new_model_work(owned, monkeypatch):
    monkeypatch.setattr(state, "MAX_RECEIPTS", 1)
    compare.run("u", "q")
    with pytest.raises(compare.CompareError) as raised: compare.run("u", "other")
    assert raised.value.code == "capacity" and len(owned[1]) == 2


def test_execution_provenance_is_private_until_reveal_and_has_exact_runs(owned):
    result = compare.run("u", "private prompt")
    assert "secret-a" not in state.path("u").read_text()
    assert "requested-a" not in json.dumps(result)
    reveal = compare.reveal("u", result["id"], "A")
    member = reveal["executions"][0]
    assert member["configured"]["model"] == "requested-a"
    assert member["provenance"]["responses"][0]["model"] == "requested-a-served"
    assert member["provenance"]["calls"][0]["run_id"] == member["run_id"]
    assert member["provenance"]["responses"][0]["run_id"] == member["run_id"]
    assert member["provenance"]["responses"][0]["revision"] == "owned-revision"


def test_same_label_at_distinct_endpoint_does_not_merge_tally(owned):
    models, _ = owned
    first = compare.run("u", "q")
    key1 = compare.reveal("u", first["id"], "A")["chosen_identity"]
    models[0] = config.Settings(provider="anthropic", model="requested-a", api_key="key", base_url="https://second.invalid")
    second = compare.run("u", "q")
    key2 = compare.reveal("u", second["id"], "A")["chosen_identity"]
    assert key1 != key2 and compare.tally("u") == {key1: 1, key2: 1}


def test_disabled_calibration_performs_no_chain_io(owned, monkeypatch):
    monkeypatch.setattr(calibration, "path", lambda: pytest.fail("calibration path touched while off"))
    cid = compare.run("u", "q")["id"]
    assert compare.reveal("u", cid, "A")["calibration"] == "disabled"
    compare.recover("u", cid)


def test_calibration_links_runs_once_without_private_text_or_quality_claims(owned, monkeypatch):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    cid = compare.run("Case.Owner", "PRIVATE_PROMPT", system="PRIVATE_SYSTEM")["id"]
    first = compare.reveal("Case.Owner", cid, "A")
    assert first["calibration"] == "recorded"
    raw = calibration.path().read_bytes()
    assert not any(secret in raw for secret in (b"PRIVATE_PROMPT", b"PRIVATE_SYSTEM", b"Owned answer", b"Case.Owner", b"secret-a"))
    assert compare.recover("Case.Owner", cid)["calibration"] == "recorded"
    compare.reveal("Case.Owner", cid, "A")
    assert calibration.path().read_bytes() == raw
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 3 and calibration.verify()["ok"]
    assert {r["body"]["run_id"] for r in rows[:-1]} == set(rows[-1]["body"]["run_ids"])
    assert all(r["body"]["verified"] is False and r["body"]["eligible_for_promotion"] is False for r in rows)


@pytest.mark.parametrize("after", [False, True], ids=["before_chain", "chain_lost_ack"])
def test_calibration_failure_remains_pending_then_recovers(owned, monkeypatch, after):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    cid = compare.run("u", "q")["id"]
    publish = files.publish
    def fault(path, raw):
        if path == calibration.path():
            if after: publish(path, raw)
            raise OSError("owned calibration fault")
        publish(path, raw)
    monkeypatch.setattr(files, "publish", fault)
    assert compare.reveal("u", cid, "A")["calibration"] == "pending"
    assert sum(compare.tally("u").values()) == 1
    monkeypatch.setattr(files, "publish", publish)
    assert compare.recover("u", cid)["calibration"] == "recorded"
    assert len(calibration.path().read_bytes().splitlines()) == 3
    assert len(owned[1]) == 2


def test_calibration_owner_ack_failure_recovers_existing_chain(owned, monkeypatch):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    cid = compare.run("u", "q")["id"]
    publish = files.publish
    def fault(path, raw):
        if path == state.path("u") and b'"calibration":"recorded"' in raw:
            raise OSError("owned receipt fault")
        publish(path, raw)
    monkeypatch.setattr(files, "publish", fault)
    with pytest.raises(compare.CompareError): compare.reveal("u", cid, "A")
    raw = calibration.path().read_bytes()
    monkeypatch.setattr(files, "publish", publish)
    assert compare.recover("u", cid)["calibration"] == "recorded"
    assert calibration.path().read_bytes() == raw
    assert sum(compare.tally("u").values()) == 1


@pytest.mark.parametrize("raw", [b"bad\n", b"incomplete", b"[]\n", b'{"seq":NaN}\n'], ids=["bad", "tail", "list", "nan"])
def test_unavailable_calibration_is_not_repaired_or_emptied(owned, monkeypatch, raw):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    cid = compare.run("u", "q")["id"]
    calibration.path().write_bytes(raw)
    out = compare.reveal("u", cid, "A")
    assert out["calibration"] == "pending" and out["recovery_required"]
    assert calibration.path().read_bytes() == raw
    assert compare.recover("u", cid)["calibration"] == "pending"
    assert calibration.path().read_bytes() == raw


def test_collection_toggle_preserves_pending_without_writes(owned, monkeypatch):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    cid = compare.run("u", "q")["id"]
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "0")
    assert compare.reveal("u", cid, "A")["calibration"] == "pending"
    assert not calibration.path().exists()
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    assert compare.recover("u", cid)["calibration"] == "recorded"


def test_replay_and_absent_provider_receipts_cannot_enter_calibration(owned, monkeypatch):
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    def replay(*a, **k):
        execution.observe(provider="anthropic", model="replayed", replay=True)
        return "Replay answer"
    for dispatch in (replay, lambda *a, **k: "No provider receipt"):
        monkeypatch.setattr(backend, "_dispatch_text", dispatch)
        cid = compare.run("u", "q")["id"]
        assert compare.reveal("u", cid, "A")["calibration"] == "excluded"
    assert not calibration.path().exists()


def test_cli_actual_entrypoint_reports_errors_and_recovery(owned, monkeypatch, capsys):
    from olympus import cli, firstrun, opconfig
    monkeypatch.setattr(firstrun, "load_env_file", lambda: None)
    monkeypatch.setattr(opconfig, "apply_secrets", lambda: None)
    monkeypatch.setattr("sys.stdin", io.StringIO())
    cid = uuid.uuid4().hex
    assert cli.main(["compare", "owned", "--owner", "a.b", "--id", cid]) == 0
    assert cli.main(["compare", "--show", cid, "--owner", "a-b"]) == 1
    assert cli.main(["compare", "--reveal", cid, "--pick", "A", "--owner", "a.b"]) == 0
    assert cli.main(["compare", "--reveal", cid, "--pick", "B", "--owner", "a.b"]) == 1
    assert cli.main(["compare", "--recover", cid, "--owner", "a.b"]) == 0
    assert "first reveal decision is final" in capsys.readouterr().out
    assert len(owned[1]) == 2


@pytest.mark.parametrize("raw", ['[', '{}', 'null', '[{"model":42}]', '[{"model":"a","model":"b"}]', '[{"api_keys":"key"}]'],
                         ids=["syntax", "object", "null", "model_type", "duplicate", "keys_type"])
def test_invalid_pool_is_distinct_from_small_pool(monkeypatch, raw):
    monkeypatch.setenv("OLYMPUS_MODELS", raw)
    with pytest.raises(compare.CompareError) as raised: compare.available_models()
    assert raised.value.code == "pool_unavailable"


def test_moa_comparison_child_runs_never_fail_over_or_publish_shared_traces(owned, monkeypatch):
    from olympus import moa
    models, calls = owned
    children = list(models)
    monkeypatch.setattr(moa, "_members", lambda: children)
    monkeypatch.setattr(moa, "_aggregator", lambda refs: refs[0])
    monkeypatch.setattr(moa, "_save_trace", lambda *a, **k: pytest.fail("private comparison trace shared"))
    monkeypatch.setattr(backend, "complete_text", lambda *a, **k: pytest.fail("fallback surface used"))
    primitive = backend._dispatch_text
    def dispatch(settings, *args):
        if settings.provider == "moa": return moa.complete_text(settings, *args)
        return primitive(settings, *args)
    monkeypatch.setattr(backend, "_dispatch_text", dispatch)
    models[0] = config.Settings(provider="moa", model="ensemble")
    cid = compare.run("u", "q")["id"]
    receipt = compare.reveal("u", cid)["executions"][0]["provenance"]
    assert len(receipt["calls"]) == 4  # Parent, two references and aggregation.
    assert [r["requested_model"] for r in receipt["calls"]] == ["ensemble", "requested-a", "requested-b", "requested-a"]
    assert all(r["parent_run_id"] == receipt["calls"][0]["run_id"] for r in receipt["calls"][1:])
    assert len(calls) == 4  # Three child completions plus the independent B answer.


@pytest.fixture()
def web_request(owned, monkeypatch):
    """Exercise real HTTP parsing, authentication binding and dispatch in memory."""
    from email.message import Message
    from types import SimpleNamespace
    from olympus import web
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(web.accounts, "require_login", lambda: True)
    monkeypatch.setattr(web.accounts, "namespace_for_token", lambda token: {
        "cookie-owner-a": "Case.Owner", "cookie-owner-b": "Case-Owner"}.get(token))
    def call(payload=None, *, method="POST", cookie="cookie-owner-a", raw=None, length=None):
        handler = object.__new__(web.Handler)
        handler.path = "/api/compare"
        handler.client_address = ("127.0.0.1", 12345)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8123))
        handler.headers = Message()
        handler.headers["Cookie"] = "olympus_sid=" + cookie
        raw = raw if raw is not None else json.dumps(payload).encode()
        handler.headers["Content-Length"] = str(len(raw)) if length is None else length
        handler.headers["Content-Type"] = "application/json"
        handler.rfile = io.BytesIO(raw)
        result = []
        handler._send = lambda code, body, *a, **k: result.append((code, json.loads(body)))
        getattr(handler, "do_" + method)()
        assert len(result) == 1
        return result[0]
    return call


def test_web_real_cookie_principal_and_reveal_retry(web_request, owned):
    code, out = web_request({"op": "run", "prompt": "q", "session": "cookie-owner-b"})
    assert code == 200 and "mapping" not in out
    cid = out["id"]
    assert web_request({"op": "get", "id": cid}, cookie="cookie-owner-b")[0] == 404
    assert web_request({"op": "reveal", "id": cid, "choice": "A"})[0] == 200
    assert web_request({"op": "reveal", "id": cid, "choice": "A"})[0] == 200
    assert web_request({"op": "reveal", "id": cid, "choice": "B"})[0] == 409
    code, view = web_request(method="GET")
    assert code == 200 and sum(view["tally"].values()) == 1
    assert len(owned[1]) == 2


@pytest.mark.parametrize("payload", [[], None, {"op": []}, {"op": "run", "prompt": 7},
    {"op": "reveal", "id": "../victim"}, {"op": "run", "prompt": "q", "owner": "victim"},
    {"op": "initialize", "acknowledge_legacy": "true"}],
    ids=["array", "null", "op_type", "prompt_type", "path", "owner_injection", "ack_type"])
def test_web_rejects_malformed_typed_requests_before_work(web_request, owned, payload):
    assert web_request(payload)[0] == 400
    assert owned[1] == []


@pytest.mark.parametrize("raw,length", [(b'{"op":"run","op":"reveal"}', None),
    (b'{"prompt":NaN}', None), (b'{}', '-1'), (b'{}', '999999'), (b'{}', '3')],
    ids=["duplicate", "nan", "negative_length", "body_bound", "short_read"])
def test_web_strict_request_bytes(web_request, owned, raw, length):
    assert web_request(raw=raw, length=length)[0] == 400
    assert owned[1] == []


def test_web_unavailable_evidence_is_503_and_missing_auth_is_401(web_request, owned):
    path = state.path("Case.Owner")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"broken")
    assert web_request({"op": "run", "prompt": "q"})[0] == 503
    assert web_request(method="GET")[0] == 503
    assert web_request({"op": "run", "prompt": "q"}, cookie="not-authorized")[0] == 401
    assert owned[1] == [] and path.read_bytes() == b"broken"


def test_web_and_cli_report_calibration_pending(web_request, owned, monkeypatch, capsys):
    from olympus import cli, firstrun, opconfig
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "1")
    _, out = web_request({"op": "run", "prompt": "q"})
    monkeypatch.setenv("OLYMPUS_CALIBRATION", "0")
    code, out = web_request({"op": "reveal", "id": out["id"], "choice": "A"})
    assert code == 202 and out["calibration"] == "pending"
    monkeypatch.setattr(firstrun, "load_env_file", lambda: None)
    monkeypatch.setattr(opconfig, "apply_secrets", lambda: None)
    assert cli.main(["compare", "--recover", out["id"], "--owner", "Case.Owner"]) == 2
    assert "pending" in capsys.readouterr().out
    assert len(owned[1]) == 2


def test_owner_context_is_bound_and_restored(owned, monkeypatch):
    from olympus import memory
    prior = backend._dispatch_text
    seen = []
    def dispatch(*a, **k):
        seen.append(memory.current_owner())
        return prior(*a, **k)
    monkeypatch.setattr(backend, "_dispatch_text", dispatch)
    with memory.user_context("Caller.Identity"):
        compare.run("Target.Identity", "q")
        assert memory.current_owner() == "Caller.Identity"
    assert seen == ["Target.Identity", "Target.Identity"]


def test_uncommitted_interrupt_is_indeterminate_not_replayed(owned, monkeypatch):
    def interrupted(*a, **k): raise KeyboardInterrupt()
    monkeypatch.setattr(backend, "_dispatch_text", interrupted)
    cid = uuid.uuid4().hex
    with pytest.raises(KeyboardInterrupt): compare.run("u", "q", cid=cid)
    recovered = compare.recover("u", cid)
    assert all(a["state"] == "indeterminate" for a in recovered["answers"])
    assert compare.run("u", "q", cid=cid) == recovered


@pytest.mark.skipif(os.name == "nt", reason="POSIX process locks; Windows single-process limit remains")
def test_posix_competing_processes_record_one_vote(owned):
    import subprocess
    import sys
    cid = compare.run("u", "q")["id"]
    script = '''import json, sys
from pathlib import Path
from olympus import compare, config
config.MEMORY_DIR = Path(sys.argv[1])
sys.stdin.read(1)
try:
    out = compare.reveal("u", sys.argv[2], sys.argv[3])
    print(json.dumps({"choice": out["choice"]}))
except compare.CompareError as err:
    if err.code != "conflict": raise
    print(json.dumps({"conflict": True}))
'''
    children = [subprocess.Popen([sys.executable, "-c", script, str(config.MEMORY_DIR), cid, label],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for label in ("A", "B", "A", "B")]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda child: child.communicate("x", timeout=30), children))
        assert all(child.returncode == 0 for child in children), results
        decisions = [json.loads(stdout) for stdout, _ in results]
        assert len({d["choice"] for d in decisions if "choice" in d}) == 1
        assert sum(compare.tally("u").values()) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.skipif(os.name != "nt", reason="native Windows extended path contract")
def test_native_windows_long_paths(owned, monkeypatch, tmp_path):
    long_root = tmp_path / ("owned-" + "a" * 100) / ("owned-" + "b" * 100)
    monkeypatch.setattr(config, "MEMORY_DIR", long_root)
    assert len(str(state.path("Owner.With.Punctuation"))) > 260
    cid = compare.run("Owner.With.Punctuation", "q")["id"]
    assert compare.reveal("Owner.With.Punctuation", cid, "A")["phase"] == "revealed"
    assert sum(compare.tally("Owner.With.Punctuation").values()) == 1


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction refusal contract")
def test_native_windows_junction_refused(owned, tmp_path):
    import subprocess
    path = state.path("u")
    path.parent.parent.mkdir(parents=True)
    target = tmp_path / "owned-junction-target"
    target.mkdir()
    (target / "state.json").write_bytes(b"preserve")
    created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(path.parent), str(target)],
                             capture_output=True, text=True, timeout=15)
    assert created.returncode == 0, created.stderr
    with pytest.raises(compare.CompareError): compare.run("u", "q")
    assert (target / "state.json").read_bytes() == b"preserve" and owned[1] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_posix_directory_barrier_failure_is_retried(owned, monkeypatch):
    cid = compare.run("u", "q")["id"]
    sync = files.sync_dir
    failed = []
    def fault(path):
        if path == state.path("u").parent:
            failed.append(path)
            raise OSError("owned directory fsync failure")
        sync(path)
    monkeypatch.setattr(files, "sync_dir", fault)
    with pytest.raises(compare.CompareError): compare.reveal("u", cid, "A")
    assert failed
    monkeypatch.setattr(files, "sync_dir", sync)
    assert compare.recover("u", cid)["choice"] == "A"
    assert sum(compare.tally("u").values()) == 1


def test_provider_receipt_hooks_use_reported_identity(monkeypatch):
    from types import SimpleNamespace
    from olympus import openai_compat, bedrock_converse, claude_code
    settings = config.Settings(provider="openai", model="request-alias", base_url="https://owned.invalid/v1")
    monkeypatch.setattr(openai_compat, "_post", lambda *a, **k: {
        "model": "served-revision", "id": "owned-openai", "system_fingerprint": "fp-owned",
        "choices": [{"message": {"content": "owned"}}]})
    with execution.capture() as receipt:
        assert backend.complete_text_once(settings, "s", []) == "owned"
    response = receipt["responses"][0]
    assert response["model"] == "served-revision" and response["revision"] == "fp-owned"
    assert response["endpoint_sha256"] == execution.digest("https://owned.invalid/v1/chat/completions")

    fake = SimpleNamespace(converse=lambda **k: {"ResponseMetadata": {"RequestId": "owned-bedrock"},
        "output": {"message": {"content": [{"text": "owned"}]}}},
        meta=SimpleNamespace(endpoint_url="https://owned-bedrock.invalid"))
    settings = config.Settings(provider="bedrock", model="amazon.owned-alias")
    with execution.capture() as receipt:
        assert bedrock_converse.complete_text(settings, "s", [], client=fake) == "owned"
    assert receipt["responses"][0]["response_id"] == "owned-bedrock"
    assert receipt["responses"][0]["model"] is None  # Request alias is not a served revision.

    monkeypatch.setattr(claude_code, "_BIN", "owned-claude")
    monkeypatch.setattr(claude_code.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps({"result": "owned", "session_id": "owned-session"})))
    with execution.capture() as receipt:
        assert claude_code.complete_text(config.Settings(provider="claude-code", model="sonnet"), "s", []) == "owned"
    assert receipt["responses"][0]["model"] is None
    assert receipt["responses"][0]["response_id"] == "owned-session"


def test_anthropic_response_identity_hook(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from olympus import llm, replaystore
    message = SimpleNamespace(model="actual-served-model", id="owned-anthropic", usage=None,
                              content=[], stop_reason="end_turn")
    @contextmanager
    def stream(**kwargs):
        yield SimpleNamespace(get_final_message=lambda: message)
    fake = SimpleNamespace(messages=SimpleNamespace(stream=stream), base_url="https://owned.invalid")
    fake.beta = SimpleNamespace(messages=fake.messages)
    monkeypatch.setattr(llm, "client", lambda *a: fake)
    monkeypatch.setattr(replaystore, "replaying", lambda: False)
    monkeypatch.setattr(replaystore, "put", lambda *a, **k: None)
    monkeypatch.setattr(replaystore, "note_call", lambda *a, **k: None)
    with execution.capture() as receipt:
        llm.complete("s", [], settings=config.Settings(provider="anthropic", model="alias", api_key="owned"))
    assert receipt["responses"][0]["model"] == "actual-served-model"
    assert receipt["responses"][0]["response_id"] == "owned-anthropic"
    assert receipt["responses"][0]["revision"] is None


def test_browser_recovery_controls_with_owned_fetch(tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; M05 review must include the executed owned UI contract")
    source = Path("olympus/web.py").read_text(encoding="utf-8")
    script = source.split("// --- compare (blind multi-model) ---", 1)[1].split("const connectBtn =", 1)[0]
    harness = Path("tests/fixtures/compare_ui_owned.cjs")
    result = subprocess.run([node, str(harness)], input=script, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OWNED_COMPARE_UI_PASSED" in result.stdout
