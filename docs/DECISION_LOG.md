# The Re-Executable Decision Log

Olympus records every run so that its **reasoning can be re-executed**, not just
inspected. For each run we freeze the exact LLM request/response pairs and pair
them with the orchestration's structured decisions. Later we re-run the *real
code* against those frozen responses and prove the decision path is
byte-identical — or pinpoint the exact request where a code or prompt change
would have altered a decision.

This is a regression test for *reasoning*. State-replay systems (e.g. Ruflo's
`StateReconstructor`) replay recorded **state**: they never store the model
request/response, so they cannot re-run the logic to detect that a code or
prompt change would have changed an outcome. We replay the reasoning itself.

## The pieces

| Module | Role |
| --- | --- |
| `olympus/replaystore.py` | Content-addressed store of frozen LLM requests/responses. |
| `olympus/llm.py` | The single LLM choke point; records in normal mode, returns frozen responses in replay mode. |
| `olympus/trace.py` | Per-run structured **decision** records + replay/diff helpers. |
| `olympus/orchestrator.py` | Records a decision at each stage (route/plan/review); `replay_run()` re-executes a run. |

### What gets frozen

`llm.complete()` is the one place every orchestration LLM call goes through
(directly and via `backend.complete_json` / `backend.complete_text`). On each
call it computes a content hash of the request and:

- **Record mode (default):** after the streamed response completes, it writes
  the response to `MEMORY_DIR/responses/<hash>.json`
  (`anthropic.types.Message.to_json()`).
- **Replay mode (`OLYMPUS_REPLAY=<run_id>`):** it returns the stored response
  for that hash **with no network call**. If the hash isn't on disk, the
  orchestration produced a *different* request than the recorded run, so it
  raises `ReplayDivergence` naming that exact request.

### The request hash — and why `container` is excluded

`replaystore.canonical_request()` serializes the request as canonical JSON
(keys sorted recursively, compact separators) so structurally-equal requests
serialize — and therefore hash — identically.

**One field is deliberately excluded from the hash: `container`.** The
Anthropic API allocates a server-side `container` id per request for web
search's code-execution sandbox; it changes on every run. If it were hashed,
no replay would ever match the recorded run. Everything else — `system`,
`messages`, `model`, `tools`, `mcp_servers`, `thinking`, `output_config` — *is*
part of the decision and **should** change the hash if it changes, because a
change there genuinely is a different decision. The exclusion list lives in
`replaystore._EXCLUDE_FROM_HASH`.

### Decision records

Alongside the frozen calls, `Trace.decision()` records one structured record per
orchestration decision (route, plan, review). Each record pairs the verbatim
`_route` / `_plan` / `_review` return (the *rationale*) with the
content-addressed reference to the LLM call that produced it
(`model_request_hash` / `model_response_ref`), plus the agent/model, parent
record, cost, outcome, and timing.

`trace.canonical_log()` reduces a run's decisions to their replay-invariant
*cores*, dropping fields that legitimately differ between a run and its replay
(`record_id`, `run_id`, `parent_record_id`, `duration_ms`, `cost`, `ts` — see
`trace._VOLATILE`). Two runs with the same canonical log took the same
reasoning path.

## Using it

```bash
# Re-execute a recorded run against its frozen responses and prove the path:
olympus replay <run_id>

# Inspect the decision path of a run, or a single decision record by id:
olympus explain <run_id>
olympus explain <record_id>
```

`orchestrator.replay_run(run_id)` is the programmatic entry point: it loads the
recorded run, sets `OLYMPUS_REPLAY`, re-runs `_pipeline` against the frozen
responses, and returns `(original, fresh_trace, diffs)`. An empty `diffs` means
the reasoning replayed byte-identically; a non-empty diff (or a raised
`ReplayDivergence`) pinpoints where a code/prompt change altered a decision.

## Scope boundary: `stream_text` is intentionally NOT replayed

Olympus has two LLM surfaces:

- **`llm.complete()`** — every *decision* the orchestrator makes (routing,
  planning, verification, review, specialist tool-use). These are recorded and
  replayed. This is the decision log.
- **`llm.stream_text()`** — the final user-facing answer, streamed token by
  token to the chat UI. This is **deliberately outside** the decision log: it
  is the *presentation* of a decision already made, not a decision itself, and
  streaming has no single final-request choke point to freeze cleanly. It does
  not call into the replay store, so it never records or replays.

In other words: the log proves the *reasoning path* (what Olympus decided and
why) is reproducible. It does not pin the exact prose of the final reply, which
is non-deterministic streamed text and not part of the decision.

## Retention

Frozen responses are content-addressed, not dated, so they are pruned by
reachability rather than age. The heartbeat's maintenance pass runs
`memory.sweep_dated_files()` (drops old dated traces/usage) and then
`memory.sweep_orphan_responses()`, which removes any frozen response no
surviving trace still references. A recorded run therefore stays fully
re-executable for its entire retained life, and storage stays bounded.
