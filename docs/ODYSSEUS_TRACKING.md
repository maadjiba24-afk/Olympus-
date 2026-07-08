# Odysseus Release Tracking

Analysis-only tracker for [pewdiepie-archdaemon/odysseus](https://github.com/pewdiepie-archdaemon/odysseus)
([site](https://pewdiepie-archdaemon.github.io/odysseus/)). This document
catalogs each version (past and present), the features it introduced, a running
watchlist of what is coming next, and — for each idea — whether it is already
implemented in Olympus. **No Odysseus code is used here** — this is
competitive/inspiration analysis only (Odysseus is AGPL-3.0; Olympus is MIT).

- **Last checked:** 2026-07-04
- **Baseline (diff against this on the next check):**
  - `APP_VERSION` = **1.0.1** (`src/constants.py`)
  - `origin/main` @ **dc3530b** (curated release branch — fast-forwarded at releases)
  - `origin/dev` @ **1f6dc80** (rolling default branch)
  - **no git tags, no GitHub releases** (their "releases" are `APP_VERSION`
    bumps + a `main` fast-forward; Docker images publish `:X.Y.Z` to GHCR)
- **Maintenance:** a scheduled session check re-clones the repo and appends new
  versions here as they ship. If the durable trigger is unavailable, re-arm it
  with "make the Odysseus watch permanent" in an interactive session; the doc
  itself is the source of truth for the baseline.

---

## 1. What Odysseus is

A self-hosted **AI workspace** (AGPL-3.0, Python/FastAPI + a single-page web UI)
for chat, autonomous agents, deep research, documents, email, notes/calendar,
and — its signature — running **local models matched to your hardware**. It is
the deployment opposite of Olympus: web-UI "admin console" oriented around
local/self-hosted models, versus Olympus's headless, frontier-API, verified
council. The overlap worth mining is the agent internals.

## 2. Release model

- **`dev`** is the default branch; all PRs land there (rolling, ~110 commits/wk
  and falling from a launch spike of ~940/wk).
- **`main`** is curated; fast-forwarded from `dev` when release-worthy. GHCR:
  `main` → `:latest` + `:X.Y.Z`; `dev` → `:dev` + `:X.Y.Z-dev.<sha>`.
- Version lives in `src/constants.py` as `APP_VERSION`. A "new version" = an
  `APP_VERSION` bump + a `main` fast-forward, **not** a GitHub release/tag.

## 3. Versions to date

| Version | Date | What it was |
|---|---|---|
| **1.0.0** | 2026-05-31 | Initial public launch ("Odysseus v1.0") — shipped essentially complete |
| **1.0.1** | 2026-06-19 | First curated point release (teacher-escalation Tier 2, OAuth email, a11y, portable Windows launcher, `manage_bg_jobs`) |

As of the baseline, `dev` holds ~13 unreleased commits, almost all bug/security
fixes (DNS-rebinding block in search, research file-path confinement,
case-insensitive sensitive-file deny-list, Ollama runner hardening) — the next
curated `main` merge is expected to be a hardening release (likely v1.0.2).

## 4. Feature catalog (cumulative)

- **Agent system** — streaming multi-round loop, plan mode (read-only), 67
  tools retrieved per-request via a RAG **tool index** (their answer to context
  bloat), skills (`SKILL.md`, auto-extraction, GitHub import), **teacher
  escalation** (a SOTA model fixes a failed local-model turn and writes a skill),
  persistent memory with vector recall, scheduled agent tasks, workspace
  confinement, sensitive-file deny-list, Anthropic prompt caching, `ask_user`.
- **Cookbook** — hardware probe + 270-model catalog scored to a GPU-bandwidth
  table; one-click download/serve over llama.cpp / vLLM / SGLang / Ollama.
- **Deep Research** — IterResearch-style plan → search/read/extract loop →
  cited report; date-grounded queries; untrusted-content wrapping.
- **Apps** — email (IMAP/SMTP, AI summaries, spam triage, **style-matched
  drafts**), documents editor, notes/todos/reminders, CalDAV calendar/contacts,
  Compare (blind A/B), gallery/image editing.
- **Search** — pluggable providers: SearXNG, Brave, DuckDuckGo, Google PSE,
  Tavily, Serper.
- **Integrations** — MCP client + 4 MCP servers; scoped external-agent APIs
  (Claude Code, Codex); STT/TTS; ntfy; webhooks; Vaultwarden; LAN pairing.
- **Security** — bcrypt + 2FA, admin/non-admin split, prompt-injection wrapping,
  CSP/security headers, SSRF guards. Known gap: no OS-level agent sandbox.
- **Deploy** — Docker Compose (+ NVIDIA/AMD GPU), native, macOS bundle,
  portable Windows, systemd, multi-arch GHCR.

## 5. Published roadmap (their "coming next")

Agent context-bloat reduction (slimmer prompts, smaller default tool sets);
Cookbook model-scan ranking overhaul; Cookbook error feedback with copyable
logs; SGLang cross-platform; Deep Research hardware presets; skill/tool
prompt-injection audit; degraded-state reporting; email performance audit;
provider setup/probing audit; editor/notes-todo AI integration; accessibility
pass; vendored CDN assets for offline mode; **agent shell/FS sandbox** (the
acknowledged #1 gap); backup/restore helper.

---

## 6. Adoption status in Olympus

Which Odysseus ideas we evaluated, and where they landed. ✅ implemented,
➖ intentionally skipped, ⏳ candidate.

| Odysseus idea | Status in Olympus | Notes |
|---|---|---|
| DNS-rebinding fetch hardening (#704) | ✅ `security.resolve_pinned_ip` + pinned openers | connect to the SSRF-validated IP; HTTPS keeps SNI/cert on the hostname |
| Case-insensitive sensitive-file deny-list (#5097) | ✅ `sandbox.is_sensitive_path` | guards read/grep/glob/edit |
| Code-nav tools (grep/glob/ranged read/edit) | ✅ Hephaestus loadout | `edit_file` stages via always-hold approval (never auto-executes) |
| Per-request RAG tool index (context-bloat) | ✅ `toolselect` (lexical, deterministic) | drops-only, strictly after security filters |
| Teacher escalation (weak→strong + write a skill) | ✅ `teacher.py` | escalates a failed rework to the strongest pool member; benchmark-gated skill |
| Deep Research (IterResearch) | ✅ `research.py` + `olympus research` + `trigger_research` tool | pool-staged; Aletheia-style verification section |
| Pluggable search providers | ✅ `websearch.py` | SearXNG/Brave/Tavily/Serper/PSE + DDG fallback |
| Skill import from GitHub URLs | ✅ `skillpack.import_url` | in-memory tarball, always provisional + scanned |
| `ask_user` (mid-run question) | ✅ `interaction.py` | TUI blocks; web/gateways surface-and-proceed; headless proceeds with a stated assumption |
| Style-matched email drafting | ✅ `emailstyle.py` | Angelos learns voice from sent mail (Gmail) |
| Cookbook (local model serving) | ➖ off-mission | Olympus consumes Ollama/vLLM/LM Studio as endpoints, doesn't manage GPU serving |
| Web-UI apps (docs editor, gallery, calendar UI, themes) | ➖ off-mission | Olympus is CLI/gateway-first |
| Plan mode via tool-stripping | ➖ already stronger | prepare→approve spine + autonomy L0/L1 |
| Prompt caching / window-scaled context / untrusted wrapping / MCP / scheduled tasks / vector memory / STT-TTS / 2FA | ➖ already present | some stronger (capability separation strips actuators, unlike their wrapping alone) |
| ntfy push | ⏳ optional | Telegram/Discord/Slack/Signal already cover push |

## 7. New Olympus configuration from this work

Env vars introduced while adopting the above (all optional, safe defaults):

| Var | Default | Effect |
|---|---|---|
| `OLYMPUS_TOOL_SELECT` | `1` | per-turn dynamic tool selection on/off |
| `OLYMPUS_TOOL_SELECT_MAX` | `16` | cap on non-essential tools kept (set 6-8 for small-context local models) |
| `OLYMPUS_TEACHER_ESCALATION` | `1` | escalate failed reworks to the strongest pool member |
| `OLYMPUS_RESEARCH_ROUNDS` | `4` | Deep Research search/read rounds |
| `OLYMPUS_EMAIL_STYLE` | `1` | style-matched email drafting on/off |
| `OLYMPUS_EMAIL_STYLE_TTL_DAYS` | `30` | when a cached style profile is considered stale |
| `OLYMPUS_SEARCH_PROVIDERS` | (auto) | explicit provider order, e.g. `tavily,ddg` |
| `OLYMPUS_SEARXNG_URL` | — | self-hosted SearXNG endpoint (sovereignty-friendly) |
| `BRAVE_SEARCH_API_KEY` / `TAVILY_API_KEY` / `SERPER_API_KEY` | — | keyed search providers |
| `GOOGLE_PSE_KEY` + `GOOGLE_PSE_CX` | — | Google Programmable Search |

`olympus doctor` reports which search providers are configured.
