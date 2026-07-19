> **⚠️ SUPERSEDED by [`TRUTH_STATE_2026-07-19.md`](TRUTH_STATE_2026-07-19.md).**
> This audit predates the M0–M4 milestone merges and ADR 0005. Its "Milestone-0
> candidates" (M0-2 sanitize-at-sink, M0-3 structural envelope, M0-4 single prompt
> writer) and its "Aletheia is soft / no checkpoint-resume" findings are all now
> resolved in `main`. Kept for history; read the 07-19 doc for current state.

# Olympus — Truth-State Audit · 2026-07-15

Read-only diagnostic. Evidence hierarchy: **executed command output > code read > docs**.
`README` / `THREAT_MODEL` / `capabilities.json` are treated as *claims to verify*, never evidence.
Audited commit: `feat/browser-self-evolution @ 62d9fb8` (the tip merged via PR #123), clean tree, Python 3.11.15.

---

## Executive summary (10 lines)

1. **Suite is green and deterministic:** `1825 passed, 20 skipped in 182.85s` — reproduced identically on two runs.
2. **Counts match claims exactly:** 13 specialists, 101 tools, 101 commands, 16 actions; `capabilities.check_repo()` → `[]` and threat-model gate covers all 101 tools.
3. **Replay determinism is REAL** (Plane 2 GO): `request_hash` byte-identical + order-independent; 30 replay tests pass incl. byte-identical zero-API replay and mutated-prompt→divergence.
4. **Action spine deny-by-default is REAL and tested:** IRREVERSIBLE/FINANCIAL never auto (min level 99); scope gate blocks; 20/20 `test_actions.py` pass.
5. **Ed25519 sign/verify works live** (round-trip True, tamper False) — but the instance runs on the **DEFAULT seed** (`posture: dev`, `is_default_seed: True`): signatures are forgeable by anyone holding the public default seed until a secret seed is provisioned.
6. **Prompt/skill/memory surfaces have MANY writers** (Plane 3 NOT single-writer): `update_prompt` writes prompt files ungated beside the gated `gate_prompt`; 26 `memory.save` sites; sanitization is caller-applied, not enforced in `memory.save`.
7. **Untrusted-data envelope** is membership-gated (`INGESTION_TOOLS`) and caller-applied — learned web content *is* wrapped (`learn.py:57`), but a new ingesting tool omitted from the set would bypass.
8. **Aletheia is a soft verify/correct stage, not a hard block** — it produces a corrected, confidence-annotated version; there is no block semantics to test.
9. **No mid-run checkpoint/resume exists** — durability is heartbeat/goals JSON only.
10. **Secrets hygiene clean:** no key material or `.env` committed; only `.env.example` + the public `witness_pubkey.txt`. **Dependency-minimal claim: PARTIAL** — 3 hard deps declared, 3 undeclared soft deps (`psycopg`/`websockets`/`yaml`) are import-guarded.

**Milestone-0 blockers before plane work:** none are *red tests* — all are gaps between the claim and enforced-by-construction. See Section D.

---

## SECTION A — Ground truth inventory

### Runtime & dependencies
- **Python (local):** `Python 3.11.15` (`python --version`). **CI pins `3.12`** (`.github/workflows/ci.yml:14,37,48`) — version drift, see Section B.
- **Declared deps** (`pyproject.toml:31-35`): `anthropic>=0.92.0`, `youtube-transcript-api>=1.0.0`, `cryptography>=41.0.0`. Same three in `requirements.txt`.
- **Actual third-party imports** (AST scan of `olympus/`): the 3 declared **plus 3 undeclared soft deps** — `psycopg` (`store.py:64`, *"imported lazily so it's a soft dependency"*), `websockets` (`browser.py:405`, `# pragma: no cover - optional dependency`), `yaml` (`sandbox.py`, try-guarded). All three are import-guarded optional backends.
- **Verdict on "stdlib-minimal":** **PARTIAL** — the *required* surface is genuinely small (3 deps), but the claim is not literal: three optional third-party libraries are imported when their backends are used.

### Size
- **38,041 LOC across 126 modules** (`wc -l olympus/*.py`). Largest: `tools.py` 3339, `browser.py` 2380, `web.py` 2304, `cli.py` 1863, `orchestrator.py` 1496.

### Real counts vs claimed (counting command shown)
```
$ python -c "from olympus import tools,cli; from olympus.specialists import SPECIALISTS; \
             from olympus import actions,builtin_actions,capabilities; builtin_actions.register_builtins(); \
             print(len(SPECIALISTS),len(tools.HANDLERS),len(cli.command_names()),len(actions.registered()),capabilities.check_repo())"
specialists: 13   tools: 101   commands: 101   actions: 16   check_repo(): []
```
| Surface | Claimed (capabilities.json) | Real (code) | Verdict |
|---|---|---|---|
| specialists | 13 | 13 | PASS |
| tools | 101 | 101 | PASS |
| commands | 101 | 101 | PASS |
| actions | 16 | 16 | PASS |

### Census
- **TODO/FIXME/XXX/HACK:** 8 matches, all benign (doc/comment references + the `LIST_TODOS`/`ADD_TODO` feature names). No abandoned-work markers.
- **Stub functions (body only `pass`/`...`):** 9, all intentional no-ops — silenced HTTP-server `log_message` overrides (`web.py:2292`, `discord.py:212`, `slack.py`, `whatsapp.py`, `webhook_gateway.py`), the abstract `Transport.send/close` (`browser.py:188/190`), and `_silent` stream callbacks. **No real stubs.**
- **Dead modules (never imported):** only `__main__` (an entry point, not dead). **No dead modules.**
- **Parallel implementations:** prompt-mutation exists via two paths (`update_prompt` ungated vs `gate_prompt` gated) — see item 10 / Plane 3; not duplicate code, but duplicate *authority*.

---

## SECTION B — Baseline health

- **Full suite:** `1825 passed, 20 skipped in 182.85s (0:03:02)` — `python -m pytest -q`. Reproduced identically on a second run.
- **CI gates replicated locally (all PASS):**
  - `python -m olympus capabilities --check` → *"Capabilities consistent: the manifest and README match the code."*
  - `python scripts/check_threat_model.py` → *"✓ threat model covers all 101 exposed tools."*
  - `python scripts/check_no_prerelease.py requirements.lock` → *"✓ requirements.lock: no pre-release dependencies."*
  - `python -m compileall -q olympus` → clean.
- **CI drift:** CI runs on **Python 3.12** and installs from `requirements.lock` with `--require-hashes`; this audit ran on **3.11.15** without the lock. A 3.12-only failure or a lock/hash mismatch would **not** surface locally. `pytest -q` invocation itself matches CI (`ci.yml:26`).
- **Skipped (20) — reasons (all environment/opt-in, none network-flaky by default):** `cryptography backend unavailable` ×2, `POSIX file modes only` / `symlink semantics: POSIX only` ×2, `real-browser smoke test (OLYMPUS_BROWSER_REAL/SMOKE)` ×2, `real transport needs websockets` ×1, plus related.
- **Tests requiring network / mutating state:** the real-Chrome smoke tests (`test_browser_smoke.py`, gated behind `OLYMPUS_BROWSER_SMOKE`/`_REAL`) are the only network/real-actuator tests, and they are **opt-in skipped** by default. No default-path test hits the live network.

---

## SECTION C — Assumption verification

| # | Claim | How verified | Verdict | Quoted evidence |
|---|---|---|---|---|
| 1 | `witness.py` Ed25519 sign+verify round trip; what is signed | **Executed** live probe | **PASS (mechanism) / CAVEAT (trust root)** | `available(): True`, `sign->verify: True`, `verify tampered: False`, `is_default_seed(): True | posture: dev`. Signs: manifest (`write_manifest`), decision log (`trace.py:162 sign_log`), backups (`backup.py:205`), attestations (`attest.py:66`). **Caveat: default seed → forgeable until a secret seed is set.** |
| 2 | `attest.py` attestations produced & verified | **Executed** live probe | **PASS (same seed caveat)** | `attest("captcha",...)` → `verify_attestation: True`; after tampering `domain` → `False`. Produced by `_browser_attest_human` tool; verified by `verify_attestation`/`verify_receipt`. |
| 3 | Approval gate deny-by-default; risk classes; blocked-action test | Code read + **executed tests** | **PASS** | Risk classes `actions.py:34-38`; IRREVERSIBLE/FINANCIAL `_min_level_to_auto=99` (`:74-75`); `blocked_no_scope` (`:366`). `test_actions.py` 20/20 PASSED incl. `test_scope_gate_blocks_execution`, `test_financial_action_never_auto_executes`, `test_irreversible_action_never_auto_executes`. |
| 4 | Autonomy dial L0–L4 enforced | Code read + tests | **PASS** | `L0..L4` `actions.py:41-46`; `autonomy_level` returns `min(level, capprofile.autonomy_cap(user))` (`:57`); `can_auto_execute` gates (`:352`). `test_trivial_action_auto_executes_only_at_l3` PASSED. **Enforced, not aspirational.** |
| 5 | Aletheia invoked where; block test | Code read | **PARTIAL** | Stage-3 `_verify` on the orchestrated answer path (`orchestrator.py:311-340`); it produces *"the corrected, confidence-annotated version of the content"* and caches facts/lessons. **It corrects/annotates — it does not hard-block.** No block semantics; a "block" test does not exist because the design flags, not blocks. |
| 6 | Untrusted-data envelope applied where; bypass? | Code read | **PASS (convention)** | `INGESTION_TOOLS` set (`security.py:76`); `should_wrap`/`wrap_untrusted` applied at `agent.py:172-173`, `openai_compat.py:226`, and — importantly — learned web content at `learn.py:57`. **Bypass risk:** wrapping is membership-gated + caller-applied; a new ingesting tool not added to `INGESTION_TOOLS` would not be wrapped. |
| 7 | Replay hash byte-identical NOW | **Executed** (live + tests) | **PASS** | Live: `request_hash` identical across calls incl. reordered keys → `0bb5294f…a7a6f2cc` both times. `test_replay_makes_zero_api_calls_and_is_byte_identical` PASSED; `test_mutated_prompt_raises_replay_divergence` PASSED; 30/30 replay tests pass. |
| 8 | Mid-run checkpoint/resume | Code read | **ABSENT (as claimed elsewhere)** | Only `heartbeat.py:229 memory.save_state`, and `goals.py` resume *at process exit*. No per-step checkpoint of an in-flight run. |
| 9 | `evals.py` before/after + auto-rollback proven by test | Code read + **executed tests** | **PASS** | `evals.py` judge/`run`; rollback in the gated path. `test_gate_prompt_reverts_change_on_regression` PASSED, `test_gate_prompt_keeps_change_on_non_regression` PASSED, `test_gate_reverts_when_score_regresses` PASSED, `test_tie_is_reverted_not_promoted` PASSED. |
| 10 | Enumerate every prompt/skill writer; gating/signing/rollback | Code read | **MULTIPLE WRITERS** | **Prompts:** `_update_prompt` writes the `.md` directly (`tools.py:697`, **ungated by benchmark**, reversible via `restore_prompt`); `_gate_prompt` (`:1062`, benchmark-gated + rollback); `_restore_prompt` (`:701`). **Skills:** `create_skill`, `gate_skills`, `browser_learn`, `browser_skill_record`. **Signing:** prompt files are hashed by the release manifest (release-time), **not signed at runtime write.** |
| 11 | Enumerate memory writers; provenance/trust labels | Code read | **MANY WRITERS; provenance PARTIAL** | Writers: `memory.save` (**26 call sites**), `relgraph.add_node/add_edge`, `facts.record`, docrag index. **Sanitization is caller-applied** (`sanitize_for_memory` at 14 sites incl. `save_lesson` `tools.py:1741`, `learn.py:146`, `operator.py:439`) — **not enforced inside `memory.save`.** Provenance: `relgraph` carries `confidence`+`src_kind`, `facts` carries `source`; **no uniform trust label.** |
| 12 | Enumerate hard-stop rules (Plane-1 checklist) | Code read | **PASS — enumerated below** | See the Plane-1 checklist. `cmdguard` DENY set + SSRF/egress + capprofile deny-sets + egress class matrix, all with file:line. |
| 13 | Browser harness bounds | Code read | **PASS (double-bounded)** | `_operator_authorized_session` requires operator enabled **and** `operator.authorized(user, host)` (`tools.py:1166-1182`), **plus** SSRF/egress `url_block_reason` on every navigation/redirect (`tools.py:521,598,841`). Allowlist AND gate. |
| 14 | MCP client + server; auth | Code read | **PARTIAL** | **Server** (`mcp_server.py`): **read-only**, scoped to `OLYMPUS_MCP_USER`, **stdio-local, no token/bearer auth** — relies on process locality + read-only tools. **Client:** MCP tool execution runs **server-side on the Anthropic backend only** (portability caveat — absent on OpenAI-compatible providers). |
| 15 | Secrets hygiene | **Executed** scan | **PASS** | No `sk-ant-…`/`xoxb-`/`ghp_`/PEM material in tracked files; no committed `.env` (only `.env.example` templates); `witness_pubkey.txt` is public (safe); no private seed file tracked (the "seed" hit is the *filename* `tests/test_seed_custody.py`). Operational note: instance uses the **default seed** at runtime (item 1). |

### Plane-1 hard-stop checklist (item 12, enumerated with file:line)
**cmdguard** (`olympus/cmdguard.py`, fail-closed `enforce` default, refuses even if approved):
- fork bomb `:81`; self-piping fork bomb `:84`; `mkfs`/repartition `:88`; `dd` to block device `:91`; redirect-to-device `:94`; `--no-preserve-root` `:97`; poweroff/reboot `:101`; overwrite critical `/etc` file `:104`; `chmod 000 /` `:106`; tokenized recursive `rm` of system/home paths `:186-196`. Levels: `SAFE/CONFIRM/DENY`; `paranoid` mode also blocks `CONFIRM`.

**SSRF / egress** (`olympus/security.py`):
- `_host_is_loopback` `:301`; `host_on_allowlist` `:338`; `egress_allowed` `:354`; metadata/internal hostnames blocked by name `:377` (`localhost`, `metadata`, `metadata.google.internal`, …); `url_block_reason` is the per-URL/redirect gate used by the browser + fetch tools.

**Capability profiles** (`olympus/capprofile.py`):
- `_READER_DENY = ACTION_TOOLS ∪ {...}` `:28`; `_GUEST_DENY ⊃ _READER_DENY` `:37`; autonomy caps `full=4 / reader=1 / guest=0` `:45-47`.

**Egress data-class matrix** (`olympus/egress.py`):
- `Verdict.{ALLOW,REDACT,HOLD}` `:42-45`; policy matrix caps the max data class each channel may emit inline; over-cap → `HOLD` → prepared action.

---

## SECTION D — Go / No-Go per plane

### Plane 1 — invariants existing only as scattered, untested code = blockers
**Mostly GO.** The core invariants are centralized *and* tested: action spine (`test_actions.py` 20/20), replay (30 tests), egress (`test_egress.py`), cmdguard, capprofile. **Residual blockers are "invariant depends on every caller behaving," not untested code:**
- **Trust root is dev-seed** (item 1): "signed = tamper-evident against an outsider" holds only once a secret seed replaces the default.
- **Sanitize-at-write** (item 11) and **untrusted-wrap** (item 6) are caller conventions, not enforced at the sink — a new writer/ingesting tool can silently bypass.

### Plane 2 — is replay determinism real? What breaks it?
**GO — real.** Byte-identical replay proven live and by test; nondeterministic tools are frozen (`test_replay_tools.py`), parallel decisions are canonicalized (`test_parallel_decisions_canonicalized_for_stable_replay`), and a mutated prompt raises `ReplayDivergence` rather than silently diverging. What *would* break it: an un-frozen nondeterministic tool, or a new provider path that bypasses `replaystore` — both currently guarded by tests.

### Plane 3 — how many prompt writers; what must be absorbed/retired for single-writer
**NOT single-writer today.** To converge:
- **Prompts:** absorb/retire `_update_prompt` (ungated direct write) into the gated `gate_prompt` path so every prompt change is benchmark-gated; keep `restore_prompt` as the rollback leg.
- **Skills:** route `create_skill` / `browser_learn` / `browser_skill_record` mutations through `gate_skills`.
- **Memory:** centralize `sanitize_for_memory` inside `memory.save` (26 sites) so sanitization can't be forgotten by a caller.

### MILESTONE-0 candidates (LIST ONLY — not fixed)
- **M0-1 — Provision a secret witness seed.** Runtime is `posture: dev`, `is_default_seed: True`; all signing/attestation is forgeable until a non-default seed is set. *(Trust-root gap, not a red test.)*
- **M0-2 — Centralize memory sanitization.** `sanitize_for_memory` is applied at 14 caller sites but not inside `memory.save` (26 write sites); an unsanitized writer can poison durable memory.
- **M0-3 — Make untrusted-wrapping structural.** Envelope is membership-gated (`INGESTION_TOOLS`) + caller-applied; a new ingesting tool omitted from the set bypasses `wrap_untrusted`.
- **M0-4 — Reconcile the two prompt writers.** `update_prompt` writes prompt files without benchmark gating alongside the gated `gate_prompt` (Plane-3 single-writer prerequisite).
- **M0-5 — Close CI/local Python drift.** CI is 3.12 + hash-locked; this audit ran on 3.11.15 unlocked — a 3.12-only or lock/hash failure would not surface locally.

**No red tests. Replay intact. No unsigned-surface blocker beyond the dev-seed posture.**

---
*Generated read-only; one file written (`docs/TRUTH_STATE_2026-07-15.md`), uncommitted. Every verdict above quotes executed output or `file:line`; nothing inferred as PASS.*
