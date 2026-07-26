# Phase 4 · Stage E — Privacy & Retention Review

**Date:** 2026-07-26 · **Tree:** `claude/colibri-deep-analysis-gpit35`
**Rule under test:** *"Raw logs must not automatically become permanent moat
data."*

---

## 1. Finding (fixed in this phase)

**Retention covered `traces/` and `usage/` only.** Every store the absorption
programme added — five append-only evidence ledgers plus watchdog forensics —
grew **without bound**. Confirmed by reading `memory.sweep_dated_files`, whose
loop is literally `for sub in ("traces", "usage")`, and by grepping the sweep for
each new store (zero hits).

**Fixed** (`89c0118`): `memory.sweep_evidence(retain_days)` prunes
`modelgrade/evidence.jsonl`, `routesub/decisions.jsonl`,
`streamguard/pathologies.jsonl`, `ingest/refusals.jsonl`,
`experiments/state.jsonl` by record timestamp, and `watchdog/forensics/*.json`
by mtime — wired into the existing heartbeat maintenance job (no new cadence).
Two safety properties are asserted by test: an **unparseable row is kept** (so a
corrupt ledger still trips its owner's reject-never-repair path instead of being
silently rewritten by a retention pass), and a **row with no timestamp is kept**
(never delete data you cannot date).

---

## 2. Data inventory

| Store | Contents | Sensitivity | Retention | Deletion |
|---|---|---|---|---|
| `conversations/<id>.json` | full conversation text | **HIGH** | **none (pre-existing gap)** | manual / `import_memory` overwrite |
| `sessions/<id>.journal.jsonl` | same text, sealed + append-only | **HIGH** | shares the snapshot's lifetime | `sessionlog.delete_session`; tombstone + `compact` physically removes payload bytes |
| `traces/YYYYMMDD.jsonl` | decisions, rationales, meta | MEDIUM | `RETAIN_DAYS` (30) | age sweep |
| `usage/YYYY-MM-DD.json` | token/cost counters, prefix hashes | LOW | `RETAIN_DAYS` | age sweep |
| `responses/`, `tool_results/`, `context/` | frozen provider payloads | **HIGH** | orphan + age sweeps | age sweep |
| `modelgrade/evidence.jsonl` | outcome counters, keys | LOW | **`RETAIN_DAYS` (new)** | sweep |
| `routesub/decisions.jsonl` | route ids, costs | LOW | **new** | sweep |
| `ctxheat` ledgers | ids/counters only — content-minimised **by construction** (the API cannot accept content) | LOW | bounded by pin budget | per-user file |
| `watchdog/forensics/` | lease timelines, spend | LOW | **new** | sweep |
| `streamguard/pathologies.jsonl` | pathology kind + detail | MEDIUM | **new** | sweep |
| `ingest/refusals.jsonl` | artifact sha, reasons | LOW | **new** | sweep |
| `ctx_calibration.json`, `repair_stats.json` | ratios / counters | LOW | bounded by key set | delete file |
| `vault` | credentials, **encrypted** (Fernet) | **CRITICAL** | none (intentional) | explicit revoke |

## 3. The thirteen required determinations

1. **Collected data** — §2.
2. **Purpose** — each store maps to one capability's evidence need; none is
   collected "in case it's useful later".
3. **Retention** — `OLYMPUS_RETAIN_DAYS` (30) now governs traces, usage, frozen
   payloads **and** the absorption ledgers. **Conversations remain unbounded —
   open work (§5).**
4. **Deletion** — per-session via tombstone + verifiable compaction (physical
   byte removal proven by a raw-file test); per-store via sweep; `delete_session`
   removes journal, snapshot and quarantine copies.
5. **Encryption** — credentials only (`vault.py`, Fernet). Conversation and
   evidence stores are **plaintext on disk**, protected by filesystem
   permissions. Stated plainly, not implied otherwise.
6. **Access control** — process-local filesystem; per-user scoping via
   `memory.safe_id` (hostile-id containment tested); HTTP surfaces auth-gated
   with constant-time comparison.
7. **Export** — `memory.export_memory` (per-user or all-users), gated;
   `calibration.export_jsonl` behind `export_allowed()`.
8. **Audit** — decisions are signed (`witness.sign_log`); actions carry an
   audit trail; ingest refusals retain signed evidence.
9. **Redaction** — replay fixtures are secret-screened (v2, seven credential
   families) and **refuse to export** on a hit; telemetry stores hashes
   (`prefix_fp`), never prompt text.
10. **User consent** — inherited from the existing product surface; **not
    re-verified in this phase** (no user-facing consent flow was exercised).
11. **Provider data handling** — governed by the operator's provider contracts;
    sovereignty mode can enforce local-only routing. Olympus adds no third-party
    processor.
12. **Replay-fixture policy** — committed fixtures are synthetic and
    secret-screened; export refuses on any credential match; base64/split-field
    secrets remain a **documented residual**.
13. **Evidence-store policy** — append-only, bounds-validated, now age-pruned;
    content-minimised where it touches user data (`ctxheat` structurally).

## 4. Verdict

**PASS with one open item.** The rule "raw logs must not automatically become
permanent moat data" now holds for every store the absorption programme
introduced. It does **not** yet hold for conversation history, which predates
this programme.

## 5. Open work

1. **Conversation retention is unbounded** (pre-existing). `conversations/` and
   their journals have no age policy. Per-session deletion exists and works; a
   *global* policy (e.g. `OLYMPUS_CONVERSATION_RETAIN_DAYS`) does not. Recorded
   as a **blocker for any deployment handling personal data under a retention
   obligation**, and as non-blocking for a single-operator instance.
2. **At-rest encryption** covers credentials only; conversation text is
   plaintext on disk.
3. **Consent flow unverified** in this phase.
4. **Secret-screen residuals**: base64-encoded and split-across-field secrets.
