"""External head anchor — closes the truncation gap in the append-only logs.

The signed ledgers (checkpoint chains, speculation records, delta snapshots)
are tamper-evident against reorder/edit/forgery, but an append-only log with no
out-of-band reference cannot by itself detect WHOLESALE TRUNCATION of its
newest records. This module is that out-of-band reference: every append
publishes the new HEAD (target, seq, hash) — witness-signed — to an anchor sink
OUTSIDE the host's write domain. `verify_anchor()` (CLI: `olympus
verify-anchor`) recomputes the local heads, compares them to the anchored ones,
and reports divergence: a truncated or rolled-back log no longer matches its
anchor.

Sinks are pluggable (`register_sink`). Built-ins:

  * `git` (default posture) — a dedicated anchor git repository: each head is a
    file, committed and PUSHED to the anchor remote, so the anchored state
    lives on another machine.
  * `dir` — a plain directory (an operator-mounted external volume).

Fail-loud contract: when anchoring is ENABLED, a failed anchor write is never a
silent skip — it alerts (gateway notification + captured error + DEGRADED
telemetry). The local append itself still commits (an unreachable sink must not
wedge execution); the alert plus `verify-anchor`'s stale-anchor report are the
guarantee. When anchoring is not configured the module is inert (off, like
every other opt-in surface — not an "unreachable sink").
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from . import config, witness

ANCHOR_SCHEMA = "olympus-anchor/1"
ANCHOR_LABEL = "anchor/v1"           # witness subkey (domain separation)

_KINDS = ("checkpoint", "speculation", "delta")

_lock = threading.Lock()


class AnchorError(RuntimeError):
    """The anchor sink could not be written or read."""


# --- signed head records ---------------------------------------------------

def _core(kind: str, target_id: str, seq: int, head: str) -> dict:
    return {"schema": ANCHOR_SCHEMA, "kind": str(kind),
            "target_id": str(target_id), "seq": int(seq), "head": str(head)}


def _canonical(core: dict) -> bytes:
    return json.dumps(core, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _seal(kind: str, target_id: str, seq: int, head: str) -> dict:
    core = _core(kind, target_id, seq, head)
    rec = dict(core)
    try:
        rec["publicKey"] = witness.sub_public_key_hex(ANCHOR_LABEL)
        rec["signature"] = witness.sign_with(ANCHOR_LABEL, _canonical(core))
    except witness.WitnessError:
        rec["publicKey"] = ""
        rec["signature"] = ""
    return rec


def record_ok(rec: dict) -> bool:
    """The anchor record's signature validates under our anchor subkey (fail
    closed) — a tampered or foreign anchor is not a trustworthy reference."""
    if not isinstance(rec, dict) or rec.get("schema") != ANCHOR_SCHEMA:
        return False
    core = _core(rec.get("kind"), rec.get("target_id"),
                 rec.get("seq", -1), rec.get("head"))
    try:
        expected = witness.sub_public_key_hex(ANCHOR_LABEL)
    except witness.WitnessError:
        return False
    if str(rec.get("publicKey", "")).lower() != expected.lower():
        return False
    return witness.verify_signature(expected, _canonical(core),
                                    str(rec.get("signature", "")))


# --- sinks ------------------------------------------------------------------

def _fname(kind: str, target_id: str) -> str:
    import hashlib
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_"
                   for c in str(target_id))[:64]
    digest = hashlib.sha256(f"{kind}|{target_id}".encode()).hexdigest()[:12]
    return f"{kind}__{safe}.{digest}.json"


class DirSink:
    """A plain directory OUTSIDE the host's write domain (an operator-mounted
    external volume). Simplest sink; also what the tests drive."""
    name = "dir"

    def __init__(self, root: str):
        if not root:
            raise AnchorError("OLYMPUS_ANCHOR_DIR is not set")
        self.root = Path(root)

    def publish(self, rec: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / _fname(rec["kind"], rec["target_id"])
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as err:
            raise AnchorError(f"anchor dir unwritable: {err}") from err

    def read(self, kind: str, target_id: str) -> dict | None:
        path = self.root / _fname(kind, target_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out


class GitSink(DirSink):
    """A dedicated anchor git repository: each head is a file, committed and
    PUSHED so the anchored state lives outside this machine. A failed commit or
    push raises AnchorError — which the publish path turns into a loud alert."""
    name = "git"

    def __init__(self, repo: str, remote: str = "", branch: str = ""):
        if not repo:
            raise AnchorError("OLYMPUS_ANCHOR_REPO is not set")
        super().__init__(repo)
        if not (self.root / ".git").exists():
            raise AnchorError(f"{repo} is not a git repository")
        self.remote = remote
        self.branch = branch

    def _git(self, *args: str) -> None:
        proc = subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AnchorError(
                f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")

    def publish(self, rec: dict) -> None:
        super().publish(rec)                    # write the head file
        name = _fname(rec["kind"], rec["target_id"])
        self._git("add", name)
        try:
            self._git("commit", "-m",
                      f"anchor {rec['kind']}/{rec['target_id']} seq {rec['seq']}")
        except AnchorError as err:
            if "nothing to commit" not in str(err):
                raise                            # an idempotent re-publish is fine
        if self.remote:
            args = ["push", self.remote]
            if self.branch:
                args.append(f"HEAD:{self.branch}")
            self._git(*args)                     # unreachable remote → AnchorError


_SINKS: dict[str, type] = {"dir": DirSink, "git": GitSink}


def register_sink(name: str, factory) -> None:
    """Plug in a custom sink class/factory: instances need publish/read/list."""
    _SINKS[str(name)] = factory


def enabled() -> bool:
    import os
    return bool(os.environ.get("OLYMPUS_ANCHOR", "").strip())


def _sink():
    import os
    mode = os.environ.get("OLYMPUS_ANCHOR", "").strip().lower()
    if not mode:
        return None
    factory = _SINKS.get(mode)
    if factory is None:
        raise AnchorError(f"unknown anchor sink {mode!r}")
    if mode == "git":
        return factory(os.environ.get("OLYMPUS_ANCHOR_REPO", ""),
                       remote=os.environ.get("OLYMPUS_ANCHOR_REMOTE", ""),
                       branch=os.environ.get("OLYMPUS_ANCHOR_BRANCH", ""))
    if mode == "dir":
        return factory(os.environ.get("OLYMPUS_ANCHOR_DIR", ""))
    return factory()


# --- publish (fail-loud) ----------------------------------------------------

def publish_head(kind: str, target_id: str, seq: int, head: str) -> bool:
    """Anchor a new head. Called from every signed-ledger append. Returns True
    on success, False when anchoring is off. NEVER raises into the append —
    when anchoring is enabled, a failure ALERTS (never a silent skip): gateway
    notification, captured error, DEGRADED telemetry."""
    if kind not in _KINDS:
        raise ValueError(f"unknown anchor kind {kind!r}")
    if not enabled():
        return False
    rec = _seal(kind, target_id, seq, head)
    try:
        with _lock:
            sink = _sink()
            sink.publish(rec)
        return True
    except Exception as err:
        _alert(f"ANCHOR WRITE FAILED for {kind}/{target_id} seq {seq}: {err}. "
               "The head is NOT anchored — a truncation of this log would be "
               "undetectable until the anchor recovers. Run `olympus "
               "verify-anchor` after restoring the sink.")
        return False


def _alert(text: str) -> None:
    """Fail-loud: an anchoring failure must be impossible to miss."""
    import sys
    print(f"🚨 {text}", file=sys.stderr)
    try:
        from . import errors
        errors.capture("anchor.publish", AnchorError(text))
    except Exception:
        pass
    try:
        from . import evolve
        evolve.record("anchor", evolve.DEGRADED, text[:200])
    except Exception:
        pass
    try:
        from . import gateway
        gateway.notify_all("🚨 " + text)
    except Exception:
        pass


# --- verification ------------------------------------------------------------

def _local_heads() -> list[dict]:
    """Recompute every local target's head from the on-disk logs (the last
    record of each): checkpoint + speculation chains, and delta snapshots."""
    out: list[dict] = []
    base = config.MEMORY_DIR / "ledger"
    if base.exists():
        for p in sorted(base.glob("*.jsonl")):
            rec = _last_json_line(p)
            if not rec:
                continue
            if p.name.endswith(".spec.jsonl"):
                out.append({"kind": "speculation",
                            "target_id": str(rec.get("run_id", "")),
                            "seq": int(rec.get("seq", -1)),
                            "head": str(rec.get("record_hash", ""))})
            else:
                out.append({"kind": "checkpoint",
                            "target_id": str(rec.get("run_id", "")),
                            "seq": int(rec.get("seq", -1)),
                            "head": str(rec.get("node_hash", ""))})
    dbase = config.MEMORY_DIR / "deltas"
    if dbase.exists():
        for p in sorted(dbase.glob("*.jsonl")):
            rec = _last_json_line(p)
            if rec:
                out.append({"kind": "delta",
                            "target_id": str(rec.get("target_id", "")),
                            "seq": int(rec.get("seq", -1)),
                            "head": str(rec.get("snapshot_hash", ""))})
    return out


def _last_json_line(path: Path) -> dict | None:
    try:
        lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None


def verify_anchor() -> dict:
    """Compare every anchored head against the recomputed local head. Returns
    {ok, enabled, checked, divergences, problems}. Divergence kinds:

      * truncated     — the local log is BEHIND its anchor (records vanished);
      * head-mismatch — same seq, different hash (the log was rewritten);
      * missing-local — anchored target has no local log at all;
      * anchor-stale  — the local log is AHEAD of its anchor (an anchor write
                        was missed — the fail-loud alert path also fired);
      * bad-anchor    — the anchor record's own signature does not verify.
    """
    out = {"ok": False, "enabled": enabled(), "checked": 0,
           "divergences": [], "problems": []}
    if not enabled():
        out["problems"].append("anchoring is not configured (OLYMPUS_ANCHOR)")
        return out
    try:
        sink = _sink()
        anchored = sink.list()
    except Exception as err:
        out["problems"].append(f"anchor sink unreadable: {err}")
        return out
    local = {(h["kind"], h["target_id"]): h for h in _local_heads()}
    for rec in anchored:
        out["checked"] += 1
        key = (str(rec.get("kind")), str(rec.get("target_id")))
        label = f"{key[0]}/{key[1]}"
        if not record_ok(rec):
            out["divergences"].append(
                {"target": label, "kind": "bad-anchor",
                 "detail": "anchor record signature invalid (tampered anchor)"})
            continue
        loc = local.get(key)
        if loc is None:
            out["divergences"].append(
                {"target": label, "kind": "missing-local",
                 "detail": f"anchored at seq {rec['seq']} but no local log"})
            continue
        if loc["seq"] < rec["seq"]:
            out["divergences"].append(
                {"target": label, "kind": "truncated",
                 "detail": f"local head seq {loc['seq']} is behind anchored "
                           f"seq {rec['seq']} — records were truncated"})
        elif loc["seq"] == rec["seq"] and loc["head"] != rec["head"]:
            out["divergences"].append(
                {"target": label, "kind": "head-mismatch",
                 "detail": f"local head differs from anchor at seq {rec['seq']}"})
        elif loc["seq"] > rec["seq"]:
            out["divergences"].append(
                {"target": label, "kind": "anchor-stale",
                 "detail": f"local seq {loc['seq']} ahead of anchored "
                           f"{rec['seq']} — an anchor write was missed"})
    out["ok"] = not out["divergences"] and not out["problems"]
    return out
