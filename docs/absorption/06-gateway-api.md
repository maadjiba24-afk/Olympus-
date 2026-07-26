# Absorption 06 — Serving, APIs & Admission Control

**Colibri domain:** the stdlib-only OpenAI **and** Anthropic dual-API HTTP gateway (§9.2),
`GenerationScheduler` bounded-FIFO admission with per-slot fairness / 429s / queue-wait headers
(§9.2), cancellation propagation from client disconnect down to the engine (`CANCEL`, MSG_PEEK
probe, §9.2), the keepalive pump for minutes-long cold prefill (§9.2), GLM tool-call parsing
with schema-typed coercion + unclosed-tail recovery + opt-in salvage (#401/#505, §9.2),
streaming marker suppression with hold-back across chunk boundaries (§9.2), the SEC-6/7/8
security decisions (§9.2), and the stdin/stdout line-protocol multiplexer with its additive
7th-field forward compatibility (§9.1, §26.10) *and* its own documented forward-compat rule
violations (§27: "Python dispatcher violates the documented ignore-unknown-lines rule;
`serve_protocol.md` documents frames the server doesn't emit").
**Olympus target:** hardened multi-channel serving — `olympus/web.py` (the `/v1/*` surface),
`olympus/openai_server.py`, `olympus/gateway.py` (Dispatcher, _Supervisor, inflight journal),
`olympus/toolcall_repair.py`, `olympus/a2a_server.py`, `olympus/mcp_server.py`,
`olympus/usage.py` (`slot()`), `olympus/steering.py`, plus `docs/GATEWAY.md`,
`docs/OPENAI_ENDPOINT.md`, `docs/SOVEREIGNTY.md`.

## Domain thesis

Colibri's serving layer exists because one irreplaceable resource — a single 370 GB
disk-streaming engine that takes minutes to prefill — must be shared politely among impatient
HTTP clients, so every mechanism in §9.2 is a *politeness protocol around scarcity*: bounded
admission instead of silent pile-up, honest 429s with `Retry-After` instead of timeouts,
queue-wait made visible in a response header, cancellation that actually reaches the engine,
keepalives so a blocked client knows the server is alive, and typed repair so a
quantization-degraded model's tool calls survive the wire. Olympus has the *same scarcity in a
different currency* — `usage.slot()` caps concurrent model calls process- and machine-wide, a
council run takes tens of seconds to minutes, and seven-plus channels (CLI, web, Telegram,
Slack, Discord, WhatsApp, webhook, A2A, MCP) all feed the same pipeline — but today the
channels each improvise: `gateway.Dispatcher` queues unboundedly per user, `web.py` 429s only
on rate/budget, nothing measures queue wait, and nothing can cancel a council run once
started. This domain absorbs Colibri's discipline as one shared admission/cancellation/progress
spine under all channels, elevates `toolcall_repair.py` (already Colibri-adjacent in spirit)
into a principled typed-recovery layer with salvage telemetry, and converts Colibri's honest
failure — a wire protocol whose documentation and consumers drifted from the code — into a
CI-enforced contract discipline Olympus already knows how to run (the drift-gated capability
counts). The accumulating asset is *serving evidence*: per-channel queue-wait, shed, cancel,
and repair-rate series that feed the Calibration Record (`docs/MOAT_ANALYSIS.md` Asset 1) and
make "Olympus under load" a measured claim instead of a vibe.

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| Stdlib-only dual-API gateway (OpenAI + Anthropic) | one threaded Python file speaks both protocols over one engine; port bound before engine launch (§9.2) | **redesign** | `openai_server.py`, `web.py`, **new `olympus/anthropic_server.py`** |
| `GenerationScheduler` bounded-FIFO admission | bounded queue over `kv_slots`, per-slot fair, 429 `queue_full`/`queue_timeout` + `Retry-After`, `x-colibri-queue-wait-ms`, counters in `/health` (§9.2) | **new-subsystem** | **new `olympus/admission.py`**, `web.py`, `gateway.py`, `usage.py` |
| Cancellation propagation | client disconnect (MSG_PEEK) → HTTP layer → `CANCEL` frame → engine drops the slot (§9.1–9.2) | **new-subsystem** | **new `olympus/cancel.py`**, `orchestrator.py`, `steering.py` pattern, `web.py`, gateways |
| Keepalive pump for long prefill | background thread emits a `reasoning_content` delta after 10 s silence, write-lock-serialized vs `[DONE]` (§9.2) | **redesign** | `openai_server.py`, `web.py` `_stream_v1`, gateways (typing/progress), orchestrator stage hooks |
| Tool-call parse + typed coercion + unclosed-tail recovery + opt-in salvage | byte-exact template, strict regex parse, schema-typed argument coercion, unambiguous-only tail recovery, `COLI_TOOL_SALVAGE` de-mangler (§9.2, #401/#505) | **redesign** | `toolcall_repair.py` (extend), `openai_compat.py`, `bedrock_converse.py`, `backend.py` |
| Streaming marker hold-back | suppress `<tool_call>`-class markers in SSE with hold-back across chunk boundaries (§9.2) | **absorb-principle** | **new `olympus/streamguard.py`**, `web.py` `_stream_reply`/`_stream_v1`, `egress.py` |
| SEC-6/7/8 gateway security decisions | fail-closed non-loopback bind w/o key; DNS-rebinding Host guard; authed telemetry; const-time compare; CORS allowlist; Slowloris timeout; body caps (§9.2) | **absorb-principle** | `web.py` (mostly built), `a2a_server.py`, `mcp_server.py`, **new `olympus/authkit.py`** + route-auth CI audit |
| Line-protocol mux & its forward-compat violations | `SUBMIT`/`CANCEL`/`DATA`/`DONE`/`ERROR` frames, additive optional field (§26.10) — but dispatcher & docs drifted (§27) | **absorb-principle** (the lesson, not the protocol) | **new `olympus/protocontract.py`** or CI script, `a2a.py`, `mcp_server.py`, `federation.py`, `capabilities.py` pattern |
| *(beyond Colibri)* cost- & priority-aware multi-tenant admission | — (Colibri is single-model, per-slot only) | **new-subsystem** | `olympus/admission.py`, `usage.py` (`budget_status`), `heartbeat.py`, `trust.py` |

---

## 1. The stdlib-only dual-API gateway (OpenAI + Anthropic surfaces)

**1. What Colibri does.** `c/openai_server.py` (1,695 lines, stdlib-only, threaded) serves
`POST /v1/chat/completions` + `/v1/completions` + `GET /v1/models` (full SSE, tools,
`response_format`, `reasoning_effort`) **and** a complete Anthropic Messages translation layer
(`POST /v1/messages`, #343 — named SSE events, tool_use/tool_result, thinking, Anthropic error
envelopes, `x-api-key` auth; Claude Code is the reference client), plus authed telemetry
endpoints and static hosting of the dashboard, all proxying one persistent engine subprocess
whose stdout a single dispatcher thread demuxes by request id (§9.2).

**2. Why it exists.** The engine speaks a private byte protocol; the world speaks OpenAI and
(increasingly) Anthropic. Serving both makes every existing client — IDE plugins, `openai`
SDKs, Claude Code — a Colibri client with a `base_url` change, and zero dependencies keeps the
gateway installable anywhere the engine runs.

**3. How it works internally.** HTTP port bound *before* the engine launches (a busy port
fails in milliseconds, not after loading 370 GB); one dispatcher thread routes engine `DATA`
frames into per-request queues; unsupported fields are refused loudly with 400s ("deliberate
non-features"), never silently ignored.

**4. Strengths.** Dual dialect = maximal client reach from one process. Stdlib-only matches
deployment reality. Bind-before-load is a beautiful fail-fast. Loud refusal of unsupported
params prevents clients silently believing `stop_sequences` worked.

**5. Weaknesses & trade-offs.** One Python thread per connection (no async) caps concurrency;
the Anthropic layer is a hand-maintained translation that must chase two evolving specs; the
single dispatcher thread is a serialization point and a single point of failure for *all*
streams; "refuse loudly" (Anthropic side) and "accept-and-ignore" (OpenAI side, e.g. Olympus
does this too for `tools`) are philosophically inconsistent between the two surfaces of the
same server.

**6. Security implications.** A wider API surface is a wider attack surface — the Anthropic
layer added a second auth header path (`x-api-key`) that had to be constant-timed too.
Translation layers are where injection hides: a field passed through unvalidated on one
dialect but sanitized on the other is a classic bug class.

**7. Scalability.** Fine for its purpose (a handful of slots on one engine); would not survive
hundreds of concurrent SSE streams. Olympus's `web.py` has the same threaded-stdlib shape and
the same ceiling — acceptable at self-hosted scale, and honestly so.

**8. Performance.** Translation cost is noise next to generation. The dispatcher demux adds one
queue hop per chunk; irrelevant at 6 tok/s, and equally irrelevant at council latency.

**9. Maintainability.** 1,695 lines covering two full dialects in one file is at the edge;
Olympus already splits translation (`openai_server.py`, pure, no I/O) from plumbing (`web.py`
`Handler`) — a strictly better factoring to keep.

**10. Olympus redesign.** *Adopt the dual-dialect principle; keep the Olympus factoring.*
Olympus serves the **council as a model** on `/v1/chat/completions` today
(`docs/OPENAI_ENDPOINT.md`; `MODEL_ID = "olympus-council"` in `openai_server.py`). Add the
Anthropic Messages surface the same way Colibri did — because Claude Code, and every
Anthropic-SDK agent, then drives the council natively:

- **New `olympus/anthropic_server.py`**, the exact mirror of `openai_server.py`: pure
  translation, no network, no HTTP — `messages_to_prompt`-equivalent over Anthropic content
  blocks, `message_start → content_block_delta → message_stop` SSE framing, Anthropic error
  envelope, stop reasons. `web.py` mounts `POST /v1/messages` beside the existing routes and
  reuses `_check_v1_auth` (accepting `x-api-key` as an alias for the bearer, constant-time via
  the same comparison Olympus already uses in `a2a_server._auth_ok`).
- **Resolve the loud-vs-silent inconsistency deliberately** (Colibri never did): Olympus policy
  is *accept-and-ignore for tuning params* (`temperature`, `top_p` — the council has its own
  routing) but *400 for semantic params we can't honor* (`n>1`, `logprobs`,
  `tools` **once we advertise support** — until then it stays documented-ignored per
  `docs/OPENAI_ENDPOINT.md` §"v1 scope"). Write the policy into both modules' docstrings and a
  shared `UNSUPPORTED_400` / `IGNORED_OK` table so the two dialects can't drift.
- **Model aliases as council modes** (beyond Colibri): `GET /v1/models` grows
  `olympus-council-fast` (maps to `/fast on` semantics) and `olympus-council-deep`, so API
  clients select latency tiers without a custom header — the `model` field is already echoed
  back, so this is additive.

**11. Final architecture.** Modules: `openai_server.py` (exists), **`anthropic_server.py`**
(new, ~300 lines, stdlib), `web.py` `Handler` mounts both under one `_check_v1_auth`. Data
model: none new (stateless translation). Env/CLI: existing `OLYMPUS_API_KEYS`;
`python -m olympus serve` unchanged. Integration: both surfaces call the same
`orchestrator.Olympus.ask` / `ask_stream`; sovereign mode is unaffected (inbound serving is not
egress); the security gate is untouched because the API is a text funnel like `a2a_server.py` —
actuation still goes through the approval spine.

**12. Why superior.** Colibri bolted the Anthropic dialect into an already-huge file; Olympus
gets it as a second pure translation module with the plumbing shared, unit-testable without
sockets (the `a2a_server.handle_request` pattern), and with the accept/refuse policy made
explicit instead of accidentally inconsistent. And the "model" being served is a verified
council, not a raw engine — every API answer already passed Aletheia.

---

## 2. GenerationScheduler → unified admission control & backpressure (all channels)

**1. What Colibri does.** A bounded FIFO over `kv_slots` capacity: at most `COLI_MAX_QUEUE`
(8) requests wait, up to `COLI_QUEUE_TIMEOUT` (300 s); overflow → 429 `queue_full`, expiry →
429 `queue_timeout`, both with `Retry-After`; admission is per-slot fair (no head-of-line
blocking across slots); every response carries `x-colibri-queue-wait-ms` and `x-request-id`;
counters are exposed on authed `/health` (§9.2).

**2. Why it exists.** One engine, minutes-long prefills, impatient clients: without bounded
admission the server accumulates doomed work, latency becomes unbounded, and clients time out
holding queue positions. Bounded FIFO + honest 429 converts overload from silent degradation
into a legible protocol.

**3. How it works internally.** A scheduler object guards slot acquisition; a queued request
waits with a deadline; per-slot queues prevent one hot session from starving others; wait time
is measured at admission and stamped into the response headers.

**4. Strengths.** Every overload behavior is *chosen*: cap, timeout, error code, retry hint.
Queue wait as a header makes the invisible visible — clients and dashboards see contention
without a metrics stack. Per-slot fairness is the minimum viable multi-tenancy.

**5. Weaknesses & trade-offs.** FIFO ignores cost: a 32k-token prefill and a "hi" queue
equally. No priority classes — an interactive user waits behind a batch job. Fairness is
per-*slot* (session), not per-*user* or per-tenant. The queue is in-memory: a restart drops
waiting requests silently (Colibri accepts this; Olympus's `gateway.py` inflight journal
already does better). Nothing sheds *proactively* on cost or budget — only on count.

**6. Security implications.** Bounded admission **is** DoS armor: `COLI_MAX_QUEUE` bounds
memory, `Retry-After` bounds retry storms. For Olympus the stakes are higher — an unbounded
queue is not just RAM, it is *money* (every admitted request fans into paid API calls). An
attacker who can enqueue 1,000 council runs on a Telegram bot spends the operator's budget.
Admission is therefore a *security gate for spend*, and must sit in front of `usage.slot()`,
not behind it.

**7. Scalability implications.** For Olympus the scarce resource is `MAX_CONCURRENT_CALLS`
(the `usage.py` `BoundedSemaphore` + machine-global `proclock` slot) and the daily budget
(`usage.budget_status()`). Today, when all slots are busy, callers *block inside* `slot()`
invisibly — the Colibri translation is: block visibly, boundedly, and report it. A shared
admission layer also finally gives the multi-process reality (heartbeat + web + gateway daemon
contending on `proclock`) one legible picture.

**8. Performance implications.** Queue-wait measurement is one `time.monotonic()` pair —
free. The win is *latency honesty*: p95 answer time decomposes into queue-wait + council-time,
which is exactly the decomposition `metrics.py`/`liveeval` need to distinguish "Olympus got
slow" from "Olympus got popular."

**9. Maintainability.** One admission module beats seven per-channel improvisations. Today:
`web.py` has ad-hoc 429s (rate limit at line ~1868, daily budget at ~2101), `gateway.py`'s
`Dispatcher.submit` queues unboundedly per user, Telegram long-poll has nothing, and
`usage.slot()` blocks silently. Divergent backpressure is exactly the kind of drift Olympus's
culture exists to kill.

**10. Olympus redesign.** **New `olympus/admission.py`** — one bounded, fair, cost-aware
admission spine in front of every council entry:

- `class Admission` with `admit(channel, user, *, priority, est_cost) -> Ticket` — bounded
  wait (`OLYMPUS_MAX_QUEUE`, default 16; `OLYMPUS_QUEUE_TIMEOUT`, default 300 s) over the
  existing `usage.slot()` capacity. Raises `QueueFull` / `QueueTimeout` typed errors that each
  transport renders natively: HTTP → 429 + `Retry-After` (spec-correct on `/v1/*` so OpenAI
  SDK backoff *just works*); Telegram/Slack → a human sentence ("The council is at capacity —
  you're #3 in line, ~40 s"); MCP/A2A → JSON-RPC error / 429 envelope.
- **Per-user round-robin fairness** (Colibri's per-slot fairness, upgraded): the queue is a
  dict of per-user FIFOs drained round-robin, so one chatty user (or one channel) cannot
  starve the rest — `gateway.Dispatcher` already serializes per-user; admission adds the
  cross-user arbiter above it.
- **Priority classes** (beyond Colibri): `interactive` (CLI/web/chat) > `api` (`/v1/*`, A2A)
  > `background` (heartbeat scans, `agentbeat`, training rounds). `heartbeat.py` jobs admit at
  `background` and are the *first shed* under load — the autonomous loop must never make a
  human wait. This is the model-price-tier translation of Colibri's VRAM/RAM/disk tiers:
  scarce fast capacity goes to the interactive tier first.
- **Cost-aware shed** (beyond Colibri): `admit()` consults `usage.budget_status()`; when the
  daily budget is nearly spent, `background` is refused outright and `interactive` degrades to
  fast mode (`config.fast_setting()` override per-ticket) before refusing — graceful
  degradation ladder instead of a cliff.
- **Observability**: every ticket records `queue_wait_ms`, `channel`, `priority`, outcome
  (`served/shed/timeout/cancelled`) to a ring + a daily JSONL under `MEMORY_DIR/admission/`
  (atomic writes, same discipline as `usage._atomic_write_json`). `/v1/*` responses gain
  `x-olympus-queue-wait-ms` and `x-request-id`; `olympus gateway --status` and `/health` (the
  authed detail tier, per SEC-8 below) surface counters. The JSONL series is Calibration
  Record feed: measured serving reliability per channel over time.

**11. Final architecture.** Modules: **`admission.py`** (new, ~250 lines, stdlib);
`usage.py` unchanged (admission wraps `slot()`, never replaces it — the semaphore remains the
hard cap); call sites: `web.py` `do_POST` (both `/api/chat` and `/v1/*`), `gateway.reply_for`
(before `bot.ask`), `a2a_server.handle_request`, `mcp_server` tool dispatch,
`heartbeat.py`/`scheduler.py` job launches. Data: `MEMORY_DIR/admission/YYYY-MM-DD.jsonl` +
in-memory ring. Env: `OLYMPUS_MAX_QUEUE`, `OLYMPUS_QUEUE_TIMEOUT`,
`OLYMPUS_QUEUE_PRIORITIES=off` escape hatch. CLI: `olympus queue` (live counters, mirrors
`olympus gateway --status` shape). Gate-cost rule (ROADMAP §0): admission adds one lock + one
clock read per request — no model calls, no measurable budget.

**12. Why superior.** Colibri's scheduler is per-slot fair over one resource on one process.
Olympus's is user-fair, priority- and *cost*-aware, spans processes (via the existing
`proclock` machine-global slot), degrades gracefully (fast-mode ladder) before shedding, and
turns every overload event into recorded evidence. Colibri's weakness — FIFO blind to cost and
tenancy — becomes the design center, because for an API *client* the queue is the budget.

---

## 3. Cancellation propagation through the whole council pipeline

**1. What Colibri does.** Client disconnect is detected with an MSG_PEEK probe on the socket;
the HTTP layer sends `CANCEL <id>` down the mux protocol; the engine frees the slot and emits
`ERROR CANCELLED`; a mid-queue request is cancelled without ever running (§9.1–9.2).

**2. Why it exists.** At 0.05–6 tok/s, an abandoned request holds a slot for minutes and
streams 19 MB expert reads for nobody. Cancellation is the difference between one impatient
user and a self-inflicted DoS.

**3. How it works internally.** The mux protocol carries `CANCEL` as a first-class frame; the
decode loop checks the cancel flag at step boundaries; disconnect detection is polled on the
serving thread between chunk writes.

**4. Strengths.** End-to-end: browser Stop button → socket close → HTTP layer → engine slot
freed. Cancellation has a *protocol frame*, not just a dropped connection — queued work is
cancellable before it starts.

**5. Weaknesses & trade-offs.** Poll-based disconnect detection has latency (a burst of
generation between polls is wasted); cancellation granularity is the decode step; nothing
propagates *upstream* (Colibri has no upstream — Olympus does, and that is the hard part).

**6. Security implications.** Uncancellable work is a resource-exhaustion primitive: an
attacker opens N streams, disconnects, and the server burns full council cost N times. For
Olympus every uncancelled run is *billed* — cancellation is spend protection, same class as
admission. Cancel handling must also be safe against *spoofed* cancels: only the admitting
transport may cancel its own ticket (tickets are unguessable ids, the `x-request-id`).

**7. Scalability implications.** The council fans one request into many provider calls
(specialists in parallel under Athena's dependency graph). Without cancellation, one abandoned
web tab costs the *whole graph*. With it, cancellation multiplies: cancelling one ticket
cancels every pending specialist step — the leverage is much higher than Colibri's single
decode loop.

**8. Performance implications.** Check-at-boundary cancellation is free (one flag read per
pipeline stage / tool round — the same cadence `steering.py` already drains notes at). The
saved cost is measured directly in the admission ledger (`outcome=cancelled`, tokens saved
estimated from stage).

**9. Maintainability.** The `steering.py` pattern is the proof that Olympus can thread a
per-conversation signal through nested specialist runs (contextvar + thread-safe queue,
drained at tool-round boundaries). Cancellation is steering's sibling with stop semantics —
same plumbing, one new module, no orchestrator rewrite.

**10. Olympus redesign.** **New `olympus/cancel.py`**, deliberately shaped like
`steering.py`:

- `request(key)` sets a cancel flag for a conversation/ticket key; `cancelled(key) -> bool`;
  a contextvar carries the current key exactly as `steering._current` does, so nested
  specialist runs and Aletheia verification all see one flag.
- **Check points** (adopt Colibri's step-boundary doctrine): the orchestrator checks between
  pipeline stages (route → plan → dispatch → verify → synthesize), Athena's executor checks
  before launching each graph step and between tool rounds in `openai_compat.run_agent` /
  the Anthropic agent loop. A cancelled run raises typed `RunCancelled`; partial state is
  discarded, the admission ticket closes as `cancelled`, and history records a
  "(cancelled by user)" turn so the transcript stays truthful.
- **Transport wiring**: `web.py` streaming paths poll the socket between SSE writes (a write
  to a closed socket already raises — catch `BrokenPipeError`/`ConnectionResetError` in
  `_stream_v1`/`_stream_reply` and call `cancel.request`); non-streaming HTTP uses a
  best-effort MSG_PEEK probe on `self.connection` (Colibri's exact trick) between stages; chat
  gateways get `/stop` — the command companion to `/steer`, fast-pathed the same way
  (`gateway.try_steer` gains a sibling `try_stop` running *before* the per-user serial queue,
  since the whole point is reaching a run already in flight).
- **The honest boundary** (differentiate, with reason): an in-flight provider HTTP call
  cannot be aborted mid-request with blocking `urllib` (`openai_compat._post` runs up to 600 s
  under `usage.slot()`). v1 cancels at call *boundaries* only, and says so — no "instant
  cancel" theater. A bounded research spike (below) evaluates moving `_post` to
  `http.client` with socket shutdown for true mid-call abort; until measured, boundary-cancel
  is the documented contract. (This is the ROADMAP §0 no-free-claims rule applied to
  ourselves.)

**11. Final architecture.** Modules: **`cancel.py`** (new, ~120 lines);
edits: `orchestrator.py` (stage checks + `RunCancelled`), `web.py` (disconnect → cancel),
`gateway.py` (`/stop` fast path + HELP text), `admission.py` (queued-ticket cancel — a queued
request cancels without ever admitting, Colibri's mid-queue cancel). Env:
`OLYMPUS_CANCEL=off` escape hatch (zero-behavior-change when off — the observability
doctrine). Integration: heartbeat/background jobs are cancellable by the same key via
`olympus queue cancel <id>`; the ledger/`trace.py` records cancellation causally.

**12. Why superior.** Colibri cancels one loop; Olympus cancels a *graph*, including work not
yet started (queued tickets, unlaunched Athena steps) — where the leverage is largest — and
records every cancel as outcome evidence. The one thing Colibri does better (sub-second abort
of the compute itself) is named, bounded, and spiked rather than claimed.

---

## 4. Keepalive pump → progress semantics for long council runs

**1. What Colibri does.** Cold prefill can block for minutes with zero bytes on the wire, so a
background thread emits a `reasoning_content: "."` SSE delta after 10 s of silence — it lands
harmlessly in clients' thinking panels — with writes lock-serialized so `[DONE]` can never
interleave with a keepalive (§9.2).

**2. Why it exists.** Idle TCP streams die: proxies, load balancers, and client SDKs time out
silent connections. A keepalive that is *protocol-legal content* survives every middlebox
without special configuration.

**3. How it works internally.** A per-request pump thread watches a last-write timestamp;
the write lock is the entire correctness story (interleaving a frame inside another frame
corrupts SSE).

**4. Strengths.** Solves a real killer (clients abandoning healthy long requests) with ~30
lines. Choosing `reasoning_content` as the carrier is elegant: semantically "the model is
thinking," invisible in final answers.

**5. Weaknesses & trade-offs.** A dot is a heartbeat, not information — the client knows the
server is alive but not *what it is doing* or *how long remains*. It is also a small protocol
lie (no reasoning was produced). Colibri had nothing better to say because an engine prefill
is opaque; **a council run is not** — Olympus knows exactly which stage and which specialists
are running.

**6. Security implications.** Progress frames must not leak pipeline internals to unauthed
API callers beyond what the operator would show (specialist names are fine; tool arguments
are not — they can contain user data and must pass the same egress discipline as
`notify_all`'s BROADCAST guard in `gateway.py`).

**7. Scalability implications.** One pump thread per stream is fine at Olympus's scale
(threaded `web.py` already spends a thread per connection). The keepalive prevents the
worst scaling failure — clients retrying abandoned-but-running requests, doubling load
exactly when the system is slowest (a retry storm amplifier Colibri's design quietly kills).

**8. Performance implications.** Zero on the pipeline. Large on *perceived* latency: a
Telegram user watching "typing…" and a web user watching "Athena: dispatching Plutus,
Hephaestus (2/5 steps)" both stop resending the question — resends are the most expensive
no-op in the system.

**9. Maintainability.** Olympus already has three ad-hoc progress behaviors (CLI spinner, web
status line, nothing on `/v1/*`). One stage-event source with per-transport renderers
replaces them.

**10. Olympus redesign.** *Adopt the pump; upgrade the payload from liveness to progress.*

- **Orchestrator stage events** (the real content): `orchestrator.py` emits
  `(stage, detail)` callbacks — `routing`, `planning`, `dispatch(step i/n, specialist)`,
  `verifying`, `synthesizing` — on an injected `on_progress` hook (default no-op: byte-identical
  behavior when unused, the DISK-CLASS "provable by construction" doctrine from §18).
- **Per-transport rendering**: `/v1/chat/completions` streams get Colibri's exact trick — a
  keepalive/progress delta after `OLYMPUS_KEEPALIVE_SECS` (default 10) of silence, carried in
  the OpenAI-compatible way (`delta.reasoning_content: "council: dispatching hephaestus…"`),
  behind one per-response write lock in `web.py` so `[DONE]` can never interleave (absorb the
  lock discipline verbatim). The Anthropic surface uses protocol `ping` events, which the
  Messages spec already blesses. Non-streaming HTTP cannot carry progress — the keepalive there
  is TCP-level only, documented. Telegram/Slack/Discord render the same events as
  typing-indicator refresh + an optional single edited status message; the CLI/TUI spinner
  subscribes to the identical hook.
- **Long-run notify** (beyond Colibri): a run exceeding `OLYMPUS_PROGRESS_NOTIFY_SECS` on a
  chat channel posts one "still working — Aletheia is verifying" message; the inflight journal
  (`gateway.inflight_mark`) already proves the message-in-progress bookkeeping exists.

**11. Final architecture.** Edits: `orchestrator.py` (`on_progress` hook), `web.py`
(pump thread + write lock in `_stream_v1`/`_stream_reply`), `openai_server.py` (a
`progress_chunk()` helper beside `_chunk`), gateways (typing refresh). New module: none —
this is deliberately a hook, not a subsystem. Env: `OLYMPUS_KEEPALIVE_SECS`,
`OLYMPUS_PROGRESS=off` (restores byte-identical streams). Integration: progress events also
feed `trace.py` timestamps, giving the stage-latency decomposition `metrics.py` wants — the
observability and the UX are the same instrumentation.

**12. Why superior.** Colibri sends a dot because its engine is a black box mid-prefill;
Olympus sends the truth because the council's structure is legible. The keepalive stops being
a protocol lie and becomes the pipeline's own telemetry, at zero cost when disabled.

---

## 5. Tool-call parsing as a principled typed-recovery layer

**1. What Colibri does.** Renders the GLM tool template byte-exactly (invented preambles make
the model hallucinate other frameworks' syntax); parses `<tool_call>` blocks with a strict
regex; **coerces arguments by the tool's JSON schema types** (a string-typed `"12345"` stays a
string); recovers **unclosed tails** when generation ran out of budget mid-call — but *only
when unambiguous*: prose can never fabricate a call (#401/#505); ships an opt-in de-mangler
(`COLI_TOOL_SALVAGE=1`) for heavily-quantized output (§9.2).

**2. Why it exists.** An int4-quantized model's tool syntax is *lossy at the edges* — the
semantics are usually intact, the bytes often aren't. Rejecting every mangled call wastes a
correct decision; accepting anything fabricates actions. The layer is a typed channel decoder
between those failure modes.

**3. How it works internally.** A recovery ladder from strict to permissive, each rung gated:
exact parse → typed coercion → unambiguous tail-closing → (opt-in) salvage; every rung
preserves the invariant that a recovered call must name a real offered tool with
schema-plausible arguments.

**4. Strengths.** The *graded* ladder with an explicit opt-in top rung is the right shape:
default behavior is conservative, degraded-model operators consciously buy more recovery. Typed
coercion driven by the schema (not by guessing) prevents the classic `"12345"` → `12345`
corruption. "Prose can never fabricate a call" is a security invariant stated as a parser
property.

**5. Weaknesses & trade-offs.** No telemetry: Colibri cannot tell an operator *how often*
salvage fired or what it changed — an unmeasured repair layer can mask model regression (the
model gets worse, the repairs get busier, quality metrics stay flat until they cliff). Salvage
is a boolean, not per-tool or per-risk. Repair happens at parse time with no memory: the same
malformation pattern is re-derived every call instead of accumulating as evidence.

**6. Security implications.** Every rung of leniency widens the forgeable surface: the more
shapes the parser accepts, the easier it is for *injected content in a tool result* to look
like a tool call. Olympus's `toolcall_repair.py` already carries the crown invariant
(refusal-safety: recovery only for offered tool names, so a refusal is never laundered into an
action) — that invariant must gate every new rung too. Repaired calls should also be visibly
marked (`id: repaired_*` already does this) so the approval spine and audit trail can treat
them with elevated suspicion; `security.should_wrap` continues to wrap results regardless of
how the call was parsed.

**7. Scalability implications.** Weak/local model support is exactly where repair earns its
keep — sovereign mode (`docs/SOVEREIGNTY.md`) forces the pool onto Ollama-class models whose
malformation rate is the highest. Repair quality directly determines how far down the
price/sovereignty ladder the council can run — the model-price-tier translation of Colibri's
"keep working at int4."

**8. Performance implications.** A recovered call saves a full failed agent iteration
(one provider round trip + re-prompt) — repair is one of the cheapest latency optimizations in
the system. Typed coercion is O(schema size), trivial.

**9. Maintainability.** `toolcall_repair.py` is already pure, no-I/O, exhaustively
unit-testable, and shared by `openai_compat.extract_json` — the correct foundation. What it
lacks vs Colibri: schema-typed coercion, budget-truncation (unclosed-tail) recovery, the
opt-in salvage tier, and any measurement.

**10. Olympus redesign.** Extend `toolcall_repair.py` — same file, same purity contract:

- **`coerce_arguments(args, schema) -> dict`** (adopt): given the tool's `input_schema`
  (already in every tool def per `_to_openai_tools`), coerce leaf types *toward the schema
  only* — numeric strings to numbers only where the schema says number, never the reverse;
  booleans from `"true"/"false"`; single values into declared arrays. Wired in
  `openai_compat.run_agent` right after `repair_arguments`, and in `bedrock_converse` /
  `backend.py`'s Anthropic loop, so all three provider paths share one decoder.
- **`close_truncated(text) -> str | None`** (adopt #401/#505): when a model's output ends
  inside a JSON object (out of `max_tokens`), close the tail **only when unambiguous** — a
  balanced-scan variant of the existing `_find_balanced_object` that completes solely
  dangling `}`/`"` with no content invention; ambiguity → `None`, caller's error path runs
  unchanged (the module's "repair can only add, never remove" contract).
- **Salvage tier** (adapt): `OLYMPUS_TOOL_SALVAGE=1` enables the aggressive rungs (key-name
  fuzzy match within `_NAME_KEYS`-distance-1, quote repair). Off by default; refusal-safety
  gate applies at every rung.
- **Repair telemetry** (Colibri's missing piece, and the measurement-culture requirement):
  every recovery increments a per-rung, per-model counter persisted beside usage
  (`MEMORY_DIR/repair_stats.json`, atomic write). `olympus scores` / `olympus models` surface
  repair rate per model; a rising rate is a model-health signal *and* Calibration Record feed
  ("model X needed argument repair on 14% of tool calls this month" is comparative evidence
  Asset 2 can use). **Golden-eval gate**: the repair corpus (every malformed shape seen in the
  wild, anonymized) becomes a fixture suite; a change to the ladder must keep 100% of prior
  recoveries recovering and 100% of prior refusals refusing — the token-exact-oracle principle
  applied to a parser.

**11. Final architecture.** Module: `toolcall_repair.py` (+~150 lines: `coerce_arguments`,
`close_truncated`, `salvage_enabled`, `record_repair`). Data:
`MEMORY_DIR/repair_stats.json`; `tests/fixtures/toolcall_corpus/*.json` golden suite. Env:
`OLYMPUS_TOOL_SALVAGE` (mirrors `COLI_TOOL_SALVAGE`). Integration: `openai_compat.run_agent`,
`bedrock_converse`, `backend.py`; approval spine sees `repaired_*` ids; Prometheus's audits can
read repair stats when proposing model or prompt changes.

**12. Why superior.** Colibri's ladder recovers silently; Olympus's recovers *measurably* —
every rung counted per model, gated by a golden corpus in CI, and feeding the comparative
evidence moat. Colibri's weakness (repair masking model decay) becomes the feature: repair
rate *is* the model-decay detector.

---

## 6. Streaming marker hold-back → the stream guard

**1. What Colibri does.** Tool-call markers (`<tool_call>` etc.) must not leak into user-visible
SSE content, but SSE chunk boundaries can split a marker across frames — so the server holds
back a suffix that could be a marker prefix until it can decide, then either suppresses the
marker or flushes the held text (§9.2; the CLI's streaming-markdown renderer does the same,
§13).

**2. Why it exists.** Chunked streaming makes every substring filter wrong by default: filtering
per-chunk misses split markers; buffering everything destroys streaming. Hold-back of the
*maximum ambiguous suffix* is the minimal correct compromise.

**3. How it works internally.** A state machine over the chunk stream keeps the longest tail
that is a proper prefix of any marker; decidable text is flushed immediately.

**4. Strengths.** Correct against arbitrary chunking; latency cost bounded by marker length
(bytes, not seconds); one reusable discipline applied at both server and CLI renderer.

**5. Weaknesses & trade-offs.** Marker set is hardcoded; only handles *markers* (Colibri never
needed to hold back semantic content like secrets — Olympus does).

**6. Security implications.** For Olympus this is bigger than cosmetics: a streamed answer
bypasses any *post-hoc* output filtering by definition — whatever left the socket left the
box. The egress guard (`egress.guard`, used by `gateway.notify_all` for BROADCAST) currently
protects whole payloads; streams need the same protection *incrementally*, or streaming
becomes the leak path that whole-message guarding quietly misses.

**7. Scalability implications.** O(1) state per stream; nothing.

**8. Performance implications.** Held-back bytes are bounded by the longest pattern; for
secret-shaped patterns (API-key prefixes like `sk-`) the hold-back window is tens of bytes —
imperceptible at token cadence.

**9. Maintainability.** One `streamguard` module used by every streaming path beats each
transport re-learning the split-marker bug (Colibri fixed it twice: server and CLI renderer —
proof that unshared, it recurs).

**10. Olympus redesign.** **New `olympus/streamguard.py`** — a pure incremental filter:

- `class StreamGuard: feed(chunk) -> str; flush() -> str` with two pattern classes:
  **internal markers** (any pipeline-internal scaffolding tokens that must never render —
  today Olympus streams only the final synthesized answer so the set is small, but the
  progress frames of §4 and any future tool-call passthrough on `/v1/*` make it real) and
  **secret shapes** (the same API-key/credential regexes the contribution anonymizer and
  `egress.py` already use): a streamed secret match is replaced with `[redacted]`, and the
  hold-back window is the max ambiguous prefix across both classes.
- Wired in `web.py` `_stream_reply` / `_stream_v1` and the Anthropic stream (one guard per
  response), and offered to the TUI renderer. `OLYMPUS_STREAM_GUARD=off` restores byte-identical
  streams (zero-behavior-change instrumentation doctrine).
- Golden tests adopt Colibri's adversarial style: every pattern split at every possible chunk
  boundary must produce identical output to the unchunked filter (the property, not examples).

**11. Final architecture.** Module: **`streamguard.py`** (new, ~120 lines, pure). Integration:
`web.py` streaming paths, `egress.py` pattern source (single pattern registry — no second
copy of the secret regexes), tests property-based over chunkings. Env: `OLYMPUS_STREAM_GUARD`.

**12. Why superior.** Colibri holds back markers for protocol hygiene; Olympus generalizes the
same automaton into the streaming half of its egress guarantee — the one place where
`docs/SOVEREIGNTY.md`-grade "refuse rather than leak" was not yet enforceable. Weakness
(hardcoded marker set) becomes a shared pattern registry with the anonymizer.

---

## 7. SEC-6/7/8 — gateway security hardening as a uniform doctrine

**1. What Colibri does.** SEC-6: binding off-loopback without an API key **fails closed**
(refuse to serve, never a silent open relay). SEC-7: a DNS-rebinding Host-header guard.
SEC-8: `/health` is always-200 liveness, but scheduler/kv/tier *detail* appears only when
authed; `/experts` authed; `/profile` ungated — a *noted inconsistency*. Plus: constant-time
key compares on both auth headers, CORS allowlist, 30 s socket timeout (Slowloris), 4 MiB
body / 1 MiB grammar caps (§9.2).

**2. Why it exists.** A local-first LLM server is one `--host 0.0.0.0` away from being an
open relay for someone's electricity and someone's model; SEC-6/7/8 make the *default*
posture safe and the unsafe posture require explicit keys.

**3. How it works internally.** Checks at the front of the request handler; the interesting
engineering is what is *not* trusted (headers for identity decisions) and the two-tier
liveness/detail split on `/health`.

**4. Strengths.** Fail-closed by default is the whole game. The liveness/detail split is
exactly right: monitors need 200s without credentials; topology detail is reconnaissance.
The *admitted* `/profile` inconsistency proves the audit was honest — and that hand-audits
miss things.

**5. Weaknesses & trade-offs.** Each protection is a hand-placed check; nothing *enforces*
that a new route gets classified (hence `/profile`). Host-guard and CORS lists are static
config. Auth is a single shared key — no per-caller identity, no revocation granularity.

**6. Security implications (for Olympus).** Olympus has already independently converged on —
and in places exceeded — SEC-6/7: `web.py`'s `/v1/*` loopback boundary is *header-independent*
(kernel peer address only) and closes the **reverse-proxy trap** Colibri never faced (a
forwarding header's *presence* denies when keyless — `docs/OPENAI_ENDPOINT.md`
§anti-spoofing); `a2a_server.py` and `mcp_server.py` are fail-closed by construction (no
token → refuse to serve; exposure flag makes auth mandatory). The gap is *uniformity*: the
rules live in three modules' prose, per-route body caps differ (`a2a_server._MAX_INBOUND`
1 MB vs web's), and — Colibri's exact lesson — nothing but discipline stops the next route
from being the ungated `/profile`.

**7. Scalability implications.** A shared-key model is fine single-operator; multi-tenant
serving (several `OLYMPUS_API_KEYS` callers with different budgets) will eventually want
per-key attribution — admission (§2) already keys tickets, so per-key quotas become a config
table, not a redesign.

**8. Performance implications.** Constant-time compares and header checks are nanoseconds;
the 30 s socket timeout and body caps are *positive* performance features (they bound the
damage of slow or huge requests).

**9. Maintainability.** The fix for "hand-placed checks drift" is the same fix Olympus applied
to capability counts: **generate the audit from the code and gate it in CI.**

**10. Olympus redesign.** *Absorb the principles (mostly built); mechanize the audit.*

- **New `olympus/authkit.py`** (small): the one place for constant-time bearer/x-api-key
  comparison, token-file loading (the `a2a_server._configured_token` shape, with its
  "unreadable file is an error, never silent no-auth" rule), and body-size guards — used by
  `web.py`, `a2a_server.py`, `mcp_server.py`, `webhook_gateway.py` instead of four private
  copies.
- **Route-auth manifest + CI drift gate** (the anti-`/profile` machine): every HTTP route in
  the codebase declares its class — `public` (liveness, agent card), `token` (`/v1/*`,
  `/a2a/task`), `local-only` — in a manifest that a test *generates from the actual route
  tables* and compares against the reviewed copy, exactly like the drift-gated capability
  counts in CI. A new unclassified route fails CI. This turns Colibri's honest footnote
  ("a noted inconsistency") into a class of bug that cannot ship.
- **Two-tier health everywhere** (adopt SEC-8): `olympus gateway --status` file and any
  `/health` route serve bare liveness unauthenticated; channel restart counts, queue depths,
  admission counters (§2) require the token.
- **Skip (with reason):** DNS-rebinding Host guard as Colibri built it — Olympus's `/v1/*`
  is bearer-gated whenever exposed and the loopback tier already ignores all headers, so a
  rebinding page gains nothing a missing key doesn't already deny; CORS allowlists remain the
  dashboard's concern (`OLYMPUS_ACCESS_TOKEN` on `/api/*`), documented rather than duplicated.

**11. Final architecture.** Modules: **`authkit.py`** (new, ~80 lines); `scripts/` or
`tests/test_route_manifest.py` (the generated-vs-reviewed gate); edits: the four server
modules import authkit. Data: `docs/ROUTES.md` or `olympus/routes.json` (generated manifest —
"truth lives at the route table," Colibri's `ENVIRONMENT.md` generation doctrine, §24). Env:
none new. Integration: threat model doc (`docs/THREAT_MODEL.md`) gains the manifest as its
enforcement arm.

**12. Why superior.** Colibri's posture was excellent and hand-maintained; one route slipped.
Olympus keeps the posture (already independently at or above SEC-6/7) and replaces the hand
audit with the same generate-and-gate machinery it already trusts for capability counts —
the honesty culture, mechanized.

---

## 8. The line-protocol mux and its forward-compat violations → contract discipline

**1. What Colibri does.** The engine's mux mode speaks a stdin/stdout line protocol:
`SUBMIT id slot bytes max_tokens temp top_p [gbytes]` + payload, `CANCEL`, and typed reply
frames (`DATA`/`PROF`/`DONE`/`ERROR(BAD_REQUEST|…|CANCELLED)`); the optional 7th `SUBMIT`
field gives additive forward/backward wire compatibility (§9.1, §26.10). But §27 records two
self-inflicted wounds: the Python dispatcher **violates the documented ignore-unknown-lines
rule** (a new engine telemetry line can break the old dispatcher), and `serve_protocol.md`
**documents frames the server doesn't emit** — with the fig-leaf "if this document and the
code disagree, the code wins."

**2. Why it exists.** HTTP lives in Python, generation lives in C; a newline protocol over
stdio is the simplest possible seam, and the additive-field rule was supposed to let either
side evolve independently.

**3. How it works internally.** Typed one-line frames, error taxonomy as first-class frame
variants, telemetry lines interleaved on the same stdout (`HWINFO`, `EMAP`, `STAT`, …) that
consumers are *supposed* to skip when unrecognized.

**4. Strengths.** The error taxonomy (`SLOT_BUSY`, `DUPLICATE_ID`, `CONTEXT_EXCEEDED`,
`CANCELLED`…) is a real contract — clients can program against failure modes. The additive
optional field is the right evolution rule. Loud `CONTEXT_EXCEEDED` replacing silent
truncation (#401/#506) is a doctrine Olympus should tattoo somewhere.

**5. Weaknesses & trade-offs.** The protocol has **three sources of truth** (C emitter, Python
consumer, markdown spec) and no conformance test tying them together — so all three drifted,
*in a project that token-exact-oracles its matmuls*. The lesson is sharp: contracts without
executable checks decay even in the most measurement-obsessed culture; prose specs with a
"code wins" disclaimer are confessions, not contracts.

**6. Security implications.** A consumer that crashes on unknown lines is a availability bug
today and a parser-differential bug tomorrow (two consumers disagreeing on frame boundaries is
how injection classics start). Olympus's equivalents — the A2A task envelope, MCP JSON-RPC
frames, `federation` requests, the webhook channel, the gateway status file schema — are
inter-*trust-domain* surfaces, where drift isn't an oops but an exploitable ambiguity.

**7. Scalability implications.** Protocol drift taxes every new consumer superlinearly: each
integration must reverse-engineer actual behavior (Colibri's own docs admit this). Conformance
fixtures make the Nth consumer as cheap as the 2nd.

**8. Performance implications.** None at runtime; the cost is all in CI seconds — bounded and
budgeted per the gate-cost rule.

**9. Maintainability.** This is the domain's purest maintainability capability. Olympus
already runs the solution pattern for one artifact class (drift-gated capability counts:
"the numbers can't drift from what's actually built," README). Extend it from *counts* to
*wire shapes*.

**10. Olympus redesign.** *Absorb the lesson as machinery; skip the protocol itself* (Olympus
has no C engine seam; sovereignty-mode local engines are reached over standard OpenAI-compat
HTTP, deliberately — no bespoke line protocols).

- **Contract fixtures for every cross-trust-domain surface:** for A2A
  (`a2a.parse_task`/`task_response`), MCP (`mcp_server` initialize/tools/call frames),
  federation, the webhook channel, and the `/v1/*` + `/v1/messages` dialects — a directory of
  golden request/response JSON fixtures exercised by pure `handle_request`-style entry points
  (Olympus's servers are *already* pure-and-injectable precisely to allow this — the design
  anticipated the need; this fills it).
- **Tolerant-reader rule, enforced:** every consumer of a foreign or versioned payload must
  pass an "unknown-field / unknown-frame" test — fixtures with injected novel fields/lines
  that must be *ignored, not fatal* (the exact rule Colibri wrote down and then violated).
  A tiny helper in **`olympus/protocontract.py`** (`tolerant(dict, known_keys)` + fixture
  loader) keeps the tests one-liners.
- **Docs generated or gated:** `docs/OPENAI_ENDPOINT.md`'s endpoint list and error codes get
  a drift test against the route manifest of §7 (same generated-vs-reviewed shape). No prose
  spec ships with a "code wins" disclaimer; if the code wins, the doc is generated from it
  (Colibri's own `ENVIRONMENT.md` generation practice, §24 — they had the cure in-tree and
  didn't apply it to the protocol).
- **Adopt the error-taxonomy doctrine:** the admission/cancel errors of §§2–3 land as a typed,
  documented set (`queue_full`, `queue_timeout`, `cancelled`, `context_exceeded`,
  `budget_exhausted`) with identical names across HTTP JSON, A2A, and MCP — one taxonomy,
  three renderings, one fixture suite.

**11. Final architecture.** Module: **`protocontract.py`** (new, ~60 lines) +
`tests/fixtures/contracts/{a2a,mcp,v1,messages,webhook}/*.json`; CI: one pytest module,
seconds of wall clock. Integration: every new serving surface (e.g. `anthropic_server.py`
from §1) must land with its fixture directory — enforced by the same review rule that
requires benchmarks for prompt changes.

**12. Why superior.** Colibri wrote the right rules and trusted humans to follow them;
Olympus makes the rules executable, using a pattern it has already proven on capability
counts. The three-sources-of-truth failure becomes structurally impossible: the fixtures
*are* the spec, and the docs either generate from code or fail CI.

---

## Open questions & research spikes

1. **True mid-call cancellation of provider HTTP requests** (bounded spike, ~3 days):
   `openai_compat._post` / the Anthropic client use blocking `urllib` with a 600 s timeout;
   cancellation (§3) currently takes effect only at call boundaries. Evaluate switching to
   `http.client` with an abortable socket (shutdown from the cancel thread) while keeping the
   stdlib-only footprint, and *measure* mean cancel-to-stop latency before/after on a golden
   scenario set. If the win is <2 s median on real council runs, keep boundary-cancel and
   record the negative result in `DEFERRED.md` (the EXPERT_BUDGET eulogy discipline, §26.4).
2. **Queue-position estimates in shed/queue messages**: "you're #3, ~40 s" needs a per-priority
   service-time estimator. Start with a trailing median from the admission JSONL (data exists
   from day one by design); decide after 2 weeks of recorded queue events whether the estimate
   is honest enough to show users (measurement-first: never show an unvalidated ETA).
3. **Per-key quotas on `/v1/*`**: admission tickets are per-user; `OLYMPUS_API_KEYS` callers
   are currently one anonymous tenant. Small design question — key-suffix as user id? — with
   privacy implications (keys in ledgers must be masked with `config.mask_key`). Decide when a
   second external API consumer actually exists (small-team scope rule; no speculative
   multi-tenancy).
4. **Tool passthrough on the inbound `/v1/*` surface** (`tools` accepted-but-ignored today):
   serving council tool-calls to API clients means exposing actuation across a trust boundary —
   it must compose with the approval spine (held calls cannot block an HTTP response forever).
   Likely shape: `finish_reason: "tool_calls"` only for read-only tools, 400 for actuating
   ones, but this needs its own design doc and is explicitly out of scope here.
5. **Progress-frame privacy audit**: confirm stage/specialist names leak nothing per-user
   (they shouldn't — they're static registry names from `specialists.py`), and add the check
   to the streamguard tests so a future `detail` field carrying tool arguments gets caught.
