# M02: exact-owner memory and graph work in progress

Base: `62b7d6dd565671220e106418052c926eefff11e0` (completed PR #312).
Base tree: `18e9fd1e104cc107ad2a9256215af5bb19341777`.
This document is an implementation record, not a completed-delivery receipt.

## Finite acceptance

| Requirement | Implementation / remaining verification |
| --- | --- |
| Five owner collections | Events, memories and candidates share `usermem.state.v3`; nodes and edges share `relgraph.state.v3`. Exact-owner envelope validation and digest-derived keys use the established owner contract. Case, punctuation, Unicode and long-owner separation have owned tests. |
| Preserve ambiguous legacy state | Original five namespaces are untouched. An absent new snapshot with legacy bytes reports unavailable. Explicit `memory initialize-empty` starts a separate empty exact-owner snapshot only after acknowledgement; it never migrates, claims or repairs old data. Qualified migration/export/erasure remain M09 and are unresolved. |
| Evidence errors | JSON, owner/schema, row bounds and identifiers are validated before publication. Existing damage cannot be repaired by an ordinary write. CLI reports an error; HTTP reports 503 with `evidence_state: unavailable`. Episodic gathering propagates state errors. Extraction reports unavailable state before a model call. |
| Atomic memory updates | Candidate approval publishes candidate removal and memory addition together; conflict approval includes supersession. Failure before publication preserves the candidate. Unconfirmed post-publication failure requires rereading; retry does not append a duplicate. A candidate is retained if its accepted memory would immediately be pruned. |
| Atomic graph updates | Node creation with edge creation, and node deletion with edge removal, publish one snapshot. Bounds cannot publish a half-created relation. Multi-collection graph readers hold a consistent snapshot. |
| Backend concurrency | File snapshots retain the existing bounded state-directory lock and atomic/durable publication. New Postgres snapshot transactions use one connection and a transaction-scoped advisory lock, including when the row is absent. Owned transaction mocks cover rollback; a real owned Postgres run remains required. Other backend callers remain M12. |
| Caller coverage | CLI and web approval now call the atomic API. Recall uses atomic replacement. Background typed-memory enumeration reads validated envelope principals, not storage keys. Full background proposal/wiki ownership and snapshot/revert lifecycle are still M03; these must not be claimed complete from enumeration alone. |
| Platform behavior | Native Windows remains one process per state directory pending M13. Windows/POSIX full suites, real HTTP fixtures, final caller review remain required before delivery. |

## Operator inspection and explicit initialization

`olympus memory state-status --user EXACT_OWNER --state memory` reports the
new snapshot state and fingerprints of unclaimed legacy values. Use
`--state graph` for the relationship graph.

Only if the operator intends to start a NEW EMPTY exact-owner store while
retaining the old data unclaimed:

```
olympus memory initialize-empty --user EXACT_OWNER --state memory --acknowledge-unclaimed-legacy
```

The graph is a separate explicit operation using `--state graph`. Existing
exact-owner snapshots, including damaged snapshots, cannot be reset by this
command. Interrupted or uncertain publication requires inspecting state before
retrying; this is not a legacy migration, recovery certificate or erasure.

## Evidence so far

A network-denied cloud run passed 554 tests, with 1 existing skip and 7 named
HTTP fixtures deselected because IP sockets are denied. An earlier preserved
attempt showed the HTTP fixture setup errors explicitly; no unconfined fallback was
used. A new owned in-memory HTTP test exercises real request parsing,
authentication, approval, serialization and unavailable-evidence responses.
The original HTTP fixtures remain required in operator Windows/POSIX runs.

No M02 patch was applied to the operator repository; only its empty working
branch was established. No M02 commit, push, PR, merge, deployment, collection,
routing, autonomy, broker activation or live verifier fan-out occurred.

## Open dependencies, not accepted completion

- Recall policy inspection and mutation now share a transaction; embedding calls occur after commit. Background snapshot/revert lifecycle is still M03; do not mistake store-level atomic publication for an entire background workflow transaction.
- Obtain owned Postgres runtime capability, then validate real rollback,
  independent-writer serialization, absent-row contention and connection failure.
- Run guarded package selfchecks, Windows and POSIX full suites, then the 24
  exact CI checks and protected delivery/synchronization sequence.
- Continue M03-M19 and the real production, calibration, routing and comparative
  evidence gates. PR #312/M01 completion is not overall hardening completion.
