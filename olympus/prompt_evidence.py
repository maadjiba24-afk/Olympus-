"""Benchmark-gated prompts with durable, specific-operation recovery.

Candidate evaluation uses a context-local prompt; concurrent serving continues
to see the original. A prepared plan preserves exact before/after bytes. Final
publication, signed evidence and operator notes are retryable without models.
An active receipt remains a read barrier until every acknowledgement is durable.
"""
from contextlib import contextmanager
from copy import deepcopy
import base64
import hashlib
import json
import math
import re
import stat
import time
import uuid

from . import config, deltas, memory, note_archive, note_evidence as notes, owner_evidence as oe, proclock

MAX_PROMPT = 128000
MAX_OPERATIONS = 1000
_STEM = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ID = re.compile(r"[0-9a-f]{32}")


def stem(value):
    if not isinstance(value, str):
        raise ValueError("invalid prompt name")
    # Preserve the documented argus.md / prompts/argus tool spellings only.
    value = value.removeprefix("prompts/").removesuffix(".md")
    if not _STEM.fullmatch(value):
        raise ValueError("unsafe prompt name")
    return value


def prompt_path(agent):
    path = notes.logical(config.PROMPTS_DIR) / (stem(agent) + ".md")
    for parent in path.parents:
        info = notes.io(parent).lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("linked or non-directory prompt parent")
    return path


def raw_prompt(agent):
    raw = note_archive.external_read(prompt_path(agent), MAX_PROMPT)
    if not raw.decode("utf-8").strip():
        raise ValueError("empty prompt")
    return raw


def _root(agent):
    return config.MEMORY_DIR / "prompt-operations-v1" / stem(agent)


def _active(agent):
    return _root(agent) / "active.json"


@contextmanager
def guard(agent):
    key = hashlib.sha256((str(notes.logical(config.PROMPTS_DIR)) + "\0"
                          + str(notes.logical(config.MEMORY_DIR)) + "\0" + stem(agent)).encode()).hexdigest()
    with proclock.lock("prompt-" + key):
        yield


def _encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode()


def _pack(raw):
    return base64.b64encode(raw).decode("ascii")


def _unpack(value):
    if not isinstance(value, str):
        raise ValueError("invalid prompt backup")
    raw = base64.b64decode(value, validate=True)
    if not raw or len(raw) > MAX_PROMPT:
        raise ValueError("prompt backup size invalid")
    raw.decode("utf-8")
    return raw


def _read(agent, identifier):
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise ValueError("invalid prompt operation ID")
    directory = _root(agent) / identifier
    plan_raw = notes.read_raw(directory / "plan.json")
    state_raw = notes.read_raw(directory / "state.json")
    plan = oe.decode(plan_raw, "prompt plan", notes.MAX_NOTE)
    state = oe.decode(state_raw, "prompt receipt", notes.MAX_NOTE)
    oe.fields(plan, ("version", "id", "agent", "prompt_root", "created", "before", "after",
                     "reason", "benchmark", "provenance", "restores", "legacy_backup"))
    if (type(plan["version"]) is not int or plan["version"] != 1 or plan["id"] != identifier
            or plan["agent"] != stem(agent) or plan["prompt_root"] != str(notes.logical(config.PROMPTS_DIR))):
        raise ValueError("prompt plan identity or configured root differs")
    _unpack(plan["before"]); _unpack(plan["after"])
    oe.number(plan["created"])
    oe.text(plan["reason"], 8192, empty=True)
    oe.records(plan["benchmark"], 4000)
    identifiers = set()
    for case in plan["benchmark"]:
        if not isinstance(case, dict) or case.get("specialist") != stem(agent):
            raise ValueError("benchmark principal differs")
        for key in ("id", "task", "criteria"):
            oe.text(case.get(key), 128000)
        if case["id"] in identifiers:
            raise ValueError("duplicate benchmark identity")
        identifiers.add(case["id"])
    oe.fields(plan["provenance"], ("source", "run_id", "trust", "detail"))
    for value in plan["provenance"].values():
        oe.text(value, 8192, empty=True)
    if plan["provenance"]["trust"] not in deltas._TRUST_ORDER:
        raise ValueError("unknown prompt evidence trust")
    if plan["restores"] is not None and not _ID.fullmatch(plan["restores"]):
        raise ValueError("invalid restoration identity")
    if plan["legacy_backup"] is not None:
        oe.text(plan["legacy_backup"], 4096)
        relative, digest = plan["legacy_backup"].rsplit(":", 1)
        from .sleeptime_evidence import digest as valid_digest
        valid_digest(digest)
        target = notes.checked_relative(relative)
        if target.parent != notes.directory("shared", "prompt_backups"):
            raise ValueError("legacy backup outside the shared backup directory")
    oe.fields(state, ("plan_sha256", "phase", "before_result", "after_result", "decision", "error", "snapshot"))
    if (state["plan_sha256"] != notes.digest(plan_raw)
            or state["phase"] not in ("prepared", "baseline", "measured", "complete")
            or state["decision"] not in (None, "kept", "reverted", "refused", "restored")):
        raise ValueError("invalid prompt receipt")
    oe.text(state["error"], 8192, empty=True)
    if state["snapshot"] is not None:
        from .sleeptime_evidence import digest
        digest(state["snapshot"])
    for value in (state["before_result"], state["after_result"]):
        if value is not None:
            _score(value, [item["id"] for item in plan["benchmark"]])
    if state["decision"] == "kept":
        if (state["before_result"] is None or state["after_result"] is None
                or state["after_result"]["avg"] < state["before_result"]["avg"]):
            raise ValueError("kept prompt lacks benchmark qualification")
    if state["decision"] == "restored" and not (plan["restores"] or plan["legacy_backup"]):
        raise ValueError("restore lacks a specific backup")
    if state["phase"] == "complete" and state["snapshot"] is None:
        raise ValueError("completed prompt has no signed evidence")
    return plan, state


def _pending(agent):
    raw = notes.read_raw(_active(agent), 4096)
    if raw is None:
        return None
    pointer = oe.decode(raw, "prompt operation", 4096)
    oe.fields(pointer, ("id",))
    plan, state = _read(agent, pointer["id"])
    return plan, state


def read_effective(agent):
    # Atomic receipt reads don't wait for a long-running benchmark. While it is
    # in progress, readers see the bound before-state rather than a trial prompt.
    pending = _pending(agent)
    current = raw_prompt(agent)
    if pending is not None:
        plan, state = pending
        if current not in (_unpack(plan["before"]), _unpack(plan["after"])):
            raise oe.OwnerEvidenceStateError("prompt", "active operation target changed")
        return _unpack(plan["before"]).decode("utf-8")
    return current.decode("utf-8")


def history(agent):
    root = _root(agent)
    if not notes._check_dir(root):
        return []
    out = []
    for path in notes.io(root).iterdir():
        if path.name == "active.json" or (path.name.startswith(".note-") and path.name.endswith(".tmp")):
            notes.read_raw(path)
            continue
        if not _ID.fullmatch(path.name) or not notes._check_dir(path):
            raise ValueError("unexpected prompt operation inventory")
        if not notes.inventory(path) and _rolled_back_preparation(agent, path.name):
            continue
        out.append(_read(agent, path.name))
        if len(out) > MAX_OPERATIONS:
            raise ValueError("prompt operation bound exceeded; preserve history")
    return sorted(out, key=lambda pair: (pair[0]["created"], pair[0]["id"]))


def _rolled_back_preparation(agent, identifier):
    """An empty directory is harmless only with its verified rollback receipt."""
    raw = notes.read_raw(notes._journal_root() / identifier / "result.json", 4096)
    if raw is None:
        return False
    result = oe.decode(raw, "prompt preparation rollback", 4096)
    oe.fields(result, ("version", "id", "sha256", "decision"))
    if result["id"] != identifier or result["decision"] != "rollback":
        return False
    plan = notes._load_plan({key: result[key] for key in ("version", "id", "sha256")})
    expected = {notes.relative(_root(agent) / identifier / name) for name in ("plan.json", "state.json")}
    expected.add(notes.relative(_active(agent)))
    return ({item["path"] for item in plan["changes"]} == expected
            and all(item["before"] is None for item in plan["changes"]))


def status(agent):
    try:
        with guard(agent):
            pending = _pending(agent)
            return {"state": "recovery required" if pending else "available",
                    "agent": stem(agent), "operation": pending[0]["id"] if pending else None,
                    "phase": pending[1]["phase"] if pending else None,
                    "operations": len(history(agent))}
    except Exception as err:
        return {"state": "unavailable", "agent": str(agent), "reason": str(err)}


def _score(result, identifiers):
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise ValueError("benchmark lacks item evidence")
    oe.number(result.get("avg"))
    items = result["items"]
    if not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in items):
        raise ValueError("invalid benchmark item evidence")
    if not identifiers or len(items) != len(identifiers) or {r.get("id") for r in items} != set(identifiers):
        raise ValueError("benchmark coverage differs from the pinned cases")
    for item in items:
        oe.number(item.get("score"))
        if item["score"] > 10:
            raise ValueError("benchmark score outside its range")
    average = sum(item["score"] for item in items) / len(items)
    if not math.isclose(result["avg"], average, abs_tol=.0051):
        raise ValueError("benchmark average differs from its item evidence")
    return deepcopy(result)


def _save_state(plan, state):
    raw = _encode(state)
    if len(raw) > notes.MAX_NOTE:
        raise ValueError("prompt receipt byte bound exceeded")
    notes.publish(_root(plan["agent"]) / plan["id"] / "state.json", raw)


def _prepare(agent, before, after, reason, benchmark, provenance, *, restores=None, legacy_backup=None):
    if _pending(agent) is not None:
        raise ValueError("prompt recovery required before another mutation")
    if len(history(agent)) >= MAX_OPERATIONS:
        raise ValueError("prompt history capacity reached; preserve backups")
    plan = dict(version=1, id=uuid.uuid4().hex, agent=agent,
                prompt_root=str(notes.logical(config.PROMPTS_DIR)), created=time.time(),
                before=_pack(before), after=_pack(after), reason=reason,
                benchmark=benchmark, provenance=provenance, restores=restores, legacy_backup=legacy_backup)
    raw = _encode(plan)
    if len(raw) > notes.MAX_NOTE:
        raise ValueError("prompt plan byte bound exceeded")
    state = dict(plan_sha256=notes.digest(raw), phase="prepared", before_result=None,
                 after_result=None, decision=None, error="", snapshot=None)
    changes = {
        notes.relative(_root(agent) / plan["id"] / "plan.json"): raw,
        notes.relative(_root(agent) / plan["id"] / "state.json"): _encode(state),
        notes.relative(_active(agent)): _encode({"id": plan["id"]}),
    }
    # The immutable plan is the exact backup; the shared note is its familiar
    # operator-facing projection. Both survive later rollback.
    notes.transact(changes, {name: None for name in changes}, operation=plan["id"])
    return plan, state


def _message(plan, state):
    name, outcome = plan["agent"], state["decision"]
    if outcome == "kept":
        return f"Prompt '{name}' gated & kept [{state['before_result']['avg']}→{state['after_result']['avg']}] — {plan['reason']}"
    if outcome == "restored":
        return f"Prompt '{name}' restored from its specific recorded backup."
    if outcome == "refused":
        return f"Cannot gate '{name}': {state['error']}"
    return f"Prompt '{name}' reverted; original bytes preserved. {state['error']}"


def _finish(plan, state):
    if state["decision"] is None:
        raise ValueError("benchmark evidence incomplete; use explicit rollback recovery")
    agent = plan["agent"]
    old, new = _unpack(plan["before"]), _unpack(plan["after"])
    current = raw_prompt(agent)
    if current not in (old, new):
        raise ValueError("prompt changed since preparation; stale publication/rollback refused")
    desired = new if state["decision"] in ("kept", "restored") else old
    message = _message(plan, state)
    # A specific-operation backup and retryable signed receipt replace the
    # old latest-file rollback stack. No unrelated backup is consumed.
    notes.create("shared", "prompt_backups", agent, old.decode("utf-8"),
                 action_id="prompt-backup:" + plan["id"])
    note_archive.external_publish(prompt_path(agent), desired)
    if raw_prompt(agent) != desired:
        raise ValueError("prompt publication could not be confirmed")
    snap = deltas.record_snapshot("prompt:" + agent, kind="prompt",
        state={"prompt": desired.decode("utf-8"), "kept": state["decision"] == "kept",
               "gate": message, "decision": state["decision"],
               "before_result": state["before_result"], "after_result": state["after_result"],
               "plan_sha256": state["plan_sha256"]},
        delta={"operation": plan["id"], "restores": plan["restores"]},
        provenance=plan["provenance"], operation_id=plan["id"] + ":" + state["decision"],
        require_signature=True)
    notes.create("shared", "evals", "prompt gate: " + agent, message,
                 action_id="prompt-report:" + plan["id"] + ":" + state["decision"])
    state.update(phase="complete", snapshot=snap["snapshot_hash"])
    _save_state(plan, state)
    notes.remove(_active(agent))
    return {"status": state["decision"], "kept": state["decision"] == "kept",
            "message": message, "operation": plan["id"], "snapshot": snap["snapshot_hash"]}


def gate(agent, new_prompt, reason, settings=None, *, provenance=None):
    from . import agent as agent_module, evals
    identifier = None
    try:
        agent = stem(agent)
        oe.text(new_prompt, MAX_PROMPT)
        oe.text(reason, 8192, empty=True)
        candidate = (new_prompt.strip() + "\n").encode()
        if len(candidate) > MAX_PROMPT:
            raise ValueError("candidate prompt exceeds byte bound")
        with guard(agent), memory.user_context("shared"):
            if _pending(agent) is not None:
                raise ValueError("prompt recovery required; inspect prompt-status")
            original = raw_prompt(agent)
            benchmark = evals.load_benchmarks(strict=True)
            with evals.benchmark_snapshot(benchmark):
                identifiers = evals.ids_for([agent])
            if not identifiers:
                return {"status": "refused", "kept": False, "message":
                    f"Cannot benchmark-gate '{agent}': no coverage; generate_benchmark first."}
            cases = [case for case in benchmark if case["id"] in identifiers]
            if (len(identifiers) != len(set(identifiers)) or len(cases) != len(identifiers)
                    or any(case["specialist"] != agent for case in cases)):
                raise ValueError("benchmark identity or specialist coverage differs")
            plan, state = _prepare(agent, original, candidate, reason, cases,
                                  provenance or deltas.Provenance(source="prompt-gate", trust="operator").to_dict())
            identifier = plan["id"]
            try:
                with evals.benchmark_snapshot(cases), agent_module.benchmark_prompt(agent, original.decode()):
                    state["before_result"] = _score(evals.run(settings, only=identifiers), identifiers)
                state["phase"] = "baseline"
                _save_state(plan, state)
                with evals.benchmark_snapshot(cases), agent_module.benchmark_prompt(agent, candidate.decode()):
                    state["after_result"] = _score(evals.run(settings, only=identifiers), identifiers)
                state["decision"] = ("kept" if state["after_result"]["avg"] >= state["before_result"]["avg"] else "reverted")
                if state["decision"] == "reverted":
                    state["error"] = "benchmark regressed"
            except Exception as err:
                state["decision"] = "refused" if state["before_result"] is None else "reverted"
                state["error"] = "benchmark unavailable: " + str(err)[:7000]
            state["phase"] = "measured"
            _save_state(plan, state)
            return _finish(plan, state)
    except Exception as err:
        return {"status": "unavailable", "kept": False, "operation": identifier,
                "message": f"Prompt operation {identifier or '(not prepared)'} unavailable: {err}. Preserve evidence; inspect prompt-status."}


def recover(agent, identifier, decision):
    if decision not in ("finish", "rollback"):
        raise ValueError("choose finish or rollback")
    with guard(agent), memory.user_context("shared"):
        pending = _pending(agent)
        if pending is None:
            plan, state = _read(agent, identifier)
            if state["phase"] == "complete":
                return {"status": state["decision"], "operation": identifier, "snapshot": state["snapshot"]}
            raise ValueError("operation is not active; preserve orphan evidence")
        plan, state = pending
        if plan["id"] != identifier:
            raise ValueError("a different prompt operation is active")
        if decision == "rollback" and state["phase"] != "complete" and state["decision"] != "reverted":
            state.update(decision="reverted", error="explicit operator rollback recovery")
            _save_state(plan, state)
        return _finish(plan, state)


def restore(agent):
    agent = stem(agent)
    with guard(agent), memory.user_context("shared"):
        if _pending(agent) is not None:
            raise ValueError("prompt recovery required before selecting a backup")
        entries = history(agent)
        restored = {p["restores"] for p, s in entries if s["phase"] == "complete" and s["decision"] == "restored"}
        available = [(p, s) for p, s in entries if s["phase"] == "complete"
                     and s["decision"] == "kept" and p["id"] not in restored]
        current = raw_prompt(agent)
        legacy = None
        if available:
            previous, _ = available[-1]
            if current != _unpack(previous["after"]):
                raise ValueError("current prompt differs from the specific backup's successor; stale restore refused")
            replacement, restores = _unpack(previous["before"]), previous["id"]
        else:
            consumed = {p["legacy_backup"] for p, s in entries if s["phase"] == "complete"
                        and s["decision"] == "restored" and p["legacy_backup"]}
            projections = {hashlib.sha256(("note-action\0shared\0prompt-backup:" + p["id"]).encode()).hexdigest()
                           for p, s in entries}
            backups = [r for r in notes.notes("shared", "prompt_backups")
                       if memory.note_title(r["body"]) == agent
                       and r["meta"].get("operation") not in projections
                       and notes.relative(r["path"]) + ":" + r["sha256"] not in consumed]
            if not backups:
                return {"status": "refused", "kept": False, "message": f"Error: no backups exist for '{agent}'."}
            selected = sorted(backups, key=lambda r: (r["meta"].get("created", ""), r["path"].name))[-1]
            # Explicit manual legacy restore only; automatic gate recovery never
            # infers the before-state from a legacy/latest backup.
            body = selected["body"].split("\n", 1)[1].lstrip("\n")
            body = body.split("<!-- update reason:", 1)[0]
            replacement, restores = (body.strip() + "\n").encode(), None
            legacy = notes.relative(selected["path"]) + ":" + selected["sha256"]
        plan, state = _prepare(agent, current, replacement, "operator requested specific-backup restore", [],
                              deltas.Provenance(source="prompt-restore", trust="operator").to_dict(),
                              restores=restores, legacy_backup=legacy)
        state.update(phase="measured", decision="restored")
        _save_state(plan, state)
        return _finish(plan, state)
