# The operator admin panel (`/admin`)

An overview and control surface for a running Olympus instance, served by
the web process (`olympus serve`). One page answers what previously took a
handful of CLI commands on the box:

| Section | Shows |
| --- | --- |
| Instance | version, provider key present, uptime, request/error counters, memory dir |
| Flags | sovereign mode, egress guard, contracts, fast mode, BYOK/login policy, prompt-cache TTL, provider fallback, token/key posture |
| Model pool | members (provider/model/host, key **presence** only), role assignment, fastest member, MoA |
| Budget & spend | today's spend vs. budget, per-model calls/tokens/cost |
| Channels | per-gateway configured/not (Telegram, Discord, Slack, WhatsApp, Signal, email, webhooks) — booleans only |
| Autonomous loop | every heartbeat cycle with cadence, last run, and next due; scheduler and goal wake times |
| Standing goals | each goal's status, latest progress, closing evidence |
| Pending approvals | held actions across all users (type, risk, why, age) |
| Skills | library size, provisional count, per-skill owner + description |
| Scheduled tasks | each job's cadence, prompt, delivery target, last run |
| Connectors | MCP servers (active/inert), plugins, registered lifecycle hooks |
| Security posture | sovereign snapshot, egress allowlist size, egress guard |
| Recent errors | last captured errors with age and location |

## Phase 2 — acting on running state

The panel can also drive the operations the CLI already exposes, via
`POST /api/admin/act` (op + params):

- **Approvals**: approve-and-execute or deny any held action, across users —
  this IS the approval spine's human step, now one click instead of
  `olympus actions` on the box.
- **Goals**: add a standing goal (with an optional completion contract),
  mark done, drop.
- **Schedules**: add / enable / disable / remove natural-language tasks.
- **Maintenance**: trigger the skill gate, curation, or a backup — long jobs
  run in a background thread and report into reports/health.
- **Autonomy dial**: set a user's L0–L4 level.

Nothing here bypasses a gate that existed before: approving an action runs
the same `actions.approve` the CLI calls, and irreversible actions still
require exactly this explicit human step. **Configuration (credentials,
channels, models) remains CLI-only — that's Phase 3, with its own review.**

Mutation requests must carry the `X-Olympus-Admin: 1` header in addition to
the auth below. Browsers only attach custom headers after a CORS preflight
this server never approves, so a hostile web page cannot fire cross-origin
mutations at a loopback panel (CSRF defense).

## Access model

The page at `/admin` is a data-free HTML shell; all data comes from
`GET /api/admin`, which is gated like the OpenAI-compatible endpoint:

- **`OLYMPUS_ACCESS_TOKEN` set** → the panel requires it (the browser sends
  the same token the chat UI stores under ⚙ *access*; the panel prompts for
  it once and keeps it in the browser).
- **No token set** → loopback-only: the request must come from the same
  machine, the server must be *bound* to loopback, and requests carrying
  reverse-proxy forwarding headers are refused. An admin surface is never
  open just because auth was left unconfigured.

## No secrets, by construction

The snapshot reports credentials as **booleans** ("configured"), reduces
`base_url`s to scheme+host (userinfo and paths stripped), and the test suite
asserts that known secret values planted in the environment never appear in
the payload (`tests/test_adminpanel.py`).
