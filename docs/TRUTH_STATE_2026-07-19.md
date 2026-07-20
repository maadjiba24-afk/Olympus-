# Olympus — Truth-State Audit · 2026-07-19

> **Erratum (post-audit correction).** A later audit found two errors in this
> snapshot; they are corrected here rather than by rewriting the dated body:
> 1. **CI Python matrix.** The exec-summary (line ~60), Section A (line ~84),
>    and the M0-5 row (line ~169) call CI "3.12 only / OPEN." Section D (line
>    ~202) correctly records it as **DONE**. The latter is right: `ci.yml`'s
>    `test` job runs the full **3.10–3.13** matrix (pinned by
>    `tests/test_ci_matrix.py`). Treat the "3.12 only / OPEN" lines as stale.
> 2. **Action count.** Passages citing "**17 actions**" are wrong — the manifest
>    and code register **23** (the extra six are the `computer_*` actions in
>    `computeruse.py`). The doc's verification one-liner omitted `import
>    computeruse`, so it counted before those registered; `capabilities --check`
>    validates 23 and passes.

Read-only diagnostic (supersedes `TRUTH_STATE_2026-07-15.md`). Evidence hierarchy:
**executed command output > code read > docs**. `README` / `THREAT_MODEL` /
`capabilities.json` are treated as *claims to verify*, never as evidence.
Audited commit: `main @ a8863b2` (0.25.0 merged via PR #157; ADR-0005 amendments
1–4 and the M0–M4 milestone series all landed), clean tree, Python 3.11.15.

This audit exists because the 2026-07-15 doc is stale: the Milestone-0 items it
listed as "candidates — not fixed" have since merged, and Aletheia is no longer a
soft stage. Every "now:" verdict below quotes current code at `file:line`.

---

## Executive summary (12 lines)

1. **Suite is green modulo a known-environmental gap:** `2203 passed, 236 skipped,
   33 failed in 303s`. **All 33 failures are environmental, not code:** 27 are the
   crypto-family tests (this container is missing `_cffi_backend`, so
   `cryptography.hazmat…ed25519` panics under pyo3), and 6 are `test_proclock_races`
   worker subprocesses that can't `import olympus` because it isn't pip-installed
   here (they pass with `PYTHONPATH` set). CI (Python 3.12, hash-locked, olympus
   installed) runs all of these green.
2. **Counts match claims exactly:** 13 specialists, 101 tools, 111 commands, 17
   actions; `capabilities.check_repo() → []` (drift gate clean).
3. **Size:** 48,419 LOC across 153 modules (was 38,041 / 126 at the 2026-07-15
   audit — ~10k LOC of merged milestone work). Largest: `tools.py` 3383,
   `browser.py` 2380, `web.py` 2304, `cli.py` 2112, `orchestrator.py` 2108.
4. **Dependency surface is truthful:** 3 required deps (`anthropic`,
   `youtube-transcript-api`, `cryptography`) + 3 optional (`psycopg`,
   `websockets`, `pyyaml`) that are declared as optional-dependencies AND
   import-guarded. No undeclared third-party imports.
5. **Census clean:** 8 TODO/FIXME hits, all benign (doc prose + feature names like
   `ADD_TODO`); no abandoned-work markers; no real stubs (147 `pass`/`...` bodies
   are `except: pass` + Protocol/abstract). One runtime-unwired module
   (`codegraph_gate.py`) — an intentional manual eval harness, not dead code.
6. **All 8 security invariants PASS** (Section C-I). The two gaps the 2026-07-15
   audit flagged are closed: memory sanitization is now enforced **at the
   `memory.save()` sink**, and the untrusted-data envelope is now **structural /
   fail-closed** (a `TRUSTED_TOOLS` allowlist, not `INGESTION_TOOLS` membership).
7. **All 8 verification / self-evolution / durability items PASS** (Section C-II).
   **Aletheia is now ENFORCING** (structured `answer.verify` verdict → forced
   rework → `⚠️ UNVERIFIED` banner), the synthesis stage is verified too
   (`answer.synthesis`), and a **mid-run checkpoint/resume ledger now exists**
   (`ledger.py`) where the last audit found none.
8. **Replay determinism still REAL** (`replaystore.py`; request-hash canonical,
   nondeterministic tools frozen, mutated prompt → `ReplayDivergence`).
9. **Action spine deny-by-default still REAL and tested:** IRREVERSIBLE /
   FINANCIAL_LEGAL never auto (min level 99); scope gate fails closed; behavioral
   re-check at the single execute chokepoint.
10. **Trust root is still the public dev seed by design** (`is_default_seed:
    True`, `posture: dev`). A secret seed is a deploy-time operator act, never a
    committed artifact — sovereign/production paths fail closed without one.
11. **Prompt writers converged to single-writer** (M0.4): the former ungated
    `_update_prompt` is now a thin alias that routes through the benchmark-gated
    `gate_prompt`; the raw writer is internal-only.
12. **No unbuilt product features remain.** What's left (Section D) is a short list
    of *deliberately deferred* engineering (egress-gateway phases B–D, one pinned
    action root, the live payment rail — all documented non-goals/future work in
    ADRs + `DEFERRED.md`) plus operational drift (CI tests only 3.12; a couple of
    doc/status staleness items, two of which this pass fixed).

---

## SECTION A — Ground-truth inventory

### Counts (executed)
```
$ python -c "from olympus import tools,cli; from olympus.specialists import SPECIALISTS; \
             from olympus import actions,builtin_actions,capabilities; builtin_actions.register_builtins(); \
             print(len(SPECIALISTS),len(tools.HANDLERS),len(cli.command_names()),len(actions.registered()),capabilities.check_repo())"
specialists: 13   tools: 101   commands: 111   actions: 17   check_repo(): []
```
| Surface | capabilities.json | code | Verdict |
|---|---|---|---|
| specialists | 13 | 13 | PASS |
| tools | 101 | 101 | PASS |
| commands | 111 | 111 | PASS |
| actions | 17 | 17 | PASS |

### Runtime & dependencies
- **Python (local):** 3.11.15. **CI pins 3.12** (`.github/workflows/ci.yml:14,37,48,74`;
  `publish.yml`, `replay-gate.yml`). `pyproject.toml:9` declares `requires-python =
  ">=3.10"` with 3.10–3.13 classifiers → **CI covers one of four supported versions**
  (see Section D, drift-2).
- **Required deps (`pyproject.toml:34-38`):** `anthropic>=0.92.0`,
  `youtube-transcript-api>=1.0.0`, `cryptography>=41.0.0` (matches `requirements.txt`).
- **Optional deps (`pyproject.toml:47-50`):** `psycopg`, `websockets`, `pyyaml` —
  all import-guarded (`except ImportError`, 5 sites). No undeclared imports.

### Size
- **48,419 LOC across 153 modules** (`wc -l olympus/*.py`). Largest 5 above.

### Census
- **TODO/FIXME/XXX/HACK:** 8 hits, all benign (doc prose in `codegraph_ast.py:8`;
  `HACK` as a node-type name in `codegraph.py:171`; `LIST_TODOS`/`ADD_TODO`/
  `COMPLETE_TODO` tool schemas in `tools.py`). No abandoned-work markers.
- **Stubs:** 147 `pass`/`...` bodies — 130 are `except …: pass`, remainder are
  Protocol/abstract/typing ellipses. No abandoned stub logic.
- **Runtime-unwired modules:** only `codegraph_gate.py` (self-documented manual
  "Phase 0 gate harness" eval; referenced by its test only). All other suspected
  candidates (`payrail`, `computeruse`, `egress`, `effortscore`, `speculate`, …)
  ARE wired.

---

## SECTION B — Baseline health

- **Full suite:** `2203 passed, 236 skipped, 33 failed in 303.37s`
  (`python -m pytest -q`).
- **The 33 failures are 100% environmental — proven by partition:**
  - 27 in the crypto family (`test_verify_and_custody` 9, `test_seed_custody` 6,
    `test_opconfig` 6, `test_secretref` 3, `test_backup` 2, `test_exfil_scan` 1):
    root cause `ModuleNotFoundError: No module named '_cffi_backend'` → pyo3 panic
    when `cryptography.hazmat.primitives.asymmetric.ed25519` loads. Top-level
    `import cryptography` succeeds; the native signing backend does not.
  - 6 in `test_proclock_races`: worker subprocesses `ModuleNotFoundError: No module
    named 'olympus'` because the package is source-run here, not pip-installed;
    they pass verbatim with `PYTHONPATH=/home/user/Olympus-` set (verified).
  - Net: **0 non-environmental failures.** On CI (3.12, `pip install -e .`,
    hash-locked deps, working `cryptography`) all pass.
- **CI gates (would run green):** `olympus capabilities --check` → consistent;
  `scripts/check_threat_model.py`; `scripts/check_no_prerelease.py`;
  `compileall`.
- **Skips (236):** environment/opt-in (real-browser smoke behind
  `OLYMPUS_BROWSER_SMOKE/_REAL`, POSIX-only file-mode tests, websockets transport,
  crypto-backend-unavailable), none default-path network-flaky.

---

## SECTION C-I — Security invariants (all PASS)

| # | Invariant | Verdict | Evidence |
|---|---|---|---|
| 1 | **Sanitize at the memory sink** | **PASS (July-15 gap CLOSED)** | `sanitize_for_memory` is called INSIDE `memory.save()` — `memory.py:134-136`; docstring `:127-133` ("only one door into memory, and it is guarded"); idempotent (`security.py:249-250`). Was caller-applied only. |
| 2 | **Untrusted-data envelope** | **PASS (now structural / fail-closed)** | `should_wrap()` defaults to WRAP; only `TRUSTED_TOOLS` allowlist skips — `security.py:174-190`; allowlist + inversion comment `:105-140`. Applied at `agent.py:172-173`, `openai_compat.py:235-236`, `learn.py:57`. Closing-tag injection neutralized `security.py:157`. Was `INGESTION_TOOLS`-membership-gated. |
| 3 | **Command guard fail-closed** | **PASS** | Default mode `enforce`, typo→enforce never off — `cmdguard.py:62-66`. DENY rules (fork bomb `:80-85`, mkfs `:87-89`, dd/redirect-to-device `:90-94`, `--no-preserve-root` `:96-98`, poweroff `:100-101`, `/etc` overwrite `:103-104`, wrapper-proof `bash -c "rm -rf /"` `:130-131`, `find / -delete` `:133-134`, shred-device `:136-137`, tokenized `rm -rf` system/home `:207-217`). Paranoid blocks CONFIRM too `:261-262`. |
| 4 | **SSRF / egress gates** | **PASS** | `_host_is_loopback` `security.py:382-391`; `host_on_allowlist` `:419-432`; `egress_allowed` `:435-441`; metadata/internal blocklist `:457-459`; DNS-rebind-pinned `resolve_pinned_ip` `:472-508`; `url_block_reason` `:511-553`. |
| 5 | **Capability profiles** | **PASS** | `_READER_DENY` `capprofile.py:28-35`; `_GUEST_DENY ⊃ reader` `:37-42`; autonomy caps full=4/reader=1/guest=0 `:44-48`; `autonomy_cap()` `:138-140`. |
| 6 | **Action spine deny-by-default** | **PASS** | Risk classes `actions.py:34-38`; L0–L4 `:41-46`; IRREVERSIBLE/FINANCIAL `_min_level_to_auto=99` `:68-76`; `can_auto_execute` `:332-352`; execute-time scope gate fail-closed `:362-366`; behavioral re-check at the one chokepoint `:382-392`. |
| 7 | **Egress data-class matrix** | **PASS** | ALLOW/REDACT/HOLD `egress.py:42-46`; classes C0–C2 `:29-33`; `_MAX_AUTO` matrix `:51-56`; `guard()` fail-closed `:89-132`. Enforcement OFF by default (`OLYMPUS_EGRESS_GUARD`). |
| 8 | **Witness trust root** | **PASS (dev seed by design)** | Public `_DEFAULT_SEED` `witness.py:39`; `is_default_seed()` True unconfigured `:135-141`; `posture()` "dev" `:234-238`; secret seed via `OLYMPUS_SIGNING_SEED[_FILE]` at runtime `:74-127`; sign/manifest/verify refuse the default in sovereign/production `:260-288,398-410,484-494`. Only the **public** pin is committed (`witness_pubkey.txt`). |

---

## SECTION C-II — Verification / self-evolution / durability (all PASS)

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | **Aletheia ENFORCING** | **PASS (July-15 "soft, not a block" RESOLVED)** | Structured `VERDICT: {status: pass/warn/reject,…}` — `orchestrator.py:421-424`; parsed `:171-194`; contract chokepoint `_enforce_answer_verify` `:509`; one forced rework on reject `:519-539`; `⚠️ UNVERIFIED` banner on 2nd reject `:564-585`; predicate fed real verdict `behavioral_contracts.py:170-181`. Tests `test_answer_verify.py`. |
| 2 | **Synthesis faithfulness** | **PASS** | `answer.synthesis` no-tools check `orchestrator.py:589-622`; `⚠️ UNVERIFIED ADDITIONS` banner `:134-138,673-674`; `OLYMPUS_SYNTH_CHECK=off` kill switch (`config.py:823-829`, **default on**). Tests `test_synth_check.py`. |
| 3 | **Replay determinism** | **PASS** | `replaystore.py`: `canonical_request`/`request_hash` `:72-83`; frozen tools/context `:122-124,159-184`; `ReplayDivergence` `:44-58`. Tests `test_replay_tools.py`. |
| 4 | **Evals + auto-rollback** | **PASS** | Benchmark-gated `gate_prompt` reverts on regression — `orchestrator.py:2039-2068` (before `:2046`, after `:2055`, restore on regress `:2064`); fail-closed without coverage `:2039-2043`. Tests `test_m0_single_prompt_writer.py`. |
| 5 | **Scaffold evolution** | **PASS (propose-only)** | No `apply()` — `scaffold_evolve.py:1-19,94,232-245`; gated `OLYMPUS_SCAFFOLD_EVOLVE` `:75-77`; security-module denylist + fail-closed allowlist `:42-63`; benchmarks on temp path only `:143-158`. Tests `test_scaffold_evolve.py`. |
| 6 | **Mid-run checkpoint/resume** | **PASS (July-15 "ABSENT" now EXISTS)** | `ledger.py`: signed content-addressed checkpoint chain `:47-72`; resumable `state()` `:195-198`; resume-skips-committed `drive()` `:289-319`; plan-divergence refused `:307-312`; crash-heal `:122-171`. Tests `test_m2_ledger.py`. **Caveat:** the primitive is tested at module level; the live interactive ask-path is not yet shown driving through `ledger.drive()` (Section D, item 5). |
| 7 | **Single prompt writer** | **PASS (M0.4)** | `_update_prompt` now routes through the gate — `tools.py:705-714` ("ROUTES through the benchmark gate … no ungated write path") → `orchestrator.gate_prompt`; `_gate_prompt` identical `:1081-1083`; raw `_apply_prompt` internal-only, reached only inside `gate_prompt` `orchestrator.py:2018-2050`. |
| 8 | **Cross-process safety** | **PASS** | `proclock.py` `fcntl.flock` reentrant, bounded `:59-125`; used by usage ledger `usage.py:130`, memory `memory.py:363,381,732,747`, goals `goals.py:111`, agentbeat `agentbeat.py:84`. Tests `test_proclock_races.py`. |

---

## SECTION C-III — Milestone deltas since 2026-07-15

| July-15 Milestone-0 candidate | Status now |
|---|---|
| **M0-1** provision secret witness seed | **By design a deploy act** — repo correctly ships the public dev seed; sovereign/production fail closed without a configured secret seed. Not a code gap. |
| **M0-2** centralize memory sanitization | **DONE** — enforced in `memory.save()` (`memory.py:134-136`). |
| **M0-3** make untrusted-wrapping structural | **DONE** — `TRUSTED_TOOLS` fail-closed allowlist (`security.py:174-190`). |
| **M0-4** reconcile the two prompt writers | **DONE** — single gated writer (`tools.py:705-714`). |
| **M0-5** close CI/local Python drift | **OPEN** — CI still tests only 3.12 (Section D, drift-2). |
| Aletheia "soft verify, not a block" | **RESOLVED** — enforcing `answer.verify`/`answer.synthesis` (ADR 0005). |
| "No mid-run checkpoint/resume" | **RESOLVED** — `ledger.py` (M2.1). |

Also merged since: M1.1/M1.2 (authority artifacts, identity grants), M2.2
(speculative branches), M3.1–M3.4 (delta substrate, prompt reflection, memory
block-mode, sleeptime scheduler), M4 (AP2 ADR), parked-1…6 (on-device user-key,
MCP auth, OTLP, payment-rail sandbox, OS computer-use, RL scaffold), and the
browser-harness / operator / graded-autonomy workstreams.

---

## SECTION D — What actually remains

Nothing here is an unbuilt *product feature*; the codebase is feature-complete
against its own roadmap (all 42 VISION backlog items + M0–M4 + parked-1…6 merged).
The five items this doc first listed as deferred/drift were then taken up in a
2026-07-19 hardening pass (PR #163); their status is below.

### Resolved by the 2026-07-19 hardening pass
- **Egress-gateway phases B–D — DONE.** B routes the contribution pool through
  `guard(..., POOLED)` (`contrib._pool_redact`); C guards the broadcast + external
  sinks (`gateway.notify_all`, `agentbeat._deliver`, `tools._propose_upgrade`'s
  GitHub issue); D consciously scopes the host/workspace sinks OUT (governed by
  the actions spine) — documented in `docs/DESIGN_BOUNDARY_LAYER.md`. The gateway
  now spans every network egress path. Still off by default (`OLYMPUS_EGRESS_GUARD`).
  Tests: `test_egress.py`, `test_contrib.py`.
- **One pinned action root — DONE.** `actions.prepare()` pins
  `_pinned_root = sandbox.workdir()` (internal, non-user-editable) for the
  workspace-rooted action types; the executors confine to it via
  `sandbox._effective_root` (tamper-safe: a bogus pin fails back to `workdir()`).
  An approved file/command action now runs under the exact root it was previewed
  under, unblocking safe per-worker roots. Tests: `test_pinned_root.py`.
- **CI Python 3.10–3.13 matrix — DONE.** The `test` job runs the full range from
  the one *universal* hash lock; `test_ci_matrix.py` guards against drift.
- **`ledger.drive()` checkpoint/resume wired into the ask-path — DONE.** `ask()`
  checkpoints the expensive council stage to the run's ledger via
  `run_checkpointed` (gated `OLYMPUS_ASK_CHECKPOINT`, default OFF, forced off under
  replay); `resume_ask(run_id)` recovers a committed answer without recomputing
  the council. The July-15 "no checkpoint exists" gap is now closed end-to-end.
  Tests: `test_ask_checkpoint.py` (+ `test_m2_ledger.py`).
- **Doc/status drift fixed:** `contracts.py` + `DESIGN_OUTPUT_CONTRACTS.md`
  ("off by default" → **on**, ADR 0005); ADR 0002 header ("HALTED before any code"
  → scoped to live-rail integration; the co-signature custody primitives shipped).

### Still deliberately deferred (conscious non-goals — a decision, not an oversight)
- **Live payment rail** — explicitly out of scope across ADR 0001/0002/0004; the
  full live-cutover path exists but ships **INERT** and stays that way by design.
  The 2026-07-19 pass *hardened the inertness* (`paylive.is_inert()`, a loud
  WARNING when a real adapter is installed, and a repo-wide structural test that
  no module can call `register_live_adapter` or write `OLYMPUS_PAYMENT_LIVE`) — it
  did **not** wire a rail. Going live still requires the two out-of-band operator
  acts. Tests: `test_payment_inertness.py`, `test_paylive.py`.
- **`DEFERRED.md`** enumerates 10 consciously-accepted limitations (LLM-judge skill
  admission, no semantic dedup at skill-write, prompt-index keyword-not-embedding,
  Athena one-shot fail-open, effort-tier no-op on some backends, per-process
  concurrency cap, proclock degraded on non-POSIX, …). Item 8 (same-name workspace
  clobber) is partially resolved by the pinned root above.

### Built but gated OFF by design (NOT "remaining" — operator opt-in)
Live payments, OS computer-use, scaffold self-evolution, browser operator, egress
guard, learned routing, browser-financial templates, earned autonomy, and the
A2A/dytopo/RL/emem/sleeptime experimental surfaces all ship disabled behind
`OLYMPUS_*` flags by deliberate design. Enabling any is a configuration decision,
not development. (ON by default, for the record: output contracts, synthesis
check, behavioral contracts, ACE, memory, tool-select, command guard `enforce`.)

---
*Generated read-only except for the small doc/status drift corrections noted in
Section D. Every verdict quotes executed output or `file:line`; nothing inferred
as PASS.*
