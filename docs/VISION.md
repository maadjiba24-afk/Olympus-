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
