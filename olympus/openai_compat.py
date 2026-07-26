"""OpenAI-compatible backend — any /chat/completions endpoint, zero deps.

Covers OpenAI, Google Gemini (OpenAI-compat endpoint), DeepSeek, Groq, Mistral,
OpenRouter, Ollama, LM Studio, vLLM... Tool calling uses standard function
calling; structured output uses prompt-enforced JSON with lenient parsing so
it works even on providers without response_format support.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import config, security, streamguard, toolcall_repair, tools, usage

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Per-endpoint credential rotation state. When a key hits a rate limit or quota
# wall we mark it exhausted and advance a shared cursor, so subsequent calls
# start from the next healthy key instead of re-hitting the dead one. Keyed by
# base_url so different providers rotate independently.
_key_cursor: dict[str, int] = {}
_key_stats: dict[str, dict[str, int]] = {}   # base -> {"<masked>": success_count}
_exhausted: dict[str, set[str]] = {}         # base -> {masked keys seen exhausted}
# Rotation state is read/written from concurrent gateway worker threads
# (Telegram, Discord, web, …). A lock keeps cursor advances and the exhausted
# set from racing — without it two threads can skip or re-hit the same key.
import threading as _threading
_ROT_LOCK = _threading.RLock()

# Status codes / body markers that mean "this key is spent — try another".
_QUOTA_CODES = frozenset({402, 429})
_QUOTA_MARKERS = ("insufficient", "quota", "exceeded your current",
                  "billing", "rate limit", "out of credit", "balance")


def _is_quota_error(code: int, detail: str) -> bool:
    if code in _QUOTA_CODES:
        return True
    low = (detail or "").lower()
    return any(m in low for m in _QUOTA_MARKERS)


# Azure OpenAI speaks the same request/response body as OpenAI, but on a
# deployment-scoped URL with an `api-key` header instead of `Authorization:
# Bearer`. Detecting it by endpoint host lets Azure ride the whole openai_compat
# rotation/failover/usage machinery unchanged (provider stays "openai").
_AZURE_DEFAULT_API_VERSION = "2024-10-21"


def _is_azure(base: str) -> bool:
    # Match only the canonical Azure OpenAI host. A broader `.azure.com` catch
    # would misroute an ordinary OpenAI-compatible proxy that merely happens to
    # be Azure-hosted (it expects Bearer auth, not the api-key header).
    host = (urlparse(base).hostname or "").lower()
    return host.endswith(".openai.azure.com")


def _endpoint_url(settings: config.Settings, base: str) -> str:
    """The /chat/completions URL for this endpoint — deployment-scoped +
    api-version query for Azure, plain for every other OpenAI-compatible host."""
    if not _is_azure(base):
        return f"{base}/chat/completions"
    import os
    from urllib.parse import quote
    deployment = (os.environ.get("OLYMPUS_AZURE_DEPLOYMENT")
                  or settings.model or "").strip()
    if not deployment:
        raise ValueError(
            "Azure OpenAI needs a deployment name — set the model to your "
            "Azure deployment, or set OLYMPUS_AZURE_DEPLOYMENT.")
    api_version = os.environ.get("OLYMPUS_AZURE_API_VERSION",
                                 _AZURE_DEFAULT_API_VERSION)
    return (f"{base}/openai/deployments/{quote(deployment, safe='')}"
            f"/chat/completions?api-version={quote(api_version, safe='')}")


def _supports_reasoning_effort(model: str) -> bool:
    """Whether this OpenAI-compatible model accepts the `reasoning_effort`
    parameter. Conservative allowlist — sending it to a model that doesn't
    support it 400s the request, so we only enable it for the known reasoning
    families (OpenAI o-series + gpt-5, Gemini 2.5 / thinking). The provider
    prefix (`openai/`, `google/`) and any `:tag` suffix are stripped first so
    OpenRouter-style ids match too."""
    m = (model or "").lower().rsplit("/", 1)[-1].split(":", 1)[0]
    if m.startswith(("o1", "o3", "o4", "gpt-5")):
        return True
    if m.startswith("gemini-2.5"):
        return True
    return m.startswith("gemini") and "thinking" in m


def _reasoning_params(model: str, effort: str) -> dict:
    """Map Olympus's effort tier (low/medium/high) to `reasoning_effort` for
    models that support it. Empty dict otherwise (or when disabled via
    OLYMPUS_DISABLE_REASONING_EFFORT, the escape hatch if a proxy rejects it)."""
    import os
    if os.environ.get("OLYMPUS_DISABLE_REASONING_EFFORT", "").strip().lower() in (
            "1", "true", "yes", "on"):
        return {}
    if not _supports_reasoning_effort(model):
        return {}
    val = (effort or "").lower()
    return {"reasoning_effort": val} if val in ("low", "medium", "high") else {}


def _auth_headers(base: str, key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        # Azure authenticates with an `api-key` header; everyone else with a
        # standard OpenAI-style bearer token.
        if _is_azure(base):
            headers["api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def _record_key_use(base: str, masked: str) -> None:
    with _ROT_LOCK:
        _key_stats.setdefault(base, {})
        _key_stats[base][masked] = _key_stats[base].get(masked, 0) + 1


def rotation_report(settings: config.Settings) -> str:
    """Human-readable provenance of key rotation for `olympus models`. Never
    prints a secret — only masked tails and per-key success counts."""
    keys = settings.all_keys()
    if len(keys) <= 1:
        return ""
    base = (settings.base_url or DEFAULT_BASE_URL).rstrip("/")
    stats = _key_stats.get(base, {})
    spent = _exhausted.get(base, set())
    lines = [f"Credential rotation ({len(keys)} keys for {settings.provider}):"]
    for i, k in enumerate(keys):
        masked = config.mask_key(k)
        flags = []
        if masked in spent:
            flags.append("exhausted")
        if i == _key_cursor.get(base, 0) % len(keys):
            flags.append("active")
        used = stats.get(masked, 0)
        tag = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {masked}: {used} calls{tag}")
    return "\n".join(lines)


def _payload_chars(payload: dict[str, Any]) -> int:
    """Cheap, deterministic character count of the request's message contents
    — the calibration input for ctxbudget.observe (C4)."""
    total = 0
    for m in payload.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else m
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(str(content))
    return total


def _record_usage(payload: dict[str, Any], usage_block: dict[str, Any]) -> None:
    """C5/C4 seam: record the cache split when the provider reports it
    (`prompt_tokens_details.cached_tokens`), and feed the reported input total
    to context calibration. Telemetry must never break the call — the observe
    hook is exception-swallowed (observe() itself validates the sample)."""
    model = payload.get("model", "unknown")
    prompt = int(usage_block.get("prompt_tokens", 0) or 0)
    details = usage_block.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, dict):
        try:
            cached = int(details.get("cached_tokens", 0) or 0)
        except (TypeError, ValueError):
            cached = 0
    cached = max(0, min(cached, prompt))
    usage.record(model, prompt - cached,
                 int(usage_block.get("completion_tokens", 0) or 0),
                 cache_read=cached, provider="openai-compat")
    try:
        from . import ctxbudget
        chars = _payload_chars(payload)
        if chars > 0 and prompt > 0:
            ctxbudget.observe("openai-compat", model, chars, prompt)
    except Exception:
        pass


def _post(settings: config.Settings, payload: dict[str, Any]) -> dict[str, Any]:
    base = (settings.base_url or DEFAULT_BASE_URL).rstrip("/")
    url = _endpoint_url(settings, base)
    # Sovereign egress choke: under sovereign mode a model call to a
    # non-allowlisted host fails closed here (no-op when sovereign is off).
    security.assert_egress_allowed(urlparse(url).hostname or "")
    body = json.dumps(payload).encode()

    keys = list(settings.all_keys())
    # No credentials (e.g. a keyless local endpoint) — one unauthenticated pass
    # through the same retry logic.
    key_ring: list[str | None] = keys or [None]
    n = len(key_ring)
    with _ROT_LOCK:
        start = _key_cursor.get(base, 0) if keys else 0

    last_err: Exception | None = None
    # Try each key in turn; each key gets the full transient-error backoff loop.
    for k_off in range(n):
        idx = (start + k_off) % n
        key = key_ring[idx]
        headers = _auth_headers(base, key)
        masked = config.mask_key(key) if key else "(no key)"

        for attempt in range(4):
            req = urllib.request.Request(url, data=body, headers=headers)
            try:
                with usage.slot():
                    with urllib.request.urlopen(req, timeout=600) as resp:
                        data = json.loads(resp.read())
                u = data.get("usage") or {}
                _record_usage(payload, u)
                # A 200 that carries no `choices` is a real provider hiccup
                # (rate-limit shedding, a content filter, an upstream error
                # rendered as an empty body). Callers index `choices[0]`, so
                # returning it raises a bare `IndexError: list index out of
                # range` — an unactionable message that killed a whole CI eval.
                # Treat it as the transient failure it is: retry with the same
                # backoff as a 429/503, then rotate to the next key.
                if not (isinstance(data.get("choices"), list) and data["choices"]):
                    last_err = RuntimeError(
                        f"provider returned no choices: {json.dumps(data)[:300]}")
                    if attempt < 3:
                        time.sleep(2 ** attempt)
                        continue
                    break                          # advance to the next key
                if keys:
                    with _ROT_LOCK:
                        _key_cursor[base] = idx    # remember the healthy key
                    _record_key_use(base, masked)
                return data
            except urllib.error.HTTPError as err:
                detail = err.read().decode(errors="replace")[:500]
                # Quota/rate-limit on THIS key with another available → rotate
                # to the next key immediately instead of burning backoff on a
                # key that's already spent.
                if keys and n > 1 and _is_quota_error(err.code, detail):
                    with _ROT_LOCK:
                        _exhausted.setdefault(base, set()).add(masked)
                    last_err = RuntimeError(f"HTTP {err.code} (key {masked}): "
                                            f"{detail}")
                    break                          # advance to the next key
                if err.code in (408, 429, 500, 502, 503, 529) and attempt < 3:
                    last_err = RuntimeError(f"HTTP {err.code}: {detail}")
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(
                    f"Provider error HTTP {err.code} from {url}: {detail}"
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                last_err = err
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Provider call failed at {url}: {last_err}")


def _to_openai_tools(tool_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic-style tool defs to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d.get("description", ""),
                "parameters": d.get("input_schema", {"type": "object"}),
            },
        }
        for d in tool_defs
    ]


def extract_json(text: str) -> dict[str, Any]:
    """Lenient JSON extraction: tolerate code fences and surrounding prose.

    Delegates to the shared brace-balanced recovery in `toolcall_repair` (which
    also backs the tool-call repair path), so both structured-output and
    tool-call recovery share one hardened scanner. Raises `ValueError` when no
    JSON object can be recovered, preserving the previous contract.
    """
    obj = toolcall_repair.extract_json_object(text)
    if obj is None:
        raise ValueError("no JSON object found in model response")
    return obj


# --- C8 degenerate-response defense (W2-PR14) -------------------------------
#
# **This module is NOT streaming.** `_post` reads the whole body and json-parses
# it in one go; there is no SSE consumer here, so there is no delta seam to feed
# and no partially-delivered answer to truncate. That is stated plainly because
# the honest consequence matters: the guard runs at the RESPONSE-PARSE point,
# before any of the payload is turned into an answer or a tool execution.
#
# It is also the seam that closes the gap the Wave-2 completion report names.
# The EVENT-based detectors (`detect_malformed_event`, `detect_event_order`,
# `detect_invalid_tool_delta`, `detect_impossible_usage`) were unreachable from
# `llm.stream_text`, because the Anthropic SDK's `text_stream` yields text and
# nothing else. An OpenAI-compatible body carries exactly that missing
# information — role/content/tool_calls/usage — merely collapsed into one
# object. `response_events()` projects it back into the canonical event
# sequence, in the order a provider would have streamed it, and the monitor
# walks it.
#
# The projection is FAITHFUL, not decorative: every event it emits is derived
# from a field the provider actually sent, and it never invents an ordering the
# payload does not assert. A trip therefore always names a real defect in the
# body — a non-mapping message, a non-str content, an argument buffer that is
# not a string, two parallel calls interleaved into one `index`, a tool RESULT
# echoed back as the model's own message, counters that cannot be true.
# Well-formed bodies (the overwhelming majority) walk the projection cleanly,
# which is the false-positive guard.


def _declared_max_tokens(payload: dict[str, Any]) -> int | None:
    """The output bound this request declared, whichever key the model needed
    (`max_completion_tokens` for reasoning models, `max_tokens` otherwise).
    `None` when nothing was declared — then there is no bound to enforce."""
    for name in ("max_completion_tokens", "max_tokens"):
        val = payload.get(name) if isinstance(payload, dict) else None
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    return None


def _arg_payload(raw: Any) -> Any:
    """The wire form of a tool call's `arguments`.

    A dict is the STRUCTURED form of the same value — `repair_arguments()`
    accepts it as a supported provider variant — so it is projected as its JSON
    encoding rather than flagged, or the guard would reject calls this backend
    has always executed. An absent value is an empty buffer. Anything else is
    handed to the detector AS IS: a number, a list or a bool where the argument
    buffer belongs is precisely `INVALID_TOOL_DELTA`."""
    if raw is None:
        return ""
    if isinstance(raw, dict):
        try:
            return json.dumps(raw)
        except (TypeError, ValueError):
            return raw              # unserialisable — let the detector see it
    return raw


def _tool_call_events(tool_calls: Any):
    """Project `message.tool_calls` into content blocks.

    OpenAI keys PARALLEL calls by `index`: entries sharing an index are one
    argument buffer. Projecting them that way is what makes the "tool id changed
    mid-block" arm of `detect_invalid_tool_delta` reachable — a provider that
    interleaves two different call ids into a single index is synthesizing a
    tool call nobody asked for. Entries with no `index` are each their own
    block, and a continuation entry that carries no id (the streaming
    convention, where only the first chunk names the call) simply extends the
    open buffer, exactly as it should."""
    if tool_calls is None:
        return
    if not isinstance(tool_calls, list):
        yield {"type": "content_block_start", "content_block": tool_calls}
        return
    open_key: Any = None
    opened = False
    for pos, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            if opened:
                yield {"type": "content_block_stop"}
                opened = False
            yield {"type": "content_block_start", "content_block": call}
            continue
        fn = call.get("function")
        fn = fn if isinstance(fn, dict) else {}
        idx = call.get("index")
        group = (("i", idx) if isinstance(idx, int) and not isinstance(idx, bool)
                 else ("p", pos))
        call_id = str(call.get("id") or "")
        if group != open_key:
            if opened:
                yield {"type": "content_block_stop"}
            yield {"type": "content_block_start",
                   "content_block": {"type": "tool_use",
                                     "id": call_id or f"call_{pos}",
                                     "name": str(fn.get("name") or "")}}
            open_key = group
            opened = True
        yield {"type": "content_block_delta",
               "delta": {"type": "input_json_delta",
                         "partial_json": _arg_payload(fn.get("arguments")),
                         "id": call_id or None}}
    if opened:
        yield {"type": "content_block_stop"}


def response_events(data: Any, *, choice_index: int = 0):
    """Project one parsed `/chat/completions` body into the canonical provider
    event sequence. PURE (W2-I8.2): a generator over the payload — no I/O, no
    clock, no hidden state, safe to call from a test with a crafted body."""
    yield {"type": "message_start"}
    if not isinstance(data, dict):
        yield data                      # not a mapping ⇒ MALFORMED_EVENTS
        return

    choices = data.get("choices")
    choice = None
    if isinstance(choices, list) and len(choices) > choice_index:
        choice = choices[choice_index]
    message = choice.get("message") if isinstance(choice, dict) else None

    if isinstance(message, dict):
        # A tool RESULT echoed back as the model's OWN message — a broken relay
        # or a proxy replaying the conversation. The payload literally asserts a
        # tool_result, and nothing before it was a tool_use, so the order
        # detector is right to reject it.
        if message.get("tool_call_id") or message.get("role") == "tool":
            yield {"type": "tool_result"}
        content = message.get("content")
        if content is not None:
            yield {"type": "content_block_start",
                   "content_block": {"type": "text"}}
            yield {"type": "content_block_delta",
                   "delta": {"type": "text_delta", "text": content}}
            yield {"type": "content_block_stop"}
        yield from _tool_call_events(message.get("tool_calls"))
    elif message is not None:
        # `message` is present but is not a mapping: project it as the content
        # block it claims to be so the shape detector sees it.
        yield {"type": "content_block_start", "content_block": message}

    usage_block = data.get("usage")
    if usage_block is not None:
        yield {"type": "message_delta", "delta": {}, "usage": usage_block}
    yield {"type": "message_stop"}


def _guard_response(settings: config.Settings, payload: dict[str, Any],
                    data: Any):
    """Walk a parsed response through the C8 monitor. Returns the monitor.

    Flag off (`OLYMPUS_STREAMGUARD` unset) ⇒ `monitor()` hands back an inert
    `NullMonitor` and this function returns immediately WITHOUT projecting
    anything, so the response path is byte-identical to the pre-C8 one and
    costs nothing (A17 rollback).

    On a trip: the evidence record is written and a typed `StreamAborted` is
    raised. The caller has not yet produced an answer or executed a tool, so
    aborting here is what "terminate safely" means at a non-streaming seam —
    there is nothing to truncate and nothing to roll back."""
    model = str(payload.get("model") or settings.model or "")
    guard = streamguard.monitor(provider="openai-compat", model=model,
                                max_tokens=_declared_max_tokens(payload))
    if not getattr(guard, "enabled", False):
        return guard
    try:
        for event in response_events(data):
            guard.feed(event)
    except streamguard.StreamPathology as exc:
        rec = streamguard.pathology_record(guard, exc,
                                           provider="openai-compat",
                                           model=model)
        streamguard.record_pathology(rec)
        raise streamguard.StreamAborted(exc.kind, exc.detail,
                                        record=rec) from exc
    return guard


def abort_disclosure(exc: streamguard.StreamAborted) -> str:
    """The user-visible disclosure for a tripped response (W2-I8.3): an explicit
    failure that names the pathology, never a truncated answer presented as a
    finished one.

    The completed tool calls are named from `safe_tool_calls()` (carried on the
    evidence record) so the disclosure is honest about what was discarded — and
    it is a DISCARD, not a deferral: nothing from a tripped response is
    executed, so the partial call in flight cannot reach a handler (W2-I8.1)."""
    kind = getattr(exc, "kind", "pathology")
    detail = getattr(exc, "detail", "") or str(exc)
    safe = [str(n) for n in (getattr(exc, "record", {}) or {}).get(
        "safe_tool_calls", []) if n]
    tail = ""
    if safe:
        tail = (f" {len(safe)} tool call(s) that had parsed cleanly "
                f"({', '.join(safe[:8])}) were discarded with it.")
    return (f"[Response aborted: the provider's reply tripped the "
            f"degenerate-stream guard ({kind}: {detail}). No tool call from "
            f"this response was executed.{tail} Nothing above is a finished "
            f"answer.]")


def complete_text(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], effort: str = "high") -> str:
    payload: dict[str, Any] = {
        "model": settings.model,
        "messages": [{"role": "system", "content": system}, *messages],
    }
    # Map the effort tier to `reasoning_effort` for models that support it
    # (allowlist-gated so non-reasoning models are never sent an unknown param).
    payload.update(_reasoning_params(settings.model, effort))
    # Bound the response — without a cap, reasoning models can generate
    # unboundedly (a major latency/cost sink). config.MAX_TOKENS is generous.
    # Reasoning models (o-series/gpt-5) REQUIRE `max_completion_tokens` and
    # reject the legacy `max_tokens`, so pick the right key per model.
    tok_key = ("max_completion_tokens" if _supports_reasoning_effort(settings.model)
               else "max_tokens")
    payload[tok_key] = config.MAX_TOKENS
    resp = _post(settings, payload)
    # C8 response-parse seam. No-op when OLYMPUS_STREAMGUARD is off. On a trip
    # the typed StreamAborted propagates INSTEAD of the text — a degenerate
    # reply must never be handed back as a finished answer (W2-I8.3).
    _guard_response(settings, payload, resp)
    return (resp["choices"][0]["message"].get("content") or "").strip()


def complete_json(settings: config.Settings, system: str,
                  messages: list[dict[str, Any]], schema: dict[str, Any],
                  effort: str = "high") -> dict[str, Any]:
    instruction = (
        "\n\nRespond with ONLY a single JSON object matching this JSON schema "
        "— no prose, no code fences:\n" + json.dumps(schema)
    )
    text = complete_text(settings, system + instruction, messages, effort)
    return extract_json(text)


def _raw_string_payload(raw: Any) -> str | None:
    """The lone STRING payload of a tool call's `arguments`, if that is what
    the model sent (directly, or JSON-encoded by the recovery path) — the
    input to the default-off salvage tier. None when arguments held anything
    structured."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return raw                      # a bare non-JSON string
    return val if isinstance(val, str) and val.strip() else None


def _validated_args(settings: config.Settings, name: str, call: dict[str, Any],
                    args: dict[str, Any], schema: dict[str, Any],
                    recovered: bool) -> tuple[dict[str, Any], bool, str]:
    """C8 execution precondition (I-T1): a call reaches its handler ONLY after
    passing rung-2 validation against the authoritative input_schema. Returns
    (args, ok, error_message): on hard failure the handler is never invoked and
    `error_message` is fed back to the model (rung 2, outcome 'rejected')."""
    rung = (toolcall_repair.RUNG_NORMALIZED if recovered
            else toolcall_repair.RUNG_STRICT)
    errors, _warnings = toolcall_repair.validate_arguments(args, schema)
    if not errors:
        toolcall_repair.record_repair("openai-compat", settings.model, name,
                                      rung, "ok")
        return args, True, ""
    # Rung 4a: schema-typed coercion, accepted only if it fully fixes the call.
    coerced = toolcall_repair.coerce_arguments(args, schema)
    errors2, _warnings2 = toolcall_repair.validate_arguments(coerced, schema)
    if not errors2:
        toolcall_repair.record_repair("openai-compat", settings.model, name,
                                      toolcall_repair.RUNG_REPAIRED, "coerced")
        return coerced, True, ""
    # Rung 4b (default OFF): lone string payload onto the single required param.
    if not args and toolcall_repair.salvage_enabled():
        payload = _raw_string_payload(call["function"].get("arguments"))
        if payload is not None:
            salvaged = toolcall_repair.salvage_arguments(payload, schema)
            if salvaged is not None:
                errors3, _w3 = toolcall_repair.validate_arguments(salvaged, schema)
                if not errors3:
                    toolcall_repair.record_repair(
                        "openai-compat", settings.model, name,
                        toolcall_repair.RUNG_REPAIRED, "salvaged")
                    return salvaged, True, ""
    toolcall_repair.record_repair("openai-compat", settings.model, name,
                                  toolcall_repair.RUNG_VALIDATED, "rejected")
    return args, False, "Error: invalid arguments: " + "; ".join(errors2)


def run_agent(settings: config.Settings, system: str, task: str,
              tool_defs: list[dict[str, Any]] | None, effort: str = "high",
              max_iterations: int = config.MAX_AGENT_ITERATIONS) -> str:
    """Agentic tool loop over the OpenAI function-calling protocol."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    payload_tools = _to_openai_tools(tool_defs) if tool_defs else None
    # Tool names the model is actually offered — the refusal-safety guard for
    # tool-call recovery: a call is only reconstructed from `content` when it
    # names one of these (see toolcall_repair.recover_tool_call).
    known_names = {d["name"] for d in tool_defs} if tool_defs else set()
    # The authoritative schema per tool (C8 rung-2 validation source) and the
    # legacy escape hatch (OLYMPUS_TOOL_VALIDATE=off ⇒ pre-C8 pass-through).
    schema_by_name = ({d["name"]: (d.get("input_schema") or {})
                       for d in tool_defs} if tool_defs else {})
    validate_on = toolcall_repair.validate_enabled()

    reasoning = _reasoning_params(settings.model, effort)
    for _ in range(max_iterations):
        payload: dict[str, Any] = {"model": settings.model, "messages": messages}
        if payload_tools:
            payload["tools"] = payload_tools
        payload.update(reasoning)
        data = _post(settings, payload)
        # C8 execution precondition (W2-I8.1): the guard runs BEFORE a single
        # tool call is looked at, so a tripped response can never reach
        # `repair_arguments` / a handler — the in-flight partial call has no
        # path to execution. On a healthy response the guard changes nothing and
        # the repair ladder keeps its job (a merely unparseable buffer is the
        # ladder's business, not a pathology).
        try:
            _guard_response(settings, payload, data)
        except streamguard.StreamAborted as exc:
            return abort_disclosure(exc)
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []

        # Weak models sometimes emit the tool call as JSON in `content` with an
        # empty `tool_calls`. Recover it — but only when it names an offered tool,
        # so a genuine refusal or final answer stays text (never faked into an
        # action). No tools offered → nothing to recover.
        if not tool_calls and payload_tools:
            content = message.get("content") or ""
            recovered = toolcall_repair.recover_tool_call(content, known_names)
            if recovered is None:
                # Truncated tail: close it ONLY when unambiguous (C8 rung 4),
                # then re-run the same refusal-gated recovery on the result.
                closed = toolcall_repair.close_truncated(content)
                if closed is not None:
                    recovered = toolcall_repair.recover_tool_call(
                        closed, known_names)
                    if recovered is not None:
                        toolcall_repair.record_repair(
                            "openai-compat", settings.model,
                            recovered["function"]["name"],
                            toolcall_repair.RUNG_REPAIRED, "closed")
            if recovered is not None:
                tool_calls = [recovered]

        if not tool_calls:
            return (message.get("content") or "").strip()

        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        })
        for call in tool_calls:
            name = call["function"]["name"]
            handler = tools.resolve_handler(name)
            recovered_call = str(call.get("id", "")).startswith("repaired_")
            try:
                args = toolcall_repair.repair_arguments(
                    call["function"].get("arguments"))
                if handler is None:
                    result = f"Error: unknown tool {name}"
                elif validate_on:
                    # Execution precondition (I-T1): validate — native and
                    # recovered calls alike — before the handler ever runs; a
                    # hard failure bounces back to the model as an error result.
                    args, ok, err_msg = _validated_args(
                        settings, name, call, args,
                        schema_by_name.get(name, {}), recovered_call)
                    result = handler(**args) if ok else err_msg
                else:
                    result = handler(**args)   # legacy pass-through
            except Exception as err:
                result = f"Error: {err}"
            text = str(result)[:40_000]
            if security.should_wrap(name):
                text = security.wrap_untrusted(text, source=name)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", name),
                "content": text,
            })

    return ("[Agent stopped: tool-use iteration limit reached. Partial work "
            "above may be incomplete.]")
