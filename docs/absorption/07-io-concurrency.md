# Absorption 07 — Concurrency, Batching & Redundancy

**Colibri domain:** the PIPE lock-free generation-tagged worker pool (§7.6), io_uring batched
submission and the IOSQE_ASYNC lesson (§7.6), batch-union expert dedup (§6.3 FASE B),
the coalesced one-pread expert layout (§7.2, §5.5), O_DIRECT twin fds and the page-cache
reserve (§4.1, §7.2, §7.4), dual-SSD deterministic mirror routing with bandwidth probing plus
the multi-drive shard split (§4.1, §26.13), and continuous-batching ragged decode with its
admission control (§6.7, §9.1, §9.2).
**Olympus target:** council-scale concurrency — `olympus/orchestrator.py`
(`_dispatch_dag`, `_dispatch`, `canonicalize_parallel_since`), `olympus/subagents.py`
(`spawn_many`), `olympus/usage.py` (`slot()`), `olympus/proclock.py` (`lock`, `slot`),
`olympus/llm.py` (key-rotation ring, prompt-cache breakpoints), `olympus/backend.py`
(`_fallback_chain`, `_with_failover`, `complete_text_once`), `olympus/websearch.py`
(provider ladder, TTL cache, cooldowns), `olympus/scheduler.py` + `olympus/heartbeat.py`,
and `docs/SOVEREIGNTY.md`'s egress choke.

## Domain thesis

Colibri's concurrency layer exists because its scarce resource — NVMe bandwidth — is orders
of magnitude slower than its compute, so every design in this domain is a discipline for
*never paying for the same slow read twice* (batch-union dedup, coalesced layout), *never
letting a slow read block compute* (PIPE, io_uring, the IOSQE_ASYNC lesson), and *never
letting redundancy break cache locality* (the deterministic mirror hash, §26.13). Olympus's
scarce resource is the token-priced, seconds-latency LLM call, and its parallel dispatch
already exists (`orchestrator._dispatch_dag` runs Athena's dependency graph level-by-level
on real `ThreadPoolExecutor` threads, with `tr.canonicalize_parallel_since` keeping replay
order-stable) — but Olympus today has **no request coalescing** (two parallel branches that
fetch the same URL pay twice), **failover-only redundancy** (keys rotate on error in
`llm.complete`, but nothing *routes* across healthy equivalent members, so multi-key setups
idle their second key and randomly cold-start prompt caches), and **admission without a
queue** (`usage.slot()` blocks silently; there is no fair queue, no wait telemetry, no loud
refusal). This document absorbs Colibri's principles into three small, measured additions —
a singleflight coalescer, deterministic mirror routing over *equivalent* pool members, and
honest bounded admission — every one instrumented so its wins land in the trace ledger and
the calibration record, per `docs/MOAT_ANALYSIS.md`: the feature is copyable, the accumulated
per-deployment evidence of what overlap and coalescing actually save is not.

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| PIPE lock-free generation-tagged worker pool | ≤16 pthreads run miss preads while main thread computes; CAS cursor `(gen<<8)\|idx` kills ABA/torn batches; −18% disk service (§7.6) | **absorb-principle** | `orchestrator.py`, `subagents.py`, `trace.py` (dispatch epochs), `usage.py` |
| io_uring batched submission + IOSQE_ASYNC lesson | 64 loads/512 SQEs per one submit syscall; `IOSQE_ASYNC` forced so "async" reads never execute inline; strict ENOTSUP over silent fallback (§7.6) | **redesign** | `orchestrator.py` (overlap prefetch), `agent.py` (parallel tool-block execution), **new `olympus/coalesce.py`** (submission seam) |
| Batch-union dedup | unique experts of the whole batch computed once; each unique expert read from disk ONCE per batch (§6.3 FASE B) | **new-subsystem** | **new `olympus/coalesce.py`**, `websearch.py`, `web.py` fetch path, `proclock.py` (cross-process) |
| Coalesced one-pread expert layout | gate/up/down contiguous → one ~19 MB pread instead of 3; converter merges tensors (§7.2, §5.5) | **absorb-principle** | `llm.py` (`_cache_tools`, `_cache_control`), specialist context assembly in `orchestrator.py`/`specialists.py` |
| O_DIRECT + page-cache reserve | eager O_DIRECT twin fd (0.8→2.3 GB/s measured); mandatory 2.5 GB page-cache reserve; misaligned direct reads fail loudly (§4.1, §7.4) | **absorb-principle** | `websearch.py`, `web.py` (fresh-read bypass), `coalesce.py` (no-pollute policy) |
| Dual-SSD deterministic mirror + bandwidth probe; multi-drive split | byte-identical mirror validated by header memcmp; pure-hash `expert_route(layer,eid)` split by `COLI_DISK_WEIGHTS` or startup probe; fallback-on-error, never written; `COLI_MODEL_DIRS` shards across drives (§4.1, §26.13) | **redesign** | **new `olympus/mirror.py`**, `llm.py`, `backend.py`, `config.py` (ModelPool) |
| Continuous-batching ragged decode + admission control | one ragged row per KV slot, S≤512, one forward/step; bounded FIFO, per-slot fair admission, 429 + Retry-After, queue-wait headers (§6.7, §9.1, §9.2) | **redesign** | `usage.py`, `scheduler.py`, `heartbeat.py`, `web.py` (queue surface), `proclock.py` |
| *(beyond Colibri)* cross-process coalescing & DAG cancellation | — Colibri is one process; its CANCEL is per-request only | **new-subsystem** | `coalesce.py` + `proclock.py`; `orchestrator.py` (level-abort epochs) |

---

## C1. PIPE lock-free generation-tagged worker pool (§7.6) → honest concurrency & dispatch epochs

**1. What Colibri does.** A persistent pthread pool (≤16 workers) services expert-load misses
into distinct working-set slots while the single main thread does all matmuls. Job handoff is
a lock-free cursor whose CAS comparand is `(generation<<8)|index` — the generation tag makes
ABA reuse and torn batch state impossible. Wait strategy is tunable (`COLI_PIPE_BLOCK`,
spin vs condvar — #159: yield storms fight OpenMP). Measured −18% disk service time; default
ON only where measured to win (Windows).

**2. Why it exists.** Disk latency must overlap compute, but a naive queue shared between a
producer that reuses slots and consumers that complete out of order corrupts state exactly
when it matters (mid-batch). The generation tag is correctness armor for slot reuse.

**3. How it works internally.** Per-slot ready flags; workers CAS the cursor to claim jobs;
a new batch bumps the generation so stale claims fail the CAS; main thread spins or blocks
per config. See §7.6.

**4. Strengths.** True overlap with zero locks on the hot path; correctness by construction
(generation tags); measured, per-platform default; bounded worker count.

**5. Weaknesses & trade-offs.** Spin-wait burns cores that OpenMP wants (the #159 fight);
the pool is engine-global, so one saturating batch starves nothing else because there *is*
nothing else — a luxury Olympus doesn't have (heartbeat, web, CLI all run concurrently).
Lock-free code is expensive to maintain — justified at microsecond granularity, absurd at
Olympus's seconds-per-call granularity.

**6. Security implications.** None in Colibri (all data is local weights). In Olympus,
worker threads cross trust boundaries: `subagents.spawn_many` already re-binds
`memory.set_user(user)` per worker precisely because `ThreadPoolExecutor` doesn't copy
context — the Colibri analog of "a worker must never touch another batch's slot" is "a
worker must never run under another user's identity or credentials" (`config.active_settings`
inheritance in `subagents.spawn_tool`).

**7. Scalability implications.** Colibri caps at 16 workers because NVMe queues saturate.
Olympus's cap is `config.MAX_CONCURRENT_CALLS` (default 6, `config.py:681`) enforced twice:
per-process (`usage._SEMAPHORE`) and machine-global (`proclock.slot` striped flocks). That
double gate is *already better* than PIPE — it spans processes — but the DAG executor
separately hardcodes `max_workers=min(4, len(ready))` (`orchestrator.py:1232`), a second,
uncoordinated cap.

**8. Performance implications.** At LLM-call granularity (1–60 s), thread overhead is noise;
what matters is that the width caps compose sensibly. Today a 4-wide DAG level whose
specialists each make tool calls can momentarily want >6 slots and serialize invisibly
inside `usage.slot()` with no telemetry — latency appears as "slow model" instead of
"queued".

**9. Maintainability implications.** Olympus must **not** absorb the lock-free machinery —
`proclock.py`'s flock striping is the right altitude (kernel-arbitrated, crash-released,
80 lines). What it should absorb is the *generation tag as an idiom*: a monotone epoch that
makes stale work self-invalidating.

**10. Redesign for Olympus.** (a) **Unify the width knobs**: replace the hardcoded `min(4, …)`
in `_dispatch_dag`/`_dispatch` with `config.DAG_WIDTH` (`OLYMPUS_DAG_WIDTH`, default 4,
clamped ≤ `MAX_CONCURRENT_CALLS`), so the two caps are visibly one policy. (b) **Dispatch
epochs**: give each pipeline run a monotone epoch recorded in trace events (`tr.event("dag.level",
epoch=…)`); background work spawned by a level (prefetches, C2 below) carries the epoch and
is dropped on arrival if the run has moved past it — Colibri's generation tag, translated
from "don't write a stale slot" to "don't inject a stale result into a later level".
(c) **Queue-wait telemetry** (see C7): `usage.slot()` records wait duration into the trace
so overlap wins/losses are measurable, honoring the "byte-identical when off" doctrine —
telemetry only, no behavior change. (d) **No fake async**: keep real threads and blocking
SDK streams; do not introduce an asyncio layer that would pretend to overlap CPU-bound JSON
munging — Olympus's calls are genuinely I/O-bound, `ThreadPoolExecutor` genuinely overlaps
them, and that honesty is worth stating in code comments the way Colibri states its measured
defaults.

**11. Final Olympus architecture.** No new module. `olympus/config.py`: `DAG_WIDTH` from
`OLYMPUS_DAG_WIDTH`. `olympus/orchestrator.py`: both executors read it; `tr.event`s gain
`epoch` and `queue_wait_ms` fields. `olympus/usage.py`: `slot()` optionally returns wait
time via a `contextvars` slot the caller logs. Integration: Athena's plan is the producer,
`_dispatch_dag` levels are the batches, `canonicalize_parallel_since` remains the replay
guard for completion-order nondeterminism (already present at `orchestrator.py:1172,1236`).

**12. Why superior.** Colibri's pool protects a single process's slots; Olympus's version
protects identity, credentials, and replay stability across *processes* (heartbeat vs web vs
CLI — the exact race `proclock.py`'s ADR 0005 documents), and its epochs make speculative
background work safe to add later (C2) without a correctness tax now. Same principle, higher
trust altitude, ~30 lines of change.

---

## C2. io_uring batched submission & the IOSQE_ASYNC lesson (§7.6) → batched submission and overlap discipline

**1. What Colibri does.** On Linux it batches up to 64 expert loads (512 SQEs) into **one**
`io_uring_enter` syscall per block; it always sets `IOSQE_ASYNC` because a cold buffered read
otherwise executes *inline in the submit syscall*, silently destroying the overlap the ring
exists to provide; PIPE and PILOT get separate rings; incompatible layouts are refused with
ENOTSUP rather than silently falling back.

**2. Why it exists.** Per-read syscall overhead and — worse — accidentally-synchronous
"async" submission are both invisible until measured. The design collapses N submissions
into one and forces the kernel to actually defer.

**3. How it works internally.** Raw-syscall ring setup (no liburing), SQE batching per expert
block, io-wq worker caps, strict capability checks at startup. See §7.6.

**4. Strengths.** One syscall per block; the ASYNC flag encodes a hard-won lesson as a
default; strictness over silent degradation; separate rings isolate speculative (PILOT) from
demand (PIPE) traffic.

**5. Weaknesses & trade-offs.** Linux-only; raw syscalls are a maintenance liability;
strictness means some configurations simply refuse (mmap + URING). The deep trade-off
Olympus must translate: **batching adds latency to the first item to save overhead on the
rest** — right for prefill blocks, wrong for a single interactive read.

**6. Security implications.** For Olympus, the analog of "submission" is issuing tool calls
and LLM calls, and the ASYNC lesson has a security twin: work kicked off "in the background"
must not *actually* run inline on a thread that holds a user's active credentials longer
than intended, and speculative fetches must pass the same `security.assert_egress_allowed`
choke as demand fetches (sovereign mode must see one funnel — `websearch._request` already
routes through it; any prefetch path must too, never a second code path).

**7. Scalability implications.** The council's version of "one syscall per block" is **one
dispatch decision per DAG level** (already true) plus **parallel execution of independent
tool calls within one agent turn**. Today `agent.py`'s tool loop executes tool_use blocks
serially; a turn that requests three web searches pays 3× latency. Batching those is the
single largest untapped overlap in the pipeline.

**8. Performance implications.** Two measured opportunities: (a) *intra-turn tool
parallelism* — execute a turn's independent, read-only tool calls concurrently under the
existing width caps; (b) *cross-level overlap* — when Athena's plan shows step B depends on
step A but B's task names a fetchable input (a URL, a memory recall), start that I/O during
A's execution, epoch-tagged (C1) and coalesced (C3), so B's wall clock starts warm. Both
must ship with before/after latency benchmarks per the measurement culture; (b) is
speculative I/O and therefore must be **hint-only** — like PILOT, it may never change which
calls the plan makes, only their temperature.

**9. Maintainability implications.** Do not build a submission ring. The seam is
`coalesce.submit(key, thunk)` (C3): callers hand thunks to one place; the coalescer decides
inline-vs-pooled. The IOSQE_ASYNC lesson becomes a **tested invariant**: a unit test asserts
that `coalesce.prefetch()` returns in <50 ms regardless of thunk latency (i.e., prefetch
never executes inline), the exact failure mode io_uring had.

**10. Redesign for Olympus.** Adopt (a) now: in the agent tool loop, when a model turn
returns >1 tool_use block and all requested tools are in a read-only allowlist (web_search,
fetch_url, recall_memory — never action tools, never sandbox exec), run them via a bounded
pool and return results in request order. Refuse the optimization (serial as today) if any
block is a write/action tool — strictness over cleverness, Colibri's ENOTSUP posture.
Adapt (b) as a bounded research spike (see Open questions): prefetch only coalescer-cacheable
reads named verbatim in a pending step's task text.

**11. Final Olympus architecture.** `olympus/agent.py`: `_run_tools_parallel(blocks)` gated
by `OLYMPUS_PARALLEL_TOOLS` (default on; `0` restores serial), read-only allowlist sourced
from `security.py` (the complement of `security.ACTION_TOOLS` that `subagents._is_privileged`
already consults), width `min(len(blocks), config.DAG_WIDTH)`. Trace event
`tools.parallel {n, saved_ms_est}`. Prefetch (if the spike passes): `coalesce.prefetch(key)`
called from `_dispatch_dag` when scheduling a level, epoch-tagged. Replay: parallel tool
execution completes out of order, so tool results are re-assembled in block order before the
next model call — the request hash `replaystore.request_hash(params)` is then identical to
the serial ordering, keeping replay byte-stable by construction.

**12. Why superior.** Colibri batches to save microseconds of syscall; Olympus batches to
save *seconds* of serialized network latency — a bigger multiplier on a smaller change. And
where io_uring's strictness protects data integrity, Olympus's strictness protects the
governance perimeter: only provably read-only tools ever run concurrently, so parallelism
can never reorder approval-gated actions.

---

## C3. Batch-union dedup (§6.3 FASE B) → request coalescing across parallel branches

**1. What Colibri does.** Before computing a batch, it takes the union of all positions'
routed experts; each *unique* expert's weights are read from disk **once** for every position
that uses it. This is why prefill amortizes I/O and why speculative verify batches are
"nearly free on I/O" (§6.5).

**2. Why it exists.** In a batch of 64 positions selecting 8 of 256 experts each, overlap is
enormous; reading per-position would multiply the scarce resource (disk) by the batch size.

**3. How it works internally.** FASE B computes the union; FASE C/D resolve each unique
expert once (pin → LRU → miss) and apply it to all positions that routed to it. See §6.3.

**4. Strengths.** Exact (same bytes, fewer reads — placement decides speed, never answers);
composes with every tier above it; makes speculation economically viable.

**5. Weaknesses & trade-offs.** Union scope is one batch in one process — no cross-batch or
cross-process reuse (that's the LRU's job, a different mechanism with different staleness
semantics). Olympus's translation must decide the same split: coalescing (identical in-flight
requests share one execution) vs caching (completed results served within a TTL) are
different tools; `websearch.py` has the TTL cache (`_CACHE_TTL` 15 min, bounded at 256
entries) but nothing coalesces *in-flight* duplicates, and nothing covers `web.py`'s fetch
path or memory recalls.

**6. Security implications.** The sharp edge Colibri never faced: **coalescing across trust
boundaries is a data leak.** If BYOK visitor A's fetch of a URL (authenticated via A's
session? personalized results?) is served to visitor B, content crosses users. Olympus's
coalescer must key on `(user, tool, canonical_args)` for anything user-scoped, and only on
`(tool, canonical_args)` for globally-idempotent public reads (web search of a public query).
The classification must be conservative and live in one table in `security.py` review scope.
Egress: a coalesced execution runs the underlying fetch exactly once through the existing
`assert_egress_allowed` funnel — coalescing must never *skip* the check for followers (the
leader's check covers the single actual egress; followers receive data, not egress rights —
document this explicitly since sovereign mode's audit story depends on it).

**7. Scalability implications.** The win grows with council width: Athena's parallel branches
routinely issue near-identical searches ("market size for X" from Plutus and Peitho), and
`dytopo` swarm consultation multiplies it. Cross-process (*beyond Colibri*): heartbeat jobs
and web requests on the same machine can coalesce through a `proclock.lock`-guarded ledger in
`MEMORY_DIR`, the same pattern the usage ledger uses — Colibri never needed this because it
is one process; Olympus is constitutionally several.

**8. Performance implications.** Every coalesced hit saves a full provider round-trip and,
for LLM-mediated fetches (`llm.server_web_fetch`), real token spend. Expected hit rates are
unknown — which is exactly why the ledger records them: `coalesce.hit` / `coalesce.miss`
trace events accumulate into a per-deployment savings record (tokens + ms avoided), feeding
the calibration/evidence asset rather than a marketing claim.

**9. Maintainability implications.** One small module with one idiom (singleflight): a dict
of in-flight futures keyed by canonical request, a bounded result cache with per-class TTLs,
and a thread-safe `do(key, thunk)` — ~120 lines. It *replaces* the ad-hoc cache in
`websearch.results` rather than adding a second one (the TTL/cooldown logic moves behind the
same seam), so total cache implementations in the tree go from N scattered to 1.

**10. Redesign for Olympus.** Build `olympus/coalesce.py`: `do(scope, key, thunk, ttl)` —
if an identical `(scope, key)` is in flight, wait on its future; if completed within `ttl`,
return cached; else execute. `scope` is `user:<id>` or `global` per the classification table.
`prefetch(scope, key, thunk)` submits without waiting (C2), epoch-tagged (C1), and **never
runs inline** (the tested IOSQE_ASYNC invariant). Wire in: `websearch.results` (global scope
for public queries), the `fetch_url` tool path in `web.py`/`tools.py` (user scope by
default, global for allowlisted public hosts), `recall_memory` (user scope, short TTL).
Replay mode disables coalescing entirely (each recorded call must replay 1:1 against
`replaystore`; a coalesced second call would make the recorded run's call count diverge) —
fail closed to the recorded behavior, Colibri's strictness posture.

**11. Final Olympus architecture.** New `olympus/coalesce.py` (singleflight + bounded TTL
cache + cross-process ledger file under `MEMORY_DIR/coalesce/` guarded by
`proclock.lock("coalesce")` for the heartbeat-vs-web case). Env:
`OLYMPUS_COALESCE` (default 1), `OLYMPUS_COALESCE_TTL` (default 900 s, mirroring today's
`websearch._CACHE_TTL` so behavior with defaults is unchanged), `OLYMPUS_COALESCE_XPROC`
(default 0 — cross-process is opt-in until measured). Data model: cache entry
`{scope, key_hash, created, ttl, size, hits}`; trace events `coalesce.hit|miss|inflight`
with saved-ms estimates. Integration: security classification table in `security.py`;
sovereign mode unchanged (single funnel); Aletheia's verification fetches use `global` scope
freely (public evidence), specialists' user-context reads use `user` scope.

**12. Why superior.** Colibri dedups within one batch of one process for one resource;
Olympus dedups across council branches, pipeline stages, *and OS processes*, across every
read-only tool — and the trust-boundary scoping is a designed strength Colibri never needed.
Where Colibri's win is invisible (fewer preads), Olympus's is ledgered: the coalescer's
accumulated savings become auditable evidence of orchestration efficiency, an
accumulated asset in the MOAT_ANALYSIS sense.

---

## C4. Coalesced one-pread expert layout (§7.2, §5.5) → one-read context assembly & stable cache-prefix layout

**1. What Colibri does.** When an expert's gate/up/down tensors are contiguous in the shard
(offset-sorted contiguity check), it loads all three in **one ~19 MB pread** instead of
three; the OLMoE converter (`convert_olmoe_merged.py`) goes further and *rewrites the
container* so each expert is one tensor — layout is arranged at write time so the read path
is minimal at run time.

**2. Why it exists.** Three 6 MB reads cost more than one 19 MB read on NVMe (seek/queue
overhead); the fix is not a smarter reader but a better *layout*.

**3. How it works internally.** Contiguity check on tensor offsets; single pread into a slab;
`QT` views (`ESlot`) point into the slab. See §7.2, §3.3.

**4. Strengths.** Zero-cost at run time; the converter bears the complexity once; views keep
the in-memory model unchanged.

**5. Weaknesses & trade-offs.** Layout optimizations are invisible until violated — a
container written without the convention silently degrades to 3 reads (Colibri checks and
degrades gracefully). The principle transfers, not the mechanism.

**6. Security implications.** None new. Olympus's analog (prompt-prefix layout) touches the
prompt-injection surface only in that assembled context blocks must keep their existing
untrusted-data wrappers regardless of ordering.

**7. Scalability implications.** Olympus's "expert weights" are a specialist's working set:
system prompt + tool schemas + skill index + memory/relgraph/codegraph context blocks. Its
"disk read" is *input tokens billed*. The Anthropic path already places cache breakpoints on
the system prompt and tool array (`llm._cache_control`, `_cache_tools` — "billed once and
then read from cache"), which is exactly the coalesced-slab idea: the expensive bytes load
once. But cache hits require **byte-stable prefixes**, and context blocks assembled per-turn
(`orchestrator.py:314-316` concatenates `relgraph.context_block` + `codegraph.context_block`
into the message) sit *after* the breakpoints — correct today, fragile tomorrow: any future
edit that moves volatile content (timestamps, per-turn IDs) above a breakpoint silently
zeroes the cache hit rate with no functional symptom, the exact "silent 3-pread degradation"
failure mode.

**8. Performance implications.** Prompt-cache misses multiply input cost on every agent-loop
turn of every specialist — at council scale (13 specialists × multi-turn loops) this is the
largest single cost lever in the domain. A layout regression is worth catching in CI, not in
the monthly bill.

**9. Maintainability implications.** The absorb is a *convention plus a gate*, not code:
document the prefix contract (stable-first: system prompt, tool schemas; volatile-last:
per-turn context blocks, timestamps) and enforce it mechanically.

**10. Redesign for Olympus.** (a) Write the contract into `llm.py`'s module docstring and
`docs/` (it is currently implicit). (b) Add a **layout gate**: a unit test builds two
consecutive turns' `params` for a representative specialist and asserts the byte region
before each `cache_control` breakpoint is identical across turns — Colibri's contiguity
check, run in CI. (c) Surface measurement: `usage.record` already receives
`cache_read_input_tokens` (`llm.py:330`) but sums it into plain input; split the ledger so
cache-hit ratio per specialist is visible in `olympus usage` — a regression then shows up in
a number someone watches. No behavior change anywhere; this is pure "byte-identical when
off" observability plus a CI tripwire.

**11. Final Olympus architecture.** No new module. `olympus/usage.py`: add `cached_in`
column to the ledger records (additive, old records read as 0). `olympus/llm.py`: pass the
cache-read count through. Test: `tests/test_prompt_layout.py`. CLI: `olympus usage` gains a
cache-hit column. Env: none needed.

**12. Why superior.** Colibri optimizes a layout it fully controls; Olympus's layout
discipline defends a *provider-side* cache it can only influence — so the design is a
contract + gate + measurement rather than a rewrite, the correct altitude for an API client.
The measured cache-hit series per specialist also joins the comparative-evidence asset:
which provider's caching actually saves money on this workload is Asset 2 material.

---

## C5. O_DIRECT & the page-cache reserve (§4.1, §7.2, §7.4) → cache-bypass discipline

**1. What Colibri does.** Every shard gets an eagerly opened O_DIRECT twin fd (buffered
reads measured 0.8 GB/s vs 2.3+ direct in the VHDX case); streamed reads can drop pages
after use (`DROP=1`, `posix_fadvise(DONTNEED)`); the RAM budget reserves a mandatory 2.5 GB
for the page cache (without it, pread collapsed 800→180 MB/s); misaligned direct reads fail
loudly rather than corrupt.

**2. Why it exists.** A general-purpose cache can be worse than no cache for streaming
access patterns, and a cache squeezed to zero collapses everything else. Colibri chooses
*per read* whether the cache helps, and *budgets* the cache as a first-class citizen.

**3. How it works internally.** Twin fds per file; 4K-alignment contract; `cap_for_ram`
accounting; `st.h`'s pread-not-mmap doctrine keeps streamed pages out of RSS. See §4.1, §7.4.

**4. Strengths.** Measured, selective, honest about failure (misalignment errors, WSL/9p
"fadvise is a no-op here" warning).

**5. Weaknesses & trade-offs.** Direct I/O trades kernel convenience for alignment burden.
The transferable insight is the *taxonomy*: some reads want the cache, some pollute it, some
must bypass it for correctness (freshness).

**6. Security implications.** Freshness-bypass is a correctness *and* integrity control for
Olympus: Aletheia verifying a claim, or `webmonitor` checking a watched page, must not be
fed a 15-minute-old cached snapshot — a stale cache can launder a hallucination past the
verifier ("the page said X" when X was edited away). Bypass reads must still traverse the
egress choke and SSRF guard; bypass is about *cache*, never about *gates*.

**7. Scalability implications.** One-shot streams shouldn't evict hot entries: bulk reads
(Mnemosyne ingesting a transcript, a training run's benchmark fetches) should not write into
the bounded 256-entry coalescer cache at all — the `DONTNEED` analog — or a heartbeat cycle
flushes the interactive path's warm entries.

**8. Performance implications.** Small but real: cache pollution converts into repeated
provider calls exactly when the interactive user is waiting. The reserve principle maps to
the coalescer's cache bound being sized so background jobs can't monopolize it (per-scope
sub-bounds).

**9. Maintainability implications.** Three flags on one seam, not three mechanisms:
`coalesce.do(..., fresh=False, pollute=True)` covers the whole taxonomy.

**10. Redesign for Olympus.** Extend C3's API: `fresh=True` skips cache read *and* in-flight
join (a verification read must be its own observation — joining an in-flight fetch started
by the thing being verified would be circular), executes, and *does* update the cache
(freshest data helps followers). `pollute=False` reads through without writing (bulk/one-shot
callers). Wire: Aletheia's verification fetch path and `webmonitor.run_due` use
`fresh=True`; Mnemosyne/`docrag` ingestion uses `pollute=False`. Per-scope cache sub-bounds:
`global` and each `user:` scope get independent LRU bounds so no scope evicts another —
the page-cache reserve, translated.

**11. Final Olympus architecture.** Inside `olympus/coalesce.py` (no separate module):
`fresh`/`pollute` kwargs, per-scope bounds (`OLYMPUS_COALESCE_MAX_GLOBAL=256`,
`OLYMPUS_COALESCE_MAX_USER=64`). Callers: `aletheia` verification in `orchestrator.py`
(`_verify` path), `webmonitor.py`, `docrag.py`. Trace: `coalesce.fresh_read` events so the
audit trail shows verification never consumed cache.

**12. Why superior.** Colibri bypasses a cache to protect throughput; Olympus bypasses one
to protect *epistemics* — the verifier's independence from the cache the claim-maker warmed
is an integrity property no inference engine needed. Marked as the differentiated absorb:
same principle, promoted from performance to honesty infrastructure.

---

## C6. Dual-SSD deterministic mirror + bandwidth probing; multi-drive shard split (§4.1, §26.13) → provider mirror routing

*(Grouped honestly: the mirror and the shard split are both read-path distribution across
equivalent-or-partitioned storage; their Olympus translation is one mechanism over the model
pool.)*

**1. What Colibri does.** `COLI_MODEL_MIRROR` points at a second byte-identical copy
(validated by size + full header memcmp); a **pure deterministic hash** `expert_route(layer,
eid)` splits reads between drives by `COLI_DISK_WEIGHTS=<p>,<m>` or a startup O_DIRECT
bandwidth probe; on error it falls back to the primary; the mirror is never written; partial
mirrors are allowed. Determinism is the point (§26.13): prefetch and demand reads for the
same expert always hit the same drive, so each drive's page cache stays coherent with its
share. `COLI_MODEL_DIRS` separately shards *different* files across drives for capacity +
parallelism.

**2. Why it exists.** Two SSDs ≈ 2× bandwidth on the binding resource — but only if the
split doesn't double-cache everything on both drives. Hash routing gives load-splitting
*and* cache locality with zero coordination state.

**3. How it works internally.** Startup validation, per-read hash, probe-derived weights,
error fallback. See §4.1.

**4. Strengths.** Stateless; cache-coherent by construction; validated equivalence before
trusting the mirror; graceful partial coverage; measured weights.

**5. Weaknesses & trade-offs.** Static weights (probe at startup only); equivalence is
checked once, not continuously. For Olympus the mapping must be drawn carefully: Olympus
*has* failover (`backend._fallback_chain` across pool members; `llm.complete`'s key-rotation
ring on rate limit/quota — `llm.py:299-356`) but failover is *reactive*: the second key or
member does nothing until the first fails. A two-key operator gets 1× throughput and, worse,
naive round-robin (the obvious fix) would be *anti*-optimal: Anthropic prompt caches are
scoped per key/org, so random spraying cold-starts the cache everywhere — precisely the
double-caching pathology §26.13's determinism avoids.

**6. Security implications.** Mirror routing must respect Olympus's hard trust boundaries:
(a) **BYOK never mirrors** — `backend._fallback_chain` already refuses to switch a visitor's
credentials ("silently switching a visitor's request onto the operator's key … would leak
spend and context"); the mirror layer inherits the identical eligibility test (fingerprint
in the operator pool). (b) Mirror members must be **equivalence-validated**: same provider,
same model ID — the memcmp analog is a startup assertion that fingerprints differ only in
`api_key` (key-mirror) or in `(provider, base_url)` for an explicitly-declared same-model
pair (e.g., Anthropic direct + Bedrock Claude). Never hash-route across *different models* —
that would make answers depend on a hash, violating Colibri's own placement-never-decides-
answers doctrine. (c) Sovereign mode: mirror members pass the same egress filter; a remote
mirror of a local model is excluded before routing.

**7. Scalability implications.** Deterministic split key: `hash(user, specialist_role)` —
coarse enough that one conversation (and its cached prefix) stays on one member, fine enough
that load spreads across users and council roles. This is `expert_route(layer, eid)` with
(layer, expert) ↔ (role, user). Health probing: a lightweight periodic probe per member
(tokens-per-second and error rate over the last N real calls — *passive* probing from the
usage ledger, no synthetic spend) adjusts weights; a member in cooldown (the
`websearch._cooling` pattern, already proven in-tree) drops out of the hash ring and its
share falls to the primary — fallback-on-error, exactly.

**8. Performance implications.** For rate-limit-bound operators, N keys → ~N× sustained
council throughput *without* losing prompt-cache hits (the C4 measurement shows whether
locality held). For multi-provider same-model pairs, the accumulated per-member latency/error
series is Asset 2 material: measured evidence of which provider actually serves this
workload better, gathered as a side effect of routing.

**9. Maintainability implications.** One new small module; the routing function is ~15 lines
(stable hash → weighted ring). The dangerous part is interaction with replay: member choice
affects `replaystore.request_hash` only via `params` (model is in params; key/base is not),
so key-mirroring is replay-transparent, and cross-provider mirroring must record the chosen
member in the trace so replay pins it. Static-weight staleness (Colibri's weakness) is
removed: weights refresh from the passive ledger on a cadence.

**10. Redesign for Olympus.** Build `olympus/mirror.py`: `members_for(settings)` returns the
validated equivalence set (default: `settings.all_keys()` expanded — the ring `llm.complete`
already knows — plus explicitly-declared `OLYMPUS_MIRROR_MEMBERS` same-model pairs);
`route(settings, user, role)` deterministically picks a member by weighted hash;
`report(member, ok, latency)` feeds passive health. `llm.complete` consults `mirror.route`
to pick the *starting* key instead of always `keys[0]`, keeping its existing rotate-on-429
loop as the error path (adopt: fallback-on-error unchanged). `backend._with_failover` is
untouched — mirror routes among *equivalents* before a call; failover crosses *roles/models*
after errors; `complete_text_once` (blind compare) bypasses both, as it must.

**11. Final Olympus architecture.** New `olympus/mirror.py` (~150 lines). Env:
`OLYMPUS_MIRROR` (default 0 — off means byte-identical current behavior, keys[0] first),
`OLYMPUS_MIRROR_MEMBERS` (declared same-model cross-provider pairs),
`OLYMPUS_MIRROR_WEIGHTS` (manual override, the `COLI_DISK_WEIGHTS` analog; default =
passive-probe-derived). Data model: `MEMORY_DIR/mirror_health.json` `{member_fp: {calls,
errors, p50_ms, updated}}` written under `proclock.lock("mirror-health")`. Trace events:
`mirror.route {member, weight}`, `mirror.demote`. Integration: `llm.complete` (start-key
selection), `openai_compat._post` (same seam, mirrored behavior), `config.ModelPool`
(equivalence declaration), sovereign filter upstream, `olympus status` shows the ring.

**12. Why superior.** Colibri's mirror doubles bandwidth on one machine; Olympus's turns
idle redundancy (spare keys/providers that today exist only as failover) into throughput
*and* into a continuously-accumulating cross-provider health record — while adding two trust
guarantees Colibri never needed (BYOK isolation, model-equivalence-or-refuse) and preserving
the deterministic-locality insight that makes redundancy cache-friendly instead of
cache-hostile.

---

## C7. Continuous-batching ragged decode & admission control (§6.7, §9.1, §9.2) → council task batching & fair admission

**1. What Colibri does.** `step_decode_batch` decodes one ragged row per active KV slot
(S≤512, distinct KVState enforced, heavy validation) — all active conversations advance in
**one forward per step**, so the fixed cost (dense layers, expert loads via batch-union) is
shared. Upstream, the Python gateway's `GenerationScheduler` provides bounded-FIFO admission
(`COLI_MAX_QUEUE`, `COLI_QUEUE_TIMEOUT`), per-slot fair admission (no head-of-line blocking),
429 `queue_full`/`queue_timeout` with Retry-After, and `x-colibri-queue-wait-ms` telemetry;
prefill is serial with KV-prefix reuse and a loud `CONTEXT_EXCEEDED` refusal.

**2. Why it exists.** Interactive multiplexing on a machine where one forward costs seconds:
sharing the forward is the only way N users coexist, and honest admission is the only
alternative to silent collapse under load.

**3. How it works internally.** Ragged rows over KV slots; mux wire protocol (SUBMIT/CANCEL/
DONE/ERROR frames); scheduler thread + per-request queues in the HTTP layer. See §6.7, §9.

**4. Strengths.** True cost-sharing; loud, typed refusals over silent truncation; fairness
designed in; queue state observable end-to-end (headers + `/health` counters).

**5. Weaknesses & trade-offs.** Speculation is force-disabled in the mux (not ragged-safe) —
batching traded against per-stream optimizations. For Olympus the honest mapping matters:
Olympus **cannot batch at the forward level** (the provider owns the forward). What it can
batch is *task admission*: N independent council tasks (heartbeat cycles, scheduler jobs,
web requests, subagent spawns) sharing one machine-global budget of `MAX_CONCURRENT_CALLS`
slots. Today that budget exists (`usage.slot()`: process `BoundedSemaphore` + `proclock.slot`
striped flocks — a genuinely cross-process counting semaphore) but admission is **blind**:
FIFO-by-luck (flock wakeup order), no priority, no wait telemetry, and a 60 s `TimeoutError`
whose message names the lock, not the workload that held it. A background training round can
starve an interactive user invisibly — the head-of-line pathology `GenerationScheduler`
exists to prevent.

**6. Security implications.** Fair admission is a DoS control: on a public BYOK instance,
per-user fairness bounds how much of the machine-global budget any one visitor consumes
(today one visitor's parallel DAG can hold most of the 6 slots). Refusal must be loud and
typed (HTTP 429 + Retry-After on `web.py`'s endpoints; a readable notice in chat), never a
silent quality downgrade — Colibri's `CONTEXT_EXCEEDED` posture. Queue telemetry must not
leak other users' identities in error text.

**7. Scalability implications.** Two admission classes suffice for the actual system:
**interactive** (CLI/web/Telegram turns, and everything `_dispatch_dag` runs on their
behalf) and **background** (heartbeat: `scheduler.run_due`, `agentbeat`, training, audits).
Background acquires slots only when interactive demand leaves headroom (reserve R slots for
interactive — the 2.5 GB page-cache-reserve principle from §7.4, translated to slot
budgeting); a starving background job doesn't error, it defers to the next tick, which
`heartbeat.tick`'s per-job try/except cadence structure already tolerates naturally.

**8. Performance implications.** Interactive p50 latency stops depending on heartbeat phase
— today `OLYMPUS_TRAIN_EVERY` cycles measurably collide with users, it's just unmeasured.
The provider-side analog of batch economics (batch endpoints that trade latency for cost on
non-interactive work — eval runs, benchmark scoring) is a real lever for exactly the
background class, but its shape and savings must be verified against current provider docs
before design, not assumed — bounded as a spike below.

**9. Maintainability implications.** Extend `usage.py`/`proclock.py`, don't replace them:
the striped-flock semaphore is correct and crash-safe (kernel releases on death). Priority
needs only a two-tier acquire: background polls with a longer sleep and a headroom check
(count free slots via non-blocking probes) before contending; interactive contends
immediately. ~60 lines, no new lock discipline.

**10. Redesign for Olympus.** (a) `usage.slot(cls="interactive"|"background")`: background
acquires only while ≥`OLYMPUS_SLOT_RESERVE` (default 2) slots remain free, else sleeps/defers;
interactive unchanged. Heartbeat-side callers (`heartbeat.tick`'s job families, `scheduler.
run_due`, training) pass `background`. (b) Queue-wait telemetry: `slot()` records wait-ms;
`_dispatch_dag` levels log it; `web.py` returns `X-Olympus-Queue-Wait-Ms` and typed 429
`{error: "queue_full", retry_after}` when the wait bound trips — the Colibri header, name-
translated. (c) Loud refusal in chat surfaces ("⚡ Olympus is at capacity — queued 40s") via
the existing `report` channel. (d) Batch-endpoint offload for the background class is
designed only after spike S3 verifies provider capabilities and prices.

**11. Final Olympus architecture.** `olympus/usage.py`: class-aware `slot()`; ledger gains
`queue_wait_ms`. `olympus/proclock.py`: add `free_count(name, count)` (non-blocking probe
over the stripe files — trivial with LOCK_NB). Env: `OLYMPUS_SLOT_RESERVE` (2),
`OLYMPUS_QUEUE_TIMEOUT` (existing 60 s default made explicit), `OLYMPUS_MAX_CONCURRENT_CALLS`
unchanged. Surfaces: `web.py` 429 + headers; `olympus status` shows slot occupancy by class.
Integration: heartbeat/scheduler/train mark background; Zeus/Athena paths interactive;
trace events `admit.wait {ms, cls}` feed the latency evidence record.

**12. Why superior.** Colibri batches to share a forward it owns; Olympus can't — so it
absorbs the *other* half of §9, the part API clients usually skip: honest, fair, observable
admission. The result is stronger than Colibri's in one dimension: admission classes span
OS processes (flock stripes), where `GenerationScheduler` governs one gateway process, and
every queue-wait lands in a ledger that makes the next capacity decision a measurement, not
a guess.

---

## Open questions & research spikes

1. **S1 — DAG-level I/O prefetch (C2b).** Bound: 1 week. Instrument first (LOOKA-style,
   zero behavior change): log, for each dependent step, whether its task text names a
   fetchable key that a `coalesce.prefetch` during the prior level would have warmed, and the
   ms it would have saved. Build the prefetch only if the log shows ≥15% median level-latency
   savings on real traces; otherwise record the negative result in `DEFERRED.md` with the
   numbers — Colibri's PREFETCH-default-off eulogy discipline.
2. **S2 — Coalescer hit-rate baseline (C3).** Before enabling cross-process mode
   (`OLYMPUS_COALESCE_XPROC`), run in-process coalescing for two weeks and read the
   `coalesce.hit` ledger: if global-scope hits are <5% of tool calls, cross-process
   complexity is not justified; keep the flag off and say so.
3. **S3 — Provider batch endpoints for the background class (C7d).** Bound: 3 days.
   Verify current batch-API capabilities, latency envelopes, and pricing against live
   provider documentation (do not design from memory); prototype only for `olympus eval`
   scoring runs, the most latency-tolerant workload. Ship only with a before/after cost
   measurement on `benchmarks.json` runs.
4. **S4 — Mirror equivalence across providers (C6).** Key-mirroring within one provider is
   safe by construction; declaring Anthropic-direct + Bedrock-Claude "equivalent" needs an
   empirical check that answers are distribution-equivalent for governance purposes (run the
   golden evals on both, compare pass rates) before `OLYMPUS_MIRROR_MEMBERS` accepts the
   pair. Refuse undeclared pairs — strictness over silent heterogeneity.
5. **S5 — Parallel tool-turn ordering vs replay (C2a).** Confirm on real recorded runs that
   re-assembling parallel tool results in block order yields byte-identical
   `replaystore.request_hash` sequences vs serial execution (it should by construction; a
   test must prove it before `OLYMPUS_PARALLEL_TOOLS` defaults on).
6. **Open question — interaction with dytopo swarm caps.** Swarm consultation
   (`orchestrator._consult`) multiplies parallel calls; admission classes treat it as
   interactive today. If S2's ledger shows swarm rounds dominating queue waits, consider a
   third `deferrable-interactive` class — decide from the ledger, not in advance.
