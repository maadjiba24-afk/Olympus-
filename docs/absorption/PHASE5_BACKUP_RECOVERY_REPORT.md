# Phase 5 — Backup, Restore and Recovery Report (Steps 13–14)

**Status: EXECUTED.** 18 drills, all passing.
**Evidence:** `tests/test_phase5_recovery.py`

---

## 1. The rule this report is written against

> **A backup that has not been restored is not a verified backup.**

Every assertion below is made against data read back out of a real archive into
a clean directory. Nothing is asserted from the archive's own manifest, which
would be the artifact grading itself.

## 2. Backup and restore drill (P5-A10)

| Drill | Result |
|---|---|
| backup creation | ✅ real archive, files and bytes non-zero |
| integrity checking | ✅ verifies |
| **tampered archive refused** | ✅ one flipped byte mid-archive → restore raises |
| **restore into a clean tree** | ✅ into an empty directory, then read back |
| session-journal verification after restore | ✅ chain verifies (`status=ok`), history replays identically |
| snapshot verification after restore | ✅ byte-identical histories |
| per-user note verification after restore | ✅ content recovered |
| **principal isolation after restore** | ✅ alice cannot see bob's material, and vice versa |
| deletion/tombstone state after restore | ✅ a principal deleted before the backup stays deleted after the restore |
| configuration compatibility | ✅ `/readyz` returns ready against the restored tree |
| application startup after restore | ✅ readiness probe passes (see limits, §5) |
| non-empty target refused without `force` | ✅ pre-existing file untouched |

## 3. Restart, failure and recovery (P5-A4)

| Fault | Behaviour | Verdict |
|---|---|---|
| normal restart | caches dropped, stores re-read from disk, history intact | ✅ |
| torn journal tail (half-written final record) | truncated to the verified prefix; history intact | ✅ |
| corrupt middle record | **quarantined**; read stops at the boundary; damaged file preserved by copy | ✅ reject-never-repair |
| corrupt snapshot | falls back to the journal — the original Wave-1 C1 motivation | ✅ |
| interrupted retention sweep | failure reported, neighbouring principal untouched and readable | ✅ |
| read-only / full filesystem | append reports failure (`seq=0`) and captures to the errors ledger | ✅ never a silent drop |
| accounting failure under disk fault | captured, never escalated into a provider retry (Phase-4 Stage-C D1) | ✅ |

## 4. Concurrency (P5-A14, P5-A15)

| Drill | Result |
|---|---|
| 3 principals writing concurrently | no cross-principal leakage; each sees only its own material |
| 4 threads appending to one journal | seqs dense `1..N`, chain verifies — no fork |
| 6 threads × 15 accounting records | **90/90 increments recorded**, no loss |
| spend cap under retry | refuses; never degrades to a cheaper model |

The accounting drill is the sharp one: a lost increment means the daily cap
silently under-counts, and a budget that under-counts is not a budget.

## 5. Exact limits

- **Restart is simulated at the data layer**, not by stopping a container:
  caches are cleared and stores re-read, which is what a new process does. No
  process was killed and no container was restarted (no Docker daemon).
- **"Application startup after restore"** is proved by the readiness probe
  returning ready against the restored tree — an honest proxy, not a full boot.
- **Disk-full and read-only are simulated by injection**, not by filling a real
  disk or remounting a filesystem.
- **No off-host backup delivery was exercised.** `OLYMPUS_BACKUP_CMD` is
  refused in staging by design; delivery to real object storage is untested.
- **Backup expiry is documented, not enforced.** A backup taken *before* a
  deletion legitimately still contains that principal's data. Operators must
  age out archives to make a deletion durable — stated in
  `PHASE5_RETENTION_DELETION_REPORT.md` §5.
