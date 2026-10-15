# M11 backup recovery and caller contracts

This describes the isolated M11 implementation. It does not declare full M11
validation, delivery, deployment qualification or remote backup availability.
The integrated vault and credential caller contracts are documented in
`M11_VAULT_CONTRACTS.md`.

## One captured workflow

`backup.run()` captures its memory root, working directory, signing and encryption
selectors, uploader selection and retention policy before mutation. Creation,
delivery, logical retirement and evidence recording borrow that same native
authority. A later ambient configuration change cannot silently select another
root or key for the next stage.

The report separates returned stages from acknowledged inner completion facts.
If a stage returns and its context exit or final authority cleanup fails, the
returned stage remains visible while the enclosing workflow remains unsuccessful.
A narrow acknowledgment never upgrades an unsuccessful enclosing operation.
System interruptions keep their original exception identity and carry bounded
workflow facts. Generic failure reporting does not include raw provider errors,
command arguments, key values or environment dumps.

An uploader attempt has its own UUID32, distinct from the publication UUID32.
Each local publication admits one attempt. Unknown outcomes and damaged or
unfinished earlier delivery history block a new attempt; changing the requested
UUID does not authorize another upload. Two successful command exits describe
local command completion. They do not attest remote receipt, retention or restore.

## Retained local receipts

Successful returned stages are recorded under
`MEMORY_DIR/.olympus-backup-evidence`. The bounded append-only namespace has a
native root binding, its own lock and initialization marker, and immutable
`backup-<UUID32>.json` / `restore-<UUID32>.json` files. Copied, damaged, busy,
unexpected or over-capacity namespaces refuse operation. Receipt writes do not
overwrite or repair existing evidence.

The trusted coordinator seals actual returned stage summaries before recording.
The writer does not accept arbitrary caller result dictionaries. A successful
write and its checks return a distinct acknowledgment bound to the operation,
archive hash and receipt hash. If a later stage exit or authority cleanup fails,
the caller retains that acknowledgment without claiming workflow completion.

Inspection is observational. A visible receipt does not reconstruct a lost write
acknowledgment, confirm current archive availability or prove completion of the
enclosing workflow. It does not repeat an upload, settle recovery, acquire an
encryption key or delete evidence.

The evidence namespace is excluded from subsequent snapshots, including full
snapshots. Restore rejects a case-equivalent top-level evidence namespace before
creating the target root. A restored archive cannot plant a copied local evidence
namespace and make it appear native to the destination.

Logical retirement withdraws an archive from automatic selection while retaining
both final images and recovery journals. It deletes no files and reclaims no
capacity. Filesystem memory archives do not cover PostgreSQL or establish an
application-consistent database snapshot.

## Local commands

The explicit recovery commands run after local environment-file loading and
before encrypted configuration hydration and service boot checks. Ordinary
backup creation, including `backup --no-deliver`, retains the usual boot checks.

| Command | Scope |
| --- | --- |
| `backup --list` | Observe the bounded local catalog. |
| `backup --drill [ARCHIVE] --into OFFLINE_ROOT` | Restore one consumed archive image offline and retain its evidence. Omitting `--into` allocates and retains a new parent with an absent child target. |
| `restore ARCHIVE --into OFFLINE_ROOT` | Verify and restore into an explicit offline target. |
| `backup-evidence receipts` | Observe the local stage receipt namespace. |
| `backup-evidence publication-inspect --operation-id ID` | Observe one retained publication. |
| `backup-evidence publication-resume --operation-id ID` | Explicitly settle that retained publication using its retained images. |
| `backup-evidence publication-retire --operation-id ID` | Explicitly retire that publication without deleting its images. |
| `backup-evidence delivery-inspect --archive ARCHIVE` | Observe delivery history; never retry the uploader. |
| `backup-evidence restore-inspect --into ROOT --operation-id ID` | Observe one retained offline restore. |
| `backup-evidence restore-resume --into ROOT --operation-id ID` | Explicitly settle the same retained offline restore. |

Mutation IDs are printed and flushed before the action starts. Failure to print
the initial request prevents mutation. If final output fails after a returned
result, the command retains that result, reports output uncertainty when possible
and never repeats the operation. Reporting failures do not replace an existing
system interruption. Requested IDs and observed older recovery IDs remain
separately labeled.

An external archive pair requires `--external`; this carries no local publication
journal claim. `--force` permits another restore only within an intact restore-owned
root. `--insecure` overrides a well-formed signature decision only; it does not
disable custody, bounded archive decoding or per-file integrity checks.

The heartbeat reports known returned stages and observed recovery IDs. A failed
notification does not repeat the backup or erase its prior stage outcome. This
contract does not qualify arbitrary abandoned worker threads or remote providers.

## Deployment readiness remains blocked

Legacy `record_backup(dict)` and `record_restore(dict, ...)` calls are refused
before inspecting their input or writing files. Historical legacy receipt files
remain intact and are not read to qualify recovery. Native stage receipts appear
only as diagnostic counts, with observed and unavailable states distinguished.

Both mandatory checks, `recoverable_off_machine_backup` and `restore_drill`, remain
false until M14 implements and qualifies a target-bound remote recovery proof
contract. Named deployment readiness therefore cannot currently become ready,
even after successful local uploader commands and an offline local restore drill.
This is an explicit pending qualification boundary, not a renamed local restore
success gate.

The native receipt observation does not bind separate mount, lifecycle and
permission probes to one held target authority. Existing lifecycle receipts and
platform permission checks still require M14 review and intended-host evidence.
No installation, reboot, deployment, real remote storage or monitoring destination
is qualified by these local controls.

## Validation scope

Focused Windows and WSL tests use fresh owned roots, mocked uploader commands and
notifications, blocked network access, retained test artifacts and bounded process
containment. They cover actual archive publication, offline restore, local receipt
recording, interruption facts, output failures and explicit local recovery.
Their pass counts belong only to their exact source inventories. Earlier failures,
fixture corrections and narrower reruns remain separate evidence. Full candidate
Windows, WSL and owned PostgreSQL validation and protected delivery remain required.
