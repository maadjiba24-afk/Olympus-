# M04: private-note ownership and recovery

Source reconciliation: 2026-09-14. Exact base is PR #313 merge
`7a96b42131707303ce5090577537e55fdfe70a6b`, tree
`81ecea4ee2200cff302125dd39ad81b15ad823b3`.

**Status: N1-N9 source implementation connected and locally regression-tested;
Windows/POSIX full suites and protected delivery remain open.** Implementation
and focused cloud evidence are not delivery or overall hardening completion. M04 precedes M03 because background proposal,
consolidation and wiki work must consume correctly attributed notes.

## Finite acceptance checklist

| ID | Confirmed defect / affected surface | Required integrated behavior |
| --- | --- | --- |
| N1 | `memory.USER_SCOPED`: lessons, corrections and feedback under `users/<normalized-owner>`; action notes under `notes/<normalized-owner>` | Full exact-owner keys and validated attribution. Case, punctuation, Unicode, blank/default and long identities must follow the existing canonical-owner contract. Preserve old normalized stores without assigning them to a colliding principal. Explicitly distinguish unclaimed legacy evidence, absent new data and unavailable new data. |
| N2 | Markdown readers use unbounded reads and replacement decoding; path enumerators accept filesystem links and incomplete files | Bound owners, titles, bodies, files, inventories and archives; validate metadata/content before use. Reject symlinks/reparse/nonregular paths, invalid UTF-8, malformed schemas and mismatched attribution with explicit evidence errors. No malformed data may silently become an empty history or a valid title. |
| N3 | `memory._save_note` uses a reusable temporary filename and no durability barrier; action notes use second-resolution overwrite names | Exclusive unique staging and publication, real durability/error handling, process-safe supported-topology serialization and verified retry semantics. Failures before and after publication must distinguish absent writes from unconfirmed completed writes. No duplicate completed action or accidental overwrite on retry. |
| N4 | `save`, search/recent/titles/count/prune and `journey` resolve normalized identities; journey refs are path-only and deletion rereads no content | Connect all direct and contextual readers/writers to exact ownership. Journey list/show/remove must use the caller's owner and bind destructive selection to inspected content, rejecting stale or colliding references. Retain explicitly shared installation notes/skills and the delivered `job_reports` private contract. |
| N5 | `_note_undo` trusts `result['path']` and unlinks it; action execution returns only that path | Save-note results bind exact owner, a constrained canonical note identity and content fingerprint. Undo uses the durable action owner context, refuses forged/arbitrary/foreign/stale/nonregular targets, and handles repeated or interrupted undo without claiming an unconfirmed deletion. Test real prepare/approve/execute/undo callers. |
| N6 | `_memory_roots` and export scope normalize owners; import restores bytes even when a declared checksum does not match, writes before validating all entries and silently skips errors | Exact-owner export manifests and consistent complete scoped inventories; preserve ambiguous legacy scope explicitly. Validate every archive entry/hash/type/path/size/duplicate/scope before mutation. Atomic or journaled recoverable restore with conflict-preserving resume and honest counts. Preserve supported archive/encryption/ingestion-gate compatibility; unsigned archives do not become independent owner authority. |
| N7 | CLI delete preview normalizes even private categories; `delete_memory` and prune swallow unlink failures; note migration rewrites files in place | One ownership-aware preview/effect path, stale-content and conflict checks, durable deletion and explicit incomplete/recovery outcomes. A note-only operation must not claim whole-principal erasure. Frontmatter migration preserves ambiguous legacy attribution and original content until verified publication. M09 remains responsible for full-platform migration/erasure/legal holds. |
| N8 | `_mirror_to_vault` flattens private lessons/corrections into shared category directories and swallows write errors | Preserve owner separation in derived mirrors and shared installation categories. Canonical save and optional mirror status are distinguished; mirror failure cannot cause repetition of a completed canonical write. Old mixed mirrors remain explicitly unclaimed. Private job reports remain excluded by the existing contract. |
| N9 | Wiki material gathering swallows unavailable note evidence and leaks `set_user`; companion evolution also leaves its owner context changed; CLI journey defaults to shared | Connect CLI, tools/MCP, gateway/TUI, companion, wiki material gathering, action rejection and orchestrator feedback to the authoritative owner with token-based restoration. Unavailable private notes must reach consumers before model-driven learning/publication; no guessed positive evidence or cross-owner context leaks. Shared background system jobs remain explicit. |

## Caller inventory

- Canonical storage and archive APIs: `memory.py`; preserve the independently
  delivered private `job_reports` APIs and conversation attribution. The old
  `users/` tree also holds unrelated prefs/wiki/compare/ctxheat stores, so it is
  not a safe blanket private-note export/delete target.
- Separate action-note storage and results: `builtin_actions.py`; durable
  principal binding and action lifecycle: `actions.py`.
- Direct file consumers: `journey.py`, `cli.py` deletion preview and
  `memory.py` listing/search/prune/migrate/export/import/delete/mirror paths.
- Actual route bindings: `gateway.py` journey commands, `tui.py` journey
  commands, `cli.py` memory/journey commands, `tools.py` recall/lesson calls,
  `mcp_server.py` memory search and request identity.
- Private learning/evidence consumers: `wiki._recent_material`,
  `companion.evolve`, orchestrator feedback/learning, `actions.reject`,
  `webreflect`, `migrate`, `digest`, `replaygate` and curator.
- Shared note writers/readers whose boundary must remain intact: operator,
  discovery upgrade proposals, evolve, replaygate reports, scheduler private
  job reports, code/eval reports, tool prompt backups, orchestration system
  reports and TUI background reports. Confirm identity at callers rather than
  changing shared categories into tenant storage.
- Derived optional Obsidian-style mirror: `OLYMPUS_VAULT_DIR`; encrypted export
  still uses the established `vault` interface. M11's KDF/rotation requirement
  is separate and remains open.

## Required validation

Exercise N1-N9 through their actual callers with owned state and mocked model,
notification and network boundaries. Include owner collision pairs and
concurrent owner contexts, oversized/damaged/nonregular input, equal-time
writers, failure at each publication/deletion barrier, stale preview/undo,
malformed multi-entry imports, interrupted restore and safe resume, context
restoration on exceptions, mirror failure after canonical success and legacy
byte preservation. Applicable native Windows and POSIX process/fsync behavior
must run and every skip must be accounted for. Review existing memory contract,
ingestion, journey, action, scheduler-report, companion and wiki tests; changed
expectations must express the repaired contract, not remove protections.

M04 cannot be called delivered until source, adversarial cases, full platform
legs, all exact CI checks, protected merge and synchronization are verified.
M03 and M05-M19 remain open afterwards. Deployment, publishing, collection,
learned routing, autonomy and live verifier fan-out remain restricted.


## Current implementation and evidence (2026-09-15)

All N1-N9 rows have connected source implementations. `note_evidence` is used by
canonical memory readers/writers, archive operations and real save-note actions;
`note_archive` is used by existing CLI/API entrypoints. Action execution and undo
include their action records in the same journal as the note mutation. Shared
web reflection includes its discovery acknowledgement in the note transaction.
Prompt restoration locates validated backup titles across old/new filenames;
M03 still owns full prompt/proposal/benchmark recovery.

The preserved cloud run passed **861 tests, 1 skipped** under kernel-denied IP
network access. The skip is the native-Windows extended-path test. Real POSIX
process writers, concurrent approvals, per-phase recovery, stale content,
archive expansion/checksum/path/schema rejection, ingest compatibility, actual
HTTP parsing without a socket, and connected consumer contexts were exercised.
All six dependency, prerelease, compile, import, capability and threat-model
guards passed. The development-signing notice describes this owned test fixture;
it is not production signing/custody evidence. Platform-specific full suites,
skip review, CI, protected delivery and synchronization remain required.

| Acceptance | Source disposition | Remaining gate |
| --- | --- | --- |
| N1-N2 | Exact ownership, legacy preservation, bounds and unavailable evidence connected | Native Windows/POSIX full suites and exact-source review |
| N3-N5 | Durable journal, integrated action confirmation/undo, content-bound journey references | Native Windows behavior; full platform suites; protected delivery |
| N6-N7 | Entire archive prevalidation, recoverable restore/delete/migration and matching preview/effect | Full platform suites and archive compatibility review on both hosts |
| N8 | Owner-separated optional mirrors, explicit failure receipt and retry | Windows filesystem/mirror fixture and full suites |
| N9 | CLI/tools/MCP/HTTP, gateway/TUI and learning/background callers connected | Full platform consumer regression and protected delivery |

No C4-C9 external gate is closed by these results. M03, M05-M19 and the current
E1-E31 register remain active. In particular, genuine ownership migration/full
erasure (including retained recovery bytes), native Windows multi-process
support, production receipts, current real calibration/routing datasets and
matched comparison measurements still require their listed implementation and
external prerequisites. Discovery acquisition remains an unresolved restriction.


### Windows path compatibility correction (2026-09-15)

The first native Windows attempt applied the original M04 tree, passed all six
guards, then stopped at **667 passed, 20 skipped, 11 failed**. All 11 failures
were unchanged hostile-owner containment assertions: the directory API returned
a Windows extended I/O spelling (`\\?\C:\...`) that did not compare as a child
of the ordinary configured root. The full suite did not start. Its source and
validation receipts remain preserved; it is not a passing Windows gate.

`memory._dir` now returns the original configured-root path after guarded
directory creation. Extended paths remain internal to actual directory/file
I/O. None of the 11 containment assertions were relaxed. The new category
contract cases and an extended native long-path private-report save/read/owner
isolation/prune case cover the correction. The latest isolated focused run
passed **696 tests, 1 native-Windows-only skip**; this includes the existing
security containment consumer. Actual corrected Windows regression/full-suite
validation and the POSIX full-suite leg remain outstanding.

The replacement review package accepts a pristine verified PR #313 baseline,
the exact original uncommitted M04 tree through a forward-only upgrade, or the
exact corrected tree for revalidation. It refuses divergent source, staged work
and mismatched evidence; no rollback, reset, old helper or prior environment
reuse is required. Whole-hardening and production/genuine-data gates stay open.


### Full-suite integration reconciliation (2026-09-15)

The Windows path correction passed **686 regression tests, 20 skipped**. The
first actual M04 full suite then stopped at **8 failed, 12,072 passed, 278
skipped**. All eight failures reproduced in a kernel-isolated four-file run
(154 passed). The full suite remains an unmet gate; both host attempts stay
preserved with their original source hashes and logs.

The configuration diagnostics correctly identified undocumented environment
settings: `.env.example` now explains the optional exact-owner note mirror and
web-reflection switches, leaving them disabled. Diagnostics/tests were not
weakened. The other seven failures came from pre-M04 fixture assumptions:

- Mirrors now live under an exact-owner directory; their integration test
  checks this path and byte-for-byte canonical equality, and forbids rebuilding
  the old flat mirror. A temporary-file obstruction replaces `/proc` in its
  failure fixture so Windows cannot accidentally create a real drive path.
- Ephemeral request tests inspect exact authority inside the real pipeline,
  assert returned model content, and verify restoration of both caller
  contexts. Generic exception swallowing was removed; an explicit pipeline
  failure test proves restoration on the exception path. The existing visible
  UNVERIFIED notice remains supported and is not bypassed.
- Legacy operations now receive explicit schema-1 normalized files in tests.
  New API writes must never be treated as commingled legacy data. Tests verify
  export/quarantine/restore bytes, acknowledgement/audit, refusal to claim
  qualified notes, and preservation of both current and legacy data when
  principal-wide deletion is unavailable. Relocated legacy notes remain
  unclaimed; an acknowledgement alone is not qualified ownership migration.

The expanded isolated regression passed **861 tests, 1 native-Windows skip**.
The operator regression now includes all four full-suite failure files.
Corrected native Windows regression/full-suite validation, reviewed skips,
POSIX full-suite validation, exact-source CI and protected delivery remain
required. M09 complete erasure/migration and all other open master-register
items are not closed by these compatibility corrections.
