# Changelog

All notable changes to Olympus are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Olympus adheres to [Semantic Versioning](https://semver.org/). The single
source of truth for the current version is `pyproject.toml`; see
[docs/SUPPORT.md](docs/SUPPORT.md) for the versioning, LTS, and release-integrity
policy, and [RELEASING.md](RELEASING.md) for how a release is cut.

Categories: **Added** (new capabilities), **Changed** (behaviour changes),
**Fixed** (bug fixes), **Security** (hardening). A `MAJOR` bump that changes the
CLI surface, the public Python API, or the on-disk memory `schema_version`
carries a migration note here.

## [Unreleased]

> **Release-state note.** As of this writing the latest *published* release is
> **0.21.0** (git tag `v0.21.0`, PyPI `olympus-council 0.21.0`). The `0.22.0`
> and `0.23.0` sections below were prepared and dated but **never tagged or
> published** — so they are not releases yet, and everything from `0.22.0`
> down to this note is effectively unreleased pending a tagging decision (see
> RELEASING.md). The dated headers are kept for review; re-date and tag them
> when a release is actually cut. `pyproject.toml` currently reads `0.23.0` as
> the in-development version, not a shipped one.

### Security — close operator API-key exfiltration and BYOK bypass

- `Settings.merged` dropped inherited credentials only on a *provider* switch,
  so a same-provider `base_url` override kept the operator's env key and sent it
  to a visitor-supplied host; the Anthropic SDK also fell back to
  `ANTHROPIC_API_KEY` when the key was cleared. Now an endpoint change drops the
  inherited key, and `llm.client` passes an empty key to a custom `base_url` so
  it fails closed. `web._brought_own_key` counts only a *primary* `api_key` as
  BYOK (a bare `base_url` or an `extra`-model key no longer unlocks the free /
  `OLYMPUS_REQUIRE_BYOK` wall while the primary pipeline runs on the operator's
  key). `/api/login|register|logout` are now rate-limited (were dispatched
  before the limiter — password brute-force + PBKDF2 CPU-DoS).
  `tools._read_source_file` uses path-component containment (a string-prefix
  check accepted sibling dirs like `<root>-backup/`). `spawn_subagent` inherits
  the calling run's credentials so a BYOK visitor's subagent runs on their key.
- Packaging: added `.dockerignore` (keeps `.env`/`deploy/.env`/`keys.sh`, `.git`,
  and local `memory/` out of the image); the Dockerfile now installs the
  hash-pinned `requirements.lock` and the package itself; the Auto-Upgrade
  workflow is gated on `author_association` so an issue body can't drive its
  write-capable coding agent.

### Fixed — replay/verify, routing, and a batch of correctness bugs

- Replay now freezes and restores conversation history and fast-mode and records
  the model that actually ran each stage, so `olympus replay` / `verify --log`
  no longer diverge spuriously on multi-turn or fast-mode runs; `_plan` no
  longer swallows a `ReplayDivergence`.
- `olympus verify` now detects files *added* since signing (injected-file
  detection), and dev-posture runs verify against the default seed's key so they
  don't fail once the instance sets `OLYMPUS_SIGNING_SEED`.
- Added the server-side `web_fetch` tool on the Anthropic provider (the
  verifiers were told to call it but it wasn't declared); the router no longer
  reports unparseable route JSON as a safety refusal.
- Misc: prompt-restore no longer corrupts a prompt on a multi-line update
  reason; `operator.authorized` matches subdomains of an authorized site;
  schedule confirmations render the coarsest unit (`7d`, not `168h`); backup
  closes a leaked temp fd; `cli restore` reports failures instead of a raw
  traceback and distinguishes an invalid signature from unsigned;
  `codegraph_path` returns a message for unknown symbols; the LLM client cache
  is bounded and the final retry no longer sleeps; the web session cap
  FIFO-evicts instead of wiping every user's session; the per-session event
  buffer is bounded; and a rejected chat (400/402) no longer burns a daily-chat
  quota slot.

### Changed — documentation honesty and drift guards

- Corrected the "server-side sandbox, never on your machine" claim across README,
  `docs/THREAT_MODEL.md`, and the code-eval path — model code runs approval-gated
  as a local subprocess by default (opt-in `OLYMPUS_EXEC_BACKEND=docker` for OS
  isolation). Fixed the "26 tools" → 60 count and the "12/11 specialists" → live
  count, and added CI-enforced guards (`threatmodel.check`, interpolated UI
  count) so both can't drift again. Marked the shipped design docs as
  implemented (were "proposed"/"not wired up").

## [0.23.0] — unreleased (prepared; not tagged or published)

### Added — Optional in-run tool-transcript compaction (`olympus/transcript.py`)

- Closes the last context boundary: the chat layer already compacts old
  *conversation* turns; this compacts within a **single agent run** so a
  tool-heavy loop (many/large `web_fetch`/`read_file`/codegraph results) doesn't
  drown in its own scrollback. When the run's messages exceed a budget, the
  **contents of older `tool_result` blocks are shrunk in place** while the most
  recent ones stay verbatim. **Off by default** (`OLYMPUS_INRUN_COMPACT`;
  `elide`/`1` = deterministic, `summarize` = LLM summary of old results;
  `OLYMPUS_INRUN_BUDGET`, `OLYMPUS_INRUN_KEEP_RECENT`).
- **Replay-safe by construction:** it never removes/reorders messages and never
  separates a `tool_use` from its `tool_result` (only the content string of old
  tool_results changes), so pairing/alternation invariants hold; and it is a
  pure function of the message stream, which is identical under replay (tool
  results are frozen) — so downstream request hashes match and replay stays
  byte-identical. The optional summarizer routes through the already-frozen
  `backend.complete_text`, so "summarize" mode replays deterministically too.
  Documented requirement: replay a recorded run with the same
  `OLYMPUS_INRUN_COMPACT` setting it was recorded with (moot at the default).
- Not needed today (the 16-iteration cap already bounds a run); this is for when
  you raise `MAX_AGENT_ITERATIONS` for deep autonomous single-runs. Covered by
  `tests/test_transcript.py`. No capability-count change.

### Changed — Strengthen the 13 specialists (three levers)

1. **Sharper prompts.** The seven thinnest specialist prompts (plutus, peitho,
   aegis, chiron, chronos, argus, mnemosyne) gain a focused "nail these" block
   tied to the concrete behaviors the deepened benchmark rewards — e.g. Plutus's
   match→high-APR-debt→buffer→invest ordering and break-even rule, Aegis's
   "never click the link / ordered incident response", Chronos's "no perfect
   cross-timezone time → rotate". Additive, identity-preserving sharpening.
2. **Per-specialist model role + effort (data-driven tiering).** `Specialist`
   gains `role` ("reasoning" | "coding" | "verify") and `effort` fields; model
   routing (`ModelPool.for_specialist`) now reads the role from the registry
   instead of a hardcoded map, and the orchestrator passes each specialist's
   effort. Defaults preserve today's behavior (single-model pools are a no-op,
   effort stays "high"); the value shows in a multi-model pool, where each
   specialist routes to the member strongest for its kind of work.
3. **Output contracts in use.** The previously-unused contract hook now carries
   real (off-by-default) guards: Iris caps reply length (concise social copy),
   Argus caps tool calls (runaway-scan ceiling). Enforced only when
   `OLYMPUS_CONTRACTS` is enabled; inert otherwise.

`tests/test_specialist_strength.py` covers all three. No capability-count change.

### Changed — Deeper specialist benchmark (strengthens the self-improvement loop)

- Expanded `olympus/benchmarks.json` from 17 items to **50** — **5 per
  user-facing specialist** (was as low as 1 for plutus/iris/chiron/chronos/
  argus/mnemosyne). The benchmark is the signal Prometheus's measured-training
  loop optimizes against and the basis for `olympus scores`; with one item per
  specialist the score was too noisy to tell a good prompt from a bad one and to
  catch regressions. Deeper, varied, harshly-gradeable items give every
  specialist real signal to be strengthened against — no engine change needed,
  the existing train-with-rollback loop now has something to push on.
- `tests/test_benchmarks.py` guards depth (≥5 per user-facing specialist),
  unique ids, valid specialist keys, and well-formed task/criteria.

### Added — "What Olympus learned on its own" readout (`olympus/digest.py`)

- A plain-language summary of the autonomous loop's recent activity: when each
  cycle last ran (world scan, video learning, daily skill distillation,
  training, self-audit), the skill count, and recent world reports / lessons /
  self-upgrades. Read-only over the heartbeat's persisted state + memory — no
  model calls. Surfaced three ways: the `olympus learned` CLI command, the
  `/learned` chat command, and the `recent_learning` agent tool (granted to
  Metis) so you can just ask "what did you learn while I was away?".
- New `memory.recent_titles(category, n)` helper. Tools 58 → 59; commands
  66 → 67. THREAT_MODEL row, `capabilities.json`, README counts, and
  `tests/test_digest.py` added.

### Changed — Always-on learning runs by default in the cloud deploy

- `deploy/docker-compose.yml` now enables the `heartbeat` service by default, so
  a standard `docker compose up -d` runs Olympus's self-learning loop (world
  scans, YouTube learning, daily skill distillation, weekly self-audit, and any
  scheduled operator jobs) around the clock — not just the chat server. It
  shares the memory volume, so what it learns is immediately available to chat.
- Background spend is bounded by `OLYMPUS_DAILY_BUDGET` (deploy `.env.example`
  ships `=20`; cycles skip once the cap is hit). Documented loudly that
  `OLYMPUS_DAILY_BUDGET=0` means *unlimited*, not off — to disable the loop you
  comment out the `heartbeat` service. `deploy/README.md` updated accordingly.

### Added — Operator interactive layer: secure capture, inline approval, auto-launch, advanced mode

- **Secure credential capture out of the model loop** (`olympus/securecapture.py`
  + `operator_remember_login`): a tool records a pending request (domain only);
  after the turn a private `getpass` prompt collects the credentials and stores
  them in the vault. The password never passes through the model or a tool arg.
- **Plain-English approval** (`olympus/approvals.py` + `interactive.after_turn`):
  a held action is shown as its preview and confirmed with a simple "yes" —
  mapped to `actions.approve`/`reject`. No `olympus approve <id>` needed.
- **Browser auto-launch** (`browser.launch_local` / `_find_chrome` /
  `_chrome_args`): finds Chrome (PATH or the bundled Playwright build) and starts
  it with remote debugging, headed by default so manual sign-in is visible.
  Opt-in via `OLYMPUS_BROWSER_AUTOLAUNCH=1`; `_build_transport` uses it.
- **Advanced-mode toggle** (`set_advanced_mode` → `operator.advanced`): off by
  default keeps everything plain-English; the Hermes prompt hides env/CLI/IDs
  unless it's on.
- New module `olympus/interactive.py` centralizes the post-turn interactions
  behind an injectable IO (fully unit-tested); `tui.run` calls it once per turn,
  guarded. Tools 56 → 58. THREAT_MODEL rows, `capabilities.json`, README, and
  `tests/test_interactive.py` added.

### Added — Operator for non-engineers: plain-English setup (`docs/DESIGN_OPERATOR_UX.md`)

- The operator can now be turned on and authorized **per site, through
  conversation** — no env vars, no CLI, no "vault" for a normal user. Settings
  persist per-user in `prefs`; `enabled(user)` is `env OR the user's opt-in` and
  `authorized(user, domain)` is `(env domains OR their authorized sites) AND the
  egress allowlist`. The `OLYMPUS_OPERATOR*` env vars remain as an additive
  engineer/admin override.
- **Manual sign-in is the default and works end to end with no password
  handling:** the person signs in themselves and Olympus reuses the session —
  it never sees or stores a password. An opt-in **remember** mode stores
  credentials in the vault via a secure local prompt (primitive shipped;
  in-chat secure capture is the next interactive step).
- **Three conversational tools** for HERMES (53 → 56): `operator_authorize_site`
  (manual | remember), `operator_forget_site`, `operator_status`. The Hermes
  prompt leads with manual sign-in and never tells a non-technical user to set
  env vars or run commands.
- All operator gating (`browser_login`, `browser_operate`, `operator_schedule`,
  scheduled jobs, and the spine `execute`) is now per-user; scheduled jobs are a
  silent no-op for users who haven't enabled the operator. THREAT_MODEL rows,
  `capabilities.json`, README count, and `tests/test_operator_ux.py` added.

### Added — HERMES operator, Phases 2-4: credentialed actions, always-on, self-healing

- **Credentialed actions on the approval spine (Phase 2).** `browser_operate`
  runs declarative action templates (`site_template_record`) as
  `actions.ActionType`s — two are registered: `browser_operate` (NOTABLE, can
  auto-run within a granted `browser.operate` scope + autonomy) and
  `browser_operate_irreversible` (IRREVERSIBLE, **always** requires explicit
  approval). They inherit the whole spine: deny-first scopes, daily runaway
  caps, and the immutable audit log. Templates are ordered assert/click/fill/
  wait steps — there is no "interpret the page" path.
- **Always-on operator jobs (Phase 3).** `operator_schedule` stores standing
  jobs that `heartbeat.tick()` runs via `operator.run_due()`. Every run goes
  back through the spine, so irreversible templates still wait for approval and
  everything is scope/budget gated. No-op unless `OLYMPUS_OPERATOR` is on.
- **METIS/Prometheus weave (Phase 4).** `operator_review` (Metis + daily
  heartbeat) prunes site profiles that fail consistently; `propose_site_profile`
  (Prometheus) files human-reviewable profile/selector patches that are never
  self-applied. Loadouts: Hermes gains operate/template/schedule, Metis gains
  the review tool, Prometheus the proposal tool.
- Tools 48 → 53; ActionTypes 11 → 13. THREAT_MODEL.md rows, `capabilities.json`,
  README counts, and `tests/test_operator_phases.py` added. Real-Chrome smoke
  still green.

### Added — HERMES browser operator, Phase 1 (`docs/DESIGN_OPERATOR.md`)

- **New specialist HERMES (Operator)** — the first agent that can perform
  **credentialed** browser actions. It is deliberately **non-ingesting**
  (`web=False`, and it has neither `browser_open` nor `browser_read`), so it
  legitimately keeps the actuator while capability separation still holds
  system-wide: the agent that reads the open web (Argus) never holds the
  actuator, and the agent that holds the actuator (Hermes) never reads open-web
  prose as instructions.
- **Four new tools** (44 → 48), all threat-modeled: `browser_login`
  (vault-backed login via a declarative site profile), `browser_exists` (a
  yes/no selector predicate — never page prose), and `site_profile_record` /
  `site_profiles` (provenance- and reliability-scored login recipes).
- **Site Profiles** (`browser.SiteProfile`) — declarative per-domain login
  recipes (login URL + selectors + success marker) with a content hash,
  provenance, and an outcome-derived reliability score. Stored at
  `MEMORY_DIR/site_profiles.json`. Credentials are **not** stored here.
- This is Phase 1 of the operator: login + structured inspection only, **no
  irreversible actions**. Always-on heartbeat playbooks and METIS/Prometheus
  weaving are later phases.

### Security — operator is off by default and fails closed

- Master switch `OLYMPUS_OPERATOR` (default off) disables the entire
  credentialed path. `browser_login` additionally requires the domain to be in
  `OLYMPUS_OPERATOR_DOMAINS` **and** on the egress allowlist, and a vault entry
  `site:<domain>` to exist — each missing gate fails closed.
- Credentials come from the encrypted vault (`vault.get`); the password is
  filled into the page but **never enters the model context or any output**.
- `browser_login` is a registered `ACTION_TOOL` (stripped from any ingesting
  run); a missing post-login success marker (2FA/CAPTCHA/selector drift) makes
  it stop and report rather than retry.

### Added — Governed browser harness (`olympus/browser.py`)

- A stateful Chrome-DevTools-Protocol harness that lets a specialist drive a
  **real** browser — Olympus's answer to the open-web "agent + CDP" pattern,
  built so the browser inherits Olympus's governance instead of bypassing it.
  The transport is pluggable: a lazy WebSocket transport attaches to Chrome
  (`OLYMPUS_BROWSER_CDP_URL`, optional `websockets` extra), while an in-memory
  `FakeTransport` keeps tests and headless CI fully offline — the core
  dependency set is unchanged.
- **Five named, threat-modeled tools** (39 → 44): `browser_open`,
  `browser_read`, `browser_act`, `browser_skill_record`, `browser_skills`.
  Granted to **Argus**.
- **Provenance-scored skill library.** A browser skill carries its source,
  author, creation time, a content hash, and an outcome-derived reliability
  score (`successes/runs`); `browser_skills` ranks by measured reliability, not
  blind trust. Stored at `MEMORY_DIR/browser_skills.json`.
- **Replayable session ledger.** Every CDP call is appended to a per-session
  ledger, so a browser session is auditable rather than a black box.
- **Real-browser attach + opt-in smoke test.** `OLYMPUS_BROWSER_CDP_URL` accepts
  a DevTools base (`http://host:port`, auto-discovering a page target) or a
  `ws://` page URL; `tests/test_browser_smoke.py` drives real headless Chrome
  through the live transport, skipped unless `OLYMPUS_BROWSER_SMOKE=1`.

### Security

- **Egress + SSRF gate on every navigation.** `browser_open` routes through
  `security.url_block_reason` — no internal/metadata address, and under
  sovereign mode no non-allowlisted host, can be reached. `browser_open` /
  `browser_read` join `INGESTION_TOOLS`, so their output is wrapped as
  untrusted.
- **Capability separation closes the credential kill-chain.** `browser_act`
  (click/type on a possibly logged-in session) is a registered `ACTION_TOOL`,
  so it is stripped from any run that also ingests untrusted page content. Argus
  reads/learns via the harness but, because it ingests the web, never holds the
  credentialed actuator in the same run — proven in `tests/test_browser.py`.
- **Hardening pass.** The SSRF/egress gate is re-run against the *landed* URL
  after navigation (a 3xx redirect or JS navigation onto an internal host is
  blocked and the tab is sent to `about:blank` instead of surfacing its
  content); the real transport bounds a single CDP frame (anti-OOM) and times
  out a stuck reply instead of wedging the agent; `browser_open` waits (bounded)
  for `readyState=complete` before reading; the CDP ledger is a bounded circular
  buffer; and the skill store caps field/step lengths, bounds the library
  (dropping lowest-reliability skills), and skips malformed entries.
- See [docs/BROWSER_HARNESS.md](docs/BROWSER_HARNESS.md) for the full
  strengths→moats / weaknesses→credibility-assets rationale.

## [0.22.0] — unreleased (prepared; not tagged or published)

### Added — OpenAI-compatible inbound endpoint (SPEC-01)

- **`olympus/openai_server.py`** (new) + the `web.py` handler — expose Olympus
  as an OpenAI Chat Completions API: `POST /v1/chat/completions` (streaming and
  non-streaming) and `GET /v1/models`, so any OpenAI client/IDE/app can drive
  the full council by changing only `base_url` + `model` + `api_key`. Pure
  stdlib request/response translation (messages→prompt, `chat.completion` /
  `chat.completion.chunk` shaping, SSE ending in `data: [DONE]`, a usage
  estimate); each request runs the existing `orchestrator` pipeline — no new
  framework, no parallel pipeline.
- **`config.api_keys()`** (`OLYMPUS_API_KEYS`) gates it: `/v1/*` is
  **loopback-only** when unset (never a silent open relay), `401` on a bad
  bearer token, `403` for a remote peer. `olympus serve` mounts the same
  handler. See [docs/OPENAI_ENDPOINT.md](docs/OPENAI_ENDPOINT.md).

### Added — provable zero-egress sovereignty mode (SPEC-02, off by default)

- **`security.py`** — a single egress choke point `assert_egress_allowed(host)`
  (typed `EgressBlocked`) with `host_on_allowlist()` / `egress_allowed()`.
  Loopback, local providers, and `OLYMPUS_EGRESS_ALLOWLIST` (hosts/IPs/CIDRs)
  are permitted; the existing SSRF guard routes through the same choke. A pure
  no-op when sovereign mode is off — behaviour byte-for-byte unchanged.
- **`config.py`** — `OLYMPUS_SOVEREIGN` / `OLYMPUS_EGRESS_ALLOWLIST`; the
  `ModelPool` filters non-local members **before** model selection and **fails
  closed** if none remain (no silent remote fallback), plus per-request
  data-class routing (`olympus ask --data-class`, the `X-Olympus-Data-Class`
  header). `olympus status` shows the mode, allowlist, and eligible models. See
  [docs/SOVEREIGNTY.md](docs/SOVEREIGNTY.md).

### Added — production-real audit & verification (SPEC-03)

- **`olympus verify --run <id>`** — one PASS/FAIL that combines `replay_run`
  (the byte-identical decision path) **and** the decision-log signature against
  the trusted key, exiting non-zero and naming the divergence/signature failure.
  `--require-production` fails a run signed under the public default seed, and a
  default-seed pass is loudly labelled `DEV / UNVERIFIED`.
- **`web.py`** — `/api/status` reports a `signing` posture object, and an OpenAI
  endpoint answer carries `X-Olympus-Run-Id` + `X-Olympus-Audit` so a caller can
  locate and verify the run behind it. New [docs/VERIFY.md](docs/VERIFY.md);
  `docs/SIGNING.md` gains seed generation, HSM/KMS guidance, and a key-rotation
  procedure.

### Security

- **Hardened the `/v1/*` loopback boundary** against header spoofing and the
  reverse-proxy open-relay trap. The remoteness decision reads only the kernel
  peer socket (never a forwarding header), unwraps IPv4-mapped IPv6, and — when
  no `OLYMPUS_API_KEYS` are set — refuses non-loopback peers and requires a key
  whenever a proxy forwarding header is present. Header values are never
  trusted, only their presence, and only to deny.

### Added — egress gateway, Phase A (boundary layer, off by default)

- **`olympus/egress.py`** — the unified egress chokepoint. `classify()` (pure,
  regex-based, reusing `security.py`'s `_KEYISH`/`_URL_CRED`/`_EMAIL`/`_PHONE`/
  `_LONGNUM`) labels a payload PUBLIC/OPERATIONAL/SENSITIVE; `guard()` checks it
  against a per-channel policy matrix and returns ALLOW / REDACT (POOLED only) /
  HOLD. No LLM, no new dependency.
- **`config.egress_guard_enabled()`** (`OLYMPUS_EGRESS_GUARD`, off by default,
  matching `require_byok`) wires the guard into the two raw actuators
  `tools._send_email` and `tools._call_webhook` (Phase A only). A SENSITIVE
  payload is HELD and routed to the existing actions spine via two new
  `IRREVERSIBLE` action types (`email_egress_held`, `webhook_egress_held`,
  reusing the existing executors); within-policy sends are unchanged.
- The approved-execution path bypasses the guard (`_approved=True`) so an
  approved HOLD sends exactly once (no held→execute→held loop).
- Each decision is recorded as a new `decision_type="egress"` record in the
  existing signed Trace (via a per-thread current-Trace accessor in `trace.py`);
  the mode is stamped into `tr.meta` and restored by `replay_run`. Off by
  default — zero change to a fresh install. See
  [docs/DESIGN_BOUNDARY_LAYER.md](docs/DESIGN_BOUNDARY_LAYER.md).

### Added — hard output contracts (primitive, off by default)

- **`olympus/contracts.py`** — a pure, dependency-free check of a specialist's
  final output against an optional `OutputContract` (size ceiling, must-be-JSON
  + shallow required-keys/top-level-type schema check, client-side tool-call
  cap). Returns a `ContractResult(ok, violations)`; no I/O, no config reads.
- **`config.contracts_enabled()`** (`OLYMPUS_CONTRACTS`, off by default,
  matching the `require_byok` convention) gates enforcement at
  `orchestrator._run_one`. A violation **fails closed** but degrades gracefully,
  returning the same "treat this part as missing" placeholder the existing
  failure path uses, so verify/synthesis tolerate it unchanged.
- Each check is recorded as a new `decision_type="contract"` record in the
  existing signed, re-executable decision log (`trace.py`) — no parallel log.
  The enforcement mode is stamped into `tr.meta` and restored by `replay_run`
  so a contracts-on run replays in the same mode. Ships with every specialist
  at `contract=None` (zero behaviour change). See
  [docs/DESIGN_OUTPUT_CONTRACTS.md](docs/DESIGN_OUTPUT_CONTRACTS.md).

## [0.21.0] — 2026-06-27

### Added — guided onboarding (Hermes-style)

- **`olympus/providers.py`** — a curated provider catalog (Anthropic, OpenAI,
  DeepSeek, GLM/Z.AI, Kimi/Moonshot, Groq, OpenRouter, Gemini, Mistral, Ollama,
  custom) with base URLs and auth styles, plus `fetch_models()` that lists the
  provider's real model IDs so users don't guess model names, and
  `build_pool_config()` to assemble the multi-key pool.
- **Guided `olympus setup` wizard** (numbered menus — robust over SSH/WSL):
  pick one or more providers, **auto-discover models**, compose them into the
  model pool, and optionally enable fast mode, choose an execution backend, and
  connect a messaging gateway — then it writes `config.env`. Subscription auth
  is first-class: **run on a Claude subscription** (the `claude-code` provider)
  with no API key.
- **Rich launch screen + status line** (`olympus/tui.py`): a branded welcome
  with the model-pool assignment and a capability overview from the manifest,
  and a per-turn status bar (model · seconds · today's spend · fast-mode).

## [0.20.0] — 2026-06-27

### Added — latency controls (fast mode)

- **`OLYMPUS_FAST=1`** — a latency dial: the lightweight pipeline stages
  (route/plan) run on the pool's **fastest** model (`ModelPool.fastest()`, picked
  by model-name hints like flash/air/mini/8k/haiku) and the optional Athena
  **review** stage is skipped — trading a little polish for markedly lower
  latency while keeping the strong model for the actual specialist work and the
  final synthesis.

### Fixed

- **Final-compose no longer crashes the run.** A provider error in the
  `synthesize` stage previously raised an unhandled traceback; it now degrades
  gracefully to the already-verified findings (both the blocking and streaming
  paths).
- **OpenAI-compatible backend now bounds output** with `max_tokens`
  (`config.MAX_TOKENS`). It previously sent no cap, so reasoning models could
  generate unboundedly — a significant latency and cost sink.

## [0.19.0] — 2026-06-27

### Added — per-user adaptive evolution (gets smarter the more you use it)

- **`olympus/companion.py`** — the personal counterpart to Metis's shared
  learning cycle. Every few conversations (`OLYMPUS_EVOLVE_EVERY`, default 6),
  Olympus re-distills *that user's own* history — durable facts, 👍/👎 feedback,
  and corrections — into a compact, private **working model** ("how to work well
  with this person") that is injected into every answer for them, and nobody
  else. A visible **growth level** (new → acquainted → familiar → attuned →
  trusted companion) deepens with use. Surfaced via `olympus growth` and the
  `/growth` chat command. Runs in the background, never delays a reply, and
  keeps the prior model on any failure so accumulated learning is never wiped.

## [0.18.0] — 2026-06-27

### Added — release tooling

- **`scripts/bump_version.py`** — one command bumps the version (patch/minor/
  major or `--set`) across `pyproject.toml` + `olympus/__init__.py` and cuts the
  CHANGELOG (`[Unreleased]` → dated section, fresh `[Unreleased]`, fixed compare
  links). `--dry-run` previews. Documented in RELEASING.md.

### Security

- **Per-specialist skill-index scoping.** Skills tagged for one specialist no
  longer appear in every other specialist's prompt. Previously a skill was
  benchmark-gated against only its own specialist yet was injected into the
  shared index every specialist sees, so it could silently degrade specialists
  it was never measured against. The index is now scoped per specialist (its own
  skills + untagged/`all` global skills); evaluation uses the same scoped view,
  and global/untagged skills are gated against the whole benchmark.
- **Pinned release-signing key.** The production Ed25519 public key is committed
  to `olympus/witness_pubkey.txt` (and shipped in the wheel), so `olympus verify`
  trust-pins releases to exactly this key — a manifest re-signed with any other
  key is rejected, not merely checked for internal consistency.

### Hardening

- Sandbox: empty commands are rejected, the timeout is clamped, and an unknown
  backend falls back to local; `docker` runs are network-isolated by default
  (opt in with `OLYMPUS_EXEC_NETWORK=1`). Added docker-command coverage.
- Scheduler: stored jobs are bounded (`MAX_JOBS`, keeping the most recent) so
  the schedule file can't grow without limit.

## [0.17.0] — 2026-06-27

### Added — install & updates (frictionless onboarding)

- **Top-of-README Install section** with copy-pasteable Linux / macOS / Windows
  one-liners, the pip/pipx path, and the upgrade commands.
- **`olympus upgrade`** (`olympus/selfupdate.py`) — one command to update to the
  latest release; detects whether you installed via pipx, the install-script
  venv, or plain pip and runs the right thing (`--git` forces a from-source
  upgrade). **`olympus version`** prints the installed version, and
  `__version__` now reflects the installed distribution's real version.

### Packaging

- Switched to the PEP 639 SPDX license declaration (`license = "MIT"` +
  `license-files`), so the built wheel/sdist pass `twine check` on current
  tooling and publish cleanly to PyPI. Requires `setuptools>=77` to build.
- Installers now **prefer the PyPI release and fall back to source**: they try
  `pip install olympus-council` first and only install from the GitHub repo if
  PyPI is unavailable — so they work before the package is published and switch
  to stable releases automatically once it is (`OLYMPUS_PACKAGE` overrides).
- CI now builds the wheel/sdist and runs `twine check` on every push/PR
  (new `package` job), catching packaging regressions before release.

### Added — operator capabilities (Hermes gap-closure)

A batch of capabilities closing the operator-axis gaps identified against
NousResearch/hermes-agent. Each ships with tests and is bound to the
CI-verified capability manifest.

- **Real execution environment** (`olympus/sandbox.py`). A workspace-confined
  shell + file surface with `local` and `docker` backends (timeout- and
  output-capped, path-escape-refused). Exposed as approval-gated actions
  `run_command` (irreversible) and `write_file` (reversible/undoable), plus
  read-only `read_file` / `list_dir` tools. Hephaestus gains the loadout.
- **Scriptable subagents** (`olympus/subagents.py` + `spawn_subagent` tool):
  ad-hoc, isolated, parallel specialist fan-out with per-branch failure
  containment.
- **Natural-language cron** (`olympus/scheduler.py`, `olympus schedule`,
  `schedule_task` tool): user-defined recurring tasks in plain English, run
  unattended by the heartbeat, results delivered to any platform.
- **Discord / Slack / Signal gateways** (`olympus/{discord,slack,signal}.py`)
  over a shared `gateway.py` router; Slack HMAC + Discord Ed25519 request
  verification.
- **Rich TUI** (`olympus/tui.py`): multiline input, `readline` slash-command
  autocomplete, streamed answers.
- **Cross-session search** (`olympus/search.py`, `olympus search`,
  `search_sessions` tool): SQLite FTS5 over all persisted conversations, with a
  LIKE fallback; indexed live on save.
- **Training-trajectory export** (`olympus/trajectories.py`,
  `olympus export-trajectories`): conversations → SFT pairs and traces →
  decision trajectories as JSONL.
- **Serverless / hibernation mode** (`olympus/hibernate.py`, `olympus tick`,
  `olympus next-wake`): run one tick and report the next-due time so an external
  scheduler can wake Olympus on demand.
- **agentskills.io interop** (`olympus/skillpack.py`, `olympus skill-import` /
  `skill-export`): import/export skills in the open SKILL.md standard.
- **Migration importer** (`olympus/migrate.py`, `olympus import-agent`): fold an
  OpenClaw/Hermes-style agent's memories, profile, and skills into Olympus;
  API keys are detected and reported, never silently stored (opt-in `--keys`).
- **Media tools** (`olympus/media.py`): `generate_image`, `text_to_speech`, and
  a link-extracting `browse_page`; generative tools degrade gracefully without a
  key and write only into the confined workspace.
- **Windows installer** (`install.ps1`): PowerShell one-liner mirroring the
  POSIX installer.

### Added — disaster recovery

- **Off-droplet data backups** (`olympus backup` / `olympus restore`,
  `olympus/backup.py`). Archives `MEMORY_DIR` (per-user memory, accounts, the
  encrypted OAuth tokens, the signed decision log), **encrypts** it at rest with
  the vault key, **signs** it with the witness Ed25519 root of trust, and hands
  it to `OLYMPUS_BACKUP_CMD` for off-machine delivery. Restore verifies the
  signature and every file's SHA-256, rejects path-traversal entries, and won't
  clobber a non-empty target. Runs on a cadence via the heartbeat (or the
  dedicated, token-free `backup` compose service). See
  [docs/BACKUPS.md](docs/BACKUPS.md).

## [0.16.0]

First formally catalogued release. Olympus is a provider-agnostic, multi-agent
assistant (Zeus routes, Athena supervises a council of specialists, Aletheia
fact-checks) with durable per-user memory, human-approved actions, and a web,
Telegram, and WhatsApp surface. This entry records the verifiability,
reliability, and public-launch work that defines the line; earlier history lives
in the git log and pull requests #1–#49.

### Added — verifiable reasoning & supply chain ("the moat")

- **Re-executable decision log.** Every run freezes its LLM requests/responses
  and pairs them with structured decision records, so a recorded run can be
  re-executed against the frozen responses and proven byte-identical, or the
  exact diverging request is pinpointed (`olympus replay`, `olympus explain`).
  See [docs/DECISION_LOG.md](docs/DECISION_LOG.md).
- **Signed releases & signed decision log.** One Ed25519 root of trust signs
  both a release manifest (`verification.json`, every tracked file's SHA-256)
  and each run's decision path. `olympus sign` / `olympus verify` /
  `olympus verify --log <run_id>`. See [docs/SIGNING.md](docs/SIGNING.md).
- **Capability manifest** generated from code and bound to the README numbers,
  enforced in CI (`olympus capabilities --check`). See
  [docs/CAPABILITIES.md](docs/CAPABILITIES.md).
- **Memory format contract** with a versioned on-disk `schema_version` and
  forward migration (`olympus memory migrate`). See
  [docs/MEMORY_FORMAT.md](docs/MEMORY_FORMAT.md).
- **Supply-chain integrity:** hash-pinned `requirements.lock`, a no-prerelease
  check, and a CycloneDX SBOM in CI. See [docs/SUPPLY_CHAIN.md](docs/SUPPLY_CHAIN.md).
- **Threat model** bound to the live tool handlers and enforced by
  `scripts/check_threat_model.py`. See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

### Added — reliability gate

- **Replay self-check tripwire:** the heartbeat re-runs real prompts on a
  cadence and a CI replay-gate workflow does the same on a schedule; a run that
  stops replaying byte-identically escalates (memory note, Telegram alert, and
  an auto-filed GitHub issue) instead of silently rotting the audit trail.
- **Operator reliability gate** (`scripts/reliability_gate.py`): runs three
  distinct prompts unattended end-to-end on a real key, proves each replays with
  zero new API calls, enforces a spend cap, and reports total spend.

### Added — public-launch safety kit

- **Problem-report channel:** a `📣 report` button and `/api/report` (works
  before login), captured durably and pushed to the operator (`olympus reports`).
- **Operator error capture:** unexpected failures are recorded to a durable
  log and rate-limited Telegram alert (`olympus errors`).
- **Cost protection:** bring-your-own-key as a *free allowance* — keyless users
  get `OLYMPUS_FREE_CHATS` operator-funded chats per day, then continue on their
  own key — alongside the all-or-nothing `OLYMPUS_REQUIRE_BYOK`, per-day and
  per-minute caps, and a daily budget.
- **Privacy & Terms pages** (`/privacy`, `/terms`) written to match real
  behaviour, with operator identity from `OLYMPUS_OPERATOR_NAME` /
  `OLYMPUS_OPERATOR_CONTACT`.

### Security — pre-release hardening

- Signing refuses to produce a release manifest under the public default seed
  (forgeable) unless explicitly marked `--dev`; verification trusts a manifest
  only against a pinned public key, and rejects dev manifests for release.
- The replay gate fails *loudly* (logged, never swallowed) on an unexpected
  internal error, distinguishing a genuine divergence from an infrastructure or
  account skip — so an empty wallet can't masquerade as a green gate.
- Load-bearing best-effort paths (memory extraction, connector token lookups)
  now log unexpected failures instead of failing silently; telemetry swallows
  are intentionally left untouched.
- `Trace.decision(status=...)` is mandatory, so a failure path can no longer
  silently record success and poison per-agent trust scoring.

[Unreleased]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.21.0...HEAD
[0.21.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/maadjiba24-afk/Olympus-/releases/tag/v0.16.0
