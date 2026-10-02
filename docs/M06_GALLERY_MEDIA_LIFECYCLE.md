# M06 gallery/media ownership and recoverable lifecycle

**Status: implemented; native execution reviewed; protected delivery pending.**
This batch continues delivered M01–M05. Its source baseline is PR #316 main
`7a9b8be8b30c08928a60bb56cb45b0ece69ba8f8`, tree
`0dad6df8d95a0c73087db7ae549ff75bbd4137bb`. The 2026-10-01 operator
read-only reconciliation reported matching main/tree/parent, all 87 reference
records, all 86 unrelated file content hashes and all 39 M05 working-file
hashes; tracked worktree/index were clean and selected Git metadata stayed
unchanged. That reconciliation is not a new full native test run.

## Finite acceptance contract

Every row requires implementation, actual connected callers, owned executed
checks, and reviewed evidence. A helper or documentation entry alone does not
close a row. No row is currently claimed delivered.

| ID | Required contract | Required negative/recovery evidence |
| --- | --- | --- |
| G01 | Exact owner at generation, edit, list, read, delete, status/recover and claim | Normalization/truncation/case/Unicode collisions; missing/invalid explicit owner; exact thread context; cross-owner refusal |
| G02 | Missing, unavailable and unclaimed are distinct | Corrupt/empty/foreign-owner/version/duplicate-key state; stat/read/list denial; legacy hidden with login either on or off; no empty-state replacement |
| G03 | Confined regular paths and bounded identities | Traversal/absolute/drive/ADS/control/reserved names, links/reparse/hardlinks/nonregular paths; no sibling-root union; native Windows deep paths |
| G04 | Admission before provider work | Invalid owner, prompt, operation/source identity, filename, configuration and capacity produce zero provider calls; concurrent name/capacity reservations |
| G05 | Bounded validated image bytes | Raw JSON cap, strict base64, cardinality/types, actual PNG/JPEG/GIF/WebP/BMP decoding; truncation/spoofing/dimension/frame/aggregate bounds; no output-URL fetch |
| G06 | Atomic publication | Readers see only a committed complete object; write/flush/fsync/rename/directory-barrier failures retain old and uncertain bytes and expose failure/pending |
| G07 | Durable non-replaying operations | Stable ID before provider work; same request retries reuse receipt; different request conflicts; failures at reservation/start/output/object/manifest/acknowledgement; recovery never calls providers |
| G08 | Source identity and recoverable delete | Edit/delete require listed image identity and content revision; stale name reuse and changed bytes refuse; repeated exact delete is idempotent; deleted bytes remain recoverable |
| G09 | Explicit qualified legacy claims | Flat and old normalized-owner roots remain unclaimed; reviewed source hash and exact attribution; two-owner/conflicting target/changed-source/partial failure cases preserve originals |
| G10 | Supported concurrency | Threads and POSIX competing processes; provider call outside lock; process-death/interrupt outcomes; Windows single-process topology explicitly retained |
| G11 | Connected API/CLI/tool behavior | Strict input types, exact owner, stable IDs, typed errors/pending, correct nonzero exits, authz and shadow behavior; no string-prefix success guessing |
| G12 | Executed UI behavior | HTTP/network/JSON failures never look empty; stale delete, repeated clicks, pending recovery, timeout identity, out-of-order refresh, close/reopen and image failure |
| G13 | Complete scoped inventory/documentation | Manifests, blobs, staging/recovery, tombstones, receipts, legacy originals/claims/locks; explicit backup coverage and M09 erasure obligations; historical M05 test scopes preserved |
| G14 | Regression and protected delivery | Confined owned focused checks; required fresh native Windows/ext4 WSL regression/full evidence; reviewed skips/warnings; exact tree/patch, all applicable feature/main CI, protected merge and synchronization |

## Storage and finite bounds

Authority lives under `<workdir>/gallery-v2/<owner_key(exact-owner)>/`:
`initialized.json`, `state.json`, `objects/<opaque-id>.blob`,
`outputs/<opaque-id>.json`, `store.lock`, and any retained `pending-*.json`
publication files. The initialization sentinel makes loss of an existing
manifest unavailable rather than a new empty store. Global `claims.initialized`,
`claims.json` and `store.lock` bind operator attribution across owners. Claim
identity includes workspace/device/inode, with source path history checked;
normal directory-name spelling is not authority.

- Owner: 8,192 characters / 32,768 UTF-8 bytes; display name: 128 UTF-8 bytes
- Image: 16 MiB; edit input: 8 MiB; prompt: 32 KiB UTF-8; provider JSON: 24 MiB
- Decoded image: dimensions at most 8,192, 16 million pixels per frame, at most
  32 frames and 32 million aggregate pixels; actual supported format decoding
  plus complete-container boundary checks, without reencoding or metadata trust
- Owner snapshot: 8 MiB; request metadata: 64 KiB; output provenance: 8 KiB; durable output receipt: 64 KiB;
  16 KiB snapshot headroom reserved per pending operation; JSON structure depth at most 32
- At most 1,000 live images, 512 MiB live bytes plus worst-case pending images,
  and 10,000 lifetime operation receipts; no silent receipt reuse/pruning
- At most 1 GiB retained owner storage, including deleted/orphaned images and
  abandoned publication bytes; storage inventory is bounded to 50,000 entries
- Before new provider work, reserve 16 MiB image + twice the 8 MiB snapshot +
  64 KiB overhead for every pending/new operation against retained quota and
  currently observed filesystem free space. Unavailable space evidence refuses.
  This is admission evidence, not a guarantee against unrelated concurrent
  disk consumption or a later storage failure
- Legacy inventory scans at most 10,000 total entries and reviews at most 1,000
  candidates per explicit page; global attribution has at most 10,000 claims

Recoverable removal does not free retained-storage quota. No automatic erasure
or evidence cleanup is performed to admit more paid work. M09 must supply its
separate reviewed erasure/migration path. A damaged authority is preserved and
unavailable; operation recovery does not pretend to reconstruct arbitrary
corruption or authorize a fresh empty store.

## Caller identity and recovery interface

Generation/editing requires a caller-owned stable 32-hex operation ID before
provider work, including the string-result Python adapters. Those adapters no
longer silently invent an ID that could be lost with a response. A valid ID is
retained in error output. The web creates the ID before POST; CLI prints it
before work; image tool schemas require it. Image tools expose explicit
`run`, `status` and `recover` modes; status/recover never invoke providers.
Editing/removal binds the listed image ID and content revision, not its name
alone. Generated images can be analyzed via the exact-owned gallery ID/revision
route; invalid/stale/cross-owner gallery identity never falls back to the shared
filesystem route.

Legacy claims expose their reviewed manifest digest before mutation and per-item
operation IDs in outcomes. After an uncertain acknowledgement, retry only the
same unchanged reviewed manifest. Claims copy locally and never invoke providers.

## Authority and residual boundaries

The gallery surface has its own exact-owner authority. Login configuration,
a normalized legacy directory spelling, filename similarity and a current
session are not proof assigning historical images to an owner. Claims require
an explicit reviewed attribution operation and preserve source bytes.

The shared sandbox file tools remain a separate, explicit workspace trust
boundary. Gallery isolation does not re-root those tools or claim whole-workspace
tenant confinement. A trusted installation administrator controls unsigned
state; content hashes are integrity bindings, not independent attestation.

Gallery removal is recoverable removal from view, not certified physical
principal erasure. M09 must inventory all new authoritative and recovery state,
references in conversations/tool outputs, old roots, backup/export copies and
cross-owner claim records. M14 still requires consistent backup snapshots,
actual host custody, restore/rollback, survival and delivery evidence. Existing
MEMORY_DIR backup coverage cannot be assumed to include a separate workspace.

Windows retains one process per state directory until M13 supplies and proves
its complete supported-topology contract. POSIX locking and directory barriers
do not establish distributed sessions, quotas, metrics or HA.

## Validation and operational separation

Cloud runs use owned image/provider fixtures and a verified kernel IP-denied
launcher. No unconfined cloud full-suite run is allowed. Real HTTP transport,
Windows handle/junction/deep-path cases, and native POSIX process/filesystem
behavior require their applicable platform evidence. Owned DOM/fetch execution
is not a real browser measurement. Every failed run and recovery artifact stays.

No code change, fixture pass or merge authorizes live image generation,
deployment, publishing, collection, routing/autonomy activation, broker work or
competitor measurement. Those prerequisites and authorizations remain separate.
M06 is not complete until G01–G14 are actually evidenced and delivered; M07–M19
remain open afterward.

### Windows correction candidate after the first native regression

The first native M06 candidate did not pass. In particular, the actual
`FILE_WRITE_ATTRIBUTES` (0x100) attack converted a pinned empty directory into a
junction. Windows sharing flags do not prevent attribute access. The old
share-mode assertion must not be reported as successful confinement. Windows
`DirEntry.stat()` also supplies zero link-count/identity fields, which incorrectly
refused ordinary retained files; claim publication encountered actual sharing
violations during pathname-based replacement. Original failure evidence is
retained independently of this correction candidate.

The corrected gallery-specific Windows layer uses `NtCreateFile` with a held
`RootDirectory`, one checked component, `OBJ_DONT_REPARSE`, and
`FILE_OPEN_REPARSE_POINT`. Returned handles must identify the required regular
file/directory with no reparse attribute and, for files, exactly one link.
Children and enumeration remain relative to held handles. Retained-file
inventory obtains real handle metadata; available space is queried from the
held volume. The exclusive staging handle survives flushing through a
same-directory, basename-only `NtSetInformationFile` rename. Only the rename is
retried for the existing bounded sharing-error policy. The shared M05 publisher
is unchanged.

The native regression now distinguishes successful OS metadata mutation from
successful gallery confinement: the known 0x100 attack must execute, and gallery
reads, writes, creation, enumeration, publication and capacity observation must
not reach its redirected target. Additional tests inject mutation after the
check at relative-open, enumeration and rename syscall boundaries. A filesystem
may refuse junction conversion once the staging file makes the directory
nonempty; that refusal is distinguished from the required successful empty-dir
attack. These tests, actual handle-sharing recovery, long paths and the complete
native suites remain delivery gates. Cloud ABI/mocked/POSIX tests do not certify
native Windows behavior. The subsequent native runs and their exact source
scopes are reconciled below; protected delivery remains pending.

Primary API contracts: [Python Windows DirEntry metadata](https://docs.python.org/3.12/library/os.html#os.DirEntry.stat),
[Windows attribute access and sharing](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
[relative native opens](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntcreatefile),
[no-reparse object attributes](https://learn.microsoft.com/en-us/windows/win32/api/ntdef/ns-ntdef-_object_attributes),
[handle enumeration](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex),
and [same-directory rename](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information).

## Native evidence reconciliation, 2026-10-02

**Implemented; reviewed native execution complete; protected delivery pending.**
The full native results below belong to the exact 1,088-file candidate tree
`ce79e7588891530b9aa8bdd33e148169539737bf`, reconstructed on the authentic
M05 baseline above. Initial delivery commit
`4a39760dab7d3111127858d16bb5131982fda753`, tree
`a88c86fe7ab2266f6941f2b2cfb735292445f104`, added documentation only.
Its Windows CI subsequently exposed the test-only encoding correction recorded
below. Full native results remain evidence for `ce79e758...`; they are not
relabeled as full reruns of a later fixture/documentation descendant. Production
code, dependencies and workflows remain unchanged; the later test delta is
exactly two explicit UTF-8 settings. Tracked modes remain identical. No workflow
authorization is changed by this reconciliation.

### Candidate and harness identity

| Artifact | SHA-256 |
| --- | --- |
| `M06-source-no-bytecode.zip` | `e38dfadec1d47ab13cf63faa0b4db5b79381c6ee1c223c71e3e2ed9b7e93fc42` |
| Canonical `source.patch` | `27bf10461b36831809d02f587e6c8431b8e8f58a4a36193f9930d20ba053c014` |
| Source `manifest.json` | `279a744ca08788590ee9b936c18638e927ee9a2a455c4fcc9db447a135ee73c0` |
| `M06-native-release-no-bytecode.json` | `004c4165caef3877405ccfc510178995891187c57043b5219b1f8013a84b8c4f` |
| Pinned `native_driver_v6.py` | `4a6c63dc1f9581b36860d20c89a8442b711a22894d2d367a4705531cf569f2ab` |

The Windows workspace serialization of the release has different CRLF bytes;
the parsed release identity matches the pinned package. WSL uses the exact LF
package bytes. The driver explicitly loads the canonical `timeout` entry point
with plugin autoload disabled. Separate owned Windows and WSL probes verified
registration and actual timeout enforcement, not just plugin import. Their
result hashes are respectively
`2b5b049a7974593289edaa1c2468ee7efcbc05ea04cd5e94d10af7f53dab5e4c`
and `4393792926f8ba8300e1e7f1a7ced0ae2c48e66031788ef48343cbfcdab09666`.

### Completed standard runs

| Platform and retained stage | Passed | Skipped | Failures/errors | Warnings | Required exact cases |
| --- | ---: | ---: | ---: | ---: | ---: |
| Windows `regression-731esy08` | 1,124 | 31 | 0 | 0 | 44/44 passed |
| Windows `full-1a20dt0q` | 12,558 | 299 | 0 | 0 | 44/44 passed |
| WSL/ext4 `regression-sywcw3zh` | 1,125 | 30 | 0 | 0 | 30/30 passed |
| WSL/ext4 `full-oaog1y7d` | 12,602 | 255 | 0 | 10 | 30/30 passed |

Windows evidence root:
`C:\Users\moust\Downloads\Olympus-M06-native-16vykcjw`.
WSL evidence root: `/home/maadjiba/Olympus-M06-native-jabn1d8b`.
Each standard full run collected the same 12,857 unique testcase identities,
used the standard full selection (no `-x` or test selector), and finished with
both pytest and driver exit 0. Windows finished at
`2026-10-02T17:02:50.853416Z`; WSL at `2026-10-02T17:24:48.121211Z`.
No owned validation process remained at the final process inspection.

| Exact retained artifact | SHA-256 |
| --- | --- |
| Windows `regression-731esy08/attestation.json` | `9d0fc6d015174c898a3607b72877f765c04d627c47ce53cc5abc42e20d7c8b53` |
| Windows `full-1a20dt0q/attestation.json` | `c4f07a8c9e6d66ccbd3151f5f70abfeeb08ce5b1cc639a5ee73a5750df4379fe` |
| Windows `full-1a20dt0q/result.json` | `e0d24150d5cc009190c0132616a87104b648bf8e8082a83a0f4f0dc970768cc5` |
| Windows `full-1a20dt0q/full.xml` | `27b046c64ce779a0fdc6ccc4683c6b40fa2969f81adb4aa2060b4bde405c21ad` |
| WSL `regression-sywcw3zh/attestation.json` | `da6620dcc5b45c1a184f0799243de5c663cba7fc5a61b0aa2ba22430b35dff64` |
| WSL `full-oaog1y7d/attestation.json` | `8245b34d100d292d8925a60ef81f66d2f294c15784d5b5654d2341fe178d31ab` |
| WSL `full-oaog1y7d/result.json` | `506dcebf563719221bd317db2e947207b8860b930907f9d8327d328fc5fd861b` |
| WSL `full-oaog1y7d/full.xml` | `8420be9da7f0430ae8bf6257688b315b67e192172fd3c8bda30ba55f7e57bde5` |

The full attestations verified all 229 Windows and 22 WSL artifacts. Inclusive
source inventories remained exactly 1,088 files with no changed bytes, missing
files or added bytecode. Dependency freezes remained unchanged: Windows
`f9a7d191c41b04804205e755e1e9e400bc30f395479103ddf96dadb8b4550306`, WSL
`517611a731105ba77aa174a63af66f0704059145a5469c2bee630e1636517ee5`.
WSL source, environment, profile, memory, temporary files and evidence were on
native ext4 `/dev/sdd`, under the owned non-root user. Windows ordinary fixture
depth remained 98 characters; dedicated native paths beyond 260 characters
remained mandatory and passed.

The Windows original-repository preservation digest before/after the full run,
and again before/after isolated delivery preparation, was
`a25216ffbf1ae1c88f6b61ebdad81d27c0fd9f6b41ffabf178c41c998da338b6`:
87 reference records, 86 unrelated files and 2,273 retained evidence records
matched. WSL reports `preservation: null`; it is not independent evidence about
the Windows repository. Child Git environment omissions did not change the
owner's environment. Existing workspaces and failed evidence remain preserved.

### Reviewed skips and warnings

The full runs share 219 skipped cases. Another 80 Windows-skipped cases passed
on WSL, and 36 WSL-skipped cases passed on Windows. None of the shared skips is
reported as a pass.

| Skip category | Windows | WSL |
| --- | ---: | ---: |
| Optional torch cases | 136 | 136 |
| Platform, permissions, modes or topology | 69 | 36 |
| Unsupported generated-code confinement | 45 | 42 |
| Browser opt-in cases | 24 | 24 |
| Live provider/search opt-in cases | 11 | 11 |
| Docker unavailable | 5 | 0 |
| Separate M03 owned PostgreSQL fixture | 4 | 4 |
| Node unavailable | 2 | 0 |
| actionlint unavailable | 1 | 0 |
| Unconditional legacy integration placeholders | 2 | 2 |

The Windows chmod-related skip wording about "running as root" reflects those
fixtures' platform/mode checks; it does not establish an administrative host
identity. WSL actually passed the owned gallery DOM and comparison fetch
contracts, and the automatic two-process machine-global admission-cap test.
All 24 `test_phase5_client_compat` cases passed on both platforms: real SDKs
over owned HTTP/SSE transport with stubbed upstreams. This is staging evidence,
not production Claude Code compatibility.

Two `test_val_integration` cases (`test_real_client_compatibility_of_v1_messages`
and `test_cross_process_admission_saturation`) unconditionally skip as historical
placeholders. Their skip is not proof that SDKs were absent or that current
cross-process evidence was unavailable. The four PostgreSQL cases explicitly
require the separate `OLYMPUS_M03_POSTGRES_FIXTURE` leg and skip before importing
psycopg when it is absent. No fresh M06 PostgreSQL run is claimed. Historical
M03/M05 PostgreSQL evidence retains its original scope; this filesystem-only
M06 change leaves the shared publisher unchanged.

All ten WSL warnings were reviewed and retained:

- Eight Python 3.12 multi-threaded `fork()` deprecations in
  `test_owner_workspace_evidence::test_posix_process_transactions_do_not_lose_updates`.
- One identical `fork()` deprecation in
  `test_trading_native_isolation_adversarial::test_rlimit_nproc_is_measured_and_found_insufficient`.
- One Python 3.14 tar extraction-filter deprecation in
  `test_release_pipeline::test_actionlint_accepts_the_publish_workflow`.

Those three cases passed. Their files are unchanged by the M06 patch; this does
not claim a baseline execution reproduced the warnings. Nothing was suppressed.
Native owned DOM/fetch passes do not replace browser CI. The exact delivery
commit still requires the applicable browser and Docker CI checks, all 24
standard checks and any additional applicable checks.

### Preserved failures, interruptions and corrections

Earlier candidate results remain separate evidence, not passes of this tree:

1. The original Windows attempt recorded 983 passed, 128 failed and 25 skipped.
   The corrected handle-relative candidate `179dcc3df16d28936be792a7f7fe388efa75e805`
   then recorded 1,111 passed, 11 failed and 31 skipped. Fixture correction in
   `test_gallery_review_contracts.py` produced tree
   `56feceb77f9b4c0cd4f209fe2109a2687b353086`.
2. The v5 Windows full run on that tree recorded 12,557 passed, 299 skipped and
   one failure: explicit loading as `pytest_timeout` did not register the
   canonical `timeout` entry-point name. Its retained attestation is
   `4cf0ae49ed14c9b1515b48460f5e16321ad8e54ff320d343811e7be3ff048775`.
   The v6 harness changes only that loader argument, retains disabled plugin
   autoload, adds the exact registration case to the mandatory gates, and has
   the executed enforcement probes above.
3. The v6 WSL regression on `56feceb7...` passed its testcase gate but failed
   the source guard because two `-I` subprocesses ignored inherited
   `PYTHONDONTWRITEBYTECODE`, adding five `.pyc` files. All five remain in
   `/home/maadjiba/Olympus-M06-native-xjmhesep/source/olympus/__pycache__`.
   Its failed source-guard attestation is
   `030df92023c62c6f9ad6fc70222fe55c68ae36b2cd32f4fbd0bc026e2973bdda`.
   The only subsequent source change adds `-B` and asserts isolated/no-bytecode
   flags in those two child fixtures in `tests/test_gallery_native.py`, producing
   the native-tested `ce79e758...` tree. No inventory exclusion was added.

The first concurrent full attempts on `ce79e758...` were interrupted by a tool
transport disconnect. Windows `full-ztzl1edo` stopped around 39% and WSL
`full-1iqblgvf` around 50%, each with one unidentified `F` marker. Subsequent
inspection found no owned process and no completion result, JUnit XML or final
attestation. Missing results alone were not treated as proof of completion.
Their stdout SHA-256 values are respectively
`d7b43e6eeef8d23b61d489d523852869d145338db07536c59b1b9ea93ae25a2f`
and `61513c382e5e7be8a4109fad3ec39a723434c07d66223aa3f894d9c235b09fd7`.
The identities and causes of those markers remain unknown; no fix or root cause
is claimed. All interrupted artifacts were hash-preserved across later attempts.

A separate Windows full-collection first-failure diagnostic (`diag-ff-efa25533`,
`-x -vv --tb=long`) finished 12,558 passed/299 skipped, no failures or warnings,
44 mandatory passes. Its attestation is
`194182ba7ffdcbbce5784f3620f73c1d2e6a754b302ffdac8788308587ccb3ed`.
It did not reproduce either marker and is not substituted for standard full
validation. The accepted standard Windows and then WSL full runs in the table
were separate sequential attempts and passed without changing source or harness.

### Remaining delivery gates

G14 remains open until the documented delivery sequence finishes: review the
exact documentation descendant and its relationship to the tested tree; commit
and push normally; open the draft PR; require the exact head's full applicable
CI including browser and Docker; reconcile the final gate; perform the protected
squash without bypass; verify actual merge parent/tree/diff, post-merge checks
and fast-forward synchronization. No merge or synchronization is claimed here.
M07-M19 and all separately authorized operational gates remain open.

### Windows CI encoding correction

PR #317's first head `4a39760dab7d3111127858d16bb5131982fda753` did not pass
the delivery gate. CI run `37044674510`, Windows job `110963023568`, finished
**1 failed, 12,576 passed, 280 skipped** in 1,690.71 seconds. The required
`test` aggregate (`110974234000`) failed; the other 23 checks succeeded. No merge
was attempted. The sole failed case was
`tests/test_web_gallery.py::test_owned_gallery_dom_contract`: its default
`Path.read_text()` tried to decode the UTF-8 `web.py` source as CP1252 and raised
`UnicodeDecodeError`. Earlier local Windows native runs explicitly retained this
Node-dependent skip; WSL's Node case passed. Neither result proves the missing
Windows encoding path.

The correction changes only that fixture's source read and Node subprocess
text transport to explicit `encoding='utf-8'`. The owned Node fixture already
reads stdin as UTF-8. No production code, assertion, skip, dependency, timeout
limit or workflow changes. The staged test-only tree is
`537e238c7b807fea8bef8bba7abb712bf1f91025`; the two-line patch SHA-256 is
`469747939606034800140ec0e25dd6e18fa7135d316f592a6413dd898861349f`.
The corrected `tests/test_web_gallery.py` SHA-256 is
`78dcb0d2acb023bfd3124cf164b5a467420f2458fc020f20e9dc397c85ab3b79`.
This report/status reconciliation is a documentation-only descendant of that
focused-regression tree; its final commit/tree and CI remain separately recorded.

Owned Windows evidence is retained under
`C:UsersmoustDownloadsOlympus-M06-dom-n9fol6_c`:

| Stage | Actual result | Result JSON SHA-256 | JUnit SHA-256 |
| --- | --- | --- | --- |
| `reproduce` on initial delivery tree | Exactly the expected CP1252 decode failure; 1 failed, 0 skipped; pytest exit 1 | `d07af51ded6ed6d34fe3dfce1bb35ef52e21612e68488cdf92cb27bf29f51037` | `1220a37d60a590e1f17a7c32d767d5f7cceac2f30c47c2b9e601c575655618de` |
| `regression` on corrected test-only tree | 1,126 passed, 29 skipped, no failures/errors/warnings; pytest exit 0; 105.76 seconds | `ab18273099d58c62ff907e92e3b4844280d17fc949d0a6038217936600325642` | `97045889b8cde02fd7a9b3147c53b8f166fb468d788256f95ccbc0f9f4e1d6da` |

Both stages used the actual Windows CP1252 file codec with Python UTF-8 mode
explicitly disabled, isolated Python and bytecode disabled, an asserted import
binding to the delivery source, disabled plugin autoload with explicit `timeout`,
a fresh owned profile/memory/temp root, and the available Node v24.19.0 executable
(SHA-256 `3602f2bb1a10f2cbab4c36886218a33c1ab3db87290e73b033c46c77147d0237`).
Node was added only to the test child's allowlisted PATH; no host installation or
environment change was made. The existing private dependency freeze remained
`f9a7d191c41b04804205e755e1e9e400bc30f395479103ddf96dadb8b4550306`.

The corrected regression ran the same 39 pinned selectors and required all 44
Windows native cases plus both owned Node gallery/comparison cases: **46/46
required cases passed**. All 1,088 source files remained unchanged during each
stage, with no added bytecode. Original-repository preservation matched before
and after. The 29 skips are the retained Windows POSIX/symlink/permission/process
topology boundaries; the two former Node skips executed and passed. They are not
reported as browser execution or a new full native suite. A prior local harness
attempt stopped during temporary-directory setup before the case; its separate
`Olympus-M06-dom-hrbqu05k` evidence is preserved and is not the reproduced failure.

The first head's browser/Docker jobs actually ran: browser-smoke 20 passed,
Firefox 4 passed, WebKit 4 passed and Docker 4 passed. Each had one unknown
`timeout` pytest configuration warning; browser-smoke additionally had 22
websocket `connect()` deprecations. These are retained and do not establish
per-test timeout configuration in those unchanged jobs. Their workflow limits
remain 15 minutes for browser-smoke/Docker and 20 minutes for Firefox/WebKit.
The separate native enforcement probes retain their own scope. These old-head
passes do not replace the corrected head's complete required CI. M06 remains
open until corrected-head CI, protected merge, merged-main CI and synchronization
are verified.
