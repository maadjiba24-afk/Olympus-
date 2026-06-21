"""Content-addressed LLM request/response store for re-executable replay.

This is the foundation of Olympus's decision log (Task 1 of the moat plan):
**freeze the reasoning, then re-run the real orchestration code against the
frozen responses** to prove the decision path is byte-identical — or pinpoint
the exact request where a code/prompt change would have changed a decision.

How it works:
- **Record mode (default):** every `llm.complete()` request is hashed
  deterministically (canonical JSON: keys sorted, the server-allocated
  `container` field dropped) and its response is stored at
  `MEMORY_DIR/responses/<hash>.json`.
- **Replay mode (`OLYMPUS_REPLAY=<run_id>`):** `complete()` returns the stored
  response for the request's hash with **no network call**. A request whose
  hash isn't on disk means the orchestration produced a *different* request
  than last time, so it raises `ReplayDivergence` naming that exact divergence.

Versus Ruflo's `StateReconstructor`, which replays recorded *state*: it never
stores the model request/response, so it cannot re-run the logic and detect
that a code or prompt change would have altered a decision. We replay the
reasoning itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

import anthropic

from . import config

# The server allocates `container` per request (web-search code execution), so
# it changes every run; hashing it would make replay never match. Everything
# else — system, messages, model, tools, mcp_servers, thinking, output_config —
# is part of the decision and *should* diverge if it changes.
_EXCLUDE_FROM_HASH = ("container",)

_local = threading.local()


class ReplayDivergence(RuntimeError):
    """Raised in replay mode when a request has no recorded response — i.e. the
    orchestration produced a different request than the recorded run."""

    def __init__(self, request_hash: str, params: dict):
        self.request_hash = request_hash
        self.params = params
        model = params.get("model", "?")
        super().__init__(
            f"replay divergence at request {request_hash[:12]} (model={model}): "
            "no recorded response — a code or prompt change altered this "
            "decision's request since the recorded run.")


def canonical_request(params: dict) -> bytes:
    """Deterministic bytes for a request: keys sorted recursively (json
    sort_keys), the server-allocated `container` dropped. Structurally-equal
    requests serialize identically, so they hash identically."""
    cleaned = {k: v for k, v in params.items() if k not in _EXCLUDE_FROM_HASH}
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def request_hash(params: dict) -> str:
    return hashlib.sha256(canonical_request(params)).hexdigest()


def _dir():
    d = config.MEMORY_DIR / "responses"
    d.mkdir(parents=True, exist_ok=True)
    return d


def put(h: str, message: "anthropic.types.Message") -> None:
    (_dir() / f"{h}.json").write_text(message.to_json(), encoding="utf-8")


def get(h: str) -> "anthropic.types.Message | None":
    path = _dir() / f"{h}.json"
    if not path.exists():
        return None
    return anthropic.types.Message.model_validate_json(
        path.read_text(encoding="utf-8"))


def replaying() -> str | None:
    """The run_id being replayed (from OLYMPUS_REPLAY), or None in record mode."""
    return os.environ.get("OLYMPUS_REPLAY") or None


def note_call(h: str) -> None:
    """Record the most recent request/response hash on this thread so the
    decision layer can stamp `model_request_hash` / `model_response_ref`."""
    _local.last = h


def last_ref() -> str | None:
    return getattr(_local, "last", None)


# --- client-side tool results --------------------------------------------
#
# Freezing the LLM responses isn't enough for byte-identical replay: a
# client-side tool can be nondeterministic (e.g. `current_time`) or read state
# that changed since the recorded run. So we also freeze each tool result,
# keyed by the model-issued tool_use id — which is itself part of the (frozen)
# assistant message, so it's stable across a replay. On replay the recorded
# result is returned instead of re-executing the tool.

def _tool_dir():
    d = config.MEMORY_DIR / "tool_results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def put_tool(tool_use_id: str, content: str, is_error: bool = False) -> None:
    (_tool_dir() / f"{tool_use_id}.json").write_text(
        json.dumps({"content": content, "is_error": bool(is_error)}),
        encoding="utf-8")


def get_tool(tool_use_id: str) -> dict | None:
    path = _tool_dir() / f"{tool_use_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
