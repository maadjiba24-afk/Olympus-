# OLYMPUS — Complete Technical Audit & Teardown

**Date:** 2026-07-29
**Auditor:** automated evidence-based teardown (main auditor + 15-way subsystem analysis + 40-way adversarial verification)
**Commit audited:** `df3dddd` (branch `claude/olympus-technical-audit-lwhsp6`, forked from `main`)
**Repo:** https://github.com/maadjiba24-afk/Olympus-
**Package:** `olympus-council` v0.26.0 · Python ≥3.10 · MIT

> **Method.** Every classification below is backed by reading the code and tracing the
> execution path from public entry point to side effect — not by trusting names, docstrings,
> READMEs, or the presence of a class. The full test suite was executed. 15 subsystem
> analyzers read every module; 40 of the most serious findings/claims were then
> independently re-verified by adversarial agents instructed to *refute* them. Of those 40,
> **23 were CONFIRMED, 16 downgraded to PARTIAL (the fact was true but the "mismatch" framing
> was wrong — the limitation is openly disclosed in the code), and 1 was REFUTED.**

---

## 1. Executive summary

**What Olympus is today.** A large, coherent, genuinely-implemented multi-agent AI framework —
about **85,000 lines of core Python across 209 modules, plus ~61,000 lines of a trading
subsystem**, with **396 test files / 8,531 tests**. It is *not* vaporware and it is *not* a
scaffold with impressive names over empty bodies. The core pipeline is real and wired end to
end: a user prompt flows `Olympus.ask()` → route (Zeus) → dependency-graph plan (Athena) →
parallel specialist dispatch → verify (Aletheia) → review → synthesize, and every stage
bottoms out in real provider calls. Persistence, cryptographic signing, egress control, the
approval/action lifecycle, the backtester, and the CLI are all real, working code.

**What it is designed to become.** A "controlled-autonomy, self-improving" sovereign AI
assistant: an always-on council that learns from its own outcomes, watches the web, improves
its own prompts/skills/parameters, verifies its own claims against a code knowledge graph,
runs fully offline in a provable zero-egress mode, and (in the trading arm) forecasts markets
with a self-evolving native model.

**What it can genuinely do today** (details in §10). Run a verified multi-agent Q&A/agentic
pipeline against Anthropic/OpenAI-compatible/Bedrock/local providers; persist memory and
conversations durably to a local file store or Postgres; prepare/approve/execute/undo
governed actions with layered safety gates; sign and byte-for-byte **replay** every decision;
enforce **fail-closed zero-egress** at its own network choke points; run a look-ahead-safe
trading **backtester** and **paper broker**; serve a browser chat UI, an OpenAI-compatible
`/v1` endpoint, and Telegram/WhatsApp/Slack/Discord/etc. channels; and self-tune a set of
numeric runtime parameters from recorded outcomes.

**What it cannot yet do** (details in §11). It has **never been deployed** and is
**architecturally single-process** (in-memory sessions/rate-limits/metrics — a second replica
breaks). Most of the *advanced* capabilities — the code knowledge graph, the ingest gate,
measured model-qualification routing, episodic-memory reconstruction, scaffold code-evolution,
semantic recall — are **implemented and tested but never triggered on a default install**
(no automatic build/observe/wire step), so out of the box they are inert. The flagship
AI-safety claims (**hallucination controller**, **controlled autonomy**) are validated in
tests only against a **stubbed model that returns constant JSON**, so their *substance* is
unproven. The **native trading model** is a research island: nothing wires it to serving, its
neural path needs `torch` (absent here so it has never run a gradient step), and on matched
evaluation the statistical model it *can* run **does not beat a random walk**. No real broker
adapter ships (paper + Binance-testnet only, the latter never having completed a request).

**Is it deployable?** Yes, for a **single-node, single-tenant** install: a working Dockerfile
and docker-compose (web + Caddy TLS + channels + heartbeat) exist and the app boots. It is
**not** deployable as a scalable, multi-instance, HA service without re-architecting request
state into the shared store.

**Is it production-ready?** **No.** Single-node only; memory persists only if an operator
mounts a volume / sets `OLYMPUS_MEMORY_DIR` (default path lives inside the image); no metrics
backend, log aggregation, or alerting; several security controls are **off by default**; the
default signing seed is **public and forgeable** (integrity, not authenticity); and the headline
autonomy/verification claims lack real-model test evidence. (The unlimited-default-budget and
unguarded-production-boot findings, E21/E22, were **fixed in 0.27.0** after this audit.)

**Most serious technical risks (all HIGH; there are no CRITICAL code defects):**
1. **Inert-by-default advanced capabilities.** Codegraph (never auto-built), ingest gate
   (off by default), model-qualification routing (`observe()` never called in prod), scaffold
   evolution (never invoked), episodic memory (`context_block` has no caller). Operators and
   readers will believe these are protecting/improving them when they do nothing.
2. **Verification correctness is unproven.** Aletheia is an LLM judging an LLM; it **fails
   open** on any verifier error (ships unverified behind a banner) and the **router LLM can
   switch it off** per answer. All tests mock the model with fixed JSON.
3. **Single-node architecture.** In-process sessions/rate-limits/metrics ⇒ cannot scale
   horizontally; a droplet loss with the default (commented-out backup) config is total data
   loss.
4. **Native trading model has not learned.** Synthetic-only training, loses to
   persistence/linear/GBT on held-out data, neural path never executed, promotion gate wired
   to nothing that serves.
5. **Security defaults.** Public signing seed; assessment scope read from an **unsigned**
   plaintext file; single-seed "two-party" custody; sandbox `local` backend gives **no OS
   isolation**; WhatsApp/webhook endpoints unauthenticated when their optional secret is unset.
6. ~~**Unsafe operational defaults.**~~ **FIXED in 0.27.0.** An unset `OLYMPUS_DAILY_BUDGET` now
   resolves to a bounded `DEFAULT_DAILY_BUDGET` ($10/day) instead of unlimited, removing the ceiling
   is an explicit opt-in, and a malformed value no longer crashes `import olympus.config`. The
   fail-closed boot checklist now also runs under `OLYMPUS_ENV=production`
   (`config.production_problems` / `require_production_config`), so production refuses to boot with an
   explicitly-unlimited budget, infinite retention, an unwritable memory dir, or an off-loopback bind
   with no credential.

**The honesty signal.** Unusually, the repository's *own* comments, ADRs, `DEFERRED.md`,
`docs/TRUTH_STATE_*.md`, and `docs/*STATUS*.md` disclose most of these limitations directly.
16 of 40 verified "claim mismatches" collapsed on inspection into "the code openly says this."
The gap here is overwhelmingly **README-headline framing vs. default-install reality**, not
concealment.

---

## 2. Architecture map

**Main components**

- **Council / orchestrator** (`orchestrator.py`, `agent.py`, `specialists.py`, `moa.py`,
  `consensus.py`, `subagents.py`, `dytopo.py`, `treesearch.py`): the multi-agent pipeline and
  13 named specialists.
- **Provider/routing layer** (`llm.py`, `backend.py`, `providers.py`, `bedrock_converse.py`,
  `openai_compat.py`, `moa.py`, routing: `routesub.py`, `learned_routing.py`,
  `bandit_routing.py`, `modelgrade.py`, cost/context: `usage.py`, `ctxbudget.py`,
  `ctxheat.py`, `streamguard.py`).
- **Tool & action layer** (`tools.py` [4,653 loc], `actions.py`, `builtin_actions.py`,
  `sandbox.py`, `cmdguard.py`, `egress.py`, `mcp_client.py`, `mcp_server.py`,
  `computeruse.py`, `skills.py`, `skillpack.py`, `playbooks.py`).
- **Memory & knowledge** (`memory.py`, `emem.py`, `usermem.py`, `recall.py`, `facts.py`,
  `docrag.py`, `embed.py`, `annindex.py`, `relgraph.py`, `codegraph_*` [17 modules]).
- **Governance & security** (`approvals.py`, `mandate.py`, `security.py`, `ingestgate.py`,
  `witness.py`, `attest.py`, `vault.py`, `assess.py`, `accounts.py`, `threatmodel.py`).
- **Persistence & ops** (`store.py`, `ledger.py`, `trace.py`, `replaystore.py`,
  `replaygate.py`, `backup.py`, `sessionlog.py`, `heartbeat.py`, `watchdog.py`,
  `scheduler.py`, `goals.py`, `config.py` [1,844 loc]).
- **Surfaces** (`web.py` [3,103 loc], `gateway.py`, `openai_server.py`, `adminpanel.py`,
  `a2a_server.py`, `health.py`, `metrics.py`, `otel.py`, `cli.py` [3,188 loc], `tui.py`, plus
  channels: `telegram.py`, `whatsapp.py`, `slack.py`, `discord.py`, `signal.py`, `sms.py`,
  `matrix.py`, `mattermost.py`, `googlechat.py`, `email_gateway.py`, `gmail.py`).
- **Self-improvement** (`evolve.py`, `scaffold_evolve.py`, `selfupdate.py`, `experiments.py`,
  `calibration.py`, `behavioral_contracts.py`, `ace.py`, `reflect.py`, `curator.py`).
- **Trading** (`trading/`: `contracts.py`, `risk.py`, `oms.py`, `portfolio.py`, `backtest.py`,
  `ta.py`, `signals.py`, `strategy.py`, `forecast.py`, brokers; `trading/native/`: the native
  forecasting model; `kronos_*`: optional torch K-line backend).

**Databases / stores.** One pluggable KV abstraction: `store.FileStore` (default — atomic
`os.replace` writes, `0600` perms, `proclock` for read-modify-write) or `store.PostgresStore`
(a single `olympus_kv` table, activated by `OLYMPUS_DATABASE_URL`). **Important boundary:** only
KV namespaces move to Postgres; decision-log traces (`trace.py`), conversation history and
user memory (`memory.py`) stay on the local filesystem regardless.

**Text system diagram**

```
                 ┌─────────────────────────── SURFACES (trust boundary: authN here) ──────────────────────────┐
   users ──────► │  web.py /api/* (token/loopback)   /v1/* OpenAI-compat   adminpanel   channels(TG/WA/Slack…) │
                 │  cli.py / tui.py                   a2a_server            gateway (unified daemon)             │
                 └───────────────┬───────────────────────────────────────────────────────────────────────────┘
                                 │  (rate-limit, admission, session — ALL in-process memory)
                                 ▼
        ┌──────────────────────────── ORCHESTRATOR  (orchestrator.Olympus.ask) ─────────────────────────────┐
        │  Zeus route ─► Athena plan (DAG) ─► dispatch specialists (parallel) ─► ALETHEIA verify ─► synthesize │
        │       │                │                     │                              │                        │
        │       └── needs_verify?│  effortscore        │ subagents/dytopo/treesearch  │ codegraph oracle       │
        │           (LLM-gated)  │  toolselect         │                              │ (empty unless built)   │
        └───────────────┬────────────────────┬─────────────────────────┬─────────────────────────────────────┘
                        │                     │                         │
                        ▼                     ▼                         ▼
              PROVIDER/ROUTING          TOOLS + ACTIONS           MEMORY / KNOWLEDGE
         llm.complete / backend.run   tools.py (130 exposed)   memory (file) · recall · facts
         anthropic│openai│bedrock│moa  ┌── APPROVAL SPINE ──┐   embed/annindex (off default)
         routesub/learned/bandit       │ prepare→approve→   │   codegraph (17 mods, not auto-built)
         (default OFF / data-starved)  │ execute→undo       │
                        │              │ gates: shadow→scope│         ── APPROVAL BOUNDARY ──
      ┌── EGRESS CHOKE ─┴──┐           │ →rate→ABC→audit    │   irreversible/financial actions HALT
      │ security.assert_    │          └────────┬───────────┘   for human approval; shadow mode
      │ egress_allowed()    │                   │               refuses them outright
      │ (fail-closed, app-  │                   ▼
      │  layer only)        │        PERSISTENCE (── PERSISTENCE BOUNDARY ──)
      └─────────┬───────────┘        store: FileStore(default) │ PostgresStore(KV only)
                │                     trace/ledger: Ed25519-signed, hash-chained, REPLAYABLE
                ▼                     witness: signs (PUBLIC default seed unless OLYMPUS_SIGNING_SEED)
      external model APIs             backup: tar.gz+encrypt (off-host only if OLYMPUS_BACKUP_CMD)
      (blocked in sovereign mode)
                                      HEARTBEAT (heartbeat.run_forever) ── drives all autonomy;
                                      NOT self-starting: needs `olympus heartbeat` / compose service
```

**Trust boundaries.** (1) Surface authN: `/api/*` requires `OLYMPUS_ACCESS_TOKEN`
(constant-time compare) or loopback; `/v1/*` and `/api/admin` refuse off-box callers with no
credential. (2) Approval boundary: irreversible/financial actions halt for human approval;
shadow mode refuses them. (3) Egress boundary: `security.assert_egress_allowed()` — but
**application-layer only** (a dependency that opens its own socket bypasses it). (4)
Persistence boundary: durable state under `MEMORY_DIR`; **default path is inside the
image/repo** unless an operator overrides it.

---

## 3. Complete capability inventory

**Classification totals (~254 capabilities across 14 subsystems, incl. the persistence-ops re-run):**

| Status | Count | Meaning |
|---|---:|---|
| WORKING | ~172 | Real path traced end-to-end + real (non-mock) test or trivially-correct pure code |
| IMPLEMENTED_UNTESTED | ~36 | Real code, but only mock/skip coverage or opt-in path never run here |
| PARTIAL | ~33 | Real but incomplete, or works only in a narrow/conditional configuration |
| MISSING | 4 | Claimed/assigned but absent (2 are audit file-list artifacts — see note) |
| MOCK_ONLY | 2 | Only a mock/stub path runs; tests assert against mocks |
| UNREACHABLE | 2 | Implemented + tested but no production caller |
| DISABLED | 2 | Present but off by default and short-circuited |
| SCAFFOLD_ONLY | 1 | Skeleton/propose-only; no functional apply path |
| BROKEN | 1 | Does not work as designed (horizontal scaling) |

> Listing all 162 WORKING items individually would be noise; they are summarized by subsystem
> below with representative entries. **Every non-WORKING / degraded capability is listed
> explicitly** — those are what matter for deployment.

### 3a. Non-WORKING / degraded capabilities (full list, with evidence)

| Capability | Status | Evidence | Problem | Prod-ready |
|---|---|---|---|---|
| Measured model-qualification store (`modelgrade`) | **MOCK_ONLY** | `modelgrade.py:390` `observe()` is the only write path; grep shows it is called **only** from `tests/test_modelgrade.py` | No prod path records qualification evidence → every member is permanently `UNTESTED`; `routesub` substitution built on it can never fire | No |
| Orchestrator/verification integration tests | **MOCK_ONLY** | `tests/test_calibration.py:486` `_stub_client()` returns a canned Anthropic client emitting fixed JSON (`mode=direct`, `verdict=approve`) | Verification/routing **correctness** is unprovable from these tests | No |
| Context-budget arbiter (`ctxbudget.plan`) | **UNREACHABLE** | `ctxbudget.py:321` `plan()`; callers only in tests + `scripts/perf_validation.py` — no `olympus/` runtime caller (only `estimate_tokens` is used) | Whole-prompt fit / `ContextExceeded` accounting never invoked in production | No |
| Episodic reconstruction (`emem.context_block`) | **UNREACHABLE** | `emem.py:105` `reconstruct` + `:197` `context_block` implemented & unit-tested, but **no production caller** anywhere | "Episodic memory added alongside existing paths" — but no path reaches it; effectively dead | No |
| Persistent-artifact ingest gate (`ingestgate`) | **DISABLED** | `ingestgate.py:273` `enabled()` reads `OLYMPUS_INGESTGATE`, default **OFF**; `:817` `check()` returns payload unchanged when disabled; all call sites short-circuit | The advertised structural defense against malicious durable artifacts gives **zero** protection on a default install | No |
| GPU/distributed/optional-dep integration tests | **DISABLED** | 168 skips: `torch` absent (~6 files), docker absent (~6), plus mcp/websockets/boto3/POSIX gates | Isolation + GPU + some driver paths unexercised | n/a |
| Governed scaffold code-evolution (`scaffold_evolve`) | **SCAFFOLD_ONLY** | `scaffold_evolve.py:127` `propose()` called **only** from tests; **no apply path by design** | The "Darwin-Gödel"/code-self-modification engine is inert in prod and cannot change behavior even if run | No |
| Live real-money trading | **MISSING** | `brokers/__init__.py:8` "No real broker adapter ships here"; only `PaperBroker` + testnet | No live execution capability (honestly disclosed) | No |
| Schema migration framework | **MISSING** | grep for alembic/migration/CREATE-TABLE finds only in-file `SCHEMA_VERSION` JSON-quarantine constants | No migration path (acceptable for fixed KV table, gap for evolving schemas) | Not present |
| Horizontal scaling | **BROKEN** | `web.py:200` `_SESSIONS` (cap 500), `:210` `_HITS`, `:233` `_DAILY`, `metrics.py` globals — all in-process | A 2nd replica forks sessions/limits/metrics; rate limits bypassable across replicas | No |
| `trading/calibrate.py`, `trading/report.py` | **MISSING** | Files do not exist | **Audit file-list artifact** — calibration lives in `evaluate.py`, reporting in `perf.py`/`champion.py`/`.to_dict()`; *not* a real defect | n/a |

### 3b. WORKING capabilities by subsystem (representative)

- **Core orchestration (15 caps, ~13 WORKING).** `Olympus.ask/ask_stream/ask_ephemeral`
  pipeline; Athena dependency-graph planning; parallel specialist DAG dispatch;
  reject→forced-rework→UNVERIFIED-banner enforcement; consensus quorum (`consensus.py`, 24
  tests); dynamic topologies (`dytopo`, 13 tests); bounded planner with approval halts
  (`treesearch`, 17 tests); tool trimming (`toolselect`); tool-call repair (OpenAI/Bedrock);
  mid-run steering. *Caveat: all model calls are mocked in tests.*
- **Providers/routing (18 caps).** WORKING: Anthropic/OpenAI-compat/Bedrock/MoA/local
  backends; streaming; real provider-reported token accounting; context estimation; stream
  guarding. PARTIAL/inert: learned routing (evidence-gated, off by default, needs 300+
  outcomes), bandit routing (off by default), `modelgrade`/`routesub` (data-starved), live
  pricing (never wired → static tables).
- **Tools/actions/sandbox (19 caps).** WORKING: 130 exposed tools; prepare→approve→execute→undo
  spine with layered gates; command guard; MCP client (with `mcp` SDK) & server; skills;
  playbooks; computer-use interface. PARTIAL: sandbox (`local` backend = **no OS isolation**);
  egress guard (off by default); AP2 payment authorization (**records/signs a mandate, moves
  no money**).
- **Memory/knowledge (22 caps).** WORKING: durable file/Postgres KV; conversation persistence;
  user profile card; facts with decay; full-text search; relationship graph; real embeddings
  API + cosine; exact-cosine ANN (with pure-Python HNSW drop-in). Off-by-default/inert:
  semantic recall, docRAG, episodic reconstruction, codegraph.
- **Web & channels (25 caps, ~20 WORKING).** WORKING: threaded web UI; `/v1` OpenAI-compatible
  endpoint; admin panel; `/healthz`+`/readyz`; per-IP rate limiter; constant-time token auth;
  Telegram/Slack/Discord/Signal/SMS/Matrix/Mattermost/email/gmail transports; A2A server. Each
  channel needs its own credentials and is a **blocking foreground process** started manually.
- **Governance/security (21 caps).** WORKING: approval spine; Ed25519 signing; SecretRef;
  accounts + per-account isolation; AP2 mandate create/sign/co-sign/enforce; threat-model
  coverage check (CI-enforced over 130 tools); real vault encryption. PARTIAL: default public
  seed, unsigned assess-scope file, single-seed custody, weak KDF.
- **Persistence/ops (WORKING majority).** Atomic file KV + Postgres; **Ed25519-signed,
  hash-chained, byte-identical replay** (`replaystore`/`replaygate`); deterministic
  encrypted `tar.gz` backup + restore; supervised heartbeat with daemon-thread leases &
  watchdog; natural-language scheduler; standing goals; data-retention sweeps. *Autonomy loop
  is real but not self-starting.*
- **Trading core (18 caps, mostly WORKING & pure-stdlib).** Contracts/validation; risk engine;
  TA; signals; strategy; **look-ahead-safe backtester** (next-bar-open fills, `latency_bars≥1`,
  modeled slippage/fees); paper broker; OMS with write-through durable ledger; portfolio.
- **Self-improvement (24 caps).** WORKING but bounded: `evolve` numeric parameter self-tuning
  read live by ~20 modules; behavioral contracts; benchmark-gated skill curation; calibration;
  reflection. Inert/scaffold: scaffold code-evolution, codegraph oracle (empty by default).

---

## 4. Module-by-module teardown

### Core orchestration — `orchestrator.py` (3,151 loc) + council
- **Purpose/responsibilities:** the whole council pipeline; banners, watchdog wiring,
  compaction, replay, one-shot autonomous routines.
- **State:** **WORKING** — control flow is real and wired to `backend`; exercised by
  `test_orchestrator`/`test_answer_verify` **with model calls mocked**.
- **Public interface:** `Olympus.ask / ask_stream / ask_ephemeral → _compute_reply → _pipeline`.
- **Connects to:** every specialist via `backend.run_agent_counted`; codegraph oracle in the
  verify stage (`orchestrator.py:682,723`); replay/trace substrate.
- **Errors/anomalies:** verification **fails open** (ships unverified behind banner on any
  error); router can set `needs_verification=False` to skip Aletheia entirely; a
  self-documented shipped compaction-truncation defect (lines ~2072-2091); dozens of silent
  `except Exception: pass`.
- **Security/prod concerns:** the "hallucination controller" is an LLM judging LLM output; its
  substance is untested (mocked) and it is bypassable by inducing verifier errors.

### Provider/routing — `llm.py`, `backend.py`, `routesub.py`, `modelgrade.py`, `usage.py`, `config.py`
- **State:** backends **WORKING**; adaptive routing **PARTIAL/inert**.
- **Key anomalies:** `modelgrade.observe()` has **no production caller** (`modelgrade.py:390`) →
  qualification store permanently empty; `config.set_live_pricing`/`providers.fetch_pricing`
  are **test-only** (`config.py:794`) → cost from static tables, and **two price tables
  disagree** (`usage.PRICES` vs `config._PRICE_PER_MTOK`); `routesub.choose` **is** wired
  (`config.py:583`) despite a stale docstring saying "nothing calls this yet," but
  `mark_warm`/`record_outcome` are uncalled so it is wired-but-starved.
- **Egress choke:** `llm.py:339,442` call `security.assert_egress_allowed()` before every model
  call — real and fail-closed.

### Tools/actions/sandbox — `tools.py` (4,653 loc), `actions.py`, `sandbox.py`, `egress.py`
- **State:** approval spine **WORKING**; sandbox/egress **PARTIAL**.
- **Approval `_execute` gates (real, in order):** shadow-mode block (irreversible/financial) →
  scope check → daily rate limit → behavioral-contract re-check (recovery→HOLD) → audit at
  every branch (`actions.py:370-436`).
- **Anomalies:** the per-user `audit.jsonl` is a **plain append text file** (no signing/
  hash-chain), so `actions.py:18`'s "immutable audit log" is overstated for *that* file (the
  *decision* log is separately signed); `sandbox` default `local` backend runs with
  `shell=True`, full user privileges, full host env and network (`sandbox.py:407`) — **no OS
  isolation**; `egress.guard()` is **off by default** and covers only email/webhook/upgrade
  paths, not "every egress."

### Memory/knowledge — `memory.py`, `recall.py`, `embed.py`, `annindex.py`, `emem.py`, codegraph
- **State:** durable core **WORKING**; semantic layer **off by default**; codegraph **inert
  by default**.
- **Anomalies:** default `MEMORY_DIR` is **inside the repo/image** (`config.py:719`) → a
  container without a mounted volume loses "persistent" memory on restart (config validation
  *does* flag this); `usermem.render_card` age field is **always 0** (reads key `created`
  instead of `created_at`, `usermem.py:213`); `emem.context_block` and codegraph
  `verify_claim`/`context_block` have no auto-trigger so they operate over empty state.

### Governance/security — `approvals.py`, `mandate.py`, `witness.py`, `attest.py`, `assess.py`, `vault.py`, `ingestgate.py`
- **State:** signing/mandate/vault **WORKING**; several defaults **weak**.
- **Anomalies:** default signing seed is **public/forgeable** (integrity, not authenticity —
  disclosed; production boot refuses it when `OLYMPUS_ENV=production`); `assess.py` scope read
  from **unsigned** `authorizations.json` (`assess.py:117`) despite an "authorization is a
  signed fact" docstring; default user co-signer derived from the **same seed** as the system
  key (`mandate.py:284`) so "two-party" is nominal by default; vault KDF is **unsalted SHA-256,
  no work factor** (`vault`) so weak passphrases → weak at-rest protection; `ingestgate` off
  by default.

### Persistence/ops — `store.py`, `ledger.py`, `trace.py`, `replaystore.py`, `sessionlog.py`, `backup.py`, `heartbeat.py`, `watchdog.py`, `config.py`
- **State:** the persistence *core* is the **strongest, most carefully engineered part of the
  codebase**. `ledger.py` (content-addressed, Ed25519-signed, hash-chained, resume-and-diverge-refuse
  — `test_m2_ledger.py`), `sessionlog.py` (sealed hash-linked journal with torn-tail healing /
  corruption quarantine — `test_sessionlog_faults.py`), `backup.py` (encrypted+signed tar.gz with
  verify-before-commit restore and traversal refusal — `test_backup.py`), and `replaystore.py`
  (request/tool/context freezing — `test_replay_*`) are all real and exercised by real
  (non-mock-of-subject) tests that pass.
- **PostgresStore is unverified.** `store.py:70-110` is real code, but **no test touches it** and
  `psycopg` isn't installed here — the docstring's "verified against a live Postgres" left no
  artifact. The Postgres path is IMPLEMENTED_UNTESTED, so "database-ready is a config switch" is
  unproven.
- **`migrate.py` is a red herring** — not a DB/schema migrator but a *competitor-agent data importer*
  (OpenClaw/Hermes to Olympus). There is **no schema-migration mechanism** for the KV table (only
  `CREATE TABLE IF NOT EXISTS`).
- **`trace.py` is not crash-durable mid-run by design** — it flushes the whole decision log once at
  the end (`trace.py:146`); a run killed before flush loses its decision log (exactly the gap
  `ledger.py`'s per-step durability closes for checkpointed runs).
- **`replaygate.py` was PARTIAL and demonstrably mis-reporting — E25, now FIXED in 0.27.0.** Its pass/fail/skip control flow
  is tested, but every test **mocks `orchestrator.replay_run`**, so the composed "run live, then
  replay byte-identically" claim is never exercised. **I ran the real gate**
  (`scripts/reliability_gate.py`, RELEASING.md step 4) in a keyless environment: all 3 prompts
  reported `completed=True decisions=3` yet every decision carried
  `model_request_hash=None, model_response_ref=None, cost=0.0` and **zero response blobs were written**
  to `memory/responses/`. Replay then attempted a real model call, recomputed a hash, found nothing,
  and raised divergence — so the gate printed **"✗ RELIABILITY GATE NOT MET"** rather than the
  "⚠ INCONCLUSIVE" it is supposed to emit when the provider is unavailable. **Fixed:**
  `check_one` now classifies a run that froze no model call as a SKIP, and both operator entry
  points (`reliability_gate.py`, `tier1_exit_check.py`) refuse up front via `firstrun.configured()`
  — the guard `heartbeat.tick` already used. The composed live claim is still unproven (it needs a
  real key), but the gate no longer *lies* about it. See E25.
- **`watchdog.py` supervision is inert by default** (`OLYMPUS_WATCHDOG` unset ⇒ mode `off`), and its
  own docstring calls every threshold "PROVISIONAL… not derived from measurement of this system."
- **Unsafe config defaults** (E21/E22) — **both fixed in 0.27.0**: an unset budget is now bounded by
  `DEFAULT_DAILY_BUDGET` and the boot checklist now covers production too;
  `outcomes.record()` swallows exceptions **without** `errors.capture()` (`outcomes.py:43`), unlike
  every sibling module.
- **Anomalies:** heartbeat `run_forever()` is a real loop but **not self-starting** (needs
  `olympus heartbeat` or the compose `heartbeat` service — omitted from the staging compose, whose
  header admits it "has never been brought up"); off-host backup delivery requires
  `OLYMPUS_BACKUP_CMD` and the scheduled backup compose service is **commented out by default** →
  default deploy is single-host, total-loss-on-droplet-failure.

### Trading core — `trading/contracts.py`, `risk.py`, `backtest.py`, `oms.py`, brokers
- **State:** **WORKING, pure-stdlib** (claim verified — `C39`). Backtester is genuinely
  look-ahead-safe.
- **Anomalies:** `execution.py` "no way to manufacture a RiskDecision the engine didn't
  produce" is overstated — `RiskDecision` is a plain frozen dataclass, nothing cryptographically
  binds it (in-process trust only); "the same code runs in backtest, paper, **and live**" —
  the live half doesn't exist.

### Trading native model — `trading/native/` (isolation.py 2,029 loc, +12 modules)
- **State:** **research island.** Stdlib statistical model + gradient-boosted trees **train and
  run** (no torch needed); neural/multitask nets **need torch (absent) → never executed here**.
- **Anomalies (the sharpest in the repo):** `NativeForecaster`/`Trainer`/`GateLedger` have
  **zero construction sites outside tests** (`forecaster.py:97`, `serve.py:180`,
  `promotion.py:400`) — nothing serves it; on matched held-out eval the model's entire error is
  a **constant location offset** and it **loses to persistence, a linear fit, and a GBT**
  (`quarantine.py:113-175`, B8); **no real market data has ever been read** (B1) — everything is
  synthetic candles; promotion "PROMOTED" only appends an audit record, no serving code
  consults it, and `assert_promotable` structurally raises while B8/B9 are open; `train_neural`
  feeds the **test split as the per-epoch validation monitor** (`train.py`), weakening the
  "held-out influenced nothing" claim.

### Surfaces — `web.py` (3,103 loc), `cli.py` (3,188 loc), channels
- **State:** web/CLI **WORKING**; channels **credential- and manual-process-gated**.
- **Anomalies:** all request state in-process (scaling BROKEN); WhatsApp signature check
  **skipped when `WHATSAPP_APP_SECRET` unset** (`whatsapp.py`, unauthenticated by default);
  `webhook_gateway` binds `0.0.0.0` and serves **open** when its secret is unset; Google Chat
  verifies only a static bearer token, not the Google-signed JWT; `media.py` generative tools
  are **OpenAI-only** so an Anthropic-configured install gets "needs an API key."

---

## 5. Incomplete and half-built features

| Feature | Exists | Missing | Why incomplete | Reusable? | To complete | Depends on |
|---|---|---|---|---|---|---|
| **Codegraph "hallucination oracle"** | Full AST parser (`codegraph_ast.py:176`), builder, query, `verify_claim`, wired into Aletheia | Any automatic **build** trigger | No startup/heartbeat/scheduler job calls `build()`; CLI-only | Yes — high quality | Add a heartbeat/first-run build job + freshness re-index | heartbeat, store |
| **Measured routing (`modelgrade`/`routesub`)** | Full ledger, Wilson-bound qualification, substitution seam (wired at `config.py:583`) | A production caller of `observe()`/`record_outcome`/`mark_warm` | The wiring PR that emits verifier/eval outcomes never shipped | Yes | Emit outcomes from verify/eval into `observe()`; feed `record_outcome` | routing_outcomes, evolve |
| **Ingest gate** | Full reject-never-repair chokepoint, kind registry, schema/provenance/integrity checks | The default-on switch + confidence it won't break ingest | Deliberate default-off rollout flag never flipped | Yes | Flip `OLYMPUS_INGESTGATE` on after a soak; add call-site tests | store, security |
| **Scaffold code-evolution** | Propose+benchmark+archive+diff (`scaffold_evolve.py:127`) | A `generate` wiring **and** any apply path (absent by design) | Safety-gated to propose-only; nothing invokes it | Partially (propose reusable) | Wire a generator + human-review apply flow (like the Auto-Upgrade GH Action) | evals, benchmarks |
| **Episodic memory** | `reconstruct` + `context_block` (`emem.py`) | Any caller | Never wired into orchestrator/cli despite docstring | Yes | Inject `context_block` into prompt assembly under `OLYMPUS_EMEM` | memory, recall |
| **Semantic recall / ANN** | Real embeddings API + exact-cosine + pure-Python HNSW | Default enablement + a real corpus | Off by default; needs external embed endpoint + >256 items | Yes | Provide a default local embedder or document setup; auto-index | embed endpoint |
| **Native trading model** | Trainable stdlib model, GBT, neural nets, promotion gate, isolation harness | Real data ingestion, torch env, serving wiring, a model that beats baselines | Synthetic-only, torch absent, not wired to serving, hasn't learned | Harness reusable; model not | Real market data pipeline; train neural on GPU; wire promotion→serve; re-validate | torch, market data feed |
| **Kronos backend** | Adapter + runtime + FakeBackend | Real weights/torch/network | Upstream not installable, hub 403, torch absent | Adapter reusable | Vendor/install Kronos + weights; validate | torch, huggingface_hub |
| **Live pricing** | `fetch_pricing`/`set_live_pricing` | A startup/runtime install call | Implemented but never invoked | Yes | Call `set_live_pricing` at boot; unify the two price tables | providers |
| **Off-host durability** | `backup.create` + `OLYMPUS_BACKUP_CMD` hook + compose service (commented) | Enabled-by-default scheduled off-host backup | Left to the operator | Yes | Uncomment/enable the backup service; document DR | backup, store |

---

## 6. Errors and anomalies

*No CRITICAL code defects were found.* Severities are the analyzers' ratings after adversarial
verification (23 CONFIRMED / 16 PARTIAL-downgraded / 1 REFUTED of the 40 checked).

| ID | Sev | File | Component | Problem | Evidence | Root cause | Impact | Recommended fix |
|---|---|---|---|---|---|---|---|---|
| E1 | HIGH | orchestrator.py | Aletheia | Verification **fails open** — verifier/provider error ships unverified output behind a banner | `_verify_timed`/`_enforce_answer_verify` | Deliberate fail-open-but-visible (ADR 0005) | Flaky verifier ⇒ "fact-checked" answers are unchecked | Add a fail-closed mode option; alarm on verify-error rate |
| E2 | HIGH | orchestrator.py | Router | Router LLM can set `needs_verification=False`, skipping Aletheia | route decision → exemption ledger | Verification is model-gated, not policy-gated | The checked model decides whether checking happens | Policy-gate verification for risk classes; cap exemptions |
| E3 | HIGH | modelgrade.py:390 | Routing evidence | `observe()` has no prod caller → store永 empty, members永 `UNTESTED` | grep: only `tests/test_modelgrade.py` | Wiring PR never shipped | `OLYMPUS_MODELGRADE`/`ROUTESUB` change nothing in prod | Emit verify/eval outcomes into `observe()` |
| E4 | HIGH→MED | config.py:794 / usage.py:34 | Cost | Live pricing never wired; two static price tables disagree | `set_live_pricing` test-only; `usage.PRICES` vs `config._PRICE_PER_MTOK` | Divergent static tables | Budget guard & "cheaper candidate" economics can be wrong | Wire live pricing at boot; unify tables |
| E5 | HIGH | ingestgate.py:273 | Ingest defense | Off by default; every call site is a pass-through | `enabled()` default OFF; `check()` returns payload unchanged | Rollout flag never flipped | Zero protection vs poisoned durable artifacts by default | Default-on after soak; add bypass tests |
| E6 | HIGH→MED | assess.py:117 | Assess scope | Scope enforced from **unsigned** plaintext `authorizations.json` | `_load_auths` no sig check; `grant`/`in_scope`/`require_scope` | "Signed" lives only on the decision-log action, not enforcement | Local file tamper grants scanner arbitrary-target reach | Verify a signature/HMAC on the scope file at read |
| E7 | HIGH | scaffold_evolve.py:127 | Code self-evolution | `propose()` never called in prod; no apply path | grep: tests only | Not wired | "Self-improving code" unrealized | Wire generate + human-review apply (GH Action pattern) |
| E8 | HIGH | codegraph.py | Hallucination oracle | Graph never auto-built ⇒ oracle over empty graph, returns UNKNOWN | no `build()` caller at startup/heartbeat | Opt-in CLI only | Code-claim verification inert out of the box | Auto-build on first run/heartbeat |
| E9 | HIGH | native/forecaster.py:97 | Native serving | Model has **zero construction sites outside tests** | `serve.py:180`, `promotion.py:400` | Research island | "Model serves/promotes/self-improves" is inert | Wire forecaster resolution → serving; consult promotion gate |
| E10 | HIGH | native/quarantine.py:113 | Native quality | Model hasn't learned — constant offset, loses to persistence/linear/GBT (B8) | matched eval MAE 0.0256 vs bias +0.0250 (~2.7σ) | Synthetic-only training, no conditional signal | No evidence it beats a random walk | Train on real data; re-run matched eval; keep gates closed |
| E11 | HIGH | web.py:200 | Scaling | Sessions/rate-limits/metrics in-process | `_SESSIONS`/`_HITS`/`_DAILY`, `metrics` globals | State not in shared store | Cannot horizontally scale; cross-replica limit bypass | Move state into `store` backend (Redis/Postgres) |
| E12 | HIGH→LOW | sandbox.py:407 | Sandbox | Default `local` backend = **no OS isolation** (`shell=True`, full env/net) | `backend()` default `local`; run() local branch | Local backend is not a sandbox (documented) | Approved command can exfiltrate host secrets/reach net | Default to docker/native confinement; warn loudly on `local` |
| E13 | MED | whatsapp.py | Channel auth | Inbound signature check skipped when `WHATSAPP_APP_SECRET` unset | `valid_signature` returns True | Optional-secret fallback | Unauthenticated council invocation if webhook exposed | Fail closed when secret unset |
| E14 | MED | webhook_gateway.py | Channel auth | Binds `0.0.0.0`, serves open when secret unset | `run_server` no refusal/loopback fallback | Missing startup gate | Open unauthenticated endpoint if launched directly | Refuse to bind non-loopback without a secret |
| E15 | MED | memory.py / config.py:719 | Memory durability | Default `MEMORY_DIR` inside repo/image | container without mounted volume | Default path choice | "Persistent memory" lost on container restart | Default to a volume path; loud readiness warning (partly present) |
| E16 | MED | actions.py:18 | Action audit | `audit.jsonl` plain append text, not signed/hash-chained | file is append-mode text | Not tamper-evident | "Immutable audit log" overstated for this file | Sign/hash-chain the action audit like the decision log |
| E17 | MED | vault | Secret KDF | Unsalted SHA-256, no work factor from passphrase | `vault` key derivation | Weak KDF | Weak passphrase ⇒ weak at-rest protection | Use scrypt/argon2 with salt |
| E18 | MED→LOW | mandate.py:284 | Custody | Default co-signer derived from same seed as system key | vault subkey from custody seed | Single trust domain by default | "Two-party" nominal unless `OLYMPUS_MANDATE_*` set | Require a distinct co-signer key by default |
| E19 | LOW | usermem.py:213 | Profile card | Age always 0 (reads `created` not `created_at`) | `render_card` | Key typo | Misleading age in transparency card | Fix key name |
| E20 | INFO×~222 | many | Reliability | ~222 silent `except: pass` swallows | orchestrator(13)/memory(11)/browser(10)… | Best-effort defensiveness | Failures invisible; harder debugging/observability | Log at debug; narrow exception types |
| E21 | HIGH — **FIXED in 0.27.0** | config.py:871 | Budget default | `OLYMPUS_DAILY_BUDGET=0` means **unlimited spend, not off** | `DAILY_BUDGET = float(os.environ.get("OLYMPUS_DAILY_BUDGET","0") or 0)`; compose comment "0 means UNLIMITED, not off" | `0` chosen as the "off" sentinel for a safety cap | A fresh install has no spend ceiling once heartbeat LLM cadences fire | Ship a conservative non-zero default; require an explicit `unlimited` token to disable |
| E22 | HIGH | config.py:1728 | Prod boot validation | Fail-closed checklist (budget/credential/retention) runs **only** under `OLYMPUS_ENV=staging`; production boot only checks the signing seed | `staging_problems()` gated by `is_staging()`; no `require_production_config` exists (grep) | Staging hardening never extended to production | A production instance can boot with unlimited budget, no off-loopback credential, infinite retention | Run the same checklist (or a superset) under `is_production()` |
| E23 | HIGH | store.py:70 | Postgres backend | PostgresStore has **zero test coverage**; `psycopg` not installed; "verified against live Postgres" unsubstantiated | no `PostgresStore`/`OLYMPUS_DATABASE_URL` reference in `tests/`; `import psycopg` fails | Optional lazy backend never wired into CI | The advertised scale/persistence path is unverified; bugs surface first in production | Add a testcontainers/compose Postgres integration test of the store contract |
| E25 | HIGH — **FIXED in 0.27.0** | replaygate.py:151 / scripts/reliability_gate.py | Release gate | The mandatory release reliability gate **cannot produce a valid verdict without a provider key, and misclassifies that state as a genuine reliability failure** | Live run: 3/3 prompts `completed=True decisions=3` with `model_request_hash=None, resp_ref=None, cost=0.0`; `memory/responses/` empty; verdict printed `✗ RELIABILITY GATE NOT MET`. `genuine_failures()` (replaygate.py:151-153) treats every non-`skipped` failure as genuine, and a keyless degraded run is never marked `skipped` | In a keyless environment the pipeline completes with zero recorded model calls, but replay still takes the model path and diverges; the skip/INCONCLUSIVE detector only recognises provider errors, not the keyless-degraded path | RELEASING.md step 4 is unpassable and, worse, actively misleading: an operator is told the release is unreliable when the gate simply never ran. Blocks a release for the wrong reason | Mark a run with zero recorded model calls as `skipped`/INCONCLUSIVE; assert a provider key up front and refuse to run rather than emitting a hard FAIL |
| E24 | MED | outcomes.py:43 | Error visibility | `record()` swallows all exceptions **without** `errors.capture()`, unlike sibling modules | bare `except Exception: pass` | Inconsistent error-capture convention | A broken store backend fails invisibly with no operator trace | Route through `errors.capture('outcomes.record', err)` |

*(38 MEDIUM, 33 LOW, 10 INFORMATIONAL findings total across all subsystems; the table shows the load-bearing ones. Persistence-ops was re-analyzed after an initial degenerate run, adding E21–E24; E25 came from actually executing the release reliability gate.)*

---

## 7. Broken or misleading claims

The pattern is consistent: **the code is honest at the docstring/comment level; the README
headline over-generalizes default-install behavior.** 16 of 40 verified items came back
PARTIAL specifically because "the code openly discloses this — it is an opt-in design, not a
mismatch." The genuinely misleading gaps:

| # | Claimed | Actual | Evidence | Risk |
|---|---|---|---|---|
| 1 | "Hallucination controller" verifies claims | LLM judges LLM; **fails open**; router can skip it; tested only vs fixed-JSON stub | orchestrator `_verify*`; `test_calibration.py:486` | Users over-trust unverified answers |
| 2 | "Self-improving" | Runtime self-improvement = **bounded numeric parameter tuning** + non-applied code proposals + benchmark-gated skill swaps; effect on quality **never measured**; code-evolution never runs | `evolve._set_param`; `scaffold_evolve` unreached | Overstates autonomy/learning |
| 3 | "Provable zero-egress" | Real & fail-closed but **application-layer only**; a dependency opening its own socket bypasses it | `security.assert_egress_allowed`; README:474 | "Provable" overstates an app-layer control |
| 4 | Code knowledge graph verifies claims | Wired but runs over an **empty graph** unless manually built | `codegraph.build` no auto-caller | Inert oracle in fresh deploy |
| 5 | "Persistent memory survives restarts" | Only if a volume is mounted / `OLYMPUS_MEMORY_DIR` set; default path is inside the image | `config.py:719` | Silent data loss on container restart |
| 6 | Measured/learned routing improves selection | Default off; data-starved (`observe()` never called); learned routing needs 300+ outcomes | `modelgrade.py:390`; `learned_routing` gate | No behavioral change out of the box |
| 7 | AP2 "Authorize a payment" | Signs a mandate; **moves no money** (no payment rail) | `builtin_actions.py:339` | Name implies payment capability |
| 8 | Native model trains/promotes/serves & is self-improving | Synthetic-only, loses to baselines, neural never executed, **not wired to serving** | `native/quarantine.py`, `forecaster.py:97` | Implies a live market model that doesn't exist |
| 9 | Kronos "K-line forecasting backend" | No real model ever run; torch/weights absent; only `FakeBackend` | `kronos_runtime.py` | Overstates a forecasting capability |
| 10 | Sandbox = "confined workspace" | Default `local` backend = no OS confinement | `sandbox.py:407` | Approved command can exfiltrate/reach net |
| 11 | Assessment scope is a "signed fact" | Enforced from an unsigned plaintext file | `assess.py:117` | Tamper-able authorization |
| 12 | "native-market-intelligence" workflow | A CI **pass/fail gate**; produces no forecasts/sentiment/reports | `.github/workflows/native-market-intelligence.yml` | Reader expects an intelligence product |
| 13 | Attestation is a "provable chain to a real human" | Under the default public seed the signing key is forgeable and unpinned verification self-validates | `attest.py`; `witness.py:39` | "Provable human clearance" unmet by default |

---

## 8. Deployment-readiness assessment

| Area | Rating | Basis |
|---|---|---|
| Containerization | **PARTIAL** | Real hash-pinned Dockerfile; runs as **root**, **no `HEALTHCHECK`** directive (endpoints exist though) |
| Cloud config / orchestration | **NOT_READY** | Single manually-provisioned VPS; hardcoded domain in Caddyfile; no orchestrator/IaC |
| Environment management | **READY** | Env handling + boot/staging validation are real and tested |
| Secret management | **PARTIAL** | Real vault + SecretRef, but default signing seed is public/forgeable; weak KDF |
| DB persistence | **PARTIAL** | "Database-ready" covers **KV only**; decision logs, conversation history & user memory stay on local FS |
| Migrations | **NOT_PRESENT** | No migration framework (OK for fixed KV; gap for evolving schemas) |
| Backups | **PARTIAL** | Real archive/encrypt/restore, tested; **off-host delivery needs `OLYMPUS_BACKUP_CMD`; scheduled service commented out** |
| Disaster recovery | **PARTIAL** | Restore logic solid & unit-tested; DR never rehearsed; single-host by default |
| Logging | **PARTIAL** | Adequate local logging; **no aggregation/shipping/alerting** |
| Metrics | **PARTIAL** | Real but **ephemeral, single-process**; no metrics backend |
| Tracing (OTLP) | **PARTIAL** | Export code real & content-safe; never validated against a live collector |
| Health checks | **READY** | `/healthz` (liveness) + `/readyz` (probes write-ability) real & tested |
| Scaling | **NOT_READY** | Architecturally single-process (in-memory sessions/limits/metrics) |
| Rate limiting | **PARTIAL** | Real per-IP sliding window (default 8/min); **not shared across replicas** |
| Authentication | **READY** | Constant-time token compare; fail-closed off-loopback; tested |
| Authorization | **PARTIAL** | Sound per-account isolation & fail-safe defaults; **no RBAC** |
| Network security | **PARTIAL** | Reasonable single-box perimeter (Caddy TLS, SSRF guard); no WAF; egress guard app-layer only |
| CI/CD | **PARTIAL** | **Rich CI** (4-version matrix, hash-pinned, compileall, capability/threat-model checks, replay/noninterference/quality gates) **and a working tag-triggered PyPI publish** (9 releases shipped, latest 0.26.0); the gap is **no automated *deploy*** of a running instance |
| Rollback | **PARTIAL** | Self-improvement/skill rollback exists; infra rollback manual |
| Data retention | **READY** | Real sweeps; honest "unset = reported blocked state"; tested |
| Audit logs | **PARTIAL** | Decision log signed/hash-chained (tamper-evident); **action `audit.jsonl` is plain append** |

**Verdict:** deployable as a **single-node, single-tenant** service today; **not** production-
ready for multi-instance, HA, or untrusted-multi-tenant use.

---

## 9. Test-quality assessment

- **Discovered:** 8,531 tests across 396 files.
- **Executed:** 8,531. **Passed: 8,363. Failed: 0. Errors: 0. Skipped: 168.** (316s;
  `python -m pytest`.)
- **Skips** are all environment-gated, not broken: `torch`/`native` (~6 files), docker sandbox
  (~6), real Chromium (`BROWSER_SMOKE`/`REAL=1`), live cloud creds (Azure, boto3/Bedrock),
  MCP/websockets/openai integration, psycopg-absent path, and 11 POSIX-only cases.
- **Integration/e2e coverage:** present in structure (orchestrator instantiated, `.ask()`
  called) **but** the model is a **stub returning constant JSON** (`test_calibration.py:486`
  `_stub_client`). So the pipeline *plumbing* is tested; **no test proves any model-dependent
  decision** (verdict/mode are hardcoded to `approve`/`direct`).
- **Mock-only, on critical controls:**
  - Verification/routing correctness → stub client only (**MOCK_ONLY**).
  - Egress-zero → asserts against `_FakeSMTP`/`_FakeOpener` with the URL blocker stubbed
    (`C50`) — no real network boundary exercised.
  - Sandbox isolation → only the `local` (non-sandbox) backend runs; docker/native-confinement
    tests **skip** (`C49`) — **the isolation control has zero passing evidence here**.
- **Security tests:** exist (subagent privilege guard, capability separation, auth, SSRF,
  sovereign filtering, threat-model coverage) and many assert real behavior; but the two
  highest-stakes controls (sandbox isolation, real egress actuation) are mock/skip.
- **Pure-logic quality is excellent:** consensus (24), dytopo (13), treesearch (17), toolselect
  (11), toolcall_repair, contracts, effortscore, backtester — real deterministic assertions.
- **Missing critical coverage:** real-model verification behavior; sandbox confinement;
  real egress; native-model-vs-baseline on real data; multi-instance persistence.

**Bottom line:** the suite proves the **deterministic plumbing** thoroughly and the
**model-dependent intelligence/safety** claims not at all.

---

## 10. What Olympus can do right now (genuinely usable today)

1. Answer questions / run agentic tasks through a real multi-agent pipeline against Anthropic,
   OpenAI-compatible, Bedrock, MoA-ensemble, or fully-local (Ollama) providers (BYOK).
2. Persist conversations, memory, facts, profile, accounts and vault to a durable local file
   store (atomic, `0600`, locked) — or Postgres for the KV namespaces.
3. Prepare → approve → execute → undo governed actions with real layered safety gates (shadow
   mode, scope, daily limits, behavioral-contract re-check, audit) and human-in-the-loop halts
   for irreversible/financial actions.
4. **Sign and byte-for-byte replay every decision** and verify the signature (`olympus verify`);
   CI enforces replay determinism per-PR.
5. Enforce **fail-closed zero-egress** at Olympus's own network choke points (remote models
   excluded from the pool; blocked egress raises) plus an SSRF guard on outbound fetches.
6. Run a **look-ahead-safe trading backtester** and a functional **paper broker** with a real
   risk engine, TA, signals, strategies, OMS and portfolio — all pure-stdlib.
7. Serve a browser chat UI, an **OpenAI-compatible `/v1` endpoint**, an admin panel, `/healthz`
   + `/readyz`, and per-IP rate limiting on a threaded stdlib server (with constant-time token
   auth).
8. Bridge to Telegram, WhatsApp, Slack, Discord, Signal, SMS, Matrix, Mattermost, Google Chat,
   and email/Gmail (each with its own credentials, as a manually-started process).
9. Self-tune a set of **numeric runtime parameters from recorded outcomes** (verifier count,
   memory-fragment caps, exploration rate, backoffs, etc.) that genuinely change behavior.
10. Run a supervised **heartbeat** loop (once started) that drives the scheduler, web monitors,
    goals, learning, and a weekly self-audit; back up memory to an encrypted archive; import
    agentskills.io skills; and expose itself as an MCP server.
11. Generate real embeddings + exact-cosine semantic recall — **if** an embedding endpoint is
    configured; run the code knowledge graph over any repo — **if** you run `olympus codegraph
    build`; use MCP tools — **if** the `mcp` SDK is installed.
12. Train and evaluate the **stdlib** trading forecasters (persistence, drift, seasonal-naive,
    gradient-boosted trees) on data you supply — no torch required.

---

## 11. What Olympus cannot do yet

1. **Run as a scalable/HA service.** Single-process only; a second replica breaks
   sessions/rate-limits/metrics.
2. **Guarantee memory durability out of the box.** Default `MEMORY_DIR` lives in the image;
   without a mounted volume, restart loses it. No default off-host backup.
3. **Prove it catches hallucinations.** Aletheia is untested against real models, fails open,
   and is router-skippable.
4. **Protect against poisoned durable artifacts by default.** The ingest gate is off.
5. **Improve its own code autonomously.** Scaffold evolution never runs and cannot apply.
   "Self-improvement" is bounded parameter tuning + benchmark-gated skill swaps, with no
   measured quality delta.
6. **Route by measured model quality.** The evidence store is never populated in production.
7. **Forecast real markets with its native model.** Synthetic-only, unwired to serving, loses
   to a random walk, neural path never executed; Kronos never run.
8. **Trade real money.** No live broker adapter; the testnet adapter has never completed a
   request.
9. **Self-update to un-audited code safely.** `olympus upgrade` *does* work (the package is published; see note), but it upgrades to whatever is latest with no signature check on the wheel at install time beyond `olympus verify` being run manually afterward.
10. **Isolate untrusted code by default.** The default sandbox backend provides no OS
    confinement; docker/native backends are untested here.
11. **Offer verified authenticity by default.** The default signing seed is public/forgeable;
    assessment scope is an unsigned file; custody is single-seed.
12. **Ship class-aware egress/PII routing, semantic recall, episodic memory, or codegraph
    verification** without operator opt-in and setup — all inert on a default install.
13. ~~Boot safely by default.~~ **Fixed in 0.27.0**: unset budget is now bounded, and production
    boot enforces the same fail-closed checklist staging gets.
14. **Rely on the Postgres backend.** It has zero test coverage and `psycopg` isn't even installed
    in the dev environment; a flip to `OLYMPUS_DATABASE_URL` would be its first real exercise.
15. **Supervise wedged jobs out of the box.** The watchdog is off by default and its thresholds are
    self-described as uncalibrated guesses.

---

## 12. Prioritized remediation plan

### P0 — Deployment / security blockers
- **P0-1 Externalize request state.** *Files:* `web.py` (`_SESSIONS`/`_HITS`/`_DAILY`),
  `metrics.py`. *Dep:* `store.py`/Redis. *Acceptance:* two replicas behind a LB share sessions,
  rate limits and metrics; kill-one-replica keeps sessions. *Tests:* multi-instance session +
  rate-limit-across-replicas. *Risk if ignored:* cannot scale; rate limits bypassable.
- **P0-2 Fix default persistence.** *Files:* `config.py:719`, `Dockerfile` (`VOLUME`),
  `deploy/*`. *Acceptance:* default deploy persists memory across container restart; `/readyz`
  fails if `MEMORY_DIR` isn't durable. *Tests:* restart-survival integration. *Risk:* silent
  total data loss.
- **P0-3 Enforce a production signing seed + refuse forgeable defaults.** *Files:* `witness.py`,
  `attest.py`, `config.is_production`. *Acceptance:* production boot refuses the public seed
  (already partly present) and attestation/verify pin to it. *Tests:* boot-refusal + pinned
  verify. *Risk:* forgeable "provable" attestations.
- **P0-4 Fail closed on unauthenticated surfaces.** *Files:* `whatsapp.py`,
  `webhook_gateway.py`, `googlechat.py`. *Acceptance:* refuse to serve / reject inbound when the
  verifying secret is unset (unless explicitly loopback). *Tests:* unauthenticated-POST rejected.
  *Risk:* open council endpoints, key burn.
- **P0-5 Sign the assessment scope file (or move enforcement to the decision log).** *Files:*
  `assess.py:117-245`. *Acceptance:* `require_scope` verifies a signature/HMAC before granting.
  *Tests:* tampered `authorizations.json` denied. *Risk:* scanner reach to arbitrary hosts.
- **P0-6 Default to a real sandbox backend (or block irreversible exec on `local`).** *Files:*
  `sandbox.py:66,407`, `cmdguard.py`. *Acceptance:* `local` cannot run approved commands without
  an explicit `OLYMPUS_SANDBOX=local-unsafe` acknowledgement; docker/native default when
  available. *Tests:* provisioned docker-sandbox lane in CI. *Risk:* host-secret exfiltration.
- **P0-7 Fix the unlimited-spend default.** *Files:* `config.py:871`. *Dep:* none.
  *Acceptance:* a fresh install has a finite daily budget; disabling the cap requires an explicit
  `unlimited` token, not `0`. *Tests:* boot with no env → budget finite; `0` no longer means
  unlimited. *Risk if ignored:* unbounded API spend the moment heartbeat LLM cadences start.
- **P0-8 Apply the fail-closed boot checklist to production.** *Files:* `config.py:1728-1808`
  (`staging_problems`), `cli.py:1017-1050`. *Acceptance:* `OLYMPUS_ENV=production` refuses to boot
  with unlimited budget, no off-loopback credential, or infinite retention — the same gate staging
  already gets. *Tests:* production-boot refusal per condition. *Risk:* a production instance
  starting wide open on all three.
- **P0-9 Verify the Postgres backend before relying on it.** *Files:* `store.py:70-110`, CI.
  *Acceptance:* a testcontainers/compose Postgres lane exercises the full store contract
  (put/get/delete/keys, upsert, concurrency). *Risk:* the advertised scale/persistence path fails
  first in production.

### P1 — Core functionality failures
- **P1-1 Make the hallucination controller policy-gated + optionally fail-closed.** *Files:*
  `orchestrator.py` (`needs_verification`, `_verify_timed`, `_enforce_answer_verify`).
  *Acceptance:* risk-classed answers cannot skip verification; a fail-closed mode blocks on
  verifier error; exemptions are capped and alarmed. *Tests:* forced verifier-error → blocked,
  not banner-shipped. *Risk:* the flagship safety property is bypassable.
- **P1-2 Prove verification with real models.** *Files:* `tests/` — add a varying fake-model
  and/or recorded real-model replay fixtures. *Acceptance:* tests fail if verify/mode logic
  regresses on model-dependent input. *Risk:* untested safety claim.
- **P1-3 Wire the codegraph auto-build.** *Files:* `heartbeat.py`, `firstrun.py`, `codegraph*`.
  *Acceptance:* fresh install has a populated graph after first run/heartbeat; `verify_claim`
  returns non-UNKNOWN. *Tests:* build-on-first-run. *Risk:* inert oracle.
- **P1-4 Populate the routing evidence store.** *Files:* emit outcomes from verify/eval into
  `modelgrade.observe()`; call `routesub.mark_warm`/`record_outcome`. *Acceptance:* after N
  runs, members leave `UNTESTED` and substitution can fire. *Tests:* end-to-end qualification.
  *Risk:* routing features do nothing.
- **P1-5 Unify cost tables + wire live pricing.** *Files:* `usage.py:34`, `config.py:794`,
  `providers.fetch_pricing`. *Acceptance:* one price source; `set_live_pricing` called at boot.
  *Tests:* budget guard uses live/unified prices. *Risk:* wrong spend/budget economics.

### P2 — Incomplete integrations & reliability gaps
- **P2-1 Enable ingest gate by default (after a soak).** *Files:* `ingestgate.py`. *Acceptance:*
  poisoned skill/plugin/memory-import rejected on default install. *Tests:* malicious-artifact
  rejection at each call site.
- **P2-2 Off-host backup by default.** *Files:* `deploy/docker-compose.yml` (uncomment backup),
  `backup.py`. *Acceptance:* scheduled encrypted off-host backup runs by default; documented DR.
  *Tests:* backup→restore round-trip on a clean host.
- **P2-3 Sign/hash-chain the action audit log.** *Files:* `actions.py`. *Acceptance:*
  `audit.jsonl` is tamper-evident like the decision log. *Tests:* tamper detection.
- **P2-4 Wire episodic memory + semantic recall defaults (or document as opt-in clearly).**
  *Files:* `emem.py`, `recall.py`, `embed.py`. *Acceptance:* `context_block` reachable;
  README labels semantic features as setup-required.
- **P2-5 Strengthen custody + vault KDF.** *Files:* `mandate.py:284`, `vault`. *Acceptance:*
  distinct co-signer key by default; scrypt/argon2 with salt. *Tests:* two-seed enforcement.

### P3 — Testing, observability, operational
- **P3-1** Provisioned CI lanes for docker-sandbox, real-egress actuation, and (GPU) native
  training so the skipped critical controls gain passing evidence.
- **P3-2** Metrics/log/trace backends: ship to Prometheus/OTLP collector + alerting; validate
  OTLP against a live collector.
- **P3-3** Reduce the ~222 silent `except: pass` swallows to logged, narrowly-typed handlers.
- **P3-4** Add a `HEALTHCHECK` to the Dockerfile; run the container as non-root.
- **P3-5** Fix `usermem.render_card` age key (`created`→`created_at`); clean up stale
  "ships inert" docstrings that contradict actual wiring (`routesub`, `ctxheat`).

### P4 — Enhancements / advanced capabilities
- **P4-1** Real market-data pipeline + GPU training for the native model; re-run matched
  evaluation; keep promotion gates closed until it beats baselines; wire promotion→serving.
- **P4-2** Vendor/install Kronos + weights and validate end-to-end (or clearly mark it as a
  research adapter).
- **P4-3** Wire scaffold code-evolution to the Auto-Upgrade GH Action pattern (propose → PR →
  human review) so "code self-improvement" has a real, safe path.
- **P4-4** RBAC on the admin/authorization surface. (Publishing is already in place: the
  tag-triggered workflow has shipped 9 PyPI releases, so `olympus upgrade` works.)
- **P4-5** Kernel/proxy-level egress enforcement (not just app-layer) to make "provable
  zero-egress" hold against third-party sockets.

---

### Appendix — audit provenance
- Build/health: `python -m compileall olympus` ✓; `python -m olympus capabilities --check` ✓
  ("manifest and README match code"); `scripts/check_threat_model.py` ✓ (130 tools covered).
- Full suite: 8,363 passed / 168 skipped / 0 failed (`python -m pytest`, 316s).
- Analysis: 14 subsystem analyzers (persistence-ops re-run after a degenerate first pass) + 40
  adversarial verifiers (23 CONFIRMED / 16 PARTIAL / 1 REFUTED); ~254 capabilities classified;
  ~102 findings (0 CRITICAL / 21 HIGH / 38 MEDIUM / 33 LOW / 10 INFO).
- No repository code was modified during this audit (report added under `docs/`).
- One diagnostic was executed that writes local state: `scripts/reliability_gate.py` (3 runs). It
  wrote only into the gitignored `memory/` directory (traces + heartbeat state) — nothing tracked
  changed, so no revert was required. Its result is finding E25.

**Correction (post-publication).** An earlier revision of this report claimed
`olympus-council` was "not a published PyPI release" and that `olympus upgrade`
therefore fails. That was wrong: the package **is** published, with **9 releases
(0.17.0 → 0.26.0)**, and the tag-triggered `publish.yml` workflow demonstrably
works. The affected claim row, the CI/CD rating, the §11 entry, and remediation
item P4-4 have been corrected. The distribution pipeline is a **working** part of
this project; "never deployed" refers to a running cloud instance, not to
packaging.
