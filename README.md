# ⚡ OLYMPUS

[![CI](https://github.com/maadjiba24-afk/Olympus-/actions/workflows/ci.yml/badge.svg)](https://github.com/maadjiba24-afk/Olympus-/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-d9b44a.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

A self-sufficient, self-recurring, **self-improving** multi-agent AI system
built on the Claude API (and any other frontier model you bring). A main agent
commands a supervised council of specialists, every answer passes through a
hallucination controller, and the system continuously scans the world, learns
from YouTube, and upgrades itself.

> **Status:** the full architecture is implemented and covered by 116 passing
> tests. Add an API key and run `python -m olympus ask "..."` to use it; run
> `python -m olympus scores` to see each specialist's measured quality.

## Architecture

```
                              user
                               │
                               ▼
                ┌─────────────────────────────┐
                │   ZEUS · Main Agent          │  routes or answers directly
                └──────────────┬──────────────┘
                               │ delegates
                               ▼
                ┌─────────────────────────────┐
                │   ATHENA · Supervisor        │  plans precise sub-tasks
                └──────────────┬──────────────┘
                               │ dispatches
        ┌──────────┬──────────┼──────────┬──────────────┐
        ▼          ▼          ▼          ▼              ▼
   PLUTUS     PEITHO    HEPHAESTUS    AEGIS          IRIS
   Financial  Marketing  Coding      Cybersecurity  Social Network
        ▼          ▼          ▼          ▼              ▼
   CHIRON     CHRONOS     ARGUS      MNEMOSYNE     PROMETHEUS
   Coaching   Scheduling  Opportunity YouTube       Evolution /
                          Scout 🌐    Learner 🎥    Self-Upgrade 🔧
                      METIS · Learning Synthesizer 🧠
                 (daily cycle: experience → skill library)
        └──────────┴──────────┴──────────┴──────────────┘
                               │ outputs
                               ▼
                ┌─────────────────────────────┐
                │  ALETHEIA · Hallucination    │  verifies every claim with
                │  Controller                  │  web search, flags/corrects,
                └──────────────┬──────────────┘  records lessons
                               │ verified findings
                               ▼
                  ZEUS composes the final answer
                               │
                               ▼
                              user
```

### The council

| Agent | Role |
|---|---|
| **Zeus** | Main agent — the user's single interface; routes or answers |
| **Athena** | Supervisor — plans a **dependency graph** of specialist steps (parallel where independent, serial where one step needs another's output) and gates quality |
| **Aletheia** | Hallucination controller — verifies claims, fixes/flags, learns from mistakes |
| **Plutus** | Financial specialist |
| **Peitho** | Marketing specialist |
| **Hephaestus** | Coding specialist — polyglot (Python, Go, Rust, TypeScript, Java, SQL, …), idiomatic per language, with a server-side code sandbox |
| **Aegis** | Cybersecurity specialist (strictly defensive) |
| **Iris** | Social network assistant |
| **Chiron** | Coaching specialist |
| **Chronos** | Scheduling management |
| **Argus** | Opportunity scout — surfs the internet via Anthropic's **server-side web search** (no MCP connections required) for business opportunities and world events |
| **Mnemosyne** | YouTube learner — watches videos via transcript, summarizes what it understood, stores lessons |
| **Metis** | Learning synthesizer — runs the **daily learning cycle**, distilling lessons, corrections, and user feedback into the self-built skill library |
| **Prometheus** | Evolution specialist — audits Olympus, finds what's missing inside it, upgrades agent prompts **measured by benchmark with automatic rollback**, files improvement proposals |

### Self-* properties

- **Self-sufficient** — web access is built in through Anthropic's server-side
  `web_search` / `web_fetch` tools; no MCP servers or external connectors.
- **Self-recurring** — `python -m olympus heartbeat` runs an autonomous loop:
  opportunity scans every 6 h, the YouTube watch-queue hourly, a full
  self-audit weekly.
- **Self-improving** — three feedback loops:
  1. Aletheia records every correction as a lesson in persistent memory.
  2. Specialists recall lessons before answering (`recall_memory`).
  3. Prometheus reads recurring corrections + Olympus's own source, rewrites
     agent prompts (`update_prompt`, with automatic backups), and files
     `propose_upgrade` notes for changes that need code.

  Prometheus runs weekly via the heartbeat **and** automatically after every
  N conversations (`OLYMPUS_AUDIT_EVERY_CHATS`, default 20 — counted across
  CLI, web, and Telegram; the audit runs in the background on the server's
  own credentials, never on a visitor's BYOK key). With `GITHUB_TOKEN` +
  `GITHUB_REPO` set, his upgrade proposals are **auto-filed as GitHub
  issues** — and the optional `upgrade-agent` workflow lets a coding agent
  implement them as pull requests automatically.

### Reasoning over serial problems (dependency-graph planning)

Most orchestrator-worker systems (Hermes included) fan tasks out in parallel
and stitch the pieces together — which breaks on problems where one step needs
another's result first. Olympus's Athena instead plans a **dependency graph**:

- Independent steps run **concurrently** (fast — latency is the slowest branch).
- A step that genuinely depends on another runs **after** it and **receives its
  output as input**, so it builds on the result instead of guessing.

So "research the market → price it from the findings → write copy for that
price" executes as a real chain (each step fed the last), while anything that
*can* run in parallel still does. The executor is cycle-safe and isolates
per-step failures. You get parallel speed *and* correct serial reasoning.

### Cross-model learning (opt-in) — the best of every frontier model

Olympus runs on whatever model a user brings (Claude, GPT, Gemini, …). Each
frontier model knows different things — so Olympus can distill the best of all
of them into one shared, quality-gated knowledge layer that lifts every model,
including the cheap ones.

It is **opt-in and privacy-first** (off by default):

- A user explicitly enables contribution (web ⚙ checkbox, `/contribute on` on
  Telegram/CLI). Only then is anything shared.
- What's shared is **anonymized at write time** — emails, phone numbers, long
  ID/account numbers, and API-key-shaped strings are stripped before a short
  snapshot (tagged with the source model) enters the shared queue. Raw user
  data never enters the pool.
- Metis's daily cycle reads the queue **grouped by source model** and distills
  genuinely cross-model insights — knowledge one model surfaced that helps
  every specialist regardless of model — into **provisional** skills.
- The **benchmark gate** then keeps only the skills that measurably help and
  reverts the rest, so a weak model's output can't pollute the shared library.

The result is something neither Hermes nor OpenClaw has: a system that gets
collectively smarter by distilling the best of every frontier model its users
bring — with consent, anonymization, and quality-gating built in.

### Keeping all 11 specialists strong (systematic training)

A specialist only improves at what's *measured* — so Olympus measures every
user-facing specialist and trains the weakest on a cadence:

- **Full coverage.** Every user-facing specialist (Plutus, Peitho, Hephaestus,
  Aegis, Iris, Chiron, Chronos, Argus, Mnemosyne) has benchmark items; any gap
  is auto-filled with a generated one. (Metis and Prometheus are internal —
  their quality is their effect on the system.)
- **`python -m olympus scores`** shows every specialist's current score,
  lowest first. **`python -m olympus train`** scores them all, then has
  Prometheus focus on the weakest — building skills and sharpening prompts,
  measured before/after with automatic rollback, so changes only stick if they
  raise the score. Coding uses the execution benchmark (real pass/fail).
- The heartbeat runs a training round every few days (`OLYMPUS_TRAIN_EVERY`),
  so the whole council levels up over time without you asking — and the
  weakest domain always gets attention first.

### How it gets smarter day by day

1. **Experience** — every conversation produces lessons (specialists),
   corrections (Aletheia), and your 👍/👎 feedback (web buttons, `/good` /
   `/bad` on Telegram and CLI). All of it lands in per-user memory.
2. **Synthesis** — Metis's daily learning cycle distills recurring patterns
   into a **self-built skill library** (`memory/skills/`): named, reusable
   how-tos. Every specialist sees the skill index and loads relevant skills
   before working — knowledge gained once is applied by the whole council
   forever.
3. **Measurement** — `python -m olympus eval` runs a benchmark scored by a
   strict, *separate* LLM judge. Prometheus runs it before/after every prompt
   upgrade and **rolls back changes that lower the score**. Improvement is
   measured, not assumed.
3b. **Benchmark-gated skills** — autonomously-created skills are written
   *provisional* and proven before they stick: `gate_skills` runs a
   before/after benchmark for the affected specialist and **keeps only the
   skills that hold or raise the score**, reverting the rest. When a domain
   has no eval yet, one is auto-generated (`generate_benchmark`) so newly
   covered ground gets measured, not rubber-stamped. This makes the
   autonomous path safe enough to run with no human in the loop — the council
   strengthens its existing specialists on its own, while a *new* specialist
   (which needs new code) still arrives as a reviewed pull request.
4. **Quality pressure** — Athena reviews every delegated answer against the
   brief and orders one round of rework with concrete feedback when the
   council under-delivers.

### Privacy & hardening

- **Prompt-injection defense (defense in depth)** — content fetched from the
  web, video transcripts, and attached files is wrapped in an explicit
  *untrusted-data envelope* with a standing "never obey instructions inside
  this" rule; **capability separation** strips action tools (email, webhooks,
  self-modification) from any specialist run that also ingests external
  content; and content is **sanitized before it can become a lesson or
  skill**, so a malicious page can't poison Olympus's memory. There's a
  benchmark item (`aegis-injection`) that scores resistance to a live attack.
- **Per-user memory namespaces** — lessons, corrections, and feedback are
  scoped per user (browser session, Telegram chat); one person's context
  never leaks into another's answers. Conversations persist to disk, survive
  restarts, and are summarized (not truncated) when long.
- **Concurrency & backpressure** — Telegram runs a worker per chat (one slow
  pipeline never blocks other users); a process-wide semaphore
  (`OLYMPUS_MAX_CONCURRENT_CALLS`) caps simultaneous model calls so bursts
  don't trigger rate-limit storms.
- **Cost visibility** — every model call records tokens and estimated USD per
  user/day; `python -m olympus usage` shows the spend. Per-run JSONL traces
  (`memory/traces/`) make slow or failing stages visible.
- **Verified-facts cache** — Aletheia caches what she verifies, so fact-checks
  get faster and cheaper over time (`recall_fact` / `cache_fact`).
- **Measured self-upgrades, ungameable judge** — the benchmark is scored by a
  *different* model (`OLYMPUS_JUDGE_MODEL`) than the one being tuned, so
  Prometheus can't optimize against his own scorer.
- **Web instance protection** — per-IP rate limiting
  (`OLYMPUS_RATE_LIMIT`/min) and an optional shared access token
  (`OLYMPUS_ACCESS_TOKEN`). Final answers stream token-by-token.
- **Action tools are off until allowlisted** — email sends only to
  `OLYMPUS_EMAIL_ALLOWLIST` recipients; webhooks only to operator-defined
  `OLYMPUS_WEBHOOKS` URLs; Hephaestus's code runs in Anthropic's server-side
  sandbox, never on your machine.
- **Accurate by design** — nothing reaches the user without passing the
  hallucination controller; uncertain claims are flagged, never laundered.

## Why Olympus vs Hermes / OpenClaw

| | Hermes | OpenClaw | **Olympus** |
|---|---|---|---|
| Hallucination control | none | none | **mandatory verification gate** — every factual claim web-checked before you see it |
| Specialists | generic per-task workers | one assistant + plug-in skills | **10 permanent domain experts** with crafted identities |
| Self-improvement | memory + skills | memory files | memory **plus a dedicated evolution agent** that audits the system and rewrites its own prompts |
| Internet access | via tools/skills | via skills/browser | server-side web search on Claude (zero connectors, zero MCP); built-in DuckDuckGo fallback on every other provider |
| Parallelism | yes | n/a | **yes** — specialists run concurrently |
| Models | model-agnostic | model-agnostic | **model-agnostic** — Claude first-class, plus any OpenAI-compatible endpoint (OpenAI, Gemini, Groq, OpenRouter, Ollama/local) with **bring-your-own-key in the web UI** |
| Footprint | full server stack | persistent Node.js service | **~2.5K lines of readable Python**, stdlib-only interfaces |

## Setup

```bash
pip install -r requirements.txt        # or: pip install .
export ANTHROPIC_API_KEY=sk-ant-...
```

Or with Docker:

```bash
docker build -t olympus .
docker run -p 8484:8484 -e ANTHROPIC_API_KEY=sk-ant-... olympus
```

### Hosting a public instance (HTTPS)

The built-in web server speaks plain HTTP — fine on `localhost`, **not** safe
to expose directly, because bring-your-own-key API keys and file contents
would travel unencrypted. Put it behind a TLS-terminating reverse proxy:

```nginx
server {
  listen 443 ssl;
  server_name olympus.example.com;
  ssl_certificate     /etc/letsencrypt/live/olympus.example.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/olympus.example.com/privkey.pem;
  location / {
    proxy_pass http://127.0.0.1:8484;
    proxy_buffering off;            # required for token streaming
    proxy_read_timeout 600s;        # pipeline runs can be minutes long
  }
}
```

Run Olympus bound to localhost (`--host 127.0.0.1`, the default) so only the
proxy can reach it, and set `OLYMPUS_ACCESS_TOKEN`, `OLYMPUS_RATE_LIMIT`, and
`OLYMPUS_MAX_CONCURRENT_CALLS` for a shared deployment. Caddy or a cloud load
balancer with managed certificates works equally well — the only hard
requirement is `proxy_buffering off` (or the equivalent) so streamed answers
aren't held back.

## Usage

```bash
python -m olympus                  # interactive chat (default)
python -m olympus web              # browser chat UI at http://localhost:8484
python -m olympus telegram         # Telegram gateway (zero extra deps)
python -m olympus ask "Find me 3 business opportunities in AI tooling"
python -m olympus scan             # Argus: world/opportunity scan now
python -m olympus watch <youtube-url>   # Mnemosyne: watch + learn now
python -m olympus queue <youtube-url>   # queue a video for the heartbeat
python -m olympus audit            # Prometheus: self-audit + self-upgrade now
python -m olympus learn            # Metis: distill experience into skills now
python -m olympus eval             # run the judge-scored quality benchmark
python -m olympus code-eval        # run Hephaestus's code against real tests
python -m olympus scores           # per-specialist benchmark scores (weakest first)
python -m olympus train            # strengthen the weakest specialists now
python -m olympus skills           # list the self-built skill library
python -m olympus gate             # benchmark-gate provisional skills now
python -m olympus usage --days 7   # estimated token/cost spend
python -m olympus heartbeat        # run the autonomous recurring loop
```

(`pip install .` also gives you a plain `olympus` command.)

### Connectors: MCP servers & custom plugins

Olympus connects to external tools and data two ways, both governed by the
same security model as its built-in tools:

**MCP servers (the ecosystem).** Olympus speaks the Model Context Protocol
natively on the Anthropic backend — plug in any of the hundreds of community
MCP servers (GitHub, Notion, Postgres, Slack, Tavily, Linear, …). Tools run
server-side on Anthropic's infrastructure.

```bash
# attach a server (tokens read from an env var, never stored on disk)
python -m olympus add-mcp tavily https://mcp.tavily.com/mcp/ \
       --type data --auth-env TAVILY_MCP_TOKEN --specialists argus,aletheia
export TAVILY_MCP_TOKEN=tvly-...
python -m olympus connectors          # list everything configured
```

Or drop a `memory/connectors.json` (see `examples/connectors.json.example`).

**Custom plugins (your own connectors).** A plugin is a plain Python function
that runs locally and works on **every** backend — the simplest way to wire
Olympus to your own REST API or internal system:

```python
from olympus.connectors import plugin

@plugin("crm_lookup", "Look up a customer by email",
        schema={"type": "object",
                "properties": {"email": {"type": "string"}},
                "required": ["email"]},
        specialists=["plutus"], action=False)
def crm_lookup(email: str) -> str:
    ...   # call your API, return a string
```

Put the file in a `plugins/` directory (or point `OLYMPUS_PLUGINS_DIR` at one;
see `examples/plugins/`). See `examples/plugins/example_rest_connector.py` for
a working template.

**The security edge nobody else has.** Hermes and OpenClaw let you plug in MCP
servers with no quality gate and no injection defense — a malicious or buggy
connector runs unchecked. Olympus gates connectors in two tiers:

- **Tier 1 — data connectors** (read-only): wired into specialists like any
  other tool. Output is wrapped as untrusted content, sanitized before it can
  touch memory, and **fact-checked by Aletheia** like any other claim.
- **Tier 2 — action connectors** (change the world): treated exactly like the
  email/webhook actuators, behind a **double gate** — defining one in
  `connectors.json` is not enough; it stays **inert** until the operator also
  names it in `OLYMPUS_MCP_ACTION_ALLOWLIST`. And capability separation still
  applies: action connectors are stripped from any specialist run that also
  ingests external content, so a poisoned page or document can never trigger
  one. Assign them to a non-web specialist like Chronos.

You get the whole MCP ecosystem *plus* the safety the others skip.

> MCP runs server-side on the Anthropic backend; custom plugins work on every
> backend. On OpenAI-compatible providers, use plugins for connectors.

### Coding that's *measurably* good — execution-scored

Hephaestus isn't graded on whether his code *looks* right — his code is **run
against real tests** and scored on whether it actually passes. `python -m
olympus code-eval` has him solve real tasks (merge intervals, an LRU cache, a
bug fix, debounce, deep flatten, Go word-count, …); each solution is extracted,
executed in an isolated sandbox with a timeout, and scored pass/fail. Languages
whose runtime isn't installed are skipped, not failed.

This objective signal feeds the self-improvement loop: Prometheus runs
`run_code_benchmark` before and after changing Hephaestus's prompt or coding
skills and keeps only changes that raise the **pass rate** — so his coding
ability improves against ground truth, not a judge's guess. It's the closest
thing to genuinely training a coding agent, and it's a coding edge neither
Hermes nor OpenClaw has.

### Controlled-autonomy actions — prepare, approve, execute, undo

Olympus doesn't just advise — it can *do* things, safely. The rule is absolute:
**agents prepare actions; they never execute them.** A prepared action is queued
with a preview; you approve it explicitly before it runs, and every step is
logged to an immutable audit trail.

```bash
python -m olympus actions          # see actions awaiting your approval (with preview)
python -m olympus approve <id>     # execute a prepared action
python -m olympus reject <id> ...  # decline it (teaches future behavior)
python -m olympus undo <id>        # reverse a reversible, executed action
python -m olympus autonomy 2       # set the autonomy dial (0–4)
python -m olympus grant email      # grant a permission scope
python -m olympus revoke all       # kill switch — revoke every scope
```

The safety model is structural, not hopeful:

- **Risk classes** — every action is `trivial` / `notable` / `irreversible` /
  `financial-legal`. **Irreversible and financial actions can NEVER auto-run**,
  at any autonomy level — they always require an explicit approval.
- **Autonomy dial (L0–L4)** — L1 (default) prepares and stops; only L3+ may
  auto-run *trivial, reversible* actions, and only within policy.
- **Two independent gates** — an action needs both its permission scope granted
  *and* its approval satisfied before it executes.
- **Injection-safe by construction** — because agents only *prepare*, a
  malicious email that tricks an agent into preparing a bad action is harmless:
  you see the preview and reject it. Reading can never become acting without you.

New capabilities (calendar, files, payments-prep) are just new action types
registered on this same spine — they inherit the gate automatically.

**First real integration — Gmail.** Connect a Google account and **Angelos**,
the correspondence manager, triages your inbox and *prepares* replies, drafts,
and archives — but **sending always goes through the approval gate**. Because
Angelos reads untrusted email, the capability-separation rule strips its direct
action tools automatically: it can only `prepare_action`, never send on its own.
So a malicious email that tries to make it wire money or leak data produces, at
worst, a prepared action you preview and reject. Set `GMAIL_ACCESS_TOKEN` (see
`.env.example`) to connect.

**Calendar too.** Angelos also reads your Google Calendar to propose genuinely
free times and *prepares* invitations — but creating an event emails the
attendees, so it's classed irreversible and **always waits for your approval**
(with undo that cancels). That completes the **email + calendar assistant**:
connect one Google account and Olympus triages your inbox, drafts replies,
proposes meeting times, and prepares the invites — you approve, it acts.

### Multilingual — native in any language

Olympus answers in the user's own language, generated natively rather than
translated. The council coordinates internally in English (cheaper, more
reliable JSON), but every word the user sees — direct replies, synthesized
answers, and marketing/social copy from Peitho and Iris — is authored in the
user's language with its cultural and idiomatic norms.

- By default it **matches the language of your message** (and the conversation,
  so short follow-ups stay in language).
- Set a **persistent preference** when you want it fixed:
  - Web UI: the ⚙ panel's *Language* field (`Spanish`, `日本語`, `auto`, …)
  - Telegram: `/lang French` (or `/lang auto`)
  - CLI chat: `/lang Arabic`
- A benchmark item (`peitho-multilingual`) scores native-quality output in
  another language, so the self-improvement loop measures multilingual quality
  too.

### Use several keys together — best of each, as one

Give Olympus more than one frontier key and it uses them **together**, not as a
switch: each part of the pipeline runs on whichever model is strongest for it.
With Claude + GPT, for example, reasoning and verification run on one while
coding runs on the other — composed into a single system.

- **Web UI:** the ⚙ panel has a "+ Model 2" row — add a second provider/model/key.
- **Env / CLI:** `OLYMPUS_MODELS=[{"provider":"openai","model":"gpt-5","api_key":"..."}]`
  alongside your primary key. `python -m olympus models` shows the assignment.

How it decides: a built-in capability ranking scores each model per role
(reasoning, coding, verification). The strongest model wins a role outright;
when two are comparable, the work is **split across keys** so both are actually
used. One key still works exactly as before — it just becomes "every role on
that model."

### Use any model (bring your own key)

Anthropic/Claude is the default and the most capable path (server-side web
search, adaptive thinking). But Olympus runs on **any OpenAI-compatible
endpoint** — OpenAI, Gemini, DeepSeek, Groq, Mistral, OpenRouter, Ollama,
LM Studio, vLLM — with zero extra dependencies:

```bash
# OpenAI
export OLYMPUS_PROVIDER=openai OLYMPUS_MODEL=gpt-4o OLYMPUS_API_KEY=sk-...

# Groq
export OLYMPUS_PROVIDER=openai OLYMPUS_MODEL=llama-3.3-70b-versatile \
       OLYMPUS_API_KEY=gsk_... OLYMPUS_BASE_URL=https://api.groq.com/openai/v1

# Fully local with Ollama (no API key at all)
export OLYMPUS_PROVIDER=openai OLYMPUS_MODEL=llama3 \
       OLYMPUS_BASE_URL=http://localhost:11434/v1
```

On non-Claude providers, web access automatically falls back to a built-in
client-side DuckDuckGo search — the architecture (and the hallucination
gate) works identically everywhere.

**In the web UI**, each visitor can click **⚙ model** and enter their own
provider, model, and API key. Keys live in the visitor's browser
(localStorage), travel only with their own requests, are used in-memory, and
are never stored or logged by the server — that's what makes a public
Olympus instance feasible without burning your own key.

### Telegram in 2 minutes

1. Message **@BotFather** on Telegram → `/newbot` → copy the token.
2. `export TELEGRAM_BOT_TOKEN=123456:ABC...`
3. `python -m olympus telegram`

Optional environment variables:

- `TELEGRAM_ALLOWED_CHAT_IDS=12345,67890` — restrict who can use the bot
  (open to everyone by default).
- `TELEGRAM_NOTIFY_CHAT_ID=12345` — the heartbeat proactively pushes
  opportunity scans and self-audit reports to this chat, so Olympus reaches
  *you* instead of waiting to be asked.

## Layout

```
olympus/
├── config.py        # model + cadence settings
├── llm.py           # streamed Claude calls (adaptive thinking, caching, retries)
├── agent.py         # the shared agentic tool loop
├── tools.py         # client-side tools + server-side web tool declarations
├── memory.py        # persistent memory (lessons, corrections, reports, upgrades)
├── youtube.py       # transcript fetching for Mnemosyne
├── specialists.py   # the council registry (add a specialist = prompt + 1 entry)
├── orchestrator.py  # Zeus → Athena → specialists → Aletheia pipeline
├── heartbeat.py     # the self-recurring loop (+ Telegram push notifications)
├── web.py           # zero-dependency browser chat UI
├── telegram.py      # zero-dependency Telegram gateway
├── cli.py           # command-line interface
└── prompts/*.md     # every agent's mind — editable, and upgradable by Prometheus
```

Memory lives in `./memory/` (gitignored): `lessons/`, `corrections/`,
`reports/`, `upgrades/`, `prompt_backups/`, plus the YouTube `watchlist.txt`.

## Safety rails

- Aegis is defensive-only by constitution; Prometheus is forbidden from
  weakening safety rules in any prompt.
- Prometheus can modify **prompts only** — code changes become written
  proposals in `memory/upgrades/` for a human (or coding agent) to apply.
- Every prompt rewrite is automatically backed up to `memory/prompt_backups/`.
