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
| 6 | Zeus's direct replies and clarify turns are unverified | They make no factual delegation — verification would fact-check small talk; the router escalates factual asks to the (fully gated) delegate path. |
| 7 | The router's `needs_verification=False` opt-out bypasses the verification chain | The router IS the easy/hard dial (ADR 0005 decision c uses its judgment as a scorer input); removing the opt-out makes every trivial turn pay the full verify cost. |
| 8 | Two specialists sharing one workspace root can still clobber the same explicitly-named file | **Partially resolved (2026-07-19).** The safe design is now built: `prepare()` pins the workspace root it resolved at preview time into the action (`_pinned_root`), and the file/command executors confine to THAT root (`sandbox._effective_root`), so an approved action always runs where it was previewed — the hazard that got per-worker re-rooting rejected (preview root ≠ execution root, ADR 0005 amendment 1) is closed. This *unblocks* safe per-worker roots (each pinned to its own dir), which would fully close the same-name clobber; introducing them is the remaining step. |
| 9 | The concurrent-model-call cap is per-process, not machine-global | The heartbeat + web processes can jointly run 2× MAX_CONCURRENT_CALLS; a cross-process semaphore adds flock traffic to every model call for a soft limit the daily budget already bounds in dollars. |
| 10 | proclock degrades to single-process locking on Windows (no fcntl) | The heartbeat-vs-web multi-process topology is documented as unsupported on Windows (ADR 0005 decision b); the degradation warns once and keeps thread safety. |
