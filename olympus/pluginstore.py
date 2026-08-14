"""Hardened plugin installer — the surveyed gateway's plugin ecosystem, built the safe way.

The surveyed gateway shipped a plugin marketplace and then a string of supply-chain CVEs:
"install wrappers could skip install policy", "non-owner persistence", plugins
that ran before any provenance check. This module gives Olympus the same
capability — installing third-party tool plugins — with the trust model
INVERTED, so breadth becomes a moat instead of an attack surface:

  1. Deny by default. An install is refused unless the operator pins the exact
     SHA-256 of the code (or explicitly opts into unpinned installs for a
     trusted local file). No hash, no install.
  2. Source policy. When OLYMPUS_PLUGIN_ALLOW is set, a remote source must
     match one of its prefixes; otherwise remote installs are refused outright.
  3. Tamper detection. Every install is recorded (name, source, sha256, time)
     in an audit manifest; `verify()` re-hashes the on-disk files and flags any
     that changed since install. Optional enforcement (OLYMPUS_PLUGIN_ENFORCE)
     makes the loader run ONLY manifest-verified plugins.
  4. Operator-initiated only. Nothing is ever fetched or run automatically;
     installing is an explicit CLI action, logged.

The actual tool registration still happens through connectors.load_plugins;
this module only governs what code is allowed to get there.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

from . import config

_MAX_PLUGIN_BYTES = 512 * 1024      # a tool plugin is small; cap fetch size


def plugins_dir() -> Path:
    """Canonical install dir (created on demand): OLYMPUS_PLUGINS_DIR, else
    MEMORY_DIR/plugins. This is also where connectors.load_plugins looks."""
    env = os.environ.get("OLYMPUS_PLUGINS_DIR")
    d = Path(env) if env else (config.MEMORY_DIR / "plugins")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path() -> Path:
    return plugins_dir() / ".manifest.json"


def _load_manifest() -> dict:
    try:
        return json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_manifest(data: dict) -> None:
    p = _manifest_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_allowed(source: str) -> bool:
    """A remote source must match OLYMPUS_PLUGIN_ALLOW (comma-separated URL
    prefixes) when that policy is set. Local files are always permitted (the
    operator already has filesystem access). No policy set = remote refused
    unless a hash is pinned (checked by the caller)."""
    if source.startswith("file:") or "://" not in source:
        return True
    allow = os.environ.get("OLYMPUS_PLUGIN_ALLOW", "").strip()
    if not allow:
        return False                    # no allowlist → deny remote by default
    prefixes = [p.strip() for p in allow.split(",") if p.strip()]
    return any(source.startswith(p) for p in prefixes)


def _fetch(source: str) -> bytes:
    """Read plugin code from a local path or an https URL (size-capped)."""
    if source.startswith("file:"):
        source = source[5:]
    if "://" not in source:
        with open(os.path.expanduser(source), "rb") as fh:
            return fh.read(_MAX_PLUGIN_BYTES + 1)
    if not source.startswith("https://"):
        raise ValueError("remote plugin sources must use https")
    with urllib.request.urlopen(source, timeout=30) as resp:  # noqa: S310
        return resp.read(_MAX_PLUGIN_BYTES + 1)


def _gate_plugin(source: str, stem: str, data: bytes, digest: str,
                 pinned: str) -> None:
    """Run the fetched plugin through the persistent-artifact ingestion gate
    (W2-C5) before ANY of it reaches disk. A plugin is durable, executable
    third-party code — the archetypal persistent artifact.

    The operator's pinned `sha256` is handed to the gate as the artifact's
    declared hash so the gate's declared-vs-actual cross-check does the
    verification (no second hash comparison is written here), and `source` is
    handed over as provenance so the gate applies its source-safety rules
    (traversal / control characters) too. Raises ValueError — install()'s
    existing failure vocabulary — and, because it runs before the first write,
    a refusal leaves nothing behind.

    Inert while `OLYMPUS_INGESTGATE` is off: the gate is not consulted at all.
    """
    from . import ingestgate
    if not ingestgate.enabled():
        return
    artifact = {
        "version": "1",
        "name": stem,
        # surrogateescape (rather than "replace") so non-UTF-8 plugin bytes
        # surface to the gate as invalid_utf8 instead of being silently
        # repaired into U+FFFD.
        "code": data.decode("utf-8", "surrogateescape"),
        "sha256": (pinned.strip().lower() if pinned else digest),
        "size": len(data),
    }
    try:
        prov_sha = ingestgate.payload_sha256(artifact)
    except Exception:            # unserializable bytes — check() refuses below
        prov_sha = ""
    provenance = {"source": source, "sha256": prov_sha}
    try:
        ingestgate.check("plugin", artifact, source=source,
                         provenance=provenance)
    except ingestgate.IngestRefused as refusal:
        ref = (f"; evidence {refusal.evidence_ref}"
               if refusal.evidence_ref else "")
        raise ValueError(
            "the ingestion gate refused this plugin "
            f"({'; '.join(refusal.reasons)}{ref}). "
            "Refusing to install — nothing was written.") from refusal


def install(source: str, sha256: str = "", name: str = "",
            allow_unpinned: bool = False) -> dict:
    """Install a plugin from `source` after verifying provenance. Returns a
    result dict; raises ValueError with a clear reason on any policy failure —
    the install never proceeds past a failed check."""
    if not _source_allowed(source):
        raise ValueError(
            f"source '{source}' is not in OLYMPUS_PLUGIN_ALLOW — remote "
            "installs are denied by default; set the allowlist to permit it.")
    data = _fetch(source)
    if len(data) > _MAX_PLUGIN_BYTES:
        raise ValueError(f"plugin exceeds {_MAX_PLUGIN_BYTES} bytes")
    digest = _sha256(data)
    if sha256:
        if digest.lower() != sha256.strip().lower():
            raise ValueError(
                f"SHA-256 mismatch — expected {sha256}, got {digest}. "
                "Refusing to install (provenance check failed).")
    elif not allow_unpinned:
        raise ValueError(
            "no --sha256 pinned. Refusing to install unverified code. "
            f"Its hash is {digest} — re-run with --sha256 {digest} if you "
            "trust it, or --allow-unpinned for a local file you wrote.")
    # Derive a safe module filename.
    stem = (name or Path(source.split("://")[-1]).stem or "plugin")
    stem = "".join(c for c in stem if c.isalnum() or c in "_-").strip("_-")
    if not stem or stem.startswith("_"):
        stem = "plugin_" + digest[:8]
    dest = plugins_dir() / f"{stem}.py"
    _gate_plugin(source, stem, data, digest, sha256)   # W2-C5, before any write
    dest.write_bytes(data)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    man = _load_manifest()
    man[stem] = {"source": source, "sha256": digest,
                 "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime())}
    _save_manifest(man)
    return {"name": stem, "path": str(dest), "sha256": digest}


def list_installed() -> list[dict]:
    man = _load_manifest()
    return [{"name": k, **v} for k, v in sorted(man.items())]


def remove(name: str) -> bool:
    man = _load_manifest()
    if name not in man:
        return False
    (plugins_dir() / f"{name}.py").unlink(missing_ok=True)
    man.pop(name, None)
    _save_manifest(man)
    return True


def verify() -> list[dict]:
    """Re-hash installed plugins against the manifest. Returns a status per
    plugin: ok / MODIFIED (on-disk hash differs — tamper) / MISSING (file
    gone). An empty list means nothing is installed."""
    man = _load_manifest()
    out = []
    for name, meta in sorted(man.items()):
        path = plugins_dir() / f"{name}.py"
        if not path.exists():
            out.append({"name": name, "status": "MISSING"})
            continue
        cur = _sha256(path.read_bytes())
        ok = cur == meta.get("sha256")
        out.append({"name": name, "status": "ok" if ok else "MODIFIED",
                    "expected": meta.get("sha256"), "actual": cur})
    return out


def verified_names() -> set[str] | None:
    """When OLYMPUS_PLUGIN_ENFORCE is on, the set of plugin module stems whose
    on-disk hash matches the manifest — the ONLY ones the loader may run.
    Returns None when enforcement is off (loader keeps its current behavior)."""
    if os.environ.get("OLYMPUS_PLUGIN_ENFORCE", "").strip().lower() not in (
            "1", "true", "yes", "on"):
        return None
    return {v["name"] for v in verify() if v["status"] == "ok"}
