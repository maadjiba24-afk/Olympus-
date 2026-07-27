# Wave 3 Implementation Specification — Colibri Absorption

**Entry gate:** `WAVE3_EVIDENCE_REVIEW.md` — **4 NO-GO (measured, deferred), 1
CONDITIONAL GO**. This spec covers **only** the approved capability. Candidates
1–4 (speculation, prefetch, local tier, provider mirror) are deferred with
recorded negative results and `next_review = 2026-10-24`; nothing here
implements, prepares, or partially builds them.

**Baseline:** `4686 passed, 26 skipped, 0 failures`; all CI gates green.
**Module budget:** 11 of 14 used. **This capability adds ZERO new modules** — it
lives inside the existing server, so the three remaining slots stay reserved for
the deferred candidates.

---

## W3-C5 — Anthropic-compatible API handler (`POST /v1/messages`)

### 1. Problem
Olympus serves an OpenAI-compatible surface (`/v1/chat/completions`) but nothing
that speaks the **Anthropic Messages** protocol, so Anthropic-native clients
(Claude Code, the `anthropic` SDK) cannot drive the council. Colibri solved the
same problem by translating into one shared generation path rather than forking
a second engine — the principle to absorb.

### 2. Current behaviour
`web.py:_handle_v1_chat` (routed at `do_POST`, line ~1828) handles
`/v1/chat/completions`, with admission refusal → 429, sovereignty checks,
streamguard-wrapped SSE via `_guarded_pieces`, usage accounting, and the
house error envelope. **No `/v1/messages` route exists.**

### 3. Proposed behaviour
A **translation layer**, not a second server: `/v1/messages` normalises the
Anthropic request into the *same* internal call the OpenAI path already makes,
and renders the result back in Anthropic shape. One generation path, two
dialects.

### 4. Invariants
- **W3-I1 single path.** The handler MUST route through the same council entry
  (`bot.ask` / `bot.ask_stream`) and the same admission, cancellation, usage,
  tool-safety, streamguard and sovereignty seams as `_handle_v1_chat`. No
  parallel server, no bypass, no duplicated policy.
- **W3-I2 loud refusal over silent divergence.** Any Anthropic field Olympus
  cannot honour (`stop_sequences`, `top_k`, non-text content blocks) is
  **refused with a typed error**, never silently ignored — Colibri's precedent
  and this programme's refusal-over-degradation rule (R6).
- **W3-I3 Anthropic semantics respected.** `max_tokens` is **required** (400 if
  absent); `system` accepts string *or* block list; stop reasons map to
  `end_turn` / `max_tokens` / `tool_use`; usage uses `input_tokens` /
  `output_tokens`.
- **W3-I4 native error envelope.** `{"type":"error","error":{"type":…,
  "message":…}}` with correct HTTP status — not the OpenAI envelope.
- **W3-I5 auth parity.** Accept `x-api-key` **and** `Authorization: Bearer`,
  both constant-time compared, reusing the existing auth check.
- **W3-I6 streaming fidelity.** Named SSE events in the required order:
  `message_start` → `content_block_start` → `content_block_delta`* →
  `content_block_stop` → `message_delta` → `message_stop`, with protocol `ping`
  keepalives. A streamguard trip must terminate the stream with a disclosure
  delta, never a silent truncation (W2-I8.3).
- **W3-I7 no compatibility claim beyond what was tested** (see §8).

### 5. Schema
Request (subset honoured): `model, max_tokens (required), messages[{role,
content}], system (str | blocks), stream, temperature, top_p, tools,
tool_choice`.
Response: `{id, type:"message", role:"assistant", content:[{type:"text",text}],
model, stop_reason, stop_sequence, usage:{input_tokens, output_tokens}}`.

### 6. Interfaces
`web.Handler._handle_v1_messages()` + a route line in `do_POST`; pure helpers
`_anthropic_to_internal(payload) -> (messages, system, opts)` and
`_internal_to_anthropic(reply, *, model, usage) -> dict`, plus
`_anthropic_sse(pieces, ...)` for the event sequence. Pure helpers are the
testable core; the handler is thin.

### 7. Security
Inherits every existing control (auth, Host guard, admission, sovereignty,
egress, tool approval). New surface area is parsing: the request body is
untrusted input, so it is size-bounded and structurally validated before use,
and unknown/unsupported fields are refused rather than coerced. No new
credential path, no new egress.

### 8. Testing & the compatibility claim
- Pure-helper tests for translation both ways, including block-form `system`,
  multi-turn, and tool blocks.
- **SDK-type verification:** `anthropic>=0.92.0` is a *required* dependency, so
  responses are validated by constructing real `anthropic.types` models from the
  handler's output (`Message.model_validate`), and streaming events are checked
  against the documented event names/order.
- Refusal tests for `stop_sequences`, `top_k`, non-text blocks, missing
  `max_tokens`.
- Parity tests: admission 429 shape, auth via both headers, streamguard
  disclosure on a tripping stream, cancellation.
- **Claim bound (W3-I7):** report as **SDK-type-verified**, *not*
  real-client-verified. No third-party client is driven over a socket in this
  environment, and the completion report must say so.

### 9. Failure behaviour
Malformed body → 400 with the Anthropic error envelope; unsupported field →
400 `invalid_request_error`; admission refusal → 429 with `Retry-After`;
sovereignty/egress refusal → the existing typed refusal, re-rendered in
Anthropic shape; internal error → 500 with the envelope and no stack trace.

### 10. Migration / rollback
Additive route; nothing existing changes. Rollback = revert (the OpenAI surface
is untouched). No new env knob is required; if one is added for enablement it
must be documented and registered like every other.

### 11. Acceptance (W3-A*)
| # | Gate | Threshold |
|---|---|---|
| W3-A1 | Single generation path | handler calls the same council entry + seams; no parallel server (source-asserted) |
| W3-A2 | Loud refusal | every unsupported field returns a typed 400, none ignored |
| W3-A3 | SDK-type verified | handler output validates as real `anthropic.types` models |
| W3-A4 | Streaming fidelity | event names + order correct; trip ⇒ disclosure, not truncation |
| W3-A5 | Parity | admission/auth/cancellation/usage behave as on `/v1/chat/completions` |
| W3-A6 | Suite + gates | full suite 0 failures; capabilities, threat-model, non-interference, env-docs green |
| W3-A7 | Honest claim | report states SDK-type-verified, not real-client-verified |

### 12. Explicit exclusions
No speculation, prefetch, local tier, or mirror work of any kind. No new module.
No `/v1/complete` (legacy Anthropic). No batch API. No files/vision blocks. No
compatibility claim that was not tested.
