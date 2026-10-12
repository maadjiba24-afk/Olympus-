# M11 vault and credential caller contracts

This describes the integrated candidate based on delivered M07 commit
`2723488e065029406152fcc41995deace0e9c4ff`. It does not declare M11 validation,
delivery or deployment qualification. M08 remains a separate candidate.

## Encryption and bounded decoding

New vault records and encrypted note/backup archives use format version 1:
scrypt with N=131072, r=8, p=1, a random 16-byte salt and a 32-byte result,
followed by Fernet authenticated encryption. The principal scrypt work array is
approximately 128 MiB; backend overhead is additional. Only this fixed profile
is accepted. Unsupported parameters and oversized input refuse without a weaker
derivation or legacy fallback.

The outer header contains magic `OLYVLT` followed by two zero bytes, format and
profile bytes, the salt and an opaque 16-byte key identifier. The authenticated
payload repeats the header and exact purpose/owner context. The identifier is
not a fast password hash or verifier. `VAULT_KEY_VERSION=2` remains the separate
exact-owner addressing version.

The codec accepts at most 64 MiB of plaintext, 1024 context bytes and 4096
passphrase bytes. Vault records impose the smaller 4 MiB JSON limit, 1024 entries,
8192 nodes, depth 16 and 65536 bytes per string. Duplicate keys, invalid Unicode,
unsupported scalar values and malformed records refuse. Note and backup archives
use distinct purpose contexts and explicit size bounds. Fernet processes complete
messages in memory.

Nonblocking process-local admission bounds concurrent message and scrypt work.
A fork receives separate admission state. This is not a host-wide memory ceiling
across arbitrary processes. General worker topology and deployment budgets remain
separate M12/M13 work.

## Captured authority and publication

An operation captures its exact owner, backend, state root and key custody before
work. File transactions retain native directory authority and fresh identity
checks. PostgreSQL transactions use the captured connection selection and span
vault/recovery changes; filesystem identity does not authorize a database
transaction. Unsupported or ambiguous custody and backend selections refuse.
Key files are bounded regular files with native ownership/permission checks;
Windows mode bits alone are not an ACL custody claim.

Reads return detached values and never rewrite old ciphertext. Failures to read,
authenticate or inspect custody are not converted to absent credentials. CRUD
read/modify/publish and deletion occur within the selected backend transaction.
Snapshot updates compare the whole captured row/file revision. A retained
`PendingUpdate` publishes its prepared ciphertext, recognizes that same committed
ciphertext after a lost acknowledgment, or refuses a conflict. It does not rebase
onto a changed credential or select a new root/key/backend.

Native cleanup uncertainty remains distinct from ordinary refusal. Actual
KeyboardInterrupt/SystemExit objects survive secondary cleanup errors. Unknown
cleanup blocks further native settlement; fixed public facts omit credential
values and raw provider exceptions.

## Legacy records, migration and rotation

Explicit legacy decoding reports version 0 with unauthenticated owner context.
Reading it proves neither historical ownership nor migration. Legacy addressing
collisions remain quarantined until an explicit ownership action; outbound secret
inspection refuses unreadable or unresolved applicable quarantine.

Migration and rotation require retained operation IDs and preserve original
encrypted recovery copies. Prepared, published and committed records bind source
and target hashes. New ciphertext is verified before completion. Resume uses the
selected operation and retained images; conflicts are preserved. An observed
historical receipt does not prove that the current live row still equals it.
Discard removes the live quarantine entry while retaining its encrypted recovery
copy. Old copies remain decryptable with their old key.

Rotation changes neither environment variables nor a key file automatically.
Replacement custody is explicitly captured. Operators must retain the operation
ID and the required old/replacement custody for reconciliation. There is no
implicit migration during read, readiness checking or secret hydration.

## Private remembered login

A remember request does not authorize the site. Its exact versioned record has a
random request ID and canonical domain. Claiming it adds a distinct workflow ID
through a full preference-image compare-and-swap. A second claimant, replacement
request, malformed policy or interrupted prompting/saving record cannot silently
admit another prompt.

The workflow captures preference and vault authority before private input. Only
a confirmed prompt claim admits input; getpass fallback warnings refuse. Saving
intent is confirmed before credential publication. After confirmed vault storage,
the historical saving image authorizes remember mode and clears the matching
request together. Concurrent policy changes refuse authorization without rebasing.
Storage and authorization are separate facts; this is not a cross-store atomic
transaction.

The local `/login` status, resume, settle and cancel actions expose bounded facts
and use the original workflow identity. Status performs no storage or custody
work. Resume permits input only for a prepared ready workflow; settle never asks
for credentials again or reacquires authority. A bounded process/thread-local
registry retains unresolved attempts and refuses capacity rather than evicting
them. Process death does not create a durable resumable private handle; retained
prompting/saving markers require explicit reconciliation.

Input interruption, cancellation, vault uncertainty, authorization uncertainty
and output failure remain distinguishable. Reporting failure does not erase a
known storage result or trigger another publication. Legacy direct credential
entry points require a prepared workflow identity.

## OAuth and signing consumers

OAuth captures writable vault authority before authorization-code exchange or
refresh. Provider completion and local publication are distinct. A lost local
acknowledgment retains one original-executor settlement handle, its response and
prepared update where available. Local settlement never repeats provider dispatch
or extends token expiry by recomputing its start time. Disconnect or replacement
conflicts refuse the stale update.

Response cleanup, storage cleanup and reporting uncertainty remain separate
facts. If the request-local handle is lost, including after a web request ends,
account reconciliation is required; no durable recovery or automatic provider
replay is implied. Gmail/Calendar use one exact-owner credential lookup. Their
explicit legacy environment/cache compatibility applies only to inspected true
absence, not malformed credentials or uncertain vault/provider outcomes.

Vault encryption and rotation do not change Ed25519 signing derivation, signing
selectors or existing public identities. Configuration hydration applies only
registered secret settings; a vault entry cannot implicitly select a signing key.
Owned rotation/recovery controls verify historical witness and backup signatures
across legacy/current vault formats and environment/file signing custody.

See [M11 backup recovery](M11_BACKUP_RECOVERY.md) for archive, restore, retained
receipt and uploader outcome contracts. Remote receipt, intended-host custody,
off-machine restore qualification and operational deployment remain separate
requirements. M11 closes only with the frozen candidate's required Windows, WSL,
owned PostgreSQL, independent review and protected delivery evidence.
