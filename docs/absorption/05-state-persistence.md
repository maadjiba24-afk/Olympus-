# Absorption 05 — Session State & Persistence

**Colibri domain:** the compressed MLA KV cache (576 vs 32,768 floats/token), KV slots as
sessions, crash-safe append-only `.coli_kv` persistence with count-written-last, the
`COLIKV1\0` model-fingerprint header, serve-mode prefix matching / truncate-and-extend,
zero-re-prefill warm resume, and the KV-resume language-bleed war story
(`docs/colibri-deep-analysis.md` §8.1–8.2, §4.5, §6.7, §9.1, §26.7).

**Domain thesis.** Colibri's session layer is built on one economic identity: *conversation
state is expensive to recompute and cheap to store, so compress it at the source, journal it
append-only, fingerprint what it depends on, and resume warm instead of re-prefilling*. Olympus
lives the same identity one abstraction up — its "prefill" is token spend and latency on every
re-sent transcript, its "KV cache" is the distilled conversation state (`ace.py` playbooks, the
`_compress_history` state block), and its "KV-prefix reuse" is the provider prompt cache that
`llm.py` already marks with `cache_control` breakpoints. But today the persistence substrate
under all of this is a whole-file atomic JSON rewrite (`memory.save_conversation`), resumability
is un-fingerprinted (a resumed session silently spans model, prompt, and code changes), and
cache-hit economics are paid but never measured. This document absorbs Colibri's
journal/fingerprint/warm-resume triad into a native **conversation-state fabric**: one new
module (`olympus/sessionlog.py`), fingerprints computed from the same canonicalization
`replaystore.py` already trusts, cache-hit telemetry in the counters `usage.py` already keeps,
and bleed guards that turn Colibri's war-story *notice* into designed detection. Everything
default-off-byte-identical until benchmarked, per the house instrumentation doctrine — and the
journal itself feeds the moat: it is the raw substrate of the signed execution corpus
(`MOAT_ANALYSIS.md` Asset 1/3) that a competitor cannot backfill.

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| Compressed MLA KV (576 vs 32,768 floats) | store the low-rank latent, not the heads — compression at the write point, not after (§6.1, §8.1) | **absorb-principle** — distilled state (ACE playbook + in-run transcript compaction) is already the analog; add the missing measurement gate | `olympus/ace.py`, `olympus/transcript.py`, `olympus/orchestrator.py` (`_compress_history`), `olympus/liveeval.py` |
| KV slots as sessions | `KV_SLOTS` ≤16/≤512 independent conversations, each with its own history + disk file (§8.2, §9.1) | **redesign** — sessions become first-class journaled objects, per-user namespaced, uniform across CLI/web/Telegram/OpenAI-endpoint `cache_slot` analog | `olympus/memory.py` (conversations), `olympus/gateway.py`, new `olympus/sessionlog.py` |
| Crash-safe append-only `.coli_kv` (count-written-last) | fixed-size appended records; `nrec` written last so a torn write is invisible (§8.2) | **new-subsystem** — append-only per-session JSONL journal with seq-written-last framing and an explicit fsync policy (fixing Colibri's `fflush`-without-`fsync`) | new `olympus/sessionlog.py`; `olympus/store.py` conventions; `olympus/backup.py` |
| Model-fingerprint header | `COLIKV1\0` + 8×int32; mismatch → file ignored; honestly noted: doesn't hash weights (§4.5, §8.2) | **redesign** — fingerprint what actually changes decisions (provider, model, system-prompt hash, tool-schema hash, code version) via `replaystore.canonical_request` machinery; mismatch → *warm-with-notice* or *cold resume*, never silent discard | `olympus/sessionlog.py`, `olympus/replaystore.py`, `olympus/config.py` |
| Prefix matching / truncate-and-extend | serve-mode prefill reuses the common KV prefix; only new positions appended (§6.7, §8.2) | **redesign** — provider prompt-cache prefix stability as the KV-prefix analog: stable system prefixes, compaction only at cache-safe points, **measured** cache-hit telemetry | `olympus/llm.py`, `olympus/usage.py`, `olympus/orchestrator.py` (`_maybe_compact`), CI prefix guard |
| Zero-re-prefill warm resume | loading `.coli_kv` resumes byte-identical with no prefill (§8.2) | **redesign** — warm resume across process restarts and `olympus upgrade`: journal + playbook + handoff compose so no re-distillation, no history loss, no re-planning | `olympus/hibernate.py`, `olympus/selfupdate.py` (handoff), `olympus/gateway.py` (inflight), `olympus/sessionlog.py` |
| KV-resume language bleed (war story) | resumed state carries conversational bias; mitigation is a prominent notice (§8.2, §26.7) | **new-subsystem** — designed bleed guards: user binding, age gate, fingerprint drift surfacing, resume banner, fresh-start default past threshold; *beyond Colibri:* automated bleed detection | `olympus/sessionlog.py`, `olympus/orchestrator.py`, `olympus/replaygate.py` |

Grouping note: Colibri implements append-only persistence, the fingerprint header, and prefix
truncate-and-extend as one ~300-line header (`kv_persist.h`). They are separated here because in
Olympus they land in different subsystems with different failure modes (durability, identity,
economics) — but §3–§5 below cross-reference each other and share `sessionlog.py`.

---

## 1. Compressed KV at the source → distilled conversation state

**1. What Colibri does.** MLA attention stores per token only a 512-float normalized latent +
64 rope floats — 576 floats instead of the 32,768 a 64-head cache would need (57× smaller). The
compression is *architectural*: the state is written compressed, never compressed after the fact
(§6.1, §8.1). This is what makes long context fit in 15 GB and what makes disk persistence cheap
(~182 KB/token instead of ~10 MB).

**2. Why it exists.** Long context on consumer RAM is impossible with a full-head cache; and a
cheap-to-store state is what makes sessions-on-disk (§3 below) economically sane at all.

**3. How it works internally.** `kv_a(x)` projects to `[kv_lora | qk_rope]`; the latent is
rmsnorm'd and stored; attention runs against the latent directly via weight absorption. No
KV quantization, no paging — the compression is in the representation, not in a post-pass.

**4. Strengths.** Compression is lossless *with respect to the model's own definition of state*
(the latent IS what attention needs); it multiplies through every downstream cost — RAM, disk,
persistence write bandwidth, resume time; and it needs no eviction policy because the compressed
state is small enough to keep everything.

**5. Weaknesses & trade-offs.** The representation is fixed by the architecture — Colibri cannot
compress *harder* when space runs out (no KV quantization, no paging; §27 lists both as absent).
There is exactly one compression level for all content, however redundant. And the compression
is invisible: nothing measures whether the latent-only path costs quality (Colibri leans on the
token-exact oracle instead, which is the right tool there but has no analog knob).

**6. Security implications.** Minimal in Colibri (the latent is as sensitive as the text it
encodes). For Olympus the analog is sharper: a *distilled* state block concentrates the most
durable facts of a conversation into one high-value blob — it inherits the full PII sensitivity
of the transcript and must live under the same protections (0o600 files, backup encryption per
`docs/BACKUPS.md`).

**7. Scalability implications.** Compression-at-source is the only thing that scales
persistence: Olympus re-sends its transcript every turn, so an uncompressed conversation costs
O(n²) tokens over its life. The distilled-state pattern converts that to O(n) + a bounded
verbatim tail — exactly the latent-vs-full-heads shape.

**8. Performance implications.** In Olympus the "floats" are tokens×dollars×latency. The analog
already exists and works: `orchestrator._compress_history` folds old turns into an ACE playbook
(evolving delta, pinned durable facts) and `transcript.py` shrinks old in-run tool results
(`OLYMPUS_INRUN_COMPACT`, replay-safe by construction). What is missing is Colibri's discipline
of *measuring the compression cost* — ACE compaction currently has no per-conversation quality
gate, only the global `liveeval` sampling.

**9. Maintainability implications.** The existing split is clean and should be kept: `ace.py`
(cross-turn state), `transcript.py` (in-run tool results), `recall.flush_slice` (pre-compaction
durable-fact extraction so compaction can never silently lose typed memory — a guard Colibri has
no analog for, because its compression is lossless). One risk: three compaction sites with three
budgets is drift-prone; absorption doc 02's `ctxbudget.py` is the place the budgets unify.

**10. How Olympus should redesign/evolve it.** *Adopt the principle, keep the existing organs,
add the two missing pieces:*
- **Adaptive compression levels** (Colibri's stated gap, removed): the state block gets a size
  budget from `ctxbudget.py` (doc 02) and ACE renders to it — pinned facts survive at every
  level; prose detail is shed first. This is the "KV quantization" Colibri lacks, done where
  Olympus can do it (semantic, not numeric).
- **Measured compression cost** (Colibri's invisible-cost gap, removed): a `liveeval` probe
  class that scores answer quality on questions whose evidence lives *only* in compacted turns —
  the before/after gate the measurement culture requires before compaction defaults ever change.
  Bounded spike: ~20 golden Q/A pairs generated per long eval conversation; runs inside the
  existing `OLYMPUS_LIVE_EVAL_EVERY` cadence, no new loop.

**11. Final Olympus architecture.** No new module. `ace.py` gains
`render(pb, budget_tokens=None)`; `orchestrator._compress_history` passes the budget from
`config.history_token_budget` (later from `ctxbudget`); `liveeval.py` gains the
`compaction_recall` probe kind; results land in the run's `tr.meta` beside the existing
`inrun_compact` settings so `orchestrator.replay_run` reproduces recorded streams unchanged.
Env: existing `OLYMPUS_HISTORY_TOKEN_BUDGET`, `OLYMPUS_ACE`, `OLYMPUS_INRUN_COMPACT` — no new
knobs; the budget parameter defaults to today's behavior.

**12. Why the Olympus approach is superior.** Colibri's compression is fixed and unmeasurable;
Olympus's is *adaptive* (budget-driven levels), *guarded* (typed-memory flush before every fold —
loss-proof where Colibri is merely lossless), and *measured* (a recall gate in the same liveeval
substrate that gates everything else). And it compounds: playbooks are per-conversation
accumulated assets, not caches.

---

## 2. KV slots as sessions → first-class journaled sessions

**1. What Colibri does.** `KV_SLOTS` gives up to 16 interactive / 512 multiplexed independent
conversations, each an isolated `KVState` with its own history and its own disk file
(`.coli_kv.slot`); the HTTP layer exposes them as `cache_slot` and the dashboard has a
KV-session selector (§8.2, §9.1, §14.1).

**2. Why it exists.** Prefill costs minutes on a disk-streaming 744B model; a session that keeps
its KV warm turns the second turn from minutes to seconds. Slots make that per-conversation.

**3. How it works internally.** Fixed slot array; each slot a flat per-layer buffer set sized by
`CTX`; the mux server requires a distinct `KVState` per ragged batch row; per-slot fair
admission in the Python scheduler.

**4. Strengths.** Hard isolation by construction (separate buffers, separate files); sessions
survive server restarts; the slot id is a stable public handle across the whole stack (engine
protocol → HTTP → web UI).

**5. Weaknesses & trade-offs.** Slots are a fixed, small, *anonymous* namespace: no ownership
(any client may claim any slot — the web UI just picks one), no per-slot metadata, no lifecycle
(nothing expires or archives a slot), and the web chat layer on top keeps its own conversation
in memory only (a stated limitation, §14.1). Session identity and session *content* live in two
places that can disagree.

**6. Security implications.** Anonymous slots are a cross-user information-leak primitive the
moment two clients share a server — client A can resume client B's slot. Colibri's single-user
posture makes this acceptable; Olympus's multi-user gateways (Telegram/Slack/web, BYOK visitors)
make it disqualifying. Olympus already namespaces per user (`Olympus(user=…)`,
`conversation_id`, "one person's context never leaks into another's session" —
`orchestrator.py` ~245); the redesign must keep user-binding *inside* the session record, not
only in the path name.

**7. Scalability implications.** Olympus sessions are unbounded in count (a `conversation_id`
per chat), so the fixed-slot model is wrong; but Colibri's discipline — *the session handle is
the unit of admission control* — is right and maps onto the gateway's per-user inflight
journal and the OpenAI-endpoint's queue.

**8. Performance implications.** The Olympus "warm slot" is threefold: the loaded history list,
the ACE playbook, and the provider prompt-cache entry keyed by the request prefix (§4 below).
Only the first two survive a restart today; the design goal is that resuming a session restores
the first two for free and re-earns the third at cache-write (not full re-reasoning) price.

**9. Maintainability implications.** Olympus already has the right identity spine
(`conversation_id` → `memory.load_conversation`/`save_conversation`, ACE keyed by the same id,
search indexing on save). The redesign should *not* invent a second session concept — it should
make the existing one durable and self-describing.

**10. How Olympus should redesign/evolve it.** *Adapt:* keep `conversation_id` as the one
session handle; attach to it a **session header record** (owner user, created/updated
timestamps, fingerprint §4, playbook ref, journal seq) written as record 0 of the journal (§3).
Slot-claiming disappears; ownership is checked on resume (`sessionlog.open(cid, user=…)` refuses
a mismatched owner — bleed guard §7). Gateways (`gateway.py`, web, CLI `--continue`) all resume
through the same call.

**11. Final Olympus architecture.** `olympus/sessionlog.py` exposes
`open(conversation_id, user) -> Session`, `Session.append(record)`, `Session.header`,
`Session.replay() -> list[record]`; `memory.save_conversation` keeps working as the compat
snapshot view (rebuilt from the journal) so nothing downstream (search indexing, previews)
changes. Data model: `MEMORY_DIR/sessions/<safe_id>.jsonl`. Integration: `orchestrator.__init__`
loads via `sessionlog` when `OLYMPUS_SESSION_JOURNAL=1`, else exactly today's path.

**12. Why the Olympus approach is superior.** Colibri's slots are anonymous, fixed, and split
identity from content; Olympus's sessions are owned, unbounded, and single-sourced — the header
travels with the data, so isolation is a property of the record, not of the deployment shape.

---

## 3. Crash-safe append-only persistence → the session journal (`sessionlog.py`)

**1. What Colibri does.** `.coli_kv` is fixed-size records appended after a header; the record
count `nrec` is written **last**, so a crash mid-append leaves a file whose header simply
doesn't admit the torn tail — corruption-free resume by ordering alone (§8.2).

**2. Why it exists.** Sessions are only trustworthy if a crash at any byte leaves a loadable
file; and append-only makes the write cost O(new tokens), not O(conversation).

**3. How it works internally.** Magic + fingerprint header, then `[token][all layers' Lc+Rc][Ic]`
per record (~182 KB/token); serve mode truncates `nrec` and appends on prefix divergence.
Honestly-noted limitations: f32 records (no compression), `fflush` without `fsync` (a power cut
can still lose acknowledged records), fingerprint doesn't hash weights.

**4. Strengths.** The count-written-last idiom is the cheapest possible crash consistency — no
WAL, no double-write, no checksums needed for *torn-tail* safety; append-only means the failure
domain of a write is only the newest record.

**5. Weaknesses & trade-offs.** Fixed-size records can't hold variable payloads; no fsync means
the durability claim is really "crash-safe against process death, not power loss"; no
per-record integrity means a *bit-rot* (not torn) record is trusted silently; truncate is
implemented by header rewrite, so concurrent readers see states flip. — Now the Olympus mirror:
`memory.save_conversation` is **whole-file atomic rewrite** (tmp + `os.replace`, torn read → `[]`
per ADR 0005). That is crash-*atomic* but not crash-*durable at record granularity*: a crash
between turns loses nothing, but a crash mid-run loses the whole turn's tool trail; and the
rewrite is O(conversation) every turn — the exact cost shape Colibri's append design exists to
kill. At 200-turn conversations with inlined tool results this is a real IO and (on
`PostgresStore`) a real row-churn cost.

**6. Security implications.** The journal concentrates transcripts (PII) and tool results
(possibly secrets) in one growing file: create 0o600 (the `store.py` precedent), live under
`MEMORY_DIR` so `backup.py`'s encryption/signing covers it, and follow `docs/BACKUPS.md`'s
include/exclude doctrine — journals are **user data, included by default** (unlike the
reproducible `responses/` replay caches). Tamper-evidence is *not* re-invented here: the signed
decision log (`traces/`) remains the integrity witness; the journal gets only a cheap per-record
`sha256` field so bit-rot fails loudly on load (Colibri's silent-trust gap, removed). No hash
chains, no signatures on the journal itself — that would duplicate `ledger`/`attest` (BFT-theater
rule, ROADMAP §0).

**7. Scalability implications.** Append-only writes are O(1) per event and Postgres-friendly
(the `PostgresStore` variant appends rows keyed `(ns="session:<cid>", k=seq)`); compaction (§1)
becomes a *journal event* (`{"type":"compact","state_ref":…}`) rather than a rewrite, so the
journal is also the audit trail of what was folded when — which today is invisible.

**8. Performance implications.** Turn append cost drops from O(n) rewrite to O(turn); resume
cost is one sequential read. Fsync policy is explicit where Colibri's was accidental:
`OLYMPUS_SESSION_FSYNC=off|turn|always` (default `turn`: one fsync per completed user-visible
turn — the measured-first culture demands the `off`→`turn` delta be benchmarked on a droplet
before `turn` becomes default).

**9. Maintainability implications.** JSONL with a `type` field is self-describing and
greppable — debugging a session is `less` on one file. The snapshot view (`conversations/…json`)
is retained as a *derived* artifact so `search.index_conversation`, previews, and every existing
consumer keep working; one rebuild function owns the derivation. Risk to own honestly: two
representations means one invariant test — "snapshot == fold(journal)" — added to the suite.

**10. How Olympus should redesign/evolve it.** *Adopt* count-written-last, translated: JSONL is
self-delimiting, so the idiom becomes **seq-written-in-record + truncate-to-last-complete-line
on load** (a torn tail is a partial last line; the loader drops it and logs). *Fix* both noted
Colibri limitations: explicit fsync policy (above) and per-record hash. *Reject* fixed-size
records (payloads are variable; JSONL, with large tool results stored by reference into
`store.py` over a size threshold to keep the journal lean).

**11. Final Olympus architecture.** New module **`olympus/sessionlog.py`** (~200 lines):
- Record types: `header` (v, owner, created, fingerprint), `turn` (role, content, ts),
  `tool` (ref or inline, sha256), `compact` (state_ref, folded_range), `resume`
  (fingerprint_then, fingerprint_now, decision), `handoff` (from_version).
- API: `open/append/replay/rebuild_snapshot`; `append` writes line + optional fsync;
  `replay` verifies per-record sha, drops torn tail, returns records + a `truncated` flag.
- Env (Olympus conventions): `OLYMPUS_SESSION_JOURNAL` (default off → flip after bench),
  `OLYMPUS_SESSION_FSYNC` (`turn`), `OLYMPUS_SESSION_INLINE_MAX` (bytes; larger tool results go
  to `store.put("blobs", sha, …)`).
- Integration: `orchestrator` appends turns where it calls `memory.save_conversation` today
  (lines ~1629, ~1993, ~2094 collapse to `session.append` + periodic snapshot rebuild);
  `backup.py` includes `sessions/` by default; `replaygate` unaffected (journals record what
  happened; `replaystore` still owns byte-identical re-execution).

**12. Why the Olympus approach is superior.** Same crash idiom, three Colibri gaps closed
(fsync, record integrity, variable payloads), zero new consumers broken (snapshot stays as a
derived view), and the journal doubles as the per-customer execution substrate the moat analysis
says to start accumulating *now* — Colibri's `.coli_kv` is a cache; Olympus's journal is an
asset.

---

## 4. Model-fingerprint headers → decision-relevant resume fingerprints

**1. What Colibri does.** The `.coli_kv` header is `COLIKV1\0` + 8×int32 derived from model
config; on mismatch the file is silently ignored and the session re-prefills cold (§4.5, §8.2).
The analysis notes the honest gap: the fingerprint hashes *dimensions*, not weights — two
different models with identical shapes would pass.

**2. Why it exists.** Resuming KV computed by a different model is silent nonsense; the
fingerprint converts that to a clean cold start.

**3. How it works internally.** Eight config-derived int32s compared on open; any mismatch →
treat as absent.

**4. Strengths.** Zero-cost identity check at exactly the right choke point (open); fail-safe
direction (worst case is a slow cold start, never wrong state).

**5. Weaknesses & trade-offs.** (a) Fingerprints the wrong closure — shapes, not weights, and
nothing about the *prompt-side* state (chat template, sampling defaults) that also shapes a
session. (b) Mismatch handling is silent discard: the user loses a warm session with no notice
and no choice. (c) Binary decision — no notion of "compatible enough to resume with a warning."
For Olympus the stakes are inverted: distilled state (text) is *portable across models*, so
discarding on model change would be wrong; but what is NOT portable is the *assumption set* —
which model/prompt/tools produced the plan the session is mid-way through.

**6. Security implications.** The fingerprint is also a confused-deputy guard: resuming a
session recorded under a different tool roster or security-gate config could replay a plan whose
approvals no longer mean the same thing. Fingerprinting the tool-schema hash and the
gate-relevant config closes that; the security screen still runs on every action regardless
(defense in depth, not replacement).

**7. Scalability implications.** Trivial (one hash compare per resume). The win is operational:
fleet upgrades (`olympus upgrade`) touch every session's fingerprint at once; the design must
make that a *warm-with-notice* path or every upgrade would cold-start every user.

**8. Performance implications.** The fingerprint decides which of three resume prices is paid:
**warm** (same fingerprint: reuse playbook + history, provider cache may even still be live
within TTL), **warm-with-notice** (model/version drift: reuse state, surface the change, §7
banner), **cold** (owner mismatch or corrupt journal: refuse/fresh). Colibri has only
warm/cold.

**9. Maintainability implications.** Olympus already owns the canonicalization machinery:
`replaystore.canonical_request` / `request_hash` define exactly which request fields are
decision-relevant (and which, like `container`, are not). The fingerprint must be computed from
the same code path, not a parallel hand-rolled hash — one canonicalization to maintain, and
fingerprint semantics stay automatically in step with replay semantics.

**10. How Olympus should redesign/evolve it.** *Adapt:* fingerprint =
`sha256(provider, model, base_url, sha256(system_prompt_prefix), sha256(tools_schema),
olympus.__version__, prompt_schema_rev)` — computed by a new `sessionlog.fingerprint(settings)`
that reuses `replaystore`'s canonical JSON rules. Stored in the journal header; on resume,
recompute and diff *component-wise* so the notice can say *what* changed ("model changed:
claude-x → claude-y") instead of Colibri's mute discard. This also **exceeds** Colibri's noted
weights gap: an API client cannot hash weights either, but `(provider, model)` is the API-world
weights identity, and the prompt/tool hashes cover the state Colibri never fingerprinted at all.

**11. Final Olympus architecture.** In `sessionlog.py`: `fingerprint(settings, tools) -> dict`
(component hashes) + `diff_fingerprint(old, new) -> list[str]`. `orchestrator` computes it at
session open; a non-empty diff appends a `resume` record (the audit trail of every drift) and
triggers the §7 banner. Env: none needed — fingerprinting is free and always on when the journal
is on; `OLYMPUS_RESUME_STRICT=1` opts into Colibri-style refuse-on-drift for regulated
deployments (fail-closed kinship with `OLYMPUS_SOVEREIGN`).

**12. Why the Olympus approach is superior.** It fingerprints the *decision closure* (the same
one the replay gate already enforces), degrades in three graded steps instead of two, tells the
user what changed, and leaves an append-only drift history — turning an identity check into
calibration-record evidence (which model/prompt revisions a long-lived session survived).

---

## 5. Prefix matching / truncate-and-extend → prompt-cache-aligned prefix stability

**1. What Colibri does.** In serve mode, a new submission is prefix-matched against the slot's
existing KV; the common prefix is kept, the divergent tail truncated, and only new positions
prefilled (§6.7, §8.2). `coli chat` attach mode measured the effect: warm engine 4%→55% hit,
~10× (§13).

**2. Why it exists.** Prefill is the dominant cost; conversations are append-mostly, so almost
every turn shares almost its whole prefix with the last.

**3. How it works internally.** Token-level compare against the persisted record stream;
truncate `nrec`; append new records; mux prefill is serial per submission with this reuse.

**4. Strengths.** Turns the append-mostly structure of chat into near-free turn starts; exact
(byte-identical results); composes with persistence (the matched prefix may have been computed
in a previous process).

**5. Weaknesses & trade-offs.** Colibri controls its own cache, so reuse is guaranteed; Olympus
rents its cache from the provider (Anthropic prompt caching: 5-min/1-h TTL, invalidated by any
byte change *anywhere in the prefix* — system, tools, or history). The Olympus dangers are
therefore inverted: it is trivially easy to *silently pay full price forever* — one
timestamp/nonce/user-detail interpolated early in the system prompt, one over-eager compaction,
or a mid-history edit, and every subsequent block is a cache miss that **nothing today
measures**. `llm.py` already places breakpoints (`_cache_tools`, system-prompt block, 1-h TTL
beta) and already *reads back* `cache_read_input_tokens`/`cache_creation_input_tokens` — but
only sums them into total tokens (llm.py ~330, ~393); the hit *ratio* is discarded.

**6. Security implications.** Prefix stability must never be bought by moving per-user or
per-request data *out* of the prompt where gates can see it; the stable prefix is the *static*
stack (persona, tool schemas, skills index), and volatile blocks go after the last breakpoint.
Provider-side: prompt caches are already per-org isolated; nothing new to defend. Sovereignty
mode is where this rubric pays double — local servers (vLLM prefix caching, llama.cpp) give
true Colibri-style KV reuse, and the same prefix discipline is what unlocks it; the design must
be provider-neutral.

**7. Scalability implications.** Cache-hit ratio is a *fleet* economics number: at N users ×
M turns, prefix stability is the difference between O(total tokens) and O(new tokens) spend.
Telemetry must aggregate per model and per prompt-stack revision so a prompt change that halves
hit rate is visible within a heartbeat cycle, not at the invoice.

**8. Performance implications.** Measured, not promised (house rule): the deliverable is the
measurement first. Per-call: `hit_ratio = cache_read / (cache_read + cache_creation +
uncached_input)`; aggregated by `usage.py` beside its existing counters. Then two mechanical
wins with before/after numbers: (a) compaction alignment — `_maybe_compact` fires only at turn
boundaries *after* response delivery (it already does; codify as the contract) and the state
block, once written, is **frozen text** (ACE delta-evolution already avoids rewriting pinned
facts — the property that makes the post-compaction prefix stable *again*); (b) a **prefix
guard** in CI: assemble the system stack twice in one process and assert byte-equality (catches
the timestamp-in-prompt class of regressions the way Colibri's byte-identical-when-off tests
catch instrumentation drift).

**9. Maintainability implications.** All changes land in files that already own the concern
(`llm.py` counters, `usage.py` aggregation, `orchestrator` compaction contract); no new module.
The CI prefix guard is one test file. Risk: over-constraining the prompt (never being able to
add a dynamic block); the contract is "volatile after the last breakpoint," not "nothing
volatile."

**10. How Olympus should redesign/evolve it.** *Adopt* prefix-reuse economics; *differentiate*
mechanism: Olympus cannot truncate-and-extend the provider's cache, so it optimizes the one
lever it has — byte-stable prefixes — and **measures** the result (Colibri's hit telemetry
culture, transplanted). *Beyond Colibri:* per-prompt-revision hit-rate attribution, so
`gate_prompt`'s before/after benchmark can also see the *cost* delta of a prompt rewrite, not
just the quality delta.

**11. Final Olympus architecture.** `llm.py` records `cache_read`/`cache_creation`/`uncached`
per call into `usage.py` (new fields on the existing usage records — additive-only;
byte-identical behavior otherwise). `olympus status` and the admin panel gain a cache-hit line
(the panel already shows `prompt_cache_ttl`, adminpanel.py ~83). New env: none (telemetry is
always-on accounting, like spend). CI: `tests/test_prefix_stability.py`. Sovereign mode: same
ratio computed from local-server usage fields when present.

**12. Why the Olympus approach is superior.** Colibri could only ever reuse its own cache;
Olympus's version makes prefix economics *visible and attributable* across rented (Anthropic),
compatible (OpenAI), and owned (sovereign/local) caches with one metric — and wires it into the
prompt-upgrade gate so cache cost becomes a first-class regression axis. The accumulated
hit-rate-by-revision series is comparative evidence (Asset 2 shape), not a static feature.

---

## 6. Zero-re-prefill warm resume → warm resume across restarts and upgrades

**1. What Colibri does.** Loading `.coli_kv` resumes a conversation with **zero re-prefill**,
byte-identical — the persisted latent is the state, so a restarted server continues as if never
stopped (§8.2).

**2. Why it exists.** The 744B engine takes minutes to load and prefill; without warm resume
every restart costs every session its accumulated context.

**3. How it works internally.** Open file → fingerprint check → read `nrec` records into the
slot → continue decoding. Composes with prefix matching (§5) for edited resumes.

**4. Strengths.** Restart cost decoupled from session length; the persistence layer *is* the
resume layer (no second mechanism).

**5. Weaknesses & trade-offs.** Colibri resumes *between* turns only — a crash mid-generation
loses the in-flight turn (nothing journals partial work); resume is per-slot manual (the client
must ask for the slot); and nothing resumes *across a binary upgrade* with state migration —
the fingerprint simply gates it out. Olympus's current state: restarts are handled piecemeal —
`hibernate.run_once` makes the heartbeat resumable-by-design, `gateway.py`'s inflight journal
retries interrupted chat requests (max 2 attempts, 24 h age cap), `selfupdate.write_handoff`/
`take_handoff` passes a snapshot across upgrades — but a *conversation* resume still reloads and
re-sends the whole snapshot, and a mid-run crash loses the run's tool trail (only the replay
caches survive, and those are for audit).

**6. Security implications.** Resume is where stale authority sneaks back in: an inflight entry
or handoff resumed after a config change must re-pass the security gate and re-check the §4
fingerprint before any action replays. The inflight journal's age/attempt caps already exist;
the same caps apply to session-level resume (§7's age gate).

**7. Scalability implications.** Warm resume is what makes the serverless posture honest:
`hibernate` wakes, `sessionlog` gives O(new work) resume for any session touched, process exits.
Without it, hibernating deployments pay O(session) reload per wake — the exact anti-pattern
Colibri's design kills.

**8. Performance implications.** Three costs at resume, handled separately: (a) *state* — free
(journal read + playbook load; no re-distillation because the `compact` records carry state
refs); (b) *provider cache* — cannot survive past TTL; the first post-restart call pays
cache-write price on the stable prefix, which the §5 telemetry will show as exactly one
creation-heavy call, not a regression; (c) *in-flight work* — the run-level story. Honest
scope: **mid-run resume is E3 (durable execution) territory** and stays there; this domain
delivers turn-level warm resume plus a journaled record of what was in flight (`sessionlog`
`tool` records give the next process *evidence* of partial work, even before E3 makes it
re-executable). No "free counterfactual replay" claims (ROADMAP §0, F9).

**9. Maintainability implications.** The redesign composes three mechanisms that already exist
rather than adding a resume engine: journal (state), handoff (upgrade continuity), inflight
(request retry). The one new seam is `take_handoff` → for each carried session, append a
`handoff` record to its journal — so "what carried across the upgrade" becomes queryable per
session instead of one global snapshot file.

**10. How Olympus should redesign/evolve it.** *Adopt* persistence-is-resume (one mechanism);
*remove* Colibri's gaps: resume works across upgrades (fingerprint diff → warm-with-notice, §4)
and partial turns leave evidence (journaled tool records). *Skip* byte-identical continuation
of a half-generated response — an API client cannot resume a provider stream mid-token; the
correct translation is re-issue-or-report, which the inflight journal already implements.

**11. Final Olympus architecture.** No new module beyond `sessionlog.py`. Wiring:
`orchestrator` opens the journal at construction (replacing bare `load_conversation` when
enabled); `gateway.inflight_take` resumption appends its retry decision to the session journal;
`selfupdate.take_handoff` stamps `handoff` records; `hibernate.run_once` needs no change (state
was never process-resident). CLI: `olympus sessions` (list, from headers) and
`olympus resume <id>` reuse the header metadata. Env: covered by §3/§4 vars.

**12. Why the Olympus approach is superior.** Colibri resumes a process's sessions; Olympus
resumes sessions across processes, *versions*, and deployment shapes (always-on, serverless,
sovereign) with one journal — and every resume leaves an audit record, which Colibri's silent
reload never does.

---

## 7. The language-bleed war story → designed state-bleed guards on resume

**1. What Colibri does.** A resumed 670-token Italian session made every subsequent reply
Italian and "looked like a quantization bug for a day" — persisted KV carries *conversational
disposition*, not just facts. The shipped mitigation is a prominent resume notice in the CLI
(§8.2, §26.7).

**2. Why it exists.** Warm resume is invisible by design — which means its side effects are
invisible too. The notice makes the invisible state visible to the human.

**3. How it works internally.** It doesn't, mechanically — it is a printed warning. That
honesty is the point: Colibri identified a *state-provenance* problem and shipped the cheapest
true fix.

**4. Strengths.** The war story names a real class: resumed state biases future behavior in
ways that masquerade as model bugs. Any system with persistent sessions has this class;
most never name it.

**5. Weaknesses & trade-offs.** A notice is the floor: it doesn't bind the session to its
owner, doesn't expire stale dispositions, doesn't distinguish "resumed 5 minutes later" from
"resumed 5 months and two model versions later," and can't detect bleed — only disclose the
possibility. Olympus's exposure is *wider* than Colibri's: bleed vectors include the distilled
state block (a summarizer's tone/language choices harden into every future prompt), ACE playbook
entries pinned forever, per-user working models, and — the sharpest — *cross-user* bleed on
shared gateways if a session is ever resumed under the wrong identity. Replay adds a subtle
vector Olympus already guards: stale `frozen_context` reads (replaystore.py's whole reason for
`frozen_context` is that mutable state injected into prompts must be provenance-tracked).

**6. Security implications.** This is the rubric where the war story becomes a security
control. Guards, in order of severity: (a) **owner binding** — `sessionlog.open` refuses a
`user` mismatch against the header (hard fail, no override env; cross-user bleed is a breach,
not a preference); (b) **age gate** — `OLYMPUS_RESUME_MAX_AGE` (default 30 d): older sessions
resume with facts (playbook) but a fresh conversational frame (verbatim tail dropped, state
block re-rendered with a staleness preamble); (c) **fingerprint drift notice** (§4) — the
componentized diff tells the user which assumptions changed under them.

**7. Scalability implications.** Guards are O(1) header checks at open; the bleed *probe*
(below) rides existing eval cadences. No new loops.

**8. Performance implications.** Zero on the hot path. The age gate actually *saves* tokens
(stale verbatim tails are pure cost).

**9. Maintainability implications.** All guard logic lives at one choke point
(`sessionlog.open` + the orchestrator's resume banner). The banner reuses the existing report
channel; Telegram/CLI/web each render it their native way.

**10. How Olympus should redesign/evolve it.** *Adopt* the notice (a resume banner: "Resuming
conversation from <age> — say 'fresh start' to begin clean", mirroring Colibri's CLI notice);
*exceed* it with the three guards above; and add — **beyond Colibri** — *bleed detection*: a
replay-gate-style probe (`replaygate` already runs `self_check` on a heartbeat cadence in an
isolated `gate` namespace) that runs one fixed English prompt against a fresh session vs. a
resumed decoy session (e.g. a journal seeded with non-English/off-domain turns) and flags when
the resumed answer's language/format diverges — turning "looked like a quantization bug for a
day" into a tripwire that fires within a cycle. Bounded spike: ~1 extra gated prompt per
replay-gate run; uses `GATE_MODEL` pricing; advisory (memory report), never CI-blocking, until
a quarter of data shows a stable false-positive rate.

**11. Final Olympus architecture.** In `sessionlog.py`: owner check + `age()` helper; in
`orchestrator`: the resume banner + age-gated frame reset; in `replaygate.py`: `bleed_probe()`
appended to `self_check` (escalation path reuses memory-report/Telegram plumbing, severity
below replay failure). Env: `OLYMPUS_RESUME_MAX_AGE` (seconds, 0 = off),
`OLYMPUS_RESUME_BANNER` (default on). Records: every triggered guard appends a `resume` record
with its decision — the audit trail Colibri's printed notice evaporates.

**12. Why the Olympus approach is superior.** Colibri disclosed the hazard; Olympus binds it
(owner), expires it (age), explains it (fingerprint diff), detects it (probe), and remembers it
(journal records). The probe results accumulate into the calibration record — per-model evidence
of how strongly resumed state biases behavior, which no fresh competitor deployment has.

---

## Open questions & research spikes

1. **Journal default-on benchmark (blocking the flip).** Measure `save_conversation` rewrite
   vs. `sessionlog.append` (fsync `off`/`turn`) on a small droplet at 50/200/1000-turn
   conversations, both FileStore and Postgres. Flip `OLYMPUS_SESSION_JOURNAL` default only on a
   clean win; keep the snapshot-equivalence invariant test either way. (~1 day.)
2. **Compaction-recall probe design.** How many golden Q/A pairs per conversation give a stable
   signal on whether ACE compaction loses answerable facts, within liveeval's budget
   (gate-cost rule, ROADMAP §0)? Spike: 3 eval conversations × 20 probes, one week of cadence
   data before any default changes.
3. **Cache-hit attribution across providers.** Anthropic returns cache counters; OpenAI-compat
   servers vary (vLLM exposes `prompt_tokens_details.cached_tokens`, others nothing). Decide the
   degrade story: ratio where counters exist, "unmeasured" (never zero) where they don't —
   an honest-gap label in `usage.py`, not a fabricated number. (~half a day, survey + shim.)
4. **Bleed-probe false-positive rate.** The decoy-resume probe judges divergence by
   language/format heuristics; a quarter of advisory-only data decides whether it can ever
   escalate louder than a memory report.
5. **Tension with absorption 02 (`ctxbudget`/`ctxheat`):** both docs claim the compaction safe
   point. Resolution proposal: `ctxbudget` decides *when/what* to fold; this domain's contract
   is only *where* (turn boundary, after delivery) and *how it persists* (a `compact` journal
   record + frozen state text). The synthesizer should ratify that split.
6. **Tension with the replay store:** journal (`sessions/`) and replay caches
   (`responses/`, `tool_results/`, `context/`) both describe a run. They are deliberately
   different truths (what-happened vs. re-executable-decisions) with different backup policies
   (`BACKUPS.md`: journals in, replay caches out by default) — but a `run_id` cross-reference
   field in `turn` records should link them so an auditor can pivot between the two. Confirm no
   third store is ever added.
7. **Sovereign-mode prefix reuse.** When `OLYMPUS_SOVEREIGN=1` routes to vLLM/llama.cpp, stable
   prefixes enable true local KV reuse (`--enable-prefix-caching`). Worth a documented recipe in
   `docs/SOVEREIGNTY.md` once §5 telemetry can prove the effect locally — measurement first,
   recipe second.
