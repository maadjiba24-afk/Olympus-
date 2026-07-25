# Calibration Record — implementation report

**Prototype status:** complete, observation-only, merged behind an off-by-default
flag. **Recommendation: CONTINUE, with conditions** (§6).

---

## 1. What was built

| Deliverable | Where |
|---|---|
| Implementation | `olympus/calibration.py` (1 new module) |
| Integration point | `olympus/orchestrator.py` `_finish()` — 1 call site, write-only |
| Tests | `tests/test_calibration.py` — 46 tests, deterministic, no paid API |
| Schema + privacy note | `docs/CALIBRATION_RECORD.md` |
| Example export | `docs/examples/calibration-export.jsonl` |
| This report | `docs/CALIBRATION_REPORT.md` |

**Files added: 4** (one module, one test file, two docs) plus one example export
and a 6-line call site. **Dependencies added: 0** — `test_deps_claim` still
passes; Wilson intervals are stdlib `math`, no numpy/scipy.

## 2. Success criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | A real run produces an entry | ✅ | `test_real_run_produces_a_calibration_entry` drives the real orchestrator (stubbed client) and asserts one observation joined to `bot.last_run_id` |
| 2 | Approval/edit/rejection/retry updates evidence without rewriting history | ✅ | `test_feedback_does_not_rewrite_the_observation` — file is strictly appended (`after.startswith(before)`), original `entry_hash` unchanged |
| 3 | Blind comparison links to its runs | ✅ | `test_blind_comparison_links_to_runs` — `compare_id` + `run_ids` |
| 4 | Records survive restart | ✅ | `test_records_survive_restart` reloads the module against the same store |
| 5 | Records verifiable and exportable | ✅ | `verify()`, `export_jsonl()`/`import_jsonl()` round-trip; tampered export detected |
| 6 | Refuses to rank on insufficient evidence | ✅ | Below `_MIN_SAMPLES=5` a cell reports `insufficient_evidence` and **omits** rates; `rank_models()` refuses, and reports CI overlap rather than false precision |
| 7 | Existing tests green | ✅ | **3604 passed, 26 skipped, 0 failed** |
| 8 | No automatic behaviour change | ✅ | Two pinned tests: no decision module references the record; the orchestrator may only write, never read |
| 9 | Minimum files/dependencies | ✅ | 4 files, 0 dependencies |
| 10 | Layers documented distinctly | ✅ | `CALIBRATION_RECORD.md` §3: raw observations / verified outcomes / inferred metrics / future decision policies (deliberately empty) |

## 3. Benchmark (recording overhead)

Measured, n=500 entries, this container:

| Operation | Cost |
|---|---|
| `record_observation` (enabled) | **2.20 ms/entry**, 891 bytes/entry |
| `record_observation` (**disabled**) | **0.0148 ms/call** — no I/O, no allocation of note |
| `verify()` full chain | 72 ms / 500 entries |
| `report()` | 17 ms / 500 entries |
| File size | 435 KiB / 500 entries (~0.9 MiB per 1000 runs) |

**The 2.2 ms is dominated by an O(n) re-read per append** (`_head()` scans the
file to find the tail hash and the seen-event-key set). Total cost is therefore
**O(n²)** over the life of the file — acceptable for a prototype at thousands of
entries, unacceptable at 10⁵. See risk R1.

## 4. Verification of the required behaviours

1. **Append-only / tamper-evident** — content-addressed entries chained by `prev`;
   editing or deleting any entry breaks every later one. Verified without crypto.
2. **Reuses existing infrastructure** — `witness` for canonical JSON, subkey
   signing, and verification; mirrors `ledger`'s chaining and `attest`'s
   append-only JSONL. No parallel crypto stack.
3. **Idempotent** — derived `event_key`; duplicates are silent no-ops.
4. **Partial records safe** — sparse bodies; `0`/`False` preserved as evidence.
5. **Provider/model explicit** — `provider`, `model`, `model_key`, and `config_id`
   are separate fields; nothing is collapsed into a single score.
6. **Readable across upgrades** — any `olympus-calibration/N` is accepted;
   `migrate_entry()` upgrades in memory only, so on-disk hashes stay valid.
7. **Documented machine-readable export** — JSONL with a header line; re-import
   re-verifies hashes independently of the exporter.
8. **Configurable** — `OLYMPUS_CALIBRATION` (off by default),
   `OLYMPUS_CALIBRATION_RETENTION_DAYS`.
9. **Unchanged when disabled** — a full orchestrator run works and writes nothing.
10. **No routing/autonomy changes** — enforced by test, not by promise.

## 5. Unresolved risks

**R1 — O(n²) append cost (engineering, known, bounded for now).** Every append
re-reads the file. At ~10³ entries this is invisible; at ~10⁵ it is not. Fix
when it matters: keep the tail hash and event-key set in memory (or a sidecar
index) and rotate the file by period. Not fixed now because premature indexing
would add a second source of truth to a store whose entire value is integrity.

**R2 — Multi-process interleaving (correctness, unmitigated).** Writes are guarded
by a `threading.Lock` only. Olympus has a documented heartbeat-vs-web
multi-process topology, and `proclock` exists but is **not** wired here. Two
processes appending concurrently could interleave lines and break the chain —
`verify()` would *detect* it, but the record would need repair. Wire `proclock`
before enabling on a multi-process deployment.

**R3 — The record is only as good as its labels.** `domain` is currently only ever
set by direct API callers; the orchestrator integration does not classify a task's
domain, so production entries will land in `unclassified`. Per-domain analysis —
the point of the exercise — needs a domain classifier or an explicit caller.
**This is the biggest gap between the prototype and the hypothesis it tests.**

**R4 — Single-model observations don't measure comparative reliability.** The
orchestrator records the *reasoning* pool member. Real cross-provider comparison
needs either blind `compare` runs or the same task genuinely routed to different
providers over time. Until then the record measures *this* configuration's
reliability, not *which model is better*.

**R5 — `result` is optimistic.** The orchestrator records `result="ok"` whenever a
run completed. It does not yet distinguish a run that completed but produced a
*bad* answer — that signal only arrives via `record_feedback`, which nothing calls
automatically. **Success rate is therefore currently "completion rate," not
"quality rate,"** and must not be read as the latter.

**R6 — Privacy residuals** (detailed in `CALIBRATION_RECORD.md` §5): traffic
analysis over volume/timing/cost can infer business activity even without text;
`task_hash` is confirmable against a candidate plaintext; provider identifiers are
recorded by design; multi-user deployments have consent obligations.

**R7 — Hypothesis risk.** The moat argument assumes accumulated calibration data
becomes decision-useful. If R3–R5 are not closed, the data accumulates *volume*
without accumulating *signal* — the failure mode that looks like progress.

## 6. Recommendation: **CONTINUE, with conditions**

The prototype does what it was asked to: it is falsifiable, cheap, honest about
uncertainty, and changes nothing. Recording costs 2.2 ms and is off by default.

**Continue, because** the moat analysis's core claim — *time cannot be
backfilled* — makes start date the dominant variable, and this now makes starting
possible at near-zero risk. It refuses to manufacture rankings, which is the
specific failure that would have made the data worse than useless.

**Conditions — the hypothesis is only tested if these are met.** Judge at
**~500 real runs** against a pre-committed bar:

1. **Close R3 (domain labelling)** — without it there is no per-domain analysis
   and the exercise measures one undifferentiated blob.
2. **Close R5 (real outcome signal)** — wire `record_feedback` to actual approve/
   edit/reject events. Completion rate is not quality.
3. **Pre-committed kill criterion:** if, at ~500 runs with domains and feedback
   flowing, `rank_models()` still cannot separate any two configurations
   (overlapping intervals everywhere), then the customer-side signal is too weak
   at single-operator volume — **kill the hypothesis** and fall back to the
   provider-neutral *audit* value alone, which does not depend on statistical
   separation.

**Do not proceed** to automatic routing, trust expansion, or self-improvement on
this data. That is a separate decision, and on current evidence (R4, R5) the data
does not yet support it. The line this prototype draws — observation first,
automation later — is the thing that makes the experiment safe to run at all.
