# ADR 0010: Native web-context capabilities (Firecrawl absorption)

Status: accepted
Date: 2026-07-22

## Context

A full inventory and security review of [Firecrawl](https://github.com/firecrawl/firecrawl)
(a large TS/Go/Rust web-scraping SaaS — see `docs/FIRECRAWL_TRACKING.md`) found a
capable feature surface — scrape, map, crawl, batch, schema-guided extraction,
llms.txt, change-diffing, document parsing, change-monitoring — sitting on a
security model Olympus already beats: SSRF resting on a single fail-open socket
hook, input-layer URL rejection commented out, a DNS-rebinding window on the
browser path, one misleadingly-named flag that disables all SSRF globally,
prompt-injection "defense" that is a soft string on one code path, unverified
LLM extraction, an open-by-default self-host, and a removed audit trail.

This ADR records how Olympus absorbs the *capabilities* natively — in its own
idioms and safety spine — and turns each of those weaknesses into a structural
strength, rather than porting a foreign subsystem or its anti-patterns. It
follows the shared contract of ADR 0008 (opt-in, replay-safe, security-spine
reuse, own tests, explicit "NOT absorbed" list).

## Decision (a): one gated seam, reused everywhere

Every outbound fetch in the new `webctx` module — page, sitemap, robots.txt,
crawl hop, document, diff, monitor check — goes through `tools._http_get` (text)
or the new `tools._http_get_bytes` (binary). Both share the exact SSRF/egress
preamble: `security.secret_exfil_reason` → `security.url_block_reason` →
`_pinned_opener()` (which pins the validated IP into the socket, defeating DNS
rebinding) or `_proxy_opener()` (the egress choke). There is **no second socket
path** in the module. This is strictly stronger than Firecrawl's fail-open
undici hook and closes the rebinding window its browser path leaves open.

## Decision (b): untrusted content is isolated structurally

Any scraped bytes that reach a model prompt are wrapped in
`security.wrap_untrusted` (fail-closed via `security.should_wrap`) — in the
extraction prompt, the verification prompt, and the summary prompt alike, per
`research.py`'s pattern. The fetching tools (the six new `web_*`/`parse_document`
ingesters plus the already-classified `browse_page`/`crawl_site`) are registered
in `security.INGESTION_TOOLS`, so any agent run that can invoke them loses its
action tools (`security.filter_tools`): a prompt-injected page can never reach
an actuator. This replaces Firecrawl's soft "ignore embedded directives" string
(present on one path, absent on others) with an envelope the model is told to
treat as adversarial data, on every path.

## Decision (c): extraction is verified

`webctx.extract` runs schema-guided extraction on the pool's cheap `general`
member, then a second `verify` member re-reads the extracted values against the
(wrapped) source and flags anything unsupported. The tool surfaces
`verified: true/false/None` and per-field flags. Firecrawl ships no fact-check;
this is the single clearest capability advantage, and it reuses the exact
composition-by-role the council already runs.

## Decision (d): opt-in autonomy, replay-safe by construction

The demand-invoked tools (`web_map`, `web_batch_scrape`, `web_extract`,
`generate_llmstxt`, `parse_document`, `web_diff`, and the upgraded
`browse_page`/`crawl_site`) are ordinary client-side tools: `agent.py` freezes
their results, so
replay is deterministic without any in-handler `OLYMPUS_REPLAY` guard. All
fetching is **synchronous** within the handler (no threads/background), and
crawl/map are bounded by **count and depth** constants — never wall-clock — so a
frozen result is stable. `to_markdown` is pure and deterministic (no rng/clock,
sorted/ordered output). The only autonomous behavior, the change-monitor
(`webmonitor.run_due`), is a heartbeat job gated `OLYMPUS_WEB_MONITOR` (default
OFF) and forced inert under `OLYMPUS_REPLAY` — off the council replay hot path
entirely.

## Decision (e): capability surface & parity mapping

New tools (8) and CLI commands (6) are registered through the normal seams
(`tools.HANDLERS` + `tools.EXTRA_TOOLS`, `cli` subparsers), counted by
`capabilities.py`, threat-modeled in `docs/THREAT_MODEL.md`, and bound to the
README markers — the truthful-accounting gate keeps holding.

Firecrawl's full surface is covered either by a new native capability or by an
existing Olympus one; nothing is left as a silent gap:

| Firecrawl capability | Olympus |
|---|---|
| scrape → clean markdown + links | existing `browse_page`, **upgraded** to `webctx.to_markdown` (readability-grade, zero-dep) |
| map (URL discovery) | `web_map` (new) |
| crawl | existing `crawl_site`, **upgraded** to `webctx.crawl` clean markdown + new `include`/`exclude` glob filters |
| batch scrape | `web_batch_scrape` (new) |
| extract (schema) | `web_extract` (new) — **verified**, which Firecrawl is not |
| llms.txt | `generate_llmstxt` (new) |
| change-tracking (git-diff mode) | `web_diff` (new) + `web_monitor_*` (new) |
| change-tracking (json mode) | composition: `web_extract` on a schedule + `web_diff` on the structured result (no new engine) |
| parse (PDF/DOCX) | `parse_document` (new, optional `[docs]` extra, path-confined) |
| search (+scrape) | existing `web_search`/`websearch.py` + `browse_page` |
| deep-research | existing `research.py` / `trigger_research` |
| screenshot | existing governed browser harness `browser_screenshot` |
| browser sessions + actions (incl. executeJavascript) | existing governed `browser.py` — `browser_act` is an ACTION_TOOL stripped from untrusted-ingesting runs, every CDP call ledgered/replayable. Olympus's answer to Firecrawl's ungoverned `executeJavascript` |
| agent ("describe what you need") | existing browser harness + `spawn_subagent` |
| monitor (scheduled) | `webmonitor` (new) |

**No duplicate surface.** Rather than ship a second scrape/crawl tool, the
content-quality win (`webctx.to_markdown`) is landed on the *existing*
`browse_page`/`crawl_site` — already classified in `INGESTION_TOOLS`, already
threat-modeled and tested — so the model keeps one scrape entry point and one
crawl entry point. The eight genuinely-new verbs use the `web_*` prefix that
matches the existing `web_search`/`web_fetch` family (with `generate_llmstxt`,
`parse_document` following the `verb_noun` convention of `crawl_site`).

## Consequences

Two small, dependency-free modules land (`webctx.py`, `webmonitor.py`) plus one
canonical byte-fetch seam, composed through named seams rather than replacing
anything. Each ships its own test module. Honest framing of the moat: Olympus
does **not** claim to out-quality Firecrawl's Go+Rust markdown converter — it
claims (and tests) that its readability-grade markdown is *zero-dependency*, its
every fetch is *IP-pinned-gated*, its every model hop is *untrusted-wrapped*, and
its extraction is *verified* — advantages Firecrawl's architecture cannot cheaply
match.

What was deliberately **NOT absorbed**: Firecrawl's network-only SSRF model (we
reject at the pinned socket, not a fail-open hook), its ungated in-browser JS
execution (actuation stays behind the governed harness's capability separation),
its open-by-default posture (new tools are inert until invoked; the monitor is
opt-in), its unverified extraction, its dual-queue/FoundationDB operational
complexity, and its polyglot FFI/native build surface (the whole suite is
pure-stdlib; PDF/DOCX is an optional extra imported lazily inside the parser, so
the three-required-dependency footprint is unchanged).

## Addendum — self-evolution extensions (new territory)

Three extensions deepen the moat along ADR 0009's self-evolving axis. All obey
the absorbed-capability contract (ADR 0008): opt-in for autonomous behavior,
replay-inert, bounded, corrupt-quarantining, additive-only, own tests.

1. **Learned action-profiles.** `domainlore` now records the *safe action
   profile* that beat a domain's byte baseline, not just that interaction
   helped. `scrape(actions=None)` with `OLYMPUS_WEB_AUTO_ACTIONS=1` replays that
   earned profile through the governed harness; every step is re-validated
   against the safe-verb allowlist, and an explicit `actions=` always wins.

2. **Operator-gated fleet lore sharing.** `federation` gains
   `export_domainlore`/`import_domainlore` and a `/federation/domainlore` route,
   mirroring the lesson-sync guarantees: signed envelopes, `trusted`-only,
   secrets/PII scrubbed. Imported facts STAGE in the corpus's staging area and
   are folded into the live corpus only by the operator's explicit
   `merge_staged` — purely additive, never overwriting local truth, never
   relaxing a fetch gate.

3. **Auto-drafted build proposals.** `webproposals` turns a proposal-kind
   discovery into a full, evidence-backed build proposal (motivation, evidence,
   concrete change, safety posture, acceptance criteria) queued for the
   operator to accept / decline / mark built. It is explicitly NOT a code
   generator — it produces a reviewable engineering artifact, never a behavior
   change.

Why still safe: none of the three can open a fetch the gate would refuse. Auto
actions run only governed safe verbs behind an opt-in flag; shared lore is a
staged hint a human merges; a proposal is text a human acts on. The moat gets a
data network effect (per-domain profiles that compound and now travel a fleet)
and a discovery flywheel (the system drafts its own roadmap) without widening
the trust surface.
