# Wave 3 Completion Report — Colibri Absorption

**Branch:** `claude/colibri-deep-analysis-gpit35`
**Spec:** `WAVE3_IMPLEMENTATION_SPEC.md` · **Gate:** `WAVE3_EVIDENCE_REVIEW.md`
**Suite:** `4735 passed, 26 skipped, 0 failures` (Wave-2 tip was 4686; **+49**).
Gates green: capabilities, threat-model (130 tools), non-interference (exit 0),
compileall, experiments-registry, env-docs.

---

## VERDICT: **COMPLETE AS SCOPED — 1 of 5 candidates implemented, 4 deferred on measured evidence**

Wave 3 was never a list to build; it was five candidates each requiring its own
evidence gate. The gate was run as code. **Four failed and were deferred with
their measurements recorded; one passed and was built.** That is the wave
completing correctly, not partially.

---

## 1. Candidate outcomes

| # | Candidate | Verdict | Evidence |
|---|---|---|---|
| 1 | Lossless council speculation | **DEFERRED** | `modelgrade` cards = 0 (store empty, flag off) — neither a draft nor a verify model is qualified for any cell; no acceptance data exists |
| 2 | Coupling-driven local pre-work | **DEFERRED** | `predictability_report(days=30)` → `n_runs=0`, `insufficient_data`, `floors.pass=False`; floor is recall@2 ≥ 0.6, CI ≤ 0.1, **n ≥ 200** |
| 3 | Local inference qualification | **DEFERRED** | 0 qualification cards; no campaign run; no local runtime present |
| 4 | Provider mirror routing | **DEFERRED** | `routesub.agreement_stats` → `decisions=0`; no provider-unavailability rate measured |
| 5 | Anthropic-compatible API handler | **IMPLEMENTED** | 49 tests; SDK-type-verified against the real `anthropic` models |

**No floor was lowered to admit a candidate. No candidate was partially built
"to be ready".** Deferred means untouched.

## 2. Wave-3 acceptance gates (candidate 5)

| # | Gate | Verdict | Evidence |
|---|---|---|---|
| W3-A1 | Single generation path, no parallel server | **PASS** | Handler calls `orchestrator.ask/ask_stream` via shared `_v1_run`; source-asserted exactly one `HTTPServer` construction in `web.py` |
| W3-A2 | Loud refusal of unsupported fields | **PASS** | 400 + Anthropic envelope for `stop_sequences`, `top_k`, missing/invalid `max_tokens`, non-text/tool blocks, bad roles/bodies — none silently ignored |
| W3-A3 | SDK-type verified | **PASS (bounded)** | `Message.model_validate` on the body; every stream frame validated against `RawMessageStart/ContentBlockStart/Delta/Stop/MessageDelta/MessageStop`; SSE `event:` name asserted equal to payload `type` |
| W3-A4 | Streaming fidelity | **PASS** | Full documented event order with pings; a streamguard trip yields a **disclosure delta** and the envelope still closes (W2-I8.3 preserved on the new surface) |
| W3-A5 | Parity with the OpenAI surface | **PASS (cancellation structural)** | Same auth, admission-429, sovereignty-403, usage accounting and audit headers — via *factored* shared helpers, not copies. Cancellation surfaces through the shared `_v1_run`; not separately exercised |
| W3-A6 | Suite + gates | **PASS** | 4735 passed / 0 failures; all CI gates green |
| W3-A7 | Honest claim | **PASS** | Reported as **SDK-type-verified, NOT real-client-verified** (below) |

## 3. The compatibility claim, stated exactly

**Verified here:** the wire contract deserializes into the *real* `anthropic`
SDK models — a required dependency, so this is the SDK's own schema judging the
output, not hand-written fixtures agreeing with themselves.

**Not verified here:** no third-party client (Claude Code, the SDK in another
process) drove the endpoint over a socket. Nothing in this environment can do
that.

**Therefore the claim is bounded:** *SDK-type-verified, not real-client-verified.*
The spec's rule — "do not claim compatibility until tested against
representative real clients" — is honoured by **not making** the broader claim,
and real-client testing is carried into Phase 4/5 as required work.

## 4. Honest limits of candidate 5 (all documented in code)

1. **`max_tokens` is enforced by truncation at the HTTP boundary** using the
   existing ~4 chars/token estimator, because the council takes no token cap.
   Reporting `stop_reason: "max_tokens"` while returning full text would be a
   silent divergence; truncating and saying so is the Anthropic semantic. The
   estimator is approximate — and its accuracy is exactly the re-declared I-C4
   limitation from the Wave-1 audit.
2. **`tool_use` as a stop reason is unreachable end-to-end.** The council
   exposes no structured tool calls at the HTTP boundary, so the mapping is
   implemented and pure-helper tested but never exercised over the wire.
3. **`tool_result` turns collapse** exactly as they do on the OpenAI surface —
   parity with the shared prompt seam, not a new limitation. Fixing it would
   have created Anthropic-only prompt behaviour, i.e. a second generation path.
4. **`tools`/`tool_choice`/`temperature`/`top_p`/`metadata` are accepted but not
   honoured** by the pipeline — the same posture the OpenAI surface documents.
5. **Behaviour change:** `x-api-key` is now also accepted on
   `/v1/chat/completions` — a superset, added at the single credential
   extraction point because W3-I5 forbids a second constant-time comparison.

## 5. Module budget

**11 of 14 used — unchanged.** Candidate 5 added zero modules, so all three
remaining slots stay reserved for the deferred candidates (`draftverify`,
`localtier`, `coalesce`). A fourth would require retiring one.

## 6. The structural finding carried forward

**Four of five Wave-3 gates depend on operational data that only Phase 5
(staging + shadow traffic) produces.** The programme order places Wave 3 before
shadow, but speculation needs `modelgrade` cards, prefetch needs ≥200 real
trajectories, local tier needs a qualification campaign, and mirror needs a
measured unavailability rate — all of which shadow generates and nothing else
does.

This is surfaced, not silently resolved. The recommendation is to **re-run the
evidence review after shadow traffic** (`next_review = 2026-10-24`, recorded in
`experiments.json`), not to weaken the floors. If the floors are ever lowered,
that must be a deliberate, argued decision — not a side effect of wanting the
features sooner.

## 7. Blockers

**None for Wave 3 as scoped.** The four deferrals are *evidence gaps by design*,
not defects: the gates worked, refused, and recorded why.

## 8. State entering Phase 4

- Waves 1–3 complete; suite **4735 passed / 0 failures**; all CI gates green.
- Every adaptive capability ships **off or shadow**; rollback to Wave-1
  behaviour is test-proven (W2-A17).
- Open work carried in: the A3 promotion signal; 19 PROVISIONAL constants
  awaiting Phase-4 calibration; the A2 fail-open revisit; real-client testing of
  `/v1/messages`; and the four deferred candidates pending shadow evidence.
- **Nothing has served real traffic.** Phase 4 (integration, security,
  reliability, performance/cost, privacy) is next, and Phase 5's shadow traffic
  is what finally makes the deferred candidates reviewable.
