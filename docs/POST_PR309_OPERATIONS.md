# Operational closure procedure (activation remains disabled)

This is a reviewable procedure, not evidence that an environment passed it.
The intended host and its operational receipts were not provided. Do not reuse
the old two-record activation smoke as a current count. Historical deployment
and calibration documents contain earlier suggested next actions; this closure
retains the subsequent halt and does not authorize arming or live traffic.

## Environment and topology gate

1. Identify the intended host/operator, exact deployed commit, state mount,
   process list, service account and rollback commit. Verify the code is the
   reviewed merged tree. Retain the evidence outside the container.
2. Native Windows supports **one Olympus process per state directory** under
   the shared proclock fallback. Do not run web + heartbeat, two web workers,
   or a mutating CLI alongside that process. The browser lease's native Windows
   lock does not upgrade every other store. Inspect the actual process list;
   unsupported/unknown topology fails this operational gate. Use the documented
   Linux/POSIX deployment for the split web/heartbeat topology. This procedure
   does not claim automatic detection of every independent Windows launcher.
3. Keep deployment, package publishing, collection, learned routing and autonomy
   off during closure. Record current settings without exposing secrets; check
   CALIBRATION and LEARNED_ROUTING remain disabled, no autonomy compose profile
   starts, and no live verifier/eval workflow is invoked.
4. Review named-deployment config using config.production_problems and
   `olympus deployment status`. Require a private persistent mount, correct
   service-account permissions, finite retention and budget, private signing
   material and separate entry/operator credentials. No placeholders qualify.
5. Inventory all active evidence files, quarantine files and failure logs.
   Evidence status is read-only; assess scope/evidence repair is not a routine
   preflight action. Preserve corruption for operator review.

## Durability, backup and recovery gate

Follow DEPLOYMENT_READINESS.md's challenge/verify procedure on the specifically
authorized host. It must prove both container replacement and actual host
reboot with changed runtime identities and unchanged challenge bytes on the
persistent mount. A process restart is insufficient. These lifecycle operations
are not authorized on an unidentified host by this document.

Require encrypted and signed backup delivery to an independently managed
location, a current delivery receipt, configured retention/versioning, and a
restore drill into an empty throwaway directory. Check checksums/signatures,
owner separation, evidence health and counts after restore. Do not overwrite
live state for a drill. Signing-root recovery needs independent secure custody
because the ordinary backup does not include that root.

Rollback must use a preserved compatible image/commit and an explicit state
compatibility check. Exercise it on the throwaway restoration, then verify
scope refusal, read-only evidence health and disabled collection/routing. A
rollback command without observed outcomes is not a rollback drill.

## Monitoring acceptance

| Signal | Required observed behavior |
| --- | --- |
| /readyz and deployment status | Failure removes instance from service; a successful /healthz alone never certifies readiness. |
| Storage capacity/write errors | Named operator receives actionable alert through an approved monitoring destination; failure logs remain durable. |
| Calibration integrity and drop rate | No collection yet; after separately authorized activation, corruption, mismatch and threshold violations pause collection under the trial protocol. |
| Backup age and restore failures | Expired/failed receipt alerts; remote retention monitored independently of uploader exit. |
| Evidence-store health | Unavailable findings/knowledge/cache reported without targets, secrets or corrupt fragments; no automatic repair. |
| Lock timeout/topology | Surface timeout/degraded process guarantees; do not start unsupported peers as a workaround. |

No monitoring destination was supplied, so no alerts/messages were sent and no
end-to-end alert delivery is claimed.

## Independent data gates

- **Calibration:** after C6 and a separate operator activation decision, genuine
  usage and real human feedback must reach 100/250/500 checkpoints. Preserve
  attempted-versus-persisted counts, chain verification, exclusions and bias
  checks. No synthetic or manufactured tasks count.
- **Learned routing:** independently verify at least 300 labeled real outcomes,
  three task types, two real sources and 25 eligible outcomes per specialist/model
  cell including incumbent evidence. Resolve source-attribution limitations,
  verify replay exclusions and obtain explicit opt-in. Meeting calibration
  sample counts does not meet this gate.
- **Evolution/superiority:** matched-task quality/reliability/recovery/cost/latency
  evidence is required; data collection alone establishes neither improvement
  nor advantage.

Exact current blockers: host/process inventory; mount and permission evidence;
replacement/reboot receipts; backup delivery and restore drill; approved
monitoring destination and alert test; rollback drill; current genuine datasets;
operator activation decision. Configuration and these runbooks cannot replace
any of those facts.
