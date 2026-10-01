"""Blind comparison with exact owners, durable decisions and no-call recovery.

Provider calls use complete_text_once: no cross-member substitution. A durable
started marker precedes each call. Interrupted/unpersisted answers are explicitly
indeterminate; retrying an id never calls a model again.
"""
from __future__ import annotations

import json
import dataclasses
import os
import random
import string
import time
import uuid

from . import backend, calibration, config, memory, owner_evidence as ev
from . import compare_execution as execution, compare_state as state
from .compare_state import CompareError

_MAX_STORED = state.MAX_RECORDS
_DEFAULT_SYSTEM = ("You are a helpful assistant. Answer the user's message "
                   "directly, accurately, and concisely.")
_SNIPPET = ('OLYMPUS_MODELS=[{"provider":"anthropic","model":"claude-sonnet-5",'
            '"api_key":"env:ANTHROPIC_API_KEY"}]')


def setup_hint():
    return ("Blind compare needs at least two configured models. Add one to "
            "OLYMPUS_MODELS (a second Anthropic model reuses your existing key "
            "and egress), e.g.:\n  " + _SNIPPET)


def model_label(settings):
    return settings.provider + ("/" + settings.model if settings.model else "")


def available_models():
    """Validate the raw pool before ModelPool's compatibility reader filters it."""
    try:
        raw = os.environ.get("OLYMPUS_MODELS", "")
        if raw:
            rows = ev.decode(raw.encode(), "comparison pool", 65536)
            ev.records(rows, 25)
            for row in rows:
                if not isinstance(row, dict) or set(row) - {"provider", "model", "api_key", "api_keys", "base_url"}:
                    raise ValueError("invalid member")
                if row.get("provider", "anthropic") not in ("anthropic", "openai", "bedrock", "claude-code", "moa"):
                    raise ValueError("invalid provider")
                if "model" in row and not isinstance(row["model"], str):
                    raise ValueError("invalid model")
                for key in ("provider", "model", "api_key", "base_url"):
                    if key in row and row[key] is not None:
                        ev.text(row[key], 8192, empty=key in ("model", "api_key"))
                keys = row.get("api_keys")
                if keys is not None:
                    ev.records(keys, 32)
                    for key in keys:
                        ev.text(key, 8192)
        pool = config.ModelPool.from_env()
        out, seen = [], set()
        for member in pool.members:
            if member.validate() is not None:
                raise ValueError("invalid configured member")
            if not member.usable():
                continue
            if member.provider in ("anthropic", "bedrock"):
                member = dataclasses.replace(member, model=config.require_model(member))
            fp = (member.provider, member.model, member.base_url)
            if fp not in seen:
                seen.add(fp)
                out.append(member)
        if len(out) > 26:
            raise ValueError("too many models")
        return out
    except Exception as err:
        # Configuration/credential errors must not become 'only one model'.
        raise CompareError("pool_unavailable", "The comparison model pool is invalid or unavailable; no models were called.") from err


def _configured(settings, effort):
    # Hash endpoint selection, never persist credentials or a credential-bearing
    # URL. Provider receipts separately identify the endpoint actually used.
    endpoint = {"base_url": settings.base_url, "provider": settings.provider}
    if settings.provider == "anthropic" and not settings.base_url:
        endpoint["base_url"] = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    if settings.provider == "bedrock":
        endpoint["region"] = (os.environ.get("OLYMPUS_BEDROCK_REGION") or
                              os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    if settings.provider == "openai":
        endpoint.update(azure_deployment=os.environ.get("OLYMPUS_AZURE_DEPLOYMENT"),
                        azure_api_version=os.environ.get("OLYMPUS_AZURE_API_VERSION"))
    return {"provider": settings.provider, "model": settings.model,
            "endpoint_sha256": execution.digest(endpoint), "effort": effort}


def _get(snapshot, cid):
    state.identifier(cid)
    if cid in snapshot["archive"]:
        raise CompareError("expired", "Answer details expired; the comparison id and vote receipt remain reserved.", 410, cid=cid)
    return snapshot["records"].get(cid)


def _view(rec, snapshot):
    out = {"id": rec["id"], "prompt": rec["prompt"], "phase": rec["phase"],
           "calibration": rec["calibration"], "recovery_required": rec["phase"] == "running" or rec["calibration"] == "pending",
           "answers": [{"label": m["label"], "text": m["text"] or state.INDETERMINATE,
                        "state": m["state"], "eligible": m["state"] == "answered" and rec["phase"] == "complete"
                        and sum(x["state"] == "answered" for x in rec["members"]) >= 2}
                       for m in rec["members"]]}
    if rec["phase"] == "revealed":
        win = state.winner(rec)
        out.update(mapping={m["label"]: state.model(m) for m in rec["members"]},
                   choice=rec["choice"], chosen_model=win["model"] if win else None,
                   chosen_identity=win["identity"] if win else None,
                   executions=rec["members"], tally=_tally(snapshot), tally_rows=state.tallies(snapshot))
    return out


def _link(uid, snapshot, rec):
    if rec["calibration"] != "pending":
        return
    from . import compare_calibration
    receipt = compare_calibration.link(uid, rec)
    if receipt is not None:
        rec["calibration"], rec["calibration_receipt"] = "recorded", receipt
        state.save(uid, snapshot, cid=rec["id"])


def run(user, prompt, *, effort="high", system="", cid=None):
    with state.guard(user), memory.user_context(user):
        state.text(prompt)
        state.text(system, empty=True)
        ev.text(effort, 32)
        cid = uuid.uuid4().hex if cid is None else state.identifier(cid)
        system = system or _DEFAULT_SYSTEM
        request = state.request_hash(prompt, execution.digest(system), effort)
        snapshot = state.load(user)
        prior = _get(snapshot, cid)
        if prior is not None:
            if prior["request_hash"] != request:
                raise CompareError("conflict", "This id belongs to a different request.", 409, cid=cid)
            return _view(prior, snapshot)  # No pool lookup, re-shuffle or provider retry.
        models = available_models()
        if len(models) < 2:
            return {"error": "Blind compare needs at least two configured models.",
                    "code": "insufficient_models", "hint": setup_hint(), "snippet": _SNIPPET,
                    "models": [model_label(m) for m in models]}
        if len(models) > 26:
            raise CompareError("invalid", "At most 26 comparison members are supported.", 400)
        state.reserve(snapshot, _MAX_STORED, members=len(models))
        order = list(range(len(models)))
        random.shuffle(order)
        members = []
        for index, model_index in enumerate(order):
            cfg = _configured(models[model_index], effort)
            provenance = {"responses": [], "calls": [], "overflow": False}
            members.append({"label": string.ascii_uppercase[index], "run_id": uuid.uuid4().hex,
                "configured": cfg, "state": "not_started", "text": "", "provenance": provenance,
                "identity": execution.identity(cfg, provenance)})
        rec = {"id": cid, "request_hash": request, "prompt": prompt, "system_hash": execution.digest(system),
               "effort": effort, "created": time.time(), "phase": "running", "members": members,
               "choice": None, "calibration": "waiting" if calibration.enabled() else "disabled",
               "calibration_receipt": None, "decision_at": None}
        snapshot["records"][cid] = rec
        state.save(user, snapshot, cid=cid)  # Prepared before the first call.
        for member, model_index in zip(members, order):
            member["state"] = "started"
            state.save(user, snapshot, cid=cid)
            with execution.capture(member["run_id"]) as receipt:
                try:
                    answer = backend.complete_text_once(models[model_index], system,
                        [{"role": "user", "content": prompt}], effort=effort)
                    state.text(answer)
                    member.update(state="answered", text=answer)
                except Exception:
                    # Never expose provider names, credentials or exception text
                    # through a blind answer. The exact called config is retained.
                    member.update(state="failed", text=state.FAILED)
            member["provenance"] = receipt
            member["identity"] = execution.identity(member["configured"], receipt)
            state.save(user, snapshot, cid=cid)
        rec["phase"] = "complete"
        state.save(user, snapshot, cid=cid)
        return _view(rec, snapshot)


def get(user, cid):
    with state.guard(user):
        snapshot = state.load(user)
        rec = _get(snapshot, cid)
        return None if rec is None else _view(rec, snapshot)


def reveal(user, cid, choice=""):
    with state.guard(user):
        ev.text(choice, 8, empty=True)
        choice = choice.strip().upper() or None
        snapshot = state.load(user)
        rec = _get(snapshot, cid)
        if rec is None:
            return None
        if rec["phase"] == "running":
            raise CompareError("recovery_required", "Recover this interrupted comparison before revealing it.", 409, cid=cid)
        if rec["phase"] == "revealed":
            if choice is not None and choice != rec["choice"]:
                raise CompareError("conflict", "The first reveal decision is final; a new vote would no longer be blind.", 409, cid=cid)
        else:
            eligible = [m["label"] for m in rec["members"] if m["state"] == "answered"]
            if choice is not None and (choice not in eligible or len(eligible) < 2):
                raise CompareError("ineligible", "A pick requires at least two durable answers and an eligible label.", 409, cid=cid)
            rec.update(phase="revealed", choice=choice, decision_at=calibration._now_iso())
            if rec["calibration"] == "waiting":
                receipts = [m["provenance"] for m in rec["members"]]
                excluded = any(r["overflow"] or not r["responses"] or any(x["replay"] for x in r["responses"]) for r in receipts)
                rec["calibration"] = "excluded" if excluded else "pending"
            state.save(user, snapshot, cid=cid)  # Decision, tally and outbox share the commit point.
        _link(user, snapshot, rec)
        return _view(rec, snapshot)


def recover(user, cid):
    """Reconcile persisted work only. Never perform a provider call."""
    with state.guard(user):
        snapshot = state.load(user)
        rec = _get(snapshot, cid)
        if rec is None:
            return None
        if rec["phase"] == "running":
            for member in rec["members"]:
                if member["state"] in ("not_started", "started"):
                    member.update(state="indeterminate", text=state.INDETERMINATE)
            rec["phase"] = "complete"
        # Also re-establish a failed directory barrier after a successful replace.
        state.save(user, snapshot, cid=cid)
        _link(user, snapshot, rec)
        return _view(rec, snapshot)


def _tally(snapshot):
    return {r["identity"]: r["count"] for r in state.tallies(snapshot)}


def tally(user):
    with state.guard(user):
        return _tally(state.load(user))


def render_tally(user):
    with state.guard(user):
        rows = state.tallies(state.load(user))
        if not rows:
            return "No blind picks yet."
        return "Your blind picks so far:\n" + "\n".join(
            f"  {r['model']} [{r['identity'][:12]}]: {r['count']}" for r in rows)


def info(user):
    with state.guard(user):
        snapshot = state.load(user)
        models = [model_label(m) for m in available_models()]
        return {"models": models, "tally": _tally(snapshot), "tally_rows": state.tallies(snapshot),
                **({"snippet": _SNIPPET} if len(models) < 2 else {}),
                "recent": [{"id": r["id"], "phase": r["phase"], "calibration": r["calibration"]}
                           for r in snapshot["records"].values()]}


def initialize_empty(user, *, acknowledge_legacy=False):
    with state.guard(user):
        if type(acknowledge_legacy) is not bool:
            raise ValueError("invalid acknowledgement")
        if state.legacy(user) and not acknowledge_legacy:
            raise CompareError("unclaimed_legacy", "Explicit acknowledgement of preserved, unclaimed legacy evidence is required.", 409)
        snapshot = state.load(user, allow_legacy=True)  # Refuses damaged existing v2.
        if ev.read_bytes(state.path(user), state.MAX_BYTES, state.NAME) is None:
            state.save(user, snapshot)
        return {"state": "valid", "legacy_preserved": state.legacy(user)}


def export(user):
    with state.guard(user):
        return state.load(user)


def cli_command(args):
    import sys
    try:
        actions = [args.tally, args.status, args.initialize_empty, args.show,
                   args.recover, args.reveal, args.prompt]
        if (sum(bool(a) for a in actions) != 1 or (args.pick and not args.reveal)
                or (args.id and not args.prompt)
                or (args.acknowledge_unclaimed_legacy and not args.initialize_empty)):
            raise CompareError("invalid", "Choose one comparison operation; --pick requires --reveal and --id requires a prompt.", 400)
        if args.tally:
            print(render_tally(args.owner))
            return 0
        if args.status:
            out = info(args.owner)
        elif args.initialize_empty:
            out = initialize_empty(args.owner, acknowledge_legacy=args.acknowledge_unclaimed_legacy)
        elif args.show:
            out = get(args.owner, args.show)
        elif args.recover:
            out = recover(args.owner, args.recover)
        elif args.reveal:
            out = reveal(args.owner, args.reveal, args.pick or "")
        else:
            cid = args.id or uuid.uuid4().hex
            print("Comparison id: " + cid + " (use --recover with this id after interruption)", flush=True)
            out = run(args.owner, args.prompt, cid=cid)
        if out is None:
            print("Comparison not found.")
            return 1
        print(json.dumps(out, indent=2, ensure_ascii=True))
        if "error" in out:
            return 1
        if out.get("recovery_required"):
            print("Recovery remains pending. No model call needs to be repeated.")
            return 2
        if args.prompt and out.get("phase") == "complete" and sys.stdin.isatty():
            choice = input("Pick a durable answer [label / Enter to reveal without a vote]: ")
            result = reveal(args.owner, out["id"], choice)
            print(json.dumps(result, indent=2, ensure_ascii=True))
            return 2 if result.get("recovery_required") else 0
        return 0
    except CompareError as err:
        print(json.dumps(err.payload(), ensure_ascii=True))
        return 1
