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

Replay-mode taxonomy (Wave 1 C2 — made explicit, no behaviour change):

- **exact** — frozen responses + frozen tool results + frozen context; fully
  deterministic. Active whenever `OLYMPUS_REPLAY` is set (`replaying()` /
  `replay_mode()`); this is what `orchestrator.replay_run` and the CI gate use.
- **provider-response** — an alias of `exact` from the provider's point of
  view: live provider calls never occur (`llm.complete` returns `get(hash)`).
- **structural** — compare `trace.decision_core` sequences only, ignoring
  volatile fields; implemented by `trace.diff_decisions`/`trace.canonical_log`
  over any two recorded runs (no store involvement).
- **live** — `replaygate.py` re-runs real prompts against the live provider;
  explicitly NOT deterministic and never asserted byte-equal.

Portable fixtures (`export_fixture` / `import_fixture`) bundle everything the
`exact` mode needs — trace record, referenced response blobs, frozen tool
results and context — into a sanitized directory that can be committed to the
repo and replayed in CI with no live `MEMORY_DIR`.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import threading
from pathlib import Path

import anthropic

from . import config

# The server allocates `container` per request (web-search code execution), so
# it changes every run; hashing it would make replay never match. Everything
# else — system, messages, model, tools, mcp_servers, thinking, output_config —
# is part of the decision and *should* diverge if it changes.
_EXCLUDE_FROM_HASH = ("container",)

_local = threading.local()

# The active run id lives in a ContextVar (not thread-local) so it survives the
# thread hop the dispatcher makes via `contextvars.copy_context()`: specialists
# run on worker threads, and their prompt-assembly `frozen_context` calls must
# see the SAME run scope the main thread set — otherwise record-mode freezing is
# silently skipped on workers while replay-mode (keyed off the global
# OLYMPUS_REPLAY env) still tries to read the frozen value, and the run diverges.
_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "olympus_replay_run", default=None)


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


def _canonical_default(obj):
    """Serialize a value json can't handle. SDK response blocks (thinking,
    tool_use, web_search_tool_result, …) get re-sent in later requests; their
    `str()` repr is NOT stable across a freeze→reload round-trip, but their
    structured `model_dump` is — so hash over that, falling back to str()."""
    dump = getattr(obj, "model_dump", None)
    if dump is not None:
        try:
            return dump(mode="json")
        except Exception:
            pass
    return str(obj)


def canonical_request(params: dict) -> bytes:
    """Deterministic bytes for a request: keys sorted recursively (json
    sort_keys), the server-allocated `container` dropped, and SDK objects
    serialized by their structured `model_dump` so a re-sent assistant turn
    hashes identically whether it's live (record) or reloaded (replay)."""
    cleaned = {k: v for k, v in params.items() if k not in _EXCLUDE_FROM_HASH}
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=_canonical_default).encode("utf-8")


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


# --- frozen run-state context --------------------------------------------
#
# A prompt can also embed *mutable state* that the same run changes — e.g. the
# memory/profile/relgraph context block injected into the router's system
# prompt, which the run's own background memory-learner rewrites. Re-reading it
# at replay time would change the request and break a byte-identical replay. So
# the orchestration wraps such reads in `frozen_context`: in record mode the
# value is frozen (keyed by the run id + a named slot), in replay mode the
# frozen value is returned — while the static prompt/roster are still read live,
# so a genuine code/prompt change is still detected as a divergence.

def set_run(run_id: str | None) -> None:
    """Mark the current run so `frozen_context` can key by it. Stored in a
    ContextVar so a `copy_context()`ed worker thread (specialist dispatch,
    verify pool) inherits the same scope the main thread set."""
    _run_id_var.set(run_id)


def _current_run() -> str | None:
    return replaying() or _run_id_var.get()


def _ctx_dir():
    d = config.MEMORY_DIR / "context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def frozen_context(slot: str, producer) -> str:
    """Freeze (record) or return (replay) a piece of mutable run state injected
    into a prompt. Keyed by run id + slot, so it's stable across a run's record
    and replay but distinct per run. Outside a tracked run it just computes."""
    rid = _current_run()
    if rid is None:
        return producer()
    key = hashlib.sha256(f"{rid}:{slot}".encode("utf-8")).hexdigest()
    path = _ctx_dir() / f"{key}.json"
    if replaying():
        if not path.exists():
            raise ReplayDivergence(key, {"model": f"context:{slot}"})
        return json.loads(path.read_text(encoding="utf-8"))["text"]
    text = producer()
    path.write_text(json.dumps({"text": text}), encoding="utf-8")
    return text


# --- portable replay fixtures (Wave 1 C2) ----------------------------------
#
# A fixture is a self-contained directory:
#   manifest.json   {"v":"1.0","run_id",flags,models,prompt_manifest,reply_sha,
#                    files:[{path,sha256}],scrub:{screen,matches}}
#   trace.json      the full recorded run (as flushed by trace.Trace.flush)
#   responses/*     every frozen LLM response the run's decisions reference
#   tool_results/*  frozen client-side tool results
#   context/*       frozen mutable run-state slots
#
# Blob-selection choice (documented per spec C2): responses are collected
# PRECISELY from the run's decision records (model_response_ref /
# model_request_hash — a missing one refuses the export, no partial fixture).
# tool_results/ and context/ cannot be reverse-mapped to a run cheaply
# (tool_use ids live inside response content; context keys are one-way hashes
# of run_id+slot), so the fixture includes ALL files present under those two
# directories — the simple, robust option: exports are taken from a per-run or
# per-test MEMORY_DIR in practice (the committed CI fixture is generated in a
# temp dir holding exactly one run), and every included file is still
# sha-pinned and secret-screened.

FIXTURE_VERSION = "1.0"

# The secret screen (invariant I-R2). Every candidate blob's text is scanned;
# ANY match refuses the whole export — fixtures are for committing/sharing.
_SECRET_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"sk-[A-Za-z0-9]{8,}",
    r"(?i)authorization:\s*bearer",
    r"(?i)api[_-]?key\s*[=:]\s*\S{8,}",
))
_SECRET_SCREEN_VERSION = "v1"

# env-flag fingerprint: the EXACT env names `orchestrator.replay_run`
# reproduces, derived from the same trace-meta keys with the same defaults
# (this mapping mirrors replay_run's env-reproduction list — the flag-pairing
# invariant says every decision-affecting flag appears in both places).
_FLAG_ENV_META: tuple[tuple[str, str], ...] = (
    ("OLYMPUS_CONTRACTS", "contracts_enabled"),
    ("OLYMPUS_EGRESS_GUARD", "egress_guard_enabled"),
    ("OLYMPUS_ABC", "abc_enabled"),
    ("OLYMPUS_INRUN_COMPACT", "inrun_compact"),
    ("OLYMPUS_INRUN_BUDGET", "inrun_budget"),
    ("OLYMPUS_INRUN_KEEP_RECENT", "inrun_keep_recent"),
    ("OLYMPUS_FAST", "fast_mode"),
    ("OLYMPUS_INTERACTIVE_VERIFY", "interactive_verify"),
    ("OLYMPUS_SWARM", "swarm_enabled"),
    ("OLYMPUS_SWARM_TOPOLOGY", "swarm_topology"),
    ("OLYMPUS_SEMANTIC_ROUTING", "semantic_routing"),
    ("OLYMPUS_SEMANTIC_SKILLS", "semantic_skills"),
)


class FixtureError(RuntimeError):
    """Base class for typed fixture export/import failures."""


class FixtureSecretsError(FixtureError):
    """Export refused: a blob matched the secret-pattern screen (I-R2)."""

    def __init__(self, files: list[str]):
        self.files = list(files)
        super().__init__(
            "fixture export refused: secret-pattern screen matched in "
            f"{len(self.files)} file(s): {', '.join(self.files)}")


class FixtureMissingBlobs(FixtureError):
    """Export refused: referenced frozen responses are absent (no partial
    fixture is ever written)."""

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__(
            "fixture export refused: run references frozen responses that are "
            f"not on disk: {', '.join(h[:12] for h in self.missing)}")


class FixtureHashMismatch(FixtureError):
    """Import refused: a file's sha256 differs from the manifest
    (reject-never-repair — the WHOLE fixture is refused)."""

    def __init__(self, mismatches: list[str]):
        self.mismatches = list(mismatches)
        super().__init__(
            "fixture import refused: sha256 mismatch (tampered or corrupt) in: "
            + ", ".join(self.mismatches))


class FixtureConflict(FixtureError):
    """Import refused: it would overwrite existing store entries and
    force=False (I-R3)."""

    def __init__(self, paths: list[str]):
        self.paths = list(paths)
        super().__init__(
            "fixture import refused: would overwrite existing store entries "
            f"(pass force=True to allow): {', '.join(self.paths[:5])}"
            + ("…" if len(self.paths) > 5 else ""))


def replay_mode() -> str:
    """The active replay mode of this process: 'exact' when OLYMPUS_REPLAY is
    set (frozen responses/tools/context — deterministic), else 'live'. The
    'provider-response' and 'structural' modes are usage patterns, not process
    states — see the module docstring taxonomy."""
    return "exact" if replaying() else "live"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prompt_manifest() -> dict:
    """{stem: sha256[:12]} over olympus/prompts/*.md — the prompt-template
    fingerprint recorded in fixture manifests (C6's modelgate.prompt_manifest
    will own this; computed inline until it lands)."""
    prompts = Path(__file__).resolve().parent / "prompts"
    out = {}
    for p in sorted(prompts.glob("*.md")):
        out[p.stem] = _sha256_bytes(p.read_bytes())[:12]
    return out


def _flags_fingerprint(meta: dict) -> dict:
    """The env values orchestrator.replay_run would reproduce for this run,
    keyed by the exact env names it sets. Mirrors replay_run's meta reads,
    including its defaults (abc on when absent; inrun budget/keep-recent only
    when recorded; swarm topology only when recorded)."""
    flags = {
        "OLYMPUS_CONTRACTS": "1" if meta.get("contracts_enabled") else "0",
        "OLYMPUS_EGRESS_GUARD": "1" if meta.get("egress_guard_enabled") else "0",
        "OLYMPUS_ABC": "on" if meta.get("abc_enabled", True) else "off",
        "OLYMPUS_INRUN_COMPACT": str(meta.get("inrun_compact", "")),
        "OLYMPUS_FAST": "1" if meta.get("fast_mode") else "0",
        "OLYMPUS_INTERACTIVE_VERIFY":
            "on" if meta.get("interactive_verify") else "off",
        "OLYMPUS_SWARM": "1" if meta.get("swarm_enabled") else "0",
        "OLYMPUS_SEMANTIC_ROUTING":
            "1" if meta.get("semantic_routing") else "0",
        "OLYMPUS_SEMANTIC_SKILLS":
            "1" if meta.get("semantic_skills") else "0",
    }
    if meta.get("inrun_budget") is not None:
        flags["OLYMPUS_INRUN_BUDGET"] = str(meta["inrun_budget"])
    if meta.get("inrun_keep_recent") is not None:
        flags["OLYMPUS_INRUN_KEEP_RECENT"] = str(meta["inrun_keep_recent"])
    if meta.get("swarm_topology"):
        flags["OLYMPUS_SWARM_TOPOLOGY"] = str(meta["swarm_topology"])
    return flags


def _reply_sha(run: dict) -> str:
    """sha256 of the final user-facing reply, when the trace recorded one
    (meta['reply']); '' otherwise — today's pipeline records decisions, not the
    streamed answer, so most runs have no recoverable reply text."""
    meta = run.get("meta") or {}
    for key in ("reply", "final_reply"):
        val = meta.get(key)
        if isinstance(val, str) and val:
            return _sha256_bytes(val.encode("utf-8"))
    return ""


def _screen_secrets(named_texts: list[tuple[str, str]]) -> list[str]:
    """Return the fixture-relative paths whose text matches any secret
    pattern."""
    offenders = []
    for name, text in named_texts:
        if any(p.search(text) for p in _SECRET_PATTERNS):
            offenders.append(name)
    return offenders


def export_fixture(run_id: str, dest) -> Path:
    """Bundle a recorded run into a portable, sanitized fixture directory.

    Collects the trace record, every frozen response the run's decisions
    reference (missing one ⇒ `FixtureMissingBlobs`, no partial fixture), and
    all frozen tool_results/context files present (see the blob-selection note
    above). Refuses with `FixtureSecretsError` if any blob matches the secret
    screen (I-R2). Returns the fixture directory path."""
    from . import trace as trace_mod
    run = trace_mod.load_run(run_id)
    if not run:
        raise ValueError(f"no recorded run '{run_id}' found in traces")

    # 1. Collect referenced response blobs — precise, and refuse on absence.
    refs: list[str] = []
    for d in run.get("decisions", []):
        for key in ("model_response_ref", "model_request_hash"):
            h = d.get(key)
            if isinstance(h, str) and h and h not in refs:
                refs.append(h)
    resp_dir = config.MEMORY_DIR / "responses"
    missing = [h for h in refs if not (resp_dir / f"{h}.json").exists()]
    if missing:
        raise FixtureMissingBlobs(missing)

    # 2. Gather blob contents (fixture-relative path -> bytes).
    blobs: dict[str, bytes] = {}
    for h in refs:
        blobs[f"responses/{h}.json"] = (resp_dir / f"{h}.json").read_bytes()
    for sub in ("tool_results", "context"):
        base = config.MEMORY_DIR / sub
        if base.exists():
            for p in sorted(base.glob("*.json")):
                blobs[f"{sub}/{p.name}"] = p.read_bytes()
    trace_bytes = (json.dumps(run, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False) + "\n").encode("utf-8")
    blobs["trace.json"] = trace_bytes

    # 3. Secret screen over EVERY blob (trace included) — refuse on any match.
    offenders = _screen_secrets(
        [(name, data.decode("utf-8", errors="replace"))
         for name, data in blobs.items()])
    if offenders:
        raise FixtureSecretsError(offenders)

    # 4. Write the fixture directory + manifest.
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if (dest / "manifest.json").exists():
        raise FixtureError(
            f"fixture export refused: {dest} already contains a manifest.json")
    for rel, data in blobs.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    meta = run.get("meta") or {}
    models: list[dict] = []
    for d in run.get("decisions", []):
        m = d.get("model")
        if m and m not in models:
            models.append(m)
    manifest = {
        "v": FIXTURE_VERSION,
        "run_id": run_id,
        "flags": _flags_fingerprint(meta),
        "models": models,
        "prompt_manifest": _prompt_manifest(),
        "reply_sha": _reply_sha(run),
        "files": [{"path": rel, "sha256": _sha256_bytes(data)}
                  for rel, data in sorted(blobs.items())],
        "scrub": {"screen": _SECRET_SCREEN_VERSION, "matches": 0},
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n", encoding="utf-8")
    return dest


def import_fixture(src, *, force: bool = False) -> str:
    """Materialize a fixture into the live MEMORY_DIR store. Verifies EVERY
    file's sha256 against the manifest first — one mismatch refuses the whole
    fixture (`FixtureHashMismatch`, reject-never-repair). Refuses to overwrite
    existing store entries unless force=True (`FixtureConflict`, I-R3). The
    trace record is appended to `traces/imported-<run_id>.jsonl` (trace.load_run
    globs *.jsonl, so it is found like any dated file). Returns the run_id."""
    src = Path(src)
    manifest_path = src / "manifest.json"
    if not manifest_path.exists():
        raise FixtureError(f"not a fixture directory (no manifest.json): {src}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(manifest.get("v", ""))
    if not version.startswith("1."):
        raise FixtureError(f"unsupported fixture version {version!r}")
    run_id = manifest.get("run_id")
    if not run_id:
        raise FixtureError("fixture manifest has no run_id")

    entries = manifest.get("files") or []
    allowed_roots = ("responses/", "tool_results/", "context/")
    mismatches: list[str] = []
    contents: dict[str, bytes] = {}
    for entry in entries:
        rel = entry.get("path", "")
        if (rel != "trace.json"
                and not any(rel.startswith(r) for r in allowed_roots)) \
                or ".." in rel.split("/") or rel.startswith("/"):
            raise FixtureError(f"fixture manifest lists a forbidden path: {rel!r}")
        path = src / rel
        if not path.exists():
            mismatches.append(f"{rel} (missing)")
            continue
        data = path.read_bytes()
        if _sha256_bytes(data) != entry.get("sha256"):
            mismatches.append(rel)
        contents[rel] = data
    if mismatches:
        raise FixtureHashMismatch(mismatches)
    # Files present in the fixture but absent from the manifest are also a
    # tamper signal — nothing unpinned is ever imported.
    listed = {e["path"] for e in entries} | {"manifest.json"}
    on_disk = {str(p.relative_to(src)).replace(os.sep, "/")
               for p in src.rglob("*") if p.is_file()}
    unlisted = sorted(on_disk - listed)
    if unlisted:
        raise FixtureHashMismatch([f"{p} (unlisted)" for p in unlisted])
    if "trace.json" not in contents:
        raise FixtureError("fixture manifest lists no trace.json")
    run = json.loads(contents["trace.json"].decode("utf-8"))
    if run.get("id") != run_id:
        raise FixtureHashMismatch(["trace.json (run id differs from manifest)"])

    # I-R3: never overwrite silently — a fixture must not poison a live store.
    from . import trace as trace_mod
    conflicts: list[str] = []
    for rel in contents:
        if rel == "trace.json":
            continue
        if (config.MEMORY_DIR / rel).exists():
            conflicts.append(rel)
    trace_path = config.MEMORY_DIR / "traces" / f"imported-{run_id}.jsonl"
    if trace_path.exists() or trace_mod.load_run(run_id) is not None:
        conflicts.append(f"traces (run {run_id} already recorded)")
    if conflicts and not force:
        raise FixtureConflict(conflicts)

    for rel, data in contents.items():
        if rel == "trace.json":
            continue
        path = config.MEMORY_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(run, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False) + "\n"
    if force and trace_path.exists():
        trace_path.write_text(line, encoding="utf-8")
    else:
        with trace_path.open("a", encoding="utf-8") as f:
            f.write(line)
    return str(run_id)
