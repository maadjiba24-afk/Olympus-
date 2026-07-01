"""Inbound OpenAI-compatible translation (clients → Olympus).

This is the *server* side of the OpenAI Chat Completions protocol: it turns an
incoming OpenAI request into a single prompt the existing council pipeline can
run (`orchestrator.Olympus.ask`), then shapes the council's answer back into a
spec-correct `chat.completion` (or a stream of `chat.completion.chunk` events).

It is the mirror image of `openai_compat.py`, which is the *outbound* client
(Olympus → providers). The two never share a request loop: this module makes no
network calls of its own — it only translates shapes — and the HTTP plumbing
lives in `web.py`'s `Handler`. Stdlib only.
"""

from __future__ import annotations

import json
import time
import uuid

MODEL_ID = "olympus-council"


def _content_text(content) -> str:
    """Flatten an OpenAI message `content` into plain text.

    `content` is either a string or the structured content-parts array
    (`[{"type": "text", "text": "..."}, ...]`). Non-text parts (images, etc.)
    have no place in the text pipeline, so they're dropped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def messages_to_prompt(messages: list) -> str:
    """Collapse an OpenAI `messages` array into one prompt for the council.

    The inbound API is stateless — the client resends the whole conversation
    each call — but `Olympus.ask` takes a single user message. So system
    messages become a preface, prior turns become a transcript, and the latest
    user message is the actual question. A lone user message (the common case)
    passes through verbatim so single-shot calls aren't wrapped in scaffolding.
    """
    if not isinstance(messages, list):
        return ""
    system = "\n\n".join(
        t for m in messages
        if isinstance(m, dict) and m.get("role") == "system"
        and (t := _content_text(m.get("content")).strip()))
    convo = [m for m in messages
             if isinstance(m, dict) and m.get("role") in ("user", "assistant")]

    if not convo:
        body = ""
    elif len(convo) == 1 and convo[0].get("role") == "user":
        body = _content_text(convo[0].get("content")).strip()
    else:
        prior = convo[:-1]
        last = convo[-1]
        lines = []
        for m in prior:
            who = "User" if m.get("role") == "user" else "Assistant"
            text = _content_text(m.get("content")).strip()
            if text:
                lines.append(f"{who}: {text}")
        transcript = "\n".join(lines)
        last_text = _content_text(last.get("content")).strip()
        if last.get("role") == "user":
            body = (f"Conversation so far:\n{transcript}\n\n"
                    f"User's latest message:\n{last_text}"
                    if transcript else last_text)
        else:
            body = transcript

    if system and body:
        return f"{system}\n\n{body}"
    return system or body


def estimate_tokens(text: str) -> int:
    """A rough token estimate (~4 chars/token) for the `usage` block.

    The council pipeline fans a single request across many internal model calls,
    so there is no single authoritative prompt/completion count to surface here.
    Rather than omit the field (which breaks strict OpenAI clients) we report a
    conservative character-based estimate. Documented as approximate."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def usage_block(prompt_text: str, answer: str) -> dict:
    prompt_tokens = estimate_tokens(prompt_text)
    completion_tokens = estimate_tokens(answer)
    return {"prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}


def new_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex[:24]


def completion_response(answer: str, model: str, prompt_text: str = "",
                        created: int | None = None,
                        cid: str | None = None) -> dict:
    """Shape a council answer into a spec-correct `chat.completion` object."""
    return {
        "id": cid or new_completion_id(),
        "object": "chat.completion",
        "created": created if created is not None else int(time.time()),
        "model": model or MODEL_ID,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
        "usage": usage_block(prompt_text, answer),
    }


def models_response(created: int | None = None) -> dict:
    """The `GET /v1/models` payload — the one council 'model' v1 exposes."""
    return {
        "object": "list",
        "data": [{
            "id": MODEL_ID,
            "object": "model",
            "created": created if created is not None else int(time.time()),
            "owned_by": "olympus",
        }],
    }


def _chunk(model: str, created: int, cid: str, delta: dict,
           finish_reason=None) -> str:
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model or MODEL_ID,
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": finish_reason}],
    }
    return "data: " + json.dumps(payload) + "\n\n"


def stream_events(pieces, model: str, created: int | None = None,
                  cid: str | None = None):
    """Yield OpenAI `chat.completion.chunk` SSE frames, ending in `[DONE]`.

    `pieces` is any iterable of text fragments (the council yields its final
    answer in one or more pieces). The first frame carries the assistant role,
    each piece becomes a content delta, and a final stop-frame plus the literal
    `data: [DONE]` terminator close the stream — exactly what the OpenAI client
    expects."""
    created = created if created is not None else int(time.time())
    cid = cid or new_completion_id()
    yield _chunk(model, created, cid, {"role": "assistant", "content": ""})
    for piece in pieces:
        if piece:
            yield _chunk(model, created, cid, {"content": piece})
    yield _chunk(model, created, cid, {}, finish_reason="stop")
    yield "data: [DONE]\n\n"
