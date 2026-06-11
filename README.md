# ⚡ OLYMPUS

A self-sufficient, self-recurring, **self-improving** multi-agent AI system
built on the Claude API. A main agent commands a supervised council of
specialists, every answer passes through a hallucination controller, and the
system continuously scans the world, learns from YouTube, and upgrades itself.

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
| **Athena** | Supervisor — turns goals into precise specialist assignments |
| **Aletheia** | Hallucination controller — verifies claims, fixes/flags, learns from mistakes |
| **Plutus** | Financial specialist |
| **Peitho** | Marketing specialist |
| **Hephaestus** | Coding specialist |
| **Aegis** | Cybersecurity specialist (strictly defensive) |
| **Iris** | Social network assistant |
| **Chiron** | Coaching specialist |
| **Chronos** | Scheduling management |
| **Argus** | Opportunity scout — surfs the internet via Anthropic's **server-side web search** (no MCP connections required) for business opportunities and world events |
| **Mnemosyne** | YouTube learner — watches videos via transcript, summarizes what it understood, stores lessons |
| **Prometheus** | Evolution specialist — audits Olympus, finds what's missing inside it, upgrades agent prompts, files improvement proposals |

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
- **Accurate by design** — nothing reaches the user without passing the
  hallucination controller; uncertain claims are flagged, never laundered.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
python -m olympus                  # interactive chat (default)
python -m olympus ask "Find me 3 business opportunities in AI tooling"
python -m olympus scan             # Argus: world/opportunity scan now
python -m olympus watch <youtube-url>   # Mnemosyne: watch + learn now
python -m olympus queue <youtube-url>   # queue a video for the heartbeat
python -m olympus audit            # Prometheus: self-audit + self-upgrade now
python -m olympus heartbeat        # run the autonomous recurring loop
```

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
├── heartbeat.py     # the self-recurring loop
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
