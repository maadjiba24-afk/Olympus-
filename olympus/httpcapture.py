"""Opt-in HTTP capture / replay store — the safe form of a Caido-style proxy.

When `OLYMPUS_HTTP_CAPTURE=1`, every governed outbound fetch (`tools._http_get`)
records its request (method / url / redacted headers) and response (status /
size / redacted body) to a local, operator-only store, so an operator can SEE
what an agent actually sent and RECEIVED, and REPLAY a request to check whether
the resource changed. Valuable for debugging and for forensic audit of
assessment runs (DEFERRED #18 / the surveyed agent watchlist — the capture-proxy value
*without* the open box).

It carries none of the Kali / `NET_RAW` / open-egress risk that the full the surveyed agent
scanner sandbox would: it only OBSERVES the already-governed fetch path
(SSRF-pinned, egress-confined, secret-exfil-scanned) — it opens no new socket
and relaxes no gate. Stored bodies and headers are secret-redacted
(`security.sanitize_for_prompt`) so the capture store never becomes a credential
sink; bodies are size-capped; files are day-stamped and swept on the normal
retention cadence. **Off by default** — capturing response bodies is opt-in
because they are attacker-influenced content the operator chooses to persist.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import config, security

_BODY_CAP = 64 * 1024          # bytes of each response body persisted
_HEADER_KEYS = ("content-type", "server", "location", "cache-control",
                "content-length", "etag", "last-modified")


def enabled() -> bool:
    """Operator opt-in (default OFF). The agent never sets this."""
    return os.environ.get("OLYMPUS_HTTP_CAPTURE", "").strip().lower() in (
        "1", "on", "true", "yes")


def _dir() -> Path:
    d = config.MEMORY_DIR / "httpcapture"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(day: str | None = None) -> Path:
    return _dir() / f"{day or time.strftime('%Y%m%d')}.jsonl"


def _rec_id(url: str, ts: float) -> str:
    h = hashlib.sha256(f"{url}|{ts}".encode("utf-8", "replace")).hexdigest()[:12]
    return f"{time.strftime('%Y%m%d')}-{h}"


def _safe_headers(headers: dict | None) -> dict:
    """A small, secret-redacted subset of headers — never persist an
    Authorization / Cookie / api-key header verbatim."""
    if not headers:
        return {}
    out = {}
    for k, v in headers.items():
        lk = str(k).lower()
        if lk in _HEADER_KEYS:
            out[lk] = security.sanitize_for_prompt(str(v))[:200]
    return out


def record(method: str, url: str, req_headers: dict | None, status: int,
           body: str, *, elapsed_ms: float | None = None,
           resp_headers: dict | None = None, run: str = "") -> str | None:
    """Append one capture record (best-effort — never raises, never breaks the
    fetch). Returns the record id, or None when disabled / on any error."""
    if not enabled():
        return None
    try:
        ts = time.time()
        rid = _rec_id(url, ts)
        body = body or ""
        # Redact secrets from the stored body so the capture store can never
        # become a credential sink; cap the size.
        red = security.sanitize_for_prompt(body[:_BODY_CAP])
        rec = {
            "id": rid, "ts": round(ts, 3), "run": str(run or "")[:80],
            "method": str(method or "GET").upper(),
            "url": security.sanitize_for_prompt(str(url))[:2000],
            "status": int(status) if status is not None else None,
            "req_headers": _safe_headers(req_headers),
            "resp_headers": _safe_headers(resp_headers),
            "bytes": len(body),
            "body_sha256": hashlib.sha256(body.encode("utf-8", "replace")).hexdigest(),
            "body": red,
            "truncated": len(body) > _BODY_CAP,
            "elapsed_ms": round(elapsed_ms, 1) if elapsed_ms is not None else None,
        }
        with open(_file(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rid
    except Exception:              # capture must never break the fetch
        return None


def _iter_records(day: str | None = None):
    files = [_file(day)] if day else sorted(_dir().glob("*.jsonl"), reverse=True)
    for f in files:
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    yield json.loads(line)
        except (OSError, json.JSONDecodeError):
            continue


def records(day: str | None = None, limit: int = 200,
            run: str | None = None) -> list[dict]:
    """Recent capture records (newest first), optionally filtered by run tag."""
    out = []
    for rec in _iter_records(day):
        if run is not None and rec.get("run") != run:
            continue
        out.append(rec)
        if len(out) >= max(1, limit) * 4:
            break
    out.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return out[:max(1, limit)]


def get(record_id: str) -> dict | None:
    for rec in _iter_records():
        if rec.get("id") == record_id:
            return rec
    return None


def replay(record_id: str, *, http_get=None) -> dict:
    """Re-issue a captured request through the SAME governed fetch and report
    whether the resource changed since capture. Never opens an ungoverned
    socket: the re-fetch goes through `tools._http_get` (SSRF/egress/secret
    gates) exactly like the original. `http_get` is injectable for tests."""
    rec = get(record_id)
    if rec is None:
        return {"ok": False, "error": f"no capture record {record_id!r}"}
    if http_get is None:
        from . import tools
        http_get = tools._http_get
    try:
        now_body = http_get(rec["url"]) or ""
    except Exception as err:
        return {"ok": False, "url": rec["url"], "error": str(err)[:200]}
    now_sha = hashlib.sha256(now_body.encode("utf-8", "replace")).hexdigest()
    return {
        "ok": True, "url": rec["url"], "id": record_id,
        "then_bytes": rec.get("bytes"), "now_bytes": len(now_body),
        "then_sha256": rec.get("body_sha256"), "now_sha256": now_sha,
        "changed": now_sha != rec.get("body_sha256"),
    }


def clear(day: str | None = None) -> int:
    """Delete capture files (all, or one day). Returns files removed."""
    n = 0
    files = [_file(day)] if day else list(_dir().glob("*.jsonl"))
    for f in files:
        try:
            if f.exists():
                f.unlink()
                n += 1
        except OSError:
            continue
    return n
