# Wave 2 Completion Report — Colibri Absorption

**Branch:** `claude/colibri-deep-analysis-gpit35`
**Spec:** `docs/absorption/WAVE2_IMPLEMENTATION_SPEC.md`
**Entry gate:** `WAVE1_INDEPENDENT_AUDIT.md` §6 = PASS.
**Suite:** `4547 passed, 26 skipped, 0 failures` (Wave-1 tip was 3999; **+548**
Wave-2 tests). Gates green: capabilities, threat-model (130 tools),
non-interference (exit 0), compileall, experiments-registry, env-docs.

---

## VERDICT: **NOT COMPLETE — capabilities implemented, integration outstanding**

All ten capabilities are built, tested, and reversible. **Wave 2 is not
finished**, because five acceptance gates speak about the *live* system and the
capabilities deliberately ship **unwired**:

> A capability is complete only when … **it integrates with the real Olympus
> execution path** — the governing operating rule.

Declaring Wave 2 done here would be exactly the "code exists ⇒ complete"
shortcut this program forbids. **Wave 3 must not start.** The remaining work is
the wiring wave (§4).

---

## 1. Capability status

| # | Capability | Module | Tests | Built | Wired |
|---|---|---|---|---|---|
| C1 | Measured model qualification | `modelgrade.py` (new) | 51 | ✅ | ❌ |
| C2 | Context & skill heat | `ctxheat.py` (new) | 86 | ✅ | ❌ |
| C3 | Routing substitution | `routesub.py` (new) | 45 | ✅ | ❌ |
| C4 | Experiments & quarantine registry | `experiments.py` (new) | 67 | ✅ | ➖ n/a |
| C5 | Persistent artifact ingestion gate | `ingestgate.py` (new) | 120 | ✅ | ❌ |
| C6 | Progress-based watchdog | `watchdog.py` (new) | 42 | ✅ | ❌ |
| C7 | Unified admission policy | `usage.py` + `web.py` | 28 | ✅ | ⚠️ partial |
| C8 | Degenerate-stream defense | `streamguard.py` (new) + `llm.py` | 42 | ✅ | ⚠️ partial |
| C9 | Optimization liveness | `doctor.py` | 61 | ✅ | ✅ |
| C10 | Configuration-skew diagnostics | `doctor/health/config` | (in 61) | ✅ | ✅ |

**Modules: 7 new** → 11 of the synthesis's 14 cap. Wave 3 has **zero headroom**.

---

## 2. Acceptance matrix (honest)

| # | Gate | Verdict | Evidence / why not |
|---|---|---|---|
| A1 | All routing decisions use one authoritative evidence store | **FAIL** | `modelgrade` exists and is tested, but routing still runs `learned_routing`/`bandit_routing`; nothing consumes the ladder yet. |
| A2 | No unqualified model selected for a protected task | **PARTIAL** | The rule is implemented and test-enforced (a manual `_CAPABILITIES` claim can never promote), but it is not enforced in the live selection path. |
| A3 | Heat changes benchmark-gated + reversible | **PASS** | `apply_pins` refuses without a passing injected gate (missing/failing/raising all refuse); shadow applies nothing; flag off ⇒ static placement. |
| A4 | Substitutions inside measured bands | **PARTIAL** | 14 preconditions each independently block, tested; not reachable from live routing. |
| A5 | Aletheia never below its verification floor | **PARTIAL** | Enforced in two independent places incl. the otherwise-fully-qualified case; unwired. |
| A6 | Every experimental feature registered | **PASS** | `check_registry()` clean; 21 entries; 11 flags mapped; expiry auto-disables. |
| A7 | All persistent artifacts pass the gate | **PARTIAL** | Gate + 12 kinds + 34-case corpus complete; not yet wired into skillpack/pluginstore/MCP. |
| A8 | Malformed artifacts cannot bypass validation | **PASS** | 2,080 seeded mutations: every accepted artifact is byte-identical to its canonicalization (never silently repaired). |
| A9 | Progress-free spend detected and stopped | **PASS (mechanism)** | model-text-only + spend ⇒ `PROGRESS_FREE_SPEND`; identical lease with real progress signals stays OK. Unwired, so no live enforcement. |
| A10 | Admission fairness under concurrency | **PASS** | Deterministic drain order `u0,u1,u2,u0,u1,u2` (pure FIFO would serve u0 twice); reserve structurally unreachable by best-effort. |
| A11 | No silent quality downgrade | **PASS** | Admission has no model/effort parameter (source-scanned); the orchestrator now **discloses** stream aborts instead of truncating silently. |
| A12 | Degenerate streams terminate safely | **PASS** | 10 detectors; `safe_tool_calls()` excludes the in-flight partial; typed failure, never a truncated answer. |
| A13 | Every enabled optimization has a liveness verdict | **PASS** | Configured-but-inactive ⇒ WARN INACTIVE, never OK; pure read (no network/model calls, proven). |
| A14 | Config skew observable and actionable | **PASS** | 8 skew classes; every finding names an operator action; no-mutation proof over environ + MEMORY_DIR. |
| A15 | Full suite + security gates pass | **PASS** | 4547 passed / 0 failures; all CI gates green. |
| A16 | Cost inside the global measurement budget | **PASS** | No new always-on cadence; nothing enabled by default; zero live spend added. |
| A17 | Rollback to Wave-1 behaviour tested | **PASS** | 6-test suite: defaults inert, no MEMORY_DIR residue, orchestrator free of Wave-2 policy, every flag has a deactivation trigger. |

**Score: 11 PASS · 5 PARTIAL/FAIL — all five blocked on the same missing step
(wiring).**

---

## 3. Defects found and fixed during Wave 2

**W2-I8.3 was not true end-to-end.** The streamguard PR honestly reported that
`orchestrator._synthesize_stream` wraps `llm.stream_text` in a broad
`except Exception` that keeps already-yielded text — so a pathological provider
stream would have reached the user as a normal, complete-looking reply. Fixed
(`b374ab5`): a typed `except streamguard.StreamPathology` now precedes the
generic handler, traces `synthesize.stream_aborted`, and appends a user-visible
notice marking the text unfinished and unverified. Four tests pin the handler
**ordering** (generic-first would restore the bug), the wording, the exception
hierarchy, and flag-off inertness.

**The env drift guard could not see its own drift.** The Wave-1 guard used a
static list, so a Wave-1 *audit* test — not the guard — caught `OLYMPUS_ADMISSION`
going undocumented. The guard now also **derives** the knob set by scanning the
absorption modules (`54bbcb0`). On its first run it surfaced **nine pre-existing
undocumented knobs** (`OLYMPUS_REPLAY`, `_FAST`, `_ABC`, `_INRUN_*`,
`_INTERACTIVE_VERIFY`, `_SEMANTIC_SKILLS`, `_LEARNED_ROUTING`, `_JOB_STALL_SECS`)
that absorption modules read for replay reproduction and drift fingerprinting —
documented rather than exempted, since changing one changes measured evidence.

---

## 4. Remaining work before Wave 2 can be called complete

Each item is a wiring PR touching a hot path, so each needs its own before/after
evidence and a flag-off replay proof:

| PR | Wire | Closes |
|---|---|---|
| W2-PR11 | `modelgrade` + `routesub` into the `choose(members, specialist, heuristic_pick)` seam (shadow first, then on) | A1, A2, A4, A5 |
| W2-PR12 | `ingestgate` into `skillpack` / `pluginstore` / MCP payloads / memory imports | A7 |
| W2-PR13 | `watchdog` leases into `orchestrator._run_one`, `heartbeat.tick`, and admission-slot release | A9 (live) |
| W2-PR14 | `streamguard` into `openai_compat` / `web.py` streaming (event-order detectors are currently unreachable — the Anthropic SDK's `text_stream` yields text only) | A12 (full) |
| W2-PR15 | `ctxheat` into `recall.context_block` behind its benchmark gate | A3 (live) |

---

## 5. Accepted debt & limitations (all registered in `experiments.json`)

- **PROVISIONAL constants ship uncalibrated** in `ctxheat` (8) and `watchdog`
  (11) — labelled, env-exposed, stamped into forensics, and calibrated in
  Phase-4 reliability validation. They ship only in shadow/off modes.
- **`routesub` PROVISIONAL** band/floor/confidence constants; calibration input
  named (its own `agreement_stats` estimated-vs-actual columns).
- **`streamguard` event-order detectors are unreachable** from the Anthropic
  streaming seam until W2-PR14.
- **`config.DEPRECATED` ships empty** — no knob has genuinely been retired;
  declaring a fake one would be the folklore W2-I10.2 forbids.
- **Skew checks are WARN, never FAIL** — `is_ready()` must keep meaning "can
  serve"; refusing to start over an operator's stated configuration would itself
  override operator intent.
- **Admission state is per-process** policy in front of the unchanged
  machine-global proclock mechanic.

---

## 6. Blockers

**None technical.** The single gating item is the integration wave (§4), which
is scheduled work, not a defect. No capability was forced through; no negative
result was discarded; nothing is quarantined-and-hidden — the registry carries
all 21 entries including every Wave-1 negative.

**Wave 3 remains closed** until §4 lands and A1/A2/A4/A5/A7 flip to PASS on the
live path.
