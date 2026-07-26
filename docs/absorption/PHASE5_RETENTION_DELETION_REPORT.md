# Phase 5 — Retention and Deletion Report (Step 11)

**Status: MECHANISM COMPLETE. POLICY UNSET — deliberately.**
**Deployment remains BLOCKED for regulated or multi-user personal data.**
**Module:** `olympus/retention.py` · **CLI:** `olympus retention …` ·
**Evidence:** `tests/test_phase5_retention.py` (26)

---

## 1. The blocker, and why Phase 5 does not clear it

`PRIVACY_RETENTION_REVIEW.md` §5 recorded that conversation snapshots and their
sealed journals have no retention bound. `RETAIN_DAYS` governs traces, usage,
frozen payloads and the absorption evidence ledgers — never the conversations
themselves.

Step 11 says: *"Do not invent a legal retention period."* So Phase 5 ships the
**mechanism** and leaves the **policy** unset.

`OLYMPUS_CONVERSATION_RETAIN_DAYS` has **no default**, and unset is
deliberately distinct from `0`:

- a default would invent a legal position — jurisdiction, contract and user
  disclosures decide this, and Olympus knows none of them;
- `0` would silently start deleting user content.

Unset is a **reported** state. `olympus retention status` prints the block and
exits non-zero:

> `OLYMPUS_CONVERSATION_RETAIN_DAYS` is unset. Conversation snapshots and their
> sealed journals grow without bound, so this deployment has no retention
> position for user content. Set it to a number of days, or to 'forever' if
> indefinite retention is the deliberate, disclosed policy. Until then, do not
> process regulated or multi-user personal data on this instance.

`forever` is accepted and **clears the block** — indefinite retention on record
beats indefinite retention by omission.

## 2. Mechanism, against Step 11's list

| Required | Status |
|---|---|
| global default retention | ✅ `OLYMPUS_CONVERSATION_RETAIN_DAYS` |
| per-principal override | ⚠️ **partial** — legal hold is per-principal; a per-principal *period* is not implemented (§6) |
| legal-hold exclusion | ✅ `OLYMPUS_LEGAL_HOLD`; outranks both sweep and explicit deletion |
| user/session deletion | ✅ `delete_principal()` |
| derived-memory deletion | ✅ notes, prefs, documents, docrag, ctxheat ledger |
| replay deletion | ✅ journal + quarantined copies |
| evidence-store deletion | ✅ via `RETAIN_DAYS` (`memory.sweep_evidence`) |
| backup-expiry documentation | ✅ §5 |
| tombstone propagation | ✅ tombstone written **before** the journal is unlinked |
| compaction | ✅ pre-existing `sessionlog.compact()` |
| deletion verification | ✅ `verify_deleted()` re-reads the filesystem |
| dry-run retention report | ✅ `retention.report()` / `olympus retention report` |

## 3. The properties that matter, and how they are proved

**Dry run is the default on every destructive call.** A destructive default is
one mis-click from an incident. Asserted from the function signatures.

**The dry run is accurate (P5-A11)** — stated as *the plan and the outcome
agree exactly*: the set of paths a dry run predicts is compared against the set
a real deletion removes. A dry run that merely "exists" is decoration.

**Deletion is complete (P5-A12).** Removing the snapshot while leaving the
sealed journal is not deletion — the journal replays the whole conversation
back. The test deletes a principal and then asserts `recover_history()` returns
nothing.

**Deletion is verified, not assumed.** `verified` is computed from the
filesystem after the fact.

**The most important negative:** with no policy set, `sweep_conversations(dry_run=False)`
deletes **nothing**. No policy must never mean "delete".

**Legal hold outranks everything**, including an explicit deletion request.

**Every operation is audited** — append-only, corrupt lines skipped rather than
repaired.

## 4. Step 12 — the legacy `api-v1` namespace (P5-A13)

Before Phase-4 B-F2, every `/v1` key ran as the single principal `api-v1`.
`users/api-v1/` is therefore the **commingled** memory of every key holder the
deployment ever had, and cannot be attributed.

| Operation | Disposition |
|---|---|
| inspect | safe, read-only |
| export | safe, non-destructive |
| **quarantine** | **recommended default** — reversible, removes the exposure immediately, keeps the data for a considered decision |
| delete | safe, verified |
| **adopt** | **never automatic** — requires a 27-word acknowledgement passed verbatim |

Adoption hands one key holder everyone else's material — the exact defect B-F2
closed. The acknowledgement exists so it cannot be given by accident or by a
script that was not written for this. P5-A13 is enforced structurally too: a
test scans every module and fails if any calls `adopt_legacy` or embeds the
acknowledgement string.

## 5. Backup expiry — the honest caveat

A deletion removes data from the live store. **A backup taken before the
deletion still contains it.** That is correct behaviour, not a bug, and it
means a deletion is only durable once every archive predating it has aged out.
Olympus does not manage archive lifetime on remote storage — `OLYMPUS_BACKUP_KEEP`
bounds local copies only. Operators must set an expiry policy on their storage
destination, and a right-to-be-forgotten response is not complete until it has
elapsed.

## 6. Open

| # | Gap | Status |
|---|---|---|
| R1 | **No retention policy is set** | blocks regulated / multi-user personal data. One variable clears it — but it is the operator's to set |
| R2 | per-principal retention *periods* not implemented | only a global period plus per-principal legal hold |
| R3 | backup expiry is documented, not enforced | §5 |
| R4 | no deletion **certificate** artifact | the audit log records it; a signed receipt would be stronger |
