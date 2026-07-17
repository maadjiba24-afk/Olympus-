# Olympus Vision — learning from Hermes, building something new

A living log. We install Hermes stage by stage, and for each thing it does we
decide: **adopt · adapt-and-improve · differentiate · skip** — always asking
"what's the *Olympus-native* version that's a step beyond?"

**Positioning thesis.** Hermes is *heavy & breadth-first*: it bundles a browser
(Playwright Chromium), ffmpeg, 72 curated skills, needs a desktop browser for
login, ~240 MB+ install. Olympus is *lightweight, headless-first, self-improving,
and verified*. Where Hermes maximizes tools/integrations, Olympus maximizes
**trust (verification gate) + measured self-improvement + per-user adaptation**.
We borrow Hermes's best UX, but keep our soul.

---

## Screen 1 — one-line install + setup wizard

### What Hermes did
- Installed a managed toolchain: its own `uv`, Python 3.11, checked Git/Node,
  and installed **ripgrep + ffmpeg** (with an explicit note: *"sudo is needed
  ONLY for optional system packages; Hermes itself does not require or retain
  root."*).
- Downloaded a **full media/browser stack**: Playwright Chromium, ffmpeg, fonts,
  Edge TTS, xvfb (~240 MB + a browser).
- Synced **72 bundled skills** into `~/.hermes/skills/` (google-workspace,
  github-*, arxiv, comfyui, apple-*, obsidian, vllm, huggingface, computer-use…).
- Ran a **setup wizard**: Nous Portal login → Terminal backend → agent defaults
  → Messaging platforms (space-toggle) → **Tool Availability Summary**.
- **Nous Portal subscription login** via OAuth device code — **FAILED on this
  headless box** (`xdg-open: no browser` → timed out).
- Ended with a **Tool Availability Summary**: `5/9 categories available`, each
  ✓/✗ with the exact missing env var, and a `hermes doctor` command.
- Config lives in `~/.hermes/`: `config.yaml`, `.env`, **`SOUL.md`** (editable
  personality), `cron/`, `sessions/`, `logs/`, `skills/`.
- Clean sub-command menu: `hermes setup [model|terminal|gateway|tools]`,
  `hermes config [edit|set]`, `hermes doctor`, `hermes update`.

### Verdicts + Olympus-native ideas
| Hermes feature | Verdict | Olympus move |
|---|---|---|
| **Tool Availability Summary** (`X/Y available`, ✓/✗ + which key enables each) | **ADOPT** — best UX here | Add to end of `olympus setup` AND a new `olympus doctor`. Show provider reachability, which optional tools (image/TTS/browse/gmail/calendar/codegraph/gateways) are on, and the exact env var to enable each. |
| **`hermes doctor`** diagnostics | **ADOPT** | `olympus doctor`: provider+key reachable? model valid (ping /models)? memory dir writable? crypto backend? budget set? each with a fix hint. |
| Sub-setup commands (`setup model/gateway/tools`) | **ADAPT** | Split our guided wizard into `olympus setup [model|gateway|backend|tools]` for targeted re-runs. |
| **`SOUL.md`** editable personality | **ADAPT** | A friendly `~/.olympus/soul.md` the user edits, injected into Zeus — warmer than editing prompts. |
| Transparent privilege note ("never needs root") | **ADOPT** (cheap trust win) | Mirror the wording in `install.sh`. |
| 72 **static, curated** skills bundled | **DIFFERENTIATE** | Olympus grows + measures its own skills and imports the agentskills.io standard. Optional: ship a *small* starter pack so a fresh install isn't empty — hybrid, not 72 hand-authored. |
| Heavy **browser/ffmpeg/Chromium** bundle (~240 MB) | **SKIP / DIFFERENTIATE** | Stay lightweight. Media/browse via API tools (no local Chromium). Selling point: "no 240 MB browser download." |
| **Browser-required OAuth** login | **DIFFERENTIATE (our advantage)** | It *failed on a headless server*. Olympus is headless-first: key/`config.env` or the `claude-code` CLI subscription — no desktop browser needed. Lean into this for cloud deploys. |
| Agent defaults shown (max-iterations, compression, session-reset) | **ADAPT** | Surface Olympus's knobs (MAX_AGENT_ITERATIONS, history budget, fast mode) in the wizard/doctor with recommended defaults. |

### Build backlog (from Screen 1), highest value first
1. **`olympus doctor`** + a **capability/readiness summary** (reused at the end of `olympus setup`). ← the standout.
2. `olympus setup <section>` sub-commands.
3. `~/.olympus/soul.md` editable personality, injected into Zeus.
4. (Consider) a tiny curated starter-skill pack, clearly marked provisional/importable.
5. Headless-first messaging in README/install as a deliberate contrast to Hermes.

---

## Screens 2–4 — setup depth choice, provider picker, key entry

### What Hermes did
- **Screen 2:** first question is *depth*, not content: `Quick setup — provider,
  model & messaging (recommended)` vs `Full setup — configure everything`.
  One keystroke commits the user to a short or long path.
- **Screen 3:** provider picker is **auth-mode-first**: subscription (Nous
  Portal), pay-per-use (OpenRouter), API key or CLI login (Anthropic — "API key
  or Claude Code" as ONE entry), OAuth reuse of other tools' logins (OpenAI
  Codex, Qwen CLI, GitHub Copilot via GITHUB_TOKEN/gh). Long tail folded behind
  `More providers...`, plus explicit `Cancel`.
- **Screen 4 (key entry):**
  - **Configuration Location** block up front: config file, secrets file, data
    folder, install dir — *before* asking for a secret. Transparency first.
  - A **Warning** when no provider is configured that names the exact fixes
    (`hermes model`, or set OPENROUTER_API_KEY/OPENAI_API_KEY in ~/.hermes/.env)
    and says it will "fall back to auto provider detection".
  - Shows **Current model / Active provider: none** as visible state.
  - Key prompt includes **"Get one at: https://openrouter.ai/keys"** and
    **"(or Enter to cancel)"** — never a dead end.
  - Note: key input is **visible** (not hidden) — Olympus's getpass hiding is
    actually better here.

### Verdicts + Olympus-native ideas
| Hermes feature | Verdict | Olympus move |
|---|---|---|
| Quick vs Full setup fork | **ADOPT** | First wizard question: `1) Quick — one provider and go` vs `2) Full — compose a pool, gateway, backend, fast mode`. Quick = pick provider → key → auto-model → done. |
| Auth-mode-first provider list ("API key **or** Claude Code" as one entry) | **ADAPT** | Merge our two Claude rows into one: `Anthropic (Claude) — API key or Claude Code subscription`, then ask which auth inside. Keeps the list short and puts *how you pay* first. |
| `More providers...` folding | **ADOPT** | Show top ~6 + `More providers...` → full catalog. Shorter first impression. |
| "Get a key at <url>" on every key prompt | **ADOPT (tiny, high value)** | Add `key_url` to each catalog Provider; print it at the key prompt. |
| "(or Enter to cancel)" escape on every prompt | **ADOPT** | Ensure every wizard prompt is skippable without Ctrl+C. |
| Configuration Location block before secrets | **ADOPT** | Print config.env path + memory dir at wizard start ("saved owner-only; env vars override"). |
| Visible state: "Current model / Active provider: none" + warning naming exact fixes | **ADOPT** | Wizard re-runs show current pool first; `doctor` warns with the exact env-var/command fix (matches Screen-1 backlog). |
| Auto provider detection fallback (uses any recognizable key in env) | **ADAPT** | On startup with no config, scan env for known keys (ANTHROPIC/OPENAI/DEEPSEEK/GROQ/OPENROUTER...) and offer: "Found DEEPSEEK_API_KEY — use it? [Y/n]". |
| Key typed in the clear | **SKIP (we're better)** | Keep getpass-hidden input. |

### Build backlog additions (batched)
6. Quick/Full fork at the top of the wizard.
7. `key_url` per provider + "Get one at ..." on key prompts; every prompt cancelable.
8. Config-location block at wizard start; show current pool on re-run.
9. Env-scan auto-detection fallback ("Found X_API_KEY — use it?").
10. Merge Anthropic entries into one auth-mode-first row.

---

## Screens 5–9 — model picker with pricing, credential rotation

### What Hermes did
- **Model picker shows live pricing columns**: `In / Out / Cache $/Mtok` per
  model, and a green `← currently in use` marker on the active one. The user
  picked `xiaomi/mimo-v2-pro` *because they could see it costs $1/$3 vs Opus's
  $5/$25* — pricing at the decision point changes the decision.
- Immediately confirms state: `Default model set to: xiaomi/mimo-v2-pro (via
  OpenRouter)`.
- **Same-Provider Fallback & Rotation**: keep *multiple credentials for one
  provider* and rotate when one is exhausted or rate-limited. Explains WHY in
  two lines ("preserves your primary provider while reducing interruptions
  from quota issues").
- Counts and attributes pooled credentials: `1 (0 manual, 1 auto-detected from
  env/shared auth)` — transparent about where creds came from.
- Extra credentials get **labels** (`api-key-2` default) for later management.

### Verdicts + Olympus-native ideas
| Hermes feature | Verdict | Olympus move |
|---|---|---|
| **Pricing columns in the model picker** | **ADOPT, then go beyond** | Wizard: where the provider exposes pricing (OpenRouter's /models does), show In/Out $/Mtok next to each model. **The Olympus step beyond: feed live pricing into the ModelPool** — role assignment (and fast-mode light-stage routing) becomes *cost-aware*, not just name-heuristic; and `usage.py` cost estimates become accurate per real provider prices instead of a static table. Hermes shows the human the price; Olympus *acts* on it. |
| `← currently in use` marker | **ADOPT** | Mark current pool members in the wizard's model list on re-run. |
| Immediate state confirmation ("Default model set to: X (via Y)") | **ADOPT** | Print the same one-liner after every wizard choice. |
| **Same-provider credential rotation** (N keys per provider, rotate on 429/exhaustion) | **ADAPT — complements our pool** | Olympus has a multi-*provider* pool (best-model-per-role) but no multi-*credential* pool per provider. Add optional extra keys per member; on 429/quota errors `openai_compat._post` rotates to the next credential before backoff. Together: Hermes-style resilience *and* our role-based quality routing — neither alone. |
| Credential provenance count ("0 manual, 1 auto-detected") | **ADOPT** | When env-scan (backlog #9) contributes a key, say so explicitly in `models`/`doctor` output. |
| Credential labels | **ADAPT** | Label = provider+suffix automatically (`kimi-2`); only prompt if the user wants custom. |

### Build backlog additions (batched)
11. **Pricing-aware wizard + pool** (show $/Mtok in picker; use live pricing in
    ModelPool role assignment + usage cost estimates). ← flagship candidate.
12. **Per-provider credential rotation** on 429/exhaustion in openai_compat,
    with labeled extra keys and provenance shown in `olympus models`.
13. State-confirmation one-liners after each wizard step; `← in use` markers.

---

## Screens 10–14 — TTS/backend pickers, agent knobs, tool-progress modes

### What Hermes did
- **Every option labeled with its trade-off in a few words**: "Edge TTS (free,
  cloud-based, no setup needed)", "ElevenLabs (premium quality, needs API
  key)", "NeuTTS (local on-device, free, ~300MB model download)". Cost +
  setup burden visible before choosing; the free zero-setup one is default.
- **`Keep current (X)` pre-selected in every picker** — the wizard is safely
  re-runnable; mashing Enter changes nothing (idempotent).
- Rotation strategy is *named and saved*: `✓ Saved openrouter rotation
  strategy: fill_first` (implies alternatives, e.g. round-robin).
- **Agent knobs explained inline with trade-off + recommended default**:
  "Maximum tool-calling iterations… Higher = more complex tasks, but costs
  more tokens. Default is 90, works for most tasks. Use 150+ for open
  exploration." One line: what it is, what it costs, when to change it.
- **Tool Progress Display modes**: `off` (final answer only) / `new` (tool
  name only on change) / `all` (every call + short preview) / `verbose`
  (full args, results, debug) — applies to CLI *and* messaging.

### Verdicts + Olympus-native ideas
| Hermes feature | Verdict | Olympus move |
|---|---|---|
| Trade-off-labeled options | **ADOPT** | Catalog + pickers gain "(free)", "(needs API key)", "(no setup)", "(~size download)" labels. |
| `Keep current (X)` defaults | **ADOPT** | Wizard re-runs pre-select current values (pairs with backlog #8). |
| Inline knob explanations | **ADOPT** | Full setup exposes MAX_AGENT_ITERATIONS, HISTORY_TOKEN_BUDGET, OLYMPUS_FAST, DAILY_BUDGET — each with one-line what/trade-off/default. |
| **Tool-progress verbosity modes** | **ADAPT — big UX win** | `OLYMPUS_PROGRESS=off|stages|all|verbose` + `/progress` command, CLI+web+gateways. Today's stage lines = `stages`. **Olympus twist:** at `all`+, surface *verification* activity distinctly (claims checked, fact-cache hits) — make the trust machinery visible; that's our moat. |
| Named rotation strategies | **NOTE** | When #12 lands: `fill_first` default, `round_robin` later. |
| Free-tier defaults | **ADAPT** | No-key options (DuckDuckGo fallback, local backend) labeled "(free, no setup)" and made defaults. |

### Build backlog additions (batched)
14. **Tool-progress verbosity modes** (off/stages/all/verbose) everywhere +
    `/progress`; verification activity highlighted at all+.
15. Trade-off labels on all pickers; free/no-setup options as defaults.
16. `Keep current` pre-selection + inline knob explanations in full setup.

---

## Screens 15–19 — context compression, session reset policy, gateway breadth

### What Hermes did
- **Context Compression threshold as a fraction (0.5–0.95)**: "Automatically
  summarizes old messages when context gets too long. Higher threshold =
  compress later (use more context). Lower = compress sooner." User set 0.75.
  Note it's *relative to the model's context window*, not an absolute token
  count.
- **Session Reset Policy** (for messaging sessions): explained with the COST
  framing — "Each message adds to the conversation history, which means
  growing API costs." Options: `Inactivity + daily reset (recommended — reset
  whichever comes first)` / inactivity-only / daily-only / never / keep
  current. Defaults: inactivity 1440 min, daily reset hour 4 (off-peak).
- **The killer detail**: "When a reset happens, the agent **saves important
  things to its persistent memory first** — but the conversation context is
  cleared." Reset ≠ forget. Plus a manual `/reset` chat command.
- **Messaging platform multi-select: 16 platforms** — Telegram, Discord,
  Slack, Signal, Email, SMS (Twilio), Matrix, Mattermost, WhatsApp, DingTalk,
  Feishu/Lark, WeCom (+Callback), Weixin/WeChat, BlueBubbles (iMessage),
  **Webhooks (GitHub, GitLab, etc.)**.

### Verdicts + Olympus-native ideas
| Hermes feature | Verdict | Olympus move |
|---|---|---|
| Compression threshold as fraction of context window | **ADAPT** | Olympus compacts at an absolute HISTORY_TOKEN_BUDGET (3000). Better: budget = fraction × model context (per pool member), so a 200k-context model isn't compacted like an 8k one. Keep absolute override. |
| **Memory-preserving reset** ("saves important things first") | **ADOPT — fits us perfectly** | `/reset` command: run recall extraction + fold history into the conversation-state block + (checkpoint) companion evolve, THEN clear. Olympus twist: reset feeds the *learning loops* — a reset literally makes Olympus smarter about you before it forgets the transcript. |
| Scheduled session resets (inactivity + daily hour) for gateways | **ADOPT** | Gateway sessions: OLYMPUS_SESSION_IDLE_RESET / OLYMPUS_SESSION_DAILY_RESET_HOUR; reset = distill-then-clear. Big cost saver on long-lived Telegram chats. |
| Cost-framed explanations ("growing API costs") | **ADOPT** | Reuse the framing in wizard/docs for history budget + resets. |
| 16 messaging platforms | **DIFFERENTIATE (mostly)** | Breadth is Hermes's game. We keep 5 solid gateways + web. Worth stealing selectively: **Email as a gateway** (reuse gmail adapter: poll inbox → pipeline → reply) and **inbound Webhooks gateway** (GitHub/GitLab events → council; pairs with Prometheus's auto-filed issues). Matrix/DingTalk/WeCom etc.: skip. |
| `/reset` manual command | **ADOPT** | Add `/reset` to TUI + gateways. |

### Build backlog additions (batched)
17. **Distill-then-clear `/reset`** + scheduled gateway session resets
    (inactivity + daily hour) — reset feeds memory/companion first.
18. Context budget as fraction-of-model-context (absolute override kept).
19. (Later) Email gateway via existing gmail adapter; inbound webhooks gateway.

---

## Screens 20–24 — tool configuration (the full tool taxonomy)

### What Hermes did — its 18 tool categories (multi-select, per-platform)
`web_search/web_extract [no API key]` · Browser Automation
(navigate/click/type/scroll) · Terminal & Processes · File Ops
(read/write/patch/search) · Code Execution · **Vision/Image Analysis** ·
Image Generation · **Mixture of Agents** (off by default) · Text-to-Speech ·
Skills · **Task Planning (todo)** · Memory · Session Search · **Clarifying
Questions (clarify)** · **Task Delegation (delegate_task)** · Cron Jobs
(create/list/update/pause/resume/run + attached skills) · RL Training
(Tinker/Atropos) · Home Assistant.
- Tools that need keys are **enabled here but configured lazily** ("configured
  when enabled") — it collected 5 that needed setup and let you skip any.
- **Browser provider sub-picker**: Local (free headless Chromium) default, or
  cloud (Browserbase/BrowserUse/Firecrawl w/ stealth+proxies), or Camofox
  (anti-detection), or Skip.

### Where Olympus already matches / exceeds
| Hermes tool | Olympus |
|---|---|
| web_search/extract | ✓ web_search/web_fetch (server-side on Anthropic, DDG fallback) |
| Terminal / File Ops / Code Exec | ✓ sandbox (run_command/write_file/read_file/list_dir) |
| Vision/Image Analysis | ✗ **gap** — we generate images but don't *analyze* them |
| Image Generation / TTS / Browser | ✓ media.py (generate_image/text_to_speech/browse_page) |
| Mixture of Agents | ✓✓ our whole council IS this (and verified + benchmark-gated) |
| Task Planning (todo) | ~ partial (playbooks) — no live per-task todo |
| Memory / Session Search | ✓ memory + FTS5 search |
| **Clarifying Questions (clarify)** | ✗ **gap** — Zeus never asks the user to disambiguate |
| Task Delegation | ✓✓ DAG planner + spawn_subagent |
| Cron (+attached skills) | ✓ scheduler (NL) — but no "attach a skill to a job" |
| RL Training export | ~ we export trajectories (trajectories.py) |
| Home Assistant | ✗ skip (breadth) |

### Verdicts + Olympus-native ideas
| Hermes idea | Verdict | Olympus move |
|---|---|---|
| **Clarifying-questions tool** | **ADOPT — real gap** | Give Zeus a `clarify` path: when a request is ambiguous/underspecified, ask 1–2 crisp questions BEFORE delegating, instead of guessing. Cheap, huge quality lift. Olympus twist: only trigger when the route confidence is low, so it doesn't nag. |
| **Vision / image analysis** | **ADOPT — real gap** | `analyze_image` tool (describe/answer-about an image) via a vision-capable pool model; wrap output as untrusted (it's external content). Rounds out media.py. |
| Live task-planning (todo) surfaced to user | **ADAPT** | Surface Athena's DAG as a visible checklist that ticks off as steps complete (ties to progress modes, backlog #14). We already plan the graph — just show it. |
| Lazy key config ("enabled now, configured when used") | **ADOPT** | Don't block setup on optional keys; enable the tool, prompt for the key on first use (or via `setup tools`). |
| Cron jobs with **attached skills** | **ADAPT** | Let a scheduled task name a skill to load first ("every Monday, using the `weekly-report` skill, …"). |
| Browser provider sub-picker; heavy cloud browsers | **SKIP/DIFFERENTIATE** | Keep browse_page (urllib, no Chromium). Note the free/no-key default is right; we already are that. |
| Per-platform tool enable/disable | **NOTE** | We have capability-separation already (action tools stripped when ingesting untrusted). Could expose per-gateway tool allowlists later. |

### Build backlog additions (batched)
20. **`clarify` capability** — Zeus asks 1–2 questions on low-confidence/ambiguous
    requests before delegating (gated on route confidence so it won't nag).
21. **`analyze_image` (vision) tool** via a vision-capable pool model; output
    wrapped as untrusted. Fills the one real tool gap vs Hermes.
22. Surface Athena's DAG as a live ticking checklist (with progress modes).
23. Lazy optional-key config; cron jobs can attach a skill.

---

## Screens 25–29 — Per-tool provider sub-pickers (Tool Configuration)

### What Hermes did
- Entered **Tool Configuration** for the 5 tools that need keys ("Configuring
  5 tool(s)"). Browser Automation was **skipped** (no key chosen earlier).
- **Image Generation → FAL.ai**: pitched "FLUX 2 Pro with auto-upscaling",
  showed **`Get yours at: https://fal.ai/dashboard/keys`**, then a bare
  `FAL API key:` prompt. The FAL dashboard flow: pick **Scope = API** ("For
  running models… Recommended for most use cases"), Description "Hermes agent",
  and a **safety note — "Keep it safe — your FAL_KEY will be shown here once
  created… it cannot be recovered."**
- **TTS provider sub-picker**: Microsoft **Edge TTS (Free, no key)** as the
  active/default, then OpenAI TTS (Premium), ElevenLabs (Premium — most
  natural), Mistral Voxtral (multilingual, native, needs `MISTRAL_API_KEY`),
  and **Skip**.
- **Web Search provider sub-picker**: Firecrawl Cloud, Exa, Parallel, Tavily,
  **Firecrawl Self-Hosted (Free — run your own)**, and **Skip**.

### Read
This is the same three patterns we already captured, now confirmed as Hermes's
*consistent* shape for every key-needing tool: (1) a **provider sub-picker**
with **free/no-key option pre-selected**, (2) a **"Get yours at <url>"** line
before the bare key prompt, (3) **Skip** always available. New detail worth
stealing: the **"shown once — cannot be recovered, save it now"** safety note
at the moment of key creation.

### Verdicts + Olympus-native ideas
| Hermes idea | Verdict | Olympus move |
|---|---|---|
| Per-tool **provider sub-picker**, free option default | **ADOPT (already backlog #7/#15/#23)** | Confirms the pattern — no new item. When we add optional tools, each gets a sub-picker whose free/no-key choice is pre-selected and marked `← recommended (free)`. |
| **"Get yours at <url>"** before key prompt | **ADOPT (backlog #7)** | Already planned; reconfirmed as universal in Hermes. |
| **"Key shown once — save it now"** safety note | **ADOPT — new nuance** | When we prompt for/echo any key path, add a one-line "your provider shows this once; store it in `~/.olympus/config.env` (owner-only) now" note. Small trust win, costs nothing. |
| Bundled premium media providers (FAL/ElevenLabs/Mistral Voxtral) | **DIFFERENTIATE** | Don't bundle a provider zoo. Keep our generate_image/text_to_speech behind the ModelPool + openai_compat, so any OpenAI-compatible media endpoint drops in via config — no per-vendor code. Free/no-key default (like Edge TTS) is the right stance; we already lean headless/free. |
| Firecrawl **self-hosted** free web-search option | **NOTE** | Nice that they surface a self-host escape hatch. Our web_search already defaults to server-side (Anthropic) with a DDG fallback — free, no key. Parity without the picker. |

### Build backlog additions (batched)
24. **"Key shown once — save it" safety note** on any key/credential prompt
    (point at owner-only `~/.olympus/config.env`). Reinforces #7; no new
    tool code — just wizard copy. (Screens 25–29 otherwise reinforce #7, #15,
    #23 with no brand-new items.)

---

## Screens 30–34 — Setup exit + first launch (tools/skills dashboard, status line, security scanner)

### What Hermes did
- **"To edit your configuration"** help block on setup exit: `hermes setup`
  (re-run wizard), `hermes setup model|terminal|gateway|tools` (edit one
  section), `hermes config` (view), `hermes config edit` (open in $EDITOR),
  `hermes config set <key> <value>`, plus the raw file paths
  (`~/.hermes/config.yaml`, `~/.hermes/.env`). Then **"Ready to go!"** →
  `hermes` / `hermes gateway` / `hermes doctor`, and `Launch hermes chat now?
  [Y/n]`.
- **First launch**: big `HERMES-AGENT` banner + `v0.9.0 (2026.4.13) · upstream
  5621fc44`. A boxed **"Available Tools"** grouped by category with real tool
  names: `browser: browser_back, browser_click…`, `clarify: clarify`,
  `code_execution: execute_code`, `cronjob: cronjob`, `delegation:
  delegate_task`, `file: patch, read_file, search_files, write_file`,
  `homeassistant: …`, `image_gen: image_generate`, "(and 10 more toolsets…)".
- **"Available Skills"** — the full 72-skill taxonomy by category
  (autonomous-ai-agents, creative, data-science, devops, email, gaming,
  general, github, leisure, mcp, media, mlops, note-taking, productivity,
  red-teaming, research, smart-home, social-media, software-development).
- Model line: **`mimo-v2-pro · Nous Research`**, session path + session id.
- **Welcome** message + **"/help for commands"**; tip that each MCP server gets
  its own toggleable toolset (`mcp-servername`).
- A **status line**: `mimo-v2-pro | ctx -- | [redacted] -- | 19s`
  (model | context usage | cost/tokens | elapsed).
- A safety warning: **"tirith security scanner enabled but not available —
  command scanning will use pattern matching only."** → Hermes has a
  pre-execution **command security scanner** that inspects shell commands
  before running them, with a pattern-matching fallback.

### Read
Two confirmations and three genuinely new ideas. Confirmed: **`clarify` is a
real shipped tool** (validates backlog #20), and the tool list shows
`image_generate` but **no image *analysis*** (vision gap #21 still stands).
Config sub-commands reinforce backlog #1 (doctor) / #2 (setup sections) and add
`config set`/`config edit`. New: the **launch capability dashboard**, the
**status line**, and — most aligned with our trust thesis — a **pre-exec
command security scanner**.

### Verdicts + Olympus-native ideas
| Hermes idea | Verdict | Olympus move |
|---|---|---|
| **Pre-exec command security scanner** (tirith) | **ADOPT — dead-on our thesis** | Add a **verification gate on shell/sandbox commands**: before `run_command` executes, Aletheia-style check classifies it (safe / needs-confirm / deny) against a policy, with a pattern-match fallback when no model budget. This is the shell analogue of our hallucination gate — trust is our whole pitch. Fail *closed* (deny on scanner-unavailable is stricter than Hermes's "fall back to patterns"). |
| **Launch capability dashboard** (Tools + Skills by category) | **ADAPT** | On `olympus` launch, print a compact **"what I can do now"**: active council roles + pool models, enabled tools, loaded skills — grouped, collapsed to counts with `--verbose` to expand. Ties to `doctor`/readiness (#1). Answers "is it actually set up?" at a glance. |
| **Status line** (model \| ctx \| cost \| elapsed) | **ADOPT** | Persistent one-line footer: **active pool model(s) \| context budget used (fraction, #18) \| est. cost so far (pricing-aware, #11) \| elapsed**. Cheap, always-on situational awareness; composes with progress modes (#14). |
| `config set <key> <value>` / `config edit` ($EDITOR) | **ADOPT (reinforces #2)** | Give `olympus config set/edit/show` so users don't hand-edit `config.env` blind — but keep secrets going only to owner-only file, never echoed. |
| Per-MCP-server toggleable toolset | **NOTE / later** | When we add MCP, mirror "each server = one named, independently-toggleable toolset" with capability-separation applied per server. |
| Version + **upstream commit** in banner | **ADOPT — tiny** | Show `olympus X.Y.Z · <git short-sha>` on launch/`doctor` for reproducible bug reports (we sign releases already; surface the provenance). |

### Build backlog additions (batched)
25. **Command security gate** — pre-execution classifier on `run_command`
    (safe/confirm/deny via policy + Aletheia check, pattern-match fallback,
    **fail-closed**). The shell analogue of our hallucination controller;
    strongest trust-thesis win in this batch.
26. **Launch capability dashboard** — compact "what I can do now" (roles +
    pool + tools + skills, grouped/counted, `--verbose` expands); ties to
    `doctor` (#1).
27. **Persistent status line** — model/pool | context-fraction (#18) | est.
    cost (#11) | elapsed; composes with progress modes (#14).
    (Also: `olympus config set/edit/show` reinforces #2; version+short-sha in
    banner for provenance. `clarify` confirmed real → keep #20; vision still a
    gap → keep #21.)

---

## Build log — shipped in 0.24.0

The full 27-item backlog above was built, tested, and released as **0.24.0**
(originally cut as 0.22.0, re-versioned after merging main's parallel
operator/admin-panel workstream, which had already claimed the 0.22/0.23
headers; 1115 tests passing on the merged tree; wheel builds clean). Sequenced into six tiers:

- **Tier A — trust core** (`2676111`): command security gate (#25), clarify
  (#20), analyze_image (#21), per-provider credential rotation (#12).
- **Tier B — pool & cost** (`cc030c9`): pricing-aware routing (#11),
  context-fraction budget (#18), distill-then-clear `/reset` + idle gateway
  resets (#17).
- **Tier C — CLI/readiness** (`17101e4`): `olympus doctor` (#1), `config
  show/set/edit` + `setup <section>` (#2), capability dashboard (#26), status
  line (#27), progress modes (#14), live DAG checklist (#22).
- **Tier D — wizard UX** (`a8f3484`): Quick/Full fork (#6), key URLs +
  save-now + cancelable (#7, #24), config-location + current pool (#8), env
  auto-detect (#9), merged Anthropic auth-first (#10), state confirmations +
  `← in use` (#13), trade-off labels + free defaults (#15), keep-current + knob
  explanations (#16).
- **Tier E — personality & content** (`7e07bb1`): `soul.md` (#3), cron-attached
  skills (#23), headless-first README (#5), starter skill pack (#4).
- **Tier F — reach & ship** (`4e83c1b`): email gateway + inbound webhook
  gateway (#19), version bump, CHANGELOG, manifest.

The positioning thesis held: every item was **adopt-and-surpass**, not clone —
the security gate fails *closed* where Hermes falls back to patterns; pricing is
*acted on* (routing) not just shown; the wizard defaults to *free/no-setup*; and
the whole thing stays headless, dependency-light, and verified.

---

# Round 2 — watching resumes (baseline: 0.24.0 + main's operator wave)

Everything below is judged against the *merged* tree: the 27-item build above
PLUS main's parallel adoptions (/learn, /journey, /moa, /steer, /undo, goals,
admin panel, browser harness, voice notes). Round-2 rule: only *genuinely new*
capability makes the backlog.

## Screens 35–39 — Top-10 CLI, Top-10 slash, file layout, delta-setup

### What Hermes did
- **"Top 10 CLI — muscle memory"**: `hermes` (TUI), `hermes -c` (continue last
  session), `status`, `model`, `insights` (tokens/cost/activity),
  **`sessions browse` (curses picker to resume)**, `skills browse`
  (discover+install), `config show`, `doctor`, `update`.
- **"Top 10 slash"**: `/new`, **`/model <name>` (swap model mid-session)**,
  **`/fast` (priority routing toggle)**, **`/bg <prompt>` (background task,
  keep chatting)**, **`/btw <question>` (ephemeral side question)**,
  **`/queue <prompt>` (next-turn queue)**, `/compress` (manual compaction),
  `/skills`, **`/yolo` (toggle dangerous-command approvals)**, `/help`.
- **"Where stuff lives"**: everything under `~/.hermes/` — `config.yaml` +
  `.env` + `memories/` (MEMORY.md, USER.md) marked *editable*; `sessions/`,
  `skills/`, `logs/`, `cron/` marked hands-off.
- **`hermes setup` re-run menu**: **"Quick Setup — configure missing items
  only"** (a delta-setup), Full (reconfigure everything), then per-section
  entries — all in one radio menu, ESC cancels.

### Where Olympus already matches (round-2 strictness)
`status`+`doctor`+`insights` (status/usage/dashboard/doctor), `model`
(setup model), `config show`, `update` (upgrade), `/new`-ish (`/reset`, and
ours distills first), `/compress` trigger condition (auto, model-fraction),
`/skills` (skills CLI + read_skill), `/help`, skills discover/install
(skill-import, agentskills.io, starter pack). `/steer` and `/undo` (from
main) cover mid-run nudging and history repair Hermes doesn't list.

### Verdicts + Olympus-native ideas
| Hermes idea | Verdict | Olympus move |
|---|---|---|
| **`sessions browse` + `-c` continue** | **ADOPT — real gap** | Our CLI is single-session (`cli-default`). Add named sessions: `olympus sessions` (list/resume picker over saved conversations, newest first, first-line preview), `olympus -c` (continue last), `/new [name]` in chat. We already persist every conversation by id — this is surfacing, not new storage. Olympus twist: the picker shows each session's distilled state line (from /reset-style distillation), not just its first message. |
| **`/bg <prompt>` background task** | **ADOPT — real gap** | Run a one-shot task through the full pipeline in a background thread while chat stays live; result is announced in-chat when done (and lands in reports). We already have every piece (threads, scheduler one-shots, reports) — compose them. Verified as always: background answers still pass Aletheia. |
| **`/btw <question>` ephemeral side question** | **ADOPT — cheap + clever** | Answer OUTSIDE the session: no history append, no memory extraction, no companion count. One flag through ask() — prevents "what's the capital of X" from polluting a work session's distilled state. |
| **`/model <name>` mid-session swap + `/fast` toggle** | **ADOPT** | In-chat `/model` re-points the pool primary (validating the name against the provider's model list) and `/fast on\|off` flips fast mode for the session. State-confirmation line shows the new assignment. No restart, no wizard. |
| **`/queue <prompt>` next-turn queue** | **SKIP (covered)** | `/steer` already reaches the *running* task; queuing the next turn is what a terminal input buffer does anyway. No real gap. |
| **`/yolo`** | **DIFFERENTIATE — validates our gate** | One keystroke to disable dangerous-command approvals is the anti-pattern our cmdguard exists to prevent. Olympus's equivalent is *governed*: per-scope autonomy grants on the action spine + OLYMPUS_EXEC_SECURITY modes, and DENY-level commands stay blocked **even with approvals off**. Document the contrast; build nothing. |
| **"Where stuff lives" transparency** | **ADAPT — small** | Add a `paths` block to `olympus doctor`/`config show`: config.env, soul.md, memory dir, workspace, sessions — with the same editable/hands-off labeling. One print block; big orientation win for new users. |
| **Delta-setup ("configure missing items only")** | **ADOPT — better Quick** | Their re-run Quick only prompts for what's *missing*. Ours re-asks. Wire doctor's readiness gaps into setup: `olympus setup` on a configured install offers "Fix what's missing (N items)" driven by the ✗/⚠ list — doctor finds it, setup fixes it, doctor confirms it. Closes the loop our two commands already imply. |
| `insights` (tokens/cost/activity) | **SKIP (covered)** | status + usage report + dashboard + admin panel already cover it. |

### Build backlog additions (Round 2, batched — NOT building yet)
28. **Named sessions + browse/resume** — `olympus sessions` picker, `olympus -c`,
    `/new [name]`; picker lines show each session's distilled-state summary.
29. **`/bg`** — one-shot background task through the full verified pipeline;
    announces completion in-chat; result saved to reports.
30. **`/btw`** — ephemeral side question: no history, no memory writes.
31. **In-chat `/model <name>` + `/fast on|off`** — live pool re-point with
    validation + state-confirmation line.
32. **Delta-setup** — `olympus setup` offers "fix what's missing" from doctor's
    gap list on a configured install.
33. **`paths` transparency block** in doctor/config show (editable vs
    hands-off labeling). (Also: document the /yolo contrast in THREAT_MODEL —
    DENY survives autonomy grants by design.)

---

## Screens 40–44 — setup re-run detail (provider zoo, reauth, model search, backends, platform multi-select)

Re-visits of the setup surface, now in re-run form. Round-2 check against what
we've built — pattern by pattern:

| Hermes screen | Olympus status |
|---|---|
| 32-provider picker | ✓ pattern done (catalog + trade-off labels + custom endpoint); breadth is a **deliberate skip** — our `custom` OpenAI-compatible entry covers the tail without per-vendor code |
| "Use existing credentials / Reauthenticate / Cancel" | ✓ equivalent done for keys: env-scan ("Found X_API_KEY — use it?"), keep-current pool line, delta-setup; OAuth reauth N/A (subscription auth reuses the `claude` CLI login) |
| Model picker with **/ search** | ✓ auto-discovery + numbered pick done; **△ small gap: no type-to-filter** — matters only for OpenRouter-scale lists (300+) |
| Terminal backends (local/docker/modal/ssh/daytona/singularity) + Keep current | ✓ local/docker + keep-current done; the rest are thin transports over the same run() contract — **deliberate skip** until someone needs one |
| Platform **multi-select checklist with "(not configured)" status** | ✓ single-pick configure done (telegram/discord/slack/signal/email/webhook); **△ small gap: multi-select + configured-status labels** in one screen; 26-platform breadth = deliberate skip |

### Remaining micro-items — BUILT
34. ✅ Model picker type-to-filter when the discovered list is large (>20).
35. ✅ Gateway step: one checklist showing all six channels with
    configured/not-configured status; several in one pass.

---

## Screens 45–49 — Memory module (the four layers, MEMORY.md/USER.md, real file)

### What Hermes did
- **Four stacking layers**: L1 built-in markdown (`MEMORY.md` + `USER.md`,
  plain files, always load), L2 **FTS5 session search**, L3 external provider
  plugin (Honcho/Mem0 — "semantic + identity modeling"), L4 **Obsidian vault
  as a skill** (filesystem KB the agent writes, the user curates in a GUI).
  "These aren't alternatives. They stack."
- **Two files with explicit caps**: MEMORY.md (projects/environment/decisions/
  lessons, cap 2,200 chars ≈ **800 tokens**, `memory_char_limit`); USER.md
  (role/preferences/communication style, cap ≈ 500 tokens). Tagline: "Plain
  text. Editable. Auditable. Portable."
- A real MEMORY.md: §-separated prose notes (paper-summary priorities, current
  project, environment pins, workflow prefs, eval metrics) — readable, but
  no provenance, no confidence, no gating: whatever got written is truth.

### Where Olympus stands — layer by layer (our strongest ground)
| Hermes layer | Olympus |
|---|---|
| L1 two markdown files | ✓✓ richer engine: gated durable memory (confidence + decay + approve/reject), profile card, companion model, playbooks, relgraph — and the identity half is split correctly: **soul.md** (how to behave, owner-authored) vs learned facts (gated). **△ gap: not as transparent** — no one-glance plain-text projection of "what Olympus believes about me" |
| Token caps | ✓ exact parity — MEMORY_RETRIEVAL_BUDGET_TOKENS defaults to **800**, same number Hermes chose; ours is configurable |
| L2 FTS5 session search | ✓✓ identical tech, shipped (search.py + search_sessions) |
| L3 external provider (Honcho/Mem0) | **DIFFERENTIATE** — semantic recall (embeddings fallback) + identity modeling (companion) are native and local; no memory-SaaS dependency, nothing personal leaves the machine |
| L4 Obsidian vault | **△ small real idea** — a plain-markdown KB dir the user curates in any GUI is cheap and genuinely useful |

### Verdicts + Olympus-native ideas
| Hermes idea | Verdict | Olympus move |
|---|---|---|
| "Plain text. Editable. Auditable. Portable." transparency | **ADOPT the transparency, keep the governance** | `olympus memory card` — render everything believed about you as one markdown page: profile, durable facts **with confidence + provenance**, companion summary, active playbooks. Hermes shows you a file; we show you a file *with receipts*. Edits still flow through approve/forget (auditable), never raw file writes (ungated memory = injectable memory). |
| Obsidian vault (L4) | **ADAPT — small** | `OLYMPUS_VAULT_DIR`: mirror lessons/notes/reports as dated plain-markdown files into a directory the user opens in Obsidian/any editor. Write-through mirror, not a second source of truth — the gated store stays canonical. |
| External memory provider (L3) | **SKIP** | Native semantic + companion covers it with zero SaaS surface. |
| No-provenance prose notes (their real MEMORY.md) | **counter-example** | Validates our gating: their file happily stores anything as fact. Note in VISION only. |

### Build backlog additions (batched, NOT building yet)
36. **`olympus memory card`** — one-page markdown projection of all per-user
    memory with confidence + provenance; `--user` for gateway users.
37. **Vault mirror** — `OLYMPUS_VAULT_DIR` write-through of lessons/notes/
    reports as dated markdown for GUI curation (Obsidian-compatible = just files).

---

## Screens 50–54 — Memory internals (frozen snapshot, memory tool, write hardening, seed)

### What Hermes did
- **Frozen-snapshot pattern (their labeled footgun)**: memory files render into
  the system prompt at session start with `cache_control`; mid-session
  `memory(action=add)` hits disk immediately but the prompt does NOT update —
  writes appear next session. "Performance over freshness" (preserves the
  prefix cache).
- **The memory tool**: three actions — `add`, `replace`, `remove` — **no read**
  (memory is always in context); §-separated entries; replace/remove match by
  substring (`old_text`); live % cap indicator in the header.
- **Telegram transparency**: memory ops surface inline as they happen
  (`+memory:`, `~memory:`, `-memory:` lines), plus self-maintenance narrated
  ("cleaned up one duplicate … to make room").
- **Why-it-works hardening**: cap-as-feature (add-overflow returns current
  entries + error → forces consolidation); trained save/skip policy (no
  memory-manager agent); **dedup is a no-op success** ("no duplicate added" —
  prevents retry-spam); **injection scanning before write** — prompt-injection
  patterns, credential exfiltration (SSH/API keys), invisible Unicode.
  "Closes the persistent-injection vector."
- **Seed USER.md yourself**: recommended skeleton — ## Role, ## Communication
  preferences, ## Current focus, ## Things to never do.

### Where Olympus stands
| Hermes mechanism | Olympus |
|---|---|
| Frozen snapshot (stale until next session) | **✓ no footgun — freshness-first**: memory context blocks are recomputed per turn, so a gated fact is visible on the NEXT TURN, not the next session. Different trade-off taken deliberately. |
| add/replace/remove tool, substring matching | ~ different philosophy: extraction is **automatic + gated** (no trained-policy burden on the model); agents add via save_lesson; **removal stays user-gated** (/journey rm, memory forget) — chat-driven substring erasure is itself an injection vector (a hostile page could talk the agent into deleting the memory of a prior warning) |
| Dedup as no-op success | ✓ parity — recall gating returns duplicate → reinforces the existing fact |
| Cap-as-feature / forced consolidation | ✓ equivalent by different means: confidence decay + retrieval token budget (default 800 — same number) keep working memory bounded without a hard write-cap |
| **Injection scan before write** | **△ partial gap**: sanitize_for_memory defangs injection-marker lines, but does NOT strip invisible/zero-width Unicode or scrub credential patterns (our _KEYISH/_URL_CRED regexes guard only the shared contribution pool) |
| Visible +memory/~memory lines in chat | **△ gap**: our extraction is background and silent; the user never sees what was learned until /journey |
| Seed USER.md skeleton | ✓ mostly soul.md + profile; template lacks Role / Current focus sections |

### Verdicts + Olympus-native ideas
| Hermes idea | Verdict | Olympus move |
|---|---|---|
| Injection scan before memory write | **ADOPT — close the same vector fully** | Extend sanitize_for_memory: strip zero-width/bidi Unicode, redact credential-shaped strings (reuse _KEYISH/_URL_CRED/_EMAIL patterns) before ANY lesson/skill/fact write. Their "closes the persistent-injection vector" claim should be at least as true here. |
| Visible memory activity | **ADOPT** | Progress-mode-aware "🧠 remembered: <fact> (conf 0.8)" line when the background extractor gates a fact in (all/verbose modes) — memory becomes observable in the moment, not just in /journey. |
| Seed-yourself skeleton | **ADOPT — tiny** | Add ## Role and ## Current focus sections to the soul.md scaffold; mention seeding in the wizard's closing hints. |
| Frozen snapshot | **DIFFERENTIATE (documented)** | Keep freshness-first per-turn blocks; our static prompt prefix still caches — we give up less than Hermes assumes. |
| Chat-driven replace/remove | **DIFFERENTIATE** | Removal stays user-gated. An agent that can erase its own memories from a chat message is an erasure vector, not a feature. |

### Build backlog additions (batched, NOT building yet)
38. **Memory-write hardening**: invisible-Unicode stripping + credential-pattern
    redaction in sanitize_for_memory (applies to lessons, skills, facts).
39. **Visible memory activity**: 🧠 progress lines when facts are gated in
    (progress-mode aware).
40. **Soul scaffold enrichment**: Role / Current focus sections + wizard hint.

---

## Screens 55–59 — L2 session search in depth + L3 provider plugins intro

### What Hermes did
- **state.db** (SQLite, FTS5, **WAL mode**) indexes every CLI + gateway session.
- `session_search` = full-text + **Gemini Flash summarization** of the hits
  (compress inside the tool before returning). The agent calls it
  **autonomously** when it suspects a prior conversation is relevant.
- v0.11.0: state.db **auto-prunes + VACUUMs at startup** — "no more cron-prune".
- Live demo: "What were the top 10 name suggestions for the luxury bag and
  watch app we were discussing?" → cross-session recall from Telegram.
- **L1 vs L2 framing**: curated (~1,300 tokens, instant, fixed cost) vs
  archived (unlimited, search+summarize, on-demand cost). "Use both."
- L3 intro: 8 memory-provider backends, one active at a time, built-in stays on.

### Where Olympus stands
| Hermes mechanism | Olympus |
|---|---|
| FTS5 index of every CLI+gateway session | ✓ identical — search.py indexes on every save_conversation; gateways included |
| Autonomous invocation | ✓ search_sessions is in BASE_TOOLS — every specialist carries it |
| L1/L2 stacking, curated vs archived | ✓ same architecture (gated memory + FTS archive) |
| LLM summarization of hits inside the tool | **△ small gap** — we return raw rendered turns; long hit-sets spend the caller's context |
| WAL mode + auto-prune + VACUUM | **△ real operational gap** — our index is never pruned or vacuumed; RETAIN_DAYS covers traces/usage only, so conversations + index grow unbounded on a long-lived droplet |
| 8 pluggable memory providers (L3) | **SKIP** — native semantic + companion (recorded at screens 45–49) |

### Verdicts + Olympus-native ideas
| Hermes idea | Verdict | Olympus move |
|---|---|---|
| Index maintenance (prune + VACUUM, WAL) | **ADOPT — droplet hygiene** | Fold into the existing heartbeat maintenance sweep (not startup — a long-lived server rarely restarts): prune index entries for conversations idle past a retention window, VACUUM after pruning, open the DB in WAL mode. Zero new surface; one sweep extension. |
| Distill long hit-sets inside search_sessions | **ADOPT — small** | When rendered hits exceed a budget, distill them with the pool's fastest model before returning (budget-aware, falls back to truncation keyless). Same council quality, less context spent. |
| L1-vs-L2 "use both" framing | **NOTE** | Already our architecture; steal the one-line framing for docs: "gated memory is what's curated; session search is what's archived." |

### Build backlog additions (batched, NOT building yet)
41. **Search-index maintenance** — WAL mode; heartbeat sweep prunes idle
    conversations from the index per retention window, then VACUUMs.
42. **Hit-set distillation** — search_sessions compresses oversized results via
    the pool's fastest model (graceful keyless fallback).
