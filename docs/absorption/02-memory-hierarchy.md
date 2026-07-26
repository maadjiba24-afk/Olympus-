# Absorption 02 — Memory & Context Hierarchy

**Colibri domain:** the 5-level expert storage hierarchy, LFRU scoring & hysteresis, PIN/AUTOPIN
learning cache (`.coli_usage`), REPIN live re-pinning, RAM budgeting / RSS guard / OOM refusal
(#305), the page-cache reserve, NUMA arenas, and mmap mode
(`docs/colibri-deep-analysis.md` §4.4, §7.1–7.4).

**Domain thesis.** Colibri's deepest idea is not "stream weights from disk" — it is that *placement
is an economics problem separated from correctness*: what lives in the fast tier only ever changes
**speed**, never answers, and the placement policy is **learned from accumulated usage**, guarded by
**honest budget arithmetic that refuses rather than degrades**. Olympus faces the exact same problem
one abstraction up: its scarce resource is not RAM bandwidth but **context-window tokens, prompt-cache
stability, and API dollars**, and its "experts" are wiki pages, skills, typed memories, and tool
schemas competing for a bounded prompt. Today Olympus assembles context from many good sources
(`recall.context_block`, `wiki.context_block`, `playbooks`, `relgraph`, `docrag` — see
`olympus/orchestrator.py` lines ~311–316 and ~975–980) but with **no unified budget planner, no
learned heat, and no refusal path** — each source self-limits and the sum is unplanned. This document
absorbs Colibri's tier/heat/guard triad into a native Olympus **context-economics subsystem**: two
small new modules (`olympus/ctxheat.py`, `olympus/ctxbudget.py`), heat fields on data Olympus already
writes, and every default-off feature byte-identical when off — Colibri's own instrumentation
doctrine, and the pattern `OLYMPUS_ANN` / `OLYMPUS_EMEM` already established. Crucially, the heat
ledger is a **per-user accumulated asset** (MOAT_ANALYSIS Asset 3: structural data locality) — the
one thing a fresh deployment cannot copy.

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| 5-level storage hierarchy | VRAM → pinned RAM → RAM LRU → page cache → NVMe (§7.1) | **redesign** — formalize the latent Olympus tier map: pinned prompt → working context → warm store → cold store | `orchestrator.py`, `memory.py`, `usermem.py`, `wiki.py`, new `ctxbudget.py` |
| LFRU scoring & hysteresis | `tier.h`: score `(heat<<8)\|recency`, 25%+4 hysteresis, periodic heat halving (§7.3) | **redesign** — token-cost-normalized LFRU for promotion/demotion; hysteresis re-targeted at prompt-cache-prefix stability | new `ctxheat.py`; heartbeat |
| PIN / AUTOPIN learning cache | `.coli_usage` cross-session selection counts; ≥5000 selections → confidence-scaled auto-pin (§7.3) | **new-subsystem** — per-user heat ledger + eval-gated auto-pinning | new `ctxheat.py`; `memory/` data files; `liveeval.py` gate |
| REPIN live re-pinning | LFRU swap of coldest-pinned for hottest-unpinned every n tokens at safe points (§7.3) | **redesign** — re-pin only at compaction/session boundaries (the prompt-cache-safe points) | `ctxheat.py` + `orchestrator._maybe_compact()` hook |
| RAM budgeting / RSS guard / OOM refusal | `cap_for_ram` slack accounting; startup refusal #305; runtime RSS guard #403 (§7.4) | **new-subsystem** — context budget planner with refusal-over-truncation + measured-vs-planned drift guard | new `ctxbudget.py`; `usage.py`; Athena plan integration |
| Page-cache reserve | mandatory 2.5 GB reserve (800→180 MB/s collapse without it) (§7.4) | **absorb-principle** — mandatory output/tool-result token reserve inside the planner | `ctxbudget.py` |
| NUMA arenas | selective `SYS_mbind` interleave of expert slabs only; blanket interleave up to 10× regression (§7.4) | **absorb-principle** (mechanics skipped) — selective locality over blanket sharding; codifies existing per-user namespacing | `memory.py`, `store.py` (doctrine, not code) |
| mmap mode | `COLI_MMAP=1` zero-copy page-cache views (§4.4, §7.2) | **absorb-principle** — reference-not-copy context: stable cached prefixes + retrieval handles instead of inlined blobs | `ctxbudget.py` assembly order; `docrag.py` |

Grouping note: PIN/AUTOPIN/REPIN are one learning-cache mechanism in Colibri and are absorbed as one
subsystem here, but rubric'd separately below where their failure modes differ; RAM
budgeting/RSS-guard/OOM-refusal and the page-cache reserve are one guard family and share a rubric
section for the reserve inside it (called out explicitly).

---

## 1. The 5-level storage hierarchy → the Olympus context/memory tier map

**1. What Colibri does.** Experts live in a speed-sorted hierarchy — VRAM → pinned RAM hot-store →
per-layer RAM LRU (`ecache`) → OS page cache → NVMe (primary + optional mirror) — and are promoted or
demoted between tiers by usage, with telemetry encoding per-expert tier + heat in the `EMAP` line
(§7.1). Placement only ever decides latency; the token stream is oracle-validated identical
regardless of where a weight was read from.

**2. Why it exists.** A 744B MoE cannot fit in 25 GB of RAM; but per token only ~11 GB of routed
experts change. A hierarchy converts "impossible" into "slow but exact," and warmth converts slow
into usable.

**3. How it works internally.** `expert_load_impl` does one coalesced ~19 MB pread per miss;
FASE C/D resolves each routed expert pin → LRU → working-set miss; end-of-block LRU promotion via
swap-buffer exchange; `PIN_GB`/`RAM_GB` size the tiers (§6.3, §7.2).

**4. Strengths.** (a) One uniform item shape (the 19 MB expert slab) makes tier accounting exact.
(b) The fidelity doctrine — placement is *never* allowed to change output — makes every placement
optimization safe to try. (c) Tiers are observable end-to-end (EMAP/Brain view), so placement quality
is a measured, debuggable property.

**5. Weaknesses & trade-offs.** The hierarchy is *anonymous*: items are identical-size blobs with no
semantics, so policy can only be frequency/recency — Colibri needed a whole research program
(expert atlas §19.1, route coupling §19.2) to bolt semantics back on. Tier boundaries are rigid and
machine-local; a second machine starts cold. And the hierarchy manages exactly one resource (bytes);
it has no notion that some residents are more expensive to *use* than others.

**6. Security implications.** Colibri's tiers hold weights (inert data); its threat model is corrupt
containers, handled at load (§4.1). Olympus's tiers hold **text that re-enters prompts** — a
promoted item is an *injection surface*, which Colibri never had to consider. Promotion must never
bypass the sanitization sink (`memory.save` → `security.sanitize_for_memory`) or the untrusted
envelope (`emem.Episode.render` → `security.wrap_untrusted`).

**7. Scalability implications.** Colibri scales tiers by adding RAM/VRAM. Olympus's fast tier — the
context window — is *fixed* per model; scaling means better selection, not bigger tiers. The warm
store already has caps (`usermem._MAX_MEMORIES=500`, `wiki.MAX_PAGES=200`) but nothing arbitrates
*between* stores when their combined context blocks compete for one prompt.

**8. Performance implications.** In Olympus the "tier latency ladder" is: pinned system prompt
(0 marginal cost when prompt-cached) → injected context blocks (paid every turn in input tokens) →
on-demand retrieval tools (`recall_memory`, wiki reads — paid in an extra tool round-trip) → cold
notes/traces (paid in search + read). Every misplaced item is either wasted spend (too hot) or an
extra round-trip and a worse answer (too cold).

**9. Maintainability implications.** Olympus already has the tiers *implicitly* — the danger is that
they stay implicit: today five `context_block()` providers self-limit independently (recall 
`MEMORY_RETRIEVAL_BUDGET_TOKENS`, wiki `budget_chars=1600`, etc.), so total context size is an
emergent accident. An explicit tier map with one arbiter is *less* code to reason about than five
uncoordinated budgets.

**10. How Olympus should redesign it.** Name the tiers and give them one arbiter:

- **T0 — pinned prefix**: identity/system prompt, specialist prompt, pinned skills & wiki pages.
  Stable across turns by design → prompt-cache-friendly (the Olympus analog of "resident in VRAM":
  ~free per use once warm).
- **T1 — working context**: conversation history since last compaction + this turn's context blocks
  (recall/wiki/playbooks/relgraph/docrag/emem). Paid per turn.
- **T2 — warm store**: typed memories (`usermem`), wiki pages, skills, facts — retrieved by
  `recall.retrieve`/`wiki.context_block` or on-demand tools. Paid on retrieval.
- **T3 — cold store**: dated notes (`memory.py` categories), conversation archives, traces,
  FTS5/`search.py` index. Paid only via explicit search.

The arbiter is the budget planner (rubric 5). Promotion/demotion between T0↔T2 is the learning cache
(rubrics 2–4). Compaction (`orchestrator._maybe_compact` + `recall.flush_slice`) is the existing
T1→T2 demotion path and stays exactly where it is — the redesign *names* it rather than replaces it.

**11. Final Olympus architecture.** No new storage; a tier *view* over existing modules:
`ctxbudget.plan()` (new module, rubric 5) takes the per-turn candidate set
`{(tier, source, item_id, est_tokens, heat)}` supplied by the existing providers and returns the
admitted set. `olympus context map` (new CLI verb on `cli.py`) prints the live tier map — the textual
analog of Colibri's Brain view: per item its tier, heat, size, and last-used age, sourced from
`ctxheat` (rubric 3). Data model: none new for this rubric; tiers are computed, not stored.
Integration: Zeus's context assembly in `orchestrator.py` calls the planner at the two existing
assembly sites; Athena's step prompts get per-step plans; heartbeat's dream (`wiki.dream_all`)
remains the T1/T2 consolidation engine.

**12. Why the Olympus approach is superior.** Colibri's hierarchy is anonymous and machine-local;
Olympus's items are *typed, provenance-carrying, per-user documents* (`usermem` rows carry
confidence, decay, provenance; wiki pages carry review horizons), so placement policy can use
semantics Colibri had to reverse-engineer with a probe atlas. And because the tier map is per-user
and portable (`memory export` — docs/MEMORY_FORMAT.md), warmth *travels with the user* instead of
dying with the machine.

---

## 2. LFRU scoring & hysteresis → token-cost-normalized promotion with cache-stable swaps

**1. What Colibri does.** `tier.h` scores every expert `(heat<<8)|recency` — frequency dominates,
recency tie-breaks — with **25%+4 hysteresis** against ping-pong and **periodic heat halving** so
ancient popularity decays (§7.3). The same policy drives LRU promotion and REPIN swaps.

**2. Why it exists.** Pure LRU thrashes under scanning workloads; pure LFU never forgets. LFRU with
hysteresis keeps genuinely hot experts resident while refusing to churn the pin set over marginal
score differences — churn costs disk bandwidth, the scarcest resource.

**3. How it works internally.** Every routing selection bumps persistent usage, decaying heat, and
LFRU recency (§6.3 FASE A); swaps happen only when the challenger beats the incumbent by the
hysteresis margin; heat halves periodically so the score is a leaky integrator, not a lifetime count.

**4. Strengths.** Frequency-dominant scoring matches a workload where the hot set is stable
(measured: profile quality beat capacity — 0.94–1.64 tok/s hot-first vs 0.29 heat-blind, §7.3).
Hysteresis is the unsung hero: it converts a noisy score into a *stable* placement, and stability is
itself a performance property.

**5. Weaknesses & trade-offs.** (a) Uniform item size: Colibri never has to ask "is this resident
worth its bytes?" because all experts cost ~19 MB — Olympus items range from a 40-token preference to
an 8,000-char wiki page (`wiki.MAX_PAGE_CHARS`), so raw heat is the *wrong* score; value density
(heat per token) is right. (b) The score is workload-global: one heavy week of a side project can
evict a year of steady preferences. (c) Hysteresis constants (25%+4) are tuned to disk economics and
meaningless for Olympus; ours must be tuned to prompt-cache economics. (d) Heat halving is wall-clock
scheduled ("periodic"), not principled; Olympus already has a better-founded decay — per-type
half-lives in `usermem.HALF_LIFE` (identity 3650 d, behavioral 30 d).

**6. Security implications.** A scoring function over attacker-influenceable counters is a
*promotion attack* vector: content that arranges to be retrieved often (e.g., keyword-stuffed text
that lexically matches everything) could climb into the pinned prefix, the highest-trust position in
every future prompt. Mitigations (designed in, not bolted on): heat counts only *distinct-turn* uses
(one bump per turn per item — Colibri's per-selection counting is fine for weights, wrong for text);
pin eligibility requires the item to already be in the *gated* store (passed `recall._gate` /
`security.sanitize_for_memory`); and auto-pinned content is flagged in `olympus context map` so a
human can see exactly what earned residency and why.

**7. Scalability implications.** Scoring is O(items) over sets already capped at hundreds per user
(`_MAX_MEMORIES=500`, `MAX_PAGES=200`, skills library) — trivially cheap, no model call, pure Python
on the hot path, exactly like `recall.retrieve`'s lexical scoring. No new scaling risk.

**8. Performance implications.** The real win is **prompt-cache stability**. Anthropic-style prompt
caching prices a stable prefix at a fraction of a cold one; a pin-set that churns every turn
invalidates the cache and *costs* money to "optimize." Hysteresis is therefore not a nicety but the
mechanism that makes learned pinning net-positive: swap only when the challenger's value density
beats the incumbent's by a configured margin (default 25%, honoring the ancestor), and only at safe
points (rubric 4).

**9. Maintainability implications.** One scoring function in one module (`ctxheat.score`) replaces
zero existing code (nothing does this today) and is pure + deterministic given injected `now` — the
`emem.reconstruct` pattern — so it is unit-testable without I/O or model calls.

**10. How Olympus should redesign it.** Score
`value = heat_decayed × importance × trust / est_tokens` where `heat_decayed` reuses the
half-life machinery (`usermem.effective_confidence` shape: `0.5 ** (age/half_life)`) applied to a
use-counter rather than a confidence; `importance` and per-type half-life come from the item itself
where typed (usermem rows), from frontmatter for wiki pages/skills. Hysteresis: a challenger must
beat the coldest incumbent by `OLYMPUS_PIN_HYSTERESIS` (default 0.25) *and* the pin set may change by
at most `OLYMPUS_PIN_MAX_SWAPS` (default 2) per safe point. Heat halving becomes the nightly
heartbeat applying the half-life decay lazily at read time (no write storm) — decay is a function of
timestamps, not a scheduled mutation, which also keeps the heat ledger append-mostly.

**11. Final Olympus architecture.** `olympus/ctxheat.py`:
`bump(user, kind, item_id, turn_id)` (dedup per turn), `score(user, kind, item_id, est_tokens, now)`,
`pin_set(user, budget_tokens, now)` (LFRU + hysteresis selection),
`swap_plan(user, current_pins, now)` (returns ≤ MAX_SWAPS swaps or `[]`). Data model:
`memory/users/<user>/context_heat.json` — **counters and ids only, never text** (see rubric 3) —
written via the tmp+`os.replace` atomic-publish idiom every module in `memory.py`/`usage.py` already
uses (ADR 0005). Env (all `OLYMPUS_*`): `OLYMPUS_PIN_HYSTERESIS`, `OLYMPUS_PIN_MAX_SWAPS`,
`OLYMPUS_HEAT_HALFLIFE_DAYS` (default: inherit per-type from `usermem.HALF_LIFE`, this var overrides
untyped kinds). Integration: `recall.context_block` and `wiki.context_block` call
`ctxheat.bump` where they already call `usermem.touch` (recall.py line ~388) — one-line additions.

**12. Why the Olympus approach is superior.** Colibri optimizes bytes-resident for uniform blobs;
Olympus optimizes *value per token* for typed items with owned metadata, reuses a decay model that is
already per-type-principled instead of a global halving clock, and re-targets hysteresis at the
resource that actually behaves like Colibri's disk: the prompt cache. Every Colibri weakness in
point 5 is addressed by data Olympus already stores.

---

## 3. PIN / AUTOPIN — the learning cache → the per-user heat ledger with an eval-gated autopin

**1. What Colibri does.** `.coli_usage` accumulates expert selections across sessions (atomic
tmp+rename); at startup, ≥5000 recorded selections triggers AUTOPIN with a **confidence-scaled
budget** — the engine literally gets faster the more you use it (§7.3, §26.6). `PIN=<stats>` /
`PIN=auto` let an operator pin from a frozen or live profile; `PIN_FILL` pads with everything
remaining.

**2. Why it exists.** Expert routing is heavily skewed and *stable per workload*; measured, profile
quality beats raw capacity (0.94–1.64 vs 0.29 tok/s on the same 150 GB tier). The best predictor of
tomorrow's hot set is yesterday's, so persist yesterday.

**3. How it works internally.** Every FASE A selection increments the persistent histogram;
startup ranks by usage, pins the top set within `PIN_GB`, scales the pin budget by how confident the
profile is (how many selections back it). Atomic writes were a hard-won lesson (the Windows CRT
rename bug silently starved the pipeline).

**4. Strengths.** (a) It is the purest *accumulated asset* in Colibri — value is an integral over
usage time, which is exactly MOAT_ANALYSIS's definition of a moat-class asset. (b) Confidence
scaling: a thin profile pins little, so a cold start can't mis-pin aggressively. (c) The data is
tiny, format-trivial, and survives crashes.

**5. Weaknesses & trade-offs.** (a) **Global, single-tenant heat**: one `.coli_usage` per model dir —
two users with different workloads poison each other's profile, and Colibri's own `warmup.ps1` had to
warm across "30 topic-diverse prompts" because single-topic warming *overfits the pin* (§17.3).
(b) **Unvalidated promotion**: AUTOPIN trusts the counter threshold (≥5000) with no quality check —
fine when placement can't change answers, unacceptable when placement *is* prompt content.
(c) Usage saves "only on clean completion" (NGEN=32 note) — crash windows lose heat. (d) No
forgetting of items that no longer exist (stale ids accumulate).

**6. Security implications.** The ledger design must make the file *unable* to carry an injection:
`context_heat.json` stores `{kind, item_id, turn_count, last_used, first_used}` — **no content
strings**. Pinning dereferences ids through the gated stores, so a tampered heat file can at worst
re-order *already-sanitized, already-committed* content; it cannot introduce text. (Contrast: a heat
file that cached snippets "for speed" would be a second, ungated door into the prompt — explicitly
rejected.) The file lives inside `MEMORY_DIR` and is covered by the export/import/delete sovereignty
contract (docs/MEMORY_FORMAT.md) — heat is user data and is deleted with the user.

**7. Scalability implications.** Per-user files shard naturally with the existing
`memory/users/<id>/` namespace scheme; entries are pruned when their referent is tombstoned/pruned
(a sweep in the nightly dream fixes weakness 5d). Hundreds of users = hundreds of small JSON files —
the same profile as everything else in `MEMORY_DIR`.

**8. Performance implications.** Reading heat is one small-file read per turn (or cached in-process
like `usage._SESSION`); bumping is an in-memory dict + atomic flush at save-conversation time, riding
the existing `memory.save_conversation` write. Zero model calls. The payoff compounds: a mature
profile means the T0 pin set converges, prompt-cache hit rates rise, and per-turn context assembly
stops re-retrieving the same three wiki pages every turn as T1 payload (they become ~free T0
residents).

**9. Maintainability implications.** One module owns the ledger; the schema is versioned like notes
(`schema_version` field, refuse-unknown on read — the `import_memory` posture). The failure mode of a
lost/corrupt heat file is *graceful cold start* (empty pins, behavior identical to today), never an
error — matching `embed.py`'s best-effort doctrine.

**10. How Olympus should redesign it.** Three changes to the ancestor: **per-user** (weakness 5a
dissolves — Olympus already namespaces memory per user; the shared namespace gets its own profile for
system/heartbeat work); **eval-gated autopin** (weakness 5b): crossing the maturity threshold
(`OLYMPUS_PIN_MIN_TURNS`, default 50 distinct turns of heat — Olympus turns are worth far more signal
than Colibri token-steps) does not silently change prompts; it *proposes* a pin set, and the proposal
is applied through the same measure-then-keep culture as `gate_prompt`: `liveeval`/`olympus eval`
scores a window before and after, auto-revert on regression. Pinning is a prompt change and there is
no unmeasured prompt-write path — that rule already exists (README: "update_prompt now routes through
the same gate") and autopin must obey it; **crash-safe incremental bumps** (weakness 5c): heat
flushes with the conversation save it rides on, not only at session end.

**11. Final Olympus architecture.** In `olympus/ctxheat.py` (same module as rubric 2):
`ledger(user)` / `flush(user)`; `autopin_proposal(user) -> {pins, evidence}`;
`apply_pins(user, pins, gate=True)` where `gate=True` routes through the benchmark gate. Data:
`memory/users/<user>/context_heat.json` (schema above);
`memory/users/<user>/pins.json` — the *applied* pin set with provenance
(`{item_id, kind, pinned_at, gate_result_ref}`). CLI: `olympus pins` (show), `olympus pins apply`,
`olympus pins clear`. Env: `OLYMPUS_PIN_AUTO` (default off; on = propose+gate automatically from the
heartbeat), `OLYMPUS_PIN_BUDGET_TOKENS` (default 1500 — T0 pins are a *slice* of the system prompt,
not the whole), `OLYMPUS_PIN_MIN_TURNS`. Integration: heartbeat runs `autopin_proposal` after the
nightly dream (`wiki.dream_all`), so pins are computed over *consolidated* pages, not raw churn;
`specialists.py` prompt assembly injects `pins` for the active user at the T0 position.

**12. Why the Olympus approach is superior.** Colibri's cache learns *speed*; Olympus's learns
speed **and is quality-gated**, because in Olympus placement can change answers and the measurement
culture demands proof. Per-user heat turns Colibri's biggest deployment weakness (shared-profile
poisoning, warm-up overfitting) into the moat property: an Olympus instance's heat ledgers are
customer-side accumulated data no competitor or fresh install can backfill — Asset 1's arithmetic
applied to context placement.

---

## 4. REPIN — live re-pinning → safe-point re-pinning at compaction boundaries

**1. What Colibri does.** With `REPIN=n`, every n tokens at safe points the LFRU policy swaps the
coldest pinned experts for the hottest unpinned; ≤16 swaps at a 16-token cadence was the measured
sweet spot in the 6×5090 ladder (part of 5.77 → 6.28 tok/s, §7.3, §10.4); the CUDA variant migrates
VRAM slots in place.

**2. Why it exists.** A session's hot set drifts (topic changes mid-conversation); a startup-frozen
pin set decays in value. Live re-pinning tracks the drift without a restart.

**3. How it works internally.** At token-count safe points, score all experts, compute the swap set
under hysteresis, execute bounded swaps (disk loads + pin-arena bookkeeping), emit `REPIN` telemetry.

**4. Strengths.** Bounded work per safe point (≤16 swaps); hysteresis prevents oscillation; measured
+4% on a config already near-optimal. The "safe point" concept — mutate placement only where the
engine can prove no in-flight computation touches it — is the transferable idea.

**5. Weaknesses & trade-offs.** Cadence is token-denominated and workload-blind; re-pinning
mid-generation risks the exact failure PILOT_REAL needed a two-part invariant to avoid (a matmul
touching a half-loaded slot, §7.5). And in Colibri the cost of a swap is one disk read; the *Olympus*
cost of the equivalent (changing the T0 prefix) is a **full prompt-cache invalidation** — orders of
magnitude more expensive relative to the win, so Colibri's "every 16 tokens" translated literally
would be actively harmful.

**6. Security implications.** Same promotion-attack surface as rubric 2, plus a subtlety:
mid-session re-pinning driven by *this session's* heat lets a single adversarial conversation pull
its own content into T0 within the session. Restricting re-pin decisions to cross-session heat (the
ledger, not the live turn counters) and to gated stores closes this: one conversation can propose
heat, but only accumulated, multi-session heat changes pins.

**7. Scalability implications.** Trivial: the swap computation is the rubric-2 scorer over
already-capped sets, run once per compaction or session start, per active user.

**8. Performance implications.** The whole design question is *when*. The Olympus safe points where
the prefix changes anyway (so a swap is free): (a) **session start** — cache is cold regardless;
(b) **compaction** — `orchestrator._maybe_compact()` already rewrites the message stream and runs
`recall.flush_slice`, so the cache above the fold is already invalidated; (c) **specialist dispatch
boundaries** — each Athena step builds a fresh specialist prompt. Re-pinning *only* at these points
makes REPIN's economics strictly positive: never pay an invalidation you weren't already paying.

**9. Maintainability implications.** Implemented as a ~20-line hook: `ctxheat.swap_plan()` called
from the two existing sites; no new lifecycle, no timers, no background thread. The `REPIN` telemetry
line becomes a `trace.py` event so swaps are auditable in the run record.

**10. How Olympus should redesign it.** `OLYMPUS_REPIN` (default `compact` — re-pin at
compaction/session boundaries; `off` disables; there is deliberately **no** per-turn mode, with the
reason recorded here: per-turn prefix churn defeats prompt caching, the analog of Colibri measuring
that blanket NUMA interleave regresses 10×). Swaps bounded by `OLYMPUS_PIN_MAX_SWAPS`; each swap
logged with before/after scores.

**11. Final Olympus architecture.** `ctxheat.swap_plan(user, current_pins, now)` invoked from
`orchestrator._maybe_compact()` (after `flush_slice`, before the state block is rebuilt) and at
conversation open; writes through `apply_pins(..., gate=False)` — the *set* was already gate-approved
at autopin time; swaps within the approved candidate pool don't re-run the benchmark (bounded cost,
per ROADMAP's gate-cost rule F8). Env: `OLYMPUS_REPIN`, `OLYMPUS_PIN_MAX_SWAPS` (shared with
rubric 2).

**12. Why the Olympus approach is superior.** Colibri had to *invent* safe points; Olympus already
owns natural ones where the prefix is being rebuilt anyway, so re-pinning is free where Colibri's was
merely cheap. And by denominating cadence in cache-invalidation events rather than tokens, the policy
cannot be configured into the pathological regime at all.

---

## 5. RAM budgeting, RSS guard & OOM refusal (#305) + the page-cache reserve → the context budget planner with refusal-over-truncation

*(One rubric: in Colibri these are one guard family around one number — projected peak vs physical
limit — and they are absorbed as one planner. The page-cache reserve is covered as the planner's
mandatory output reserve.)*

**1. What Colibri does.** `cap_for_ram` does honest slack accounting (working-set slabs, KV pool,
scratch, 1.2 GB activations, **mandatory 2.5 GB page-cache reserve** — without it, pread throughput
collapsed 800→180 MB/s); auto budget = 88% of boot MemAvailable; **refuses to start** if projected
peak exceeds physical RAM (#305, override `COLI_RAM_OVERCOMMIT=1`); `CAP_RAISE` grows the cache when
the budget allows (#12); the **RSS guard** (#403) checks measured RSS every ~16 tokens and, on
breach, frees least-used slabs and *permanently lowers the cap* (§7.4). The same refusal posture
appears at the serving layer: the mux server answers `CONTEXT_EXCEEDED` loudly instead of the old
silent truncation (#401/#506, §6.7).

**2. Why it exists.** Silent OOM-kills and silent truncation are the two worst failures: both
destroy work invisibly. Honest arithmetic up front plus a reactive guard for when arithmetic was
wrong converts them into loud, actionable refusals.

**3. How it works internally.** Startup: sum every planned consumer + reserve, compare to physical,
refuse or size the LRU cap. Runtime: poll RSS, on breach evict and ratchet the cap down (never back
up — the measurement outranks the plan).

**4. Strengths.** (a) *Plan → refuse → guard → ratchet* is a complete control loop: the plan can be
wrong and the system still never lies. (b) The reserve encodes a measured second-order effect
(starving the OS page cache destroys *disk* performance) — budgeting isn't just "don't exceed X" but
"leave room for the substrate to work." (c) The override exists but is explicit and shouted.

**5. Weaknesses & trade-offs.** Startup-only planning: Colibri's workload is homogeneous so one plan
suffices; Olympus's per-turn context varies wildly (a one-line question vs a docrag-heavy research
step), so a boot-time plan is meaningless — planning must be per-turn and per-step. Token estimation
is also fuzzier than byte counting: Olympus has no tokenizer dependency and uses `len//4`
heuristics (`recall.retrieve` cost model); the plan must carry an error margin and be corrected by
measurement (the RSS-guard move) rather than pretend precision.

**6. Security implications.** Refusal messages must not leak withheld content (say *what class* of
context was refused admission, not its text). The overcommit override is an operator env var, not a
per-request parameter — a remote caller must never be able to disable the guard (matching the
posture that `X-Olympus-Data-Class` can *restrict* but not loosen routing in SOVEREIGNTY.md).
Beyond Colibri: the planner is also a *defense* — a tool result or webpage that balloons to fill the
window (a context-stuffing attack) hits the reserve wall and gets summarized-or-refused instead of
silently evicting the system prompt's tail.

**7. Scalability implications.** Athena's dependency graphs multiply the problem: N parallel steps
each carry context, and a step that "receives its output as input" (README §dependency-graph) can
cascade growth. Per-step plans with a per-run aggregate cap (the existing
`OLYMPUS_RUN_BUDGET_USD` / `usage.run_over_budget` pattern, but token-denominated) bound the cascade.

**8. Performance implications.** The planner is pure arithmetic on the hot path (no model call,
no I/O beyond reads already happening) — the `recall` read-path doctrine. Its *positive* performance
duty is the reserve: `OLYMPUS_CTX_RESERVE_PCT` (default 20%) holds back window space for (a) the
model's response (`max_tokens`) and (b) mid-run tool results, the two consumers that arrive *after*
planning — precisely Colibri's page-cache reserve logic (the substrate needs slack to function; a
context planned to 100% guarantees a mid-run overflow or a truncated answer).

**9. Maintainability implications.** One module, one number, one refusal type. The alternative —
scattering `if too_big` checks across five context providers and every gateway — is the current
implicit state and is why total context size is unowned today. A typed `ContextExceeded` exception
mirrors `usage.BudgetExceeded` exactly; every gateway (CLI, Telegram, web, `openai_server.py`
`/v1/chat/completions`) already knows how to surface a budget refusal.

**10. How Olympus should redesign it.** Per-turn: `ctxbudget.plan(window, candidates, reserve)` →
admitted set + a `plan` record `{window, reserve, est_total, per_source}`. Admission order is by
rubric-2 value density within per-source floors (recall and wiki each keep a minimum slice so no
source is starved by another's verbosity — the CAP_RAISE spirit inverted). **Refusal over
truncation:** if the *non-discretionary* set (system prompt + pins + un-compactable recent history +
the user's message + reserve) exceeds the window, raise `ContextExceeded` with the arithmetic and the
remedy ("compact / new session / larger-window model"), never silently drop the middle. Zeus may
*route* on this: a `ContextExceeded` against the cheap model is a legitimate reason to select a
longer-window pool member (a decision `bandit_routing`/`learned_routing` can learn from). **Drift
guard (the RSS-guard analog):** after each call, compare the plan's `est_total` to the provider's
*actual* input-token count (already recorded by `usage.record`); persist the observed
chars-per-token ratio per model in `memory/ctx_calibration.json` and ratchet the estimator — the
measurement outranks the plan, permanently, exactly like #403. **Override:**
`OLYMPUS_CTX_OVERCOMMIT=1` (operator-only) downgrades refusal to a loud warning, the
`COLI_RAM_OVERCOMMIT` twin.

**11. Final Olympus architecture.** New `olympus/ctxbudget.py`:
`plan(...)`, `class ContextExceeded(RuntimeError)`, `observe(model, est, actual)` (drift guard),
`window_for(model)` (per-model window table beside `usage.PRICES`, same maintenance posture).
Data: `memory/ctx_calibration.json` (per-model observed ratios — itself a small accumulated
calibration asset, in the Asset-1 sense); per-run plans recorded into the existing `trace.py` run
record so `witness`/replay can audit admission decisions. Env: `OLYMPUS_CTX_BUDGET_TOKENS` (absolute
cap; default = model window), `OLYMPUS_CTX_RESERVE_PCT` (default 20), `OLYMPUS_CTX_OVERCOMMIT`.
CLI: `olympus context plan` (dry-run the arithmetic for the current session — the `coli plan`
analog, reasons included). Integration: called at the two `orchestrator.py` assembly sites and by
`subagents`/Athena per step; `effortscore.py` consults it exactly as it consults
`usage.budget_headroom_low()` today — "thinks harder" must not defeat the context guard either.

**12. Why the Olympus approach is superior.** Colibri plans once at boot and guards a single global
number; Olympus plans **every turn and every Athena step**, refuses with actionable arithmetic at
the same fidelity, and — beyond Colibri — closes the loop against *ground truth* (provider-reported
token counts) instead of a self-measured proxy, so the estimator provably converges. The reserve
principle transfers intact; the refusal culture (`CONTEXT_EXCEEDED`, #305) lands in a system whose
gateways already speak typed refusals.

---

## 6. NUMA arenas → the selective-locality doctrine (mechanics skipped)

**1. What Colibri does.** `COLI_NUMA=1` interleaves *expert slabs only* across nodes via raw
`SYS_mbind`; the pinned hot-store binds one arena per layer; measured +13% (2-socket) / +40%
(4-socket) — while blanket `numactl --interleave` measured up to **10× regression** (§7.4).

**2. Why it exists.** On multi-socket boxes, remote-node memory access halves effective bandwidth;
but interleaving *everything* destroys the locality of the structures that need it.

**3. How it works internally.** Raw syscalls (no libnuma), EPERM-probe graceful downgrade, per-layer
arenas to dodge `vm.max_map_count` exhaustion.

**4. Strengths.** The finding, not the code: **locality decisions must be per-data-class, and the
blanket version of a locality optimization can be worse than nothing.** Also exemplary
fail-soft: no permission → feature off, engine unchanged.

**5. Weaknesses & trade-offs.** Entirely below Olympus's abstraction floor — Olympus is pure Python
over APIs; it has no memory-placement lever and should not grow one.

**6–9. Implications.** None operational for Olympus (no syscall surface, no scaling/security
consequence). The maintainability lesson is what transfers: Olympus already practices selective
locality — per-user memory namespaces (`memory/users/<id>/`), per-user heat (rubric 3), per-user
wiki — and its `store.py` kv backend keeps each user's documents whole rather than sharding them.

**10–11. Redesign & architecture.** **Skip the mechanics; codify the doctrine** (this rubric is the
house-style "skip with reason"): (a) data that is read together stays stored together (a user's
memories are one JSON document per namespace — already true in `usermem._load`); (b) any future
distribution of the store (Postgres partitioning, multi-instance federation) shards **by user**,
never by record type across users — the analog of "interleave expert slabs only, bind hot arenas";
(c) any such change ships with a before/after measurement, because the NUMA result proves locality
intuitions mispredict by an order of magnitude. No module, no env var. One paragraph in
`docs/MEMORY_FORMAT.md` records the doctrine.

**12. Why the Olympus approach is superior.** It isn't "superior" — it is *correctly refused*: the
capability's value is hardware-bound, and Olympus absorbs the transferable invariant (selective
locality + measure-blanket-vs-selective) at zero code cost.

---

## 7. mmap mode → reference-not-copy context & the stable cached prefix

**1. What Colibri does.** `COLI_MMAP=1` switches experts to zero-copy page-cache-backed mmap views
registered with Metal, with CPU pre-touch (GPU demand-faulting of file pages was "measured
catastrophic") and deferred mlock (§4.4, §7.2). Notably the *default* is pread-not-mmap because mmap
once made peak RSS equal the whole model (§4.1).

**2. Why it exists.** When the OS page cache already holds the bytes, copying them into private
buffers doubles memory; a view shares instead of copies. Metal's unified memory makes the same bytes
GPU-visible for free (§10.3).

**3. How it works internally.** File-backed mappings replace slab loads; the page cache becomes the
cache tier; incompatible with URING; strictness enforced (refuses unquantized layouts).

**4. Strengths.** Zero-copy where the substrate supports it (measured ~2× on Metal with `DIRECT=1`
registration). And the *pair* of lessons is the treasure: mmap is a win only under specific
conditions, and the project measured both directions.

**5. Weaknesses & trade-offs.** Sharp platform edges (RSS accounting, demand-fault storms,
URING conflict). The Olympus translation must inherit the honesty: "don't copy what can be
referenced" has failure modes too — a reference the model can't dereference (a path with no read
tool in scope) is worse than an inlined excerpt.

**6. Security implications.** Reference-not-copy *reduces* exposure: content retrieved by handle on
demand passes through the tool pipeline (security gate, `wrap_untrusted` envelopes) at use time,
instead of riding pre-inlined into every prompt. But a handle is also a *deferred* injection — the
dereference must apply the same sanitization the inline path does (it already does: tool results go
through the gate).

**7. Scalability implications.** Inlining is O(document) per turn forever; handles are O(pointer)
per turn + O(document) once when actually needed. For docrag-scale corpora only handles scale.

**8. Performance implications.** Two Olympus analogs: (a) **the prompt cache as page cache** — the
T0 prefix (rubrics 2–4) is the "mmap'd" region: identical bytes across turns, priced near zero once
warm; assembly order must therefore be strictly stability-sorted (immutable prefix first, volatile
blocks last), which is a *requirement the budget planner enforces*, not a convention. (b) **retrieval
handles** — for large T2/T3 items, inject a one-line pointer ("wiki: deploy-runbook, 6.2k chars —
`read_wiki('deploy-runbook')`") instead of the body, letting the specialist demand-fault it. Colibri's
"CPU pre-touch because GPU demand-faulting was catastrophic" translates directly: *pre-touch what you
know you'll need* (the planner inlines high-heat small items) and *demand-fault the long tail*
(handles for big cold ones) — the heat score from rubric 2 is exactly the pre-touch predictor.

**9. Maintainability implications.** No new machinery: handles are the existing on-demand tools
(`recall_memory`, wiki read, docrag fetch) — this rubric changes *which* representation the planner
admits, a policy inside `ctxbudget.plan`, not a new subsystem.

**10. How Olympus should redesign it.** Inside `ctxbudget.plan`: items whose `est_tokens` exceed
`OLYMPUS_CTX_INLINE_MAX` (default 400) are admitted as handles, not bodies, unless their heat score
puts them in the top pre-touch set. Assembly emits sections in stability order:
`[T0 pins] → [conversation state block] → [handles] → [volatile per-turn blocks] → [user turn]`.
Default behavior with the planner off: byte-identical to today (the `OLYMPUS_ANN` contract).

**11. Final Olympus architecture.** Policy code in `olympus/ctxbudget.py` (`_as_handle(item)`);
stability-ordering in the orchestrator's existing assembly; env `OLYMPUS_CTX_INLINE_MAX`. No new
files.

**12. Why the Olympus approach is superior.** Colibri's mmap is a platform-conditional 2×; Olympus's
version is unconditional economics — the prompt cache exists on every provider that matters, and
handles are already implemented as tools. Olympus also keeps Colibri's negative result: no blanket
"always reference" mode ships, because a handle to content the step's tool scope can't dereference is
the demand-fault catastrophe in API clothing.

---

## Beyond Colibri (capabilities this domain needs that Colibri lacks)

- **Cross-session, cross-user quality feedback into placement.** Colibri's heat only measures *use*;
  Olympus can weight heat by *outcome* — items that were in-context for 👎-rated or
  Aletheia-corrected answers should not accrue the same pin credit as items present for 👍 runs.
  Wire: `outcomes.py` verdicts adjust `ctxheat` importance multipliers during the nightly dream.
  Marked as a research spike below (attribution is confounded; ship counting first).
- **Sovereign-mode neutrality.** Everything in this design is pure-Python, no model call on the hot
  path, no egress — it works identically under `OLYMPUS_SOVEREIGN=1`, where context economics matter
  *more* (local models have smaller windows). The planner's `window_for(model)` must carry local
  models' real windows so refusal arithmetic is honest in the fail-closed regime.
- **Placement audit.** Every plan/refusal/swap lands in the `trace.py` run record — placement
  decisions become part of the signed execution corpus (MOAT Asset 1 substrate), something Colibri's
  EMAP telemetry never persisted.

## Open questions & research spikes

1. **Token-estimator calibration (bounded spike, ~2 days).** Validate the `len//4` heuristic per
   model against `usage.record`'s provider-reported counts across a week of real traffic; ship the
   drift-guard ratchet only if raw error exceeds ±15% (below that, the 20% reserve already covers it).
2. **Does learned pinning actually help? (the gating measurement, ~1 week wall-clock, near-zero
   marginal API cost).** A/B via `liveeval` on the existing benchmark set: pins-on vs pins-off, per
   specialist. Ship `OLYMPUS_PIN_AUTO` default-on only after a no-regression + latency/cost win is
   recorded in `quality_baseline.json` provenance. Until then it stays opt-in, like `OLYMPUS_ANN`.
3. **Outcome-weighted heat (deferred).** Credit assignment from 👍/👎 to individual context items is
   confounded (many items per prompt). Candidate design: shadow-score only, surfaced in
   `olympus context map`, never driving eviction until a measurement design exists. Record in
   DEFERRED.md if not scheduled.
4. **Per-source floors vs pure value-density.** Starvation-free admission (floors) may admit junk
   from a weak source; pure density may starve relgraph/playbooks entirely. Needs a two-week
   observation window of plan records before choosing a default.
5. **Hysteresis constant.** 25% is inherited provenance, not a measurement in our domain; sweep
   {10%, 25%, 50%} against prompt-cache hit rate once provider cache metrics are captured in
   `usage.record`.
