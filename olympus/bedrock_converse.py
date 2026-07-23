"""Native AWS Bedrock Converse client for NON-Claude models (Titan/Llama/Mistral/…).

Claude-on-Bedrock rides the API-compatible `AnthropicBedrock` client (see
`llm._bedrock_client`), so every Claude call path works unchanged. But the
Anthropic SDK cannot drive Bedrock's *other* model families — Amazon Titan/Nova,
Meta Llama, Mistral, Cohere — which speak Bedrock's own `Converse` API. This
module is that client (DEFERRED #13): a thin, dependency-lazy wrapper over
`bedrock-runtime.converse` so those models are first-class Olympus backends.

Design:
  * **boto3 is an OPTIONAL, lazily-imported extra** — imported only inside
    `_client()`, so the pure-stdlib import footprint (the three-dependency claim,
    enforced by `test_deps_claim`) is unchanged. Bedrock already required the AWS
    extra for Claude; this reuses the same credential chain.
  * **Injectable client** — every entry point accepts a `client=` so the request
    shaping and response parsing are unit-testable with a fake, no network.
  * **Pure mapping** — Olympus's `(system, messages)` shape ↔ the Converse
    request/response is a pure transform (`_to_converse` / `_text_from`),
    tested directly.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import config, toolcall_repair

_MAX_TOKENS_DEFAULT = 4096


def is_converse_model(model: str) -> bool:
    """True for a Bedrock model that must use the Converse API — i.e. anything
    that is NOT Claude (Claude rides the API-compatible AnthropicBedrock client).
    Matches on the model id / inference-profile ARN."""
    m = (model or "").lower()
    return bool(m) and "claude" not in m and "anthropic." not in m


def _region() -> str:
    return (os.environ.get("OLYMPUS_BEDROCK_REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")


def _max_tokens() -> int:
    try:
        return max(1, int(os.environ.get("OLYMPUS_BEDROCK_MAX_TOKENS",
                                         str(_MAX_TOKENS_DEFAULT))))
    except (TypeError, ValueError):
        return _MAX_TOKENS_DEFAULT


def _client():
    """A `bedrock-runtime` client from the standard AWS credential chain (env /
    shared config / instance role), same as `llm._bedrock_client`. boto3 is
    imported HERE (lazy) so the module import stays stdlib-only."""
    try:
        import boto3
    except Exception as err:                     # pragma: no cover - env-dependent
        raise RuntimeError(
            "Bedrock Converse needs the AWS extra — run `pip install boto3`. "
            f"Import failed: {err}") from err
    kwargs: dict[str, Any] = {"region_name": _region()}
    ak = os.environ.get("OLYMPUS_BEDROCK_ACCESS_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
    sk = os.environ.get("OLYMPUS_BEDROCK_SECRET_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if ak and sk:
        kwargs["aws_access_key_id"] = ak
        kwargs["aws_secret_access_key"] = sk
        st = os.environ.get("AWS_SESSION_TOKEN")
        if st:
            kwargs["aws_session_token"] = st
    return boto3.client("bedrock-runtime", **kwargs)


def _content_text(content: Any) -> str:
    """Flatten an Olympus message `content` (a string, or a list of text/blocks)
    into the plain text Converse carries. Non-text blocks are dropped — Converse
    for these families is text-first."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                out.append(str(block["text"]))
            elif isinstance(block, str):
                out.append(block)
        return "\n".join(out)
    return str(content or "")


def _to_converse(system: str, messages: list[dict], effort: str = "high",
                 max_tokens: int | None = None) -> dict:
    """Pure: Olympus (system, messages) → a Bedrock Converse request body.
    Consecutive same-role turns are merged (Converse requires strictly
    alternating roles and a leading user turn)."""
    conv: list[dict] = []
    for m in messages or []:
        role = "assistant" if str(m.get("role")) == "assistant" else "user"
        text = _content_text(m.get("content"))
        if not text:
            continue
        if conv and conv[-1]["role"] == role:
            conv[-1]["content"][0]["text"] += "\n" + text
        else:
            conv.append({"role": role, "content": [{"text": text}]})
    if not conv or conv[0]["role"] != "user":
        conv.insert(0, {"role": "user", "content": [{"text": "(begin)"}]})
    body: dict[str, Any] = {
        "messages": conv,
        "inferenceConfig": {"maxTokens": max_tokens or _max_tokens()},
    }
    if system and str(system).strip():
        body["system"] = [{"text": str(system)}]
    return body


def _text_from(response: dict) -> str:
    """Pure: pull the assistant text out of a Converse response, tolerant of a
    missing/oddly-shaped body (returns '' rather than raising)."""
    try:
        content = response["output"]["message"]["content"]
    except (KeyError, TypeError):
        return ""
    if not isinstance(content, list):
        return ""
    parts = [str(b["text"]) for b in content
             if isinstance(b, dict) and "text" in b]
    return "\n".join(p for p in parts if p)


def complete_text(settings: config.Settings, system: str,
                  messages: list[dict], effort: str = "high",
                  *, client=None) -> str:
    """Native Converse text completion for a non-Claude Bedrock model."""
    model = config.require_model(settings)
    cli = client or _client()
    body = _to_converse(system, messages, effort)
    resp = cli.converse(modelId=model, **body)
    return _text_from(resp)



def complete_json(settings: config.Settings, system: str,
                  messages: list[dict], schema: dict, effort: str = "high",
                  *, client=None) -> dict:
    """Structured output via Converse. These families have no universal
    structured-output mode, so we instruct JSON and parse tolerantly (first
    balanced object), matching how the OpenAI-compat path handles non-native
    schema models. Raises ValueError if no JSON object can be recovered."""
    sys2 = (str(system or "") + "\n\nRespond with ONLY a single valid JSON "
            "object matching the requested schema. No prose, no code fence.")
    text = complete_text(settings, sys2, messages, effort, client=client)
    # Shared brace-balanced recovery (also backs openai_compat + tool-call
    # repair) — tolerates a fenced or prose-wrapped object and trailing text.
    obj = toolcall_repair.extract_json_object(text)
    if obj is not None:
        return obj
    raise ValueError("Bedrock Converse model did not return parseable JSON")
