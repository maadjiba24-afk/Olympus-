"""Request-scoped provider receipts for blind comparisons; never enables I/O.

A configured model name is not proof of a served revision. Providers which do
not report an identity leave it unavailable. This is provenance, not attestation
or evidence of correctness. Outside capture() the hooks do nothing.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import uuid

_CURRENT = ContextVar("compare_execution", default=None)
_CALL = ContextVar("compare_call", default=None)
_ROOT = ContextVar("compare_root", default=None)
MAX_RESPONSES = 16


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def capturing():
    return _CURRENT.get() is not None


@contextmanager
def capture(run_id=None):
    receipt = {"responses": [], "calls": [], "overflow": False}
    token = _CURRENT.set(receipt)
    root = _ROOT.set(run_id or uuid.uuid4().hex)
    call = _CALL.set(None)
    try:
        yield receipt
    finally:
        _CALL.reset(call)
        _ROOT.reset(root)
        _CURRENT.reset(token)


@contextmanager
def dispatch(settings):
    receipt = _CURRENT.get()
    if receipt is None:
        yield
        return
    if len(receipt["calls"]) >= MAX_RESPONSES:
        receipt["overflow"] = True
        raise ValueError("comparison execution bound exceeded")
    row = {"run_id": _ROOT.get() if not receipt["calls"] else uuid.uuid4().hex,
           "parent_run_id": _CALL.get(), "provider": settings.provider,
           "requested_model": settings.model,
           "endpoint_sha256": digest(settings.base_url), "state": "started"}
    receipt["calls"].append(row)
    token = _CALL.set(row["run_id"])
    try:
        yield
    except BaseException:
        row["state"] = "failed"
        raise
    else:
        row["state"] = "returned"
    finally:
        _CALL.reset(token)


def observe(*, provider, model=None, response_id=None, revision=None,
            endpoint=None, replay=False):
    receipt = _CURRENT.get()
    if receipt is None:
        return
    if len(receipt["responses"]) >= MAX_RESPONSES:
        receipt["overflow"] = True
        return

    def bounded(value):
        return value if isinstance(value, str) and 0 < len(value) <= 1024 else None

    receipt["responses"].append({
        "run_id": _CALL.get() or _ROOT.get(),
        "provider": bounded(provider), "model": bounded(model),
        "response_id": bounded(response_id), "revision": bounded(revision),
        "endpoint_sha256": digest(endpoint) if isinstance(endpoint, str) else None,
        "replay": replay is True,
    })


def identity(config, receipt):
    """Response ids identify runs, and must not fragment configuration tallies."""
    return digest({"config": config, "overflow": receipt["overflow"],
                   "calls": [{k: v for k, v in row.items() if k not in ("run_id", "parent_run_id")}
                             for row in receipt["calls"]],
                   "responses": [{k: v for k, v in row.items() if k not in ("response_id", "run_id")}
                                 for row in receipt["responses"]]})
