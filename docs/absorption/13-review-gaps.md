# 13 — Review-Identified Gaps: Coverage Addendum

The adversarial review (recorded in `00-SYNTHESIS.md` §5) walked the full Colibri
inventory (`docs/colibri-deep-analysis.md`) against domains 01–12 and found seven
capabilities no file owned. House rule: nothing is skipped silently. Each gap gets
a compact rubric here — points 1–3 reference the analysis doc; depth goes to the
redesign. Owners are assigned into the consolidated architecture of
`00-SYNTHESIS.md` (no new modules beyond its budget).

| Gap | Colibri capability | Verdict | Olympus owner |
|---|---|---|---|
| G1 | DSA sparse attention ("lightning indexer", §6.2) | redesign | `ctxbudget` + `recall` |
| G2 | Sampling armor & degraded-substrate defaults (§6.4) | redesign | `streamguard` layer + `modelgrade` |
| G3 | o200k ahead-of-need tokenizer support (§26.9) | **skip with reason** | — (principle absorbed into `providers.py` doctrine) |
| G4 | Tokenizer fidelity / token-estimation calibration (§12) | redesign | `ctxbudget` calibration ratchet |
| G5 | Fuzz/property-testing discipline (§5.4, §22 absence) | new practice | test suite + CI |
| G6 | Silent no-op substrate detection (§26.11) | redesign | `doctor` + `usage` telemetry |
| G7 | Startup-only settings & config skew (§3.4) | redesign | `doctor` + `health` fingerprints |

---

## G1. DSA sparse attention → relevance-budgeted context selection

**1–3 (Colibri).** GLM-5.2's lightning indexer scores every cached position with a
cheap learned scorer and restricts attention to the top-`index_topk` when context
exceeds the threshold; below it the indexer is a provable no-op, so the dense path
validates token-exactly. "Shared" layers reuse the previous full layer's selection
(§6.2).

**4–5. Strengths / weaknesses.** Strength: sub-linear attention cost with a
*no-op-below-threshold* contract that keeps correctness testable. Weakness:
selection assumes full windows (`kv_start==0`, §27) and the scorer is fixed at
training time.

**6–9.** Security: none material at Colibri's layer; in Olympus, context
*selection* decides what the model sees — a manipulation surface (an injected
item that games the scorer gets itself selected). Scalability: this is exactly
the long-conversation problem Olympus has (histories beyond any window).
Performance: cheap scorer, big win. Maintainability: one scorer, one threshold.

**10–11. Olympus redesign.** When assembled context exceeds the `ctxbudget` plan
(domain 02), do not truncate-oldest: run a **relevance budgeter** — score prior
turns/memory items with the existing embedding index (`annindex.py`) plus recency
and heat (`ctxheat`), select top-k under the token budget, and **reuse the
selection across the steps of one Athena plan** (the shared-indexer-layer analog:
compute at plan time, reuse until the query materially shifts — an embedding-drift
test, not a step counter). Below budget the selector is a **no-op by
construction**, preserving 02's testability contract. Selection events are
recorded in the trace (which items were dropped and why) so Aletheia and the user
can audit what the council did not see. Injection-gaming is mitigated by the
domain-10 rule: only gated-store content is eligible for selection; ephemeral
web content never outranks durable memory by heat it earned in the same request.

**12. Why superior.** Colibri's indexer is frozen and window-bound; Olympus's
selector uses live per-user heat, is auditable per decision, feeds its drops into
the observability trace, and degrades to refusal (`ContextExceeded`, domain 02)
rather than silent truncation when even selection cannot fit the plan.

## G2. Sampling armor → degenerate-stream defense & measured defaults

**1–3 (Colibri).** One NaN logit once produced a silent unbroken stream of
token 0; the fix is finite-argmax fallback with a one-time warning (#369), a
heap-based partial nucleus (#335), and defaults deliberately tighter than the
model card because "the int4 tail is quantization noise" (§6.4).

**4–9.** The transferable insight: **defend the decode loop against degenerate
output, and tune defaults to the measured degradation of your substrate, not the
vendor's brochure.** For an API client the failure class is real and unowned:
providers emit empty-delta stalls, repetition loops, malformed tool JSON,
mid-stream encoding garbage. Silent acceptance corrupts transcripts and memory;
per-token client-side resampling is impossible (we don't see logits).

**10–11. Olympus redesign.** The gateway `streamguard` layer (domain 06,
consolidated in `00-SYNTHESIS.md`) gains **stream pathology detectors**:
repetition-loop detection (n-gram window over deltas), stall detection (no
progress against the watchdog's spend-vs-progress currency, domain 11),
non-decodable/garbage-run detection. On trip: abort the stream, log a typed
pathology record to `routing_outcomes`, and retry once on the failover member
(existing `llm.py` chain) with disclosure in the reply footer — never a silent
partial answer. **Measured defaults:** per-member sampling defaults (temperature
ceilings for weak/local members) live in `modelgrade` grade cards as *measured*
fields, replacing vibes; the Colibri principle "tighter than official because the
substrate is noisier" becomes "tighter than official where the grade card shows
elevated pathology rates." Unsupported provider params (penalties, top-k) are
refused loudly per house style — absorbed as-is from Colibri's non-features
doctrine.

**12. Why superior.** Colibri armors one process's sampler; Olympus armors every
provider stream at one choke point, turns pathology *rates* into routing evidence
(a rising rate is a model-decay detector, complementing the toolcall-repair
telemetry of domain 06), and never loses the incident — each trip is a ledger
record, not a stderr line.

## G3. o200k ahead-of-need support → skip, with reason

Colibri ships GPT-4o-family tokenizer support auto-detected before any model
needs it (§26.9). **Skip:** Olympus does not implement tokenizers; providers do.
The transferable principle — *build the seam before the second consumer arrives*
— is already Olympus law in `providers.py` (a new provider is a catalog data
change, not a code change) and is reaffirmed as doctrine in domain 12. Recording
this skip closes the review's house-style violation (no silent skips).

## G4. Tokenizer fidelity → estimator calibration owned, not spiked

**1–3 (Colibri).** The engine's tokenizer is an oracle-validated faithful replica
of `tokenizer.json` semantics (§12); every byte the engine budgets is counted with
the real vocabulary.

**5, 10–11.** Olympus's analog is the `len//4` token estimator — and *every*
budget design in domains 02 and 05 rides on it. The review correctly flagged that
leaving it as an unowned "2-day spike" makes the whole budget stack rest on an
unvalidated constant. **Decision: the calibration ratchet in `ctxbudget`
(domain 02 R6) is the owned fix and is promoted from open question to committed
design** — planned-vs-provider-reported token counts from `usage.record` feed
per-provider chars-per-token ratios persisted in `memory/ctx_calibration.json`;
the estimator converges against the invoice, per provider, with the initial
`len//4` only a cold-start prior. A hard mismatch beyond guardband triggers a
doctor warning, not silent drift.

**12.** Colibri needs a perfect tokenizer because it owns decoding; Olympus needs
a *convergent estimator* because it rents decoding — calibrating against the
provider's own count is strictly more honest than replicating any one vocabulary,
and it works for every provider at once.

## G5. Fuzz & property-testing discipline → adopted for hostile-input parsers

**1–3 (Colibri).** The rANS codec ships with truncation fuzz, 2,000-flip
corruption fuzz under ASAN, and integrity seals (§5.4) — while Colibri's own CI
notably lacks sanitizer/fuzz jobs (§22), a gap its docs admit.

**10–11. Olympus adoption.** Olympus has the same class of hostile-input parsers
and today the same gap: `toolcall_repair`'s recovery ladder, a2a/MCP envelope
parsing, `contracts` schema validation, webhook payloads, and the new `sessionlog`
record reader (domain 05). Adopt **property-based tests** (`hypothesis`, as a dev
extra — the 3-dependency runtime footprint is untouched) plus **golden
malformation corpora** (domain 06 already specifies one for toolcall_repair;
extend the pattern to each parser above). CI gets one time-boxed property-test
job with committed seeds — bounded, deterministic in CI, exploratory locally.
The sessionlog reader inherits Colibri's seal doctrine directly: per-record
hashes, count-written-last, truncate-don't-repair on torn tails.

**12.** Colibri fuzzed its one binary codec; Olympus fuzzes every place untrusted
bytes become structure, with the corpus-as-regression-suite pattern making each
real-world malformation a permanent test — the incident-response-as-code culture
of domain 10 applied to parsing.

## G6. Silent no-op substrate detection → optimization liveness checks

**1–3 (Colibri).** The engine detects WSL/9p filesystems by statfs magic and warns
that `fadvise` is a silent no-op there (§26.11) — the principle: *detect
substrates where your optimization is provably inert, and say so.*

**10–11. Olympus redesign.** Olympus's inert-optimization risks: prompt caching
configured but the provider never reports `cache_read` tokens (cache_control as a
no-op — the exact analog); a sovereignty allowlist with no reachable local member;
learned-routing enabled with an outcome ledger too thin to alter any decision;
speculation enabled where acceptance telemetry shows it never fires. **`doctor`
gains a "liveness of optimizations" section**: for each enabled optimization it
checks the telemetry stream that would prove it alive (cache_read>0 within N
requests, ledger row counts, acceptance rates) and reports "configured but
provably inert on this substrate — here's why" with the knob to turn. Pure reads
of existing telemetry; no new module.

**12.** Colibri hardcodes one filesystem check; Olympus generalizes the principle
into a uniform *observed-effect* audit over every optimization it ships — which
also keeps future features honest, since a feature without a liveness signal is
flagged at design time by this very check.

## G7. Startup-only settings & config skew → restart-required detection

**1–3 (Colibri).** OpenMP reads env in a pre-main constructor, so the engine
seeds tuning vars and **re-execs itself once**; a side effect (process renamed
`exe`) broke naive process management (§3.4, §26.2).

**5, 10–11.** The transferable hazard: settings that only apply at process start,
silently stale on a long-running daemon. Olympus has it: gateway/heartbeat
daemons read `Settings.from_env` at boot; an operator edits config and nothing
changes. Rather than Colibri's re-exec magic (which created its own forensics
problem), **make skew visible and refuse silently-stale state**: the running
process exposes a **config fingerprint** (hash of resolved startup-scoped
settings) in `health`; `doctor` compares on-disk resolved config against the
running fingerprint and reports "restart required for: X, Y" — the config-skew
twin of the version-skew guard `selfupdate` already implements for upgrades.
Startup-scoped settings are *registered* as such in `config.py`, so the set is
enumerable, not folklore.

**12.** Colibri solved the problem invisibly and paid for it in process-forensics
complexity (`coli stop` scanning for a process named `exe`); Olympus solves it
*legibly* — no self-re-exec, no renamed processes, just a machine-checkable
statement of what is stale and why, consistent with refusal-over-silent-anything.
