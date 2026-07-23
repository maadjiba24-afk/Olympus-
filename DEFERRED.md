# Deferred — known limitations, deliberately not fixed yet

Every item here was surfaced by an audit and consciously deferred with a
reason. Deferral is allowed; silent deferral is not (ADR 0005 hardening
addendum). Revisit any of these by opening an issue that quotes the line.

| # | Limitation | Why deferred |
|---|---|---|
| 1 | Skill admission/retirement gates are scored by an LLM judge (only Hephaestus has objective test-execution scoring) | Objective benchmarks per specialist domain are a research project; the strict `>` improvement bar plus regression-gated removal bounds the damage of judge noise. |
| 4 | Athena's quality review stays **fail-open** (a nudge, not a gate) | The one-shot half is closed: when Athena orders a rework, the reworked output is now RE-REVIEWED once (bounded — never a third pass), and because that second review runs ONLY on the minority of turns that actually reworked, the common approve-first path still pays for a single review (the cost the deferral warned about is avoided). Athena remaining fail-open is deliberate — Aletheia is the enforcing gate (reject → rework → UNVERIFIED banner); Athena is the quality nudge and must never block a reply on its own infra failure. |
| 5 | Effort tiers are a no-op on the **claude-code** backend only (the OpenAI-compatible path now maps them) | The OpenAI-compatible backend now maps `low/medium/high` to `reasoning_effort` for the reasoning families that accept it (OpenAI o-series/gpt-5, Gemini 2.5 / thinking), allowlist-gated so non-reasoning models are never sent an unknown param (and reasoning models now correctly use `max_completion_tokens`). The `claude-code` backend stays a no-op deliberately — it delegates to the `claude` CLI's own subscription defaults and exposes no per-call reasoning knob. |
| 10 | proclock degrades to single-process locking on Windows (no fcntl) | The heartbeat-vs-web multi-process topology is documented as unsupported on Windows (ADR 0005 decision b); the degradation warns once and keeps thread safety. |
| 11 | No Daytona (or other vendor) remote-sandbox backend | `sandbox.run` already has `local` + `docker` backends; docker (`--network none`, memory/PID capped) covers the isolation need. A Daytona backend adds an external SDK, credentials, and an egress path for marginal benefit — deferred until a concrete remote-execution requirement appears. Recipe: a `daytona` branch in `sandbox.run` submitting the same (still `cmdguard`-checked) command to the remote workspace API. |
| 12 | No native A2A (agent-to-agent) server | `mcp_server.py` already exposes the council inbound over JSON-RPC (stdio) to Claude Desktop / IDEs, which covers the practical "let another agent call Olympus" need; A2A is experimental in OpenManus itself. Recipe when needed: an `a2a_server.py` mirroring `mcp_server`'s TOOLS/handle_message shape over the A2A wire format, funnelling every side-effecting request through the approval spine (never a direct actuation surface). |
| 13 | Bedrock supports only Claude models (via `anthropic.AnthropicBedrock`), not the native converse API for Titan/Llama/Mistral | Claude-on-Bedrock delivers full capability parity and is nearly free (the Anthropic SDK is API-compatible). A native SigV4 `converse` client for non-Claude Bedrock models would add a boto3 dependency + signer for models already reachable through `openai_compat` (Groq/OpenRouter/Together/Ollama). |
| 14 | Sandbox execution is one-shot (`run_command`/`run_python`), not a persistent interactive shell session | OpenManus keeps a bash session alive across calls; Olympus deliberately runs each command through a fresh confined, approval-gated `sandbox.run` so state can't accumulate outside the reviewed path. A persistent shell would complicate the confinement + per-call approval story for little gain (a script can be written once and run once). |
| 16 | The Aegis Assessment suite (ADR 0011) has **no active-exploitation** phase — no payload spraying, brute force, or `agent-browser`-style in-page exploitation like Strix's | Deliberate, not a gap: Olympus's charter is defensive, evidence-producing assessment of the operator's own assets. The absorbed capabilities observe and analyze (recon, header/config audit, SAST, secret + dependency scanning); confirming a finding that needs active exploitation is described as safe reproduction steps, not performed. Adding an exploitation actuator would reintroduce exactly the risk surface (open egress + suppressed judgment) that Strix's design shows is dangerous. |
| 17 | `assess_deps` checks a **bundled, static advisory index**, not a live CVE feed (OSV/GitHub Advisories) | Offline and deterministic keeps the three-dep footprint and makes findings replay-stable. An operator can extend the index via `OLYMPUS_ASSESS_ADVISORIES` (a workspace-confined JSON file, same shape). Recipe when a live feed is wanted: a gated OSV batch-query behind `tools._http_get` (POST support) under the same egress gate, cached, with the static index as the offline fallback. |
| 18 | No Strix-style Dockerized scanner sandbox (nmap/nuclei/ffuf/sqlmap) or Caido-grade full HTTP capture/replay proxy | The native scanners are pure-Python and non-intrusive by design; shelling out to raw-socket scanners needs NET_RAW caps + an open-egress container (Strix's highest-risk surface). Recipe when a real engagement needs them: route each scanner invocation through the existing `sandbox.run` (already `cmdguard`-checked, `--network none` by default) with egress opened only to the code-enforced in-scope targets, and log every request/response into the signed ledger — the capture-proxy value without the open-box risk. The 25-file offensive skills library is likewise deferred (methodology folded into the Aegis prompt for now). |

> Closed: **#2** (write-time semantic skill dedup — `skills.near_duplicates()`
> now flags near-duplicates at `create()` time, embedding-based and best-effort),
> **#3** (skill retrieval's prompt index is now *replaceable* by embedding search
> in the live specialist prompt — `OLYMPUS_SEMANTIC_SKILLS` scopes a specialist's
> in-prompt skill index to the top-K most relevant to the task via
> `skills.scoped_index()`; opt-in, engages only once a library outgrows the
> prompt, replay-frozen per specialist, and degrades to the full index so no
> skill becomes unreachable), **#6** (direct-reply verification — M4), **#7**
> (router opt-out now a ledgered exemption — M4), **#8** (per-worker workspace
> roots — M1), **#9** (machine-global model-call cap — M3), **#15** (code-graph
> qualified-call precision — a QUALIFIED Python call is now pinned to the module
> its qualifier names via the import-alias map, emitting a precise EXTRACTED edge,
> so `impact` stops under-reporting and `verify` answers CONFIRMED/REFUTED instead
> of only UNKNOWN for such names). Numbers are stable IDs; the gaps are
> intentional so existing references stay valid.
