#!/usr/bin/env python3
"""Phase 5 Step 8 — external-client compatibility campaign.

Drives BOTH supported API dialects through a REAL HTTP path: a real
`ThreadingHTTPServer` running the real `web.Handler`, bound to a real TCP
socket, driven by the REAL vendor SDKs (`anthropic`, `openai`) — the same
client libraries a third-party application would use.

WHAT THIS PROVES, EXACTLY
-------------------------
Phase 4 could only say "SDK-type-verified": the response *shapes* validated
against the SDK's Pydantic models, in-process, with no socket. This campaign
upgrades that to **real-SDK-over-HTTP verified in staging**, because the SDK
does its own HTTP, its own SSE framing, its own retry logic and its own
deserialization against a live server.

WHAT IT DOES NOT PROVE
----------------------
The UPSTREAM provider is a stub — no real model is called, so nothing here says
anything about answer quality, latency or cost. And no third-party application
drove this across a real network from another host. The claim ceiling is
therefore exactly:

    real-SDK-over-HTTP verified in staging
    NOT production-client verified

Anything stronger would breach Phase-5 rule 1. `PHASE5_CLIENT_COMPATIBILITY_
REPORT.md` states it at that strength and no higher.

Usage:  python scripts/client_compat_campaign.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY_A = "staging-key-alpha-000000000000"
KEY_B = "staging-key-bravo-111111111111"


# ---------------------------------------------------------------------------
# the stub council — stands in for the provider, NOT for the HTTP layer
# ---------------------------------------------------------------------------

class StubCouncil:
    """Replaces `orchestrator.Olympus` only. Everything from the socket down to
    the handler is the real thing; this is the seam where a real provider would
    otherwise be called, and there is no credential for one."""

    users: list[str] = []
    lock = threading.Lock()

    def __init__(self, *_a, **kw):
        self.last_run_id = "stagingrun00"
        self.user = kw.get("user", "")
        with StubCouncil.lock:
            StubCouncil.users.append(self.user)

    def ask(self, message: str) -> str:
        if "SLOW" in message:
            time.sleep(2.0)
        return f"stub reply to: {message[:120]}"

    def ask_stream(self, message: str):
        for chunk in ("stub ", "streamed ", "reply ", "to: ", message[:40]):
            yield chunk


def start_server():
    """Start the real server with the council stubbed.

    Returns (server, base_url, restore). `restore()` puts the real
    `orchestrator.Olympus` back — the stub is installed on a MODULE ATTRIBUTE,
    so leaving it in place would silently replace the council for every
    subsequent caller in the same process. That is exactly what happened the
    first time this ran inside the test suite: 100 unrelated tests failed
    because they were running against StubCouncil. Callers must restore."""
    from olympus import web
    original = web.orchestrator.Olympus
    web.orchestrator.Olympus = StubCouncil
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def restore():
        web.orchestrator.Olympus = original
        srv.shutdown()
        srv.server_close()

    return srv, f"http://127.0.0.1:{srv.server_address[1]}", restore


# ---------------------------------------------------------------------------
# case harness
# ---------------------------------------------------------------------------

class Campaign:
    def __init__(self):
        self.results: list[dict] = []

    def case(self, dialect: str, area: str, name: str, fn):
        t0 = time.perf_counter()
        rec = {"dialect": dialect, "area": area, "case": name,
               "provenance": "real-client-staging"}
        try:
            detail = fn()
            rec.update(status="PASS", detail=str(detail or "")[:200])
        except AssertionError as err:
            rec.update(status="FAIL", detail=f"assertion: {err}"[:400])
        except Exception as err:                                # noqa: BLE001
            rec.update(status="ERROR",
                       detail=f"{type(err).__name__}: {err}"[:400],
                       traceback=traceback.format_exc()[-800:])
        rec["ms"] = round((time.perf_counter() - t0) * 1000, 2)
        self.results.append(rec)
        return rec


def run(base: str) -> Campaign:
    import anthropic
    import openai

    c = Campaign()
    A = lambda key=KEY_A, **kw: anthropic.Anthropic(          # noqa: E731
        api_key=key, base_url=base, max_retries=0, **kw)
    O = lambda key=KEY_A, **kw: openai.OpenAI(                # noqa: E731
        api_key=key, base_url=base + "/v1", max_retries=0, **kw)

    MSG = [{"role": "user", "content": "hello"}]

    # ---------------- Anthropic dialect ----------------

    def anth_auth_ok():
        m = A().messages.create(model="olympus-council", max_tokens=64,
                                messages=MSG)
        assert m.content[0].text.startswith("stub reply"), m
        assert m.role == "assistant" and m.type == "message"
        return f"stop_reason={m.stop_reason}"

    def anth_auth_rejected():
        try:
            A(key="wrong-key").messages.create(
                model="olympus-council", max_tokens=64, messages=MSG)
        except anthropic.AuthenticationError as err:
            return f"401 as expected ({err.status_code})"
        raise AssertionError("a bad key was ACCEPTED")

    def anth_streaming():
        with A().messages.stream(model="olympus-council", max_tokens=64,
                                 messages=MSG) as s:
            text = "".join(s.text_stream)
            final = s.get_final_message()
        assert text.startswith("stub streamed") or "stub" in text, text
        assert final.type == "message"
        return f"{len(text)} chars, final validated by the SDK"

    def anth_usage_reported():
        m = A().messages.create(model="olympus-council", max_tokens=64,
                                messages=MSG)
        assert m.usage is not None
        assert m.usage.input_tokens >= 0 and m.usage.output_tokens >= 0
        return f"in={m.usage.input_tokens} out={m.usage.output_tokens}"

    def anth_system_prompt():
        m = A().messages.create(model="olympus-council", max_tokens=64,
                                system="You are terse.", messages=MSG)
        return m.content[0].text[:60]

    def anth_multiturn():
        m = A().messages.create(
            model="olympus-council", max_tokens=64,
            messages=[{"role": "user", "content": "one"},
                      {"role": "assistant", "content": "two"},
                      {"role": "user", "content": "three"}])
        return m.content[0].text[:60]

    def anth_malformed_missing_max_tokens():
        req = urllib.request.Request(
            base + "/v1/messages",
            data=json.dumps({"model": "m", "messages": MSG}).encode(),
            headers={"content-type": "application/json", "x-api-key": KEY_A})
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as err:
            body = json.loads(err.read())
            assert err.code == 400, err.code
            assert body["type"] == "error" and body["error"]["type"], body
            return f"400 {body['error']['type']}"
        raise AssertionError("a request with no max_tokens was ACCEPTED")

    def anth_unknown_field_is_refused_loudly():
        """W3-A2: an unsupported field must never be silently ignored."""
        req = urllib.request.Request(
            base + "/v1/messages",
            data=json.dumps({"model": "m", "max_tokens": 16, "messages": MSG,
                             "top_k": 5}).encode(),
            headers={"content-type": "application/json", "x-api-key": KEY_A})
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as err:
            assert err.code == 400, err.code
            return f"400 on top_k ({json.loads(err.read())['error']['type']})"
        raise AssertionError("top_k was silently ignored")

    def anth_hostile_scalar():
        """Phase-4 B-F1 over a real socket: `Infinity` must refuse in band,
        not drop the connection."""
        req = urllib.request.Request(
            base + "/v1/messages",
            data=b'{"model":"m","max_tokens":Infinity,'
                 b'"messages":[{"role":"user","content":"hi"}]}',
            headers={"content-type": "application/json", "x-api-key": KEY_A})
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as err:
            assert err.code == 400, err.code
            return "400, connection intact"
        raise AssertionError("Infinity was ACCEPTED")

    def anth_large_payload():
        big = "x" * 200_000
        m = A().messages.create(model="olympus-council", max_tokens=64,
                                messages=[{"role": "user", "content": big}])
        return f"200 KiB accepted, {len(m.content[0].text)} char reply"

    def anth_oversize_payload_refused():
        huge = "x" * 2_000_000            # over the 1 MB _MAX_BODY guard
        try:
            A().messages.create(model="olympus-council", max_tokens=64,
                                messages=[{"role": "user", "content": huge}])
        except Exception as err:                                # noqa: BLE001
            return f"refused: {type(err).__name__}"
        raise AssertionError("a 2 MB body was ACCEPTED past the 1 MB guard")

    def anth_timeout_is_client_side():
        try:
            A(timeout=0.25).messages.create(
                model="olympus-council", max_tokens=64,
                messages=[{"role": "user", "content": "SLOW please"}])
        except Exception as err:                                # noqa: BLE001
            return f"client timed out cleanly: {type(err).__name__}"
        raise AssertionError("the 2 s stub returned inside a 0.25 s timeout")

    def anth_client_disconnect_mid_stream():
        """The server must survive a client vanishing mid-SSE."""
        with A().messages.stream(model="olympus-council", max_tokens=64,
                                 messages=MSG) as s:
            for _ in s.text_stream:
                break                      # abandon the stream
        m = A().messages.create(model="olympus-council", max_tokens=64,
                                messages=MSG)
        return f"server healthy after disconnect: {m.type}"

    def anth_concurrent():
        errs, oks = [], []

        def hit(i):
            try:
                m = A().messages.create(
                    model="olympus-council", max_tokens=64,
                    messages=[{"role": "user", "content": f"req-{i}"}])
                oks.append(m.content[0].text)
            except Exception as err:                            # noqa: BLE001
                errs.append(f"{type(err).__name__}: {err}")

        ts = [threading.Thread(target=hit, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        assert not errs, errs[:3]
        assert len(oks) == 8, len(oks)
        return "8/8 concurrent requests OK"

    def anth_audit_headers():
        raw = urllib.request.urlopen(urllib.request.Request(
            base + "/v1/messages",
            data=json.dumps({"model": "m", "max_tokens": 16,
                             "messages": MSG}).encode(),
            headers={"content-type": "application/json",
                     "x-api-key": KEY_A}), timeout=15)
        assert raw.headers.get("X-Olympus-Audit"), dict(raw.headers)
        return (f"audit={raw.headers.get('X-Olympus-Audit')} "
                f"mode={raw.headers.get('X-Olympus-Mode')} "
                f"run={raw.headers.get('X-Olympus-Run-Id')}")

    def principal_isolation():
        """P5-A14 over the wire: two DIFFERENT keys must derive two DIFFERENT
        memory principals."""
        StubCouncil.users.clear()
        A(key=KEY_A).messages.create(model="olympus-council", max_tokens=16,
                                     messages=MSG)
        A(key=KEY_B).messages.create(model="olympus-council", max_tokens=16,
                                     messages=MSG)
        seen = list(StubCouncil.users)
        assert len(seen) == 2, seen
        assert seen[0] != seen[1], f"both keys collapsed onto {seen[0]!r}"
        assert KEY_A not in seen[0] and KEY_B not in seen[1], seen
        return f"distinct principals: {seen[0][:14]}… / {seen[1][:14]}…"

    for name, fn in (
            ("authenticated request", anth_auth_ok),
            ("bad key is rejected", anth_auth_rejected),
            ("streaming", anth_streaming),
            ("usage reporting", anth_usage_reported),
            ("system prompt", anth_system_prompt),
            ("multi-turn", anth_multiturn),
            ("malformed: no max_tokens", anth_malformed_missing_max_tokens),
            ("unknown field refused", anth_unknown_field_is_refused_loudly),
            ("hostile scalar (Infinity)", anth_hostile_scalar),
            ("large payload 200 KiB", anth_large_payload),
            ("oversize payload refused", anth_oversize_payload_refused),
            ("client timeout", anth_timeout_is_client_side),
            ("disconnect mid-stream", anth_client_disconnect_mid_stream),
            ("8 concurrent requests", anth_concurrent),
            ("audit headers", anth_audit_headers),
            ("principal isolation", principal_isolation)):
        area = name.split(":")[0]
        c.case("anthropic", area, name, fn)

    # ---------------- OpenAI dialect ----------------

    def oai_auth_ok():
        r = O().chat.completions.create(model="olympus-council", messages=MSG)
        assert r.choices[0].message.content.startswith("stub reply")
        return f"finish_reason={r.choices[0].finish_reason}"

    def oai_auth_rejected():
        try:
            O(key="wrong-key").chat.completions.create(
                model="olympus-council", messages=MSG)
        except openai.AuthenticationError as err:
            return f"401 as expected ({err.status_code})"
        raise AssertionError("a bad key was ACCEPTED")

    def oai_streaming():
        stream = O().chat.completions.create(
            model="olympus-council", messages=MSG, stream=True)
        text = "".join(ch.choices[0].delta.content or "" for ch in stream)
        assert "stub" in text, text
        return f"{len(text)} chars"

    def oai_models_list():
        ids = [m.id for m in O().models.list().data]
        assert ids, "empty model list"
        return ",".join(ids[:3])

    def oai_usage_reported():
        r = O().chat.completions.create(model="olympus-council", messages=MSG)
        assert r.usage is not None
        return (f"prompt={r.usage.prompt_tokens} "
                f"completion={r.usage.completion_tokens}")

    def oai_malformed_body():
        req = urllib.request.Request(
            base + "/v1/chat/completions", data=b"{not json",
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {KEY_A}"})
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as err:
            assert 400 <= err.code < 500, err.code
            return f"{err.code} on unparseable body"
        raise AssertionError("unparseable JSON was ACCEPTED")

    def oai_unknown_field_tolerated():
        """The OpenAI dialect is intentionally more permissive than the
        Anthropic one; record which, so the asymmetry is a decision on record."""
        r = O().chat.completions.create(
            model="olympus-council", messages=MSG,
            extra_body={"an_unknown_openai_field": 123})
        return f"accepted, finish={r.choices[0].finish_reason}"

    def oai_concurrent():
        errs, oks = [], []

        def hit(i):
            try:
                r = O().chat.completions.create(
                    model="olympus-council",
                    messages=[{"role": "user", "content": f"c-{i}"}])
                oks.append(r.choices[0].message.content)
            except Exception as err:                            # noqa: BLE001
                errs.append(f"{type(err).__name__}: {err}")

        ts = [threading.Thread(target=hit, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        assert not errs, errs[:3]
        return f"{len(oks)}/8 concurrent requests OK"

    def oai_timeout_is_client_side():
        try:
            O(timeout=0.25).chat.completions.create(
                model="olympus-council",
                messages=[{"role": "user", "content": "SLOW please"}])
        except Exception as err:                                # noqa: BLE001
            return f"client timed out cleanly: {type(err).__name__}"
        raise AssertionError("the 2 s stub returned inside a 0.25 s timeout")

    for name, fn in (
            ("authenticated request", oai_auth_ok),
            ("bad key is rejected", oai_auth_rejected),
            ("streaming", oai_streaming),
            ("models list", oai_models_list),
            ("usage reporting", oai_usage_reported),
            ("malformed body", oai_malformed_body),
            ("unknown field tolerated", oai_unknown_field_tolerated),
            ("8 concurrent requests", oai_concurrent),
            ("client timeout", oai_timeout_is_client_side)):
        c.case("openai", name.split(":")[0], name, fn)

    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    os.environ["OLYMPUS_API_KEYS"] = f"{KEY_A},{KEY_B}"
    os.environ.setdefault("OLYMPUS_MODEL", "claude-opus-4-8")
    import tempfile
    from olympus import config
    config.MEMORY_DIR = Path(tempfile.mkdtemp(prefix="olymp5-compat-")) / "mem"

    import anthropic
    import openai

    srv, base, restore = start_server()
    try:
        camp = run(base)
    finally:
        restore()

    passed = sum(1 for r in camp.results if r["status"] == "PASS")
    total = len(camp.results)
    payload = {
        "executed": True,
        "provenance": "real-client-staging",
        "transport": "real TCP socket + real HTTP",
        "server": "olympus.web.Handler via ThreadingHTTPServer",
        "upstream_provider": "STUBBED — no real model call was made",
        "clients": {"anthropic": anthropic.__version__,
                    "openai": openai.__version__},
        "python": sys.version.split()[0],
        "passed": passed, "total": total,
        "results": camp.results,
    }

    if args.json:
        print(json.dumps(payload, indent=1))
        return 0 if passed == total else 1

    print("=" * 92)
    print("PHASE 5 STEP 8 — EXTERNAL-CLIENT COMPATIBILITY (real SDKs, real HTTP)")
    print("=" * 92)
    print(f"clients: anthropic {anthropic.__version__} · "
          f"openai {openai.__version__} · python {sys.version.split()[0]}")
    print(f"transport: real TCP socket → real ThreadingHTTPServer → real "
          f"web.Handler")
    print(f"upstream provider: STUBBED (no credential available; no real model "
          f"call was made)")
    print("-" * 92)
    print(f"{'dialect':<11} {'case':<34} {'status':<7} {'ms':>8}  detail")
    print("-" * 92)
    for r in camp.results:
        print(f"{r['dialect']:<11} {r['case']:<34} {r['status']:<7} "
              f"{r['ms']:>8.1f}  {r['detail'][:38]}")
    print("-" * 92)
    print(f"{passed}/{total} passed")
    print()
    print("CLAIM CEILING: real-SDK-over-HTTP verified in staging.")
    print("NOT production-client verified — the upstream provider is stubbed,")
    print("and no third-party application drove this across a real network.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
