# Absorption 11 — Operations, Planning & Reliability

**Colibri domain:** `resource_plan.py` auto-tune WITH REASONS — setdefault-only, user-env-wins,
bottleneck classification (§17.2); `coli doctor`'s read-only check matrix incl. linkage
forensics (§17.1); the setup self-test oracle (§17.3 `setup.sh`, §9.1 oracle modes);
`warmup.ps1` topic-diverse cache priming (§17.3); `supervisor.sh`'s zombie-killing babysitter
(§17.3); `coli stop`'s process forensics and the `exe`-rename ghost-engine incident (§13,
§26.2); OOM-refusal-over-silent-death (§7.4, #305/#403/#12); honest error surfacing (§4.1
#236 "Success"; §11.3 #307); attach-vs-private topology (§13, §26.12); RLIMIT raising (§13);
`engine_diag` OOM forensics (§13).
**Olympus target:** self-configuring reliable ops across `olympus/doctor.py`,
`olympus/health.py`, `olympus/firstrun.py`, `olympus/selfupdate.py`, `olympus/heartbeat.py`,
`olympus/hibernate.py`, `olympus/proclock.py`, `olympus/errors.py`, `olympus/gateway.py`,
`olympus/replaygate.py`, `olympus/usage.py`, plus two new modules proposed below.

## Domain thesis

Colibri's ops layer exists because a 744B model on a 25 GB laptop dies in a hundred
undramatic ways — silent OOM-kills, ghost `exe` processes, stalled downloads, a plan that
quietly ran a 128 GB box with a 16 GB box's cache — and the project answered every one of
them with the same three doctrines: **plan with reasons but never override the user**,
**refuse loudly rather than degrade silently**, and **prove readiness with an oracle, not a
vibe**. Olympus's failure modes are economic and orchestral rather than physical — a wedged
heartbeat job instead of a stalled `pread`, a burned daily budget instead of an OOM-kill, a
cold routing prior instead of a cold pin store — but the doctrines translate exactly, and
Olympus already owns better substrates than Colibri had (a cross-process lock layer in
`proclock.py`, a durable error ledger in `errors.py`, a replay store, an upgrade handoff
journal in `selfupdate.py`). What is missing is the *derived* layer: no environment planner,
no job babysitter (today one hung job stalls `heartbeat.tick()`'s entire serial loop), no
golden self-test at lifecycle transitions, no attach topology, and refusal doctrine applied
to money and context only patchily. Everything below is priced against ROADMAP §0's
small-team rule — **two** new modules total — and oriented so each mechanism feeds the
MOAT_ANALYSIS accumulators: plans, postmortems, and self-test outcomes are time-series
evidence about *this* deployment, which is Asset 1/Asset 3 material no lab can backfill.

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| Auto-tune with machine-readable reasons | `resource_plan.py`: hardware detect → tier plan → env emission with reasons, `setdefault` only, never sets affinity vars (§17.2, #325) | **redesign** | **new `olympus/opsplan.py`**, `config.py`, `doctor.py`, CLI `olympus plan` |
| Bottleneck classification | disk / mixed / compute / memory verdict in the plan (§17.2) | **redesign** | `opsplan.py` reading `usage.py`, `metrics.py`, `liveeval.py` |
| Doctor read-only check matrix + linkage forensics | pass/warn/fail/skip, JSON, schema-versioned; CUDA requested × detected × **linkage** matrix incl. marker-string scan (§17.1) | **redesign** | `doctor.py`, `health.py` |
| Setup self-test oracle | `setup.sh` ends in tiny-oracle self-test; `TF=1` 32/32 canonical gate (§17.3, §9.1) | **redesign** | `replaygate.py`, `firstrun.py`, CLI `olympus selftest` |
| Upgrade handoff hardening | (Olympus need; Colibri analog: `.build-config` stamp + release behavioral verification, §21) | **redesign** *(beyond Colibri)* | `selfupdate.py`, `heartbeat.py`, `replaygate.py` |
| Warm-up topic-diverse cache priming | `warmup.ps1`: 30 topic-diverse prompts; NGEN=32 because usage saves only on clean completion (§17.3) | **redesign** | `learned_routing.py`, `bandit_routing.py`, `toolselect.py`, CLI `olympus warmup` |
| Background-work babysitter with zombie detection | `supervisor.sh`: flock singleton, kills downloads stalled >180 s (§17.3) | **new-subsystem** | **new `olympus/watchdog.py`**, `heartbeat.py`, `scheduler.py`, `proclock.py` |
| Process lifecycle forensics (`coli stop` + `engine_diag`) | pidfile + `/proc`-scan kill of demonstrably-ours processes; OOM-kill forensics in the chat client (§13, §26.2) | **redesign** | `gateway.py`, `cli.py`, `errors.py`, `watchdog.py` |
| RLIMIT raising | `coli` raises RLIMIT_NOFILE to 65536 for 144+ shards (§13) | **absorb-principle** | `gateway.py` / `web.py` startup |
| OOM-refusal-over-silent-death | refuses to start when projected peak > physical RAM; `COLI_RAM_OVERCOMMIT` explicit override; RSS guard; CAP_RAISE (§7.4) | **absorb-principle** | `usage.py`, `orchestrator.py`, `config.py`, `memory.py` |
| Honest error surfacing | #236 "Success" fix, per-thread GetLastError preservation, fail-hard-for-data / fail-soft-for-accelerators (§3.1, §4.1, §11.3) | **absorb-principle** | `errors.py`, `backend.py`, doctrine text |
| Attach-vs-private topology | `coli chat` health-probes a running server in ms and attaches (4%→55% hit, ~10×) else spawns private (§13, §26.12) | **redesign** | `cli.py`, `web.py`, `gateway.py`, `openai_compat.py` |

---

## O1. Resource planner: auto-tune WITH REASONS + bottleneck classification (§17.2)

*(Two listed capabilities grouped honestly: in Colibri the bottleneck classification IS the
reason engine of the auto-tuner — they are one artifact, `coli plan`.)*

**1. What Colibri does.** `resource_plan.py` detects hardware with zero dependencies
(safetensors header parsing, per-OS "reclaimable without swapping" memory probes,
physical-core counting with declared ctypes signatures), builds a tier plan with honest slack
accounting, classifies the bottleneck (disk/mixed/compute/memory, projected hit rate), and
emits environment variables **with a written reason each** (`DRAFT=0` because compute-bound
per #389; `PIN_GB=all` when fully resident).

**2. Why it exists.** ~130 env vars (§25) is an expert-only surface; most users would run a
128 GB box with a 16 GB box's cache (#12) or pin decode to one core (#325) without it.

**3. How it works internally.** Applied via `setdefault` **only** — the user's environment
always wins — and it deliberately **never sets** OMP affinity vars, because the one time a
silent fallback guessed cores wrong the damage was 20× (#325, #471): variables whose wrong
value is catastrophic are excluded from auto-tuning by policy.

**4. Strengths.** Reasons make the plan auditable and teachable; setdefault makes it safe to
re-run forever; the exclusion list encodes learned humility; dependency-free detection means
the planner works before anything else does.

**5. Weaknesses & trade-offs.** The reasons are prose in emitted output — no machine-readable
schema, no link to the measurement that justified the rule, no record of which plans were
applied or whether they helped. The plan is a point-in-time snapshot: hardware rarely
changes, so that is fine for Colibri, but the rules themselves are hand-maintained issue
folklore (#389, #467) that can silently rot. And there is no rollback: a plan that hurts
stays until a human notices.

**6. Security implications.** Low for Colibri (env emission only). High for Olympus: a
component that *writes configuration* is a self-modification channel. If a planner may set
`OLYMPUS_MODEL` or concurrency caps, it must be firewalled from ever touching security posture
(`cmdguard` mode, `OLYMPUS_SOVEREIGN`, `OLYMPUS_EGRESS_ALLOWLIST`, approval gates) — the exact
generalization of Colibri's affinity-var exclusion: *the class of variables whose wrong value
is catastrophic is not auto-tunable.*

**7. Scalability implications.** Olympus's "hardware" is mostly economic (provider pool,
prices, rate limits, budgets) plus a little physical (cores → `OLYMPUS_MAX_CONCURRENT_CALLS`,
disk under `MEMORY_DIR`). Both are cheap to probe; the plan is O(config), not O(model).

**8. Performance implications.** The wins are real: a single-model install never told about
`OLYMPUS_MODELS` pool composition; a budget set far below actual spend capacity (the CAP_RAISE
inverse-failure, #12); history token budgets left at defaults on a 200K-context model;
`OLYMPUS_MAX_CONCURRENT_CALLS` defaulting low on a 32-core box running wide Athena graphs.

**9. Maintainability implications.** Rules must be data, not code: a table of
`(condition, variable, value, reason, evidence-ref)` entries is reviewable and testable one
row at a time. Anything else becomes a second config system.

**10. How Olympus should redesign it.** Build `olympus plan` as an **evidence-linked,
gate-measured planner**:

- **Detect:** pool composition (`config.ModelPool.from_env()`), live prices
  (`providers.fetch_pricing`), the last N days of the usage ledger (`usage.py`: spend rate,
  429/quota events, latency percentiles per role), eval scores (`liveeval.py`,
  `quality_baseline.json`), `os.cpu_count()` + free disk at `config.MEMORY_DIR`.
- **Classify the bottleneck** — the translation of disk/mixed/compute/memory:
  **spend-bound** (budget exhausts before the day does), **rate-limit-bound** (429s cluster on
  one provider → recommend key rotation / pool member), **latency-bound** (serial plan chains
  dominate wall time → raise concurrency, enable fast-path), **context-bound** (history
  compaction fires often → raise `OLYMPUS_HISTORY_TOKEN_BUDGET`), **quality-bound** (scores
  lag baseline → recommend a stronger `OLYMPUS_GATE_MODEL`/pool member — recommend only,
  never apply, since it costs money).
- **Emit** `PlanItem{var, value, reason, evidence: [ledger refs], class: apply|recommend|never}`
  as JSON (`--json`) and prose. **Apply** (`--apply`) writes only vars absent from both the
  environment and `~/.olympus/config.env` — the setdefault discipline is already native:
  `firstrun.load_env_file` applies saved values only `if key not in os.environ`, and
  `firstrun.show_config` already flags overrides. Applied values are written via
  `firstrun.save_env_value` with a `# planned: <reason> <ts>` comment line.
- **The never-list** (hard-coded, tested): `OLYMPUS_SOVEREIGN`, `OLYMPUS_EGRESS_*`, cmdguard
  mode, `OLYMPUS_GATE_CONFIRM`, budget *raises* beyond user-set values, any `*_KEY/TOKEN`.
  The planner may WARN about these through `doctor.py`; it may never set them.
- **Measurement gate (the part Colibri lacks):** an applied plan is journaled to
  `MEMORY_DIR/plans/plan-<ts>.json` with the pre-plan 7-day baseline (spend/day, p50 latency,
  eval score). The heartbeat's existing `feature_evolution` cadence re-reads it after a
  window; if the tracked metric regressed, the plan item is **auto-reverted and the rule
  demoted to recommend-only** — the `gate_prompt` culture applied to configuration.

**11. Final Olympus architecture.** New module **`olympus/opsplan.py`** (~250 lines: probe
functions, rule table, `plan() -> list[PlanItem]`, `apply(items)`, `review(now)`); CLI
`olympus plan [--json] [--apply]`; env `OLYMPUS_PLAN_AUTO=0/1` (heartbeat re-plans weekly and
*notifies*, applying only with the flag on); data under `MEMORY_DIR/plans/`. Integration:
`doctor.py` gains a "plan" check showing unapplied recommendations; Prometheus's
`evolution_audit` reads plan history as audit input; the review step reuses
`heartbeat.tick()`'s cadence mechanism (`_due`).

**12. Why the Olympus approach is superior.** Colibri emits reasons a human can read;
Olympus emits reasons a machine can audit, each pinned to ledger evidence, and every applied
item is benchmark-gated with auto-rollback — configuration changes join the same
measurement-first regime as prompt changes. The plan journal is an accumulating record of
what tuning worked *on this workload* (Asset 3), which Colibri's stateless planner never
builds.

---

## O2. Doctor's read-only check matrix incl. linkage forensics (§17.1)

**1. What Colibri does.** `coli doctor`: read-only checks (pass/warn/fail/**skip**),
JSON-able, **schema-versioned**; model path/config/tokenizer, persistence writability, engine
binary, disk space, RAM feasibility, plan warnings — and a CUDA matrix crossing **requested ×
detected × linkage**, where linkage forensics means scanning the binary for a baked marker
string because a runtime-loaded DLL leaves no import-table entry.

**2. Why it exists.** The deadliest config failures are the ones the happy path can't see: a
stale CPU-only binary that *runs* (#306), a GPU "enabled" but never linked (#121's
silently-CPU benchmarks).

**3. How it works internally.** Pure reads — no generation, no mutation — so doctor is safe
to run anywhere, any time, and in CI.

**4. Strengths.** The requested/detected/linked distinction turns "it doesn't work" into a
named cell; JSON + schema version make it scriptable; read-only makes it trustworthy.

**5. Weaknesses & trade-offs.** Doctor can only verify presence, not behavior (that's the
self-test's job, O3) — Colibri keeps this boundary clean and so must Olympus. A check matrix
also rots: every new subsystem needs a doctor row or the tool silently stops covering the
install.

**6. Security implications.** Doctor output is a reconnaissance map (paths, providers,
gateway inventory) — fine locally, but `--json` output pasted into issues must mask secrets;
Olympus already has `config.mask_key` and `firstrun._is_secret` to reuse. Read-only-ness is
itself a security property: a diagnostic that mutates is an attack surface.

**7. Scalability implications.** Checks are O(subsystems) and offline; trivially cheap.
The registry pattern in `health.py` (`_COMPONENTS` resolved by name, probe errors can never
crash the report) is the right chassis and should be shared.

**8. Performance implications.** None at runtime; the payoff is failure-time — minutes of
debugging replaced by a named cell.

**9. Maintainability implications.** Olympus's `doctor.py` today returns only OK/WARN/FAIL,
has no JSON mode, no schema version, and no matrix concept. Adding subsystems already means
editing `_optional_checks` by hand; a declarative probe registry (name → requested? →
importable? → functional?) makes each optional backend one entry.

**10. How Olympus should redesign it.** Extend `olympus/doctor.py`:

- Add `SKIP` to the status enum and `to_json()` emitting `{"schema": 1, "checks": [...]}`;
  `olympus doctor --json` exits non-zero on FAIL (CI-usable, mirroring `is_ready()`).
- Add the **linkage matrix** for every optional/lazy backend, three axes per row —
  *requested* (env/config says on) × *importable* (the extra is installed) × *functional*
  (a cheap read-only probe passes): Postgres store (`OLYMPUS_STORE_*` set × psycopg import ×
  `SELECT 1`), Docker sandbox (`sandbox.backend()=="docker"` × client import × daemon ping),
  operator (`OLYMPUS_OPERATOR` × CDP transport × Chrome binary present — the direct analog of
  "DLL present but nvidia-smi absent"), computer use (already three-state in
  `_optional_checks`: enabled × actuator named × `actuator_ready()` — generalize this exact
  pattern), media keys, each gateway token. The forensic doctrine: **for every lazily-imported
  extra, doctor must be able to distinguish "off", "on but not installed", and "installed but
  non-functional" without running the feature.**
- Add **config forensics**: saved values shadowed by env vars (surface
  `firstrun.show_config`'s `overridden` flag as a WARN row), `OLYMPUS_MODEL` absent from the
  provider catalog, a pool member whose key is missing, sovereign mode on with zero eligible
  local members (fail row, per `docs/SOVEREIGNTY.md` fail-closed).
- Share one probe registry with `health.py`: doctor = "is it set up" (static axes), health =
  "is it degraded now" (the functional axis, live). One probe definition, two consumers.

**11. Final Olympus architecture.** All in existing `doctor.py`/`health.py`; no new module.
`Check` gains `status: ok|warn|fail|skip` and `axes: {requested, importable, functional}`;
CLI `olympus doctor [--json]`; `olympus setup` continues to end with doctor (it already
does). `opsplan.py` (O1) feeds recommendation rows; the security never-list violations
surface here as WARN.

**12. Why the Olympus approach is superior.** Colibri's matrix covers one accelerator;
Olympus generalizes it into a contract over *every* lazy extra — which Olympus needs more
than Colibri did, because "three pure-Python deps + lazy extras" (README) is precisely the
architecture that produces installed-but-non-functional states. And Olympus's doctor feeds a
governance loop (plan recommendations, audit input) instead of terminating at a printout.

---

## O3. Setup self-test oracle + upgrade handoff hardening (§17.3, §9.1; beyond-Colibri half)

*(Grouped honestly: both are the same mechanism — a golden gate at a lifecycle transition —
fired at install-time and upgrade-time respectively.)*

**1. What Colibri does.** `setup.sh` ends with the tiny-oracle self-test: a random-weight
**true-architecture** fixture generated greedily and compared token-exactly (`TF=1` 32/32 is
the canonical gate, §9.1); the release workflow adds a behavioral verification step (unpack +
`coli info` in a clean dir) that caught a real v1.1.0 launcher failure (§21).

**2. Why it exists.** "Installed" and "works" are different facts; the oracle converts the
second into a binary, free, deterministic check with zero model downloads.

**3. How it works internally.** The fixture preserves the real architecture (MLA/MoE/DSA
shapes) with fake weights — the *pipeline* is fully exercised even though the *content* is
noise. That is the transplantable idea.

**4. Strengths.** Deterministic, free, offline, and canonical: every refactor answers to
32/32 before anything else.

**5. Weaknesses & trade-offs.** Token-exactness is unreachable for Olympus — frontier APIs
are nondeterministic and there is no local fixture model guaranteed on an install. Chasing
output-exact oracles would be vaporware. The honest translation: **structure-exact, not
token-exact** — assert the pipeline's *shape* (routing happened, the plan graph was built,
gates engaged, the trace chain verifies), which is deterministic even when text isn't.
Olympus already owns the seam: `replaygate.self_check()` runs divergence checks on the
heartbeat cadence (`heartbeat.tick`'s `replay_gate` block), and `replaystore` can play a
canned model backend.

**6. Security implications.** The offline tier must run with a **mock backend and zero
egress** so `olympus selftest` is safe on air-gapped/sovereign installs — it doubles as the
sovereignty smoke test (assert `EgressBlocked` fires when the fixture tries a remote host).
The live tier spends real money and sends a fixed innocuous prompt; it must be cost-capped
and never include user data.

**7. Scalability implications.** Offline tier: seconds, free, runs in CI on every commit
(it becomes the ops analog of the golden-eval regression gate). Live tier: one minimal call
per pool member, bounded by `OLYMPUS_SELFTEST_BUDGET_USD` (default $0.05).

**8. Performance implications.** None at runtime; catches the expensive failures (a broken
provider key discovered mid-heartbeat at 3am; a post-upgrade import error discovered by the
first user message).

**9. Maintainability implications.** The golden expectations must be *structural invariants*
(N steps planned, verifier invoked, memory write landed, trace hash-chain valid), not
snapshot strings — snapshot oracles on nondeterministic systems rot into permanent skips.

**10. How Olympus should redesign it.**

- **`olympus selftest`** (CLI; function in `replaygate.py` — extend, don't add a module):
  *Tier 0 (offline oracle):* run one canned council request end-to-end against the replay
  backend, asserting: Zeus produced a route; Athena produced a dependency graph with ≥1
  serial edge on the canned task; the executor isolated an injected step failure; Aletheia
  ran; the security gate screened an injected risky command (assert *blocked*); memory and
  trace writes landed and the chain verifies. Deterministic, free, no key needed —
  runnable before `firstrun.configured()` is even true.
  *Tier 1 (`--live`):* one minimal real completion per pool member — auth, response shape,
  latency — under the budget cap. This is `doctor --live`'s implementation.
- **Wire into lifecycle transitions:** `olympus setup` ends with doctor + Tier 0 (extend the
  existing "doctor reused at the end of setup" pattern in `doctor.py`'s docstring).
  `selfupdate.run()` runs Tier 0 **after** the upgrade command succeeds; on failure it prints
  the pinned rollback (`pip install olympus-council==<from_version>`) — the version is already
  captured in `write_handoff()`.
- **Upgrade handoff hardening (beyond Colibri):** `selfupdate.write_handoff()` gains
  `{"selftest": {...}}` filled by the post-upgrade run, and `heartbeat.tick`'s existing
  `take_handoff()` block reports it: "restarted from v X; self-test PASS; 2 in-flight tasks
  resume." A failed post-upgrade self-test escalates through `gateway.notify_all` instead of
  waiting to be discovered. Handoff consumption stays exactly-once (the current
  `take_handoff` unlink semantics).

**11. Final Olympus architecture.** `replaygate.selftest(tier: int)`; CLI
`olympus selftest [--live]`; env `OLYMPUS_SELFTEST_BUDGET_USD`; integration points:
`firstrun` wizard tail, `selfupdate.run`, heartbeat handoff block, CI (Tier 0 in the pytest
suite — it is pure-offline). Results journaled to `MEMORY_DIR/selftest.jsonl` (a reliability
time series — Asset 1 material for *this* install).

**12. Why the Olympus approach is superior.** Colibri's oracle proves numerics; Olympus's
proves **governance** — that the gates, verifier, and refusal paths are actually engaged, the
properties a council platform must never ship broken. And by firing at both install and
upgrade transitions with journaled results, "does it still work" becomes a recorded time
series rather than a one-time setup event.

---

## O4. Warm-up: topic-diverse priming of learned caches (§17.3 `warmup.ps1`)

**1. What Colibri does.** An overnight script runs 30 **topic-diverse** prompts to populate
`.coli_usage` so AUTOPIN has a real heat profile — diverse because *single-topic warming
overfits the pin*; `NGEN=32` because usage stats save only on clean completion.

**2. Why it exists.** The learning cache (§26.6, profile quality beat capacity 0.94–1.64 vs
0.29 tok/s) is worthless on day one; warming converts idle hours into day-one performance.

**3. How it works internally.** Nothing clever — the insight is entirely in the *sampling
discipline* (diversity, clean-completion-only accounting), which is what transfers.

**4. Strengths.** Turns cold-start into a one-night fix; the overfit warning is a measured
lesson most cache-warming schemes miss.

**5. Weaknesses & trade-offs.** Colibri's warmup spends only electricity; Olympus's spends
**dollars** unless routed through the offline tier. Worse, synthetic traffic can poison
Olympus's *learning* assets: routing priors trained on synthetic prompts could mis-route real
traffic, and — critically — synthetic outcomes must never enter the calibration record or
`compare.py` tallies (MOAT_ANALYSIS Asset 1/2 are only valuable because they are *real*
measured behavior).

**6. Security implications.** Warmup prompts are operator-authored fixtures, never derived
from user data; the run is tagged synthetic end-to-end so downstream consumers (memory,
outcomes, contribution queue) can exclude it. `OLYMPUS_ROUTING_SYNTHETIC` already exists as
exactly this seam — extend its meaning rather than invent a parallel flag.

**7. Scalability implications.** The warm set is O(specialists): one canonical task per
domain (13 today) plus a serial-chain and a parallel-fan plan shape. Coverage is enforced
the same way capability counts are — a drift check that fails when a specialist is added
without a warmup item (mirroring the benchmark-coverage auto-fill rule in README §"Keeping
all 13 specialists strong").

**8. Performance implications.** Three caches benefit: (a) **routing/bandit priors**
(`learned_routing.py`, `bandit_routing.py`) — a fresh install explores blind; seeded priors
cut early mis-routes; (b) **tool-selection priors** (`toolselect.py`); (c) **provider prompt
caches** — before heartbeat batch jobs, one priming call re-establishes the shared system
prefix in the provider's cache TTL window, a measurable token-cost saving on the very next
burst (the closest true analog of "prime the cache before the day starts").

**9. Maintainability implications.** Keep the warm set as data
(`olympus/prompts/warmup.jsonl` beside the existing `prompts/` directory), one line per
specialist, with the coverage check in CI.

**10. How Olympus should redesign it.**

- `olympus warmup [--live N]`: default runs the warm set through the **offline replay tier**
  (free) to exercise plumbing and seed structural stats; `--live N` runs up to N items
  against real models under a budget cap to seed *quality*-bearing priors.
- **Diversity doctrine encoded:** the set must cover every specialist and both plan shapes;
  the CLI refuses to run a set that fails coverage (refusal-over-degradation, O7).
- **Clean-completion accounting:** priors update only from runs that pass Aletheia and
  complete unerrored — the direct translation of NGEN=32/"usage saves only on clean
  completion." Partial or failed warm runs record nothing.
- **Anti-overfit:** synthetic priors are written with a `synthetic: true` tag and a low
  bandit prior weight that real traffic rapidly outweighs; they *decay*, never accumulate —
  the pin-overfit lesson made structural instead of procedural.
- **Exclusion firewall:** `usage.py`, `outcomes.py`, `compare.py`, `liveeval.py`, and the
  contribution queue drop synthetic-tagged runs. One tag, checked in tests.
- Heartbeat: an optional `warmup` cadence (`OLYMPUS_WARMUP_EVERY`, default off) re-primes
  provider prompt caches before the dense job window.

**11. Final Olympus architecture.** No new module: `warmup()` lives in
`learned_routing.py` (it owns the priors being seeded), warm set in `prompts/warmup.jsonl`,
CLI `olympus warmup`, env `OLYMPUS_WARMUP_EVERY`, `OLYMPUS_WARMUP_BUDGET_USD`. Integration:
`firstrun` offers Tier-0 warmup at the end of setup ("prime the council? [free, ~30 s]").

**12. Why the Olympus approach is superior.** Colibri warms one cache with one currency;
Olympus warms three (routing priors, tool priors, provider prompt caches) under an explicit
budget, with the overfit lesson promoted from a script comment to a structural decay rule,
and with a firewall that keeps synthetic traffic out of the moat-bearing datasets — a failure
mode Colibri never had to consider.

---

## O5. Background-work babysitter with zombie detection (§17.3 `supervisor.sh`)

**1. What Colibri does.** A flock-singleton shell babysitter for the long conversion
pipeline that kills downloads **stalled** (no byte progress) for >180 s, letting the
converter's own resume logic recover — kill-and-let-resume rather than nurse-along.

**2. Why it exists.** Multi-day unattended work fails in the middle, and a stalled process
is worse than a dead one: it holds the lock, burns no progress, and looks alive.

**3. How it works internally.** Progress-based (bytes on disk), not liveness-based (pid
alive) — the load-bearing distinction: zombies are processes that are alive but not
*progressing*.

**4. Strengths.** Tiny, safe (kills only what its flock scope owns), and paired with
resumable work so a kill is always recoverable.

**5. Weaknesses & trade-offs.** It babysits exactly one job type, externally, in shell.
Olympus's exposure is broader and currently **unguarded**: `heartbeat.tick()` runs every job
serially and inline — a wedged Argus scan (network hang inside a provider SDK) stalls the
scheduler, goals, backups, and every other cadence *silently and indefinitely*.
`proclock.DEFAULT_TIMEOUT` bounds lock waits, and its own docstring names the residual
pathology this rubric closes: "the only unbounded wait is a LIVE-but-wedged holder."
`hibernate.run_once()` inherits the same risk — a serverless tick that never returns.

**6. Security implications.** A component with kill authority is a destructive actuator. It
must kill **only provenance-matched processes** (O6's marker discipline) and only its own
lease-holding children — never a name-pattern kill (`pkill -x glm` is the cautionary tale,
§13). Kill events are `errors.capture`d with full forensics; the watchdog itself takes no
approval-gated actions beyond that scoped kill.

**7. Scalability implications.** Leases are one small JSON file per running job; the sweep
is O(running jobs) at tick start. Works identically under the always-on heartbeat and the
hibernating `olympus tick` (a fresh serverless invocation can adopt or clean up leases left
by a killed predecessor — which the current handoff/inflight design almost provides, via
`selfupdate.pending_work()`'s `MEMORY_DIR/inflight` scan).

**8. Performance implications.** The headline gain is *scheduling* reliability: one job's
hang no longer starves the other ~15 cadences in `heartbeat.tick`. Secondary: bounded job
wall-clock keeps `hibernate.next_due_in` honest (a hung tick today makes every "next wake"
computation moot).

**9. Maintainability implications.** This earns the second (and last) new module: the lease
lifecycle is a coherent unit that `heartbeat.py` (331 lines, already dense) should consume,
not contain.

**10. How Olympus should redesign it.** New **`olympus/watchdog.py`**:

- **Leases:** `MEMORY_DIR/leases/<job>.json` = `{job, pid, started, deadline, progress_ts,
  progress_note, spend_usd}`. A job renews `progress_ts` via a callback (steps completed,
  bytes fetched, dollars spent — progress, not liveness).
- **Execution wrapper:** `watchdog.supervised(job_name, fn, timeout, stall)` runs `fn` in a
  worker thread; the wrapping heartbeat job returns a "still running" log line instead of
  blocking the tick when `fn` overruns (`OLYMPUS_JOB_TIMEOUT`, per-class defaults: scans
  minutes, training hours). Long jobs stop being tick-blocking by construction.
- **Sweep at tick start:** for each lease — pid dead → clean up, `errors.capture` with the
  postmortem verdict (O6); pid alive but `now - progress_ts > stall` → **zombie**: verify
  provenance (O6 marker), kill, capture, clean. Stall default `OLYMPUS_JOB_STALL_SECS=300`,
  overridable per job.
- **Singleton:** the watchdog sweep itself runs under `proclock.lock("watchdog")` — the
  flock-singleton translated onto the existing lock layer.
- Resumability contract: only jobs that are safe to kill-and-resume run supervised with kill
  enabled; others (rare) get timeout-report-only. Each heartbeat job declares which.

**11. Final Olympus architecture.** New `olympus/watchdog.py` (~200 lines); `heartbeat.tick`
wraps its LLM-dependent jobs in `watchdog.supervised`; `scheduler.py` jobs get leases too;
`hibernate.run_once` sweeps leases before computing `next_wake_secs`; CLI `olympus jobs`
lists live leases. Env: `OLYMPUS_JOB_TIMEOUT`, `OLYMPUS_JOB_STALL_SECS`.

**12. Why the Olympus approach is superior.** Colibri babysits one pipeline from outside;
Olympus makes supervision a platform facility with typed leases, progress-based stall
detection in *both* currencies (time and spend — a job can zombie by burning budget without
progress, a failure class Colibri doesn't have), provenance-scoped kill authority, and
integration with hibernation so serverless installs get the same guarantee as always-on ones.

---

## O6. Process lifecycle forensics: `coli stop`, `engine_diag`, RLIMIT (§13, §26.2)

*(Grouped honestly: all three are "know your processes" — provenance-safe teardown, death
forensics, and fd headroom are one lifecycle discipline.)*

**1. What Colibri does.** `coli stop` does a pidfile + `/proc`-scan kill of
**demonstrably-ours** processes — necessary because the OpenMP self-re-exec renames the
binary to `exe`, so `pkill -x glm` once left double ghost engines that OOM'd the box.
`coli chat`'s `engine_diag()` performs OOM-kill forensics when the engine dies. The launcher
raises RLIMIT_NOFILE to 65536 because 144+ shards exhaust the default.

**2. Why it exists.** Name-based process management is a loaded gun (the ghost-engine
incident), and a dead process with no diagnosis becomes a support thread instead of a fix.

**3. How it works internally.** Provenance = evidence chain (pidfile + `/proc` cmdline
match), never name match; forensics = read what the kernel left behind.

**4. Strengths.** Kill decisions based on evidence; the diagnosis lands *in the tool the
user is already holding*.

**5. Weaknesses & trade-offs.** Colibri's forensics target one death mode (OOM). Olympus's
process family is wider — heartbeat, gateway daemon, web server, sandbox children, operator
Chrome, docker containers — and its death modes are wider too (provider quota exhaustion,
disk-full memory dir, OOM, unhandled exception). Today `gateway.py` already records
`{"pid": os.getpid()}` in the status file and computes `running`/`stale` via `_pid_alive`
(gateway.py ~640–753) — presence detection exists; provenance-safe *teardown* and *death
diagnosis* do not.

**6. Security implications.** Kill authority scoped by provenance is the security property:
`olympus stop` must never signal a process merely because its cmdline contains "olympus"
(another user's install, an editor with the repo open). Marker: every Olympus-spawned
process carries `OLYMPUS_INSTANCE=<uuid>` in its environment (uuid minted per
`MEMORY_DIR`), and teardown requires **both** the recorded pid *and* the marker read from
`/proc/<pid>/environ` (readable for same-uid processes — exactly the scope kills should have).
Non-Linux fallback: pid + cmdline + start-time triple. `/proc` reads are read-only.

**7. Scalability implications.** O(our pids), not O(system pids) — the status files and
lease files (O5) enumerate candidates; `/proc` is consulted only to *verify*, never to
*discover*.

**8. Performance implications.** RLIMIT: the gateway daemon with many channels + ANN index
files + sandbox children can brush the default 1024 fd ceiling; raise-only-never-lower at
daemon startup (log the raise and its reason — a one-line absorb).

**9. Maintainability implications.** Forensics rules must be a small verdict table, not a
diagnostic AI: exit signature → named verdict → named knob.

**10. How Olympus should redesign it.**

- **`olympus ps`** — list Olympus processes (from gateway status + leases), with age, RSS
  (`/proc/<pid>/status` VmRSS), and provenance verification state.
- **`olympus stop [component]`** — SIGTERM → grace → SIGKILL, only on
  provenance-verified pids; refuses (with the evidence printed) on any mismatch.
- **Death postmortem** (in `watchdog.py`'s sweep and `olympus ps` for stale entries), the
  `engine_diag` translation with the verdict-names-the-knob doctrine (absorption doc 09):
  exit code 137 / waitpid signal 9 + last-known RSS near system RAM → "likely OOM-killed;
  last RSS N GB; lower `OLYMPUS_MAX_CONCURRENT_CALLS` or add swap"; `errors.jsonl` tail shows
  429s → "provider quota exhausted; rotate keys (`OLYMPUS_PROVIDER_KEYS`) or lower cadences";
  disk-full on `MEMORY_DIR` → names `OLYMPUS_RETAIN_DAYS`/maintenance. Verdicts are
  `errors.capture`d and shown by `olympus errors`.
- **RSS self-report:** the gateway status heartbeat (already periodic) adds its own VmRSS,
  giving the postmortem its "last known RSS" for free.
- **RLIMIT_NOFILE raise** at `gateway`/`serve` startup: soft → min(hard, 65536), logged.

**11. Final Olympus architecture.** No new module: `gateway.py` (status enrichment, RLIMIT),
`cli.py` (`ps`/`stop`), `watchdog.py` (postmortem at sweep time), `errors.py` (verdict
records). Env: none new (marker is automatic).

**12. Why the Olympus approach is superior.** Colibri's forensics live in one client for one
death mode; Olympus generalizes to a verdict table over its real death modes (money, disk,
kernel), stores every postmortem durably (a reliability history for this deployment —
Asset 3), and makes provenance a *mint-time* property (the instance marker) rather than a
*kill-time* heuristic.

---

## O7. OOM-refusal-over-silent-death → refusal-over-degradation doctrine (§7.4)

**1. What Colibri does.** `cap_for_ram` refuses to start when the projected peak exceeds
physical RAM (#305 — the alternative is a silent kernel OOM-kill mid-load), with an explicit
override (`COLI_RAM_OVERCOMMIT=1`); an RSS guard (#403) sheds cache and lowers the cap when
measured RSS exceeds budget; `CAP_RAISE` fixes the inverse failure (#12: a 128 GB box running
a 16 GB box's cache); the mux server refuses `CONTEXT_EXCEEDED` loudly instead of silently
truncating (#401/#506).

**2. Why it exists.** Silent degradation converts a configuration bug into a *correctness or
availability* bug that surfaces far from its cause.

**3. How it works internally.** Project the peak *before* committing; compare to a hard
resource; refuse with the arithmetic shown; accept an explicit, logged overcommit.

**4. Strengths.** The failure happens at second 1 with a named cause, not at minute 40 with
a kernel log line; the override keeps expert users unblocked.

**5. Weaknesses & trade-offs.** Refusal is user-hostile when the projection is wrong or the
stakes are low — Colibri accepts that trade for RAM because the downside (OOM-kill after
loading 370 GB) is catastrophic. Olympus must grade the doctrine by stakes: refuse on
*money and data*, disclose on *context*, because a mid-conversation hard refusal over a
mis-projected $0.40 is worse than the disease.

**6. Security implications.** Refusal-over-degradation is already Olympus's security posture
(cmdguard fail-closed, sovereign `NoLocalModelError`, `EgressBlocked`); this rubric extends
the same shape to economics. Overcommit overrides must be env-explicit and logged in the
trace — an agent must never be able to "override" a budget refusal conversationally.

**7. Scalability implications.** Cost projection is arithmetic over the Athena plan (steps ×
model prices × max_tokens ceilings) — O(plan), free, and it *improves* fleet behavior: a
budget-refused heartbeat job reschedules instead of half-running.

**8. Performance implications.** The CAP_RAISE inverse matters as much as the refusal:
detecting *under*-provisioning (budget slack never used; tiny `OLYMPUS_HISTORY_TOKEN_BUDGET`
on a 200K model) is O1's planner recommending a raise, with reasons. Refusal prevents the
worst spend pattern there is: paying for 80% of a run that cannot finish.

**9. Maintainability implications.** One admission function at one choke point
(orchestrator entry), like Colibri's single `cap_for_ram` — scattered per-feature budget
checks are how "Success" bugs happen.

**10. How Olympus should redesign it.**

- **Admission preflight** in `orchestrator.py`: before executing an Athena plan, project the
  cost ceiling; if projected > remaining (`OLYMPUS_DAILY_BUDGET` /
  `OLYMPUS_RUN_BUDGET_USD` via `usage.py`), **refuse with the itemized projection** and offer
  the degraded plan *explicitly* ("projected $1.90 vs $0.60 remaining; run the 2-specialist
  reduced plan instead? [needs confirmation]") — degradation becomes a user choice, never a
  silent shrink. Override: `OLYMPUS_BUDGET_OVERCOMMIT=1` (the `COLI_RAM_OVERCOMMIT`
  translation), logged into the trace.
- **Context:** compaction (`OLYMPUS_INRUN_COMPACT`) stays, but gains **loud disclosure** — a
  one-line notice of what was summarized away, in-band, every time; and a hard
  `CONTEXT_EXCEEDED`-style refusal when even compaction cannot fit the request.
- **Disk:** `OLYMPUS_MEMORY_FLOOR` exists; below-floor memory writes raise a typed error
  surfaced via `errors.capture` — never a silent skip that quietly stops the system learning.
- **Mid-run guard (the RSS-guard translation):** `usage.py` spend tracked per run; crossing
  the run budget mid-flight stops cleanly at the next step boundary with partial results
  clearly labeled partial.

**11. Final Olympus architecture.** No new module: one `admit(plan) -> Admission` in
`orchestrator.py` backed by `usage.py` projections and `providers.fetch_pricing`; envs
`OLYMPUS_BUDGET_OVERCOMMIT` (new), existing budget/floor vars unchanged. Refusals are typed
(`BudgetRefusal`, carrying the projection) so gateways render them well, and journaled —
refusal frequency is itself a planner signal (O1: raise the budget or shrink the council).

**12. Why the Olympus approach is superior.** Colibri refuses over one resource with one
override; Olympus grades the doctrine over four currencies (money, tokens, context, disk) by
stakes, makes every degradation either refused or *explicitly chosen and disclosed*, and
feeds refusal statistics back into planning — the doctrine becomes a measured control loop
rather than a startup check.

---

## O8. Honest error surfacing — the #236 "Success" class (§4.1, §11.3, §3.1)

**1. What Colibri does.** Fixed the short-read path that printed "Success" (errno unset,
#236); preserves per-thread real `GetLastError` across the Windows pread shim (#307); refuses
loudly rather than ignoring unsupported API fields (§9.2); and draws an explicit line —
**fail-soft for accelerators, fail-hard for data** (§3.1): grammar/prefetch/mirror failures
degrade silently to correct-but-slower; corrupt containers `exit(1)`.

**2. Why it exists.** A wrong error message is worse than no message — it sends the debugger
in the wrong direction with confidence.

**3. How it works internally.** Discipline, not machinery: capture the real cause at the
failure site, before anything can overwrite it; classify every failure as
accelerator-class or data-class and pick the failure mode by class.

**4. Strengths.** The classification makes "should this crash?" a one-word design review
question.

**5. Weaknesses & trade-offs.** Olympus already has the durable half (`errors.capture` —
JSONL + rate-limited Telegram alerts, "error handling must not create errors") and pockets of
the honesty half (`heartbeat._job_error` collapsing expected keyless failures into one quiet
line, FEATURE_AUDIT §2.2). The gaps: (a) truncation amputating causes — `errors.py` clips to
500 chars and `health.py` to 120, which routinely cuts off the provider's JSON error body,
the exact "Success" failure shape (the recorded message no longer names the real cause);
(b) no fingerprinting — one repeating bug burns the whole `_ALERT_MAX_PER_HOUR=12` alert
budget and buries a second, different failure; (c) the accelerator/data line is folklore, not
a written contract.

**6. Security implications.** Honest errors must still be *safe* errors: provider bodies can
echo request content, so the durable record keeps the structured cause (status code, provider
error `type`/`code`, retry-after) rather than raw bodies, and secrets are masked with the
existing `config.mask_key` path. Data-class failures (vault, ledger, approval records,
memory writes) must fail hard — a governance system that silently drops an approval record
is unsound, not degraded.

**7. Scalability implications.** Fingerprinting (`hash(where + exc_type + normalized
message)`) makes the ledger O(distinct bugs) in alert cost instead of O(occurrences), and
gives `olympus errors` a "top recurring" view — which is what Prometheus's audit actually
wants to read.

**8. Performance implications.** Negligible; the payoff is diagnosis time and alert
signal-to-noise.

**9. Maintainability implications.** Write the class table down (module docstring in
`errors.py` + THREAT_MODEL cross-ref): **accelerator-class** (routing priors, prompt caches,
telemetry, warmup, prefetch analogs, ANN index) = degrade silently to correct-but-slower,
capture at DEBUG; **data-class** (memory, ledger, vault, approvals, trace chain, usage
ledger) = typed exception, capture, surface. New modules declare their class in review.

**10–11. Redesign & final architecture.** All in `errors.py`: add `fingerprint` to the
record; per-fingerprint alert dampening (first occurrence alerts; repeats within the hour
aggregate into "×N" summaries) while keeping the global cap; a `cause` sub-dict
`{status, provider_code, retryable}` populated by `backend.py` at the failure site (the
capture-before-overwrite discipline); `olympus errors` gains `--top`. Plus the doctrine text.
No new envs, no new module.

**12. Why the Olympus approach is superior.** Colibri's honesty lives in C call sites;
Olympus's lives in a durable, deduplicated, cause-typed ledger that an operator, the
heartbeat, and Prometheus's self-audit all read — errors become an accumulating input to
self-improvement instead of a scrollback artifact. The fail-soft/fail-hard line, written as
contract, is what lets every other rubric here (planner, warmup, watchdog) degrade safely by
construction.

---

## O9. Attach-vs-private topology (§13, §26.12)

**1. What Colibri does.** `coli chat` health-probes for a running server in milliseconds and
**attaches** to it (sharing the warm engine: pin hit 4%→55%, ~10× throughput) or transparently
spawns a private engine — the user types the same command either way.

**2. Why it exists.** The warm state *is* the performance (§26.6); two cold private engines
on one box is the worst of both worlds.

**3. How it works internally.** Millisecond `/health` probe → attach via the wire protocol,
else spawn. Feature detection keeps the client honest against either topology.

**4. Strengths.** Zero-configuration topology; the expensive asset (warm engine) is shared
by default.

**5. Weaknesses & trade-offs.** Olympus today has the inverse defect: `olympus` (CLI chat)
always builds a fresh in-process council even when `olympus serve`/the gateway daemon is
running — two processes contending on `MEMORY_DIR` (the exact race `proclock.py`/ADR 0005
exists to referee), two usage-ledger writers, two sets of routing stats warming
independently, and the daemon's provider prompt-cache locality diluted. Olympus's "warm
state" is smaller than Colibri's (no 370 GB to reload) but real: in-memory routing/bandit
state, ANN index handles, circuit-breaker/rate-limit windows, provider prompt caches keyed to
the daemon's traffic. The trade-off Colibri didn't face: attaching moves the conversation
across a **privilege boundary** — the CLI's interactive affordances (local approvals, sandbox
on this terminal's host) don't all survive an HTTP hop.

**6. Security implications.** Attach only to a **verified own daemon**: loopback + the
`OLYMPUS_INSTANCE` marker echoed in the health payload (O6) + API key from the local
`config.env` — never auto-attach to an arbitrary port answering `/health`. Approval-gated
actions must either round-trip approvals through the attached session (the gateway already
does interactive approvals for chat channels) or force private mode for commands that demand
local-terminal approval (`OLYMPUS_ATTACH=never` per invocation). Fail closed: any doubt →
private mode, which is today's behavior.

**7. Scalability implications.** Single-writer memory (fewer `proclock` timeouts), one
usage ledger, one learning stream. On multi-host installs the Postgres store already covers
state; attach stays a same-machine optimization, exactly like Colibri's.

**8. Performance implications.** Shared prompt-cache locality and warm routing state; no
double model-pool spin-up; CLI startup latency drops to a probe + HTTP call.

**9. Maintainability implications.** The attach client must be the existing
OpenAI-compatible surface (`openai_compat.py` / `web.py` endpoints) — inventing a private
CLI↔daemon protocol would be a third API to maintain. Feature skew is governed by version
negotiation, not capability sniffing.

**10. How Olympus should redesign it.**

- `cli.py` startup: probe the gateway status file (`gateway.read_status()` — pid-alive and
  heartbeat-fresh checks already exist) then `GET /api/health` with ~100 ms timeout.
  Healthy + same `__version__` + instance marker match → **attach** as an API client;
  else private in-process (unchanged default path).
- `OLYMPUS_ATTACH=auto|never|always` (default `auto`); `always` errors honestly when no
  daemon answers (refusal-over-degradation) instead of silently going private.
- **Version-skew guard** (upgrade handoff tie-in, O3): after `olympus upgrade`, a CLI probe
  finding a daemon still on `from_version` prints "daemon running v X, you have v Y —
  `olympus stop && olympus serve` to restart it" rather than attaching across skew.
- Health payload gains `{version, instance, capabilities}` so the client downgrades
  affordances explicitly (feature detection, the Colibri dashboard's `cache_slot` pattern).

**11. Final Olympus architecture.** `cli.py` (probe + client mode), `web.py` (health payload
enrichment), `gateway.py` (status file already carries pid; add version + instance),
`openai_compat.py` (the wire). Env `OLYMPUS_ATTACH`. No new module.

**12. Why the Olympus approach is superior.** Colibri attaches for speed; Olympus attaches
for speed *and* consistency — single-writer memory and one learning stream mean the
accumulated assets (routing stats, usage ledger, calibration data) stop being split across
processes, which is a data-integrity win Colibri's design never needed. And the topology is
governed: marker-verified, version-gated, fail-closed to private.

---

## Open questions & research spikes

1. **Planner-vs-evolve boundary (O1).** `evolve.py` and Prometheus already own bounded
   self-tuning; `opsplan.py` must not become a second, competing knob-setter. Spike (≤2 days):
   inventory which vars each may touch and write the disjoint ownership table before coding.
   Tension flagged for the synthesizer — absorption doc 09's verdict engine ("verdicts name
   knobs") plus this planner ("planner sets knobs") must be one pipeline, not two.
2. **Offline oracle fidelity (O3).** How much of the real pipeline can run against the
   replay backend without a live key? If `replaystore` can't yet stand in for tool calls,
   Tier 0's assertions shrink. Spike (≤3 days): drive one canned council request fully
   offline; enumerate what had to be stubbed; only then freeze the golden invariant list.
3. **Prompt-cache priming economics (O4).** The claim that a priming call before heartbeat
   bursts saves net tokens depends on provider cache TTLs and job spacing. Measure before
   shipping the cadence: one week A/B on the heartbeat's own usage ledger; keep only if the
   ledger shows a saving (gate_prompt culture; kill it with a written eulogy otherwise).
4. **Thread-kill semantics (O5).** Python can't safely kill a wedged *thread*; the watchdog
   can only truly kill subprocess-shaped work. Decision needed: move the worst offenders
   (network-bound scans) to subprocess execution, or accept report-only supervision for
   in-thread jobs. Spike bound: prototype `watchdog.supervised` both ways on the Argus scan.
5. **Attach-mode approvals (O9).** Approval round-tripping over the attached session needs a
   design pass with the security gate owners — the CLI must never render a weaker approval
   surface when attached than private mode gives. Until resolved, commands touching
   approval-gated tools force private mode.
6. **Windows degradation.** `proclock` already degrades on non-POSIX (ADR 0005); the
   watchdog's `/proc` forensics and environ-based provenance need the documented Windows
   fallback (pid + cmdline + start-time) or an honest "unsupported, report-only" note —
   Colibri's lesson (§11.3) is that the Windows path must be *designed*, not assumed.
