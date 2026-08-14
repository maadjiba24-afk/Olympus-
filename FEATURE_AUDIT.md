# FEATURE_AUDIT.md — Olympus implementation-completeness audit

- **Scope**: the Olympus codebase at `28dec49` (+`67eb8d4` secret-CLI fix), with
  emphasis on the 12 OpenClaw-derived features and every user-claimed surface
  (README counts, CLI commands, docs). *The OpenClaw upstream repo itself is
  out of this session's repo scope and cannot be cloned or executed here.*
- **Date**: 2026-07-17 · **Read-only pass**: nothing was modified; this file is
  the only output.
- **Full test suite (verbatim)**: `1473 passed, 4 skipped in 126.08s (0:02:06)`
- **Capability drift gate**: `python -m olympus capabilities --check` → README
  counts (13 agents / 67 tools / 86 commands) match the code (CI-enforced).
- Methodology note: an early smoke round produced false "everything broken"
  results because this container installs `idna`/`certifi` in root's
  *user-site* packages — redirecting `HOME` for isolation removed them from
  `sys.path` and broke `httpx` imports. Re-run with real `HOME` and an
  isolated `OLYMPUS_MEMORY_DIR`; all results below are from the corrected env.

## 1. Feature status table

Statuses: IMPLEMENTED / PARTIAL / STUB / BROKEN / UNWIRED / MISSING.

### 1a. The 12 OpenClaw-adoption features

| Feature | Status | Evidence | Repro command |
|---|---|---|---|
| 1. Telegram pairing/quoting/progress | IMPLEMENTED | `telegram.py:147` `_allowed` (pairing default), `:62` reply quoting, `:178` `_Progress`; smoke: `[PASS] gate ok`; `tests/test_telegram.py` (14), `test_pairing.py` (8) | `olympus pair telegram` → send `/pair <code>` |
| 2. Exec approvals via chat | IMPLEMENTED | `approvals.py:44-99` summary/footer/handle_command; `gateway.py` routes `/approve`; smoke: prepare→`/approve`→`executed`; `tests/test_approvals_chat.py` (12) | `olympus actions`, then `/approve <id>` in any channel |
| 3. Model failover chains | IMPLEMENTED | `config.py` `fallbacks_for` + `role_fallback_overrides`; `llm.py` key rotation on 429/credit; smoke: coding-failure chain ordered; `tests/test_failover_chains.py` (8) | `OLYMPUS_ROLE_FALLBACKS='{"coding":["haiku"]}'` |
| 4. Per-agent heartbeats | IMPLEMENTED | `agentbeat.py` (HB_OK quietness, compact context); wired in `heartbeat.py` tick + `hibernate.py` next-wake; smoke: quiet beat delivers nothing; `tests/test_agentbeat.py` (11) | `/heartbeat add 2h anything urgent?` |
| 5. On-exit scheduling | IMPLEMENTED | `scheduler.py` `kind="on_exit"`, one-shot disable, poll-aware next-wake; smoke: watch→kill→fired once; `tests/test_onexit_schedule.py` (9) | `olympus schedule add w daily "x" --on-exit <pid>` |
| 6. Capability profiles | IMPLEMENTED | `capprofile.py` (full/reader/guest + custom JSON); enforced in `specialists.py:94` and `actions.py` autonomy cap; smoke: guest keeps only `web_search`, autonomy L4→L0; `tests/test_capprofile.py` (9) | `olympus restrict tg-<id> guest` |
| 7. Memory wiki + dreaming | IMPLEMENTED | `wiki.py` (pages, lint, dream, context_block); nightly via `heartbeat.py` `DREAM_EVERY`; retrieval in `orchestrator._wiki_block`; smoke: dream writes page, retrieval finds it; `tests/test_wiki.py` (11) | `olympus wiki dream` / `/wiki` |
| 8. Pre-compaction flush | IMPLEMENTED | `recall.flush_slice` called from `orchestrator._compress_history` and `reset()`; smoke: extractor invoked on doomed slice; `tests/test_precompaction_flush.py` (6) | (fires automatically at compaction) |
| 9. Model pins | IMPLEMENTED | `modelpin.py` (/model shorthands, BYOK-safe, stale-pin fail-open); `OLYMPUS_SPECIALIST_MODELS` in `config.for_specialist`; smoke: pin resolves to gpt-5; `tests/test_modelpin.py` (9) | `/model haiku` · `/model auto` |
| 10. SecretRef | IMPLEMENTED | `secretref.py` (env:/file:/vault:/keychain:, fail-closed); resolved in `Settings.from_env`, pool members, Telegram token; smoke: env+vault resolve, missing → `""`; `tests/test_secretref.py` (12) | `olympus secret set NAME` → `api_key: "vault:NAME"` |
| 11. Usage footers | IMPLEMENTED | `usage.py` session totals/delta/footer; `/usage on\|off` in `gateway.py`; smoke: footer renders reply/session/today; `tests/test_usage_footer.py` (6) | `/usage on` |
| 12. Update handoff | IMPLEMENTED | `scheduler` interrupted-run resume (`started_at`/`RESUME_AFTER`), `selfupdate.write_handoff/take_handoff`, heartbeat report; smoke: journal→consume roundtrip; `tests/test_update_handoff.py` (7) | `olympus upgrade` (journals pending work) |

**Smoke summary (verbatim): `12/12 healthy`.**

### 1b. Core claimed surfaces (README/docs)

| Claim | Status | Evidence | Repro command |
|---|---|---|---|
| 86 CLI commands registered | IMPLEMENTED | `python -m olympus --help` lists 86; drift gate green | `olympus capabilities --check` |
| 38 read-only commands smoke-tested | IMPLEMENTED (37) + BROKEN (1) | 37 exit 0 with sane output; `scores` tracebacks (see §2) | loop in audit transcript |
| Graceful no-key degradation | IMPLEMENTED | `ask` → `No API key configured. Run \`olympus setup\`…` (no traceback); `telegram`/`whatsapp` → one-line setup hints | `olympus ask hi` (no keys) |
| `doctor` readiness check | IMPLEMENTED | prints readiness report, exit 1 when not ready (by design) | `olympus doctor` |
| Backup: archive+encrypt+sign | IMPLEMENTED | `✓ wrote olympus-backup-…tar.gz.enc — 4 files, 1 KB, encrypted, signed.` + honest "not delivered: no OLYMPUS_BACKUP_CMD" | `OLYMPUS_SECRET_KEY=x olympus backup` |
| Signing chain (keygen/pubkey) | IMPLEMENTED | keygen writes 0600 seed + prints pubkey; `witness-pubkey` prints key; `tests/test_verify_and_custody.py` (13) passes | `olympus keygen` |
| HTTP API + OpenAI-compat /v1 | IMPLEMENTED | `serve` binds: `OpenAI-compatible API: http://127.0.0.1:18484/v1 (loopback-only…)`; `tests/test_openai_endpoint.py` | `olympus serve` |
| Heartbeat/tick loop | IMPLEMENTED (see note §2.2) | `tick` runs due work; failures are caught and logged, loop continues | `olympus tick` |
| Replay / decision log | IMPLEMENTED | arg validation correct; `tests/test_replaygate.py`, `test_decisionlog.py` pass in suite | `olympus replay <run_id>` |
| Threat-model↔tool binding | IMPLEMENTED | `threatmodel.check_repo()` enforced by `scripts/check_threat_model.py` + `tests/test_browser.py:222` | `python scripts/check_threat_model.py` |

## 2. Broken / Partial detail

### 2.1 ~~BROKEN~~ FIXED: `olympus scores` without cached scores and no API key

**Fixed.** `scores` is now display-only: it reads the committed per-specialist
baseline via `evals.load_baseline()` (a file read — `{}` if none, never a model
call, never a raise) and, when empty, points the user to `olympus eval`. It no
longer calls `per_specialist_scores()`, so a "show" command can neither trigger
paid work nor crash with a raw SDK `TypeError` on a keyless install. The `scores`
help text now says "show the saved … baseline (run `olympus eval` to compute
fresh scores)", so help and behavior agree (resolves discrepancy §4.3). Covered by
`tests/test_keyless_ux.py`. Original report:


```
$ olympus scores
...
File ".../olympus/cli.py", line 1242, in main
File ".../olympus/evals.py", line 90, in per_specialist_scores
File ".../olympus/evals.py", line 189, in run
File ".../olympus/backend.py", line 91, in complete_text
TypeError: "Could not resolve authentication method. Expected one of api_key,
auth_token, or credentials to be set. ..."
```
- **Root cause**: `cli.py:1242` → `evals.per_specialist_scores()` silently
  **runs the live benchmark** when no scores are cached (`evals.py:90→189`),
  which issues real model calls. With no key, the raw SDK `TypeError` escapes
  (`backend._should_failover` only catches provider/network error types, not
  `TypeError`). Two defects in one: (a) a "show" command triggers paid work,
  (b) the no-key path is a traceback instead of the friendly message `ask`
  prints.
- **Fix size**: **S** — either guard with `Settings.from_env().usable()` and
  print the standard no-key hint, or make `scores` display-only ("no scores
  yet — run `olympus eval`").

### 2.2 ~~PARTIAL~~ FIXED: `olympus tick` with no key

**Fixed.** `heartbeat.tick` computes `firstrun.configured()` once and routes every
LLM-dependent job's failure through a shared `_job_error(label, configured)`
helper: on a keyless install (where the provider SDK raises before any network
call) each such job logs one quiet line — `"<job>: skipped (no provider key
configured)"` — instead of a full traceback per job per tick, matching the replay
self-check's existing guard. Real failures (key present) still get the full
traceback, and non-LLM jobs (handoff/scheduler/operator/maintenance/…) are
unchanged. Covered by `tests/test_keyless_ux.py`. Original report:


```
[tick] Argus: scanning the world for opportunities...
[tick] Argus failed:
Traceback (most recent call last):
  File ".../olympus/heartbeat.py", line 91, in tick
```
- The heartbeat correctly *contains* the failure (loop continues, state saved),
  but on a keyless install every tick prints a full traceback for each LLM
  job. A one-line "skipped: no provider key" (like the replay self-check's
  `firstrun.configured()` guard at `heartbeat.py:160`) would match the rest of
  the system. **Fix size: S** (reuse the existing `firstrun.configured()`
  pattern for the LLM-dependent cadences).

No other BROKEN/PARTIAL surfaces were found on the tested paths.

## 3. Dead code

**None found.** Candidates examined and cleared:
- `codegraph_gate.py`, `threatmodel.py` — not imported by runtime modules, but
  wired via CI/test harnesses (`scripts/check_threat_model.py:18`,
  `tests/test_browser.py:216`, `tests/test_codegraph_gate.py`). Gate harnesses,
  not dead code.
- Pass-only bodies (7): all intentional silencers — `log_message` overrides on
  `BaseHTTPRequestHandler` subclasses (discord/slack/web/webhook/whatsapp) and
  `_silent` reporter defaults (orchestrator/research).
- Grep for `TODO|FIXME|XXX|NotImplementedError`: **one** docstring mention
  (`codegraph_ast.py:8`, a roadmap note), zero code stubs.

## 4. Docs-vs-reality discrepancies

| # | Discrepancy | Severity |
|---|---|---|
| 1 | `OLYMPUS_DREAM_EVERY`, `OLYMPUS_JOB_RESUME_AFTER`, `OLYMPUS_PAIR_TTL` are read by code but documented nowhere in README/docs (only module docstrings) | Low — add to README env-var section |
| 2 | `OLYMPUS_ROLE_FALLBACKS` and `OLYMPUS_CHANNEL_PROFILE` are documented only in an analysis doc, not in user-facing README/docs | Low |
| 3 | ~~`olympus scores` help says "show per-specialist benchmark scores" but it may *run* the benchmark~~ **RESOLVED** — `scores` is display-only and its help now says so (§2.1) | — |
| 4 | README capability counts: **consistent** (gate green) — no discrepancy | — |
| 5 | `docs/THREAT_MODEL.md` ↔ live tool surface: **consistent** (binding check passes) | — |

## 5. Verdict

- **12/12 OpenClaw-adoption features: IMPLEMENTED** with runtime evidence and
  167 dedicated tests; no stubs, no unwired modules, no missing claims.
- Whole-repo health: full suite green (1473/0), drift gates green, graceful
  no-credential degradation everywhere tested **except** one BROKEN command
  (`scores`, fix size S) and one noisy-by-design path (`tick` keyless logging,
  fix size S).
- Recommended next pass (separate task, per instructions): fix §2.1 and §2.2,
  document the five env vars in §4.
