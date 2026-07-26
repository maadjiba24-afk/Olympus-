# Wave 1 Implementation Specification — Colibri Absorption

**Authority chain:** production code → `docs/ROADMAP.md` → `docs/absorption/00-SYNTHESIS.md`
→ `13-review-gaps.md` → domain docs 01–12 → the Colibri analysis.
**Scope:** Wave 1 only (the measurement substrate). Nothing here activates adaptive
routing, learned heat, speculative execution, local inference, or prefetch.
**Baseline:** full suite green at spec time — `3649 passed, 26 skipped in 140.19s`.

## 0. Implementation map (Phase-0 reconnaissance results)

Verified against the code (file:line refs checked at spec time):

- **Execution path:** `cli._chat` → `tui.run` → `orchestrator.Olympus.ask_stream` →
  `_pipeline` (route `:1368` → plan `:1401` → `_dispatch_dag` `:1177` → verify `:1430`
  → review `:1457` → `_synthesize` `:970`). Decisions are recorded via
  `trace.Trace.decision`; runs flush to `MEMORY_DIR/traces/YYYYMMDD.jsonl`.
- **Session persistence today:** `memory.save_conversation` rewrites the whole
  `conversations/<id>.json` array every turn (atomic tmp+replace, **no fsync**);
  `load_conversation` returns `[]` on `JSONDecodeError` — **silent history loss**.
- **Replay today:** `replaystore.py` (request-hash freeze of LLM responses, tool
  results, frozen context; `ReplayDivergence`), `orchestrator.replay_run` (env-flag
  reproduction + `trace.diff_decisions`), `replaygate.py` (weekly live gate). The
  decision path is PRNG-free; parallel nondeterminism is canonicalized
  (`trace.canonicalize_parallel_since`).
- **Usage today:** `usage.record(model, in, out)` → daily aggregate JSON; `llm.py:326`
  reads Anthropic `cache_read_input_tokens`/`cache_creation_input_tokens` and
  **discards the split**. No output reserve; three disjoint price tables; token
  estimation is `chars//4` in five places.
- **Tool-call repair today:** `toolcall_repair.py` is pure extraction/recovery
  (balanced-brace scan, double-encode unwrap, shape A/B/C, known-names refusal gate);
  **no schema coercion, no truncated-tail close, no counters**; sole tool-loop wire-in
  at `openai_compat.run_agent:289-337`; arguments are never validated against
  `input_schema` before execution.
- **Trajectories today:** no per-tool sequence exists; specialist-level sequences are
  reconstructable from trace events (`dispatch`, `dag.level`, spans) + `plan`
  decisions + `routing_outcomes` (cap 2000).

**Existing modules extended:** `memory.py`, `replaystore.py`, `orchestrator.py`,
`connectors.py`, `usage.py`, `llm.py`, `doctor.py`, `toolcall_repair.py`,
`openai_compat.py`, `heartbeat.py`, `cli.py`, `evals.py` (consumed, not modified).
**New modules (admission test applied — see per-capability):** `olympus/sessionlog.py`,
`olympus/ctxbudget.py`, `olympus/modelgate.py`, `olympus/coupling.py` — **4 of the
synthesis's 14-module cap.** Rejected as new modules: replay fixtures (state already
owned by `replaystore`), non-interference gate (a script + CI job), cache telemetry
(extends `usage`), tool repair (extends `toolcall_repair`).
**Schemas migrated:** usage daily-file rows gain optional keys (backward-compatible);
conversation snapshots gain a journal beside them (snapshot format unchanged).
**Public interfaces that must remain stable:** `usage.record(model, in, out)`
positional form; `memory.load_conversation/save_conversation` signatures;
`toolcall_repair.recover_tool_call/repair_arguments` signatures; `olympus` CLI
commands (Wave 1 adds **no new commands** — new surfaces ride flags on existing
commands to avoid capability-manifest churn); trace decision schema v1.
**Obsolete code to retire eventually (not in Wave 1):** the three duplicated
synthesis-prefix assemblies in `orchestrator.py` (noted for Wave 2 prefix-stability
work); `openai_server.estimate_tokens` fabricated usage once calibration lands.

**Config conventions binding on all capabilities:** new knobs are `OLYMPUS_*`,
read via zero-arg functions (runtime-flippable), default **off** unless the change
is provably additive; every new knob is listed in `.env.example` and exercised by a
test; any new flag that alters the decision path must be added to BOTH `tr.meta`
and `orchestrator.replay_run`'s env-reproduction list (the flag-pairing invariant).

---

## 1. Capability specifications

Template per capability: problem → current behaviour → proposed behaviour →
invariants → schema → interfaces → ownership → threats → failure behaviour →
migration → rollback → tests → benchmarks → acceptance → exclusions → files →
PR unit.

### C1. Sealed session journal (`olympus/sessionlog.py`) — PR 2

1. **Problem.** Conversation persistence is a whole-file rewrite per turn with no
   fsync and silent `[]` on corruption: a torn write or crash loses the entire
   session invisibly.
2. **Current behaviour.** `memory.save_conversation` (tmp+`os.replace`);
   `load_conversation` → `[]` on parse error; no integrity, no recovery, no record
   of what was lost.
3. **Proposed.** An append-only journal per session beside the snapshot:
   `MEMORY_DIR/sessions/<safe_id>.journal.jsonl`. Each turn appends one sealed
   record; the existing snapshot becomes a derived view (still written, still the
   default read path). On snapshot corruption, recovery rebuilds from the journal's
   verified prefix instead of returning `[]`.
4. **Invariants.**
   - I-J1: records are append-only; a committed record is never modified in place.
   - I-J2: `seq` is dense and monotonic per session starting at 1.
   - I-J3: each record carries `sha` = SHA-256 over the canonical serialization of
     the record with `sha` blanked, and `prev` = previous record's `sha`
     (`prev=""` for seq 1) — hash-linked.
   - I-J4: canonical serialization = `json.dumps(sort_keys=True,
     separators=(",",":"), ensure_ascii=False)` (matches `replaystore` canonical
     form).
   - I-J5: the seal (`sha`) is computed over the complete payload, and the record
     line is written in one `write()` call ending in `\n` — a torn tail is
     detectable as a non-verifying final line ("commit = the line verifies").
   - I-J6: replay/recovery stops at the last verified record; verified truncation
     of a torn tail is the ONLY permitted mutation, and only ever of the final
     unverifiable line.
   - I-J7: reject-never-repair — a mid-journal record that fails verification
     (hash mismatch, seq gap/duplicate, unknown `v` major) stops recovery at the
     boundary; the file is quarantined by copy to
     `sessions/quarantine/<id>.<ts>.journal`, a structured integrity event is
     emitted (`errors.capture` + journal-status record), and resume proceeds only
     from the verified prefix with an explicit notice — never silently past it.
   - I-J8: durability policy is explicit: `flush()` after every append;
     `os.fsync` per append when `OLYMPUS_SESSION_FSYNC=always`, else fsync on
     close/turn-end (`auto`, default). Policy is documented, not implied.
   - I-J9: isolation — path derives from `memory.safe_id(conversation_id)` under
     `config.MEMORY_DIR`; no cross-session or cross-user reads.
   - I-J10: bounded replay — recovery reads at most
     `OLYMPUS_SESSION_JOURNAL_MAX_MB` (default 64) from the tail; beyond that,
     recovery refuses with an operator action (see failure behaviour).
5. **Schema.** JSONL; record:
   `{"v":"1.0","seq":int,"ts":float,"kind":"turn"|"reset"|"tombstone"|"snapshot_mark",
   "conversation_id":str,"payload":{...},"prev":str,"sha":str}`.
   `turn` payload: `{"messages":[{"role","content"},...]}` (the delta appended that
   turn). `reset` marks history clear. `tombstone` supports user-data deletion:
   payload names the seq range logically deleted; compaction physically drops it.
   `snapshot_mark` records `{"snapshot_sha":str,"through_seq":int}` — the verifiable
   compaction boundary. `v` uses major.minor: unknown **major** ⇒ refuse that
   record (I-J7); unknown minor ⇒ read known fields.
6. **Interfaces.** `append_turn(conversation_id, messages) -> int(seq)`;
   `read_verified(conversation_id) -> tuple[list[records], JournalStatus]`;
   `recover_history(conversation_id) -> list[messages] | None`;
   `compact(conversation_id, through_seq) -> None` (writes snapshot_mark, rewrites
   file atomically dropping tombstoned/compacted prefix — interruption-safe:
   tmp+replace, old file valid until replace); `journal_status(conversation_id)
   -> JournalStatus` (`ok|torn_tail_truncated|quarantined|absent|oversize`);
   `delete_session(conversation_id)` (journal + snapshot + quarantine removal).
   Internal hook: `memory.save_conversation` calls `sessionlog.append_turn` when
   enabled; `memory.load_conversation` consults `sessionlog.recover_history` only
   when the snapshot is corrupt.
7. **Ownership.** `sessionlog.py` owns journal format, seals, recovery, compaction,
   retention. `memory.py` remains the read/write facade. Admission test: owns
   distinct durable state ✓; enforces an independent invariant (sealed
   append-only persistence) ✓; independent failure boundary/lifecycle ✓ — 3/4.
8. **Threats.** Journal tampering (payload or hash edit) → detected by I-J3
   verification; rollback attack (truncating committed suffix) → detectable across
   restarts only if paired with external anchoring (out of Wave-1 scope —
   documented residual; the hash chain makes *internal* splices detectable);
   cross-user leakage → I-J9 path derivation + no shared state; disk-full →
   append raises, caught, reported via `errors.capture`, turn still completes
   (journal is additive; snapshot remains source of truth when journaling fails);
   secrets — journal stores exactly what the snapshot already stores (conversation
   text), same trust class, same directory permissions (0700 memory dir
   convention).
9. **Failure behaviour.** Append failure: log + continue (never blocks a reply).
   Corruption on read: quarantine + structured event + verified-prefix resume +
   user-visible notice via the existing degraded-notice path; never an exception
   to the caller. Oversize journal: `journal_status=oversize`, recovery refuses,
   operator action = `compact()` or `delete_session()`; snapshot path unaffected.
10. **Migration.** None required: journals are created lazily on first append;
    absent journal = current behaviour exactly. Snapshot format untouched.
11. **Rollback.** `OLYMPUS_SESSION_JOURNAL=off` stops appends and recovery
    consults; journals on disk are inert data. Delete `sessions/` to reclaim.
12. **Tests** (fault-injection suite, `tests/test_sessionlog.py` +
    `tests/test_sessionlog_faults.py`): kill-during-append simulated by truncating
    the file at every byte offset of the final record (torn tail ⇒ verified-prefix
    recovery, exactly one record lost, status `torn_tail_truncated`); partial
    write; mid-file payload mutation ⇒ quarantine + stop at boundary; mid-file
    hash mutation ⇒ same; reordered records ⇒ seq-gap refusal; duplicated seq ⇒
    refusal; unknown major version ⇒ refusal at that record; concurrent writers
    (two threads appending — `proclock`-guarded, seqs stay dense); disk-full
    (mocked `write` raising `OSError(ENOSPC)`) ⇒ reply unaffected + error
    captured; permission failure ⇒ same; compaction interruption (kill between
    tmp write and replace) ⇒ old journal intact; tombstone + compact drops
    payload bytes from disk (deletion verified by absence in raw file bytes);
    snapshot-corrupt → journal recovery returns full history (the silent-`[]`
    bug becomes recovery).
13. **Benchmarks.** Append overhead measured in-test: p50 per-turn append with
    fsync=auto on tmpfs and disk; recorded in the completion report.
14. **Acceptance.** All fault tests pass; zero corrupted records accepted (I-J7
    tests); recovery from snapshot corruption returns non-empty history where the
    journal is intact; overhead < 5 ms p50 per turn (fsync=auto, local disk).
15. **Exclusions.** No key-rotation of seals (SHA-256 integrity, not
    authentication — signing via `witness` is a Wave-2 option); no cross-restart
    rollback anchoring; no journal-first reads while the snapshot is healthy.
16. **Files.** New: `olympus/sessionlog.py`, tests. Modified: `olympus/memory.py`
    (two hooks), `.env.example`.
17. **PR unit:** PR 2.

### C2. Deterministic replay fixtures (extends `replaystore`) — PR 3

1. **Problem.** Replay exists but is bound to a live `MEMORY_DIR`: there is no
   portable, sanitized fixture that can be committed, shared, or run in CI; and
   the four replay modes are implicit.
2. **Current.** `OLYMPUS_REPLAY` + frozen `responses/`, `tool_results/`,
   `context/`; `orchestrator.replay_run` reproduces env flags and diffs decisions;
   the weekly `replay-gate.yml` re-runs live prompts.
3. **Proposed.** Fixture export/import in `replaystore`:
   `export_fixture(run_id, path)` bundles the trace record, every frozen response/
   tool-result/context blob referenced by the run, plus a manifest with: config
   fingerprint (the same flag set `replay_run` reproduces), prompt-template
   hashes (`modelgate.prompt_manifest()`, C6), pool model identifiers, schema
   versions, and the final-reply SHA-256. `import_fixture(path, memory_dir)`
   materializes it. **Mode taxonomy made explicit** in code and docs:
   `exact` (frozen responses + frozen tools + frozen context ⇒ deterministic;
   this is what the gate uses), `provider-response` (same, alias — live calls
   never occur), `structural` (compare `decision_core` sequences only — used by
   `diff_decisions` today), `live` (replaygate — explicitly NOT deterministic,
   never asserted equal). No pretence that live calls are deterministic.
4. **Invariants.** I-R1: a fixture round-trip (`export` → fresh dir → `import` →
   `replay_run`) yields `diff_decisions == []` and the same final-reply hash.
   I-R2: fixtures contain no credentials: export scrubs env-typed fields,
   refuses if any blob matches the secret-pattern screen (`sk-`, `key=`,
   Authorization headers; screen list in code), and records the scrub report in
   the manifest. I-R3: import never overwrites existing store entries unless
   `force=True`.
5. **Schema.** Fixture = a directory (or `.tar.gz`): `manifest.json`
   (`{"v":"1.0","run_id","flags":{...},"models":[...],"prompt_manifest":{...},
   "reply_sha","files":[{path,sha}], "scrub":{...}}`), `trace.json`,
   `responses/*.json`, `tool_results/*.json`, `context/*.json`. Every file
   sha-listed in the manifest; import verifies all hashes (reject-never-repair).
6. **Interfaces.** `replaystore.export_fixture(run_id, dest) -> Path`,
   `replaystore.import_fixture(src, *, force=False) -> str(run_id)`;
   CLI: `olympus replay <run_id> --export-fixture PATH` and
   `olympus replay --fixture PATH` (flags on the existing command — no new
   command, no capabilities churn).
7. **Ownership.** `replaystore` (it already owns all frozen state). No new module
   (fails admission: no new durable state class, no new invariant owner).
8. **Threats.** Secrets in fixtures → I-R2 screen + scrub report + test with
   planted secrets; fixture tampering → manifest sha verification on import;
   replay-store poisoning via import → `force` guard + hash checks.
9. **Failure.** Export of a run with missing blobs → typed error listing missing
   refs (no partial fixture); import hash mismatch → refuse entire fixture.
10. **Migration.** None; additive.
11. **Rollback.** Flags unused ⇒ inert; remove functions.
12. **Tests.** Round-trip determinism (record a fake-client run → export → import
    into fresh MEMORY_DIR → replay → zero diffs + same reply hash); secret-scrub
    (planted `sk-...` in a response ⇒ export refuses); tamper (flip one byte ⇒
    import refuses); missing-blob export refusal; CLI flags.
13. **Benchmarks.** n/a (tooling path).
14. **Acceptance.** I-R1 proven in CI with a committed fixture under
    `tests/fixtures/replay/`; I-R2 test passes.
15. **Exclusions.** No cassette recording of *live* provider traffic in CI; no
    fixture format for streaming deltas (final-message level only, matching
    `replaystore` today).
16. **Files.** Modified: `olympus/replaystore.py`, `olympus/cli.py` (two flags),
    tests + committed fixture.
17. **PR unit:** PR 3.

### C3. Observability non-interference gate — PR 4

1. **Problem.** "Instrumentation must not alter decisions" is asserted by
   convention, not proven per commit. One live hole exists: `connectors.emit
   ("pre_llm_call", params)` hands observe-only plugins the *live mutable* params
   dict after `request_hash` is computed (`llm.py:281`,`:296`) — a mutating hook
   silently desyncs record/replay.
2. **Current.** `otel` is off unless endpoint set, skipped under replay, exports
   post-flush; `metrics` is HTTP-only; `usage` feeds budget gates but is
   replay-disabled at the one decision-affecting site.
3. **Proposed.** (a) Close the hole: `connectors.emit` passes a deep copy for
   observe-only events (`pre_llm_call`, `post_llm_call`) — mutation of the copy
   is discarded by design (matches the documented observe-only contract; the
   mutating tool hooks `emit_pre_tool`/`emit_post_tool` are untouched).
   (b) `scripts/noninterference_gate.py`: load the committed replay fixture
   (C2), run `replay_run` twice in-process — pass 1 with all optional
   instrumentation OFF; pass 2 with instrumentation ON (`OLYMPUS_OTLP_ENDPOINT`
   set with a stub poster injected, metrics recording, usage footers on, a
   registered observe-only test plugin that *attempts* mutation) — and compare
   `decision_core` sequences and final-reply hashes. Any diff ⇒ exit 1.
   Exit contract: 0 pass/clean-skip, 1 behavioural diff, 2 harness error (house
   gate contract). (c) CI: a keyless job in `ci.yml` running the script on every
   PR. (d) The forbidden-effects list (no prompt mutation, no RNG consumption, no
   cache-key change, no network, no admission change, no exception swallowing
   that alters flow) is encoded as assertions in the gate where mechanically
   checkable (stub poster records zero real sockets; params-copy test).
4. **Invariants.** I-N1: for the fixture corpus, instrumentation-on vs -off
   produces `diff_decisions == []` and identical reply hashes. I-N2:
   `connectors.emit` observe-only events cannot mutate caller state (enforced by
   copy; regression-tested by a deliberately hostile plugin).
5. **Schema.** Gate report JSON to stdout: `{"fixtures":[{"run_id","diffs":n,
   "reply_match":bool}],"verdict":"pass"|"fail"}`.
6. **Interfaces.** Script only; no library surface change beyond the emit copy.
7. **Ownership.** Script + `connectors.py` one-line semantics fix.
   No new module.
8. **Threats.** A malicious plugin using the observe hook to poison prompts —
   closed by I-N2. Gate evasion by fixtures too trivial to exercise stages —
   mitigated: fixture must contain ≥ route+plan+dispatch+verify decisions
   (asserted by the gate).
9. **Failure.** Timing/trace-id/timestamp fields differ freely (`_VOLATILE`
   already excluded by `decision_core`); everything else fails the gate.
10. **Migration.** Plugins that (incorrectly) relied on mutating `pre_llm_call`
    params break — this was never contract; noted in migration notes.
11. **Rollback.** Remove CI job; revert emit copy (one commit).
12. **Tests.** Hostile-plugin mutation test; gate self-test (inject an artificial
    decision diff ⇒ exit 1); gate green on the committed fixture.
13. **Benchmarks.** Gate wall-time < 60 s in CI (keyless, no network).
14. **Acceptance.** CI job green on the fixture; hostile-plugin test proves
    non-interference; hole regression-tested.
15. **Exclusions.** Concurrency-ordering interference beyond what
    `canonicalize_parallel_since` already normalizes; retry-timing effects (no
    retries occur under exact replay).
16. **Files.** New: `scripts/noninterference_gate.py`, tests. Modified:
    `olympus/connectors.py` (copy semantics), `.github/workflows/ci.yml` (job).
17. **PR unit:** PR 4.

### C4. Calibrated context budgeting (`olympus/ctxbudget.py`) — PR 5

1. **Problem.** Token estimation is uncalibrated `chars//4` in five places; there
   is no output reserve; compaction failure silently drops history
   (`orchestrator.py:1729`); budgets are uncoordinated.
2. **Current.** `config.history_token_budget(model)` = 35% of a substring-matched
   window table; `_estimate_tokens` chars//4; `MAX_TOKENS=16000` flat, never
   subtracted from any input budget.
3. **Proposed.** One arbiter module:
   - `estimate_tokens(text|messages, *, provider, model) -> int` using calibrated
     chars-per-token ratios (cold-start prior 4.0).
   - Calibration: `observe(provider, model, prompt_chars, reported_input_tokens)`
     called from the two provider seams that already see reported usage
     (`llm.py`, `openai_compat.py`); EMA-ratcheted ratios persisted to
     `MEMORY_DIR/ctx_calibration.json` keyed by
     `(provider, model, estimator_version, prompt_template_version)` with sample
     count + date range; writes via tmp+replace under `proclock`.
   - `plan(blocks, *, provider, model) -> BudgetPlan`: accounts system prefix,
     history, task, tool schemas (chars of serialized defs), tool-output
     allowance, **output reserve** (`min(config.MAX_TOKENS, window·R)` with
     `OLYMPUS_CTX_OUTPUT_RESERVE_FRACTION`, default 0.20), provider overhead
     constant, and retry/repair reserve; returns fit | needs_compaction |
     exceeded(typed `ContextExceeded` with the block accounting attached).
   - Wiring (flag-gated `OLYMPUS_CTX_BUDGET`, default **off** ⇒ byte-identical):
     `orchestrator._estimate_tokens` delegates to the calibrated estimator;
     `_maybe_compact` uses `plan()`'s needs_compaction verdict; the compaction
     *failure* fallback emits `tr.event("context.truncated", dropped=n)` always
     (event addition is observability-additive) and, when the flag is on,
     surfaces a degraded-notice instead of dropping silently.
   - Silent truncation prohibition (flag on): any path that would drop context
     emits the event and either compacts through the existing ACE path, refuses
     with `ContextExceeded` (non-interactive callers), or discloses in the reply
     footer (interactive) — per synthesis ruling R6.
4. **Invariants.** I-C1: flag off ⇒ no behavioural change (replay-verified).
   I-C2: `plan()` never returns fit when Σ(estimates)+reserve > window.
   I-C3: calibration monotonically converges: |error| on the calibration set is
   non-increasing per ratchet (EMA with clamped step). I-C4: estimator error
   for calibrated (provider,model) pairs ≤ declared tolerance (±15%) on the
   recorded corpus; uncalibrated pairs use the prior and are labeled so.
   I-C5: flag on ⇒ zero silent drops: every context reduction has a trace event.
5. **Schema.** `ctx_calibration.json`:
   `{"v":1,"ratios":{"<provider>/<model>":{"cpt":float,"n":int,"first":ts,
   "last":ts,"est_ver":1,"tmpl_ver":str}}}`.
6. **Interfaces.** Above, plus `ctxbudget.window_for(provider, model)` reading
   `config.context_window` (single source; no second table — synthesis tension
   resolved: ride the existing table).
7. **Ownership.** New module: owns calibration state ✓, enforces the
   no-silent-truncation invariant ✓, independent testable lifecycle ✓ — 3/4.
8. **Threats.** Calibration poisoning via malformed provider usage responses →
   observe() validates (ints, sane bounds 0.5≤cpt≤20, else discarded +
   `errors.capture`); denial-of-wallet via forced refusals → refusal costs
   nothing (it prevents spend).
9. **Failure.** Missing/corrupt calibration file → cold-start prior + WARN in
   doctor (reject-never-repair: corrupt file quarantined, not patched).
   Malformed usage response → sample discarded, counted.
10. **Migration.** None (new file; flag off).
11. **Rollback.** Flag off; delete calibration file.
12. **Tests.** Extreme tool schemas (100 KB serialized defs); oversized tool
    outputs; multilingual (CJK chars-per-token ≠ 4 — calibration corrects);
    code-heavy prompts; long sessions (compaction verdicts); estimator drift
    (feed shifted ratios ⇒ ratchet converges, I-C3); output-reserve exhaustion
    (plan flags exceeded, never negative reserve); retry accounting; memory-
    retrieval overload (recall block beyond budget ⇒ needs_compaction);
    malformed usage rows (discarded); flag-off byte-identity via replay diff.
13. **Benchmarks.** Estimator accuracy report on a recorded multilingual corpus:
    mean |error| chars//4 vs calibrated (must improve or tolerance re-declared);
    plan() overhead < 1 ms.
14. **Acceptance.** I-C1..I-C5 tests green; declared tolerance met on the corpus;
    no silent truncation with flag on (grep-level: the `self.history = keep`
    fallback is event-covered).
15. **Exclusions.** No T0–T3 tier *store* (that is Wave-2 `ctxheat` territory);
    no wiring into `openai_server.estimate_tokens` (noted for retirement); tiers
    are documented mapping only in Wave 1.
16. **Files.** New: `olympus/ctxbudget.py`, tests. Modified: `olympus/
    orchestrator.py` (3 seams, flag-gated), `olympus/llm.py` + `olympus/
    openai_compat.py` (observe() one-liners), `.env.example`.
17. **PR unit:** PR 5.

### C5. Prompt-cache usage telemetry (extends `usage`) — PR 6

1. **Problem.** Cache-token splits are read and discarded; costs are computed
   cache-blind; nothing can say whether prompt caching works.
2. **Current.** `llm.py:326-333`/`:389-395` sum three fields into `in_tokens`;
   `usage.PRICES` two-tier; no liveness signal.
3. **Proposed.** Canonical schema: `usage.record(model, in_tokens, out_tokens, *,
   cache_read=0, cache_creation=0, provider="", prefix_fp="")` — `in_tokens`
   remains the **uncached** input count; totals stay backward-compatible
   (existing keys unchanged; new keys added: `cache_read`, `cache_creation`,
   `uncached_in`, per-model and session). Cost:
   `estimate_cost(..., cache_read, cache_creation)` applies configurable
   multipliers `OLYMPUS_CACHE_READ_MULT` (default 0.1) /
   `OLYMPUS_CACHE_WRITE_MULT` (default 1.25) to the input price — documented as
   published-price defaults, overridable, **no TTLs hardcoded anywhere**.
   `llm.py` passes the split (both sites); `openai_compat.py` reads
   `prompt_tokens_details.cached_tokens` when present. Stable-prefix
   fingerprint: sha256[:12] of the system-block text that `llm._cache_control`
   marks cacheable, recorded per call, aggregated per day — a layout change
   shows as a fingerprint change with a hit-rate cliff. Liveness:
   `usage.cache_stats(days)` → hit rate, savings estimate, active/inert verdict;
   surfaced in `olympus usage` output and as a doctor check ("prompt caching:
   configured but 0 cache reads across N≥20 calls ⇒ WARN inert + knob").
4. **Invariants.** I-U1: positional callers of `usage.record` keep identical
   behaviour. I-U2: `in+cache_read+cache_creation` equals the provider-reported
   total input (asserted where provider reports all three). I-U3: no TTL
   constants in code. I-U4: fingerprint changes never alter the request
   (observability-only — covered by the C3 gate).
5. **Schema.** Daily file rows gain optional int keys; absent = 0 (old files read
   fine — migration-free).
6. **Interfaces.** Above + `usage.cache_stats()`; doctor check `_cache_checks()`.
7. **Ownership.** `usage.py` (extension; fails module admission deliberately).
8. **Threats.** Secrets in telemetry: fingerprint is a hash, never text; day
   files carry counts only.
9. **Failure.** Providers not reporting cache fields ⇒ zeros recorded, liveness
   says "no signal from provider" (distinct from "caching inert").
10. **Migration.** None (additive keys).
11. **Rollback.** Revert commit; old files remain readable throughout.
12. **Tests.** Split preserved end-to-end from a fake Anthropic message with
    cache fields; cost math with multipliers; old-format day files load; footer
    unchanged for positional calls; liveness verdicts (active / inert / no
    signal); fingerprint stability across identical prompts and change on layout
    edit; openai cached_tokens path.
13. **Benchmarks.** n/a (accounting).
14. **Acceptance.** The system can answer, from recorded data: caching active?
    prefix producing hits? measured hit rate? estimated savings? layout
    regression? — demonstrated in tests against synthetic recorded days.
15. **Exclusions.** No per-provider TTL probing (needs live traffic — Wave 2
    measurement); no pricing-table unification (three tables noted, out of
    scope).
16. **Files.** Modified: `olympus/usage.py`, `olympus/llm.py`,
    `olympus/openai_compat.py`, `olympus/doctor.py`, `olympus/cli.py` (usage
    output), `.env.example`; tests.
17. **PR unit:** PR 6.

### C6. Provider-drift tripwire (`olympus/modelgate.py`) — PR 7

1. **Problem.** A provider can change the model behind a pinned name; today the
   quality gate reports fingerprint drift without gating, golden results are not
   keyed by provider/template/flags, and there are no severity classes or spend
   budget.
2. **Current.** `evals.run` + `regression_check` + `quality_baseline.json`
   (single-provider provenance); weekly workflow; `gate_prompt` for prompt
   changes.
3. **Proposed.** `modelgate.py` consuming `evals`:
   - `prompt_manifest() -> {"stem": sha256[:12]}` over `prompts/*.md` + a
     combined `tmpl_ver` hash (also consumed by C2 fixtures and C4 calibration
     keys).
   - `run_gate(settings, *, corpus="golden") -> GateResult` executing the golden
     set with per-item spend tracking; results appended (JSONL,
     `MEMORY_DIR/modelgate/results.jsonl`) keyed by
     `{provider, model, model_revision?, tmpl_ver, olympus_commit, flags_fp,
     eval_set_ver, ts, scores, cost}`.
   - Severity policy vs the keyed baseline: `info` (Δ within tolerance),
     `warn` (single-domain regression, reproduced per `confirm_regressions`),
     `freeze` (multi-domain regression ⇒ writes a routing-freeze marker file
     that `modelpin`-style selection surfaces refuse to *newly* select the
     member; existing pins keep working — fail-visible, not fail-dead),
     `block` (regression on verify/refusal domains ⇒ doctor FAIL),
     `quarantine` (provider errors/malformed responses above threshold ⇒
     member marked in the freeze file with reason).
   - Corpus: extends `benchmarks.json` with a Wave-1 drift set covering routing
     JSON discipline, plan-graph validity, refusal, tool selection, tool-arg
     generation, structured output, code, factual verification, adversarial/
     injection probes, malformed-response handling, long-context marker recall,
     multilingual — as `benchmarks_extra.json`-style items (existing loader),
     tagged `drift:<domain>`.
   - Budget: hard per-run cap `OLYMPUS_DRIFT_BUDGET_USD` (default 1.00) enforced
     by cost accumulation mid-run (stop + partial verdict "budget_exhausted",
     never overrun); registers in the **existing** heartbeat cadence pattern
     (`config.DRIFT_GATE_EVERY`, default 0 = off — no uncontrolled job family)
     and calls `usage.check_budget()` first like every other job.
4. **Invariants.** I-M1: gate never exceeds its dollar cap. I-M2: every result
   row carries the full key. I-M3: severity actions are reversible files, not
   code state (delete marker ⇒ unfrozen). I-M4: default-off cadence; CLI/CI
   invocation explicit.
5. **Schema.** Results JSONL row above; freeze marker
   `MEMORY_DIR/modelgate/freeze.json`
   `{"<provider>/<model>": {"severity","reason","ts","result_ref"}}`.
6. **Interfaces.** `run_gate`, `prompt_manifest`, `frozen_members()`,
   `classify(result, baseline) -> Severity`; surfaced via existing `olympus
   scores`-adjacent CLI flag (`olympus scores --drift`); doctor check reads
   freeze file.
7. **Ownership.** New module: durable state (results ledger + freeze markers) ✓,
   independent invariant (no silent provider drift) ✓, independent lifecycle ✓.
8. **Threats.** Provider compromise serving degraded model → detected by
   corpus; evidence-store poisoning → results file append-only, rows carry
   commit+key; adversarial corpus items are fixtures, never executed as code.
9. **Failure.** Provider unreachable ⇒ `skipped` verdict (exit-0 clean-skip
   contract), never a false freeze; malformed judge output ⇒ item error counted
   toward quarantine threshold, not scored 0.
10. **Migration.** None; new files. Baseline seeding: first keyed run writes the
    baseline for its key (no gate until a baseline exists for that key —
    explicit "no baseline" verdict).
11. **Rollback.** Cadence off by default; delete `modelgate/` state; module
    inert.
12. **Tests.** Fixture-driven: fake provider A responses (baseline) vs drifted
    responses (fake provider B) through a stubbed `evals.run` ⇒ severity ladder
    (info/warn/freeze/block) classified correctly; reproduce-before-believe
    honored (single-run regression stays `warn-pending` until confirmed);
    budget cap stops mid-corpus deterministically; freeze marker honored by
    selection surface test; keying completeness; clean-skip on missing key.
13. **Benchmarks.** Cost per full drift run on the stub corpus (item count ×
    declared per-item cap) recorded in the completion report.
14. **Acceptance.** Controlled-drift fixture detected at correct severity (CI,
    keyless via stubs); budget invariant test green; no new heartbeat job
    enabled by default.
15. **Exclusions.** No live multi-provider comparative runs in CI (keyless); no
    automatic baseline refresh (operator command only); routing-freeze
    integrates with pool *selection surface* only, not mid-run failover chains
    (Wave 2).
16. **Files.** New: `olympus/modelgate.py`, `olympus/benchmarks_drift.json`,
    tests. Modified: `olympus/heartbeat.py` (+`config.py` cadence var),
    `olympus/doctor.py`, `olympus/cli.py` (flag), `.env.example`.
17. **PR unit:** PR 7.

### C7. Predictability report (`olympus/coupling.py`) — PR 8

1. **Problem.** Prefetch is banned until predictability is proven (synthesis
   ruling R10 / Colibri's LOOKA-first discipline); no measurement exists.
2. **Current.** Traces hold per-run specialist sequences (dispatch/dag.level
   events, plan decisions); `routing_outcomes` holds labels; no per-tool
   sequences exist (recon-verified) — predictors are specialist/plan-level.
3. **Proposed.** Offline-only analyzer reading existing traces:
   predictors P1 marginal frequency, P2 previous-specialist conditional,
   P3 task-keyword, P4 plan-adherence (predict executed set from plan decision).
   For each: recall@k, precision, wasted-work rate, would-have-avoided latency
   (from span timings), estimated cost of acted-on predictions, false-positive
   *security* note (which predictions would have touched gated tools — always
   listed, never acted), cache/eviction impact (est. bytes of context warmed),
   Wilson confidence intervals, split by task class (`routing_outcomes.
   task_type`). Output: JSON + rendered table via `olympus routing-stats
   --predictability`. Acceptance floors (from domain 08): a predictor qualifies
   for *future* prefetch consideration only if recall@2 ≥ 0.6 with CI half-width
   ≤ 0.1 on ≥ 200 runs. **No prefetch code, no activation path, network
   speculation prohibited** — the module contains no side-effectful execution at
   all in Wave 1.
4. **Invariants.** I-P1: read-only over `MEMORY_DIR` (enforced: module performs
   zero writes outside its report path). I-P2: report always states sample
   sizes and abstains below n=50 ("insufficient data", not numbers).
5. **Schema.** Report JSON `{"v":1,"n_runs","by_predictor":{...},"floors":
   {"pass":bool,...}}` written to stdout / optional `--out`.
6. **Interfaces.** `coupling.predictability_report(days=30) -> dict`; CLI flag on
   existing `routing-stats`.
7. **Ownership.** New module per synthesis R10 (future coupling tables + the one
   orchestrator hook will live here in Wave 3 — the module boundary is the
   admission argument: independent lifecycle, future durable state, and keeping
   prefetch code OUT of the orchestrator).
8. **Threats.** None material (read-only); report may reveal usage patterns ⇒
   stays in MEMORY_DIR/stdout, never exported.
9. **Failure.** No traces ⇒ "insufficient data" report, exit 0.
10. **Migration/rollback.** None/none.
11. **Tests.** Synthetic trace corpus with known conditional structure ⇒
    P2 recall matches analytically expected value ±ε; abstention below n;
    floors logic; read-only guarantee (fs audit in test); CLI rendering.
12. **Benchmarks.** Runtime < 5 s on 1,000 synthetic runs.
13. **Acceptance.** Floors machine-evaluated; **prefetch remains disabled** —
    asserted by absence test (no `OLYMPUS_PREFETCH` consumer exists).
14. **Exclusions.** Coupling-table persistence, orchestrator hooks, any warmers.
15. **Files.** New: `olympus/coupling.py`, tests. Modified: `olympus/cli.py`
    (flag).
16. **PR unit:** PR 8 (first half).

### C8. Tool-call recovery extension (extends `toolcall_repair`) — PR 8

1. **Problem.** Repair has no schema-typed coercion, no principled truncated-tail
   recovery, no telemetry; repaired calls execute without validation against the
   tool's `input_schema`.
2. **Current.** Ladder A–E (recon §1); wire-in `openai_compat.run_agent`;
   failures degrade to `{}`/`None` silently; no counters.
3. **Proposed.** Typed ladder, explicit rungs with per-rung outcome:
   1 strict parse (existing fast path) → 2 **schema validation**
   (`validate_arguments(args, input_schema)`: required keys present, primitive
   type checks; unknown keys allowed-but-flagged) → 3 safe syntactic
   normalization (existing balanced-scan/fence/double-encode — ephemeral data
   only) → 4 constrained repair: `coerce_arguments(args, schema)` (schema-typed:
   `"5"`→5 for integer params, `"true"`→bool, number→string **only** for
   string-typed params — digits in string params stay strings; never invents
   keys) and `close_truncated(text)` (append missing closers only when
   unambiguous: single open object/string at EOF, no trailing partial key) →
   5 regenerate (existing loop retry — unchanged, recorded) → 6 refuse
   (unknown-name gate stays absolute). **Execution precondition:** a call that
   reached execution MUST have passed rung 2 against the authoritative
   `input_schema` from `tool_defs` — repaired-then-invalid ⇒ error result to
   the model, handler never invoked. Salvage tier (Colibri's `COLI_TOOL_SALVAGE`
   analog — lone payload mapped onto the single required param) exists behind
   `OLYMPUS_TOOL_SALVAGE`, default **off**. Telemetry:
   `record_repair(provider, model, tool, rung, outcome)` → aggregated counts in
   `MEMORY_DIR/repair_stats.json` (proclock, tmp+replace, day-bucketed, schema
   `{"v":1,"days":{date:{"<provider>/<model>/<tool>":{rung:n,...}}}}`);
   rising repair-rate = decay signal, surfaced via doctor WARN when the 7-day
   rung≥3 share exceeds `OLYMPUS_REPAIR_WARN_RATE` (default 0.15) with ≥ 50
   calls. Golden malformation corpus: `tests/fixtures/toolcall_corpus/*.json`
   (each: raw payload, tool schema, expected rung, expected args or refusal) —
   every future real-world malformation gets a corpus entry (G5 discipline).
4. **Invariants.** I-T1: no unvalidated repaired call ever executes. I-T2:
   refusal-safety — unknown tool names never recovered (existing gate,
   regression-kept). I-T3: telemetry failure never affects the call (record is
   best-effort, exception-swallowed-and-captured). I-T4: rung numbering stable
   (telemetry comparability).
5. **Schema.** Corpus + stats files above.
6. **Interfaces.** New pure functions in `toolcall_repair.py`
   (`validate_arguments`, `coerce_arguments`, `close_truncated`,
   `record_repair`, `repair_rate(days)`); `openai_compat.run_agent` wires
   validation before every `handler(**args)` (both native and recovered calls).
7. **Ownership.** Existing module (admission test correctly fails for a new
   one).
8. **Threats.** Tool-call injection via crafted malformed payloads → rung 2 is
   a *narrowing* step (validation), rung 4 never invents structure, rung 6 gate
   absolute; salvage off by default; the approval spine (`actions.prepare`) and
   `security.filter_tools` remain downstream unchanged — repair runs strictly
   before, never around, policy.
9. **Failure.** Validation failure ⇒ typed error string to the model (existing
   error-feedback pattern), counted; corrupt stats file ⇒ quarantine-and-restart
   file (counts are ephemeral telemetry — sanitize-and-continue class).
10. **Migration.** None; behaviour change is confined to: (a) previously-
    executed calls with schema-invalid args now bounce back as errors (strictly
    safer; noted in notes), (b) new coercions only on schema-typed mismatches.
11. **Rollback.** `OLYMPUS_TOOL_VALIDATE=off` reverts to legacy pass-through
    (kept for one release); telemetry removable independently.
12. **Tests.** Corpus-driven (≥ 20 cases: valid, fenced, double-encoded,
    truncated-unambiguous, truncated-ambiguous(refuse), digit-string-in-string-
    param (stays string), int-coercion, missing-required (refuse), unknown-name
    (refuse), salvage on/off, nested shapes A/B/C); execution-precondition test
    (invalid repaired call never reaches handler — spy handler); telemetry
    aggregation + warn threshold; stats corruption handling; rate math.
13. **Benchmarks.** Ladder overhead per call < 0.5 ms (pure-python, measured in
    test).
14. **Acceptance.** I-T1 spy test green; corpus green; doctor decay warning
    fires on synthetic rising-rate data.
15. **Exclusions.** Anthropic-path repair (SDK delivers typed dicts — nothing to
    repair); streaming partial-call repair; auto-retry policy changes.
16. **Files.** Modified: `olympus/toolcall_repair.py`, `olympus/openai_compat.py`,
    `olympus/doctor.py`, `.env.example`; new corpus + tests.
17. **PR unit:** PR 8 (second half).

---

## 2. Wave-1 threat model additions

(The CI threat-model gate covers `tools.HANDLERS`; Wave 1 adds no tools. These
additions extend `docs/THREAT_MODEL.md`'s narrative scope and are tested where
marked.)

| Threat | Surface | Mitigation | Test |
|---|---|---|---|
| Session-journal tampering | sessionlog files | hash-linked seals; reject-never-repair; quarantine | ✓ faults suite |
| Rollback (journal suffix truncation) | sessionlog | detectable within-file (chain); cross-restart anchoring = documented residual (Wave 2 witness-signing) | ✓ documented |
| Replay-fixture poisoning | import_fixture | manifest sha verification; force guard | ✓ |
| Secrets in fixtures/telemetry | export_fixture, usage, modelgate | secret screen + scrub report; hashes not text in telemetry | ✓ planted-secret test |
| Cross-user/session leakage | sessionlog, calibration, repair stats | safe_id pathing; shared files carry no content, only counts/ratios | ✓ path tests |
| Cache/evidence poisoning | ctx_calibration, modelgate results | bounds validation; append-only keyed rows; corrupt ⇒ quarantine | ✓ |
| Provider compromise / silent swap | modelgate | drift corpus + severity ladder + freeze markers | ✓ fixture drift |
| Malicious observe-plugin | connectors.emit | deep-copy observe events | ✓ hostile plugin |
| Tool-call injection via malformation | toolcall_repair | validation precondition; no structure invention; salvage default-off; spine unchanged | ✓ corpus |
| Denial-of-wallet | modelgate, ctxbudget | hard per-run dollar cap; refusal costs nothing; shared `usage.check_budget` | ✓ budget test |
| Verifier (Aletheia) compromise | out of Wave-1 scope | documented: verify decisions are now replay-diffable (C2/C3 make verifier drift *observable*); independence rules land with Wave-2 coalesce | documented |

## 3. Pass/fail acceptance matrix

| # | Gate | Threshold | Evidence |
|---|---|---|---|
| A1 | Existing suite | 3649+ pass, 0 new failures | pytest run per PR unit |
| A2 | Journal faults | 12/12 fault classes handled per C1.12 | test_sessionlog_faults |
| A3 | Zero corrupted-record acceptance | 0 accepted in tamper matrix | same |
| A4 | Fixture replay determinism | diff_decisions == [] ∧ reply-hash equal | test + CI gate |
| A5 | Non-interference | 0 decision diffs, instrumentation on vs off | scripts/noninterference_gate.py in CI |
| A6 | No silent truncation (flag on) | every drop path emits event/refusal | ctxbudget tests |
| A7 | Estimation tolerance | calibrated |err| ≤ 15% on corpus; declared per pair | benchmark in tests |
| A8 | Cache telemetry liveness | active/inert/no-signal verdicts correct on synthetic days | usage tests |
| A9 | Drift detection | controlled fixture ⇒ correct severity incl. freeze | modelgate tests |
| A10 | Measurement budget | drift run stops ≤ cap; cadence default off | budget test + config default |
| A11 | No network speculation | no prefetch consumer exists; coupling module write-free | absence + fs-audit tests |
| A12 | No cross-user leakage | path-isolation tests green | sessionlog/usage tests |
| A13 | Config documented | every new OLYMPUS_* in .env.example + a test touching it | grep test |
| A14 | Fail-safe defaults | OLYMPUS_CTX_BUDGET, OLYMPUS_TOOL_SALVAGE, DRIFT cadence, SESSION_FSYNC=always all default off/safe | config tests |

## 4. PR decomposition & sequencing

Implemented as dependency-ordered commits on `claude/colibri-deep-analysis-gpit35`
(session constraint: single push branch — each commit is a self-contained
reviewable unit with tests, migration/rollback notes in the message; splitting
into literal PRs is mechanical afterwards).

| Unit | Content | Depends on |
|---|---|---|
| PR 1 | this spec (no behaviour change) | — |
| PR 2 | sessionlog + faults suite + memory hooks | — |
| PR 3 | replay fixtures (replaystore + CLI flags + committed fixture) | — |
| PR 4 | connectors copy fix + noninterference gate + CI job | PR 3 |
| PR 5 | ctxbudget + calibration + flag-gated wiring | — |
| PR 6 | usage cache telemetry + doctor liveness | — |
| PR 7 | modelgate + drift corpus + severity/budget + heartbeat (off) | PR 6 (cost math) |
| PR 8 | coupling predictability + toolcall ladder + corpus | — |
| final | completion report + acceptance matrix evidence + .env.example sweep | all |

## 5. Explicit global exclusions (Wave 1)

No Wave-2/3 work: no `ctxheat`, `routesub`, `draftverify`, `ingestgate`,
`watchdog`, `experiments`, `localtier`, `coalesce`/mirror, Anthropic-compatible
serving, prefetch activation, learned/adaptive anything. No new CLI commands.
No pricing-table unification. No hypothesis dependency (property-style tests
written with seeded stdlib `random` parameter sweeps instead — hypothesis noted
as a candidate dev extra for Wave 2).
