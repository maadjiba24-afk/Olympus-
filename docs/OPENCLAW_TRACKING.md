# OpenClaw Release Tracking

Analysis-only tracker for [openclaw/openclaw](https://github.com/openclaw/openclaw).
This document catalogs every release (past and present), the feature set each one
introduced, and a running watchlist of what is coming next, so we can later decide
which ideas are worth implementing in Olympus. **No OpenClaw code is used here** —
this is competitive/inspiration analysis only.

- **Last checked:** 2026-07-18
- **Latest stable release:** `2026.7.1` (2026-07-13)
- **Latest pre-release:** `2026.7.2-beta.3` (2026-07-18)
- **Maintenance:** a scheduled session check re-scans the releases page and appends
  new versions to this file as they ship.

---

## 1. What OpenClaw is

OpenClaw is a self-hosted, single-user **personal AI assistant gateway** (MIT,
TypeScript/Node ≥ 22.19, ~382k stars, ~63k commits). A long-running **Gateway
daemon** is the control plane for sessions, channels, tools, and events; the
assistant is reachable through the chat apps the user already has. Created by
Peter Steinberger and community; the project renamed several times:

| Era | Name | Versions | Period |
|---|---|---|---|
| 1 | **warelay** (WhatsApp relay CLI) | 0.1.1 → 1.3.0 | Nov–Dec 2025 |
| 2 | **clawdis** | 2.0.0-beta1 → beta5 | Dec 2025 – Jan 2026 |
| 3 | **clawdbot** | CalVer 2026.1.x | Jan 2026 |
| 4 | **openclaw** (npm rename w/ shim) | 2026.1.29 → today | Jan 31, 2026 – present |

Release cadence is intense: CalVer (`YYYY.M.D`), roughly weekly stables with
several betas in between (~215 releases in 7 months; the 2026.7.1 beta alone
merged 222 PRs).

---

## 2. Feature catalog (cumulative, by category)

Everything OpenClaw has shipped to date, grouped for adoption decisions.

### 2.1 Channels (chat-app integrations)
- 20+ channels: WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Google Chat,
  Microsoft Teams, Matrix (incl. E2EE), IRC, Feishu/Lark, LINE, Mattermost,
  Nextcloud Talk, Nostr, Synology Chat, Tlon, Twitch, Zalo, WeChat, QQ Bot, WebChat.
- Channel abstractions: per-channel routing to isolated agents/workspaces,
  DM topics as separate sessions, per-account DM session scoping, group-chat
  activation modes, ambient turns in groups, thread-bound conversations.
- Delivery quality: native streaming with progress bubbles, rich HTML/markdown
  rendering per platform, reply-quoting correctness, reaction-based approvals
  (approve/deny a command by emoji reaction), native reactions, native polls
  (iMessage), typing indicators, message splitting.
- Slack **router relay mode**: a central router dispatches mentions/threads to the
  right gateway in managed multi-gateway deployments.
- Mattermost native slash commands (`/oc_queue` tunes queuing mode and debounce).

### 2.2 Model / provider layer
- Multi-provider catalog: Anthropic, OpenAI (+ Codex OAuth, no API key needed),
  Google Gemini / Vertex, xAI Grok (incl. OAuth for SuperGrok), DeepSeek, GLM/Zhipu,
  Moonshot Kimi, MiniMax (OAuth), Ollama (+ Ollama Cloud), LM Studio, OpenRouter,
  Vercel AI Gateway, Cloudflare AI Gateway, Amazon Bedrock (+ Guardrails),
  Cerebras, Arcee AI, StepFun, Xiaomi, GitHub Copilot, Venice, and more.
- Day-one support for new frontier models (GPT-5.x, Opus/Sonnet 4.x, Gemini 3.x,
  GLM-5.x, Kimi K2.x) with forward-compatibility fallbacks.
- **Model failover chains**: configured fallback models on rate-limit/credit
  exhaustion, including for scheduled (cron) runs.
- Per-conversation / per-contact / per-DM model overrides; model shorthands
  ("opus", "sonnet", "gpt"); searchable model picker; `/models` in chat.
- **Fast mode** incl. `/fast auto` (start fast, hand long work back to normal mode).
- Provider auth registry: plugin-driven OAuth/API-key flows (`models auth login`),
  named auth profiles, auth-profile rotation, Model Auth status cards.
- Tiered model pricing support; token-usage footers in chat; provider
  request/timing logs; oversized/malformed provider responses rejected safely.

### 2.3 Agent runtime
- Multi-agent routing: several isolated agents (own workspace, memory, channel
  bindings) behind one gateway; agent-to-agent messaging with turn limits;
  **nested subagents** with configurable depth; subagent context forking.
- External harness attachment: `openclaw attach` runs an outside coding
  harness against an existing gateway session; ACP (agent client protocol)
  thread-bound agents; Codex/Claude-CLI/Copilot run as embedded runtimes.
- Session management: automatic compaction with pre-compaction memory flush and
  checkpoints, session pruning, daily/idle session rollover, `/new`, `/stop`,
  queued follow-ups with debounce, human-delay pacing between replies.
- **Heartbeats**: periodic autonomous wake-ups per agent with dedicated context
  (proactive behavior, "check if anything needs attention").
- Cron/scheduling: cron jobs with per-job model + fallbacks, delivery modes,
  wait/blocking controls, and **event-driven runs** (`on-exit` schedule kind wakes
  an agent when a watched command exits); Raft-style external wake ("wake agent
  when workspace has pending work").
- Background task control plane shared across surfaces (tasks chat board).
- Streaming architecture: block-level streaming to channels, provisional text
  replacement, prompt-cache stability across tool-heavy turns.

### 2.4 Memory
- Layered memory: vector search over SQLite (later LanceDB, incl. cloud storage),
  **hybrid BM25 + vector search**, asymmetric embedding endpoints, configurable
  embedding dims, GitHub Copilot / Voyage AI embeddings.
- **Memory Wiki**: agent-maintained wiki of concepts/pages with health linting,
  freshness warnings, re-ingestion that preserves user-written notes.
- **Active Memory plugin**: automatic context retrieval during conversations.
- **Dreaming / REM**: nightly background jobs that consolidate memory ("light and
  REM dreaming phases"), grounded REM backfill from a structured diary; ChatGPT
  export ingestion.
- QMD workspace memory backend; automatic pre-compaction flush so nothing is lost
  when context is trimmed.

### 2.5 Plugins & ecosystem
- First-class plugin system: loader + CLI management, plugin SDK, HTTP hooks,
  gateway RPC methods, channel plugins, provider plugins as independent npm
  packages loaded at startup.
- **ClawHub**: a plugin marketplace/registry with metadata, icons, install
  policies (operator-governed installs), trust warnings with copy-ready
  `plugins.allow` examples, and lookalike-path rejection.
- **Skills**: managed skills platform (meme-maker, Python debugging, healthcheck
  security audit, etc.), skill caching, global targeting.
- Hooks system (v2026.1.16): lifecycle hooks incl. plugin approval hooks and
  policy hooks that block/rewrite sensitive tool calls.
- Config: modular via `$include`; SecretRef indirection for all credentials
  (secrets kept out of config files); localized setup wizards (EN/中文).

### 2.6 Tools & capabilities
- Web search (Brave API, SearXNG, parallel bundled search), web fetch.
- Browser automation: dedicated browser control with CDP, batched actions,
  selector targeting, modal handling, **Chrome extension relay takeover** (drive
  the user's own browser), realtime browser transport.
- Media: inbound image/audio/video understanding; image generation (Codex OAuth,
  OpenRouter, MiniMax); video generation (Seedance 2.0); adaptive model-aware
  image compression; native **PDF analysis tool**; file-transfer plugin.
- Voice: wake word on macOS/iOS, continuous voice on Android, Talk Mode realtime
  voice sessions, TTS stack (ElevenLabs v3, Azure, Gemini, Edge, Xiaomi, MLX on
  macOS), voice-note transcription, Twilio/Telnyx phone-call streaming with
  barge-in, Google Meet / Discord voice participation with meeting notes and
  transcript-backed summaries.
- `openclaw infer`: CLI hub to run inference against any configured provider.
- OpenAI-compatible HTTP endpoint exposed by the gateway; HTTP `/tools/invoke`
  endpoint with auth; MCP client support (HTTP servers) + Tool Search / Code Mode.
- **Live Canvas**: agent-driven visual workspace (A2UI).

### 2.7 Clients / surfaces
- Control UI (web dashboard): chat, session/agent management, workspace rails,
  extension health, copy-as-markdown, structured chat bubbles.
- TUI with syntax highlighting; macOS menu-bar app; Windows Hub; iOS + Android
  node apps (iOS 26 visual refresh, Android Talk Mode); **Apple Watch companion**
  with inbox + controls; iOS share extension.
- WebChat channel served by the gateway.
- **Olympus absorption (2026-07-22):** where OpenClaw ships a stack of native
  clients (menu-bar app, Windows Hub, iOS/Android node apps, Apple Watch), the
  Olympus web UI is instead an **installable PWA** — a web app manifest,
  offline-shell service worker, and app icons rendered by a pure-Python PNG
  encoder (stdlib `zlib` only, no new dependencies). One surface, add-to-home-
  screen on any platform, served by the existing web server (`olympus/pwa.py`).

### 2.8 Security & trust
- Inbound DMs untrusted by default; pairing-based access policies; DM lockdown
  defaults (2026.1.8 hardening release).
- Sandboxing: non-main sessions in Docker/SSH sandboxes; per-agent sandbox
  defaults; approval-gated exec (`/approve`, reaction approvals, mobile approval
  resolution); exec approvals return to the originating channel.
- **Per-conversation capability profiles** (2026.7.1): scoped tool/access
  boundaries per conversation; per-sender tool policies.
- TLS 1.3 minimum on gateway listeners; WebSocket origin validation; SSRF
  protections; SHA-256 config hashing; secret redaction in logs/diagnostics;
  DOMPurify patching; prompt-injection guardrails in system prompts.
- Security audit skill; `security-review`-style healthchecks; signed release
  verification; official Docker Hub mirror.

### 2.9 Ops & DX
- One-command onboarding (`openclaw onboard --install-daemon`) with
  launchd/systemd service install; health-check endpoints for container
  orchestration; graceful update handoff through a durable state DB (survives
  gateway restarts with continuation messages); restart diagnostics.
- QA-Lab: benchmark/parity test infrastructure, scenario building with VNC
  screenshot/video capture; character-vibes evaluation reports.
- OpenTelemetry observability; structured provider logs; gateway lazy-loading
  and startup-latency work; plugin metadata caching.

---

## 3. Release timeline

Stable releases with their headline content (newest first). Betas exist between
almost every pair; they are folded into the stable that shipped them unless a
beta introduced something notable on its own.

### OpenClaw era (2026)

| Version | Date | Headline changes |
|---|---|---|
| **2026.7.2-beta.3** *(pre-release)* | 2026-07-18 | Supersedes beta.2 (07-17); ~300 contributions. Incremental hardening of the 7.2 cycle: Codex CLI bump (0.144.6), **cron claim-race fix**, gateway restart-admission and reply-finalization recovery, Signal reconnection, config auto-saving. Carries: cloud-worker sessions, headless-node device capabilities, allowlist-privilege fix, Linux deb/AppImage, plugin provenance verification, guided setup. |
| **2026.7.2-beta.2** *(pre-release)* | 2026-07-17 | Supersedes beta.1; 400+ fixes from 100+ contributors. New vs beta.1: **Linux deb/AppImage bundles** + Windows install continues right after winget adds Node; iOS fresh-install credential-handling security fix; **plugin provenance verification for untrusted sources**; Unicode-safe truncation/display; stalled-provider network timeouts; gateway restart/session-recovery hardening. Carries the cycle themes: cloud-worker sessions, headless-node device capabilities, channel-safety fixes, guided setup. |
| **2026.7.2-beta.1** *(pre-release)* | 2026-07-15 | Next cycle opens (2,425 PRs merged). **Remote coding sessions on cloud workers** (Control UI sessions run on remote workers; Codex/Claude catalog sessions open in terminals on their owning hosts); foreground Voice Wake on Android; camera/location/notification capabilities exposed from headless Linux nodes; **channel-allowlist fix: allowlists no longer grant owner access**; Telegram durable-ingress preserved across restarts; Signal stop/approval controls stay responsive mid-turn; guided Control UI provider/channel setup; gateway restart-admission wedge fix. |
| **2026.7.1** | 2026-07-13 | The six-beta cycle consolidated: 2,018 PRs from 532 contributors. **GPT-5.6 default + Claude Sonnet 5/Mythos 5, Tencent Hy3, Meta Muse Spark 1.1, Featherless**; **ClawRouter** (credential-scoped dynamic model discovery, budget tracking); `openclaw attach` grants Claude Code temporary TTL-bound session access; **Crestodian conversational onboarding** (validated connections, resumable setup); Control UI overhaul (session-first, tasks/usage/pairing/health in the conversation view); **change-triggered scheduled work** (run only when something changed) + on-exit cron; workspace terminals on web/iOS/Android; remote browser-tab pairing; mobile offline caches, Watch voice, auto session titles; **gateway safe mode ends restart loops**; iMessage polls; Telegram `/login` pairing + `/steer`/`/tell` run steering. |
| **2026.6.11** | 2026-06-30 | Dependability release: 150+ fixes for misplaced replies, stuck sends, reconnects, model setup; per-DM model overrides; Mattermost `/oc_queue` slash command; Slack router relay mode; Raft external agent wake; safer admin defaults; official Docker Hub mirror. |
| **2026.6.10** | 2026-06-24 | **`/fast auto`** (auto fast-mode for short exchanges); session-transcript SDK for plugins; GLM-5.2 / Kimi K2.7 catalog; cross-channel session identity fix. |
| **2026.6.9** | 2026-06-21 | Telegram rich-HTML delivery; agent turn recovery (retries, history repair, reply reconciliation); Codex auto plugin approvals + remote-node exec; provider plugins as independent npm packages; Watch controls on iOS. |
| **2026.6.8** | ~2026-06-17 | GLM-5.2 + Claude Haiku 4.5 support; richer Telegram/WhatsApp delivery; native usage footers; safer model routing; resilient memory. |
| **2026.6.6 / 2026.6.5** | 2026-06-07..10 | Reasoning stripped from QQBot output; MCP results can no longer poison sessions; Anthropic extended-thinking recovery after restarts; bundled parallel web search; Matrix voice; Vertex model resolution. |
| **2026.6.1 / 2026.6.2** | 2026-06-01..03 | Operator install policies for plugins/skills (governed installs); safer channel delivery; steadier chat UI; stricter config/exec safety checks. |
| **2026.5.28 – 2026.5.31** | 2026-05-28..31 | Agent/Codex runtime recovery; cwd/workspace separation; Claude Opus 4.8 support; faster Control UI chat; iOS Pro UI refresh; tighter input validation. |
| **2026.5.24 – 2026.5.26** | 2026-05-24..27 | Transcript-backed meeting summaries; named auth profiles; Signal reaction approvals; adaptive image compression; Discord meeting-notes plugin; plugin metadata caching (faster startup); Alpine installer; Windows fixes. |
| **2026.5.19** | 2026-05-20 | Mac Settings redesign; meme-maker & Python-debug skills; overlapped gateway startup; **Android Talk Mode** realtime voice; QA-Lab runtime parity testing; Node 22.19 floor; Docker/Podman install path; HTTPS proxy support. |
| **2026.5.16** | ~2026-05-17 | **xAI Grok OAuth** (SuperGrok); CLI cron scheduling with wait controls; localized EN/中文 wizards; skill caching; Telegram group ambient turns. |
| **2026.5.12 – 2026.5.14** | 2026-05-12..15 | Per-agent bootstrap profile overrides; **Telnyx voice-call streaming**; per-sender tool policies; ACP session lineage; agent-to-agent turn limit raised to 20; Slack unfurl controls; QA scenario capture (VNC). |
| **2026.5.2 – 2026.5.7** | 2026-05-01..07 | **External plugin installs with ClawHub metadata**; bundled file-transfer plugin; leaner gateway hot paths; Google Meet voice via Twilio (paced audio, barge-in); heartbeat-poisoning fix; Codex OAuth route hotfix. |
| **2026.4.25 – 2026.4.29** | 2026-04-25..29 | **TTS overhaul** (Azure, Xiaomi, ElevenLabs v3); plugin registry cold storage; expanded OpenTelemetry; Cerebras plugin; generic browser realtime transport; asymmetric embeddings for memory. |
| **2026.4.23 – 2026.4.24** | 2026-04-24..25 | **Image generation via Codex OAuth / OpenRouter** (no API key); subagent context forking; Google Meet bundled participant plugin with personal Google auth. |
| **2026.4.14 – 2026.4.20** | 2026-04-14..21 | Default model → Claude Opus 4.7; Gemini TTS; Model Auth status cards; **LanceDB cloud storage** + Copilot embeddings for memory; tiered pricing; wizard security disclaimers; GPT-5 family polish. |
| **2026.4.9 – 2026.4.11** | 2026-04-09..12 | **Bundled Codex provider with native threading**; **Active Memory plugin** (auto context retrieval); macOS MLX speech; Seedance 2.0 video; **grounded REM memory backfill + structured diary UI**; ChatGPT-export memory ingestion; Teams reactions; browser/tool security hardening. |
| **2026.4.7 / 2026.4.8** | 2026-04-08 | **`openclaw infer` CLI**; media-generation auto-fallback; memory-wiki health linting; webhook ingress plugin; session compaction checkpoints; Arcee AI provider. |
| **2026.3.31 – 2026.4.1** | 2026-03-29..04-01 | Shared **background-task control plane**; QQ Bot channel; Matrix history/streaming/proxy; **MCP HTTP server support**; tasks chat board; SearXNG search; **Bedrock Guardrails**; voice wake mode. |
| **2026.3.28** | 2026-03-29 | xAI/Grok Responses API; MiniMax image generation; **plugin approval hooks**; OpenAI patch-application default. |
| **2026.3.11 – 2026.3.13** | 2026-03-11..14 | Dashboard refresh (modular views); **fast-mode toggles** (OpenAI/Anthropic); provider-plugin architecture; iOS home canvas redesign; Android chat settings redesign; browser batched actions; WebSocket origin validation. |
| **2026.3.1 / 2026.3.2** | 2026-03-02..03 | **SecretRef across all credential surfaces**; native **PDF analysis tool**; Android device capabilities; health-check endpoints; Telegram DM topics. |
| **2026.2.19 – 2026.2.26** | 2026-02-18..27 | **Apple Watch companion app**; iOS share extension; external **secrets-management workflow**; ACP thread-bound agents; agent-routing CLI; Gemini 3.1 support; SSRF/ACP security hardening. |
| **2026.2.15 – 2026.2.17** | 2026-02-16..18 | Discord Components v2 (interactive prompts); **nested subagents with configurable depth**; Sonnet 4.6 support; Slack native streaming; SHA-256 config hashing. |
| **2026.2.1 – 2026.2.6** | 2026-02-02..07 | Opus 4.6 + Codex model support; xAI provider; Voyage AI embeddings; Cloudflare AI Gateway; **Agents dashboard**; Feishu plugin; QMD memory backend; system-prompt guardrails; **TLS 1.3 minimum**; healthcheck security skill. |
| **2026.1.29 / 2026.1.30** | 2026-01-30..31 | **Rename clawdbot → openclaw** (npm shim for compatibility); shell completions; MiniMax OAuth; per-account DM session scoping; Telegram stickers with vision caching. |

### Clawdbot era (Jan 2026)

| Version | Date | Headline changes |
|---|---|---|
| 2026.1.23 / 2026.1.24 | Jan 24–25 | LINE channel; **exec approvals via `/approve`**; Telegram TTS in core; HTTP `/tools/invoke`; Edge TTS fallback; DM topics as sessions. |
| 2026.1.20 | Jan 21 | **Hybrid memory search (BM25 + vector)**; TUI syntax highlighting; searchable model picker; copy-as-markdown. |
| 2026.1.16 | Jan 17 | **Hooks system**; inbound media understanding (image/audio/video); Zalo Personal channel; Vercel AI Gateway auth. |
| 2026.1.14 / 2026.1.15 | Jan 15–16 | **Web search tools (Brave)**; **Chrome extension relay takeover**; plugin HTTP hooks; provider-auth registry + OAuth login flows; per-agent heartbeats. |
| 2026.1.12 | Jan 13 | "Providers" → **"channels"** rename; **vector memory search (SQLite)**; compaction modes; voice-call plugin parity. |
| 2026.1.11 | Jan 12 | **Plugins become first-class** (loader + CLI); modular config `$include`; automatic pre-compaction memory flush. |
| 2026.1.8 – 2026.1.10 | Jan 8–11 | **Security hardening: DMs locked down by default**; per-agent sandbox defaults; Microsoft Teams channel; OpenAI-compatible gateway endpoint; table status command; Codex CLI fallback; human-delay reply pacing. |
| 2026.1.5 | Jan 5 | Image-specific model routing + dedicated image tool; model shorthands (opus/sonnet/gpt). |

### Clawdis era (Dec 2025)

| Version | Date | Headline changes |
|---|---|---|
| 2.0.0-beta5 | Jan 3 | **First-class tools replace skills**; per-session model selection; group-chat activation modes; Discord transport; gateway webhooks + command queue modes. |
| 2.0.0-beta2–4 | Dec 21–27 | Bundled gateway (bun compile); managed skills platform; macOS app connection settings; canvas host on gateway port; Discord provider. |
| 2.0.0-beta1 | Dec 19 | **The pivot**: macOS companion app, WebSocket Gateway control plane, iOS node, voice wake, browser control; dropped legacy providers. |

### warelay era (Nov–Dec 2025)

| Version | Date | Headline changes |
|---|---|---|
| 1.3.0 | Dec 2 | **Pluggable agent support (Claude, Pi, Codex)** with per-agent CLI builders and safety stop words. |
| 1.2.x | Nov 27–28 | Heartbeat system (configurable intervals, manual sends, dry-run); session idle expiration; media MIME handling. |
| 1.1.0 | Nov 26 | Media resize/recompress; voice-note transcription; command auto-replies with timeouts. |
| 0.1.1–0.1.3 | Nov 25 | Initial release: WhatsApp relay CLI, file logging, working-dir command replies. |

---

## 4. Future / upcoming (watchlist)

OpenClaw does not publish a formal roadmap; "future" is visible through betas and
merge activity. Current signals:

- **2026.7.2 cycle open** (beta.1 2026-07-15): remote cloud-worker sessions, mobile automation parity, channel-safety hardening. Next signals to watch:
  GPT-5.6 support, external harness attach, event-driven cron (`on-exit`),
  capability profiles per conversation, iMessage polls, iOS 26 refresh.
- Recurring investment themes to expect more of: reliability/dependability
  releases (like 2026.6.11), multi-gateway routing (Slack relay mode debuted),
  deeper Codex/Claude-CLI/Copilot harness embedding, memory (wiki + dreaming)
  maturation, and progressive security scoping (capability profiles, per-sender
  policies, operator install policies).

New releases will be appended here by the scheduled check with the same format:
version, date, what's new, what it improves.

---

## 5. Themes most relevant to Olympus (for later decision — analysis only)

Olympus already has a council-of-agents architecture, hallucination controller,
security gate, memory, and self-improvement loop. OpenClaw ideas that map onto
gaps or could amplify existing Olympus systems:

1. **Channel layer** — Olympus has no chat-app presence; OpenClaw's biggest draw
   is "your agent inside WhatsApp/Telegram/Discord". Even one channel plugin
   (Telegram) would change how Olympus is used day-to-day.
2. **Heartbeats + event-driven cron** (`on-exit`, external wake) — pairs
   naturally with Olympus's self-recurring world-scanning loop.
3. **Model failover chains & per-task model overrides** — Olympus's model pool
   already composes by strength/price; failover-on-quota and per-agent overrides
   are incremental wins.
4. **Memory Wiki + Dreaming/REM consolidation** — a structured, self-maintained
   knowledge layer with nightly consolidation; Olympus's memory format could
   adopt the wiki + freshness-linting concepts.
5. **Reaction/`/approve` exec approvals** — Olympus's approval-gated sandbox
   could gain lighter-weight approval UX.
6. **Capability profiles / per-sender tool policies** — matches Olympus's threat
   model; a per-conversation scoping layer is a clean next step for the security
   gate.
7. **Plugin marketplace with install policies + SecretRef** — if Olympus opens a
   plugin surface, OpenClaw's trust-warning + allowlist + secret-indirection
   design is the reference to study.
8. **Prompt-cache-aware session compaction with pre-compaction memory flush** —
   directly relevant to long-running council sessions.
9. **OpenAI-compatible endpoint on the gateway** — Olympus already has
   `docs/OPENAI_ENDPOINT.md`; OpenClaw validates the pattern.
10. **QA-Lab style scenario capture** (VNC screenshots/video, parity benchmarks)
    — would strengthen Olympus's CI-verified capabilities story.

---

## 6. Adoption plan (decided 2026-07-03 — **implemented 2026-07-03**)

All 12 features below are now built, tested, and merged into the Olympus
codebase (concept adoption only — no OpenClaw code was copied). Where each
landed:

| # | Feature | Where it lives |
|---|---|---|
| 1 | Telegram channel | `telegram.py` (pairing, reply quoting, live progress), `pairing.py`, `olympus pair` |
| 2 | Exec approvals via chat | `approvals.py` + `gateway.py` (/approvals, /approve, /deny, held-for-approval footer) |
| 3 | Model failover chains | `config.ModelPool.fallbacks_for`, `OLYMPUS_ROLE_FALLBACKS`, key rotation in `llm.py` |
| 4 | Heartbeats | `agentbeat.py` (/heartbeat, HB_OK quietness contract, compact context) |
| 5 | Event-driven scheduling | `scheduler.py` on_exit kind (`--on-exit`, /onexit) |
| 6 | Capability profiles | `capprofile.py` (full/reader/guest, autonomy caps, `olympus restrict`, OLYMPUS_CHANNEL_PROFILE) |
| 7 | Memory Wiki + dreaming | `wiki.py` (concept pages, freshness lint, nightly dream via heartbeat, /wiki, `olympus wiki`) |
| 8 | Pre-compaction flush | `recall.flush_slice` wired into compaction and reset |
| 9 | Model overrides | `modelpin.py` (/model shorthands), `OLYMPUS_SPECIALIST_MODELS` |
| 10 | SecretRef | `secretref.py` (env:/file:/vault:/keychain:), `olympus secret` |
| 11 | Usage footers | `usage.py` session totals + footer, /usage on·off |
| 12 | Update handoff | `scheduler` interrupted-run resume, `selfupdate` handoff record, heartbeat report |

Original plan for reference:

### Phase 1 — highest value, lowest friction
1. **Telegram channel** — single channel plugin over the pure-HTTP Bot API
   (no browser, no OAuth), with DM pairing/allowlist (untrusted by default),
   streaming progress messages, and reply quoting.
2. **Exec approvals via chat** — surface pending sandbox commands in Telegram;
   approve/deny by reply, routed through the existing security gate.
3. **Model failover chains** — per-role fallback model lists in the pool;
   retry the next model on rate-limit/credit exhaustion instead of failing.
4. **Heartbeats** — per-agent periodic autonomous wake-ups with compact
   dedicated context, wired into the self-recurring scan loop.

### Phase 2 — deepens existing strengths
5. **Event-driven scheduling** — `on-exit`-style triggers: wake an agent when a
   watched command/process exits, alongside time-based cron.
6. **Per-conversation capability profiles** — scoped tool/access boundaries per
   conversation/sender, layered on the security gate.
7. **Memory Wiki + nightly consolidation** — self-maintained wiki of durable
   concepts with freshness linting; scheduled job consolidates session memory
   into it (OpenClaw's "dreaming").
8. **Pre-compaction memory flush** — flush salient facts to memory before any
   context compaction in long council sessions.

### Phase 3 — worth doing, not urgent
9. **Per-agent/per-task model overrides** — pin models per council member or
   conversation, with shorthands (opus/sonnet/gpt).
10. **SecretRef-style secret indirection** — credentials referenced by name in
    config, resolved from env/keychain at runtime, never inline.
11. **Usage/cost footers** — per-reply and per-session token/cost accounting.
12. **Graceful update handoff** — durable state for in-flight work so restarts
    and upgrades resume with a continuation instead of dropping tasks.

### Explicitly not adopting
- 20+ channels, mobile/watch apps, Control UI (contradicts headless-first).
- Voice stack: wake word, TTS, phone calls, meeting bots (no media stack).
- Browser automation / Chrome relay (operator harness already covers this).
- Plugin marketplace (premature for single-user; revisit if a plugin surface
  opens — then reuse OpenClaw's trust-policy design as reference).
- Image/video generation, Live Canvas (outside the verified-answers focus).
