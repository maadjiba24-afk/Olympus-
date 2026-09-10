# Post-PR #309 finite closure register

Baseline verified through GitHub PR/commit reads and `git ls-remote`: merged PR
#309, main `a1502b1667b2e2fe732c24056d435811c53b09cd`, tree
`3a05003c5d4c0974c1b49b5f04e128ccd0b0e54c`. The implementation branch starts
at that actual upstream commit. The earlier source snapshot and unfinished
edits were preserved separately. Reverify remote state before delivery.

Nine items define this closure. This does not authorize reviving rejected
NORTH_STAR proposals or expanding deployment/autonomy. C1-C3 are one coherent
assessment-result evidence change. Their delivery gates are separate from
operational and real-data gates.

| ID | Scope | Disposition and remaining acceptance evidence |
| --- | --- | --- |
| C1 | Findings | Implemented: bounded strict records, missing/unavailable distinction, exact-owner transactions, visible report/export/import refusal, content-addressed preserve-before-reset repair; concurrency, corruption and publication-failure tests. Full local suite, 24 CI checks, guarded merge/sync remain open. |
| C2 | Learned assessment knowledge | Implemented: strict counters/provenance/limits, serialized update, replay/agent exclusions, sanitized unavailable prompt context, exact-owner Aegis lookup, surfaced self-assessment failures. Cross-file partial publication is an explicit bounded limitation below. Same delivery gates as C1. |
| C3 | Advisory cache | Implemented: bounded records, explicit fresh/live/stale/missing/unavailable semantics, optional/offline/replay behavior, no corruption overwrite, transaction merge, explicit repair. Incomplete/unpersisted coverage reaches tools and run/self-assessment output. Same delivery gates as C1. |
| C4 | Hardening closure audit | Historical E1-E31 reconciled in POST_PR309_AUDIT.md. Scope review is complete; repository-wide hardening is NOT closed. Named residual code prerequisites include normalized non-assessment stores, model-grade wiring, pricing consistency, Postgres verification and older error-visibility defects. No historical high/critical row is unclassified. |
| C5 | Windows concurrency | Existing documented limitation retained: one process per state directory on native Windows; shared proclock has thread-only fallback. Thread behavior tested; native Windows cross-process support is NOT claimed. The operational gate requires topology inspection before activation; automatic prevention of every independently launched Windows process is not implemented. Browser owner leases already have a separate native lock. |
| C6 | Production readiness | Runbook completed in POST_PR309_OPERATIONS.md. BLOCKED: intended host, mount/permissions, current lifecycle receipts, backup/restore evidence, monitoring destination and rollback drill not supplied. No deployment performed. |
| C7 | Calibration/evolution | BLOCKED: no current genuine deployment dataset, activation decision or 100/250/500 checkpoint reports. Two historical smoke records do not establish a current live count. No generated tasks or feedback were used. |
| C8 | Learned routing | BLOCKED: no current evidence meeting 300 labeled real outcomes, 3 task types, 2 real sources and 25 outcomes per eligible cell including incumbent; operator opt-in absent. Normalized source attribution is also disclosed in C4. This gate is distinct from C7. |
| C9 | Comparative capability | Source comparison and finite matched-task protocol are in POST_PR309_COMPARISON.md. BLOCKED for superiority/whole-goal closure: no matched measurements and no unambiguous source repositories for Manus/Odysseus. Source features and passing Olympus unit tests are not competitive scores. |

## Validation and delivery

- Original review assessment/adjacent/security evidence regression: **519 passed**
  on Python 3.12 with the repository's pinned runtime dependencies. It includes
  assessment authorization, results, SARIF, self-assessment, specialists,
  process races, threat-model coverage, deployment readiness, calibration
  reporting, learned routing, owner stores, preferences, companion and mandates.
- The new result-evidence suite uses mock/local data. Corruption matrices cover
  duplicate JSON keys, wrong types, non-finite counters, oversized input,
  nonregular files, owner collisions, learning exclusions and prompt consumers.
- Process tests prove POSIX serialized updates; simulated no-fcntl tests prove
  thread fallback only. Publication tests interrupt file fsync, replace and
  directory fsync, including interrupted quarantine/reset.
- A network-denied broad-suite attempt stopped at **277 passed, 20 setup
  errors**: local HTTP server fixtures cannot create IP sockets under the
  isolation policy. This is an **unsatisfied full-suite gate**, not a green
  run and not evidence those tests fail in a supported environment. No external
  metadata call was permitted. Do not repeat an unconfined cloud full suite.
- No commit, push, PR, CI, merge or local-main synchronization is asserted by
  this source document. The delivery package pins its canonical patch hash,
  expected baseline/tree and exact changed files. Run its guarded validation
  locally and return the actual results before advancing through remote gates.
- `scripts/ci-local.ps1` requires both native Windows and POSIX validation when
  locking/filesystem barriers change. Both supported local full-suite outputs
  remain required; the isolated cloud subset does not replace either full leg.
- PR #309's 11,689/253 and 24-check results apply only to that baseline.
- Current threat-model guard covers all 130 exposed tools; the locked runtime
  dependency prerelease guard passes. This is not the full CI guard job.

## Native Windows repair-path correction, 2026-09-10

The operator's first Windows run passed dependency, compile, import, capability
and threat-model guards, then stopped at **481 passed, 13 failed, 25 skipped**.
It did not run the full suite. LongPathsEnabled was 0; digest archive names
produced paths of 272-273 characters and staging paths up to 287 characters.
These failures are retained as evidence, not reclassified as passing tests.

Assessment-result Store paths now use the Windows extended-length API spelling,
preserving the same absolute files, exact owner keys and full SHA-256 archive
names. Staging names use a short unique prefix. No registry change, shortened
owner/digest, state migration or test-root workaround is required. POSIX paths
and Windows single-process/directory-fsync limits remain unchanged. This is a
result-store file-I/O correction, not a claim of repository-wide long-path
support: the configured state directory must still be reachable by its owner
directory and lock providers.

Reconfirming an existing archive opens it without truncation using a writable
handle on Windows, as required by
[FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers).
Identical-archive reuse is tested without changing its preserved bytes.

The updated isolated regression with the locked runtime packages passed
**524 tests, with 3 native-Windows tests skipped on Linux**. Those new tests
exercise repair with archive paths deliberately exceeding MAX_PATH. The native
Windows follow-up is recorded below; both full-suite gates remain open. The earlier 25
Windows skips were 3 symlink-privilege cases, 3 FIFOs, 3 directory-fsync cases,
1 unsupported Windows multiprocess case, 14 POSIX flock cases and 1 POSIX mode
contract; the POSIX leg must exercise its applicable cases.

The revised review package preserves the original package and applies a pinned
delta only after verifying the original applied tree and canonical patch hash.
Unknown edits are refused. Its PowerShell launcher uses the process object's
exit status to correct the observed Windows PowerShell 5.1 capture issue.

## Windows full-suite follow-up: configuration documentation, 2026-09-10

The operator applied the exact repair-path delta and completed the Windows
regression: **502 passed, 25 skipped**. This includes the three new native
long-path tests. The first nine dependency/import/capability/threat-model guards
also passed. The full suite then returned exit 1: **11,810 passed, 1 failed,
263 skipped** in 562.64 seconds. Its record is
`local-validation-win32-20260910T075134Z-c16186b5/validation-result.json` under
the operator's `Olympus-PR309-review-20260910-105130-9b90fc01` package directory.
The failed run and both earlier packages remain preserved.

The only failure was
`tests/test_liveness_skew.py::test_clean_environment_reports_no_skew_check`.
The validator explicitly disables five switches. Four of them were absent from
`.env.example`: `OLYMPUS_ASSESS_OSV`, `OLYMPUS_CALIBRATION`,
`OLYMPUS_EARNED_AUTONOMY`, and `OLYMPUS_SLEEPTIME_AUTOAPPLY`. The diagnostic
correctly warned that they were undocumented, even at `0`; consequently the
test's expected clean configuration check was absent. The same failure was
reproduced in an isolated Linux process with the five switches kept at `0`.

The correction documents those four existing switches as commented, disabled
examples. It does not edit a live environment, change runtime behavior, suppress
the diagnostic, remove the safety switches, skip the failing test, or relax any
gate. Existing liveness and environment-documentation tests join the review's
regression list. This expanded 18-file isolated regression passed **588 tests,
with only the 3 native-Windows cases skipped on Linux**.

The Windows full suite must pass again against this exact source before the
Windows leg can close. The 263 reported skips include optional native-model,
browser/provider integration, Docker, and platform-specific coverage; they are
not evidence that those capabilities ran. POSIX flock/directory-fsync coverage
still requires the native-filesystem WSL/Linux full-suite leg. No missing live
credentials, generated-code confinement, production host/data, or activation
authorization is inferred from local test counts.

## Bounded implementation decisions

| Boundary | Decision |
| --- | --- |
| Findings vs knowledge transaction | Shared owner lock prevents concurrent interleaving, but two files are not one atomic database commit. Existing damaged knowledge is rejected before recording a native finding. If findings publish and knowledge publication fails, the caller receives an explicit partial-publication error. Duplicate retries do not increment learned counters; an operator must inspect preserved source before reconciliation. Process death between publications can leave under-counted knowledge. No exactly-once learning or automatic cross-file recovery is claimed. |
| Learning retention | At most 50 CWE classes, 200 fingerprints per class; bounded historical aggregates are priors, not complete forensic history. Duplicate findings already in the findings store never relearn after fingerprint-window eviction. |
| Authentication | Strict JSON/fingerprint consistency does not authenticate an administrator with filesystem write access. Assessment source labels are local producer provenance, not cryptographic attestations. Legacy ambiguous directories remain unclaimed. |
| Atomicity/durability | Unique private staging file, file fsync, atomic replace and atomicio's directory-fsync contract. After a directory-fsync error publication can already be visible; re-read before retry. Host power-loss, filesystem permissions and Windows durability still require C6 evidence. |
| Optional OSV | Corrupt cache yields explicit unavailable coverage without blocking bundled dependency checks. Stale/future entries never become current findings after failed refresh. A successful live response with failed cache write is labeled live-unpersisted. Disabled/replay operation reads no cache and performs no lookup. |
| Coverage | OSV complete means fresh/live coverage of parsed declarations within the cap; it does not cover transitive dependencies or prove safety. Bundled advisories remain a limited seed set. Exported historical findings do not certify completeness of a scan. |
| Repair | Only explicit CLI/API operator repair preserves exact bytes before resetting a damaged store. Valid/missing state is unchanged. Oversized-for-preservation, unreadable, nonregular or conflicting quarantine files are refused unchanged. No automatic reader, prompt or tool repair. |

Restrictions remain: project source and local/mocked verification only; no real
third-party scanning, verifier fan-out, publishing, deployment, collection or
autonomy activation; no fabricated production evidence; no branch deletion,
force reset, cleanup of unrelated files or protection bypass.
