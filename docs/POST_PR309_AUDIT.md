# Post-PR #309 audit reconciliation

Source boundary: PR #309 baseline plus the assessment-result changes described
in POST_PR309_CLOSURE.md. Historical findings are from
TECHNICAL_AUDIT_2026-07-29.md. This is a bounded reconciliation of that register
and the security-relevant stores below, not a new exhaustive penetration test.
Tests named below are source evidence unless covered by the recorded executed
suite. No percentage or universal isolation/security claim is made.

## Historical errors E1-E31

“Retained limit” means an existing documented product or trust boundary; it is
not permission to call unavailable evidence verified. “Open prerequisite” names
work/evidence still required; these rows are not silently counted as fixed.

| ID | Current disposition | Source / evidence / exact prerequisite |
| --- | --- | --- |
| E1 | Retained limit | orchestrator._enforce_answer_verify forces bounded rework on rejection and applies UNVERIFIED after synthesis on failure. ADR 0005 retains visible degradation, not guaranteed factual truth. test_answer_verify covers enforcement; real-model reliability requires C7/C9. |
| E2 | Retained limit | Router opt-out still exists, now ledgered through _record_verify_exempt. Do not claim universal independent verification. Interactive verification has a separate path and tests/test_interactive_verify.py. |
| E3 | Open prerequisite | modelgrade.observe has no direct production caller found in the source call-site inventory. Qualification evidence needs explicit integration and genuine observations; routing_outcomes/learned_routing are separate mechanisms. |
| E4 | Open prerequisite | config._PRICE_PER_MTOK/set_live_pricing and usage.PRICES remain distinct; current price ingestion and consistent cost semantics must be implemented/verified before precise comparative cost claims. |
| E5 | Retained opt-in | ingestgate.enabled is default-off. Validation exists (test_ingestgate/test_ingest_wiring); deployment opt-in/soak is absent and must not be activated by this closure patch. |
| E6 | Partially fixed; retained trust boundary | P2U fixed owner binding, corruption and mutation integrity. Authorization JSON still has no per-file authenticity check; a writable host is trusted. Do not equate signed approval actions with cryptographic authentication of every authorization read. |
| E7 | Retained product boundary | scaffold_evolve is explicitly propose-only, default-off (ADR 0003); no autonomous code apply is authorized. NORTH_STAR_REVIEW rejects treating this refusal as unfinished live training. |
| E8 | Retained opt-in; operational prerequisite | codegraph_cli builds/updates, codegraph_watch updates a selected project. Graph-dependent claims require an actual built/fresh graph; no universal automatic index is assumed. |
| E9 | Open evidence prerequisite | native/forecaster.py provides a checkpoint-dependent adapter; source existence does not prove deployed serving integration. Require verified configured construction/serving path and promotion receipt before claiming native serving. |
| E10 | Genuine-data gate | Historical synthetic comparison is not current market evidence. Real held-out data, matched baselines and promotion qualification are absent; no native-model quality claim. |
| E11 | Retained topology boundary | Web sessions/metrics/rate state include process-local state. Single-node operation is not horizontally scalable HA. Multi-replica enforcement/state proof remains a prerequisite. |
| E12 | Retained explicit boundary | sandbox.py documents local as NOT an OS sandbox; docker uses network-none and confined mounts. Untrusted-command deployment requires an isolated backend. One-shot confined shell remains intentional. |
| E13 | Fixed in source | whatsapp.valid_signature now returns False without WHATSAPP_APP_SECRET. tests/test_whatsapp.py includes signature boundary coverage. |
| E14 | Fixed in source | webhook_gateway.run_server defaults to loopback and refuses missing secret or server-owned user. Owner binding hardened separately; tests/test_webhook_owner_binding.py. |
| E15 | Configuration hardened; C6 blocked | config._deployment_problems requires explicit writable state; deployreadiness verifies mount/lifecycle/backup receipts. Actual host durability remains unverified. |
| E16 | Retained evidence distinction | actions._audit still writes auxiliary audit.jsonl. It is not an immutable signed ledger. Signed decision-log/approval provenance is distinct; a requirement to authenticate this auxiliary log would need a scoped change. |
| E17 | Open prerequisite | vault key derivation still SHA-256s a supplied passphrase. Strong randomly generated keys and host access controls remain essential; password-hardening/KDF migration is not implemented by these assessment fixes. |
| E18 | Retained custody boundary | mandate uses separate subkeys under the same root by default. Independent human custody requires independently managed keys, not merely two signatures. |
| E19 | Open low-severity defect | usermem render-card age still reads created while records contain created_at; precise age-display repair remains required. |
| E20 | Partially fixed by subsystem | New result evidence no longer swallows failures. Broad best-effort catches elsewhere require consumer-specific review; this historical count is not a current defect count. |
| E21 | Fixed in source | config.resolve_daily_budget defaults to a finite ceiling, malformed input fails safe; test_budget_default_and_production_boot. Zero still means explicit unlimited, not disabled. |
| E22 | Fixed in source; C6 blocked | production_problems/require_production_config exist and share named-deployment checks. Deployment proof is still required separately. |
| E23 | Open prerequisite | No live Postgres store-contract validation was supplied or executed. Mock configuration does not qualify the optional shared backend or backups. |
| E24 | Open prerequisite | outcomes.record still swallows write failures; _load also maps malformed JSON to an empty list. This separate normalized store is not fixed by assessment knowledge validation. |
| E25 | Fixed in source | replaygate/reliability gate distinguishes unavailable/no-recorded-model evidence from a genuine regression; relevant release/replay tests exist. No live reliability fan-out executed here. |
| E26 | Fixed in source | pyproject package-data closes the signed/shipped surface; test_package_data and release pipeline tests. No package was published in this closure. |
| E27 | Fixed in source | Anonymous free-chat quota has an independent IP ceiling; tests/test_quota_integrity.py. Does not imply cross-replica safety. |
| E28 | Fixed in source | Web v1 rate-limit path exists and is separately bounded; tests cover endpoint quota/rate behavior. Runtime proxy identity still requires deployment verification. |
| E29 | Fixed in source | Quota validation ordering covered by test_quota_integrity; no production quota experiment performed. |
| E30 | Fixed in source | Atomic daily allowance consumption and race tests address single-instance check/consume races. Distributed limit enforcement remains E11. |
| E31 | Open prerequisite | /healthz still calls metrics.snapshot, which consults usage. A cheap liveness accessor is still needed to remove disk I/O from this route. |

No historical critical/high item is unclassified. Several remain open;
assessment store completion does not close those separate prerequisites.

## Security-relevant store and consumer inventory

| Store / entry points | Owner and missing/corrupt behavior | Mutation and consumer disposition |
| --- | --- | --- |
| Assessment authorization; scope/grant/revoke/require_scope | Exact owner since P2U; absent=no grants, unavailable=refusal | Owner lock + atomic publication; operator preservation/repair; target I/O gated. |
| Assessment findings; scans/record/import/report/export/fix/MCP | Exact owner; absent=empty, corrupt=explicit unavailable | Full transaction lock; strict schema and byte bounds; export refuses before replacing destination. |
| Assessment knowledge; native findings/Aegis/CLI | Exact owner, including current_owner prompt consumer; invalid counters/provenance refused | Shared owner lock; known damage preflight; surfaced partial publication; prompt marks unavailable. |
| Advisory cache; dep_audit/tools/run/selfassess | Exact owner; missing/stale/current/unavailable distinguished | Fetch outside lock; reload+merge inside lock; bounded optional failure and explicit coverage. |
| Actions and mandates | actions owner_key layout; mandate_store storage_key with strict evidence errors | Existing approval/payload/nonce isolation boundaries retained; see owner and mandate evidence tests. |
| Vault/preferences/companion | Exact-owner layouts and explicit legacy non-attribution | Prior fail-closed evidence/repair paths retained; preferences/companion tests rerun in closure suite. |
| Scheduler/goals/webmonitor/gateway/browser sessions | Prior owner-bound entry-point hardening | Existing owner-exact runtime/store tests; browser lease has separate process locking. No new runtime topology enabled. |
| documents/docrag/todos/playbooks/emailstyle/discovery | Current source still uses safe_id | **Residual owner-collision boundary**; do not use these stores as exact-owner authorization or claim global tenant isolation. Requires owner-key migration, legacy non-attribution and consumer tests. |
| Conversation search index | THREAT_MODEL documents normalized residual namespace | Exact-owner migration and verification remain prerequisites; do not infer it was fixed by assessment directory changes. |
| outcomes/routing_outcomes | safe_id backend keys; outcomes damage fallback remains; routing rows have validation but normalized attribution | Distinct from new assessment knowledge. Routing is disabled; exact source attribution and error semantics require separate review before stronger multi-owner evidence claims. |
| Calibration record/failure log | Existing chain/reporting and replay/synthetic exclusions | Reporting tests are not genuine observations; C6/C7 govern activation and current evidence. |
| Deployment receipts | Named deployment/host identities, not tenant authorization | Missing/invalid receipts keep readiness unsatisfied; host administrator is inside the trust boundary. |

ROADMAP sequencing and MOAT_ANALYSIS require measurable accumulation, not merely
more static features. NORTH_STAR_REVIEW's cuts remain: no live weight-training,
distributed-consensus claims from a tally function, live payment rail, or
unapproved autonomous code application. Athena's quality nudge is intentionally
bounded and non-enforcing. These are not revived as missing assessment fixes.
