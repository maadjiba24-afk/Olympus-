# Deferred — known limitations, deliberately not fixed yet

Every item here was surfaced by an audit and consciously deferred with a
reason. Deferral is allowed; silent deferral is not (ADR 0005 hardening
addendum). Revisit any of these by opening an issue that quotes the line.

| # | Limitation | Why deferred |
|---|---|---|
| 1 | Skill admission/retirement gates are scored by an LLM judge (only Hephaestus has objective test-execution scoring) | Objective benchmarks per specialist domain are a research project; the strict `>` improvement bar plus regression-gated removal bounds the damage of judge noise. |
| 2 | No semantic dedup at skill write time | Consolidation runs on the curator's cadence instead; write-time embedding comparison would add a model call to every skill save for a rare collision. |
| 3 | Skill retrieval is a prompt index the model must notice, not embedding search | The per-specialist index is small enough to fit in prompt today; embedding retrieval becomes worthwhile only when libraries outgrow it. |
| 4 | Athena's quality review is one-shot and fail-open | A review loop multiplies latency and cost on every turn; the enforcing Aletheia gate (reject → rework → banner) now backstops correctness, leaving Athena as a quality nudge. |
| 5 | Effort tiers are a documented no-op on OpenAI-compatible and claude-code backends | Generic /chat/completions endpoints reject unknown params; mapping per-provider reasoning knobs is provider-matrix work, marked deliberate in those modules. |
| 10 | proclock degrades to single-process locking on Windows (no fcntl) | The heartbeat-vs-web multi-process topology is documented as unsupported on Windows (ADR 0005 decision b); the degradation warns once and keeps thread safety. |
| 11 | No Daytona (or other vendor) remote-sandbox backend | `sandbox.run` already has `local` + `docker` backends; docker (`--network none`, memory/PID capped) covers the isolation need. A Daytona backend adds an external SDK, credentials, and an egress path for marginal benefit — deferred until a concrete remote-execution requirement appears. Recipe: a `daytona` branch in `sandbox.run` submitting the same (still `cmdguard`-checked) command to the remote workspace API. |
| 12 | No native A2A (agent-to-agent) server | `mcp_server.py` already exposes the council inbound over JSON-RPC (stdio) to Claude Desktop / IDEs, which covers the practical "let another agent call Olympus" need; A2A is experimental in OpenManus itself. Recipe when needed: an `a2a_server.py` mirroring `mcp_server`'s TOOLS/handle_message shape over the A2A wire format, funnelling every side-effecting request through the approval spine (never a direct actuation surface). |
| 13 | Bedrock supports only Claude models (via `anthropic.AnthropicBedrock`), not the native converse API for Titan/Llama/Mistral | Claude-on-Bedrock delivers full capability parity and is nearly free (the Anthropic SDK is API-compatible). A native SigV4 `converse` client for non-Claude Bedrock models would add a boto3 dependency + signer for models already reachable through `openai_compat` (Groq/OpenRouter/Together/Ollama). |
| 14 | Sandbox execution is one-shot (`run_command`/`run_python`), not a persistent interactive shell session | OpenManus keeps a bash session alive across calls; Olympus deliberately runs each command through a fresh confined, approval-gated `sandbox.run` so state can't accumulate outside the reviewed path. A persistent shell would complicate the confinement + per-call approval story for little gain (a script can be written once and run once). |

> Closed: **#6** (direct-reply verification — M4), **#7** (router opt-out now a
> ledgered exemption — M4), **#8** (per-worker workspace roots — M1), **#9**
> (machine-global model-call cap — M3). Numbers are stable IDs; the gaps are
> intentional so existing references stay valid.
