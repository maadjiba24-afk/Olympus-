# Absorption 08 — Prediction & Prefetching

**Colibri domain:** the measured prefetch ladder (§7.5): `SPEC` willneed of the previous
token's routing; `PILOT`/`PILOT_TWO` router-state next-layer prediction (71.6% recall vs
41.3% for previous-token heuristics); `PILOT_REAL` real loads under a two-part safety
invariant with eviction guards (#441/#490); `COUPLE` offline cross-layer co-activation
tables (#176, §19.2, +3.6..+9.4 pp recall); `LOOKA` pure-measurement predictability
reporting; `PREFETCH` proper shipped **default-off** because measurement showed real
parallel loads made bare WILLNEED superfluous; the lock-free 1P/1C ring-buffer hint
architecture feeding a single detached I/O thread (§3.4, §7.5); and the preserved
**negative result** — next-layer prediction with GPU staging had real signal (70–79%
recall) and still lost net on PCIe contention (§10.4, §19.2 lineage).
**Olympus target:** predictive orchestration — predicting the *next specialist, next
tool, next context page* from live orchestration state; warm-up actions that are
**provably harmless** (context retrieval, embeddings, connection warm-up — never
anything with side effects); displacement guards so speculation never evicts genuinely
hot state or steals contended resources; offline coupling tables mined from
`olympus/trajectories.py` / `olympus/trace.py` data; predictability instrumentation
**before** any predictor is built; and the discipline of shipping every prefetch level
OFF until measured on the target workload. Home modules: `olympus/trace.py`,
`olympus/trajectories.py`, `olympus/routing_outcomes.py`, `olympus/heartbeat.py`,
`olympus/recall.py`, `olympus/docrag.py`, `olympus/annindex.py`, `olympus/embed.py`,
`olympus/playbooks.py`, `olympus/evolve.py`, plus three new modules:
**`olympus/predictability.py`**, **`olympus/coupling.py`**, **`olympus/prefetch.py`**.

## Domain thesis

Colibri's prefetch stack is the cleanest expression of its engineering culture: a
*ladder* of predictors, each level measured against the one below it, each acting only
through provably harmless hints, each shipped off by default until the target workload
proved it out — and its best predictor reads **live state** (the router's post-attention
activations), not history. Olympus's translation is exact once the units are converted:
Colibri predicts *which expert weights the next layer will need* to hide **disk
latency**; Olympus predicts *which specialist, tool, and context page the next step will
need* to hide **retrieval latency and token-assembly time**, where the scarce resources
are **API concurrency slots, rate limits, and dollars** instead of disk bandwidth and
page cache. Olympus starts with one structural advantage Colibri never had — **Athena
already emits a dependency graph**, so a large share of "prediction" is *certainty*
(the plan's later steps are known at plan time and can be warmed deterministically) —
and one structural discipline Colibri had to engineer around: because Olympus stages
speculative artifacts *out of band* and injects them only at demand time, the
displacement bug class that #441/#490 guarded against is **impossible by construction**
rather than guarded by invariant. Everything in this domain ships in Colibri's order:
measurement first (`predictability.py`, zero behavior change), offline mining second
(`coupling.py`, pure), warm-up actions last (`prefetch.py`, default-off, harmless-only,
auto-disabling) — and every level's live hit/waste record accumulates into the
per-deployment workload evidence that `docs/MOAT_ANALYSIS.md` says is the only kind of
asset worth building.

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| `SPEC` willneed of previous routing | default-on WILLNEED of the last token's expert set (§7.5) — the cheap recency heuristic, 41.3% recall baseline | **absorb-principle** | `prefetch.py` baseline predictor (`last_step`), `predictability.py` baseline row |
| `PILOT` / `PILOT_TWO` router-state prediction | predict layer L+1's top-K from L's post-attention state; 71.6% recall; shared-expert-corrected state +2.3% (§7.5) | **redesign** | new `olympus/prefetch.py` reading `trace.py` live state + Athena's plan graph; `coupling.py` tables |
| `PILOT_REAL` + eviction guards | real loads into next layer's LRU under generation barrier + mutex; misprediction never fatal; speculation may not evict a warm resident (#441/#490) | **redesign** | `prefetch.py` staging cache (out-of-band by construction), slot/budget yield guards, generation tags |
| Ring-buffer hint architecture | lock-free 1P/1C ring → one detached I/O thread; inline fadvise measured 0.5 ms × 169k = +92 s (§7.5, §3.4) | **absorb-principle** | `prefetch.py` bounded drop-oldest hint queue + single daemon worker |
| `COUPLE` offline co-activation tables | `route_pairs.py` mines `ROUTE_TRACE` dumps → `.coli_pairs`; median lift 1.8×, p99 40×; +3.6..+9.4 pp; transfers across workloads (§7.5, §19.2) | **new-subsystem** | new `olympus/coupling.py` mining `trace.py`/`trajectories.py`, run from `heartbeat.py` |
| `LOOKA` predictability instrumentation | pure measurement: a 4-predictor routing-predictability report printed at exit; zero behavior change (§7.5, §18) | **new-subsystem** | new `olympus/predictability.py` over recorded traces; `olympus predictability` CLI |
| `PREFETCH` default-off honesty | real parallel prefetch shipped default-off because measurement showed it superfluous once PIPE existed (§7.5); `coli plan` emits knobs *with reasons* (§17.2) | **absorb-principle** | `OLYMPUS_PREFETCH` default off; `evolve.py` telemetry + bounded auto-disable; `DEFERRED.md` eulogies |
| Negative result: GPU-staging prefetch | 70–79% recall, still a net loss — staging contended with demand streams on PCIe; "revisit with dedicated streams" (§10.4) | **absorb-principle** | contended-resource doctrine in `prefetch.py` (idle-slot-only, yield-on-demand, cancellation); recorded in `DEFERRED.md` style |
| Plan-graph deterministic prefetch (**beyond Colibri**) | — (Colibri has no plan; every future need is probabilistic) | **new-subsystem** | `prefetch.py` consuming Athena's dependency graph + `playbooks.py` steps |

Boundary with `docs/absorption/03-speculation.md`: that domain owns *executing*
speculative work (draft answers, speculative tool calls — things that produce candidate
outputs and cost verification). This domain owns *warming* — strictly side-effect-free,
output-free preparation. The line is: **if the action's result could ever be shown to a
user or verifier, it belongs to 03; if it only makes a later demand cheaper/faster, it
belongs here.** The synthesizer should hold that line (tension recorded below).

---

## R1. LOOKA — predictability instrumentation before predictors

**1. What Colibri does.** `LOOKA` is a pure measurement mode (§7.5, §18): during a
normal run it scores four candidate predictors of next-layer routing (marginal heat,
previous-token, router-state, coupled) against what the router actually chose, and
prints a predictability report at exit. It changes nothing about execution; it exists so
the team knew *before* building PILOT that router state carried 71.6%-recall signal and
that previous-token heuristics were stuck at 41.3%.

**2. Why it exists.** Building a predictor is expensive; building one for an
unpredictable stream is pure waste. LOOKA converts "should we build prefetch?" from an
opinion into a table.

**3. How it works internally.** Passive counters keyed by predictor × layer, updated in
the decode loop where routing decisions are already in hand; report emitted once at
exit. Same architectural family as `DISK-CLASS` (§18): instrumentation whose
byte-identity-when-off is provable by construction because it only reads state the hot
path already produced.

**4. Strengths.** Zero risk (no behavior change); answers the *ordering* question (which
predictor family is worth productionizing); the four-predictor ladder gives an honest
baseline so a fancy predictor must beat a cheap one, not zero.

**5. Weaknesses & trade-offs.** It is exit-time and per-process — no accumulation across
runs, no per-workload segmentation, no confidence intervals; a short run gives a noisy
report the operator may over-read. It measures recall only, not the *value* of a hit
(all experts cost the same 19 MB; not all Olympus prefetches save the same tokens or
milliseconds). And it lives inside the engine, so adding a fifth predictor means
touching the hot loop.

**6. Security implications.** None in Colibri. In Olympus the analog reads recorded
traces, which contain user inputs — so the report must aggregate (counts, recalls),
never quote trace content; it runs locally over `MEMORY_DIR` and makes no model calls.

**7. Scalability implications.** Olympus's version is *offline* over persisted traces
(`memory/traces/*.jsonl`, the same stream `trajectories.trace_trajectories()` reads), so
it scales with disk, not with the hot path, and can segment by task type, user, model,
and time window — everything the exit-time report could not.

**8. Performance implications.** Zero on the request path. The offline scan is bounded
(cap files scanned per invocation, like `sleeptime._MAX_MEMS_SCANNED`).

**9. Maintainability implications.** A pure module with no model calls and no I/O
besides reading JSONL is the cheapest kind of code Olympus owns (house style:
`dytopo.py`, `toolselect.py`). Predictors are pure functions `(history, state) →
ranked candidates`, so adding one is a unit-tested function, not a hot-loop edit.

**10. How Olympus should redesign it.** Ship **`olympus/predictability.py` first, alone,
before any prefetch code exists** — the Colibri ordering is the whole point. It replays
recorded decision streams and scores a fixed predictor ladder on three questions:

- **next-specialist**: given the turn's route/plan decisions so far, predict which
  specialist runs next (ground truth: the `decisions` records `trace.py` already
  persists — the ROUTE_TRACE analog).
- **next-tool**: given a specialist's tool-call sequence so far, predict the next tool.
- **next-context**: given the request and step, predict which memory/document pages
  `recall.py`/`docrag.py` will retrieve at the next step.

Predictor ladder (each must beat the one above it to justify existing, mirroring
marginal-heat < previous-token < router-state < coupled):

1. `static_prior` — global frequency (Colibri's marginal heat).
2. `last_step` — repeat the previous step's choice (Colibri's SPEC/previous-token).
3. `coupled` — the offline coupling table (R5).
4. `plan_graph` — Athena's dependency graph where one exists (the "free oracle";
   expected recall ≈ 1.0 for planned steps — reported separately so it doesn't flatter
   the probabilistic predictors).

**11. Final Olympus architecture.**
- Module: `olympus/predictability.py`. Pure functions:
  `report(streams=("specialist","tool","context"), *, days=30, user=None) -> dict`
  returning per-stream `{predictor: {"recall@1": .., "recall@3": .., "n": ..}}`;
  `record_rows()` iterates `config.MEMORY_DIR / "traces"` with the same tolerant JSONL
  parsing as `trajectories._traces()`.
- CLI: `olympus predictability [--days N] [--json]` (registered like `journey`/`scores`).
- Heartbeat: none needed — this is operator-invoked, or invoked by `coupling.py` (R5) as
  its own go/no-go check.
- Env: none. It is always safe; there is nothing to gate.
- Data model: no new persistence — it reads what `trace.py` already writes. One
  addition to `trace.py`: ensure tool-call events carry the tool name in the decision
  record (they already carry stage + agent), a one-line enrichment.

**12. Why the Olympus approach is superior.** Colibri's LOOKA is a per-run exit report;
Olympus's is a queryable, segmentable measurement over the full accumulated trace
history — and because it runs offline it costs the hot path literally nothing, which is
stronger than "byte-identical when off": there is no "on" state in the request path at
all. It also produces the go/no-go artifact the ROADMAP's gate-cost rule (F8) demands:
prefetch work below is *forbidden* to proceed for a stream whose measured recall@3 for
the best non-plan predictor is under a stated floor (see R7).

---

## R2. SPEC — the previous-step heuristic as the honest baseline

**1. What Colibri does.** `SPEC` (default on, §7.5) issues WILLNEED readahead for the
previous token's expert routing — betting the next token reuses much of the last
token's expert set. Cost: a hint syscall; benefit: page-cache warmth; measured recall
41.3%.

**2. Why it exists.** It is the cheapest possible predictor with a nonzero hit rate, and
because a WILLNEED hint is *advisory* (the kernel may ignore it; nothing is evicted
deterministically, nothing can break), it is the only level safe enough to default on.

**3. How it works internally.** After each token's routing, hint the same
(layer, expert) set for the next token via `st_prefetch` willneed (§4.1); on Windows,
emulated by fire-and-forget overlapped reads (§11.3).

**4. Strengths.** Nearly free; harmless by construction (advisory); establishes the
recall floor every smarter predictor is measured against; degrades to a no-op on
filesystems where fadvise is a no-op (WSL/9p detection, §26.11).

**5. Weaknesses & trade-offs.** 41.3% recall means most hints are wasted; it can only
exploit *temporal locality*, which MoE routing has less of than caches assume; and an
advisory hint's benefit is invisible without the DISK-CLASS instrumentation to attribute
it — a feature that "can't hurt" but also can't prove it helps invites cargo cult.

**6. Security implications.** None for Colibri. For Olympus, the analog ("warm what the
last step used") touches only artifacts already loaded this run — no new data crosses
any boundary, so it is the one predictor sovereign mode never needs to inspect.

**7–8. Scalability & performance implications.** The Olympus analog is keeping the
*last step's* retrieval artifacts staged for the next step: the chunk-index entries
`docrag.py` just scored, the `annindex` neighborhoods just walked, the specialist's
skill pages just loaded. These are all local memory/disk — zero tokens, zero egress,
microseconds. The one caution is memory: staged artifacts must live in a bounded LRU
(count + byte cap), or "free" warmth becomes RSS creep — Colibri's RSS guard (#403)
lesson applied preemptively.

**9. Maintainability implications.** Trivial if it lives inside `prefetch.py` as the
`last_step` predictor rather than as scattered per-module caches — one staging cache,
one eviction policy, one metric surface (`docrag.py` already keeps a per-user chunk
cache; the staging layer sits above such module caches and never duplicates their
bytes, holding references/keys, not copies).

**10. How Olympus should redesign it.** Keep it as the **default-on level of the
ladder** exactly as Colibri does — but only after `predictability.py` confirms the
temporal-locality assumption on real Olympus workloads (plausible: consecutive plan
steps in one run often hit the same documents/memories; unknowable without measuring).
Its whole implementation is "don't drop what the previous step demand-loaded until the
run ends or the byte cap binds."

**11. Final Olympus architecture.** Inside `olympus/prefetch.py`: a per-run
`StagingCache` (dict keyed by `(kind, key)` → reference + generation tag + bytes,
LRU-bounded by `OLYMPUS_PREFETCH_STAGE_MB`, default 64). The `last_step` predictor is
implicit: demand-loaded artifacts enter the staging cache automatically at zero cost.
Hit/miss counters flow to `evolve.log_event("prefetch", "stage", {...})`.

**12. Why the Olympus approach is superior.** Colibri's SPEC could never verify its own
benefit without a separate instrumentation subsystem; Olympus's staging cache *is* its
own instrument — every injection at demand time is a counted hit with a measured
latency delta (time saved vs a cold retrieval, sampled). And where WILLNEED's effect
depends on kernel mood, a staged reference is deterministic: if it's in the cache, the
step gets it.

---

## R3. PILOT / PILOT_TWO — predicting the next need from live orchestration state

**1. What Colibri does.** PILOT (§7.5) predicts layer L+1's top-K experts from layer
L's **post-attention hidden state** run through L+1's router — live state, not history —
achieving 71.6% recall vs 41.3% for previous-token heuristics; PILOT_TWO refines the
state with the shared-expert correction for +2.3%. Predictions become hints (fadvise) or
real loads (R4).

**2. Why it exists.** The insight: the best predictor of what a system will need next is
*the system's own current state pushed one step forward*, not statistics about its
past. The router for L+1 already exists; running it early on an approximate input is
nearly free compared to a 19 MB disk miss.

**3. How it works internally.** During layer L's compute, take the residual stream,
apply L+1's router matmul (tiny), take top-K, enqueue hints on the ring (R4). Never
changes routing — PILOT is explicitly the prefetch-side lever; CACHE_ROUTE (§6.3) is the
routing-side one, and the analysis doc is careful to keep them distinct.

**4. Strengths.** Highest recall of any online predictor; costs one small matmul; reads
state that exists anyway; strictly decoupled from correctness (a wrong prediction wastes
a read, never changes an answer).

**5. Weaknesses & trade-offs.** The predictor is architecture-specific (it *is* the
model's router — nothing transfers to another model family). Recall is bounded by how
much the post-attention state at L resembles the true input to L+1's router — a
fundamental approximation error that no engineering removes. It predicts exactly one
layer ahead (PILOT_TWO's +2.3% shows deeper corrections yield diminishing returns), so
its latency-hiding window is one layer's compute time. And it needed a dedicated worker
thread plus the ring to be affordable at all (R4).

**6. Security implications.** None in-engine. In Olympus the equivalent must be designed
carefully: "predict the next need from live state" means reading the current plan,
the current step's partial output, and routing telemetry — and then *acting* (warming).
The acting side is where all the risk lives, and it is confined in R4. The predicting
side must never call a model on untrusted-derived state in a way that can steer
warm-ups toward attacker-chosen targets: predictions are computed by **pure code over
structured state** (plan graph nodes, tool names, retrieval keys), never by asking an
LLM "what should I prefetch?" — an injection-resistant design decision, not a
performance one.

**7. Scalability implications.** Olympus's per-step state is tiny (a plan graph, a few
decision records), so the predictor is O(1) per step. The scaling dimension is breadth:
many concurrent runs each wanting warm-ups compete for the same worker and the same
provider concurrency — handled by R4's contention rules.

**8. Performance implications.** What is actually worth hiding, in measured Olympus
units: (a) **context assembly** — `recall.py` read-path is pure Python and fast, but
`docrag.retrieve` on a changed corpus re-embeds via a network embeddings call
(`embed.py`), 100 ms–2 s; (b) **ANN index build/load** (`annindex.py` persistent HNSW)
— amortizable off the turn path; (c) **connection warm-up** — TLS + HTTP setup to a
provider not recently used, 50–300 ms; (d) **provider prompt-cache warmth** — an
Anthropic cache hit vs re-processing a large system prompt is both latency and money
(`OLYMPUS_CACHE_TTL` already exists in `config.py`). Each is a real, measurable saving
per plan step; none touches output correctness.

**9. Maintainability implications.** The predictor set must stay small and pure. The
Colibri trap to avoid: PILOT works because the "router" was a ready-made predictor the
architecture handed over. Olympus's ready-made predictor is **Athena's plan graph** —
already computed, already trusted, already the thing the executor walks. The
maintainable design leans on it first and treats learned prediction (coupling tables)
as the supplement, not the core.

**10. How Olympus should redesign it.** Two levels, cleanly separated:

- **Level 0 — plan-graph prefetch (beyond Colibri; deterministic).** When Athena's plan
  commits, the *later* steps are known: which specialist, with what input dependencies.
  For every not-yet-runnable step, the warm-up set is computable with certainty:
  stage its specialist's skill pages (`skills` index entries), pre-run
  `recall.recall_block`-equivalent retrieval and `docrag.retrieve` for the step's stated
  inputs, warm the connection for the model `learned_routing`/`config.capability_score`
  would pick. Colibri never had this: its every future need was probabilistic. Recall
  here is ~1.0 minus plan revisions — and *measured* plan-revision rate is exactly what
  `predictability.py`'s `plan_graph` row reports.
- **Level 1 — live-state prediction (the true PILOT analog; for what the plan doesn't
  state).** Zeus's routing decision before it's final, the next tool inside a
  specialist's loop, the next context page mid-step. Predictor = coupling table (R5)
  conditioned on live keys (task type, current specialist, last tool), i.e. Colibri's
  COUPLE feeding PILOT's ring. No LLM in the loop.

**11. Final Olympus architecture.**
- Module: `olympus/prefetch.py` — `predict(state) -> list[Hint]` where `state` is a
  small dataclass built from `trace.py`'s live run context (run id/generation, plan
  graph, current step, last tool, task type) and `Hint = (kind, key, source, gen,
  deadline)` with `kind ∈ {"context", "embed", "skills", "connection", "cache"}`.
- Integration: `orchestrator.py` calls `prefetch.on_plan(plan)` once per committed plan
  (Level 0) and `prefetch.on_step(state)` at step boundaries (Level 1). Both are
  fire-and-forget enqueues (R4); with `OLYMPUS_PREFETCH` unset both return immediately —
  zero-behavior-change off state.
- Env: `OLYMPUS_PREFETCH` (default off; `plan` enables Level 0 only, `all` adds
  Level 1), `OLYMPUS_PREFETCH_KINDS` (default `context,embed,skills` — `connection` and
  `cache` are opt-in because they touch the network/billing).
- Sovereignty: every network-touching warmer (`embed`, `connection`, `cache`) funnels
  through the same egress choke point as demand traffic
  (`security.assert_egress_allowed`); in sovereign mode a remote-provider warm-up is
  refused by the same check that refuses the demand call — prefetch inherits the
  fail-closed guarantee instead of re-implementing it.

**12. Why the Olympus approach is superior.** Colibri's best predictor tops out at
71.6% because it approximates an unknowable (the next layer's true input). Olympus's
Level 0 predictor reads an *artifact of its own architecture* — the dependency graph —
and is right whenever the plan holds, which the system itself controls and measures.
Colibri teaches the method (live state beats history; use the predictor the
architecture already gives you); Olympus has a strictly better predictor to hand.

---

## R4. PILOT_REAL + eviction guards — acting on predictions without ever paying in correctness or hot state

**1. What Colibri does.** `PILOT_REAL=1` (§7.5) upgrades hints to **real loads** into
the next layer's LRU, under a two-part safety invariant: a generation barrier + mutex so
matmuls can never touch a half-loaded slot, and loads in non-fatal mode so a
misprediction can never kill the server (§7.2). The **eviction guard** (#441/#490) is
the hard-won rule: speculation may not evict a genuinely warm resident — a prior bug
let speculative loads displace hot experts and silently dropped ~100% of speculations'
value.

**2. Why it exists.** Hints only help if the OS cooperates; real loads guarantee the
bytes arrive. But a real load consumes a real cache slot and real bandwidth — so acting
on speculation created the two failure modes the invariants close: corruption
(half-loaded state observed) and displacement (speculation evicting certainty).

**3. How it works internally.** The PILOT worker performs the pread into a designated
LRU slot; the slot is published only after the generation-tagged barrier; eviction
candidates are filtered by warmth so a resident with recent demand hits is never the
victim; misprediction cost is bounded to wasted I/O.

**4. Strengths.** Converts prediction into guaranteed latency hiding; failure containment
is total (wrong = wasted read; never wrong answer, never crash, never colder cache than
before — after #441/#490).

**5. Weaknesses & trade-offs.** The guards exist *because the speculative and demand
paths share one resource pool* (the LRU, the disk queue). Every invariant here is the
price of that sharing. Real loads also consume the very bandwidth demand misses need —
Colibri controls this by giving PILOT its own io_uring ring (§7.6) and one worker, but
the tension is inherent. And "genuinely warm" needs a warmth oracle, which is one more
clock to maintain.

**6. Security implications.** This is where the Olympus redesign concentrates all its
care, because "act on a prediction" in an agent system is categorically more dangerous
than in an inference engine:

- **Harmless-by-construction warmer whitelist.** `prefetch.py` does *not* get access to
  the tool registry (`tools.py`), the action spine, or `operator.py`. It owns a closed,
  hardcoded set of warmer functions — stage retrieval results, compute embeddings for
  known-local text, open/keep-alive a provider connection, refresh a prompt-cache
  prefix. There is no code path from a Hint to an arbitrary tool call; the security
  gate is not asked to screen speculative actions because speculative actions that
  would need screening **cannot be expressed**. (Compare Colibri: "a misprediction must
  never kill the server" → Olympus: "a misprediction must never *do* anything.")
- **No speculative fetching of model-suggested URLs.** A web fetch on a predicted URL is
  a beacon (leaks intent/timing to an external host) and an injection amplifier
  (prompt-injected text steering the prefetcher). Speculative `web_fetch` is
  **rejected**, not deferred — `webctx.py`/`webplan.py` fetch on demand under the
  SSRF-pinned path, never speculatively. Recorded as a conscious *differentiate*: Colibri
  prefetches from a local disk it trusts; Olympus's "disk" (the web) is adversarial.
- **Trust labels survive staging.** A staged artifact carries the same
  provenance/sensitivity it would have if demand-loaded (`security.wrap_untrusted` is
  applied at injection time, not skipped because the fetch was early).

**7. Scalability implications.** The contended resources are `OLYMPUS_MAX_CONCURRENT_CALLS`
slots, provider rate limits, and the run/daily budgets (`OLYMPUS_RUN_BUDGET_USD`,
`OLYMPUS_DAILY_BUDGET`). The displacement guard translates directly: **speculative work
may only use idle capacity and must yield instantly** — a network warmer runs only when
concurrency slots are free, is tagged cancellable, and is dropped (not queued ahead)
the moment demand work arrives. This is Colibri's "separate ring for PILOT" made
stronger: not a separate queue into the same disk, but a strict-priority-zero class
that cannot delay demand at all.

**8. Performance implications.** Two guards make waste bounded: a per-run speculative
spend cap (`OLYMPUS_PREFETCH_BUDGET_USD`, default **0** — only free warmers run unless
the operator raises it; embeddings and cache writes are the only dollar-costing
warmers), and deadline/generation expiry (a Hint older than its step, or from a
superseded plan generation, is discarded unexecuted — the generation-barrier analog:
stale speculation is never *observed*, here never *executed*).

**9. Maintainability implications.** The out-of-band staging cache removes the entire
invariant burden Colibri carried: there is no shared LRU for speculation to corrupt or
displace, so there is no mutex discipline for future contributors to break. The
priority/cancellation logic is one bounded queue policy in one module.

**10. How Olympus should redesign it.** Adopt the *guard philosophy*, discharge the
guards structurally:

| Colibri invariant | Olympus mechanism |
|---|---|
| generation barrier (never observe half-loaded) | staged artifacts published atomically with a run/plan `gen` tag; injection only if `gen` matches current plan generation |
| eviction guard #441/#490 (never displace warm) | speculation writes only to its own byte-capped staging cache; prompt-budget assembly (`OLYMPUS_MEMORY_BUDGET`, `OLYMPUS_HISTORY_TOKEN_BUDGET`) never reserves tokens for staged content — staged pages compete at demand time under exactly the ranking `recall.py` would apply anyway |
| non-fatal speculative loads | warmers wrapped in `errors.capture("prefetch.<kind>", err)`; any failure = dropped hint |
| dedicated PILOT ring/worker | strict-priority-zero worker: runs only on idle slots, cancels on demand arrival |

**11. Final Olympus architecture.** In `olympus/prefetch.py`:
`class Warmers` with exactly five members (`stage_context`, `stage_docrag`,
`stage_skills`, `warm_connection`, `warm_prompt_cache`); `execute(hint)` dispatches by
`kind` with per-kind enable flags (`OLYMPUS_PREFETCH_KINDS`) and the budget check;
`inject(step) -> staged` is the demand-time seam `orchestrator.py` consults before
calling the retrieval modules cold. Counters per kind: `hits` (staged artifact used),
`waste` (expired unused), `yields` (cancelled for demand), `spend_usd` — logged via
`evolve.log_event("prefetch", "cycle", {...})` so the feature-evolution review sees it.

**12. Why the Olympus approach is superior.** Colibri needed #441 and #490 — two
production bugs — to learn that speculation must never displace certainty. Olympus
absorbs the lesson as architecture: the resource speculation uses (the staging cache,
idle slots, a zero-default budget) is *disjoint by construction* from the resources
demand uses, so the failure mode has no representation. And where Colibri's worst case
was wasted disk reads, Olympus's worst case is capped at `OLYMPUS_PREFETCH_BUDGET_USD`,
default $0.00.

---

## R5. COUPLE — offline co-activation tables mined from the system's own trajectories

**1. What Colibri does.** `COUPLE` (#176, §7.5, §19.2): `route_coupling_report.py` does
copula/Fréchet-bound dependence screening of cross-layer expert co-activation from
`ROUTE_TRACE` dumps, with **equal-budget prefetch simulations** (coupled scoring vs
marginal heat, depth 1–2, train/test transfer); `route_pairs.py` productionizes the
result into the `.coli_pairs` table (text header `COLIPAIRS 1 <n>`, §4.5) the engine
consumes at 1–2 layers ahead. Measured: median lift 1.8×, p99 40×, +3.6..+9.4 pp recall
over marginal heat, and — critically — the tables **transfer across workloads**.

**2. Why it exists.** Router-state prediction (PILOT) only sees one layer ahead;
co-activation statistics reach further and are nearly free at inference time (a table
lookup). Mining offline keeps the hot path clean and lets the statistics be *validated*
(train/test transfer, equal-budget simulation) before the engine ever consumes them.

**3. How it works internally.** Offline: dump routing traces → screen pair dependence →
keep pairs whose lift clears support/significance floors → emit a versioned table.
Online: on routing layer L's experts, look up coupled partners at L+1/L+2 and enqueue
hints on the same ring.

**4. Strengths.** The offline/online split is the deep design: heavy statistics run
where compute is free and mistakes are harmless; the runtime consumes a small, audited,
versioned artifact. Equal-budget simulation is honest evaluation (never "more prefetch
helped" but "this *scoring* beats that scoring at identical budget"). Transfer testing
guards against overfitting one workload.

**5. Weaknesses & trade-offs.** Tables go stale as workloads shift (Colibri's are static
files regenerated manually); pair mining is combinatorially limited (pairs, not longer
motifs); cold-start — a fresh deployment has no trace corpus; and a table mined from
polluted traces (speculative/draft routing counted as real — the Expert Atlas's
measured confounds, §19.1) silently degrades, so the miner must control provenance.

**6. Security implications.** Trajectory mining reads conversation-adjacent data. The
Olympus miner must (a) run locally with no model calls (pure counting), (b) store only
**structural keys** in the table — task-type tokens, specialist names, tool names,
document/memory *ids* — never content strings, so the coupling table is safe to back up
and even share across an org's instances; (c) exclude synthetic/replay rows exactly as
`routing_outcomes.gate_status()` already does, so test traffic cannot shape production
warm-ups (a poisoning vector otherwise: spam a pattern in replays, steer the
prefetcher).

**7. Scalability implications.** Counting pairs over capped windows of JSONL is linear
and boundable (`_MAX_TRACES_SCANNED` per cycle, resumable via a high-water mark). The
table itself is small (thousands of rows) and loads at startup like `benchmarks.json`.

**8. Performance implications.** Runtime lookup is a dict hit. The mining cost lands on
the heartbeat (idle time), the same placement as `sleeptime.py` — the "shift upkeep off
the user-facing critical path" precedent already in-tree.

**9. Maintainability implications.** Mirror `sleeptime.py`'s shape exactly: pure
selection/statistics core (unit-testable, deterministic), bounded caps as module
constants, `run()` heartbeat entry that returns quiet log lines, state in a store
namespace. No new infrastructure concepts.

**10. How Olympus should redesign it.** New module **`olympus/coupling.py`**:

- **Mine three co-activation streams** from `memory/traces` + `routing_outcomes`:
  `(task_type → specialist sequence)`, `(specialist → tool | next-tool bigrams)`,
  `(specialist+task_type → context page ids retrieved)`. Keep entries with
  `n ≥ MIN_SUPPORT` (default 10) and `lift ≥ MIN_LIFT` (default 1.5).
- **Validate before publish, Colibri-style**: split the trace corpus by time
  (train = older, test = newer — the honest split for drifting workloads) and publish a
  table only if test-set recall@3 beats the `last_step` baseline from
  `predictability.py` by a stated margin. A table that fails validation is written to a
  `rejected/` sidecar with its numbers — the negative result preserved, not deleted.
- **Version + fingerprint** the table (`{"version": 1, "mined_at": ..., "window_days":
  ..., "rows": {...}}` at `MEMORY_DIR/coupling.json`, atomic tmp+rename per ADR 0005);
  `prefetch.py` ignores a table whose schema version it doesn't know — the `COLIPAIRS 1`
  header lesson.
- **Cadence**: a heartbeat job (`OLYMPUS_COUPLE_EVERY`, default 7 d, 0 disables) beside
  `skill_curation` in `heartbeat.tick`; also invokable as `olympus coupling mine`.

**11. Final Olympus architecture.** `coupling.py` (pure core + `run()` heartbeat entry +
`load_table()` for `prefetch.py`); table consumed by R3 Level 1; mining outcomes logged
via `evolve.record("coupling", OK|DEGRADED, ...)`. No model calls anywhere in the
module.

**12. Why the Olympus approach is superior.** Colibri's tables are static artifacts a
maintainer regenerates by hand from deliberately-collected dumps; Olympus's regenerate
themselves on the heartbeat from traces the system already writes, are validated
against a time-split before they can influence anything, and — per the moat thesis —
are an **accumulating, deployment-specific asset**: six months of coupling tables mined
from *your* workload is exactly the kind of integral-over-time a competitor cannot
backfill (`docs/MOAT_ANALYSIS.md` Asset 1/3 pattern). Colibri proved the transfer
property; Olympus gets the stronger property that the table *is* the workload's
fingerprint.

---

## R6. The ring-buffer hint architecture — the hot path never pays for speculation

**1. What Colibri does.** PILOT's hints flow through a **lock-free 1-producer/1-consumer
ring buffer** to a single detached I/O thread (§3.4, §7.5), because doing the fadvise
inline was measured at 0.5 ms × 169k calls = **+92 s per 48 tokens** — the hint
mechanism itself would have cost more than it saved. PILOT gets its own io_uring ring
separate from PIPE's (§7.6) so speculative and demand I/O never share a submission
queue.

**2. Why it exists.** The prime directive of speculation: the *mechanism* of
speculating must be free on the critical path. A hint that blocks the decode loop is a
tax on every token to maybe save some tokens.

**3. How it works internally.** Producer (decode loop) writes hint entries into a fixed
ring, never blocking — full ring = dropped hint (correct semantics: a hint is
discardable by definition); consumer (the PILOT worker) drains and executes; generation
tags let a new token's hints invalidate a stale batch wholesale.

**4. Strengths.** Zero hot-path cost by measurement, not assertion; drop-on-full is the
right overload behavior for advisory work; the single-consumer design avoids all lock
contention; separation from demand I/O prevents priority inversion.

**5. Weaknesses & trade-offs.** A fixed-size ring drops the *newest* hints under burst
(arguably the most relevant ones); single consumer caps throughput (fine for hints,
a bottleneck if the consumer ever does slow work — which PILOT_REAL's real loads
are, hence its own uring); and lock-free SPSC code is a correctness liability in C that
Python simply doesn't need.

**6. Security implications.** None per se; in Olympus the queue is also the **audit
choke point** — every hint that will ever be executed passes one `enqueue()`, so
logging/refusing/rate-limiting speculation happens in exactly one place.

**7. Scalability implications.** One daemon worker per process (Colibri's "single
detached PILOT worker") is deliberately chosen over a pool: speculation parallelism is
capped at what idle capacity allows anyway (R4), and one worker makes "yield to demand"
a single flag check. Multiple gateways/processes each own their worker; no cross-process
queue (hints are process-local ephemera, never persisted).

**8. Performance implications.** In Python the enqueue is a bounded
`collections.deque.append` under a lock or a `queue.Queue.put_nowait` —
sub-microsecond; the "inline fadvise" mistake to avoid is doing retrieval/embedding
synchronously in `orchestrator`'s step transition "because it's quick" (an embeddings
HTTP call is Olympus's 0.5 ms fadvise — 100 ms–2 s, ×N steps). Drop policy: **drop
oldest** (unlike Colibri's drop-newest) — Olympus hints carry deadlines and older hints
are strictly less likely to still matter; this is a conscious improvement, cheap in
Python where the queue isn't lock-free.

**9. Maintainability implications.** ~40 lines: bounded deque + one
`threading.Thread(daemon=True)` + generation filter + counters. The invariant to
enforce in review: **no module may call a warmer directly** — everything goes through
the queue, or the zero-cost-when-off property dies by a thousand inline "quick"
warm-ups.

**10. How Olympus should redesign it.** Absorb the principle whole: `prefetch.enqueue()`
returns in O(1) always; with `OLYMPUS_PREFETCH` off it returns *before touching the
queue* (the byte-identical-when-off doctrine, §18's PROF additive-only guarantee); the
worker thread is started lazily on first enqueue and is marked daemon so it can never
block shutdown; hints carry `(gen, deadline)` and the worker discards stale ones before
executing.

**11. Final Olympus architecture.** In `olympus/prefetch.py`: `_QUEUE` (bounded deque,
`OLYMPUS_PREFETCH_QUEUE` default 64, drop-oldest), `_worker()` loop (pop → gen/deadline
check → idle-capacity check → `Warmers` dispatch → counters), `drain_for_tests()` for
deterministic tests. Dropped/stale/executed counts land in the same
`evolve.log_event("prefetch", "cycle", ...)` record as R4's hit/waste economics.

**12. Why the Olympus approach is superior.** Same guarantee (hot path pays nothing),
two upgrades Colibri's C constraints precluded: a smarter drop policy (oldest-first,
deadline-aware) and a single audit/refusal choke point that makes the security story of
R4 enforceable in one function.

---

## R7. PREFETCH default-off honesty + the GPU-staging negative result — the shipping discipline

**1. What Colibri does.** Two acts of measured humility. (a) `PREFETCH` proper — real
parallel prefetch loads — ships **default-off** because once PIPE overlapped demand
loads with compute, measurement showed bare WILLNEED-plus-PIPE made eager prefetch
superfluous (§7.5); the flag remains, with its eulogy, for workloads where it might yet
pay. (b) The 6×5090 experiment (§10.4) tried next-layer expert prediction with **GPU
staging**: the predictor was *good* (70–79% recall) and the feature still **lost net**,
because staging transfers contended with demand expert/attention streams for PCIe —
recorded as rejected-with-data, "revisit with dedicated streams." Related discipline:
`coli plan` emits every auto-tune knob **with a reason** (§17.2), and `env_for()` only
`setdefault`s — the user's env always wins.

**2. Why it exists.** The project's culture (§3.1): negative results are preserved as
opt-in flags with written eulogies, not deleted; features ship at the default that the
*measured target workload* justifies, not the default that flatters the feature.

**3. How it works internally.** Default-off env flags; measured justifications in
comments/docs; the auto-tuner recommends (with reasons) rather than forcing;
rejected-with-data experiments listed in the experiment write-ups.

**4. Strengths.** Prevents the most common prefetch failure in the wild: shipped-on
speculation quietly taxing every workload that doesn't match the benchmark. Keeps the
knowledge (the flag + eulogy) without the risk. Makes "revisit when X changes" a
recorded, condition-tagged decision instead of tribal memory.

**5. Weaknesses & trade-offs.** Default-off means most users never benefit even where it
would pay — Colibri partially mitigates with `coli plan`'s reasoned recommendations and
per-OS measured defaults (`PILOT_REAL=1` is defaulted on Windows because it was measured
there, §13). Honesty costs adoption; the mitigation is *automating the measurement* so
the default can be earned per deployment instead of guessed globally.

**6. Security implications.** Default-off is also the security posture: every warmer
class that touches network or money is individually opt-in (`OLYMPUS_PREFETCH_KINDS`),
so the exposure surface is exactly what the operator enabled, and `olympus status` can
print it (the sovereignty-mode proof-surface pattern).

**7. Scalability implications.** The GPU-staging lesson generalizes to Olympus's scarce
lanes: **high recall does not imply net win when speculation and demand share a
contended resource.** Olympus's PCIe is the provider rate limit / concurrency slot pool
/ daily budget. The design consequence is R4's strict-priority-zero rule — Olympus
builds the "dedicated streams" Colibri's post-mortem asked for, in the only form an API
client has: capacity that demand traffic cannot even see speculation using (idle slots
only, instant yield, zero-default budget).

**8. Performance implications.** The enable decision is per-deployment and per-stream,
driven by accumulated counters, not a global constant: `prefetch` reports, for each
hint kind, `hit_rate`, `median_latency_saved_ms`, `waste_usd`; the feature-evolution
review (`evolve.review()`, already on the heartbeat) surfaces the verdict in plain
language — the `PROF` "plain-language verdict naming the knob to turn" (§18) applied to
prefetch itself. An auto-disable guard (the #163 soft-guard pattern from the
speculation domain): if a kind's hit rate over the last `WINDOW` executed hints falls
below `FLOOR`, the kind disables itself for the rest of the process and logs why —
bounded self-adjustment of a non-security parameter, which is precisely the class
`evolve.py`'s doctrine permits.

**9. Maintainability implications.** Negative results get the house home:
`DEFERRED.md` entries with the measured numbers and the revisit condition (e.g.
"speculative web prefetch — rejected: beacon/injection surface + unmeasured benefit;
revisit only behind an operator-pinned allowlist *and* a measured next-context recall
≥ 70%"). Capability counts don't drift because `predictability.py` and `coupling.py`
are counted by `olympus capabilities` like every other module.

**10. How Olympus should redesign it.** Adopt as binding sequencing for this whole
domain:

1. **Phase 1 (build now):** `predictability.py` — pure measurement, zero risk. Gate:
   nothing; it is the gate.
2. **Phase 2:** `coupling.py` — offline mining + time-split validation. Gate: only
   publishes a table that beats `last_step` on held-out traces.
3. **Phase 3:** `prefetch.py` Level 0 (plan-graph warmers, local kinds only), default
   off. Gate to *recommend* enabling (never auto-enable): a shadow week — hints
   enqueued and *counted but not executed* — projecting hit rate ≥ 60% and
   latency-saved > 0; `olympus doctor`/`status` then prints the recommendation with
   reasons, `coli plan`-style.
4. **Phase 4:** Level 1 (coupling-driven) and the network kinds
   (`connection`, `cache`), each gated the same way per kind.
5. **Explicitly deferred (research spikes, bounded):** prompt-cache keep-warm economics
   (a heartbeat cache-refresh ping costs real money against a 5-minute-class provider
   TTL; almost certainly a *negative* result on low-traffic instances — measure on one
   busy instance for one week, then write the eulogy or the default); speculative web
   prefetch (rejected above, revisit condition recorded).

**11. Final Olympus architecture.** Config surface, all `config.py`-conventional:
`OLYMPUS_PREFETCH` (off | `plan` | `all`), `OLYMPUS_PREFETCH_KINDS`,
`OLYMPUS_PREFETCH_BUDGET_USD` (default 0), `OLYMPUS_PREFETCH_STAGE_MB` (64),
`OLYMPUS_PREFETCH_QUEUE` (64), `OLYMPUS_COUPLE_EVERY` (7 d). Verdict surfaces:
`olympus predictability`, `olympus status` (prefetch block: enabled kinds, hit/waste,
recommendation + reason), `evolve.review()` (health), `DEFERRED.md` (eulogies).

**12. Why the Olympus approach is superior.** Colibri's honesty is manual: a maintainer
measured, chose the default, wrote the eulogy. Olympus mechanizes the same honesty on
infrastructure it already runs — shadow-mode counters, heartbeat review, bounded
auto-disable, drift-gated docs — so the default-off discipline holds *per deployment
and per hint kind, continuously*, not once at ship time on the maintainer's box. And the
shadow-mode ledger itself accrues: a year of "what prefetch would have saved on this
workload" is comparative evidence in the MOAT_ANALYSIS sense, produced before the
feature ever risked a dollar.

---

## Open questions & research spikes

1. **Plan-revision rate (blocks Phase 3 sizing).** Level 0's value is bounded by how
   often Athena's committed plans actually execute as planned. `predictability.py`'s
   `plan_graph` row measures this from existing traces — run it on real deployments
   before writing any warmer. Bound: reuses recorded traces; zero API cost.
2. **Prompt-cache keep-warm economics (bounded spike, 1 week, one busy instance).**
   Warming a provider prompt-cache prefix trades cache-write/refresh cost against
   cache-read savings and TTFT; the break-even depends on traffic density and provider
   TTL pricing, and is plausibly negative for most instances (the PREFETCH-superfluous
   result reborn). Measure, then default accordingly; consult provider pricing docs at
   implementation time rather than hardcoding today's numbers.
3. **Does next-context predictability clear the floor at all?** Colibri's routing had
   71.6%-recall structure; Olympus's context-page stream may be far noisier (user turns
   are open-ended). If `predictability.py` shows next-context recall@3 < ~40% for every
   non-plan predictor, Level 1 context warming should be *skipped*, not built —
   the LOOKA discipline's entire point.
4. **Boundary arbitration with 03-speculation.** `03-speculation.md` reserves
   "speculative parallel tool-calls" as a spike under `speculate.py`/`treesearch.py`;
   this domain forbids prefetch from executing anything tool-shaped. The synthesizer
   must ratify the line proposed here (outputs vs warmth) so the two modules can't grow
   into each other.
5. **Heartbeat congestion.** `heartbeat.tick` already runs ~18 job families; adding
   `coupling` mining is cheap but the tick's serial structure is nearing the point
   where one slow job starves cadences — a domain-external observation the
   synthesizer should route to whichever domain owns scheduling.
6. **Cross-instance coupling-table federation (deliberately deferred).** Tables contain
   only structural keys, so org-level sharing (via `federation.py`) is *possible*; it is
   deferred until single-instance tables prove lift, and would need the same
   synthetic-row provenance controls before any shared consumption.
