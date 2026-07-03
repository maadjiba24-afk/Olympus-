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
