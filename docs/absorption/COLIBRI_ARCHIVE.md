# Colibri Absorption Programme — Archive Index

> ## Colibri is no longer the active architectural roadmap for Olympus.
>
> This programme is **CLOSED**. The material below is preserved as the permanent
> historical record. It may be consulted only for:
>
> - historical research,
> - regression comparison,
> - benchmark comparison,
> - analysis of a **specifically proposed** capability.
>
> **Future teams must not reopen broad Colibri absorption without a new approved
> programme charter.** A proposal to revisit any single capability must stand on
> its own evidence — user value, security, reliability, cost, maintainability,
> scalability or developer experience — never on Colibri parity.

---

## Programme purpose

Absorb every *meaningful* capability, principle and engineering technique from
[Colibri](https://github.com/JustVugg/colibri) into Olympus **without copying
it** — treating Colibri as research input rather than as a design to reproduce.
Colibri is a pure-C single-file inference engine whose scarce resource is disk
bandwidth; Olympus is a Python multi-agent council whose scarce resource is
token spend. The programme's job was to find which ideas survive that
translation, redesign them from Olympus's own constraints, and reject the ones
that do not.

## Closure status

| | |
|---|---|
| **Status** | **CLOSED** |
| **Final verdict** | **COLIBRI ABSORPTION COMPLETE** |
| **Closed on** | 2026-07-26 |
| **Final report** | [`COLIBRI_ABSORPTION_FINAL_REPORT.md`](COLIBRI_ABSORPTION_FINAL_REPORT.md) |
| **Machine-readable marker** | [`archive-status.json`](archive-status.json) |
| **Runtime dependency on Colibri** | **No** — verified by AST scan (0 imports, 0 identifiers, 0 code tokens) |
| **Build dependency** | **No** |
| **Roadmap dependency** | **No** |
| **Remaining role** | historical reference; optional benchmark |
| **Capabilities dispositioned** | 26 |
| **New Olympus modules** | 13 (of a hard cap of 14; 1 reserved) |
| **Deferred on measured evidence** | 4 — designs and gates complete, floors never lowered |

**Important boundary:** completion of this programme does **not** equal
production approval for Olympus. Production readiness is a separate question,
currently **CONDITIONAL GO FOR CONTINUED STAGING**. See the final report §14.

---

## Index

Authoritative status legend:
**AUTHORITATIVE** — current truth · **HISTORICAL** — true when written, later
corrected or superseded on specific points (corrections listed in the final
report §11) · **SUPERSEDED** — replaced in full.

### Final closure

| File | Phase | Authority | Description |
|---|---|---|---|
| [`COLIBRI_ABSORPTION_FINAL_REPORT.md`](COLIBRI_ABSORPTION_FINAL_REPORT.md) | Closure | **AUTHORITATIVE** | The definitive programme record: capability matrix, corrections, security outcome, dependency argument, verdict. Supersedes every other document on any point of conflict. |
| [`COLIBRI_ARCHIVE.md`](COLIBRI_ARCHIVE.md) | Closure | **AUTHORITATIVE** | This index. |
| [`archive-status.json`](archive-status.json) | Closure | **AUTHORITATIVE** | Machine-readable closure marker, consistency-validated by `tests/test_archive_consistency.py`. |

### Analysis and synthesis (Stage 0)

| File | Phase | Authority | Description |
|---|---|---|---|
| [`../colibri-deep-analysis.md`](../colibri-deep-analysis.md) | Analysis | HISTORICAL | Full reverse-engineered Colibri inventory, 28 sections. The programme's input. |
| [`00-SYNTHESIS.md`](00-SYNTHESIS.md) | Synthesis | **AUTHORITATIVE** (design rulings) | Master synthesis: rulings R1–R11, budgets B1–B4 (incl. the 14-module cap). Governed every later wave. |
| [`01-execution-tier.md`](01-execution-tier.md) | Analysis | HISTORICAL | Model execution tier: quantization, backends, determinism. |
| [`02-memory-hierarchy.md`](02-memory-hierarchy.md) | Analysis | HISTORICAL | 5-level expert store, LFRU, PIN/AUTOPIN, RAM budgeting. |
| [`03-speculation.md`](03-speculation.md) | Analysis | HISTORICAL | Lossless speculative decoding, Leviathan rejection sampling. |
| [`04-routing.md`](04-routing.md) | Analysis | HISTORICAL | MoE routing, `CACHE_ROUTE`, expert top-p. |
| [`05-state-persistence.md`](05-state-persistence.md) | Analysis | HISTORICAL | Compressed MLA KV cache, crash-safe `.coli_kv`. |
| [`06-gateway-api.md`](06-gateway-api.md) | Analysis | HISTORICAL | Dual OpenAI/Anthropic gateway, admission scheduler. |
| [`07-io-concurrency.md`](07-io-concurrency.md) | Analysis | HISTORICAL | PIPE worker pool, io_uring, batch-union dedup. |
| [`08-prefetch.md`](08-prefetch.md) | Analysis | HISTORICAL | The measured prefetch ladder: `SPEC`, `PILOT`, `PILOT_TWO`. |
| [`09-observability.md`](09-observability.md) | Analysis | HISTORICAL | `PROF` verdicts, byte-identical-when-off instrumentation. |
| [`10-security-integrity.md`](10-security-integrity.md) | Analysis | HISTORICAL | Untrusted-model-mirror threat model, reject-never-repair. |
| [`11-ops-reliability.md`](11-ops-reliability.md) | Analysis | HISTORICAL | `resource_plan.py` auto-tune, `coli doctor` check matrix. |
| [`12-engineering-culture.md`](12-engineering-culture.md) | Analysis | HISTORICAL | Measurement-justified comments, negative results as opt-ins. |
| [`13-review-gaps.md`](13-review-gaps.md) | Analysis | HISTORICAL | Seven gaps (G1–G7) an adversarial re-read of the inventory found. |

### Wave 1 — the measurement substrate

| File | Phase | Authority | Description |
|---|---|---|---|
| [`WAVE1_IMPLEMENTATION_SPEC.md`](WAVE1_IMPLEMENTATION_SPEC.md) | Wave 1 | HISTORICAL | Binding spec: 8 capabilities, 17 points each, acceptance matrix. |
| [`WAVE1_COMPLETION_REPORT.md`](WAVE1_COMPLETION_REPORT.md) | Wave 1 | HISTORICAL | Completion record. **Correction:** its "A6 flag-independent" claim was later disproved (final report §11.2). |
| [`WAVE1_INDEPENDENT_AUDIT.md`](WAVE1_INDEPENDENT_AUDIT.md) | Wave 1 | **AUTHORITATIVE** (audit findings) | Four independent auditors forbidden from patching source; +151 adversarial tests; 2 blockers + 1 false claim, all resolved. |

### Wave 2 — the policy layer

| File | Phase | Authority | Description |
|---|---|---|---|
| [`WAVE2_IMPLEMENTATION_SPEC.md`](WAVE2_IMPLEMENTATION_SPEC.md) | Wave 2 | HISTORICAL | Binding spec: 10 capabilities, 17 acceptance gates. |
| [`WAVE2_COMPLETION_REPORT.md`](WAVE2_COMPLETION_REPORT.md) | Wave 2 | HISTORICAL | Completion record. Retains its **superseded first verdict** (NOT COMPLETE — built but unwired) as the record; revised to COMPLETE with one named gap (A3) after the integration wave. |

**No separate Wave-2 independent audit exists.** That role was served by the
Phase-4 validator suites, which covered Waves 1–2 together. Recorded as a real
deviation from the Wave-1 pattern in the final report §3.

### Wave 3 — evidence-gated candidates

| File | Phase | Authority | Description |
|---|---|---|---|
| [`WAVE3_EVIDENCE_REVIEW.md`](WAVE3_EVIDENCE_REVIEW.md) | Wave 3 | HISTORICAL | The Phase-3 gate: floors measured by running the code. 4 NO-GO, 1 CONDITIONAL GO. **Correction:** its published reproduction command for `modelgrade` no longer measures what it claims (final report §11.5). |
| [`WAVE3_IMPLEMENTATION_SPEC.md`](WAVE3_IMPLEMENTATION_SPEC.md) | Wave 3 | HISTORICAL | Spec for the one candidate that passed its gate. |
| [`WAVE3_COMPLETION_REPORT.md`](WAVE3_COMPLETION_REPORT.md) | Wave 3 | HISTORICAL | 1 of 5 implemented, 4 deferred untouched. No floor lowered. |
| [`WAVE3_REVIEW_AFTER_SHADOW.md`](WAVE3_REVIEW_AFTER_SHADOW.md) | Phase 5 | **AUTHORITATIVE** (gate re-run) | Re-run after Phase 5: still 4 NO-GO, floors unmodified. Records finding W3R-1. |

### Phase 4 — offline validation

| File | Phase | Authority | Description |
|---|---|---|---|
| [`PRODUCTION_READINESS_REPORT.md`](PRODUCTION_READINESS_REPORT.md) | Phase 4 | HISTORICAL | Verdict CONDITIONAL GO. 12 defects (2 HIGH) found and dispositioned. **Correction:** recorded G2 real-client verification as blocked; Phase 5 executed it (final report §11.4). |
| [`PRIVACY_RETENTION_REVIEW.md`](PRIVACY_RETENTION_REVIEW.md) | Phase 4 (Stage E) | HISTORICAL | Data inventory and the thirteen required determinations. Identified the unbounded conversation-retention gap. |

### Phase 5 — staging and shadow foundations

| File | Phase | Authority | Description |
|---|---|---|---|
| [`PHASE5_STAGING_SHADOW_SPEC.md`](PHASE5_STAGING_SHADOW_SPEC.md) | Phase 5 | HISTORICAL | Binding spec, 32 sections, including the environment's exact limits stated *before* the work. |
| [`PHASE5_STAGING_REPORT.md`](PHASE5_STAGING_REPORT.md) | Phase 5 | **AUTHORITATIVE** | Staging profile: authored and validated, **never deployed**. |
| [`PHASE5_SHADOW_EVIDENCE_REPORT.md`](PHASE5_SHADOW_EVIDENCE_REPORT.md) | Phase 5 | **AUTHORITATIVE** | Shadow substrate complete and proven; **no operational evidence collected**. |
| [`PHASE5_CLIENT_COMPATIBILITY_REPORT.md`](PHASE5_CLIENT_COMPATIBILITY_REPORT.md) | Phase 5 | **AUTHORITATIVE** | 25/25 with real SDKs over real HTTP. Claim ceiling: real-SDK-over-HTTP verified in staging. |
| [`PHASE5_PROVIDER_QUALIFICATION_REPORT.md`](PHASE5_PROVIDER_QUALIFICATION_REPORT.md) | Phase 5 | **AUTHORITATIVE** | Campaign **NOT EXECUTED** — no credentials. Zero cards written, deliberately. |
| [`PHASE5_BACKUP_RECOVERY_REPORT.md`](PHASE5_BACKUP_RECOVERY_REPORT.md) | Phase 5 | **AUTHORITATIVE** | 18 drills; restore verified by reading data back into a clean tree. |
| [`PHASE5_RETENTION_DELETION_REPORT.md`](PHASE5_RETENTION_DELETION_REPORT.md) | Phase 5 | **AUTHORITATIVE** | Mechanism complete, **policy deliberately unset**; legacy `api-v1` procedure. |
| [`PHASE5_COMPLETION_REPORT.md`](PHASE5_COMPLETION_REPORT.md) | Phase 5 | **AUTHORITATIVE** (readiness) | Verdict CONDITIONAL GO FOR CONTINUED STAGING; 24/25 acceptance gates. |

### Registries (live, outside this archive)

| File | Authority | Description |
|---|---|---|
| `olympus/experiments.json` | **LIVE** | Quarantine/experiment registry: 21 entries (8 accepted_debt, 8 proposed, 5 active). CI-enforced. All four deferred Wave-3 candidates live here. |
| `olympus/capabilities.json` | **LIVE** | Capability manifest; drift-gated against README and code. |

### Validation harnesses (live, outside this archive)

| File | Description |
|---|---|
| `scripts/wave3_gate_rerun.py` | Re-runs the four deferred gates; floors are constants with no mechanism to lower them. |
| `scripts/client_compat_campaign.py` | Real-SDK-over-HTTP compatibility campaign. |
| `scripts/perf_validation.py` | Stage-D performance harness. |
| `scripts/noninterference_gate.py` | Proves observe-plugins cannot alter a run. |
| `scripts/check_threat_model.py` | Threat-model coverage over all 130 tools. |
| `tests/test_val_{integration,security,reliability,performance}.py` | Phase-4 validator suites (238 tests). |
| `tests/test_phase5_{staging,shadow,client_compat,retention,recovery}.py` | Phase-5 suites (139 tests). |
| `tests/test_archive_consistency.py` | Guards this archive against contradictory closure states. |

### Migration and operator procedures

| Procedure | Where |
|---|---|
| Legacy `api-v1` namespace (inspect / export / quarantine / delete / adopt) | `olympus/retention.py`; CLI `olympus retention legacy-*`; final report §12 |
| Conversation-retention policy | `OLYMPUS_CONVERSATION_RETAIN_DAYS`; CLI `olympus retention status` |
| Backup and restore | `olympus/backup.py`; CLI `olympus backup` / `olympus restore`; `docs/BACKUPS.md` |
| Staging deployment | `deploy/docker-compose.staging.yml`, `deploy/.env.staging.example` |

---

## Where new work goes

**Olympus-native work belongs under `docs/native/`, not here.** This directory
is closed to new programmes.

The active roadmap is the Olympus Native Evolution programme:
[`../native/README.md`](../native/README.md).
