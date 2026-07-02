# Hermes Agent — Release Watch

Tracking document for [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
("the self-improving AI agent" — an autonomous agent framework with a skills
learning loop, persistent memory, and a multi-platform messaging gateway).
Hermes is the closest large open-source project to Olympus's design space, so
every Hermes release is analyzed here for ideas worth adopting.

**Scope of this document: analysis only.** Nothing here is a commitment to
implement. Each release entry ends with an *Olympus relevance* note; the
consolidated shortlist at the bottom is the input for later adopt/skip
decisions.

> Naming note: Olympus has its own specialist named Hermes (the credentialed
> browser operator, `olympus/operator.py`). Everywhere in this document
> "Hermes" means the external NousResearch framework, not our specialist.

---

## Snapshot (as of 2026-07-02)

| | |
|---|---|
| Latest release | **v0.18.0 "The Judgment Release"** — 2026-07-01 (tag `v2026.7.1`) |
| Total releases | 19 (v0.2.0 → v0.18.0) |
| Cadence | Roughly one minor release every 1–2 weeks since March 2026 |
| Language | Python (~82%), TypeScript (~14%) |
| License | MIT |
| Scale | ~207k stars, ~37.6k forks, 300–950 issues closed per release |
| Roadmap | No GitHub milestones or published roadmap — future releases are only predictable by cadence |

**How to check for a new release:** watch
`https://github.com/NousResearch/hermes-agent/releases` (or the Atom feed
`releases.atom`). Tags are date-based (`v2026.7.1`), titles carry the semantic
version. When a new one lands: add an entry to the history below, assess
relevance against `olympus/capabilities.json`, and update the shortlist.

---

## Release history (newest first)

### v0.18.0 — "The Judgment Release" — 2026-07-01 (`v2026.7.1`)

~1,720 commits, 998 PRs, 949 issues closed (all P0/P1 across the repo — ~700
highest-priority items cleared in a 12-day sweep), 381 contributors.

**What's in it**

- **Mixture-of-Agents (MoA) as a first-class model.** Named ensembles are
  selectable models under a `moa` provider; reference-model reasoning shows as
  labeled blocks before the aggregator synthesizes; `/moa` one-shot command;
  optional full-turn trace persistence.
- **Verification & goal completion.** `/goal` gains *completion contracts*
  (done conditions defined up front); standing-goal loops judge completion
  against evidence rather than the model's own claim; a coding "evidence
  ledger" tracks project checks; `pre_verify` hook for custom verification.
- **Self-improvement.** `/learn <anything>` distills a reusable skill from a
  directory, URL, or workflow; `/journey` shows a learning timeline (with a
  desktop radial "memory graph"); background review made cheaper via auxiliary
  model routing.
- **Delegation.** `delegate_task` fans out to multiple parallel background
  subagents without blocking chat; consolidated return; status-bar tracking.
- **Desktop app**: first-class Projects (sidebar, coding rail, review pane,
  git worktrees), multi-terminal panel, PR-style diffs in chat, context-usage
  popover, opt-in roaming pet.
- **Gateway scaling**: scale-to-zero idle detection, drain coordination,
  self-healing for stranded gateways, in-flight transcript persistence.
- **Providers**: Google Vertex AI first-class (auto-minted short-lived OAuth2
  tokens), Z.AI endpoint picker, Ollama-cloud reasoning effort.
- **Security**: cron `base_url` credential-exfiltration override blocked,
  browser cloud-metadata floor on all backends, private-network guard
  re-checked after `browser_back`, `/resume`//`/sessions` scoped to caller
  origin (IDOR fix), Slack `xapp-` token redaction, aiohttp CVE floor.
- **Cron reliability wave**: unpinned jobs fail closed on provider drift,
  missed-grace jobs run once instead of deferring forever, ticker survives
  `BaseException`.

**Olympus relevance** — the most relevant release to date.
*Already have:* evidence-based verification is Aletheia's core concept;
approval-spine and SSRF work matches our 0.23.0 hardening; parallel fan-out
exists in the DAG orchestrator. *Gaps/candidates:* MoA ensembles as a
selectable "model" (we have a model pool per role, not ensembling); completion
contracts for standing goals (our heartbeat/scheduler has no "done condition"
primitive); `/learn` from a URL/directory (our `create_skill` is
agent-initiated, not user-command-driven); `/journey` memory timeline;
scale-to-zero for the gateway; cron fail-closed-on-provider-drift.

---

### v0.17.0 — "The Reach Release" — 2026-06-19 (`v2026.6.19`)

~1,475 commits, ~800 PRs, 300+ issues closed, 245 contributors.

**What's in it**

- Messaging: iMessage via Photon Spectrum (no Mac relay), Raft agent-network
  channel, official WhatsApp Business Cloud API adapter, SimpleX groups,
  Telegram Bot API 10.1 formatting.
- Desktop: subagent watch-windows, rebindable shortcuts, OS notifications,
  per-model composer presets, VS Code Marketplace theme install.
- Agent: background/async subagents with immediate handles; image-to-image
  editing in `image_generate`; atomic batch memory operations against a
  character budget.
- Infra: **Automation Blueprints** (scheduling without cron syntax), managed
  scope for admin-pinned config, pluggable CronScheduler, gateway-to-gateway
  relay (phases 0–3).
- Skills Hub redesign with connected hubs and security scanning of skills.
- Curator cost reduction (consolidation opt-in, routine pruning free).
- Security: MCP stdio config validated against exfiltration patterns,
  subprocess env sanitization, urllib3/PyJWT CVE patches.

**Olympus relevance** — *Already have:* WhatsApp (Meta Cloud API), subagents,
approval-gated memory writes. *Gaps/candidates:* atomic batch memory ops (our
event-sourced memory applies single mutations); Automation Blueprints-style
natural-language scheduling on top of `scheduler.py`; skill security scanning
before import (we have `skillpack.py` import/export but no scanner);
admin-pinned config scope. iMessage/Raft/SimpleX are platform breadth we
haven't targeted.

---

### v0.16.0 — "The Surface Release" — 2026-06-05 (`v2026.6.5`)

874 commits, 542 PRs, 399 issues closed (2 P0, 62 P1, 16 security), 170
contributors.

**What's in it**

- **Native desktop app** (Electron; macOS/Linux/Windows) with one-click
  install, self-update, drag-and-drop, streaming chat, remote-gateway connect
  over secure WebSocket with OAuth or username/password.
- **Web dashboard administration panel** — configure channels, MCP catalog,
  credentials, webhooks, memory, system controls from the browser (no more
  hand-editing config).
- Fuzzy model picker across desktop/web/TUI/CLI with hourly catalog refresh.
- `/undo [N]` — revert N user turns with message prefill, on CLI, TUI, and
  messaging platforms.
- Streamlined default skill set + `environments:` relevance gating (skills
  contextually hidden when irrelevant); NVIDIA/skills as a trusted tap.
- Quick setup via Nous Portal ("chatting in seconds").
- Simplified Chinese translation of all UI surfaces.
- Security: Starlette BadHost CVE-2026-48710 patch, SSRF checks moved off the
  event loop, credential stripping from subprocess environments.

**Olympus relevance** — *Already have:* web chat UI (`web.py`) and an
operator health dashboard, but **not** a full admin panel; our config is file
+ CLI driven. *Gaps/candidates:* dashboard-based administration (channels,
credentials, connectors) is the single biggest UX gap this release exposes;
`/undo` turn-revert; skill relevance gating (`environments:`) to keep prompt
budgets tight; fuzzy model picker for `olympus models`. A native desktop app
is a large investment — note it, don't chase it.

---

### v0.15.2 — patch — 2026-05-29 (`v2026.5.29.2`)

Packaging fix: bundle `plugin.yaml` manifests in wheel and sdist.

**Olympus relevance** — none directly; a reminder to keep packaging-data
tests in CI (we ship `capabilities.json`, `benchmarks.json` inside the wheel —
same failure class).

### v0.15.1 — "The Patch Release" — 2026-05-29 (`v2026.5.29`)

28 commits, 21 PRs. Dashboard infinite-reload loop in loopback mode fixed
(401 identity probe); Docker insecure mode now explicit opt-in via env var
instead of inferred from network binding; skills picker expanded from 858 to
19,932 catalog entries; `/yolo` per-session approval bypass; kanban workers
respond to termination signals and receive referenced images.

**Olympus relevance** — the Docker change mirrors our fail-closed philosophy
(explicit opt-in for insecure modes — we already do this with
`OLYMPUS_SOVEREIGN` / egress). `/yolo`-style per-session autonomy bump is
already covered by our autonomy dial (L0–L4).

---

### v0.15.0 — "The Velocity Release" — 2026-05-28 (`v2026.5.28`)

1,302 commits, 747 PRs, 560+ issues closed (15 P0, 65 P1, 19 security), 321
contributors.

**What's in it**

- **Core refactor**: monolithic `run_agent.py` cut from 16,083 → 3,821 lines
  (−76%) into 14 cohesive `agent/*` modules, backward compatible.
- **Performance wave**: −47% function calls per conversation; `--version`
  cold start 701ms → 258ms; Termux cold start 2.9s → 0.8s; ~1s saved per
  agent turn.
- **Session search rebuilt**: from an LLM-powered tool (~$0.30 and ~30s per
  call) to three inference-free modes (discovery/scroll/browse) running in
  1–20ms.
- Multi-agent kanban platform: 104+ PRs — orchestrator auto-decomposition,
  swarm topology generation, per-task model overrides, worktree paths.
- **Security**: "promptware" injection defense at three chokepoints,
  tool-result delimiters against output spoofing, ~15 new threat patterns
  (Brainworm/C2), Bitwarden Secrets Manager integration.
- ntfy as 23rd messaging platform; skill bundles (one slash command loads
  several skills); xAI wave; OpenRouter sticky routing; Nous-approved MCP
  catalog with interactive picker.

**Olympus relevance** — *Already have:* FTS5 session search is already
inference-free (`search.py`); injection chokepoints and tool-result envelopes
match `security.py`'s untrusted-data envelope. *Gaps/candidates:* the
cold-start/performance discipline (measure per-turn overhead; we've never
profiled `olympus` startup); skill bundles; external secrets-manager backend
for `vault.py` (Bitwarden/etc.); per-task model overrides in DAG plans (our
model pool assigns per role, not per task).

---

### v0.14.0 — "The Foundation Release" — 2026-05-16 (`v2026.5.16`)

808 commits, 633 PRs, 545 issues closed (12 P0, 50 P1), 215 contributors.

**What's in it**

- **PyPI distribution** (`pip install hermes-agent`) replacing repo cloning;
  debloating wave — heavy backends lazy-install on first use.
- OpenAI-compatible local proxy (`hermes proxy`) backed by OAuth subscription
  providers (Claude Pro, ChatGPT Pro, SuperGrok).
- xAI SuperGrok OAuth; grok-4.3 at 1M context.
- X/Twitter search as a native tool; Microsoft Teams end-to-end via Graph.
- Cross-session Claude prompt caching (1-hour) for system prompts + skills.
- Browser: CDP WebSocket routing → "180× faster" console evaluations.
- `/handoff` — live session transfer between models/personas, no context
  loss.
- Native button UI for clarifying questions on Telegram/Discord.
- Per-turn file verification (delta summaries catch silent write failures);
  LSP semantic diagnostics after edits.
- Computer use for non-Anthropic models (cua-driver backend).
- Native Windows support (beta): PowerShell/cmd, MinGit auto-install, 40+
  Windows fixes.
- Security: sudo brute-force blocking, tool-error sanitization against
  instruction injection, supply-chain advisory check on every install.

**Olympus relevance** — *Already have:* PyPI/pipx distribution, Windows
installer, supply-chain policy (`docs/SUPPLY_CHAIN.md`), approval buttons via
platform gateways. *Gaps/candidates:* cross-session prompt caching for the
Anthropic backend (system prompt + skills are stable — direct cost win);
per-turn file-write verification and post-edit lint/LSP delta (Hephaestus
writes code; we verify by benchmark, not per-write); `/handoff` between
models mid-session; OAuth-subscription-backed local proxy (we already have
`claude_code.py` for Claude subscriptions — the proxy generalizes the idea).

---

### v0.13.0 — "The Tenacity Release" — 2026-05-07 (`v2026.5.7`)

864 commits, 588 PRs, 282 issues closed (13 P0, 36 P1), 295 contributors.

**What's in it**

- **Durable multi-agent kanban**: heartbeat monitoring, worker reclaim,
  zombie detection, task retry budgets, hallucination recovery gates.
- **`/goal` persistent goals** — "Ralph loop" as a first-class primitive; the
  agent doesn't forget the standing objective across turns.
- Session durability & checkpoints v2 — gateway auto-resumes interrupted
  sessions after restart; checkpoint pruning + disk guardrails.
- Security: secret redaction on by default; Discord role-allowlists
  guild-scoped (closed a CVSS 8.1 cross-guild bypass); WhatsApp rejects
  strangers by default; TOCTOU windows closed in auth/OAuth flows.
- Google Chat as 20th platform; i18n to 7 locales; providers pluggable via
  `ProviderProfile` ABC; native `video_analyze`; post-write delta linting for
  Python/JSON/YAML/TOML; cron `no_agent` watchdog mode; SearXNG backend.

**Olympus relevance** — *Already have:* WhatsApp stranger rejection and
scoped allowlists match our gateway defaults; heartbeat exists. *Gaps/
candidates:* persistent `/goal` loop (our heartbeat runs scheduled scans, but
a user-stated standing goal with progress tracking is absent — pairs with
v0.18's completion contracts); session auto-resume after gateway restart;
task retry budgets + zombie detection if we ever grow a work queue;
post-write delta linting (cheap, high value for Hephaestus).

---

### v0.12.0 — "The Curator Release" — 2026-04-30 (`v2026.4.30`)

1,096 commits, 550 PRs, 213 contributors, ~57% TUI cold-start reduction.

**What's in it**

- **Autonomous curator**: an independent background agent on a 7-day cycle
  grades the skill library, consolidates related skills, prunes unused ones,
  and emits per-run reports (archived = consolidated | pruned).
- Self-improvement loop: rubric-based grading instead of free-form review;
  scoped toolsets so the reviewer can't reach shell/web.
- 4 new providers (GMI Cloud, Azure AI Foundry, LM Studio, MiniMax OAuth,
  Tencent Tokenhub); remote model-catalog manifests update without releases.
- Microsoft Teams (first plugin-shipped platform); Tencent Yuanbao (18th).
- Spotify (7 tools, PKCE OAuth); Google Meet plugin (join, transcribe, voice
  replies).
- `hermes -z` one-shot non-interactive mode; `update --check`; HERMES_HOME
  backup before updates.
- Performance: lazy agent init, lazy provider imports, mtime-cached config,
  memoized tool definitions.
- Security note: secret redaction flipped **off** by default (false positives
  corrupted patches/payloads) — reversed again in v0.13.0.

**Olympus relevance** — *Already have:* benchmark-gated skill lifecycle
(provisional → proven with regression revert) — arguably stronger than
grading rubrics; Metis daily synthesis. *Gaps/candidates:* periodic
**pruning/consolidation** of the skill library (we gate additions but never
retire stale skills); rubric-based self-review with scoped toolsets (matches
our capability-separation philosophy — the 0.23.0 `_ingests` work already
enforces this pattern for ingestion, extend to the review loop); remote
catalog manifests so model lists update without a release. The
secret-redaction flip-flop is a cautionary tale for our redaction tuning.

---

### v0.11.0 — "The Interface Release" — 2026-04-23 (`v2026.4.23`)

1,556 commits, 761 PRs, 224k insertions, 290 contributors incl. co-authors.

**What's in it**

- New React/Ink terminal UI (`hermes --tui`) over a Python JSON-RPC backend;
  live streaming, clipboard, status bar.
- **Transport layer extracted** from the agent loop into pluggable
  `agent/transports/` (Anthropic, ChatCompletions, Responses API, Bedrock).
- 5 new inference paths (NVIDIA NIM, Arcee, Step Plan, Gemini CLI OAuth,
  Vercel ai-gateway); GPT-5.5 via ChatGPT Codex OAuth.
- QQBot (17th platform).
- Plugin system: plugins can register slash commands, dispatch tools, block
  execution, rewrite results.
- **`/steer <prompt>`** — inject a note the running agent sees after its next
  tool call (mid-run steering without interrupting).
- Dashboard i18n, live themes, mobile responsiveness.

**Olympus relevance** — *Already have:* TUI (`tui.py`), plugin system,
provider abstraction (`providers.py`/`openai_compat.py`). *Gaps/candidates:*
`/steer` mid-run steering of a running specialist (we can only wait or
abort); plugin hooks that can block/rewrite tool execution (our `@plugin`
adds tools but has no interception points — would compose well with the
approval spine).

---

### v0.10.0 — "The Tool Gateway Release" — 2026-04-16 (`v2026.4.16`)

180+ commits (details folded into v0.11.0 notes).

Nous Tool Gateway: paid Nous Portal subscribers get web search (Firecrawl),
image generation (FLUX 2 Pro), TTS (OpenAI), and browser automation (Browser
Use) through the subscription — zero extra API keys; per-tool `use_gateway`
opt-in.

**Olympus relevance** — this is vendor-monetization plumbing. The pattern —
one credential unlocking a bundle of managed tools — is worth remembering if
Olympus ever offers hosted tooling, but nothing to adopt now. (We already
route web search server-side on the Anthropic backend.)

---

### v0.9.0 — "The Everywhere Release" — 2026-04-13 (`v2026.4.13`)

487 commits, 269 PRs, 167 issues, 24 contributors.

Mobile via Termux/Android; iMessage (BlueBubbles) + WeChat adapters (16
platforms total); Fast Mode priority queues for OpenAI/Anthropic; local web
dashboard (first appearance); background process monitoring with watch
patterns; xAI + Xiaomi MiMo providers; pluggable context-management slot;
`hermes backup`/`hermes import` data portability; security hardening (Twilio
webhook signatures, git argument injection, SSRF redirect guards).

**Olympus relevance** — *Already have:* encrypted signed backups
(`backup.py`), SSRF redirect re-validation (0.23.0), web UI. *Candidates:*
background process watch-patterns (alert on output regex) for long-running
Hephaestus jobs; Termux/Android as a cheap "Olympus in your pocket" story —
low priority.

---

### v0.8.0 — "The Intelligence Release" — 2026-04-08 (`v2026.4.8`)

209 PRs, 82 issues, 18 external contributors.

Background task completion auto-notifications (no polling); live `/model`
switching mid-session on all platforms with fallback; self-optimized GPT/
Codex tool-use guidance (5 failure modes fixed via automated behavioral
testing); native Google AI Studio provider; smart inactivity timeouts (track
task activity, not wall clock); approval buttons on Slack/Telegram; MCP OAuth
2.1 PKCE + OSV malware scanning for MCP packages; structured logs +
`hermes logs`; 100× regex backtracking fix.

**Olympus relevance** — *Already have:* approval buttons via gateways, model
switching via pool. *Candidates:* activity-based (not wall-clock) timeouts
for long specialist runs; OSV scanning for MCP servers/plugins before enable
(composes with our connector two-tier gating); behavioral test suites for
tool-calling failure modes per provider (we support many OpenAI-compatible
backends and have no such matrix).

---

### v0.7.0 — "The Resilience Release" — 2026-04-03 (`v2026.4.3`)

168 PRs, 46 issues, 48 contributors.

Pluggable memory providers (Honcho, vector stores via plugin ABC);
**credential pool rotation** (multiple keys per provider, least-used
strategy, auto-failover on 401); Camofox anti-detection browser backend with
VNC debugging; inline diff previews in the tool feed; API server streams tool
progress + session continuity headers; secret-exfiltration scanning of
browser URLs and LLM responses; expanded credential-directory protections;
`/yolo`, `/btw` (ephemeral side questions), `/profile` commands.

**Olympus relevance** — *Already have:* multi-key model pool (per-role);
memory is deliberately not pluggable (event-sourced, signed). *Candidates:*
auto-rotation on 401/429 within the pool (ours assigns, but failover
behavior is worth verifying); secret-exfiltration scan on **outbound**
browser URLs and model responses — a strong complement to our egress choke
(`egress.py`) that inspects content, not just destinations; `/btw` ephemeral
side-questions that don't pollute session memory.

---

### v0.6.0 — "The Multi-Instance Release" — 2026-03-30 (`v2026.3.30`)

95 PRs in 2 days.

Profiles — isolated instances (config, memory, sessions, gateway) via
`hermes profile create` / `hermes -p`; MCP **server** mode (`hermes mcp
serve`, stdio + HTTP); official Docker images; fallback provider chains;
Feishu/Lark + WeCom adapters; Slack multi-workspace OAuth; Exa search;
remote skill/credential mounting into containers.

**Olympus relevance** — *Already have:* Docker deploy, per-user account
isolation. *Candidates:* named profiles for wholly isolated instances (e.g.
"work" vs "personal" Olympus on one host — accounts share one brain today);
**MCP server mode** exposing Olympus to MCP clients — we expose an
OpenAI-compatible endpoint (`openai_server.py`) but not MCP, and MCP is
winning as the integration surface; provider fallback chains (pool assigns
roles but has no ordered failover chain).

---

### v0.5.0 — "The Hardening Release" — 2026-03-28 (`v2026.3.28`)

157 core PRs + community.

Hugging Face provider; `/model` overhaul; Telegram Private Chat Topics with
per-topic skill binding; native Modal SDK backend; plugin lifecycle hooks
(`pre_llm_call`, `post_llm_call`, `on_session_start/end`); tool-use
enforcement for GPT models; supply-chain: compromised `litellm` removed, all
dependency ranges pinned, audit CI on PRs; Nix flake; SQLite WAL contention
fix (15–20s freezes); zip-slip fix in self-update.

**Olympus relevance** — *Already have:* pinned deps + supply-chain policy,
signed releases. *Candidates:* plugin lifecycle hooks around the LLM call —
the clean way to let users add telemetry/guardrails without forking (today
`@plugin` only adds tools); per-topic/channel skill binding (bind a specific
specialist or skill set to a Telegram topic or Discord channel).

---

### v0.4.0 — "The Platform Expansion Release" — 2026-03-23 (`v2026.3.23`)

280 core PRs, ~200 bug fixes, 16 community contributors.

OpenAI-compatible `/v1/chat/completions` server + cron REST API; MCP client
management CLI with OAuth 2.1 PKCE; 6 new messaging adapters (Signal,
DingTalk, SMS/Twilio, Mattermost, Matrix, Webhook); 4 providers (GitHub
Copilot OAuth 400k ctx, Alibaba DashScope, Kilo Code, OpenCode Zen); `@file`
/ `@url` context injection with tab completion; streaming by default;
gateway prompt caching across turns; structured context compression with
token-budget tail protection; SSRF protection for vision/web tools; malicious
code pre-execution scanner; real-time config reload with `${ENV_VAR}`
substitution.

**Olympus relevance** — *Already have:* OpenAI-compatible server, Signal,
SSRF gates, sandbox scanning. *Candidates:* `@file`/`@url` context references
in chat (fast context injection UX for the CLI/interactive mode); gateway
prompt caching across turns (cost); config hot-reload.

---

### v0.3.0 — "The Streaming, Plugins, and Provider Release" — 2026-03-17 (`v2026.3.17`)

50+ bug fixes; 220+ core PRs.

Unified token streaming across CLI and all gateways; plugin architecture
(`~/.hermes/plugins/`); native Anthropic provider with Claude Code credential
auto-discovery, OAuth PKCE, prompt caching; learned approvals ("remember safe
commands") + `/stop`; voice mode (push-to-talk CLI, Telegram/Discord voice
notes, Discord voice channels, local Whisper STT); Honcho memory
integration; concurrent tool execution; PII redaction before provider calls;
browser CDP connect; ACP editor integration (VS Code/Zed/JetBrains);
persistent shell state across tool calls; agentic on-policy distillation
(OPD) RL environment.

**Olympus relevance** — *Already have:* plugins, Anthropic-first provider,
approvals (ours are risk-tiered rather than learned), parallel tools,
concurrent DAG. *Candidates:* **voice input (STT)** — Olympus is TTS-only
today, and local Whisper push-to-talk plus voice notes on
Telegram/Discord/WhatsApp is a real capability gap; persistent shell state
across `run_command` calls; learned approvals as a *narrow* complement to the
risk-tier spine (remember approved-safe command patterns per user).

---

### v0.2.0 — first tagged release — 2026-03-12 (`v2026.3.12`)

216 PRs from 63 contributors in ~2 weeks; 3,289 tests.

Multi-platform gateway (Telegram, Discord, Slack, WhatsApp, Signal, Email,
Home Assistant); MCP client (stdio + HTTP, reconnection, sampling); 70+
bundled skills with conditional activation and a Skills Hub; centralized
provider router; ACP editor integration; filesystem checkpoints before
destructive ops with rollback; git worktree isolation (`hermes -w`); themed
CLI; provider fallback; session compression; security hardening (path
traversal, shell injection, symlink boundaries).

**Olympus relevance** — baseline; Olympus covers nearly all of this.
Noteworthy: filesystem checkpoint-before-destructive-op with rollback is a
nice belt-and-braces layer under an approval spine; git worktree isolation
for parallel coding tasks.

---

## Consolidated gap analysis

### Where Olympus already matches or exceeds Hermes

- **Verification**: Aletheia hallucination controller + signed, replayable
  decision logs (`witness.py`) — Hermes only got evidence-based goal
  verification in v0.18 and has no signed audit trail.
- **Approvals/safety spine**: risk-tiered actions, autonomy dial, dual gates,
  capability separation for ingesting specialists, egress choke / sovereign
  mode — richer than Hermes's learned approvals + `/yolo`.
- **Benchmark-gated self-improvement**: `gate_prompt`/`gate_skills` with
  regression rollback vs Hermes's rubric-graded review.
- **Orchestration**: DAG planning with parallel fan-out has existed in
  Olympus from the start; Hermes reached parallel background fan-out in
  v0.17–v0.18.
- Session FTS5 search, encrypted signed backups, pip/pipx + one-line
  installers + self-update, supply-chain policy, OpenAI-compatible endpoint.

### Candidate list for a later adopt/skip decision

Ranked by (impact on Olympus's mission) × (fit with existing architecture) ÷
(effort). **No implementation implied.**

| # | Candidate | From | Why it fits Olympus | Effort |
|---|-----------|------|--------------------|--------|
| 1 | **Standing goals with completion contracts** (`/goal` + evidence-judged done conditions) | v0.13 + v0.18 | Heartbeat already runs cycles; a user-stated goal that persists, tracks progress, and is judged done by Aletheia-style evidence is the natural next step for "self-sufficient" | M |
| 2 | **Skill-library curation cycle** (grade / consolidate / prune on a schedule) | v0.12, v0.17 | We gate skill *additions* but never retire; Metis's daily cycle is the obvious host | S–M |
| 3 | **Cross-session prompt caching** (1h TTL for stable system prompt + skills) | v0.14 | Pure cost/latency win on the Anthropic backend; zero behavior change | S |
| 4 | **Voice input (STT)** — local Whisper + voice notes on Telegram/WhatsApp/Discord | v0.3, v0.9 | We're TTS-only; voice-note → transcript → Zeus is high-visibility UX | M |
| 5 | **Post-write delta lint / per-turn file verification** for Hephaestus | v0.13, v0.14 | Catches silent write failures cheaply; complements benchmark gating | S |
| 6 | **Outbound content exfiltration scanning** (URLs + responses scanned for encoded secrets) | v0.7 | Direct upgrade to `egress.py`: inspect content, not just destination | M |
| 7 | **`/steer` mid-run steering + `/undo` turn revert** | v0.11, v0.16 | Operator control while a long DAG run is in flight; today it's wait-or-abort | M |
| 8 | **MCP server mode** (expose Olympus to MCP clients) | v0.6 | We serve OpenAI-compatible only; MCP is becoming the default integration surface | M |
| 9 | **Plugin lifecycle hooks** (`pre/post_llm_call`, tool block/rewrite) | v0.5, v0.11 | Lets users attach guardrails/telemetry without forking; composes with the approval spine | S–M |
| 10 | **Dashboard administration** (channels, credentials, connectors from the web UI) | v0.16 | Biggest onboarding-UX gap vs Hermes; `web.py` is the host | L |
| 11 | **Session auto-resume after gateway restart** | v0.13, v0.18 | Durability parity for long conversations | M |
| 12 | **Activity-based timeouts** + background watch-patterns | v0.8, v0.9 | Stops killing active long jobs; alert on output patterns | S |
| 13 | **Provider fallback chains / 401-429 rotation** in the model pool | v0.6, v0.7 | Resilience; verify what the pool already does before building | S |
| 14 | **MoA ensembles as a selectable "model"** | v0.18 | Interesting for the council (Zeus could consult an ensemble), but partially redundant with the council itself | M–L |
| 15 | **`@file`/`@url` context injection** in interactive mode | v0.4 | CLI ergonomics | S |
| 16 | **OSV/malware scanning for MCP servers & skill imports** | v0.8, v0.17 | Extends two-tier connector gating and `skillpack` imports | S–M |

Deliberately **not** shortlisted: native desktop app (huge surface, web UI
covers it), platform breadth race (iMessage/WeChat/QQ/Feishu/LINE/etc. —
adopt per user demand only), Nous Tool Gateway (vendor plumbing), pet system,
kanban board UI (our DAG orchestrator covers coordination; a board is UI
sugar), i18n (revisit when there's non-English demand), Termux/Android.

### Recurring themes worth stealing at the philosophy level

1. **Ship a P0/P1 sweep release** (v0.18): dedicating a cycle to zeroing the
   priority backlog, with the stats published, built enormous trust.
2. **Performance is a feature** (v0.12, v0.14, v0.15): they publish cold-start
   and per-turn numbers each release. Olympus has never profiled its startup
   or per-turn overhead.
3. **Fail-closed by default, opt-in for insecure** (v0.13, v0.15.1, v0.18):
   matches Olympus doctrine — and their v0.12 redaction flip-flop shows why
   precision matters before enabling a guard by default.
4. **Reverts are normal** (v0.12, v0.18): they ship, measure, and roll back
   publicly (kanban board, cua-driver, prompt-caching toggle) — same spirit
   as our benchmark-gated `gate_prompt`.

---

## Future releases

Hermes publishes no roadmap and uses no GitHub milestones. Observable
signals:

- **Cadence**: minor releases every 1–2 weeks (March–July 2026 average
  ~11 days). Next release (presumably v0.19.0) is statistically due
  **mid-July 2026**.
- **Trajectory** from the last three releases: desktop app maturation,
  gateway scaling (relay phases 4+, scale-to-zero follow-through),
  verification/judgment deepening, MoA expansion, and continued
  security/cron reliability waves are the likeliest continuations.

When a new release appears, append it above using the same template:
*stats → what's in it → Olympus relevance (already have / gaps / candidates)*,
then re-rank the candidate table.

---

*Log of analysis passes:*

| Date | Covered | By |
|------|---------|----|
| 2026-07-02 | Initial pass: all 19 releases (v0.2.0 → v0.18.0), full Olympus capability mapping | Claude session (hermes-release-tracking) |
