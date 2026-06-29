# Olympus as an OpenAI-compatible endpoint

Olympus speaks the **OpenAI Chat Completions protocol** on the way *in*, so any
tool that already talks to OpenAI — the stock `openai` Python/JS clients, IDE
plugins, agent frameworks, `curl` — can drive the **full Olympus council** by
changing only three things:

| OpenAI setting | Point it at Olympus |
| -------------- | ------------------- |
| `base_url`     | `http://<host>:<port>/v1` |
| `model`        | `olympus-council` (any value works — see below) |
| `api_key`      | one of your `OLYMPUS_API_KEYS` |

Every request runs the same pipeline the CLI/TUI/dashboard use —
route → plan → dispatch → verify (hallucination control) → review →
synthesize — and the council's final answer comes back as a spec-correct
`chat.completion`.

## Start the server

```bash
export OLYMPUS_API_KEYS=sk-my-secret-key      # comma-separated list of bearer keys
python -m olympus serve --host 127.0.0.1 --port 8484
# (python -m olympus web also mounts the same /v1/* routes)
```

The HTTP API and the OpenAI-compatible `/v1/*` routes are served by the same
process; `olympus serve` and `olympus web` are equivalent for this purpose.

## Authentication

The `/v1/*` endpoints are gated by **`OLYMPUS_API_KEYS`** — a comma-separated
list of accepted bearer keys. Send one as a standard bearer token:

```
Authorization: Bearer sk-my-secret-key
```

- A **missing or invalid** key returns **`401`**.
- **Loopback-only default:** if `OLYMPUS_API_KEYS` is *unset*, the `/v1/*`
  routes answer **only on loopback** (`127.0.0.0/8`, `::1`). A remote caller is
  refused with **`403`** — Olympus never becomes a silent open relay. Set
  `OLYMPUS_API_KEYS` before exposing the endpoint off-box.

> This is independent of the dashboard's `OLYMPUS_ACCESS_TOKEN` (the
> `X-Olympus-Token` header), which gates the browser UI's `/api/*` routes.

## Endpoints

### `GET /v1/models`

```bash
curl -s http://127.0.0.1:8484/v1/models \
  -H "Authorization: Bearer sk-my-secret-key"
```

```json
{
  "object": "list",
  "data": [
    {"id": "olympus-council", "object": "model", "created": 1782700000, "owned_by": "olympus"}
  ]
}
```

For v1 there is exactly one logical model, `olympus-council`. Any `model` value
you send to `/v1/chat/completions` maps to the same council pipeline; the value
you send is echoed back in the response's `model` field.

### `POST /v1/chat/completions` — non-streaming

```bash
curl -s http://127.0.0.1:8484/v1/chat/completions \
  -H "Authorization: Bearer sk-my-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "olympus-council",
    "messages": [{"role": "user", "content": "Say hello in one short sentence."}]
  }'
```

```json
{
  "id": "chatcmpl-1a2b3c...",
  "object": "chat.completion",
  "created": 1782700000,
  "model": "olympus-council",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "Hello — the council greets you."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
}
```

### `POST /v1/chat/completions` — streaming

Set `"stream": true` to receive Server-Sent Events of OpenAI
`chat.completion.chunk` objects, terminated by a literal `data: [DONE]`:

```bash
curl -s -N http://127.0.0.1:8484/v1/chat/completions \
  -H "Authorization: Bearer sk-my-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "olympus-council",
    "stream": true,
    "messages": [{"role": "user", "content": "Count to three."}]
  }'
```

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}], ...}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"One, two, three."},"finish_reason":null}], ...}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}], ...}

data: [DONE]
```

## The stock `openai` Python client

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8484/v1", api_key="sk-my-secret-key")

# non-streaming
resp = client.chat.completions.create(
    model="olympus-council",
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
)
print(resp.choices[0].message.content)

# streaming
stream = client.chat.completions.create(
    model="olympus-council",
    stream=True,
    messages=[{"role": "user", "content": "Count to three."}],
)
for chunk in stream:
    delta = chunk.choices[0].delta
    if delta and delta.content:
        print(delta.content, end="", flush=True)
```

## v1 scope and limitations (stated plainly)

- **One pipeline, any model name.** Per-model/per-specialist selection via the
  API is out of scope for v1; any `model` value maps to the one council
  pipeline.
- **`tools` is accepted but ignored.** You may include a `tools` field (and
  other OpenAI params such as `temperature`, `max_tokens`, `top_p`) — Olympus
  accepts them syntactically and ignores the ones it doesn't use, rather than
  erroring. Function-calling passthrough on the inbound API is not implemented
  in v1.
- **Streaming is correct but coarse-grained.** Olympus streams the final
  synthesized answer. On the Anthropic backend that arrives token-by-token; on
  other backends the final answer is emitted as a single content chunk followed
  by `[DONE]`. Either way the stream is well-formed — no fabricated intermediate
  tokens.
- **`usage` is an estimate.** A single inbound request fans out across many
  internal model calls, so there is no one authoritative prompt/completion
  count. The `usage` block reports a conservative character-based estimate
  (~4 chars/token) and is always present — never omitted.
- **Stateless.** Each request is independent; send the full `messages` array
  (system + prior turns + the new user message) each call, as with OpenAI.
