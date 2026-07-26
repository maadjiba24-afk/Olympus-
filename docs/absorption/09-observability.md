# Absorption 09 — Observability & Live Visualization

**Colibri domain:** `PROF` with plain-language verdicts naming the knob (§18), additive-only /
byte-identical-when-off instrumentation and the DISK-CLASS private-clock isolation doctrine
(§18, §26.5), the `EMAP`/`HITS` dashboard telemetry protocol and the web "Brain" cortex view
(§18 `telemetry.h`, §14.2), the Profiling view's phase economics (§14.3), the efficiency
regression harness — printf-anchored regex parsers, CI floors, advisory optimization dossiers
(§19.4) — and the telemetry-driven community performance-report protocol (§20).
**Olympus target:** council observability — `olympus/trace.py` (the signed decision log and
span events), `olympus/otel.py` (content-safe OTLP export), `olympus/metrics.py`,
`olympus/dashboard.py`, `olympus/health.py`, `olympus/doctor.py`, `olympus/liveeval.py`,
`olympus/tui.py` (`/progress`, `/reasoning`), `olympus/usage.py` (spend), `olympus/evolve.py`
(bounded self-tuning telemetry), `olympus/adminpanel.py`/`olympus/web.py`/`olympus/pwa.py`
(the operator surfaces), and the CI measurement culture (`quality_baseline.json`, drift-gated
capability counts).

## Domain thesis

Colibri's observability is built on one uncompromising contract: **watching the engine must
never change what the engine does** — `PROF` output is byte-identical-off by guarantee,
DISK-CLASS keeps a *private clone* of the recency clock so classification can't perturb the
LRU it observes, and every profiler ends not with a wall of numbers but with a **plain-language
verdict that names the exact knob to turn**. Olympus already has the superior *substrate* —
a signed, replayable, structured decision log (`trace.py` + `replaystore`) where Colibri has
printf lines — but it has almost none of the *derived* layer: no per-request phase economics
(where did the 40 seconds and the $0.31 go: routing, planning, specialists, verification,
tools?), no verdict engine, no live "watch the council think" view, and no CI floors on
orchestration metrics. The absorption move is therefore asymmetric: adopt Colibri's doctrines
(non-interference, verdicts-name-knobs, floors-vs-advisory separation, structured perf intake)
while replacing its mechanisms with ones the trace substrate makes strictly stronger — in
particular, Olympus can *mechanically prove* non-interference per commit by replaying a fixture
run with observability on and diffing decision paths (`trace.diff_decisions`), where Colibri
can only argue byte-identity by construction. Every derived artifact (phase-economics history,
council heat maps, floor baselines, perf-report corpus) is an accumulating asset in the
`MOAT_ANALYSIS.md` Asset 1/3 sense: a time series of measured behavior on this deployment's
real workload, priced in the two currencies Colibri never had to track at once — latency *and*
dollars.

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| PROF plain-language verdicts naming the knob | latency ring p50/p90/p99, phase shares, P0 execution split, verdict → `RAM_GB`/`PIPE`/`CTX`… (§18) | **redesign** | **new `olympus/phases.py`** (verdict engine), `doctor.py`, `health.py`, CLI `olympus profile` |
| Additive-only / byte-identical-when-off instrumentation + DISK-CLASS private clocks | PROF unset ⇒ byte-identical stdout; cold/warm classifier runs on a private recency-clock clone (§18, §26.5) | **absorb-principle** | doctrine in `phases.py`/`councilmap.py` docstrings; replay-diff CI proof via `trace.py` + `replaystore`; `codegraph_gate.py` observer lint |
| EMAP/HITS Brain protocol & web cortex view | per-expert tier+heat byte, hits bitmask, flash-and-decay canvas, atlas-labeled tooltips (§18, §14.2) | **new-subsystem** | **new `olympus/councilmap.py`**, `web.py` `/api/council`, `pwa.py`, `tui.py` `/council`, `dashboard.py` |
| Profiling view phase economics | rolling PROF window, five fixed phases, stacked share bars, "overlapped service time" footnote (§14.3) | **redesign** | `olympus/phases.py` (data model), `web.py` `/profile` view, `adminpanel.py`, `tui.py` `/reasoning` |
| Efficiency regression harness | printf-anchored regexes shared with the fixture, CI floors on the tiny model, opt-in advisory dossier that never fails CI (§19.4, §20) | **redesign** | `liveeval.py` (floor scorers), `phases.py` (dossier), `replaystore`/`replaygate`, `scripts/` CI hook, `quality_baseline.json` pattern |
| Telemetry-driven performance-report intake | GitHub issue template requiring commit/hardware/env/medians/profile timings; every benchmark row links to an issue (§20) | **absorb-principle** | `support.py`, `github.py`, CLI `olympus report --perf`, `.github/ISSUE_TEMPLATE/perf-report.yml` |

Only **two new `olympus/*.py` modules** are proposed. ROADMAP §0 (F1/F16: "~230 modules with
one maintainer is debt, not inventory") is treated as binding; everything else extends files
that exist today.

---

## O1. PROF with plain-language verdicts naming the knob (§18)

**1. What Colibri does.** `PROF=1` prints a startup config header, a per-forward latency ring
(p50/p90/p99/max), expert-I/O GB and MB/token, pin/LRU hit split, read-service vs *felt* wait,
phase time shares, a P0 execution split, and — the crown — a **plain-language verdict naming
the knob to turn**: I/O-bound → `RAM_GB`/`PIPE`/`DIRECT`; compute-bound → cores/`IDOT`/GPU;
attention-bound → `CTX`/`DSA` (§18).

**2. Why it exists.** Colibri has ~130 env vars (§25). A raw profile is useless to the
25-GB-laptop user; the verdict compresses the tuning cookbook (`docs/tuning.md`) into the one
sentence the user needs, and every knob it names is a real `getenv()` site.

**3. How it works internally.** Counters live in a profiling block inside `Model` (§3.3),
accumulated at phase boundaries in the decode loop; classification is threshold rules over
phase shares; output is printf lines at end-of-run, additive-only.

**4. Strengths.** Diagnosis and remedy in one artifact; the verdict vocabulary is closed over
the actual configuration surface (no advice you can't act on); thresholds encode measured
lore (e.g. `DRAFT=0` when compute-bound per #389 — §17.2 shows the same rules feed `coli
plan`'s auto-tune, so the *diagnostic* and the *planner* share one brain).

**5. Weaknesses & trade-offs.** (a) Time is the only currency — Colibri never spends dollars,
so its verdicts can't say "your bottleneck is *spend*, not latency," which for an API client is
half the story. (b) Verdict rules are hardcoded thresholds; there is no record of whether
turning the named knob actually helped — the loop is open. (c) Per-process, per-session: no
history, no fleet view, no percentile drift over weeks. (d) The verdict prints at exit; a
40-minute cold prefill gives you the diagnosis only after you've suffered it.

**6. Security implications.** Colibri's PROF lines are hardware/timing facts — low
sensitivity. Olympus's equivalent touches user-adjacent data: phase economics is derived from
the decision log, which contains rationale text and user meta. The verdict layer must obey
`otel.py`'s content-safety rule — structure, timings, costs, hashes, agent names; **never**
rationale or input text — because verdicts surface on operator dashboards and in exported
reports (O6).

**7. Scalability implications.** Colibri profiles one engine process. Olympus runs
multi-process (CLI, web, heartbeat all flush to the shared daily trace files — `trace.flush`
already serializes via `proclock`). Deriving economics from the trace files (not in-process
counters like `metrics.py`) makes the profile automatically fleet-wide and multi-user, and it
scales with `liveeval.py`'s existing bounded-scan discipline (`_MAX_FILES=7`,
`_MAX_SAMPLE=50`).

**8. Performance implications.** Zero new hot-path cost: every input the verdict engine needs
is **already recorded** — `Trace.span` timestamps per stage (route/plan/specialist/verify/
synthesize), per-decision `cost` and `duration_ms`, `usage.py` token accounting. Colibri had
to add guarded counters to a nanosecond-scale loop; Olympus's "loop" is seconds-scale API
calls and the record already exists. Analysis is offline and pure.

**9. Maintainability implications.** One rules table in one module, unit-testable as pure
functions over run dicts (the `liveeval.py` Scorer pattern). The knob vocabulary must be
drift-gated: every knob a verdict names must be a real `OLYMPUS_*` env var or CLI command —
the same discipline as the CI-verified capability counts in `README.md` and Colibri's "if a
knob isn't at a `getenv()` site, it isn't real" (§24).

**10. How Olympus should redesign it.** Build a **two-currency verdict engine** over the trace
substrate. Phase economics (O4) produces, per run: wall seconds, API cost, and tokens per
phase, plus parallelism efficiency. Verdict rules classify the dominant regime and name the
knob:

- *verification-bound* (verify share > 40% wall or cost) → `OLYMPUS_CONSENSUS` quorum size /
  Aletheia sampling rate;
- *planning-bound* → `OLYMPUS_FAST=auto`, direct-answer routing;
- *serial-chain-bound* (critical path ≈ wall, parallelism efficiency ≈ 1 with deep chains) →
  Athena plan-shape guidance;
- *history-bound* (input tokens dominated by conversation history) →
  `OLYMPUS_HISTORY_TOKEN_BUDGET` / `OLYMPUS_ACE`;
- *spend-bound* (cost concentrated in one specialist × model) → `modelpin` / pool tier;
- *tool-bound* (tool wall > model wall) → tool timeouts / `toolselect`;
- *backpressure-bound* (semaphore wait in `usage.py`) → concurrency cap;
- *budget-throttled* → `OLYMPUS_RUN_BUDGET_USD` / daily budget.

Close Colibri's open loop (**beyond Colibri**): when a verdict names a knob and the operator
turns it, the next window's economics are compared and the (verdict, knob, delta) triple is
recorded via `evolve.log_event("phases", ...)` — verdict rules accrue a track record, and a
rule whose advice measurably doesn't help gets flagged in its own dossier. This is the
Calibration-Record shape applied to the diagnostics themselves.

**11. Final Olympus architecture.**
- **New module `olympus/phases.py`** — pure core: `phase_breakdown(run: dict) -> PhaseEcon`
  (dataclass: per-phase `{wall_secs, api_cost, tokens_in, tokens_out, calls}`, plus
  `wall_total`, `sum_branch_secs`, `parallel_efficiency`, `critical_path`), computed from
  `run["events"]` span pairs and `run["decisions"]`; `verdict(econ, window) -> Verdict`
  (dataclass: `regime`, `evidence`, `knob`, `expected_effect`) with the rules table
  `_KNOB_RULES` as data; `window(runs) -> WindowEcon` with p50/p90/p99 per phase (Colibri's
  latency ring, computed over the last N runs from `liveeval.recent_runs`).
- **CLI:** `olympus profile [run_id] [--window N] [--json]` — one run's economics + the
  window + the verdict; wired into `cli.py` beside `olympus dashboard`.
- **Integration:** `doctor.py` gains an optional `_performance_checks()` (WARN with the
  verdict text when a window regime persists — doctor stays offline: it reads trace files
  only); `health.py` gains a `performance` component reporting `ok`/`degraded` with the
  current regime string; `dashboard.py.summary()` gains `last_verdict`; `tui.py`
  `/reasoning` appends the run's one-line economics + verdict.
- **Env:** `OLYMPUS_PROFILE_WINDOW` (default 20, clamped like liveeval's sample),
  `OLYMPUS_VERDICT=off` to silence verdict lines in interactive surfaces (analysis still
  available on demand). No flag is needed to *collect* anything — collection already exists.

**12. Why the Olympus approach is superior.** Colibri's verdict reads a process's counters and
dies with the process; Olympus's reads a signed, durable, fleet-wide corpus, so the same
engine yields per-run diagnosis, week-scale drift, and per-user/per-model splits for free. It
prices **both** currencies (Colibri: time only). Its knob vocabulary is drift-gated by the
same CI culture that gates capability counts. And it closes the loop: verdicts are themselves
measured, which no inference engine bothered to do.

---

## O2. Provably non-interfering instrumentation: additive-only + DISK-CLASS private clocks (§18, §26.5)

*Grouped honestly: byte-identical-when-off and private-clock isolation are two halves of one
doctrine — "the observer must not perturb the observed" — and Olympus should absorb them as
one contract with one enforcement mechanism.*

**1. What Colibri does.** With `PROF` unset, engine output is **byte-identical** to an
uninstrumented run (additive-only guarantee). DISK-CLASS classifies every expert load
cold/warm against a **private clone** of the recency clock, deliberately isolated so the
byte-identity claim is "provable by construction" (§18, §26.5): classification never reads or
advances the real LRU clock that placement decisions use.

**2. Why it exists.** Colibri's fidelity doctrine (§1: "placement only ever decides speed,
never answers") extends to observation: a profiler that touched the LRU clock would change
eviction order, which changes I/O, which changes timing-sensitive paths — and would silently
invalidate every A/B in the repo. The token-exact oracle culture demands that instrumentation
be outside the causal path.

**3. How it works internally.** Profiling counters are written only under the flag;
classification state is a structurally separate copy updated by the observer alone; the
guarantee is enforced by code review + the design ("no shared mutable state"), not by a test.

**4. Strengths.** Makes every measurement trustworthy by default; lets profiling stay on in
production benchmarks without asterisks; the private-clock trick is the rare case where
*duplicating state* is the correct engineering call.

**5. Weaknesses & trade-offs.** (a) "Provable by construction" is a *human* proof — nothing in
CI re-verifies it after the next contributor adds a counter in the wrong place. (b) Private
clones can drift from the real clock's semantics (a calibration burden, acknowledged in
comments). (c) Byte-identity of *stdout* is a weak observable for a system whose real output
is tokens — it works only because Colibri is deterministic under serial configs. (d) The
doctrine is implicit: no manifest says which modules are observers.

**6. Security implications.** For Olympus the doctrine has a second edge: an observer that can
*write* shared state is an escalation channel (a poisoned dashboard poll perturbing trust
scores or routing state). Making observers provably read-only over decision-relevant state is
a security property, not just a measurement one. It composes with the sovereign egress choke:
`otel.export_run` already funnels through `security.assert_egress_allowed` so "structure-only"
telemetry still can't leak past the allowlist — that is the egress half of the same contract.

**7. Scalability implications.** As surfaces multiply (dashboard, adminpanel, PWA, TUI, OTLP,
liveeval, and now councilmap + phases), the number of readers grows; without an enforced
observer contract, each new reader is a chance to touch the stores that feed auto-tuning
(`evolve.py` tunables, `routing_outcomes`, `trust`, memory recency). A declared observer set
keeps the reader population growable without re-auditing the world.

**8. Performance implications.** Olympus's core observability cost is already sunk (`trace.py`
records unconditionally; that is the audit spine, not optional profiling). The doctrine
therefore governs the *new* layers: derived analysis must be offline (O1), and any mid-run
emission (O3's live map) must be pull-activated — zero writes when nobody is watching,
exactly Colibri's `PROF=1` economics.

**9. Maintainability implications.** The contract must be executable, or it will rot the way
§4's item (a) predicts. Olympus has two enforcement tools Colibri lacks: a replay harness that
can diff decision paths (`trace.diff_decisions`, `replaystore`), and a code graph
(`codegraph_gate.py`) that can lint import edges.

**10. How Olympus should redesign it.** Absorb the doctrine; **upgrade the proof from
by-construction to by-machine**, two layers:

1. **Replay non-interference gate (CI).** A fixture run is recorded with all observability
   off. CI replays it (`OLYMPUS_REPLAY`) with everything on — `OLYMPUS_LIVE_EVAL=1`,
   `OLYMPUS_OTLP_ENDPOINT` pointed at a local sink, councilmap live, phases analysis
   invoked mid-run — and asserts `trace.diff_decisions(original, fresh) == []`. This is
   Colibri's byte-identity claim made *mechanical and per-commit*, on the observable that
   actually matters (the decision path, volatile fields excluded via `_VOLATILE`). Note
   `otel.py` already skips export under replay ("an export is a side effect, not part of the
   byte-identical decision path") — the gate generalizes that comment into a tested law.
2. **Observer lint (CI).** An explicit `OBSERVERS` manifest — `metrics`, `dashboard`,
   `health`, `liveeval`, `otel`, `adminpanel`, `phases`, `councilmap` — checked by a
   `codegraph_gate.py` rule: no import/call edge from an observer into mutating APIs of
   `store`, `memory`, `trust`, `evolve` tunables, `routing_outcomes`, or `usage` recording
   (each observer's own bounded namespace excepted, e.g. `metrics._C`). `liveeval.py`'s
   docstring promise ("it reads runs, it never changes them") becomes a lint, not a comment.
   This subsumes E1's effect-typing direction (ROADMAP): observers are the first
   effect-typed cohort — `read_only` — at zero research risk.

**Private clocks, translated:** where an Olympus observer needs warmth/recency classification
(O3's heat decay; "was this skill warm?"), it maintains **its own decayed counters derived
from trace records** — it never reads-and-touches the live recency metadata that the curator,
bandit routing, or memory ranking consume. Duplication is the feature.

**11. Final Olympus architecture.** No new module. (a) `tests/test_noninterference.py` — the
replay-diff gate over a checked-in fixture run (uses `replaystore` frozen responses; fully
offline, deterministic, cheap — satisfies ROADMAP's gate-cost rule F8). (b) The `OBSERVERS`
set + edge rules in `codegraph_gate.py` config. (c) A short doctrine section in
`docs/THREAT_MODEL.md` or a new `docs/OBSERVABILITY.md`: *additive-only; pull-activated
emission; private clocks for any recency classification; content-safe export; sovereign choke
on every egress.* (d) `phases.py` and `councilmap.py` are written under the contract from
birth.

**12. Why the Olympus approach is superior.** Colibri asserts non-interference by
architecture and hopes contributors preserve it; Olympus **proves it per commit** with a
replay diff and **prevents regressions structurally** with an import-graph lint. The doctrine
also does double duty as a security boundary (read-only observers can't be abused to steer
auto-tuning), which a single-user C engine never needed.

---

## O3. EMAP/HITS Brain protocol & the live "watch the council think" view (§18, §14.2)

**1. What Colibri does.** The engine emits `EMAP` (one byte per expert: 2-bit tier
disk/RAM/VRAM + 6-bit log₂ heat) and `HITS` (freshly-routed bitmask) lines; the web Brain view
polls `/experts` every 1.5 s and renders a 76×256 canvas — tier as base color, heat as
luminance, fresh hits flashing white and decaying ×0.94 per frame — with atlas-labeled hover
tooltips (§14.2). You literally watch the model think.

**2. Why it exists.** Trust and diagnosis: the storage hierarchy is the whole product, and the
Brain view makes placement quality *visible* (a cold pin profile looks dark; a hot one glows).
It is also the project's best demo (§16 reuses it, honestly, as a replay).

**3. How it works internally.** Counters updated during routing (FASE A, §6.3) are encoded
into a compact line by `telemetry.h`; the Python gateway caches the latest and serves it
authed; the React canvas does flash-decay client-side; feature detection degrades cleanly
against non-Colibri backends (§14.4).

**4. Strengths.** Astonishing information density (19,456 cells, one byte each); polling not
push (simple, resilient); tier+heat is exactly the mental model the tuning docs teach; the
atlas overlay (§19.1) turns a heat map into a *semantic* map.

**5. Weaknesses & trade-offs.** (a) Single-process: the view dies with the engine and shows
one session's heat. (b) No history — heat is a decaying now, never a record. (c) `/experts`
is authed but `/profile` isn't (§9.2, a noted inconsistency): telemetry endpoints grew
ad hoc. (d) The visualization is descriptive only — nothing connects a dark quadrant to a
named remedy (that lives in PROF, a different surface). (e) For Colibri the cells are
anonymous weights; privacy is free. Olympus's cells are *agents doing user work* — naive
porting would leak content.

**6. Security implications.** The council map must be **content-safe by schema**: agent
name/key, phase, tier, decayed activation heat, per-cell call counts and cost — the
`otel._SAFE_DECISION_ATTRS` philosophy applied to a live endpoint. Served authed by `web.py`
under the same operator-token posture as `/api/metrics` and `/api/admin` (`adminpanel.py`'s
"secrets never appear in the payload" rule). Colibri's authed-`/experts`-vs-open-`/profile`
inconsistency is the cautionary tale: **one auth posture for all telemetry endpoints**, stated
once. Sovereign mode is unaffected (local serve, no egress); any future push channel goes
through the egress choke.

**7. Scalability implications.** Olympus's grid is tiny (13 specialists + 3 pipeline stages ×
a handful of pool models) — the challenge is inverted: not rendering 19k cells, but making a
~16-cell view *worth watching*. The answer is Colibri's own trick at the right resolution:
tier = which price/locality tier served the cell (frontier / mid / cheap / **local-sovereign**
— the model-price-tier translation of VRAM/RAM/disk), heat = decayed activation across recent
runs, flash = live dispatch right now. Multi-process is handled the way `gateway.read_status()`
already handles it: a cross-process snapshot file.

**8. Performance implications.** Pull-activated emission (O2): in-process, `councilmap` keeps
a bounded ring updated at decision-record time (nanoseconds, in-memory, always on — same class
as `metrics.record_response`). The cross-process snapshot file is written only when a viewer
is live (a `viewers` touch-file freshened by the poll handler; no recent touch → no writes),
atomically under `proclock`. End-of-run flush piggybacks on `trace.flush`, which already does
serialized I/O. Zero cost unwatched — Colibri's `PROF=1` economics, kept honest by the O2
replay gate.

**9. Maintainability implications.** One snapshot schema (versioned, like `trace.SCHEMA_VERSION`)
consumed by three thin renderers (web/PWA poll, TUI, `dashboard.render`). No chart libraries
(Colibri hand-rolled SVG; Olympus renders an HTML grid / unicode blocks). The heat/decay
math lives once, in the module, tested pure.

**10. How Olympus should redesign it.** A **council activation map**: rows = Zeus, Athena,
Aletheia + the 13 specialists (from `specialists.SPECIALISTS`, never hardcoded — drift rule);
columns/cell-facets = tier of the model that served them + live phase. Cell state:
`{agent, phase|idle, tier, heat, hits_recent, cost_window, last_run_id}`. Heat decays with a
half-life over runs (private counters per O2 — never the trust/routing stores). Fresh
dispatch flashes (client-side decay, exactly the Brain's ×0.94 trick). Tooltip/detail = the
specialist's measured score from `olympus scores` and its recent verdict regime (O1) — the
**atlas overlay translated**: measured capability labels on the heat map, which absorption doc
04 builds as the Specialist Atlas. Beyond Colibri: each cell links to the underlying signed
runs (`/reasoning`-grade drill-down), because Olympus's telemetry is backed by an audit log,
not a decaying byte.

**11. Final Olympus architecture.**
- **New module `olympus/councilmap.py`** — `note_dispatch(agent, phase, model, tier)` /
  `note_done(agent, status, cost)` hooks called where `orchestrator` already records
  decisions; pure `snapshot() -> dict` (schema-versioned, content-safe); decay math;
  cross-process snapshot at `MEMORY_DIR/state/councilmap.json` (atomic tmp+rename — the
  `.coli_usage` lesson, §7.3); `render_text(snapshot)` for TUI/dashboard.
- **Surfaces:** `web.py` `GET /api/council` (operator-authed, polled ~2 s; PWA gets it free);
  `adminpanel.py` embeds it as a section; `tui.py` gains `/council` (one-shot text map) and
  `/progress verbose` shows live flashes during a run; `dashboard.py.render` appends the
  compact map.
- **Env:** `OLYMPUS_COUNCIL_MAP=off` kill switch (default on: in-memory cost is negligible
  and file writes are viewer-gated); `OLYMPUS_COUNCIL_HEAT_HALFLIFE` (runs, bounded, and
  registered as an `evolve.py` tunable within [5, 200] — self-tuning within guardrails, the
  native pattern).
- **Integration:** heartbeat runs appear on the same map (its runs flush to the same traces),
  answering "what is the autonomous loop doing right now" — a question `adminpanel.py` asks
  today in prose.

**12. Why the Olympus approach is superior.** Colibri's Brain shows anonymous weights in one
process with no memory; Olympus's map shows *named, measured agents* across every process,
each cell backed by signed runs you can open, scored capabilities you can read, and verdicts
that name knobs — observation, provenance, and remedy on one surface. And it is content-safe
and auth-consistent by design rather than by accretion.

---

## O4. Profiling-view phase economics (§14.3, §18)

**1. What Colibri does.** The Profiling view polls `/profile` (rolling 120-turn PROF window)
and renders five fixed phases (I/O wait, expert matmul, attention, LM head, other) as 100%
stacked share bars, stat tiles (tok/s, wall, tokens/forward, disk service "overlapped with
compute"), two hand-rolled SVG charts over the last 40 turns, and a detail table with a
footnote explaining why overlapped disk *service* time doesn't appear in the wall stack
(§14.3).

**2. Why it exists.** Phase shares are how a streaming-MoE user learns *what kind of machine
problem they have* — the same decomposition PROF's verdict consumes, rendered over time.

**3. How it works internally.** PROF phase counters per turn → gateway ring buffer →
polled JSON → client-side aggregation; the honest footnote exists because overlap makes
naive stacking lie.

**4. Strengths.** The fixed small phase vocabulary makes turns comparable; the
overlapped-time footnote is measurement honesty in UI form; window + last-turn side by side
catches "this turn was weird" vs "it's always like this."

**5. Weaknesses & trade-offs.** (a) Time-only again — no cost axis. (b) The window is
in-memory in the gateway: restart forgets everything. (c) Phases are engine-hardcoded; there
is no per-request breakdown you can attach to a *specific* complaint. (d) The view is
disconnected from the verdict (you diagnose visually, then go find PROF output elsewhere).

**6. Security implications.** Same as O1/O3: economics are structure and must stay so. One
addition: per-phase *token* counts are safe, but per-phase *content* previews are not — the
web `/profile` view must render numbers from `phases.py` output only, never excerpt
rationale. The endpoint sits behind the same operator auth as all telemetry (the §9.2
inconsistency, fixed by policy).

**7. Scalability implications.** Because Olympus derives economics from durable traces, the
"window" is whatever the query says — last 20 runs, last week, one user, one specialist, one
model — with liveeval's bounded-scan caps preventing pathological reads. Colibri's
120-turn RAM ring becomes a query over an archive that already exists.

**8. Performance implications.** Pure derivation (O1 point 8). The only new runtime work is
optional: recording semaphore-wait (backpressure) as a span in `usage.py`'s call path so
*queue wait* is a visible phase — Colibri's `x-colibri-queue-wait-ms` header made the same
call (§9.2). That is one `Trace.event` per model call: negligible, and covered by the O2
replay gate.

**9. Maintainability implications.** The phase vocabulary must be **closed and versioned**:
`route, plan, specialist:<name>, tools, verify, synthesize, gates, queue_wait, other` —
derived from the stage names `trace.py` already documents ("route, plan, specialist, verify,
review, synthesize"). A schema test pins the vocabulary so renderers and the verdict engine
can't drift apart (the printf-anchor lesson of O5, applied preemptively).

**10. How Olympus should redesign it.** One data model, four renderers. `phases.py` (O1) is
the single source: per-run `PhaseEcon` and windowed `WindowEcon`, each phase carrying
`{wall_secs, api_cost, tokens_in, tokens_out}`. Render **two stacked bars per scope — one for
time, one for dollars** (beyond Colibri: the wall-share and cost-share pictures routinely
disagree, e.g. verification is cheap-fast, one frontier specialist is slow-expensive; seeing
both is the point of a council profiler). Translate the overlap footnote exactly: with
parallel specialist dispatch, `sum(specialist wall) > run wall`, so the stack shows
critical-path time with a stat tile "specialist time overlapped by parallelism: Xs
(efficiency Y×)" — Olympus's disk-service-vs-felt-wait. The verdict (O1) renders *on* this
view, closing Colibri's diagnose-here-remedy-elsewhere gap.

**11. Final Olympus architecture.** `olympus/phases.py` (shared with O1 — grouped by design,
stated honestly: O1 is the verdict engine, O4 is the data model + views; one module).
Surfaces: `web.py` `/profile` section (operator-authed; PWA-installable for a phone-glance
"what is my instance spending time/money on"); `adminpanel.py` section; `olympus profile`
CLI (O1); `tui.py` `/reasoning` one-liner per run. Env: `OLYMPUS_PROFILE_WINDOW` (shared).
Data: no new stores — traces + `usage.py` ledger are the archive; a derived
`WindowEcon` cache may live in `MEMORY_DIR/state/` with mtime invalidation if scan cost ever
shows up (measured first — measurement-first culture applies to the profiler too).

**12. Why the Olympus approach is superior.** Durable window (survives restarts, queryable by
user/specialist/model/date) vs a RAM ring; two currencies vs one; per-run drill-down bound to
the *signed audit record* of that exact run vs anonymous aggregates; and the diagnosis
(verdict) lives on the same surface as the picture. The overlap-honesty footnote — the best
small idea in Colibri's UI — is preserved as parallelism efficiency.

---

## O5. Efficiency regression harness: anchored parsers, floors, advisory dossiers (§19.4, §20)

**1. What Colibri does.** `tools/efficiency.py` parses engine output with 25+ regexes
**anchored to exact printf strings shared with the CUDA benchmark fixture** (drift breaks the
parser, not the analysis); `test_inefficiency.py` gates CI with **floors on the tiny model**
(tok/s, phase sanity, determinism, CUDA-actually-used, CPU/CUDA teacher-forcing agreement
≥70%); `test_efficiency_report.py` is an opt-in real-model **"optimization dossier"** — 9
sections of advisory FLAG thresholds naming concrete knobs that **never fail CI**;
`diag_harness.py` runs a 5-phase qualification campaign; `bench_ux.sh` enforces median
discipline (§19.4).

**2. Why it exists.** Performance claims are the project's currency (§20: "everything on this
page is a measurement, not a promise"); the harness makes regressions loud in CI where they
are deterministic (tiny model) and *visible without being blocking* where they are noisy
(real hardware).

**3. How it works internally.** Fixture runs under CI → text parsed → hard assertions
(floors) vs FLAG lines (advisory); the tiny-oracle model keeps floor runs seconds-cheap and
deterministic; the shared-printf-anchor convention keeps parser and emitter co-versioned.

**4. Strengths.** The **floors/dossier split** is the masterstroke: deterministic invariants
gate; noisy real-world metrics advise. Floors encode *measured quality claims as regression
tests* (§23: int3-beats-int4 shipped as a test). The parser-drift-breaks-CI property means
silent decoupling is impossible.

**5. Weaknesses & trade-offs.** (a) Printf + regex is the weakest possible contract — it
works only because of the shared-anchor discipline; structured output would delete the whole
parser class. (b) Floors run on a tiny fixture, so they catch orchestration regressions, not
real-model quality drift (Colibri accepts this; the dossier covers the rest). (c) The dossier
is opt-in and manual — nobody is paged when a FLAG appears. (d) Nothing accumulates: each CI
run's numbers evaporate.

**6. Security implications.** Minor for Colibri. For Olympus: floor fixtures are recorded
runs containing frozen LLM responses (`replaystore`) — fixtures must be scrubbed/synthetic so
no user content lands in the repo; the dossier, if auto-filed as an issue (O6/`github.py`),
must pass the content-safety filter.

**7. Scalability implications.** Floors must stay cheap as the suite grows — ROADMAP's
gate-cost rule (F8: "the measurement substrate is the system's true bottleneck and is already
its flakiest component; nothing may be gated 'for free'") is binding. Replay-based floors
cost zero API dollars and run in seconds; that is the only floor class allowed to gate.

**8. Performance implications.** The harness itself is offline. Its *output* protects
performance: today an orchestration regression (e.g. a change that doubles decisions per run,
serializes a parallel level, or bloats history tokens) ships silently because `pytest` tests
logic and `olympus eval` tests answer quality — nothing tests *economics*.

**9. Maintainability implications.** Olympus starts ahead: the "printf anchor" is already a
versioned structured schema (`trace.SCHEMA_VERSION`, JSONL records) — the parser class
Colibri had to discipline simply doesn't exist. The absorbed principle is the residue:
**the harness must consume the exact artifact production emits, from one source of truth**
— floors read real flushed run records through `phases.py`/`liveeval.py`, never a parallel
"test-only" summary that can drift.

**10. How Olympus should redesign it.** Three layers:

1. **CI floors (gate, deterministic, $0).** Extend `liveeval.py` with floor scorers and run
   them in CI over **replayed fixture runs** (via `replaystore` + `OLYMPUS_REPLAY` — the
   tiny-oracle translation: frozen responses make the pipeline deterministic and free):
   decision-path replay identity (`diff_decisions == []` — subsumes O2's gate), decision
   count ceiling per fixture, phase-vocabulary sanity (every event stage known), parallel
   levels actually parallel (`canonicalize_parallel_since` invoked where the plan says >1
   worker), orchestration overhead floor (non-model wall per decision below a bound —
   Python-side, deterministic enough with margin), history-token budget respected, and
   verdict-engine determinism (same fixture → same verdict). Wired as `pytest` tests +
   `scripts/` CI step; floor values live beside `quality_baseline.json` as
   `perf_baseline.json` **with `_provenance` history** — floors are ratcheted by recorded
   evidence, the house pattern.
2. **Advisory dossier (never gates).** `olympus profile --dossier`: sections over the live
   window — regime distribution, p50/p90 wall + cost per phase, parallelism efficiency,
   verification overhead ratio, spend per specialist×model, liveeval pass-rate trend,
   backpressure/queue-wait, budget-throttle events — each with FLAG thresholds **naming the
   knob** (O1's rules table reused; one brain, like PROF and `coli plan`). Heartbeat runs it
   on the `OLYMPUS_TRAIN_EVERY`-style cadence and, beyond Colibri, **pages someone**: FLAGs
   land in `evolve.record("phases", DEGRADED, ...)` and the operator push channels
   (`_notifications()` targets), so a dossier finding is an event, not a file.
3. **Accumulation (beyond Colibri).** Every dossier snapshot appends to
   `MEMORY_DIR/perf/dossiers.jsonl` (bounded) — the phase-economics *time series* becomes
   part of the deployment's Calibration Record substrate (MOAT Asset 1): "verification
   overhead on this workload, by month, by model" is exactly the un-backfillable evidence
   class.

**11. Final Olympus architecture.** No new module: floor scorers in `liveeval.py`
(same `Scorer` signature; pure); dossier builder in `phases.py`; fixtures under
`tests/fixtures/replay/` (scrubbed); `perf_baseline.json` + provenance; CI job in the
existing workflow; heartbeat hook beside the existing liveeval entry (`liveeval.run`). Env:
`OLYMPUS_PERF_FLOORS=strict|advisory` for local runs (CI pins strict), dossier cadence
`OLYMPUS_DOSSIER_EVERY` (heartbeat-read).

**12. Why the Olympus approach is superior.** Structured schema replaces printf-regex (the
drift class is deleted, not disciplined); replayed fixtures make floors deterministic *and*
API-free, satisfying F8 where Colibri needed a purpose-built tiny model; the floors/dossier
split is kept, but dossiers alert and **accumulate** instead of evaporating — turning
Colibri's best process idea into an Olympus moat-class asset.

---

## O6. Telemetry-driven performance-report intake (§20)

**1. What Colibri does.** A dedicated GitHub performance-report issue template requires
commit, hardware/storage, env, commands, warm-up policy, run count, medians, and profile
timings; every row in `docs/benchmarks.md` and every headline number links back to such an
issue (§1, §20). Community datapoints are the project's benchmark corpus.

**2. Why it exists.** A single maintainer cannot own 6×5090 rigs, M5 Maxes, and POWER8 boxes;
the template turns anecdotes into admissible evidence by forcing the metadata that makes a
number reproducible, and the issue link makes every published claim auditable.

**3. How it works internally.** Pure process: template + review norms + the culture of
linking `#NNN` provenance in code comments (§1, "issue numbers used pervasively as design
provenance").

**4. Strengths.** Evidence-or-it-didn't-happen as a community contract; the corpus *is* §20's
benchmark page; the template encodes methodology (medians, warm-up) so quality is front-loaded
into intake.

**5. Weaknesses & trade-offs.** (a) Manual: humans transcribe numbers, so transcription errors
and missing fields survive review. (b) Size-only integrity — nothing verifies a report's
numbers came from the claimed build/config. (c) Nothing machine-readable accumulates; the
benchmark page is hand-curated. (d) High-friction for the reporter (fill twelve fields).

**6. Security implications.** For Olympus this is the sharpest point in the domain: a perf
report from a real deployment risks leaking user content, hostnames, or key-shaped strings.
Intake must be **machine-generated and content-safe by construction**: only `phases.py`
structure, `otel.py`-safe fields, the capabilities manifest hash, and *redacted* env (knob
names + values for `OLYMPUS_*` performance knobs only, secrets never — the `secretref`/vault
posture). Sharing is explicit and opt-in, matching the `/contribute` consent model, and the
upload path goes through the sovereign egress choke like every other outbound call.

**7. Scalability implications.** As deployments grow, hand-curated intake collapses;
structured JSON bundles can be aggregated mechanically (a `docs/benchmarks.md` equivalent
generated from the corpus — drift-gated like capability counts, never hand-edited).

**8. Performance implications.** None at runtime; the bundle is assembled from existing
artifacts (window economics, dossier, doctor snapshot).

**9. Maintainability implications.** One bundle schema, versioned; the template becomes thin
("attach the bundle; describe what felt slow") because the machine fills what Colibri asked
humans to fill. Provenance culture transfers directly: Olympus already files Prometheus
upgrade proposals as GitHub issues (`GITHUB_TOKEN`/`GITHUB_REPO`, `github.py`) — perf reports
ride the same rail.

**10. How Olympus should redesign it.** `olympus report --perf [--window N] [--file out.json]`:
builds a signed, content-safe bundle — `{version, capabilities_manifest_hash, pool
composition (provider/model/host only, adminpanel rule), WindowEcon, current verdict, dossier
FLAGs, doctor summary, redacted knob dump, run count + medians}` — signed with the witness
key (`witness.sign_log` pattern) so a bundle is attributable and tamper-evident (fixes
Colibri's weakness (b): the numbers carry proof of origin). Destinations: local file
(default); operator's own tracker via `github.py` when configured; and — strictly opt-in,
`/contribute`-gated — the project's intake. `.github/ISSUE_TEMPLATE/perf-report.yml` asks for
the bundle attachment plus free-text symptoms. `support.py` gains the same path for
*complaint-attached* economics: a user's "this is slow" report can carry the machine's
account of where the time went — turning O4's data into first-response triage.

**11. Final Olympus architecture.** No new module: CLI subcommand in `cli.py`; bundle
builder in `phases.py` (`export_bundle(window, *, redact=True)`); signing via `witness`;
filing via `github.py`/`support.py`; the issue template file; consent via the existing
contribute flag. Env: none new beyond the existing `GITHUB_TOKEN`/`GITHUB_REPO`.

**12. Why the Olympus approach is superior.** Colibri asks humans to be rigorous; Olympus has
the machine assemble, redact, and **sign** the evidence, so intake is lower-friction, more
complete, privacy-safe, and verifiable. And because bundles are structured, the community
corpus is aggregatable — the benchmark page becomes generated truth in the same way the
README's capability counts already are.

---

## Open questions & research spikes

1. **Orchestration-overhead floor stability (spike, ≤2 days).** The replay-based floors are
   deterministic in decision path but not in wall time (Python + CI runner variance). Spike:
   run the fixture replay 50× in CI-like conditions, measure non-model wall-per-decision
   variance, and set the floor with the margin the data dictates — or demote that one floor
   to the dossier if variance exceeds ~3×. (Measurement-first: the floor itself needs a
   before/after.)
2. **Mid-run economics for long runs.** O1/O4 derive from *flushed* runs; a 10-minute run is
   invisible until it ends. Colibri's heartbeat stats and keepalive pump (§9.2) argue for a
   live partial view. The `_CURRENT` trace contextvar makes an in-flight snapshot possible —
   but it must stay read-only under the O2 contract. Decide whether `/api/council`'s flash
   feed (which already knows dispatch/done) is enough, before adding any in-flight economics
   endpoint.
3. **Queue-wait span placement.** Recording semaphore wait in `usage.py` adds one event per
   model call inside the recorded path. Confirm via the O2 replay gate that it is
   decision-path-invisible (it should be: events are not decisions), and that `_VOLATILE`
   handling needs no extension.
4. **Cross-deployment perf corpus governance.** If opt-in O6 bundles ever aggregate across
   deployments, that corpus brushes against Asset 2 (comparative evidence) and its
   counter-positioning claims — who hosts it, and does publishing per-model phase economics
   cross the line from "our deployment's record" to "public model benchmarking"? Needs a
   deliberate policy note in `MOAT_ANALYSIS.md` terms before any aggregation ships.
5. **Tension with absorption 04's telemetry.** Routing agreement telemetry (swap%/overlap/KL
   from `routesub.py`) and this domain's phase economics both want a home on the profiling
   surfaces and both append derived series under `MEMORY_DIR`. The synthesizer should mandate
   one derived-telemetry substrate convention (bounded JSONL under `MEMORY_DIR/perf/`,
   schema-versioned, observer-contract-bound) rather than two.
6. **Module-count budget.** This domain proposes two new modules (`phases.py`,
   `councilmap.py`); other absorption docs propose their own. Against F1/F16, the synthesizer
   holds the global budget — if consolidation is needed, `councilmap.py` can fold into
   `phases.py` (both are observers over the same substrate) at some cohesion cost; `phases.py`
   itself must not fold into `liveeval.py`, whose purity contract ("reads runs, never changes
   them, no clock") is worth keeping minimal.
