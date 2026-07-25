# The Calibration Record — schema, grounding, and privacy note

**Status:** falsifiable prototype, **Phase 2** (evidence quality + production-safe
collection). **Observation-only.** Collection is **off by default** and changes
no Olympus behaviour when enabled.

> **Phase 2 additions** (this document updated): a controlled, versioned domain
> **taxonomy** (deterministic from the dispatched specialist — never from prompt
> text); a four-level **evidence hierarchy** (completion / implicit / explicit /
> verified) that analytics never collapses into one "success"; automatic
> **feedback linkage** (👍/👎 → explicit-level evidence appended, never rewriting
> the run); **multi-process safety** via the repo's `proclock` (bounded timeout,
> visible-on-drop); a **categorizing verifier** (valid · unsigned-valid ·
> corrupted-chain · incomplete-trailing-write · unsupported-schema ·
> missing-referenced-evidence); corrected **analytics** (completion/approval/
> rejection/edit/retry/verified rates all separate) with stronger ranking
> refusals; and a **trial-mode** `olympus calibration health/status/report/export`
> inspection command. See `docs/CALIBRATION_REPORT.md` for the Phase 2 verdict.

**Hypothesis under test** (from `docs/MOAT_ANALYSIS.md`): a provider-neutral,
customer-side record of measured model reliability is the only asset in the
Olympus design that compounds — because *time cannot be backfilled*, labs
*won't* build provider-neutral comparison, and outcome data is *structurally
invisible* behind an inference endpoint. This prototype exists to make that
hypothesis **falsifiable**, not to assume it.

---

## 1. Grounding — exact APIs and storage this extends

Read in full before implementation (code and tests, **not** docstrings — per the
`NORTH_STAR_REVIEW.md` F1 finding, where docstring-skimming inverted two
modules' meaning).

| Module | Verified API used | Verified storage |
|---|---|---|
| `witness` | `canonical_json(obj)->bytes`, `sub_public_key_hex(label)->str`, `sign_with(label, bytes)->str`, `verify_signature(pub, data, sig)->bool`, `available()->bool`, `WitnessError` | Ed25519 root of trust |
| `ledger` | Pattern reused, not called: `SCHEMA`/`LABEL` constants, `_content_hash = sha256(canonical_json(core))`, `parent` = prior node hash, **unsigned degradation** on `WitnessError` | `MEMORY_DIR/ledger/<run_id>.jsonl` |
| `attest` | Pattern reused, not called: append-only JSONL, tolerant reader skipping malformed lines, receipt header/footer export | `MEMORY_DIR/attestations.jsonl` |
| `outcomes` | `APPROVED`, `APPROVED_AFTER_EDIT`, `REJECTED`, `UNDONE`; `record(user, ref, outcome, kind)`; `_MIN_SAMPLES = 5` precedent | `store.backend()` ns `outcomes` |
| `compare` | `model_label(s)->"provider/model"`, `run()->{"id": cid,…}`, `reveal(user,cid,choice)->{"chosen_model","mapping",…}`, `tally(user)` | `MEMORY_DIR/users/<id>/compares/*.json` |
| `usage` | `estimate_cost(model, in_tokens, out_tokens)->float` | session totals |
| `config` | `MEMORY_DIR`; `Settings.provider/.model/.base_url` | — |
| `liveeval` | Env-flag pattern `os.environ.get("OLYMPUS_…","").strip().lower() in (…)`; `_MIN`-style sample bounds | `MEMORY_DIR/traces/` |

**New storage (one file):** `MEMORY_DIR/calibration.jsonl` — append-only,
hash-chained JSONL.

### Architectural decisions NOT reversed (F1 rule)

- **`outcomes.py`** states outcomes are *"suggested to the user, not imposed: no
  dark patterns, no manipulation, no hidden self-modification."* **Upheld** — this
  module writes no decision, and nothing reads it to alter behaviour.
- **`rlscaffold.py`** states it has *"NO path that writes to routing, config,
  prompts, or any decision."* **Upheld** — calibration is not wired to routing,
  `trust`, prompts, or permissions. Requirement 10 is enforced by a test.

### One decision flagged for review

`attest.attest()` **fails closed without crypto** (*"we must not mint an unsigned
proof"*). This prototype deliberately follows **`ledger`'s** degradation instead:
record the entry **unsigned** and have verification report it as unverified.

**Why the divergence is correct here, not a weakening:** an attestation is a
*proof about a human act* — an unsigned one is worthless and must not exist. A
calibration entry is *observational telemetry*; silently dropping observations on
a crypto-less host would **bias the dataset** — the one failure a calibration
record cannot tolerate. Entries remain hash-chained (tamper-evident) with or
without crypto; `verify()` reports `signed`/`unsigned` counts honestly. This is
the same trade `ledger` already made for resumability.

---

## 2. Schema — `olympus-calibration/1`

Append-only JSONL. Every entry shares an envelope; `body` varies by `kind`.

```jsonc
{
  "schema": "olympus-calibration/1",
  "seq": 0,                       // monotonic, 0-based
  "prev": null,                   // previous entry_hash — the chain
  "at": "2026-07-25T18:04:00+00:00",
  "kind": "observation",          // observation | feedback | comparison | tombstone
  "event_key": "sha256:…",        // idempotency key; duplicates are no-ops
  "body": { … },                  // see below
  "entry_hash": "sha256:…",       // sha256(canonical_json(core)) — core excludes signature
  "publicKey": "",                // "" when crypto unavailable (honest degradation)
  "signature": ""
}
```

**`kind: "observation"`** — one governed model call/run. Every field optional
except `run_id`; missing evidence is simply absent (never a corrupt record).

| Field | Meaning |
|---|---|
| `run_id` | Olympus run identifier — the join key |
| `domain` | controlled taxonomy value (Phase 2) — deterministic from the specialist |
| `domain_source` | `explicit` \| `specialist` \| `tool` \| `none` — provenance of the label |
| `domain_confidence` | 1.0 for explicit/specialist, 0.9 tool, 0.0 unclassified |
| `taxonomy_version` | the taxonomy version this row was classified under |
| `provider`, `model` | **kept explicit and separate — never collapsed into one score** |
| `config_id` | `sha256(provider, model, base_url, effort)[:16]` — model configuration identity |
| `specialist`, `tool` | which council member / tool was involved |
| `latency_ms`, `tokens_in`, `tokens_out`, `cost_usd` | performance + `usage.estimate_cost` |
| `result` | `ok` \| `error` \| `refused` \| `timeout` |
| `evidence_level` | `1` (completion) — the observation's own evidence tier |
| `task_hash` | `sha256(task)[:16]` — **a reference, never the text** |
| `provenance` | `{trace_id, compare_id}` references |

**Domain taxonomy (controlled + versioned, Phase 2).** A small explicit set —
`code`, `research`, `finance`, `marketing`, `social`, `scheduling`, `inbox`,
`security`, `coaching`, `evolution`, `learning`, `general`, plus `unclassified`
and `other`. It mirrors `routing_outcomes._TASK_TYPE` (the pipeline's own routing
tag reflecting real Olympus workloads), so classification is **deterministic from
structured metadata** — the dispatched specialist, then the tool — and **never
reads prompt text**, so no sensitive attribute is inferred. An explicit caller
override is honoured but still constrained to the taxonomy (an unknown label
becomes `other`, never invented). `DOMAIN_TAXONOMY_VERSION` is recorded on every
classified row; bump it on any change to the sets.

**`kind: "feedback"`** — appended *later*; **never rewrites the observation**, and
a run may carry **many** feedback events. `{ref_run_id, outcome, evidence_level,
evidence, note_hash}` where `outcome` ∈ `approved`, `approved_after_edit`,
`edited`, `rejected`, `undone`, `retried`, `overridden`, `abandoned`,
`preference`, `verified`. Each carries its **evidence level** (see §3), so an
implicit `edited` is never confused with an explicit `approved`, and neither is
confused with a `verified` downstream outcome. An edit is **not** a completion
failure; an approval is **not** proof of correctness.

**`kind: "comparison"`** — `{compare_id, chosen_model, models[], run_ids[], blind}`.
Links a blind comparison to its runs.

**`kind: "tombstone"`** — `{ref_event_key, reason}`. Deletion **never rewrites
history**: readers treat the referenced entry as redacted while the chain stays
intact and verifiable.

### Schema upgrades
Readers accept any `olympus-calibration/N`, skip entries whose `schema` is
unknown-major, and never fail on unknown fields. Historical entries remain
readable; `migrate_entry()` upgrades in-memory only, never in place.

---

## 3. The evidence hierarchy (four levels, never collapsed)

Feedback carries an **evidence level** — a different QUESTION per level, not
points on one axis. Analytics reports each separately; there is deliberately no
single "success" number.

| Level | Question | Outcomes | Metric |
|---|---|---|---|
| **1 completion** | did it run? | observation `result` | `completion_rate` |
| **2 implicit** | behavioural signal | `edited`, `retried`, `undone`, `abandoned`, `overridden` | `edit_rate`, `retry_rate` |
| **3 explicit** | did the user accept it? | `approved`, `rejected`, `preference` | `approval_rate`, `rejection_rate` |
| **4 verified** | was it actually correct? | `verified` | `verified_outcome_rate` |

**Completion is not quality; approval is not correctness.** A run can complete
(level 1) and be rejected (level 3); a run can be approved (level 3) yet fail an
external check (level 4). Blending them is the specific error Phase 2 exists to
prevent — `report()` keeps them in separate fields and `rank_models()` ranks
**only** completion, refusing the others as "a separate decision."

### The data layers (still distinct)

| Layer | What it is | Where |
|---|---|---|
| **Raw observations** | what happened: latency, tokens, cost, result | `kind:"observation"` |
| **Verified outcomes** | what a human judged / an external check confirmed | `kind:"feedback"`, `kind:"comparison"` |
| **Inferred metrics** | computed, never stored: the rates above, Wilson intervals | `report()` output |
| **Future decision policies** | routing, trust, autonomy — **NOT IN THIS PROTOTYPE** | *(none — deliberately)* |

Layer 4 must never silently consume layers 1–3. Tests pin that no decision module
references `calibration`, and that the orchestrator (the one integration point)
only **writes**, never reads it back.

---

## 4. Minimum-sample policy

`_MIN_SAMPLES = 5` per `(provider, model, domain)` cell, mirroring
`outcomes._MIN_SAMPLES`. Below it, `report()` emits
`"insufficient_evidence": true` and **omits the rate entirely** rather than
publishing a number. `rank_models()` refuses to return an ordering when any
candidate is under-sampled — it returns the refusal reason instead. Uncertainty
is a **Wilson score interval** (deterministic, no numpy/scipy — preserves the
three-dependency footprint).

---

## 5. Privacy & threat model

**Recorded by default:** run/trace identifiers, timestamps, domain label,
provider + model + config id, specialist/tool name, latency, token counts,
estimated cost, result status, outcome verdicts, comparison picks, content
*hashes*.

**Never recorded by default:** prompt text, response text, tool arguments, tool
output, file contents, URLs, user notes, credentials. There is **no mode in v1
that stores raw text** — the safest default is no switch at all.

| Control | Behaviour |
|---|---|
| **Collection** | `OLYMPUS_CALIBRATION` — **off by default**; disabled ⇒ zero writes, zero behaviour change |
| **Retention** | `OLYMPUS_CALIBRATION_RETENTION_DAYS` (0 = keep) — `prune()` tombstones entries older than the window |
| **Deletion** | Tombstone-by-append; the chain is never rewritten, so tamper-evidence survives deletion |
| **Export** | `export_jsonl()` is explicit and operator-invoked; never automatic, never networked |

**Residual risks (stated, not solved):**

- **Traffic analysis / business inference.** Even without text, *volume, timing,
  domain labels, and cost* can reveal business activity — a spike in `legal` at
  02:00 is a signal. Mitigation: domain labels are coarse and operator-defined;
  the file inherits `MEMORY_DIR` permissions. **Not eliminated.**
- **Task-hash correlation.** `task_hash` is a hash of *plaintext*; an attacker
  with a candidate task list can confirm-by-hashing. It is a correlation key, not
  a secret. Use only for dedupe/joins.
- **Provider identifiers** are recorded deliberately (the whole point is
  provider-neutral comparison) and reveal which vendors a customer uses.
- **Consent.** Outcome data is *user behaviour* (what they rejected/edited).
  Multi-user deployments must disclose collection before enabling it. Off-by-
  default exists so enabling is a deliberate, documentable act.
- **Export leakage.** An export is a portable behavioural dataset. Treat it as
  confidential; the format is documented so it can be reviewed before sharing.

---

## 6. Multi-process safety, recovery, and filesystem assumptions (Phase 2)

### Concurrency
Appends run under **`proclock.lock("calibration", timeout=…)`** — the repo's
existing `fcntl.flock` cross-process lock (ADR 0005), not a second abstraction.
Two Olympus processes sharing `MEMORY_DIR` (the heartbeat-vs-web topology)
serialize on it, so they cannot interleave lines or fork the hash chain. The lock
is **bounded** (`OLYMPUS_CALIBRATION_LOCK_TIMEOUT`, default 5 s): on a wedged peer
the append is **dropped with a visible `errors.capture`**, never a silent success
and never an unbounded hang on a real run. Each write is a single
`write()`+`flush()`+`fsync()` of one newline-terminated line.

### Recovery — what `verify()` distinguishes (never silently repairs)
- **valid** — signed and chained.
- **unsigned_valid** — structurally valid, no crypto signature (crypto-less host).
- **incomplete_trailing_write** — the LAST physical line is truncated (a crash
  mid-`write`). Recoverable: the good prefix reads normally; the partial tail is
  ignored by readers. Reported, not fatal.
- **corrupted_chain** — a MIDDLE entry was altered, removed, or won't parse. The
  hash chain breaks at that point. **Never auto-healed** — flagged for an
  operator, because silently rebuilding a middle would erase the evidence of
  tampering that the chain exists to preserve.
- **unsupported_schema** — an entry with a newer major than this build can read.
- **missing_referenced_evidence** — feedback/comparison referencing a `run_id`
  with no observation (e.g. out-of-order arrival). Reported; resolves when the
  observation lands.

### Filesystem assumptions and known limits
- **Local POSIX (ext4/xfs/apfs):** `flock` is honoured and a single small
  `write()` is effectively atomic — the design target.
- **NFS / network filesystems:** `flock` semantics are weaker or advisory-only,
  and large writes can tear. Multi-host deployments should use the Postgres store
  backend and treat the JSONL as single-host. This limit is inherited from
  `proclock` (ADR 0005), not new here.
- **Windows (no `fcntl`):** `proclock` degrades to single-process locking with a
  one-time warning — the heartbeat-vs-web split is documented unsupported there.
