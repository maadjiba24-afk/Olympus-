# Phase 5 Step 8 — External-Client Compatibility Report

**Status: EXECUTED.** 25/25 cases passed.
**Harness:** `scripts/client_compat_campaign.py` · **CI gate:**
`tests/test_phase5_client_compat.py` (24 tests)
**Provenance:** `real-client-staging`

---

## 1. The claim, stated exactly

> **Real-SDK-over-HTTP verified in staging.**
> **NOT** production-client verified.

Phase 4 could only say *SDK-type-verified*: response shapes validated against
the vendor SDK's Pydantic models, in-process, with no socket. `G2` in
`PRODUCTION_READINESS_REPORT.md` recorded real-client verification as blocked
on "no third-party client can drive a socket here."

**That was wrong, and recon caught it.** `anthropic==0.120.0` and
`openai==2.48.0` are installed, and loopback sockets work. The campaign
therefore ran for real.

### What is genuinely real

| Layer | Real? |
|---|---|
| TCP socket | ✅ real, loopback |
| HTTP request/response framing | ✅ real |
| SSE stream framing | ✅ real |
| Vendor SDK serialization, deserialization, retry, timeout | ✅ **the real `anthropic` and `openai` packages** |
| `olympus.web.Handler`, auth, admission, audit headers | ✅ real |
| Upstream model provider | ❌ **stubbed** |
| Third-party application, across a real network, from another host | ❌ **not attempted** |

### What this therefore does not say

Nothing about answer quality, latency, or cost — no model was called. Nothing
about behaviour across a real network (TLS, proxies, NAT, partial writes, real
packet loss). `G2` is **substantially discharged, not fully discharged**; the
residue is the network and the third-party application.

A CI test enforces the ceiling: `test_no_report_upgrades_the_compatibility_
claim` scans every document in `docs/absorption/` and fails on any *unhedged*
assertion of production-client verification. It distinguishes asserting from
mentioning (this report mentions the phrase repeatedly, in negation), and a
second test proves the detection actually fires on a bare claim rather than
passing vacuously.

---

## 2. Environment

| | |
|---|---|
| `anthropic` | 0.120.0 |
| `openai` | 2.48.0 |
| Python | 3.11.15 |
| Server | `olympus.web.Handler` on `ThreadingHTTPServer`, ephemeral loopback port |
| Upstream | `StubCouncil` replacing `orchestrator.Olympus` — the single seam where a real provider would be called |
| Keys | two distinct staging keys, for the isolation case |

---

## 3. Results — Anthropic dialect (`/v1/messages`), 16/16

| Case | Result | ms | Evidence |
|---|---|---|---|
| authenticated request | PASS | 113.6 | `Message` deserialized by the SDK; `stop_reason=end_turn` |
| bad key is rejected | PASS | 58.0 | SDK raised `AuthenticationError` (401) |
| streaming | PASS | 110.2 | 29 chars via `text_stream`; **`get_final_message()` validated by the SDK** |
| usage reporting | PASS | 56.5 | `usage.input_tokens=1`, `output_tokens=5` |
| system prompt | PASS | 59.2 | system content reached the council |
| multi-turn | PASS | 61.5 | 3-message history preserved |
| malformed: no `max_tokens` | PASS | 2.2 | 400 + Anthropic error envelope |
| unknown field refused | PASS | 1.4 | `top_k` → 400. **W3-A2: never silently ignored** |
| hostile scalar `Infinity` | PASS | 1.1 | 400, **connection intact** — Phase-4 B-F1 over a real socket |
| large payload 200 KiB | PASS | 66.1 | accepted and answered |
| oversize payload refused | PASS | 97.8 | 2 MB → `BadRequestError` at the 1 MB guard |
| client timeout | PASS | 305.9 | 0.25 s client timeout against a 2 s handler → `APITimeoutError` |
| disconnect mid-stream | PASS | 134.1 | stream abandoned after one chunk; **server healthy on the next request** |
| 8 concurrent requests | PASS | 646.5 | 8/8, no errors |
| audit headers | PASS | 1.8 | `X-Olympus-Audit`, `X-Olympus-Run-Id`, `X-Olympus-Mode` |
| **principal isolation** | PASS | 131.1 | two keys → two distinct principals, neither containing the key |

## 4. Results — OpenAI dialect (`/v1/chat/completions`), 9/9

| Case | Result | ms | Evidence |
|---|---|---|---|
| authenticated request | PASS | 525.6 | `finish_reason=stop` |
| bad key is rejected | PASS | 60.8 | `AuthenticationError` (401) |
| streaming | PASS | 68.1 | 29 chars across deltas |
| models list | PASS | 57.4 | `/v1/models` → `olympus-council` |
| usage reporting | PASS | 58.3 | `prompt_tokens=1`, `completion_tokens=5` |
| malformed body | PASS | 1.5 | unparseable JSON → 400 |
| unknown field tolerated | PASS | 56.4 | accepted — see §5 |
| 8 concurrent requests | PASS | 403.1 | 8/8 |
| client timeout | PASS | 307.8 | `APITimeoutError` |

---

## 5. Findings

**F-C1 (recorded, not a defect).** The two dialects treat unknown fields
differently: Anthropic **refuses** `top_k` with a 400, OpenAI **accepts** an
unknown field. This is deliberate — `W3-A2` requires loud refusal of
unsupported *Anthropic* fields, and the OpenAI surface has always been the more
permissive of the two. Recorded so the asymmetry is a decision on record rather
than an accident, and pinned by a campaign case so a future change to either
side has to argue with a test.

**F-C2 (observation).** `mode=live` in the audit-header case, because the
campaign runs with shadow mode off. A separate test
(`test_shadow_mode_is_visible_to_a_real_client`) drives the same endpoint with
`OLYMPUS_SHADOW_MODE` on and off, asserting the header reads `shadow` and
`live` respectively — proving the marker is visible at the HTTP layer a real
client actually sees, not only in an internal object.

**No defects were found.** Every Phase-4 fix that touches this surface — B-F1
(`Infinity`), B-F2 (per-key principals), W3-A2 (loud refusal) — held over a
real socket driven by a real SDK.

---

## 6. Coverage against Step 8's required list

| Required | Anthropic | OpenAI |
|---|---|---|
| authentication | ✅ both directions | ✅ both directions |
| standard response | ✅ | ✅ |
| streaming | ✅ + SDK-validated final message | ✅ |
| tool use | ⚠️ **NOT COVERED** — see below |
| malformed requests | ✅ | ✅ |
| unknown fields | ✅ refused | ✅ tolerated (F-C1) |
| large payloads | ✅ 200 KiB accepted, 2 MB refused | — |
| cancellation | ✅ via disconnect | — |
| disconnects | ✅ | — |
| timeouts | ✅ | ✅ |
| max token limits | ✅ missing/invalid refused | — |
| error schemas | ✅ Anthropic envelope | ✅ |
| concurrent requests | ✅ 8/8 | ✅ 8/8 |
| principal isolation | ✅ over the wire | — |

**Tool use is not covered, and that is an honest gap.** Exercising a
client-driven tool round-trip requires the *council* to emit a tool call, which
requires a real model. The stub cannot fabricate one without the test grading
its own fixture — the self-confirming pattern Step 6 forbids. Tool-use
compatibility therefore remains **UNTESTED at the client layer** and is carried
forward as a blocking item for the provider-credentialed campaign.

---

## 7. What must still happen before a canary

| # | Gap | Needs |
|---|---|---|
| C1 | Tool-use round-trip through a real client | provider credentials |
| C2 | Real network: TLS, reverse proxy, NAT, partial writes | a deployed staging endpoint |
| C3 | A third-party *application* (not just the vendor SDK) | a deployed endpoint + a client app |
| C4 | Sustained concurrency beyond 8 | a deployment and a load source |

None of these is buildable here. None has been simulated and reported as
though it were done.
