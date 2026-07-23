# Changelog

All notable changes to Olympus are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Olympus adheres to [Semantic Versioning](https://semver.org/). The single
source of truth for the current version is `pyproject.toml`; see
[docs/SUPPORT.md](docs/SUPPORT.md) for the versioning, LTS, and release-integrity
policy, and [RELEASING.md](RELEASING.md) for how a release is cut.

Categories: **Added** (new capabilities), **Changed** (behaviour changes),
**Fixed** (bug fixes), **Security** (hardening). A `MAJOR` bump that changes the
CLI surface, the public Python API, or the on-disk memory `schema_version`
carries a migration note here.

## [Unreleased]

### Added — Absorb Page Agent's capabilities natively (ADR 0014, decision (a))

Begins absorbing [alibaba/page-agent](https://github.com/alibaba/page-agent)'s
capability surface as native Olympus features, each built on the security spine
with the corresponding page-agent weakness inverted into a structural strength
(analysis in `docs/PAGE_AGENT_TRACKING.md`, design in ADR 0014).

- **Refusal-safe tool-call repair** (§3.1). New pure module
  `olympus/toolcall_repair.py` absorbs page-agent's `autoFixer` malformed-tool-call
  salvage — double-encoded arguments, ```json-fenced / prose-wrapped objects, and
  a tool call emitted as JSON in `content` with an empty `tool_calls` array — for
  the weak-model backends (`openai_compat`: Ollama/vLLM/LM Studio/Mistral/DeepSeek/
  OpenRouter/Gemini; `bedrock_converse`: Titan/Nova/Llama/Mistral/Cohere). Wired
  into `openai_compat.run_agent`; a shared brace-balanced JSON extractor now backs
  `openai_compat.extract_json` and `bedrock_converse.complete_json` too (ad-hoc
  regexes removed). **The inversion:** page-agent's fixer will reconstruct a tool
  call from *any* content JSON — which can mask a refusal or fake an action from a
  final answer. `recover_tool_call` fires **only when the content names a tool the
  model was actually offered**, so a refusal or plain answer is returned untouched
  as text, never laundered into an action. Pure module, no new dependency; 27
  tests (`tests/test_toolcall_repair.py`) incl. two `run_agent` integration cases.

Deliberately still declined (ADR 0014 "NOT absorbed"): running the agent *as the
page* (origin sharing), `eval()` of model code in a live origin, an
unauthenticated localhost control socket behind a reusable dialog, and sending
unredacted page HTML to the model behind only an opt-in hook.

### Docs — Competitive analysis: alibaba/page-agent (analysis-only)

Added `docs/PAGE_AGENT_TRACKING.md`, a complete feature/capability inventory and
security/design critique of [alibaba/page-agent](https://github.com/alibaba/page-agent)
(the zero-install in-page GUI agent, npm 1.12.2), mapped to Olympus's own
browser harness and web-context suite. Verdict: **nothing adopted** — Page
Agent's in-page DOM-automation surface is already matched or exceeded by
`olympus/browser.py` (numbered element map, index/selector/AX targeting, shadow
+ cross-origin iframe traversal, self-healing selectors, provenance-scored
skills), while its distribution model (page-origin sharing, `eval()` JS tool,
unauthenticated localhost MCP socket behind a reusable `window.confirm()`,
unredacted page HTML to a third-party LLM) is a deliberate Olympus non-goal. A
short watchlist of ergonomic/robustness ideas (LLM malformed-response
auto-repair, "new element since last step" delta, geometry-aware scroll hints,
human-click hit-testing) is recorded but unbuilt.

### Added — Three deferred capabilities built native (DEFERRED #12/#13/#11)

Closes three `DEFERRED.md` items as first-class, tested, hardened capabilities.
Each is opt-in where it touches the network, egress-gated, fail-closed, and
injectable-tested; no change to the three required dependencies.

- **Native inbound A2A server** (#12). `a2a_server.py` serves the public agent
  card and a bearer-authenticated, fail-closed `POST /a2a/task` that funnels the
  task text (wrapped untrusted) to the council — a text funnel, never a tool/
  actuation surface. Opt-in (`OLYMPUS_A2A_SERVER`); `olympus a2a serve`.
- **Native Bedrock Converse for non-Claude models** (#13). `bedrock_converse.py`
  drives Bedrock's Converse API for Titan/Nova/Llama/Mistral/Cohere; `backend`
  routes to it when a Bedrock model isn't Claude. boto3 is a lazy `[bedrock]`
  extra, so the required-dependency footprint is unchanged.
- **Daytona remote-execution backend** (#11). `OLYMPUS_EXEC_BACKEND=daytona`
  submits the (cmdguard-checked) command to a Daytona workspace API over the
  gated `tools._http_post_json` (which also scans the outbound body for secrets)
  and maps the reply into the standard `Result`. The security gate runs first;
  no new dependency. `tools._http_post_json` gained an optional `headers` arg for
  the bearer.

Deliberately still declined (safety-line items, ADR 0011 / `DEFERRED.md`):
weaponized/arbitrary-target exploitation (#16), raw-socket scanner sandbox (#18),
persistent unconfined shell (#14).

### Security

- **OSV → trusted-tool injection (fix).** `assess_deps` is a `TRUSTED_TOOL` (its
  result reaches the model unwrapped), but the OSV feed put external,
  attacker-influenceable text (`summary`/`id`/`cwe`/CVSS vector) into finding
  evidence — a prompt-injection channel. `_osv_parse_vuln` now neutralizes every
  OSV-derived string at the source: the summary is injection-defanged
  (`sanitize_for_memory`), and the id/cwe/vector are restricted to their safe
  charset/shape.

### Added — MCP surfacing: governed read-only Aegis + discovery over the council server

The `olympus mcp-serve` server now exposes three **read/prepare-only** tools, so
an MCP client (Claude Desktop, an IDE, another agent) can consume the assessment
and discovery output without ever crossing the actuation boundary:

- `olympus_assess_report` — export the ledgered findings as markdown / json /
  SARIF 2.1.0, scoped to `OLYMPUS_MCP_USER`.
- `olympus_assess_scorecard` — the Aegis self-benchmark (precision/recall/F1)
  plus the blast-radius containment proof. Pure — no scope, network, or scan.
- `olympus_discover_report` — the self-discovery ledger (open gaps + proposals).

Governed by the same rule as the workspace tools: **no write, no actuator, no
network** crosses the pipe. An assessment still requires a signed, in-process
authorization — grant-scope / recon / audit / validate / run are deliberately
NOT exposed and cannot be initiated across the MCP boundary (a test asserts the
exclusion). Tests: 6 new.

### Added — Discovery auto-signal: an UNVERIFIED answer becomes a knowledge gap

Closed the loop between verification and self-discovery. When Aletheia ships an
answer behind an **UNVERIFIED** banner because it could not support the factual
claims — the council path (`reject_after_rework`) or the quick-reply path
(`direct_reject`) — the orchestrator now records that as a `knowledge` gap in the
discovery ledger (`discovery.note_gap`), so the topic is queued for later
research instead of forgotten. Fully hands-free: no tool call, no extra model
call. Strictly bounded and safe — gated on `discovery.enabled()` (opt-in
`OLYMPUS_DISCOVERY`, **off during replay**), best-effort (every failure is
swallowed, so it can never perturb the answer path), and deduped/capped by the
existing gap ledger. Tests: 4 new. No new tool/action/command.

### Added — Detection breadth: C#/Rust SAST + SSTI/header-injection/CORS validation

Widened the Aegis engine's coverage on both the whitebox and the active-
confirmation surface, all **benchmark-gated** so quality cannot regress:

- **SAST**: six new sink rules — C# SQL concatenation (CWE-89), shell
  `Process.Start` (CWE-78), `BinaryFormatter` deserialization (CWE-502), weak
  MD5/SHA-1 (CWE-327); Rust `Command sh -c` (CWE-78) and `format!`-built SQL
  (CWE-89). The labeled `_BENCH_CORPUS` grew a vuln + clean sample per language
  (24 samples total); precision / recall / F1 stay **1.0** (0 false positives),
  the floor `test_assess.py` enforces in CI.
- **Active validation** (benign, scope-locked, parameter-directed, capped): three
  new checks join the self-evolving registry —
  - **SSTI** (CWE-1336): sends a random arithmetic `{{a*b}}` and confirms only if
    the engine *evaluated* it to the product (pure arithmetic — no code, shell,
    or data access).
  - **HTTP response-header injection / CRLF** (CWE-113): injects an inert
    `X-Olympus-Canary-<tok>: 1` header via a URL-encoded CRLF and confirms only
    if it reflects into the response headers.
  - **CORS origin reflection** (CWE-942): sends an arbitrary `.invalid` `Origin`
    and confirms only if the app echoes it into `Access-Control-Allow-Origin`
    (high severity when `Allow-Credentials: true`).

  `tools._http_probe` gained a CR/LF-filtered `extra_headers` seam (used by the
  CORS Origin probe — it can never be turned into a request-splitting primitive).
  The active-probe cap rose 20→40 (still ≤50, so the containment scorecard's
  "no spraying" vector stays proven), and the containment allowlist now names all
  five benign checks. Tests: 10 new (all checks confirm-and-only-confirm, plus a
  containment re-proof).

### Added — Live CVE feed for dependency auditing (OSV.dev, opt-in)

`assess_deps` now ALSO queries the live **OSV.dev** feed when the operator opts
in (`OLYMPUS_ASSESS_OSV`), merging live advisories with the bundled index
(deduped by CVE, CVSS computed from the OSV vector). Closes `DEFERRED #17`.
Hardened: each query goes through a new gated `tools._http_post_json` — the
canonical gated POST, with `_http_get`'s SSRF/rebinding-pin + assessment
egress-confinement preamble **plus an outbound-body secret-exfil scan** (a POST
can leak in the body, not just the URL). The confinement permits only the
trusted `api.osv.dev` infra host (not target-adjacent hosts). Results are cached
with a 24 h TTL (bounded); any failure degrades silently to the bundled index
(offline-first preserved); off during replay. Default behaviour is unchanged
(bundled index) unless opted in. No new tool/action/command. Tests: 7 new.

### Added — Blast-radius containment: own each of Strix's damage vectors

Made the assessment guardrails **owned, active, and demonstrable** (ADR 0013).
Two additions to `olympus/assess.py`:

- **Active egress confinement.** `confined_egress()` pins ALL outbound network
  for an assessment to the signed authorization's hosts, enforced at the
  gated-fetch layer (`tools._http_get`/`_http_get_bytes`/`_http_probe` now call
  `assess.egress_confined_reason` after their SSRF preamble). A host outside the
  signed scope is refused at the socket layer, fail-closed — so even a hijacked
  assessment cannot reach the operator's LAN, a metadata endpoint, or any
  out-of-scope host (the inversion of Strix's open-egress sandbox). A strict
  **no-op** when no assessment is active, so ordinary fetches are unchanged;
  `run_assessment` runs inside it.
- **A containment self-check.** `containment()` / `olympus assess containment`
  maps each of Strix's five blast-radius vectors (prompt-only scope, open
  egress, refusal-suppression, arbitrary payloads, removed audit trail) to its
  owning Olympus control and PROVES each is contained — live checks where it
  can. `test_assess.py` asserts all five stay contained, so a regression that
  widens the blast radius fails CI.

No new tool/action/command (an `assess` subcommand + one no-op-by-default check
on the shared fetch path). Tests: 7 new.

### Added — Self-discovery: acquire knowledge + propose features over time

New `olympus/discovery.py` — a native loop pointed at what Olympus does NOT yet
know or cannot yet do, closing those gaps over time (ADR 0012). It complements
the existing "improve what we have" machinery (Prometheus prompts, Metis skills,
`evolve.py` tunables, wiki dreaming) with a "discover what we're missing" spine:

- **Knowledge gaps → durable knowledge.** A gap ("I don't understand X") is
  recorded via the new `note_knowledge_gap` tool (Metis/Argus/Prometheus), via
  friction derivation, or `olympus discover note`. The heartbeat cycle researches
  open gaps (`research.run`, SSRF-gated + wrapped upstream) and writes the cited
  result to a durable wiki page. A degraded result ("no usable evidence") is
  never written as knowledge — the gap stays open and retries.
- **Capability gaps → feature proposals.** Recurring action friction
  (`outcomes.insights`) becomes structured proposals on the existing upgrade
  store (surfaced in the digest and `olympus discover`), for the operator to
  review — the native, recurring form of the "analyze the landscape → propose
  what to absorb" pattern that produced the Firecrawl/Strix absorptions. Nothing
  is auto-built.

1 new tool (125 total): `note_knowledge_gap` (TRUSTED / own-state). 1 new command
(127 total): `olympus discover` (run/note/gaps/report). Heartbeat cadence
`DISCOVERY_EVERY`, **opt-in** via `OLYMPUS_DISCOVERY`, replay-inert, bounded.
Safety: notice-don't-impose (features proposed never applied; knowledge
sanitized at the memory sink + wrapped upstream). THREAT_MODEL, capabilities.json,
README, ADR 0012 updated. Tests: `tests/test_discovery.py` (13 new).

### Added — Aegis detection breadth (5 languages) + a precision fix

Expanded the SAST rule set from Python/JS to **6 languages** — added 21 curated,
high-signal rules for Go (insecure TLS, shell exec, weak hash, Sprintf-built
SQL), PHP (eval, shell-with-variable, request-file-inclusion, echoed input,
unserialize), Java (Runtime.exec, concatenated SQL, ObjectInputStream, weak
hash), Ruby (eval, interpolated system, Marshal.load, html_safe), and extra
Python (JWT verify disabled, unfiltered `extractall` path-traversal, Django
`mark_safe`). Added 7 more dependency advisories (Django/urllib3/werkzeug/
aiohttp; express/jsonwebtoken/ws). The benchmark corpus grew from 8 to 20 labeled
samples across all six languages — **precision/recall/F1 = 1.0** (34 true
positives, zero false positives/negatives).

Also a precision fix surfaced *by* the benchmark loop: pure-comment lines that
merely name a sink ("# eval(x) is dangerous") are no longer flagged. Found the
false-positive class, added comment-only clean fixtures to the corpus, fixed the
scanner, and the benchmark floor now enforces **zero** false positives (was
≥ 0.9 precision, now == 1.0) — the measured, regression-gated evolution loop
working as designed. No new tool/command/manifest change. Tests: 3 new.

### Added — Aegis assessment experience (the self-evolving memory loop)

Every weakness Olympus's OWN scanners/validators confirm now accrues into a
compact, durable knowledge record (`assess.knowledge` / `insights_block` /
`olympus assess insights`), and that record is injected into Aegis's system
prompt (`specialists._extra_context`) — so future assessments prioritise the
weakness classes Olympus has most often confirmed. This is the genuine
experience → knowledge → better-future-performance loop (Metis's daily cycle,
scoped to security), and the answer to "keeps self evolving so Olympus becomes
stronger over time": Aegis's prompt literally sharpens as it works.

Safety: only findings from Olympus's deterministic producers (`sast`,
`http_audit`, `dep_audit`, `secret_scan`, `active_validation`) are learned from
— NEVER a finding an agent recorded via the `record_finding` tool
(`source="agent"`), whose text could carry content an injected page steered in,
so nothing untrusted can reach the self-evolving prompt. Knowledge is CWE-class
aggregates (name + count + method — no target data), deduped by fingerprint (no
double-counting across runs), replay-inert, and bounded. No new tool or manifest
change. Tests: 6 new (84 assess/sarif/threat/envelope/specialist green).

### Added — Aegis self-benchmark (measured, regression-gated evolution)

Added `assess.bench` / `assess.bench_scorecard` and `olympus assess bench` — a
self-benchmark that scores the assessment engine's detection (precision / recall
/ F1) against a labeled corpus of known-vulnerable and known-clean fixtures,
scored on the EXACT production detection logic (`_sast_findings_for_text`, the
dependency matcher — refactored out of `sast_scan` so the score can't drift from
what really runs). This is the spine that makes the self-evolving loop safe:
capability can grow over time (new checks/rules), but detection quality cannot
silently regress — `tests/test_assess.py` asserts a quality floor (recall 1.0,
precision ≥ 0.9), so a change that misses a known bug or fires on clean code
fails CI. Mirrors Olympus's benchmark-gated prompt-upgrade philosophy (Prometheus
rolls back a regression). Pure — no scope/network/memory; no new tool or manifest
change. Tests: 3 new. Current score: 8 samples, precision/recall/F1 = 1.0.

### Added — Aegis active validation (scope-locked, benign confirmation)

Added `assess_validate` (10th assess tool, 124 total; `olympus assess validate`)
— a **scope-locked, non-destructive** active-validation layer that upgrades a
finding from "potential (static)" to "confirmed (observed)". It is the deployable
superset of Strix's exploitation phase and the moat's answer to "make it stronger
than Strix": it confirms with a BENIGN marker sent only to a parameter the
operator named (never guessed/sprayed), only against a code-authorized target,
through the SSRF-pinned gated fetch, hard-capped (≤20 probes) — so it produces a
real proof while remaining safe to run unattended. Checks live in an extensible
registry (`assess._ACTIVE_CHECKS`) that compounds over time — currently
reflected-input confirmation (missing output encoding → XSS surface) and open
redirect (a benign canary read from the `Location` header *without following it*,
so the canary is never actually requested). Adding a check needs no new tool,
command, or manifest change — the self-evolving moat by design. It does
**not** perform arbitrary-target exploitation, payload spraying, or open-egress
access — those stay declined (ADR 0011 Decision (f); `DEFERRED.md` #16/#18).
Tests: 7 new (`tests/test_assess.py`). THREAT_MODEL, capabilities.json, README,
and ADR 0011 updated.

### Added — Native "Aegis Assessment" suite (Strix absorbed as a moat)

Absorbed [Strix](https://github.com/usestrix/strix)'s security-assessment
capability surface as native Olympus capabilities — turning each of its
weaknesses into a structural strength (design locked in ADR 0011, analysis in
`docs/STRIX_TRACKING.md`). New modules `olympus/assess.py` (code-enforced scope,
recon, HTTP security-header audit, source SAST, secret + dependency scanning,
findings model with dedup + orchestration under a USD budget stop) and
`olympus/sarif.py` (pure-Python CVSS 3.1 scoring + SARIF 2.1.0 export), wired as
**9 new tools** (123 total), **1 new action** (25 total), and **1 new CLI
command group** (126 total):

- Tools: `assess_recon`, `assess_http_audit` (ingestion, gated + wrapped);
  `assess_scope`, `assess_sast`, `assess_secrets`, `assess_deps`,
  `record_finding`, `list_findings`, `export_findings` (own/local, trusted).
- Action: `authorize_assessment` (IRREVERSIBLE, revocable) — the signed,
  human-approved, ledger-recorded scope grant.
- CLI: `olympus assess`
  (authorize/scope/revoke/recon/audit/sast/secrets/deps/run/report/clear).
- Aegis upgraded from defense-advice-only to defense **plus** authorized
  assessment of the operator's own assets (holds the assess + source-read
  tools; still holds no actuators).

The moat inversions: **scope enforced in code** (`require_scope()` fails closed
against a signed grant — not a prompt); **a signed authorization** instead of
Strix's refusal-suppression (agents cannot self-authorize); **untrusted target
content isolated structurally** (recon/audit fetch via the IP-pinned
`tools._http_probe`, INGESTION-classified → wrapped + actuators stripped);
**findings with CVSS computed from a vector** + SARIF 2.1.0 + fingerprint dedup +
a ledger note (the audit trail Strix removed); **secret evidence redacted** so a
report can't leak the secret. Pure-stdlib — no new dependencies. Tests:
`tests/test_assess.py`, `tests/test_sarif.py` (49 new). THREAT_MODEL.md,
`capabilities.json`, and README markers updated; the tool classification stays
complete + disjoint (envelope fail-closed).

### Added — Web context: learned action-profiles, fleet lore sharing, build proposals

Three self-evolution extensions turn the web-context corpus into a deeper moat
(ADR 0009/0010 addendum), all under the absorbed-capability contract (opt-in for
autonomy, replay-inert, bounded, additive-only, own tests):

- **Learned per-domain action-profiles.** `domainlore` now remembers the exact
  *safe action profile* (the scroll/expand/click steps) that beat a domain's byte
  baseline — not just that interaction helped. With `OLYMPUS_WEB_AUTO_ACTIONS=1`,
  a plain `scrape` of that domain auto-replays the earned profile through the
  governed browser harness (no manual `actions` argument). Every step is
  re-validated against the safe-verb allowlist on use; an explicit `actions=`
  (including `[]`) always wins; inert under replay.
- **Operator-gated fleet lore sharing.** `federation` gains `export_domainlore`/
  `import_domainlore` and a signed `/federation/domainlore` route, mirroring the
  lesson-sync guarantees (signed, `trusted`-only, secrets/PII scrubbed). Imported
  per-domain facts *stage* in the corpus and are folded into the live corpus only
  by the operator's explicit merge — purely additive, never overwriting local
  truth, never relaxing a fetch gate. New CLI: `olympus webknowledge --share
  <peer>` / `--staged` / `--merge`.
- **Auto-drafted build proposals.** New `webproposals` module turns a
  proposal-kind discovery into a full, evidence-backed build proposal (motivation,
  cited domains, concrete change, safety posture, acceptance criteria) queued for
  the operator to accept/decline/mark built — surfaced on `olympus webknowledge
  --proposals` and the moat board. Explicitly *not* a code generator: it drafts a
  reviewable engineering artifact, never a behavior change.

New tests: `test_domainlore_share.py`, `test_webproposals.py`,
`test_federation_lore.py`, plus action-profile auto-apply coverage in
`test_webctx.py`. No new tools/commands (new CLI flags on `webknowledge`).

Adversarial hardening of the three seams:
- Auto-applied learned profiles are now **best-effort**: if the profile can't run
  (no browser, nav/interaction error) the scrape falls back to a plain fetch
  instead of failing — enabling the flag can't break a scrape a plain fetch would
  have handled. An *explicit* `actions=` still surfaces its error.
- Shared/merged **domains** are validated as bare hostnames (rejecting paths,
  spaces, control chars, userinfo, single-label hosts) so a peer can't pollute
  the corpus with malformed keys.
- Action-profiles are **structurally sanitized at rest** — bounded step count and
  field sizes, well-formed steps only, whole-step drop-to-fit (never a
  string-sliced, invalid-JSON dump). Verb safety stays enforced authoritatively
  at use (`_ACTION_VERBS`). Federation shares the profile structurally rather than
  running a whole-string PII scrub that would mangle valid JSON.

### Added — Self-evolving web context: knowledge, discovery, self-tuning

The absorbed web-context system now compounds and discovers over time inside
Olympus's evolve/moat spine (ADR 0009), so it gets stronger the more it runs —
a data network effect a copier starts at zero:

- **`domainlore`** — a per-domain learned-knowledge corpus. Every scrape/map
  folds durable facts (sitemap location, robots posture, interaction/mobile
  wins, page size, brand, JSON-LD/feed exposure, fetch success) into a store and
  feeds them back as *purely-additive* hints to the next visit (a known sitemap
  is tried first) — never relaxing a safety gate. Opt-outable, replay-inert,
  bounded, corrupt-store-quarantining. Recorded on the moat board.
- **Self-tuning** — new evolve knob `webctx.fetch_timeout`; domainlore records
  ok/fail from every visit, so the heartbeat reviewer lengthens the fetch
  timeout when fetches keep failing (bounded).
- **New deterministic, LLM-free formats** — `jsonld` (parses
  `<script type="application/ld+json">` structured data) and `feeds` (RSS/Atom
  discovery), on `web_scrape`. A capability Olympus *discovered it should have*
  from the data it observed.
- **`webreflect`** — an opt-in heartbeat routine that turns the accumulated
  corpus into *discoveries*: domains exposing JSON-LD (use the free format),
  publishing feeds (watch instead of re-scrape), needing interaction (a default
  action-profile candidate), or fetching poorly (try mobile/proxy). Each
  standing pattern is surfaced once (deduped), saved as a lesson, and notified —
  evidence-backed feature proposals, not autonomous code-gen.
- **Closed learn→apply loop** — `scrape(mobile=None)` (the new default) auto-
  applies the per-domain bias the corpus has *earned*: a domain where a mobile
  fetch repeatedly beat the desktop baseline is scraped mobile-first next time,
  no prompting. Purely additive (an explicit `mobile=True/False` always wins),
  and the win is learned from real byte-gain. Extraction quality (verify
  confirmed/flagged) is now recorded per domain too.
- **Operator surface + federation** — new `olympus webknowledge` command prints
  the corpus report, the most-visited domains, and the live discoveries; the
  `web_context` row on `olympus moat` shows lore stats and the tuned knob.
  Discoveries ride the existing signed/scrubbed **lessons federation** seam, so
  web knowledge compounds across a fleet without widening the trust surface.
  Corpus hygiene: the heartbeat maintenance sweep now prunes domains unseen
  within the retention window (`domainlore.prune`), and the store is bounded and
  corrupt-quarantining. Documented in `docs/WEB_CONTEXT.md`.

Internal capabilities + new formats + one new command. New tests:
`test_domainlore.py`, `test_webreflect.py`, plus jsonld/feed coverage.

### Added — Web Context: the previously-declined Firecrawl features, built native

The Firecrawl capabilities ADR 0010 had declined are now first-class — built in
Olympus's idioms, without importing the anti-patterns:

- **New scrape formats** on a new `web_scrape` tool (the advanced sibling of the
  quick `browse_page` reader): `images` (all `<img>`/og image URLs, absolute),
  `branding` (site name, theme color, favicon, social image, description),
  `html` (cleaned) vs `rawHtml` (as-fetched), and `attributes` (selector →
  attribute pairs, with a pure-stdlib `tag/.class/#id` selector). All parsed in
  one deterministic pass; no new dependency.
- **JSON change-tracking mode** — `web_diff`/`web_monitor_add` accept a `schema`
  and structurally diff the *extracted object* (which fields changed), alongside
  the existing git-diff text mode. `webmonitor` persists per-monitor JSON state.
- **`mobile` / `location` hints** — a mobile User-Agent and an `Accept-Language`
  from a country/locale, threaded through the gated fetch. (A rotating
  residential/geo *proxy mesh* is hosted infrastructure, not code — set
  `HTTPS_PROXY`/`PROXY_SERVER` to route through your own egress.)
- **In-scrape actions** — `web_scrape(actions=[…])` drives the page through the
  **governed** browser harness (click/scroll/type/wait) before scraping, with a
  new `BrowserSession.html()` accessor. Actuation stays SSRF-gated, ledgered, and
  capability-separated; **`executeJavascript` is rejected, not run** — Olympus
  does not expose ungoverned JS eval. Degrades cleanly when no browser is present.

New tool `web_scrape` (115 total). Tests, threat-model row, and capability
counts updated.

### Security — Harden the Web Context suite against adversarial input

A four-dimension hardening pass (parser DoS, resource bounds, SSRF
defense-in-depth, extraction/monitor robustness) on `webctx`/`webmonitor`:

- **Parser (`to_markdown`) DoS.** Fixed two quadratic-output amplifiers a 400 KB
  page could exploit — nested-list indent multiplication (`<ul>`×N) and a
  `</tr>` that re-emitted the table separator with stale cell state — plus a
  quadratic-backtracking whitespace `re.sub` (a `<pre>` space-run took seconds).
  Added a running output-byte guard, capped list depth / link count / title, and
  recover from unclosed `<a>`/`<title>` instead of swallowing the page. Hostile
  inputs that previously OOM'd or spun the CPU now finish in milliseconds,
  bounded to the markdown cap.
- **SSRF defense-in-depth.** Web-context fetches are restricted to ordinary web
  ports (80/443/8080/8443) so the suite can't be turned into a port-prober;
  `map_urls` now only fetches *same-site* sitemaps (a hostile `robots.txt` can't
  point us at third parties) with a visited-set and early-exit; `crawl` honors
  `robots.txt` Disallow (fetched through the gate) and bounds its BFS frontier;
  the shared host blocklist is normalized (`*.localhost`, trailing-dot, case);
  and the redirect ceiling is made explicit (5).
- **Resource bounds.** Bulk fetches use a tight socket timeout (slow-drip
  defense); `diff` caps the previous snapshot and line count before `difflib`;
  `parse_document` early-exits accumulation, bounds pages/paragraphs, and
  decrypts empty-password PDFs — so a compression-bombed document can't exhaust
  memory or hang the worker.
- **Robustness.** `extract` tolerates a non-object model result instead of
  crashing the tool and caps the extracted object size; `web_extract`'s `verify`
  now respects `OLYMPUS_WEB_EXTRACT_VERIFY`; the change-monitor no longer holds
  its store lock across network I/O (a slow site could freeze `add`/`remove`),
  quarantines a corrupt store instead of silently wiping it, backs off and
  auto-pauses permanently-failing monitors, and surfaces dropped change alerts.

25 hardening tests added (`test_webctx.py`, `test_webmonitor.py`).

### Added — Native "Web Context" suite (Firecrawl absorbed as a moat)

Absorbed [Firecrawl](https://github.com/firecrawl/firecrawl)'s full web-data
surface as native Olympus capabilities — turning each of its weaknesses into a
structural strength (design locked in ADR 0010). New module `olympus/webctx.py`
(pure-stdlib readability→markdown, scrape, map, crawl, batch, verified
extraction, llms.txt, diff, PDF/DOCX parse) and `olympus/webmonitor.py`
(opt-in, replay-inert scheduled change-monitoring), wired as **8 new tools**
(114 total) and **6 new CLI commands** (120 total):

- `web_map`, `web_batch_scrape`, `web_extract`, `generate_llmstxt`,
  `parse_document`, `web_diff` (ingestion), `web_monitor_add`/`web_monitor_list`
  (own-state), plus `olympus scrape|crawl|map|extract|llmstxt|monitor`.
- The existing `browse_page`/`crawl_site` are **upgraded** to `webctx`
  clean-markdown (readability-grade, zero-dependency) rather than shipping a
  duplicate scrape/crawl surface; `crawl_site` gains `include`/`exclude` filters.

Every Firecrawl weakness answered natively: **SSRF** — every fetch (page,
sitemap, robots, crawl hop, document, diff, monitor) routes through the
IP-pinned `tools._http_get`/new `tools._http_get_bytes` (no fail-open socket
hook, no rebinding window); **injection** — every model hop wraps scraped bytes
via `security.wrap_untrusted` and the fetching tools are in `INGESTION_TOOLS`
so actuators are stripped from the run; **unverified extraction** —
`web_extract` runs a second `verify` pool role that flags unsupported values;
**open-by-default / ungoverned JS** — new tools are inert until invoked, the
monitor is opt-in (`OLYMPUS_WEB_MONITOR`, off during replay), and page actuation
stays behind the governed browser harness. PDF/DOCX parsing is a new optional
`[docs]` extra (lazy-imported, path-confined via `sandbox._confine`), and a new
AST import-scan test enforces the "3 required deps" claim so no lazy import can
ship undeclared. New tests: `tests/test_webctx.py`, `tests/test_webmonitor.py`.

Analysis that motivated this ships in `docs/FIRECRAWL_TRACKING.md` (inventory +
security critique + adoption watchlist; no Firecrawl code used).

### Added — Absorbed capabilities as a native, self-evolving moat

Six capabilities surveyed from an external agent harness (ruflo), re-built
natively in Olympus's own idioms — each **off by default**, deterministic, and
replay-safe — then wired into Olympus's self-evolution spine so they measure
themselves and get stronger the more they run. Design locked in ADR 0007
(federation), ADR 0008 (the shared opt-in/replay-safe contract), and ADR 0009
(the self-evolving moat).

- **Vector recall at scale (`OLYMPUS_ANN`).** A pure-Python HNSW index
  (`annindex.py`). One-shot recall stays an exact scan; `docrag` keeps a
  PERSISTENT graph (built once, keyed to a corpus signature) for real sublinear
  recall over the document corpus.
- **Swarm topologies (`OLYMPUS_SWARM`).** `dytopo` gains explicit mesh / star /
  hierarchical / ring shapes and a post-dispatch consultation pass; wired into
  `_pipeline` (recorded in trace meta and restored on replay).
- **Quorum consensus verification (`OLYMPUS_CONSENSUS`).** `consensus.py` folds
  N lens-diverse verifiers with a safety-biased quorum, and **self-tunes its
  verifier panel** via `evolve` when the quorum keeps failing to form (frozen
  per run for replay).
- **Bandit routing (`OLYMPUS_BANDIT_ROUTING`).** A deterministic UCB1 explorer
  (`bandit_routing.py`) alongside the conservative learned selector; its
  exploration constant **self-tunes** via `evolve` (explores less when routing
  outcomes degrade).
- **Semantic routing (`OLYMPUS_SEMANTIC_ROUTING`).** Orders the specialist roster
  shown to Zeus by embedding-relevance to the request (reorder-only, replay-
  frozen) — a no-op for the curated 13, earning its keep at scale with many file
  agents.
- **File-defined agents (`OLYMPUS_AGENTS`).** `agentreg.py` loads `<key>.md`
  agents into the registry, safety-bounded to a read-only tool allowlist.
- **Cross-instance federation (`OLYMPUS_FEDERATION`).** `federation.py` +
  `olympus federation` CLI: ed25519 handshake over the `witness` root of trust,
  pinned/tiered peers, egress-guarded transport, and scrubbed, trusted-only,
  candidate-only lesson sync.
- **`olympus moat`.** A status board showing each capability's enabled state,
  self-evolution health, and current self-tuned settings.

Capability accounting stays truthful: `olympus federation` and `olympus moat`
are the only new commands (113 → 114); no new tools or agents.

### Added — Two more `DEFERRED.md` closures (code-graph precision, skill retrieval)

Two further deepenings along the same axes, each closing a tracked deferral. No
new tools or commands — capability accounting is unchanged.

- **Code graph — qualified-call precision (closes #15).** A qualified Python call
  (`memory.save()`) is now pinned to the exact module its qualifier names via the
  per-file import-alias map, emitting a precise `EXTRACTED` (confidence 1.0) edge
  instead of being dropped as ambiguous. So `impact` stops under-reporting and
  `verify` answers `CONFIRMED`/`REFUTED` where it previously could only say
  `UNKNOWN` for a name defined in many files. Strictly additive: only narrows
  `len(cands) > 1` qualified calls; unqualified / unique / unresolved calls are
  unchanged, and non-unique narrowing falls back to the prior behaviour.
- **Skills — task-scoped semantic retrieval in the live prompt (closes #3).**
  `OLYMPUS_SEMANTIC_SKILLS` swaps a specialist's full in-prompt skill index for
  the top-K skills most relevant to the task (`skills.scoped_index()`, cosine
  over the existing embedding cache). Opt-in and off by default; engages only
  once a specialist's library outgrows the prompt; degrades to the full index
  when embeddings are absent so no skill becomes unreachable (any skill stays
  loadable by name via `read_skill`). Recorded in trace meta and replay-frozen
  per specialist on the prompt-assembly path, and surfaced on the `olympus moat`
  board.

- **Federation — capability discovery + multi-peer aggregation.** A pinned peer
  can POST a signed request to `/federation/capabilities` and receive a signed
  card of what an instance offers (specialist roster + skill count, no skill
  contents), gated by the same pinned-peer + `task`-trust check as a task.
  `federation.call_peers()` fans one task across several trusted peers and
  collects each reply as untrusted data, isolating a dead peer so it never sinks
  the fan-out. New CLI actions `olympus federation capabilities <peer>` and
  `ask-all <message>` (positional actions — command accounting unchanged). Reuses
  the existing signed-envelope, trust, scrub, and egress machinery — no new trust
  surface.

### Fixed

- **Code graph — no false-precise edge on a shadowed method.** Qualifier
  narrowing now skips shadow-prone method names (`get`, `update`, `count`, …):
  the import-alias map has no scope tracking, so a local var/param that shadows an
  imported module name (`def h(store): store.get(k)`) would otherwise assert a
  false `EXTRACTED`/1.0 edge to `store.get` for a plain `dict.get`. Those names
  fall through to the same-file-only `_SHADOWED` rule, as before.
- **Replay — the semantic prompt-shaping paths re-raise `ReplayDivergence`.**
  `semantic_roster` and the new `_skill_index_for` wrapped `frozen_context` in a
  broad best-effort `except`; since `ReplayDivergence` subclasses `RuntimeError`
  it was swallowed, masking a genuine divergence (e.g. a skill library that
  crossed the size threshold since the recorded run) behind a later request-hash
  mismatch. Both now re-raise it, matching the orchestrator's frozen-context
  sites.
- **Replay — frozen run-state now survives the dispatch thread hop.** The active
  run scope used by `replaystore.frozen_context` moved from a `threading.local`
  to a `ContextVar`, and the specialist worker re-publishes it (like the trace
  contextvar) — so a `frozen_context` call made on a dispatch worker (the new
  task-scoped skill index) freezes in record mode and reproduces on replay,
  instead of silently skipping the freeze on workers and diverging.

## [0.26.0] — 2026-07-22

### Added — Four native-capability deepenings (verification, adaptation, providers, review)

Four extensions that strengthen existing Olympus subsystems along its core axes.
No new tools — capability accounting is unchanged. Closes three tracked
`DEFERRED.md` items (#2, #4, and the OpenAI-compatible half of #5).

- **Code graph — verify honesty fix.** `verify_claim` no longer false-REFUTEs a
  real call whose callee name is defined in more than `_MAX_AMBIGUOUS` places
  (the resolver skips those edges, so absence isn't proof). It now returns the
  honest `UNKNOWN` — an Aletheia correctness fix, since it was asserting a true
  claim false (e.g. `_run_command_execute calls run`). Refutation power is kept
  for uniquely-named callees.
- **Skills — semantic dedup + retrieval.** Write-time embedding dedup flags
  near-duplicate skills at `create()` time (deterministic, no model call), and a
  new `skills.search()` gives cosine-ranked retrieval. Reuses `embed.py`;
  best-effort and zero-cost when `OLYMPUS_EMBED_MODEL` is unset. The embedding
  cache is keyed by content hash AND model so a model change re-embeds rather
  than silently serving stale-dimension vectors. Closes DEFERRED #2.
- **Providers — per-model effort tiers.** `low/medium/high` now map to
  `reasoning_effort` for the OpenAI-compatible reasoning families that accept it
  (OpenAI o-series/gpt-5, Gemini 2.5 / thinking), allowlist-gated so a
  non-reasoning model is never sent an unknown param. Also fixes a latent break:
  those models require `max_completion_tokens`, not `max_tokens`. Kill-switch:
  `OLYMPUS_DISABLE_REASONING_EFFORT`. Closes the OpenAI-compatible half of #5.
- **Orchestrator — Athena bounded multi-pass review.** When Athena orders a
  rework, the reworked output is now RE-REVIEWED once (bounded — never a third
  pass). It runs only on the minority of turns that actually reworked, so the
  common approve-first path pays for a single review. Closes the one-shot half
  of DEFERRED #4.

### Added — Absorb OpenManus's capabilities as native Olympus features

The capabilities of [OpenManus](https://github.com/FoundationAgents/OpenManus)
are absorbed as *native* Olympus features — registered in Olympus's own
registries, security-gated, capability-accounted, and tested — not a bolted-on
copy. Most of OpenManus was already native (browser, computer-use, web
search/fetch, Docker sandbox, file tools, gated shell exec, MCP server, the
specialist council); these close the genuine gaps and, in security and provider
reach, go past the original.

- **`run_python`** — provider-independent Python execution as an approval-gated
  `ActionType` (irreversible, `exec` scope, never auto-runs) routed through the
  confined `sandbox.run_python` (cmdguard, root confinement, timeout, output
  cap), plus a first-class tool that stages it.
- **Native MCP client** (`olympus/mcp_client.py`) — connects to external MCP
  servers over **stdio + SSE** on *every* backend (not just Anthropic's
  server-side connector). Tools are namespaced `mcp__server__tool` and dispatch
  through `resolve_handler`; capability-separated; stdio gated behind
  `OLYMPUS_MCP_STDIO_ALLOWLIST`; output enveloped as untrusted; tool
  descriptions injection-scanned; discovery cached with a TTL. Requires the
  optional extra: `pip install olympus-council[mcp]`.
- **`chart_from_data`** — tabular data → bar/line/scatter/pie chart as
  pure-Python SVG (zero new deps), labels XML-escaped, path-confined.
- **`crawl_site`** — bounded recursive crawl over the SSRF-gated fetcher
  (depth/page/byte caps, same-domain option); classified as untrusted ingestion.
- **Azure OpenAI** — deployment-scoped URL + `api-key` header, detected by
  endpoint host; rides the existing key-rotation/failover machinery.
- **AWS Bedrock** — Claude via `anthropic.AnthropicBedrock`, full capability
  parity; server-side tools degrade to the client-side path by provider string.
- **Docker sandbox hardening** — `--cap-drop ALL` + `no-new-privileges` +
  memory/PID caps by default, `--network none` kept.

Security & accounting: new tools classified in exactly one of `TRUSTED_TOOLS`
xor `INGESTION_TOOLS` (fail-closed envelope test enforced), threat-model rows +
capability manifest regenerated (106 tools / 24 actions), README counts bound.
Consciously-deferred edges (Daytona remote sandbox, native A2A server,
non-Claude Bedrock converse, persistent shell) are recorded in `DEFERRED.md`.
Opt-in live integration tests validate the MCP client end-to-end against a real
stdio server, and the docker/Azure/Bedrock external legs where the
infrastructure exists.

### Added — Code graph drives self-evolution + hardening

The code graph becomes an ACTIVE part of how Olympus improves itself, not just a
tool a specialist can call — then a two-front adversarial review (correctness +
security) hardened the wiring, with a regression test per confirmed finding.

- **Aletheia auto-checks structural claims.** `codegraph.scan_claims()` finds
  the structural claims in the specialist outputs and verifies each against the
  graph; the hallucination controller receives CONFIRMED facts as ground truth
  and "no edge found" notes as ADVISORY — deterministic, firing even if the
  verifier never calls the tool.
- **Every self-upgrade proposal is stamped with its blast radius.**
  `codegraph.impact_report()` + `propose_upgrade` now record what depends on the
  code a proposal names, from the EXTRACTED-truth graph.
- **`olympus codegraph gate`** wires the previously-standalone A/B harness into
  the CLI (does the graph reduce file reads / tokens / model calls /
  hallucination flags).
- **TS/JS/Vue class-method shorthand** (`name(params) {`) is now extracted —
  guarded so calls, arrow/function callbacks, and control statements are never
  false positives.

Hardening (all confirmed by review, each with a regression test):
- scan_claims no longer treats the English word "uses" as a call claim and
  requires both symbols to be *distinctive* identifiers, so ordinary prose
  ("the parser uses config") can't become a false REFUTED verdict; a hard match
  cap bounds work on a hostile bundle (was unbounded — 13.5 s → 3 ms).
- `with`/`using`/`lock`/`foreach`/… control statements are no longer welded in
  as phantom method nodes.
- the method-shorthand regex was rewritten to be genuinely near-linear (a
  40 k-char padded line: 12–15 s → 9 ms), not merely saved by the line-length
  cap.


### Added — Code graph: package-manifest nodes (iteration 4)

`pyproject.toml`, `package.json`, `go.mod`, and `pom.xml` are now indexed:
the declared package and each dependency become **canonical name-keyed**
`entity` nodes (one hub per package, however many manifests name it), linked
by `depends_on`. So a dependency shared across services shows up as the single
hub it is, `impact` follows `depends_on` ("what breaks if this package
changes"), and it's a graph query to find every dependent. Parsed with the
stdlib (tomllib/json + line regexes) — no new dependency, dep count bounded
against a hostile manifest. Edges run through the shared resolver, so an
incremental update drops a removed dependency instead of leaving it stale.
5 new tests.

### Added — Code graph: 4 more languages + ADR/RFC citations (iteration 3)

- **37 languages** now: adds Pascal/Delphi, Verilog/SystemVerilog, Salesforce
  Apex (classes + triggers), and Vue/Svelte/Astro single-file components
  (matched via their `<script>` block). Pascal and Apex extract inheritance.
- **ADR/RFC citation nodes**: `ADR-0007`, `ADR 7`, `RFC 2119`, `RFC-7231` in any
  doc or code comment become one canonical `citation` node (normalized label)
  with a `cites` edge from the file — so "what code depends on ADR-7?" is a
  graph query. Citation labels are a fixed normalized form, never raw text, and
  are excluded from god-node ranking.
- Class/module patterns now match before function patterns, so a line that
  could look like both (Apex `trigger X on Y (...)`) is read as the declaration
  it is. 11 new tests; ReDoS-safety check re-run over all 37 languages.

### Added — Code graph: 12 more languages (self-evolution iteration 2)

The regex engine grows from 21 to **33 languages**, the biggest parity gap from
the completeness audit — all via the zero-dependency engine, all ReDoS-checked
at the line cap: Objective-C, Groovy/Gradle, SQL, Terraform/HCL, Perl, R,
Haskell, OCaml, Clojure, Erlang, Solidity, Nim. Objective-C and Solidity also
extract inheritance (`@interface X : Y`, `contract X is Y`). 15 new tests,
including a blanket ReDoS-safety check over every language's every regex.

### Added — Code graph: inheritance edges (self-evolution iteration 1)

The code graph is Olympus's own compounding asset — a moat it keeps deepening
over time. This iteration adds `inherits`/`implements` edges, closing the
richest single parity gap with the tool the capability was absorbed from and
strengthening every downstream view at once (impact now follows a base-class
change to its subclasses; communities and the report see the type hierarchy).

- Python: class bases from the AST → `inherits` edges, EXTRACTED (ground truth,
  so `verify_claim` can confirm/refute them). External bases (`Exception`,
  `object`) don't resolve, keeping the tier honest.
- ~10 OO languages via the regex engine (Java/TS/Ruby/C#/Kotlin/Swift/Scala/
  PHP/C++): base names pulled from the class line's inheritance clause,
  recorded INFERRED (regex evidence, never ground truth); multiple same-named
  bases → AMBIGUOUS. `extends` + `implements` with multiple interfaces all
  captured. Base identifiers are extracted in code, not a second regex — no new
  ReDoS surface (verified at the line cap).
- `inherits`/`implements` join the dependency relations traversed for impact
  and the cross-file relations rebuilt on every incremental update.

Roadmap for subsequent iterations (each one bounded, test-gated, committed):
more languages (Objective-C, HCL, SQL, Groovy), deeper council integration
(Prometheus consults `codegraph_impact` before self-upgrades), graph-health
metrics tracked over time, and the remaining parity gaps.

### Security & Fixed — Code-graph hardening pass

Two adversarial reviews (correctness + security) of the new code-graph engine,
with a regression test for every confirmed finding (`test_codegraph_hardening.py`).

Security:
- **ReDoS closed.** The Java/C# definition regexes had three overlapping
  whitespace quantifiers and backtracked catastrophically — a crafted
  `.java`/`.cs` file in a cloned repo hung `codegraph build`/`update`/`watch`
  for minutes. Rewritten into a provably linear token form (disjoint
  token/separator classes), plus a per-line input cap; a hostile file now
  builds in ~1 s.
- **Symlink escape closed.** `collect_files` followed symlinks, so a cloned
  repo could plant `x.yaml -> ~/.ssh/id_rsa` and pull its contents into the
  graph. It now walks with `followlinks=False`, skips symlinked files/dirs, and
  drops any path whose real location escapes the root.
- **Attacker prose kept off the trusted path.** The auto-injected context block
  and the `codegraph_subgraph`/`codegraph_overview` tools (both TRUSTED, hence
  unwrapped) now emit STRUCTURE ONLY — rationale/document node labels (free text
  from comments, docstrings, doc headings) are excluded, so a hostile comment in
  an analyzed repo can't reach the model as clean text. The explicit CLI view
  still shows prose.
- **Global graph bounded.** `codegraph global add` wrote nodes directly, bypassing
  the node/edge caps; it now enforces them, so the cross-repo graph can't grow
  without bound.
- `github.py` refuses a non-HTTPS `GITHUB_API` (never sends the token in the
  clear), caps the response body, and stops on a redirect to a plaintext host.
- The self-contained HTML export neutralizes `<!--` and `<script` in the JSON
  payload, not just `</script>`.

Fixed (correctness):
- **Node-cap corruption.** At the 20 000-node cap, `add_node` returned an
  unrelated node, welding every later file's structure onto node[0] as false
  EXTRACTED edges the oracle would then confirm. It now returns `None` and every
  extractor skips cleanly.
- **Incremental update now equals a full build.** A changed file could leave a
  stale EXTRACTED call/import edge between two *unchanged* files after new code
  made it ambiguous. `update()` clears all cross-file edges and re-resolves from
  the manifest, so incremental and full builds are identical.
- **Oracle honesty.** "X is unused" was REFUTED on AMBIGUOUS/INFERRED incoming
  edges; it now returns UNKNOWN unless an EXTRACTED caller exists.
- **Python imports resolve by full dotted path**, so `from b.util import x` with
  both `a/util.py` and `b/util.py` present resolves to `b.util` (was a false
  REFUTED / wrong-module edge); an ambiguous stem-only match is INFERRED, never
  EXTRACTED.
- **gitignore directory patterns work.** Anchored (`/build/`) and multi-component
  (`foo/bar/`) directory patterns now correctly exclude the files beneath them.
- **Watch debounce works.** It snapshotted twice with no wait between, so a
  save-storm rebuilt once per poll instead of once total; it now waits for the
  tree to settle (bounded), and an `on_update` exception can't kill the loop.
- Go bare quoted-string lines no longer become false import edges; regex-engine
  and stem-only imports are INFERRED, never ground truth; global-graph tags are
  sanitized so one tag can't be an id-prefix of another (`acme` vs `acme:web`);
  merged/global edge ids are regenerated to stay `sha(src+rel+dst)`;
  `pull_files` paginates to 300; multi-language notes get distinct rationale
  slots; incremental `updated_at` no longer overwrites the full build's
  `commit_sha`.

### Added — The code graph absorbs Graphify's pipeline as native capability

The Phase 0 code graph (Python-only, query/impact/path/verify) grows into a
full knowledge-graph engine — Graphify's feature set (MIT), reimplemented in
Olympus's idiom: store-backed, injection-sanitized, zero new required
dependencies, and honest about confidence everywhere.

- **Multi-language extraction** (`codegraph_langs`): one regex engine + a
  per-language shape table covers ~20 languages (JS/TS, Go, Rust, Java, C/C++,
  C#, Ruby, PHP, Kotlin, Swift, Scala, Lua, Bash, PowerShell, Elixir, Dart,
  Zig, Julia, Fortran); Python keeps the stdlib-`ast` extractor. Regex-derived
  call edges are INFERRED — never EXTRACTED — so the hallucination oracle
  stays sound, and it now answers UNKNOWN instead of REFUTED where only
  regex-level evidence exists.
- **Documents in the same graph** (`codegraph_ingest`): md/mdx/rst/txt/yaml
  become DOCUMENT nodes; wikilinks and local markdown links become
  `references` edges; headings become sanitized rationale.
- **Build + incremental update** (`codegraph_build`): `.gitignore` /
  `.olympusignore` handling (subset, `!` negation honored), a per-file
  content-hash manifest, `update()` that re-extracts only changed files, and
  a bulk write session that turns whole-repo builds from minutes into seconds
  (this repo: 449 files → ~2 s full build, ~0.5 s incremental).
- **Analysis** (`codegraph_analysis`): pure-Python deterministic Louvain
  communities with heuristic zero-token labels, god nodes, surprise-scored
  cross-community connections, suggested questions, `CODEGRAPH_REPORT.md`,
  and a measured token benchmark (subgraph vs corpus; 99.8% reduction on this
  repo's own graph).
- **Token-budgeted subgraph query** (`codegraph.subgraph_query`): BFS/DFS
  retrieval that stops at an explicit token budget.
- **Near-duplicate merging** (`codegraph_dedup`): MinHash + token blocking →
  Jaro-Winkler verification → union-find merge, prose-labeled nodes only.
- **Exports** (`codegraph_export`): graph.json, GraphML, Mermaid,
  self-contained HTML visualization (no CDN, payload escaped), Obsidian vault
  (own subdirectory, never touches existing notes), agent-crawlable markdown
  wiki, Cypher, and optional live Neo4j / FalkorDB pushes (lazy drivers).
- **Watch mode** (`codegraph_watch`): mtime polling + debounce, no daemon deps.
- **Global graph** (`codegraph_global`): merge per-project graphs into a
  cross-repo graph with tag-prefixed identity; add/remove/list.
- **Introspection** (`codegraph_introspect`): live PostgreSQL schema (tables/
  views/FKs, via the existing `postgres` extra) and Cargo workspace crate
  dependencies become ENTITY nodes.
- **Graph-aware PR dashboard** (`codegraph_prs` + read-only `github.py`
  endpoints): open PRs mapped to the communities and god nodes they touch,
  with shared-subsystem merge-order risk flagged.
- **Surface**: `olympus codegraph <action>` CLI (build/update/watch/stats/
  show/report/query/path/impact/verify/communities/label/dedup/export/
  benchmark/prs/global/postgres/cargo), two new agent tools
  (`codegraph_subgraph`, `codegraph_overview` — threat-modeled, stripped when
  the graph is disabled), and four read-only MCP tools
  (`olympus_query_codegraph`, `olympus_codegraph_impact`,
  `olympus_codegraph_report`, `olympus_verify_code_claim`).

### Changed — Budget-aware escalation + total scorer (ADR 0005, amendment 7)

- "Thinks harder" never defeats the spend guard: with <10% of the daily
  budget remaining, a scored effort raise above the specialist floor is
  capped back to the floor and traced (`effort.budget_capped`); the run —
  reworks included — always still happens.
- The effort scorer is a total function: garbage inputs coerce to harmless
  defaults (never an exception, always a valid tier), pinned by a seeded
  property test.

### Changed — Lock and audit-trail hardening (ADR 0005, amendment 6)

- `proclock.lock` acquisition is bounded by default (60 s; `timeout=None`
  is the explicit block-forever opt-in) — a wedged peer process becomes a
  visible TimeoutError, never a silent hang. The reply-path callers handle
  it explicitly (ledger write skipped + captured, audit trigger skipped,
  watchlist entry stays queued); a kill -9 test proves flock's kernel
  release + prompt recovery with consistent state.
- The signed decision log's shared daily file is now multiprocess-safe:
  appends serialize cross-process, and a wedged lock diverts the record to
  a unique overflow file instead of dropping it — pinned by a two-process
  concurrent-flush integrity test.

### Changed — Verification gate hardening (ADR 0005, amendment 5)

- **Structural output contracts are ON by default** (`OLYMPUS_CONTRACTS=off`
  is the kill switch) — enforcement never ships dormant.
- The verify stage runs under a wall-clock cap (`OLYMPUS_VERIFY_TIMEOUT`,
  default 600 s): a hung verifier takes the visible UNVERIFIED path instead
  of stalling the reply.
- An errored (as opposed to failed) rework ships degraded immediately — no
  retry loop on either rework path.
- Tests pin that fast mode cannot skip the answer.verify gate and that
  verdict parsing never crashes and never silently passes an invalid
  status.

## [0.25.0] — 2026-07-19

### Added — session & flow ergonomics (Hermes round 2)

- **Named sessions + browse/resume** — `olympus sessions` (list newest-first
  with a distilled-state preview line, pick to resume), `olympus -c` (continue
  the last session), `olympus chat --session <id>`, and `/new [name]` in chat
  (the old session stays resumable).
- **`/bg <task>`** — run a one-shot task through the full verified pipeline in
  the background while you keep chatting; the answer announces itself in-chat
  and is saved to reports.
- **`/btw <question>`** — an ephemeral side question: answered with the current
  context visible but leaving NO trace (no history, no memory extraction, no
  companion count).
- **`/model <name>` and `/fast on|off`** — swap the pool's primary model or
  toggle fast mode mid-session; `/model` keeps the provider/key/endpoint so a
  credential can never silently migrate hosts.
- **Delta-setup** — on a configured install, `olympus setup` offers "Fix what's
  missing", driven by `olympus doctor`'s ✗/⚠ gaps, and ends on the doctor
  summary (find → fix → confirm).
- **"Where stuff lives"** — doctor now prints the key paths (config, soul,
  memory, sessions, workspace) labeled editable vs managed.
- THREAT_MODEL: documented the no-/yolo stance — DENY-tier commands stay
  blocked even when approvals are granted.
- **Model picker type-to-filter** — aggregator-scale discovered lists (>20
  models) get a substring filter before the numbered pick.
- **Gateway checklist** — the wizard's messaging step now shows every channel
  (telegram/discord/slack/signal/email/webhook) with its configured status and
  lets you set up several in one pass.

### Security — teardown-loop hardening

- **Memory-write hardening** — `sanitize_for_memory` now strips invisible/
  bidi Unicode BEFORE the injection scan (closing the zero-width-split
  evasion) and redacts credential-shaped content (API keys, private-key
  blocks, JWTs, creds-in-URLs) so memory can never become an exfiltration
  channel.
- **Command-gate reinforcements** — wrapper-proof raw rule catches
  `bash -c "rm -rf /"` (payload hidden inside quotes), plus `find / -delete`
  and `shred` against block devices. Documented fail-closed trade-off: raw
  rules see inside quotes, so *printing* a catastrophic command is denied too.
- **Search-index maintenance** — the index opens in WAL mode; the heartbeat
  maintenance sweep prunes orphaned conversations (file deleted) and, only if
  `OLYMPUS_SEARCH_RETAIN_DAYS` > 0, aged ones — then VACUUMs. Conversation
  files are never touched; the index stays rebuildable via `reindex()`.
- **Email spoof-guard** — when `OLYMPUS_EMAIL_ALLOW` is set, the email gateway
  also requires Gmail's own DMARC/SPF+DKIM pass verdict
  (`Authentication-Results`); a From: header alone no longer satisfies the
  allowlist. Absent verdict = fail closed. Without an allowlist, behavior is
  unchanged (identity isn't load-bearing).

### Added — memory transparency & reach

- **`olympus memory card`** — one markdown page of everything believed about a
  user, every fact with live (decayed) confidence, type, age, and id; held
  candidates listed separately. A projection of the gated store — never an
  editable file.
- **Vault mirror** — `OLYMPUS_VAULT_DIR` write-through of lessons, reports, and
  corrections as dated markdown for curation in Obsidian/any editor. A mirror,
  not a second source of truth; a broken vault path never breaks a save.
- **Visible memory activity** — "🧠 remembered/updated/reinforced: …" progress
  lines as the background extractor gates facts in (all/verbose progress modes).
- **Soul scaffold** now seeds ## Role and ## Current focus; the wizard's
  closing hints point at `olympus soul edit`.
- **Search hit-set distillation** — oversized session-search results are
  condensed by the pool's fastest model (citations kept); keyless installs fall
  back to truncation.

### Security — teardown-loop hardening (iteration 3)

- **Webhook rate limiting** — the inbound webhook gateway now enforces a
  per-IP sliding-window limit (`OLYMPUS_WEBHOOK_RATE_LIMIT`, default 20/min;
  0 disables). A public entry point that runs the full council on the
  operator's key can no longer be turned into a key-burn DoS.
- **Rotation state thread-safety** — credential-rotation state (cursor,
  exhausted set, per-key stats) is now guarded by a lock, so concurrent
  gateway worker threads can't race the cursor into skipping or re-hitting a
  key.
- **Bounded background-thread registry** — `/bg` finished threads are compacted
  from the registry on each launch, so a long terminal session can't leak
  Thread objects.

### Added — Synthesis faithfulness check (ADR 0005, amendment 4)

- The composed answer is now verified too — the last unverified hop on the
  interactive path. A no-tools faithfulness check compares Zeus's reply
  against the already-verified findings via the new `answer.synthesis`
  contract: unfaithful → exactly one recompose with the unsupported
  additions named; still unfaithful → a structural `⚠️ UNVERIFIED
  ADDITIONS` banner. Streaming replies (whose tokens can't be retracted)
  get a trailing correction note instead. Checker infrastructure failures
  are traced but never bannered — the findings themselves were verified.
  Skipped in fast mode; `OLYMPUS_SYNTH_CHECK=off` kill switch.

### Fixed — Second-ring cross-process safety (ADR 0005, amendment 3)

- The acceptance re-audit swept the tree for the heartbeat-vs-web RMW class
  and found six more shared files; all are now locked and/or atomic:
  agent beats (`run_due` marks due beats under the lock BEFORE the
  minutes-long LLM phase — an add from the chat process mid-run is no
  longer silently deleted), operator jobs (same mark-first restructure),
  todos, the verified-facts cache (append+trim serialized cross-process
  with a bounded wait on the verify agent's tool path), heartbeat state,
  saved conversations, and the conversation-counter reset. Each carries a
  two-process or lock-contention race test.

### Added — Deterministic difficulty pre-scorer (ADR 0005, Phase 3)

- New `olympus/effortscore.py`: a pure, deterministic scorer — (risk class,
  prompt length, tool count, retry index, needs_verification) → effort tier,
  zero model calls, zero I/O. Thresholds are plain constants, deliberately
  not evolve-tunable.
- The static `effort=` literals in routing, planning, synthesis, and every
  specialist run are replaced with scored values; the per-specialist
  `effort` field is now a FLOOR the scorer can raise but never lower below
  ("high" remains synthesis's floor).
- A rework runs at the top tier (`retry_index >= 1` → high) on BOTH rework
  paths (Athena quality retry and the Aletheia answer.verify forced rework).
  On a single-model pool — where `teacher_for` has no stronger member to
  swap in — this same-model-more-compute bump IS the escalation, traced as
  `teacher.effort_escalated` only when it genuinely changes the call.
- A 10-agent adversarial review then made the dial REAL rather than
  cosmetic: a second prompt-length tier makes "high" reachable from length
  alone (routing included); the tool signal counts only a specialist's
  EXTRA tools (the 7 shared BASE tools had put most of the roster at the
  threshold); `risk_class` is genuinely wired — a specialist holding an
  irreversible/financial action tool always thinks at the top tier; and
  three light specialists (Iris, Mnemosyne, Chiron) now floor at "medium",
  so a cheap path exists in production — backstopped by the enforcing
  answer.verify gate, Athena review, and the retry→high rule. Accepted:
  replay recordings are version-bound — a behavior-changing release
  invalidates old recordings by design (the divergence is the tripwire).

### Added — Cross-process safety on shared mutable state (ADR 0005, Phase 2)

- New `olympus/proclock.py`: `fcntl.flock` lockfile wrapper — cross-process
  AND cross-thread exclusive, reentrant per thread, POSIX-only with a
  documented degraded fallback. The heartbeat process and the web process no
  longer race each other's read-modify-writes.
- Usage ledger RMW (`usage.record`) now holds the cross-process lock —
  `os.replace` prevented torn files but not lost updates; a two-real-process
  race test asserts exact totals.
- `memory.save` filenames are collision-proof (pid + `O_EXCL` create):
  concurrent same-title writers each keep their note; the 14-digit timestamp
  prefix is unchanged so date-parsing readers keep working.
- `watchlist_add`/`watchlist_pop` and every mutating goals load-modify-save
  cycle now serialize under the cross-process lock.
- `FileStore.put` last-writer-wins is documented as the accepted KV contract;
  cross-process RMW callers must hold `proclock` (the Postgres upsert is a
  blind overwrite, not a CAS — see ADR 0005).
- A 22-agent adversarial review of this phase confirmed 18 findings — all
  fixed in-phase: lock scopes split so no in-process mutex is ever held
  across a flock wait (a wedged peer could have frozen every reply);
  proclock gained a bounded-`timeout` acquire for hot best-effort paths and
  sanitized-name reentrancy identity; atomic publish added everywhere a
  torn read decodes as empty (goals, scheduler jobs, FileStore.put, prefs,
  watchlist, note bodies via `os.link`); proclock coverage extended to
  evolve's telemetry/tunables blob (whose torn read could have reset
  tighten-only security knobs), the scheduler, prefs, and the conversation
  counter; the goals completion write re-checks active status under the
  lock; prompt-backup restore matches collision-suffixed filenames.
- Per-worker sandbox scratch re-rooting was built, adversarially reviewed,
  and REJECTED: a context-sensitive workdir made approved file actions
  execute in a different root than they were previewed in, and broke
  file handoff, the gallery, and pre-existing workspaces. `workdir()` stays
  one shared, context-free root; the residual concurrent same-path write is
  documented as accepted in ADR 0005.

### Added — Aletheia is ENFORCING on the interactive path (ADR 0005, Phase 1)

- `_verify` now emits a structured verdict — `{status: pass|warn|reject,
  unsupported_claims[], confidence}` — parsed from a mandatory machine-readable
  `VERDICT:` line (missing/malformed = infrastructure failure, handled
  visibly, never silently).
- New `answer.verify` behavioral contract evaluated AFTER verification and
  BEFORE synthesis, feeding the real verdict to the previously dormant
  `aletheia_verified` predicate (which was never given a `verify_verdict` in
  production and ran a stage too early).
- Policy: an affirmative `reject` forces exactly one rework of the council;
  a second reject ships the reply hard-downgraded behind a structural
  `⚠️ UNVERIFIED` banner (prepended after synthesis on both the blocking and
  streaming paths, so the composing model can never drop it), with the
  unsupported claims listed and the event recorded in the signed decision log.
- Verify-stage infrastructure errors now degrade visibly (banner + logged
  decision) instead of silently falling through to raw findings.

### Changed — Hardening + self-evolution pass over the integration-depth components (D1–D8)

The eight new components (webplan, AP2 flow, extended ABC surface,
scaffold_evolve, dytopo, emem, a2a, liveeval) are hardened and made
self-evolving within guardrails.

- **Self-evolution.** Five new non-security `evolve` tunables, each with a hard
  `[lo,hi]` clamp: `treesearch.max_nodes` [10,100], `dytopo.max_out_degree`
  [1,3], `emem.max_fragments` [4,12], `liveeval.sample_size` [10,50],
  `scaffold_evolve.max_archive` [50,200]. Each feature reads its tunable at the
  I/O boundary via `evolve.current(...)` (fail-safe fallback to the default) and
  records OK/DEGRADED outcomes + structured `log_event` telemetry. **No
  security-relevant knob is tunable** — the side-effect halt, egress guard,
  allowlist/denylist, signing, and approval gates stay hard constants outside the
  tuner; a drift test asserts the D-component tunables are never `tighten_only`.
- **Hardening.** The five new modules are pyflakes-clean with no bare `except`,
  silent catch (only best-effort telemetry is swallowed), TODO, or debug print.
  Adversarial tests added at every external boundary: A2A oversize-reply capping
  + malformed peer reply/card, scaffold refusal of every security module,
  tree-search runaway (node cap) + webplan never-applies-a-side-effect,
  dytopo dense-input-stays-sparse, and liveeval malformed/huge-trace tolerance.
- **Poisoned-feedback gate.** A poisoned emem fragment (injection text) is kept
  verbatim but leaves the module **only inside the untrusted envelope** — never a
  clean instruction; and a liveeval regression signal is proven to carry only
  numeric aggregates + run ids, never injection-laden trace content.

### Added — Live-trace online evaluation (`liveeval`)

Sampled quality scoring of recent runs, so a regression surfaces on its own
instead of after a complaint. Live-eval reads a bounded sample of recent signed
decision logs (`trace.py`) and scores each with cheap rule-based scorers.

- **New `olympus/liveeval.py`.** Pure scorers over a run dict — no contract
  violation, no errored decision, verified (a review decision that passed; direct
  answers pass vacuously), within latency, within cost. `score_run` / `evaluate`
  are deterministic; sampling is a fixed stride (the most recent N, capped). An
  LLM-judge scorer is pluggable and off unless supplied; a scorer that raises is
  skipped, never fatal.
- **Regression signal.** `run()` records the pass-rate to feature-evolution
  telemetry (OK ≥ 0.9, else DEGRADED) and the structured evolution log; a drop
  is visible on the `evolve` board and the admin panel (`report()`).
- **Opt-in, bounded, read-only.** `OLYMPUS_LIVE_EVAL` off by default; wired into
  the heartbeat + hibernation cadence (only wakes when enabled) and surfaced via
  **`olympus liveeval`**. It reads traces, never modifies them; the sample size
  is hard-capped and only a week of daily trace files is scanned.
- Tests (15): each scorer, aggregation, deterministic/bounded/most-recent
  sampling, corrupt-line-tolerant reader, disabled-by-default, and the regression
  report.

### Added — A2A agent card + governed task client (`a2a`)

Agent-to-agent interoperability, built over what Olympus already exposes, with no
DID/verifiable-credential stack and no new dependency.

- **Agent card.** `a2a.card()` builds an A2A-style discovery document
  (`/.well-known/agent.json`-shaped) from the live capabilities manifest —
  identity, protocol, capability counts, specialist skills, endpoints. Pure.
  Printable via **`olympus a2a card`**.
- **Inbound task mapping.** `parse_task` normalizes an A2A task envelope
  (message/parts or `{input}`) into a `Task`; `to_internal_request` wraps the
  peer's message in the **untrusted-data envelope** before it can reach the
  council — capability separation at the boundary. `task_response` builds the
  result envelope.
- **Governed outbound client.** `call_agent(url, message)` and `fetch_card(url)`
  are opt-in (`OLYMPUS_A2A`, off by default) and every outbound URL passes the
  existing SSRF/egress guard first (loopback, link-local, cloud-metadata,
  sovereign allowlist all refused). A peer's reply is returned **wrapped as
  untrusted** — a remote agent's output is never trusted as instructions. The
  fetcher is injectable (stdlib `urllib` by default) so the core is testable
  without a network, and responses are size-capped.
- Tests (13): card from manifest + live, inbound parse/validation, peer message
  and reply both enveloped, outbound disabled-by-default, SSRF-blocked, HTTP-error
  tolerant.

### Added — E-mem episodic memory reconstruction (`emem`)

An on-demand, **non-destructive** alternative to lossy summarization (arXiv
2601.21714), added *alongside* the existing retrieval + compaction paths (not in
place of them). Given a query, E-mem gathers the raw fragments related to it —
event-log entries, typed memories with their provenance, and conversation
snippets from the FTS5 index — and reconstructs a **chronologically-ordered,
provenance-tagged episode**. Fragments are selected and time-ordered, never
summarized or rewritten, so the reconstruction stays faithful and attributable.

- **New `olympus/emem.py`.** The reconstruction core (`reconstruct(query,
  fragments, now=)`) is pure and deterministic — relevance-score, budget-select,
  then assemble in time order — with `now` injected so it is replay-safe and
  testable without I/O. A thin best-effort `gather` reads the existing
  `usermem` + `search` substrate.
- **Non-destructive + attributable** — fragment text is byte-identical to the
  source (proven by test); `Episode.provenance()` attributes every fragment to
  its origin.
- **Enveloped** — `Episode.render()` leaves the module only through
  `security.wrap_untrusted(source="episodic-memory")`, since a reconstruction may
  include conversation snippets that originated from tools/web.
- **Opt-in** — `OLYMPUS_EMEM` off by default; `context_block` is a no-op unless
  enabled. Bounded by fragment-count and char-budget caps.
- Tests (14): relevance floor, chronological ordering, verbatim (non-destructive)
  text, determinism, provenance, envelope, and the caps.

### Added — DyTopo dynamic topology routing (`dytopo`)

An optional, runtime-induced collaboration graph for the specialist council
(arXiv 2602.06039). Each specialist emits a natural-language `query` (what it
needs) and `offer` (what it provides); `dytopo.induce` matches those descriptors
to wire a **sparse directed graph** — so the specialists that genuinely have
something for each other are connected for a bounded number of consultation
rounds, instead of a fixed or all-to-all shape.

- **New `olympus/dytopo.py`** — the pure core: descriptor matching → graph →
  rounds. Deterministic (token-overlap similarity, stable tie-breaks — no
  embeddings, clock, or rng), so the same descriptors always induce the same
  topology (replay-safe).
- **Bounded by construction** — hard caps on nodes, out-degree, total edges, and
  rounds; a self-edge is never created and the induced graph is provably sparse.
- **Opt-in** — `OLYMPUS_DYTOPO` is off by default; the fixed
  Zeus→Athena→specialists→Aletheia pipeline stands unless an operator turns it
  on. The existing per-specialist governance is unchanged (this only decides who
  consults whom).
- Tests (13): edges reflect query→offer matches, induction is order-independent
  (deterministic), out-degree / node / edge / round caps hold, and threshold
  filters weak edges.

### Added — Governed scaffold evolution (propose-only; ADR 0003)

Adopts the Darwin Gödel Machine idea (measured, archived, benchmark-gated
self-improvement of code) with the dangerous part removed. **There is no code
path that writes to Olympus's own source tree, and no `apply()` function** — the
running agent never modifies itself.

- **New `olympus/scaffold_evolve.py`.** `propose(module, generate, benchmark=)`
  generates a candidate patch, benchmarks it in isolation (must at least
  `compile()`; a pluggable benchmark may run more, written only to a throwaway
  temp path), archives the variant, and returns it. It never touches the real
  module (proven by a test that the source is byte-identical after a propose).
- **Non-security modules only, fail-closed.** A curated `_EVOLVABLE` allowlist
  plus an independent `_SECURITY_MODULES` denylist; `propose` on a security
  module (`security`, `cmdguard`, `actions`, `behavioral_contracts`, `mandate`,
  `witness`, `vault`, `egress`, …) raises. Unknown ⇒ not evolvable.
- **Governed by ABC.** The new `scaffold.propose` contract (target evolvable +
  candidate compiles + benchmark passed) decides whether a candidate is a
  *surfaceable* proposal; a failing candidate is archived as a failed variant but
  never surfaced.
- **Surfaced as diffs; nothing auto-applies; off by default.**
  `olympus scaffold-evolve proposals` renders each valid proposal as a unified
  diff for a human to apply by hand; `OLYMPUS_SCAFFOLD_EVOLVE` gates whether the
  engine runs at all. Every proposal also lands in the structured evolution log.
- Tests (17) are the safety proof: security modules unreachable, no apply
  path exists, the real tree is never modified, failing/empty/non-compiling
  candidates are excluded, and the contract blocks each bad input.

### Added — Extended ABC contract surface (memory commit, skill import, goal completion)

Three more governance chokepoints become formal Agent Behavioral Contracts,
binding each declarative rule to the existing enforcer as defense in depth.

- **`memory.commit`** (recovery `hold`) — a durable memory auto-commit
  (`recall._gate`) must be injection-sanitized (`memory_content_sanitized`) and
  not high-sensitivity (`memory_not_sensitive_autocommit`); a violation **holds**
  the candidate for the user instead of committing it.
- **`skill.import`** (recovery `block`) — an imported skill must pass the
  security scan (`skill_scan_clean`); `skillpack.import_file` now refuses through
  the contract.
- **`goal.complete`** (recovery `block`) — a standing goal may be marked done
  only against concrete evidence at the confidence floor (`goal_evidence_present`);
  an evidence-free "done" is refused at `goals.judge`.

Predicates bind to the real enforcers (`security.looks_like_injection`, the skill
scan, the goals evidence doctrine), the YAML mirror and embedded fallback stay in
sync (drift-tested), and block-path tests prove each contract refuses a bad input
at its wired chokepoint.

### Added — AP2 mandate user-facing flow (`authorize_payment`, no rail)

The mandate primitives (ADR 0001) gain a real authorization flow on the action
spine (ADR 0004) — still with **no live payment rail; no money moves.**

- **New `authorize_payment` action** (`FINANCIAL_LEGAL`, scope
  `payment.authorize`). Because it is `FINANCIAL_LEGAL`, `_min_level_to_auto` = 99
  — it can **never** auto-run at any autonomy level; it is always prepared and
  waits for explicit human approval. The preview is a plain-language summary of
  the exact bounded authorization (amount, cap, allowed merchants, item, expiry)
  and states outright that it moves no money.
- **The approval IS the signing event.** On approval, `execute` builds the
  intent + cart, applies the system signature AND the **user co-signature**
  (`mandate.co_sign`), runs `mandate.enforce_commit` — the `payment.mandate` ABC
  contract (intent-containment, non-expiry, fresh nonce, valid signature, user
  co-signature, capability-within-bound; recovery `block`) — and only then
  records the verified mandate. A spoofed / over-cap / wrong-merchant / expired /
  replayed / un-co-signed mandate fails the action closed and records nothing.
- **New `olympus/mandate_store.py`** — an append-only, bounded per-user record of
  issued mandates plus the set of consumed nonces (replay defense); every record
  carries `moved_money: false`.
- Tests (9): never auto-runs even at L4; approval signs + verifies + records with
  no money moved; over-cap and wrong-merchant carts fail closed and record
  nothing; the store is append-only, replay-safe, and corrupt-blob tolerant.

### Added — Tree search as a live browser planner (`webplan`)

Best-first tree search stops being a library the code *could* call and becomes a
live planner over the governed browser harness. New `olympus/webplan.py` plans a
read-only path from a starting page toward a goal — navigating (a reversible GET
+ back) and perceiving, scoring each page, backtracking — and **never** auto-taks
a side-effectful step (click/type/submit): those are withheld and handed to the
approval spine via `treesearch.to_approvals`.

- Pure, injected core (`explore(start, navigate=, perceive=, score=)`) so it is
  fully testable with fakes; `plan_with_browser` adapts a live `browser.Browser`
  using only read-only verbs (`open`, `read`, `_eval` for title/same-origin
  links). It never clicks or types.
- Bounded by `treesearch.SearchCaps` (nodes / tokens / wall-clock / depth), so a
  runaway crawl is structurally impossible; a dead link drops that branch.
- Tests prove it finds the goal page read-only, withholds every side-effectful
  step for approval, and — via a fake Browser mirroring the real API — that the
  planner never invokes `click`.

### Added — Phase 3 evolution governance (structured logs, diffs, gate inventory)

ACE and the sleep-time loop are the self-evolution layer, so their behaviour is
now governed the way the loop spec demands: observable, propose-first, and
gated.

- **Structured evolution log.** `evolve.log_event(feature, kind, fields)` +
  `evolve.events(feature, limit)` — a bounded, machine-readable event log on the
  same store substrate as feature telemetry. ACE compaction now emits its delta
  counters (version, bullets, pinned, added, pruned, helpful, harmful) and each
  sleep-time cycle emits its rewrite metrics (proposed, committed, rejected,
  clean, clean_cycles, graduated, autoapply) as structured events, queryable via
  **`olympus evolve log [feature]`** (JSONL output).
- **Destructive changes are proposed as diffs.** `sleeptime.render_diff` renders
  a proposal as a unified diff — the source memories it would supersede against
  the consolidated rewrite, flagged verified/UNVERIFIED — and
  `olympus sleeptime proposals` now prints diffs instead of raw JSON.
- **Nothing auto-applies; the gates are inventoried.** New governance tests pin
  the invariants in one place: an ungraduated loop never commits even with
  `OLYMPUS_SLEEPTIME_AUTOAPPLY=1`; a graduated loop still needs the explicit
  opt-in; payment mandates can never auto-run (`_min_level_to_auto` = 99); tree
  search only PREPARES actions; every security-relevant tunable is registered
  tighten-only; ACE pinned facts survive prune pressure.

### Security — Phase 2 hardening of the 2026-landscape components

A hardening pass over ACE, ABC, sleep-time, tree search, and AP2 mandates —
each checklist item proven by a test or a quoted tool result.

- **Poisoned-feedback defense (headline).** Malicious "execution feedback" can no
  longer survive into durable state un-neutralized. ACE already sanitized added
  bullets and rendered them only inside the untrusted envelope; **sleep-time now
  also runs `security.sanitize_for_memory` on a rewrite before it can become a
  memory** (on both the proposal and auto-apply paths) — a lexical gate in front
  of the Aletheia semantic gate. New tests prove an injection echoed by the
  Generator is defanged at rest and, for ACE, only ever leaves the engine
  enveloped.
- **Explicit failure handling.** `ace._bullet_cap` now catches the specific
  read/parse errors and fails safe to the hard ceiling (never unbounded) instead
  of a broad swallow. No bare `except`, no silent catch in any of the five
  modules.
- **Dead code removed.** Cleared unused imports flagged by pyflakes in
  `behavioral_contracts.py`, `treesearch.py`, and `mandate.py`; the five modules
  are pyflakes-clean.
- **Adversarial coverage confirmed.** Mandate-spoofing (unsigned / wrong-key /
  tampered-field), tree-search runaway (node cap) and budget-exhaustion (token
  cap), and ABC contract-violation paths all have passing tests; input-validation
  boundaries (invalid user, cart-without-intent, non-mandate input) are covered.

### Added — AP2-style payment mandates (creation + verification only; no rail)

A verifiable-authorization primitive for agent-initiated commerce (Google AP2):
a **signed, constraint-bound, tamper-evident record that a human authorized a
specific, bounded financial action** — replacing the status-quo "approved: true"
flag with something a third party could verify. Preceded by an ADR + threat
model (`docs/adr/0001-ap2-payment-mandates.md`, `docs/AP2_THREAT_MODEL.md`),
approved before any code.

- **New `olympus/mandate.py`.** `IntentMandate` (user-authorized constraints:
  amount cap + currency, merchant allowlist, item, expiry, nonce) and
  `CartMandate` (the concrete cart), with `create_intent` / `create_cart` /
  `sign` / `verify` and a pure `contained()` intent-containment check.
- **No live rail, no new dependency.** No payment rail, card/VC issuance, or
  PSP/merchant network calls exist in this phase — a mandate authorizes nothing
  to move money. Signing reuses the Ed25519 root of trust via a new
  **domain-separated subkey** (`witness.sign_with`/`sub_public_key_hex`, label
  `mandate/v1`) — a key distinct from the release/decision-log key, same custody
  and sovereign-mode fail-closed.
- **Mapped to the autonomy dial.** A payment mandate is `FINANCIAL_LEGAL` risk →
  `actions._min_level_to_auto` = 99 → it can **never** auto-execute at any
  autonomy level (`mandate.can_auto_execute()` is structurally `False`). The
  mandate is the artifact the human produces at the approval step, not a bypass.
- **Governed by ABC.** The new `payment.mandate` contract (preconditions:
  intent-containment, non-expiry, fresh nonce; governance: valid signature,
  trusted construction; recovery `block`) refuses a spoofed, tampered, replayed,
  expired, over-cap, or injection-constructed mandate via `enforce_commit`.
- **Adversarial tests.** Spoofing (unsigned / wrong-key / tampered-field),
  construction-injection (untrusted intent unsignable, over-cap / wrong-merchant
  cart rejected), replay (nonce reuse + expiry), and escalation (never
  auto-runs) — all covered.

### Added — Best-first tree search (a governed runtime planner)

A runtime planner that explores, scores, and backtracks over candidate action
sequences — inference-time tree search (arXiv 2407.01476), complementary to the
existing DAG orchestration and layerable onto the governed browser harness —
with safety built into the engine rather than bolted on.

- **New `olympus/treesearch.py`.** Generic best-first search over a duck-typed
  `Problem` (expand / apply / evaluate / is_goal). The clock is injectable so
  tests are deterministic with no real waiting.
- **Exploration touches only READ-ONLY or REVERSIBLE steps.** The engine never
  calls `apply()` for a side-effectful step — it cannot mutate the world while
  planning (proven by a Problem whose `apply` raises if ever handed one).
- **Side-effectful steps halt for approval.** Each is withheld on
  `pending_approval` and can be handed to the approval spine via `to_approvals`,
  which PREPARES actions (never executes). A plan that can only advance through a
  side-effectful action ends `halted_for_approval`.
- **Hard caps bound every search** — nodes, tokens, wall-clock, and depth
  (`SearchCaps`, env-configurable via `OLYMPUS_TREESEARCH_MAX_*`), clamped to
  strictly-positive minimums so a zero/negative cap can't disable a guard.
  Whichever trips first stops the search and returns the best node so far.
- **Fail-closed classification.** `classify_browser` maps perception verbs to
  read-only and navigation to reversible; any unknown verb (or a classifier that
  errors) is treated as side-effectful and withheld. Per-search outcome recorded
  to feature-evolution telemetry.

### Added — Sleep-time memory refinement (idle-time consolidation, earns its autonomy)

A Letta-style **idle-time** loop that reviews a user's typed memory during
downtime and proposes refinements (consolidating near-duplicate memories), so
future recall is cleaner without a live turn paying for it — with every safety
property made structural.

- **New `olympus/sleeptime.py`.** Selection (which memories to consolidate) is
  pure and deterministic; the two model-backed steps — generate the consolidated
  memory, and verify it — are pluggable (tests run with no network), mirroring
  `ace.py`.
- **Reversible + versioned.** A rewrite never destroys its sources: they are
  `supersede()`d (kept as history) and an **append-only snapshot** of their
  pre-state is written. `olympus sleeptime revert <snapshot>` restores the
  originals exactly and tombstones the rewrite.
- **Aletheia-gated.** A consolidation is verified before it can commit — it may
  assert nothing its sources don't support. An unverified rewrite is never
  committed and **resets the trust streak**.
- **Provenance/trust preserving.** A consolidated memory inherits the **union**
  of its sources' provenance and their **strongest** sensitivity — it can never
  launder a high-sensitivity fact into a normal one, nor drop provenance.
- **Governed by ABC.** Every commit passes the new `memory.rewrite` behavioral
  contract (preconditions `rewrite_preserves_provenance` +
  `rewrite_preserves_trust`, governance `rewrite_verified`, recovery `block`), so
  the three properties above are enforced at the contract layer too.
- **Off by default; earns autonomy.** `OLYMPUS_SLEEPTIME` is off. Even enabled,
  the loop runs **supervised** — proposing reversible diffs, committing nothing —
  until it logs `SLEEPTIME_GRADUATION` (10) clean cycles; auto-apply additionally
  requires `OLYMPUS_SLEEPTIME_AUTOAPPLY`. Wired into the heartbeat/hibernation
  cadence (only wakes when enabled) and surfaced via `olympus sleeptime` +
  the admin panel; per-cycle metrics recorded to feature-evolution telemetry.

### Added — Agent Behavioral Contracts (runtime Design-by-Contract governance)

Governance rules that were scattered across the action spine, the autonomy dial,
capability separation, and Aletheia verification are now expressed as formal,
declarative **behavioral contracts** — `C = (Preconditions, Invariants,
Governance, Recovery)` — and enforced at runtime. Native implementation; no
AgentAssert or any new hard dependency.

- **New `olympus/behavioral_contracts.py`** + **`behavioral_contracts.yaml`.**
  Contracts are authored in YAML and loaded at runtime with `yaml.safe_load`
  when PyYAML is importable (as `sandbox.py` already does); an embedded mirror of
  the same defaults is the fallback when it is not, so the guarantees never
  vanish for want of a parser (a drift test pins the two equal). A contract that
  names an unknown predicate or recovery **fails to load** — a governance rule
  never passes vacuously.
- **Every existing invariant is a contract.** `action_execution` (approval spine
  + autonomy dial), `specialist_output` (output contract + Aletheia
  verification), and `tool_loadout` (capability separation) ship as defaults,
  each with pure, individually-tested predicates.
- **Violations block and trigger Recovery.** `enforce()` raises
  `ContractViolation` carrying the failing clause and a recovery directive —
  `BLOCK` (fail closed), `HOLD` (revert to awaiting-approval), `REJECT` (drop the
  output), or `DEGRADE` (record + allow). The `action_execution` contract is
  wired at `actions._execute`, the single execution chokepoint: any path that
  reaches it with an irreversible/financial action that never earned a *genuine*
  human approval (a real `approved_at`, not a flipped flag) is blocked and the
  action is **held** for the human — defense in depth behind the imperative
  spine. `specialist_output` is wired at the orchestrator's output-acceptance
  point.
- **Fail-open on engine error, fail-closed on violation.** A bug inside a
  predicate degrades to "ok" (the primary spine remains the real guard); only an
  evaluated contract violation blocks. `OLYMPUS_ABC=off` is the kill switch, and
  the enforcement mode is recorded into the run trace so replay reproduces it.
  ABC status appears on the operator admin panel.

### Added — ACE delta-context engine (evolving playbook replaces monolithic compaction)

Conversation compaction stops re-summarizing the whole history from scratch and
instead evolves a durable **playbook** by incremental *delta* — the pattern from
*Agentic Context Engineering* (ACE, arXiv 2510.04618). This kills the two decay
modes of lossy rewrites: **context collapse** (a summary of a summary of a
summary, eroding turn over turn) and **brevity bias** (the summarizer quietly
dropping whatever it deems least important).

- **New `olympus/ace.py`** implements the three ACE roles. *Generator* proposes
  candidate bullets from the slice of turns being folded away (the only
  model-backed step; routed through the frozen `backend.complete_json`, so it
  replays deterministically, and pluggable for tests). *Reflector* scores
  existing bullets against the new slice (helpful++ / harmful++). *Curator*
  deterministically merges the delta — sanitize, dedup, pin-preserving prune,
  bounded size — in pure Python.
- **Durable facts are pinned and never lost.** Bullets in the `facts`/`decisions`
  sections auto-pin; pinning is sticky and prune-proof, so the non-pinned size
  cap and the harmful-vote prune can never evict a pinned fact. Pinned facts
  dedup by exact normalized-text identity (never fuzzy-merged), so two distinct
  facts that differ only by a number or a short word are never conflated. A
  50+ turn replay test proves zero loss of pinned facts across repeated
  compaction under prune pressure and a process reload.
- **`orchestrator._compress_history` runs delta-only by default.** It loads the
  per-conversation playbook, evolves it, persists it (`memory/playbooks/`), and
  renders it as the conversation-state block. The legacy monolithic summarizer
  is retained behind `OLYMPUS_ACE=off` as a kill switch and as the automatic
  fallback if the Generator fails.
- **Playbook content enters prompts only as DATA.** The rendered block leaves
  `ace.py` exclusively through `security.wrap_untrusted(source="playbook")`, and
  every added bullet is first run through `security.sanitize_for_memory`, so an
  injection retrieved from a web tool cannot smuggle an imperative into the
  durable state block.
- **Self-evolution aware.** The non-pinned size cap is a registered `evolve`
  tunable (`ace.max_bullets`, hard ceiling 60, tuner may only narrow), and each
  compaction records its delta counters (version, bullets, pinned, added,
  helpful/harmful) to feature-evolution telemetry.

### Added — Browser/operator absorbed into feature self-evolution (tighten-only)

The operator and earned-autonomy line becomes a first-class citizen of
`evolve.py`'s measure→auto-tune loop — self-monitoring *and* self-correcting
toward caution, with no way to self-escalate.

- **`operator.execute` now records OK / DEGRADED / FAIL** to feature evolution
  (feature `operator`): a clean run is OK, a self-healed run is DEGRADED, and a
  checkpoint hand-off / drift / missing-success-marker is FAIL (with detail). The
  operator's execution health now appears on the `evolve` health board.
- **Earned-autonomy policy knobs are now self-tuning — but TIGHTEN-ONLY.** Three
  security-relevant tunables (`operator.establish_after`, `operator.cooldown_secs`,
  `operator.daily_ceiling`) auto-adjust when the operator degrades: the trust bar
  rises, the post-surprise cooldown lengthens, and the daily auto-run ceiling
  drops — so a failing actuator *narrows its own freedom* with no human in the
  loop. `trust.py` reads the live values, so a once-established site can be
  demoted automatically until it re-earns trust.
- **The tighten-only guarantee is structural.** A new `Tunable.tighten_only` flag,
  validated at registration (the default must sit at the loose bound, `on_fail`
  pointing at the tight bound), plus a defensive clamp in `review()`, make it
  impossible for the reviewer to ever *loosen* a security knob — auto-tightening a
  trust gate is safe (worst case: ask a human more often), auto-loosening never
  is. Only a human widens it back, via **`olympus evolve reset [feature]`**
  (new `evolve` action; restores tuned params to defaults, telemetry preserved).

### Added — Earned per-domain autonomy (moat, self-evolving)

Freedom without a blanket blank cheque: Olympus stops asking permission for
safe, reversible actions on a site it has *already proven itself on*, while a
human stays at the one gate that can't be walked back.

- **`olympus/trust.py`** grades a **domain's** trust from Olympus's own witnessed
  action history. Trust is **earned slowly** (an unbroken run of clean, governed
  successes on that exact domain) and **snaps back fast** — a single surprise
  (a failed run, a reversal/undo, a rejection, or a human-verification checkpoint,
  all of which land as non-success in the immutable audit log) resets the domain
  to zero. Tiers: `probation` → `trusted` (5 clean runs) → `established` (20).
- The score is a **pure function of the append-only audit log** — there is no
  mutable trust counter for a prompt-injected agent to inflate.
- Two hard invariants keep it inside the moat: (1) earned trust can only ever
  widen auto-execution for **reversible** actions — the approval gate on
  irreversible / financial / legal actions is never touched (min-to-auto stays
  99); (2) the boost is always **re-capped by the conversation's capability
  profile**, so an ingesting or guest run can never be lifted by it.
- Two runaway guards on top of the streak, both of which fall back to asking
  (never fail an action): a **post-surprise cooling-off window** (a site that
  just surprised us must settle for an hour before it can re-earn trust, so a
  compromised session can't fail-then-rapidly-succeed to re-arm unattended
  auto-run) and a **per-domain daily auto-run ceiling** (a proven site still
  can't fire an unbounded number of unattended actions in a day). Both are
  surfaced in the `operator_trust` report.
- Wired through the spine: `actions.can_auto_execute` / `auto_or_hold` take an
  optional earned level; `operator.run` computes the domain's effective level.
- **`operator_trust`** (read-only tool) and `olympus earned-autonomy [on|off]`
  (CLI) surface and toggle the ladder; the operator review folds in a hint about
  which proven sites have earned auto-run. OFF by default — opt in per-user or
  instance-wide with `OLYMPUS_EARNED_AUTONOMY=1`.

### Added — feature self-evolution (`olympus/evolve.py`)

- **Capabilities that measure and improve themselves.** Every instrumented
  feature (MoA, goals, curator, /learn, browser_open) records success /
  degraded / failure outcomes; a daily heartbeat review
  (`OLYMPUS_FEATURE_EVOLUTION_EVERY`, also `olympus evolve review`) computes
  per-feature health and **auto-tunes only registered, non-security
  parameters within hard `[lo, hi]` guardrails, reversibly** — MoA narrows
  its reference-model fan-out when the ensemble is flaky; the goal cadence
  backs off for goals that keep failing to close; the curator prunes more
  cautiously when the benchmark keeps reverting its prunes. Anything not
  safely auto-tunable is *surfaced as a suggestion*, never imposed, and no
  egress/auth/capability setting is reachable by the reviewer (enforced by
  test). Health board on the admin panel and `olympus evolve status`.

### Hardening

- Goal text/contract are length-bounded on add; MoA telemetry-drives its own
  fan-out; the curator's hard prune cap is now an absolute ceiling the tuner
  can only narrow within.

### Added — browser harness advances (beyond the open-web pattern's ceiling)

- **In-page sub-resource egress enforcement.** The egress gate previously
  covered only the top-level navigation; a loaded page's own sub-resource
  requests (`fetch`/XHR, `<img>` beacons, tracking pixels) were ungated.
  `Network.setBlockedURLs` (from the new `security.subresource_block_patterns`)
  now blocks sub-resource requests to metadata hosts and IP-literal
  private/loopback/link-local targets at the network layer, installed before
  the first navigation. Honest limit documented: string-pattern matching, so
  the navigation gate's resolve-time IP check still guards the top-level
  document; this is defense-in-depth beneath it.
- **Accessibility-tree perception** (`browser_read_ax`). Reads the page's AX
  tree (role + accessible name per node) — far more resilient to CSS/DOM
  redesigns than selectors and cheaper than a screenshot. Untrusted,
  blocked-landing-guarded, node/label capped.
- **Verifiable capture.** `browser_save_pdf` prints the current page to a PDF
  in the workspace (durable evidence of a confirmation/receipt for the
  approval + goal-verification loops; blocked-landing-guarded, path-confined).
  `browser_console` returns the page's captured console messages (real
  debugging signal; page-controlled text treated as untrusted).

### Added — Verifiable attestation receipts (moat, outward-facing)

The signed human-attestation becomes a shareable trust artifact.

- **`operator_attest_receipt`** exports a human-cleared attestation as a portable,
  human-readable **receipt** (facts + signer public key + signature; nothing
  secret). **`operator_verify_receipt`** verifies a pasted receipt: signature
  validity, and — with the expected pinned key (`OLYMPUS_ATTEST_PIN`) — whether
  it was signed by the trusted signer. Tampering any field (domain, kind, time)
  fails verification.
- This is the check a **third party** runs, holding the key out of band — so
  Olympus can *prove to someone else* that a human cleared a specific
  verification on a specific site at a specific time. First-party crypto tools
  (read-only; a receipt is crypto-verified, never executed).

### Added — Governed cross-origin frame *acting* + hardening (moat)

Completes the governed crossing with the write half.

- **`browser_frame_act`** — click / type / select / press INSIDE a cross-origin
  frame (by index), permitted **only if the frame's origin is an authorized
  operator site** (same default-deny per-origin gate as `browser_frame_observe`).
  Selector-based verbs only (the form interactions a payment/login frame needs);
  typed text is never journaled; landed steps ARE journaled (with an "in frame"
  marker) so a proven cross-frame flow can be learned like any other — the moat
  self-evolves. Operator-gated, capability-separated.
- **Hardened:** `list_frames` now lists only frames with a real, loaded
  `http(s)` origin — an `about:blank` / `chrome-error://` / unloaded sub-frame
  has no authorizable origin and is skipped, so it can't be mis-authorized or
  driven. (Note: a fully *loaded* cross-origin frame's content can't be
  demonstrated in a sandbox whose Private-Network-Access/proxy policy blocks
  iframe loads; the OOPIF attach + sessionId routing the frame ops ride is
  verified against real Chromium at the transport level.)

### Added — Governed cross-origin frames (moat)

The second boundary turned into a moat: reach *into* a cross-origin (third-party)
iframe — but **only under per-origin authorization**, never casually.

- **OOPIF-aware transport.** The event-driven `_RealTransport` now auto-attaches
  to child frames (`Target.setAutoAttach` flatten) and tracks out-of-process
  iframes by their CDP `sessionId`; commands route into a child frame session via
  a new `session_id` on `send`. (Same-origin frames were already reached by the
  deep walk; this adds the cross-origin ones behind the same-origin boundary.)
- **Governed crossing** — `browser_frames` lists a page's cross-origin frames
  with each origin's authorization status; `browser_frame_observe` perceives
  inside one **only if its origin is an authorized operator site** (default
  deny), reusing the existing authorization concept — so an injected ad/widget
  frame is listed but never reached into. Both operator-gated, capability-
  separated. The same-origin policy is crossed *only* under explicit per-origin
  authorization, honored as a governed act rather than defeated.
- Verified against real Chromium: a real cross-site iframe surfaces as an
  out-of-process session and eval routes into it. Governance (list + per-origin
  gate + refuse-by-default) covered offline.

### Added — Attested Human Handoff: evolve + honest-automation policy (moat, part 4/4)

The moat compounds and the stance is documented.

- **Need it less over time.** `attest.burden_by_domain()` / `evolution_report()`
  track how often each site required a human — folded into the operator review
  cycle, which now surfaces the heaviest sites and prompts saving their sessions
  (`browser_save_auth`), so a cleared 2FA is reused and the checkpoint rate
  falls. Clear once, reuse many; every action stays attested.
- **Honest automation, documented** (`docs/DESIGN_HANDOFF.md`): no CAPTCHA
  solvers, no anti-bot / fingerprint evasion, no 2FA bypass — refused by design
  and pinned by test; prefer official APIs, otherwise operate transparently.
  Olympus is detectable and attested, and treats that as the trust feature.

### Added — Attested Human Handoff: the handoff + operator wiring (moat, part 3/…)

The loop closes: detect → hand off → verify cleared → attest.

- **Operate is checkpoint-aware.** When a template step can't proceed,
  `operator.execute` now first checks for a human-verification checkpoint; if one
  is present it reports a **handoff** ("clear it in the browser, then I'll
  continue and record a signed attestation") instead of mistaking it for
  template drift — no spurious self-heal proposal, never a solve attempt.
- **`browser_attest_human`** records the signed attestation *only after
  re-checking the live page and confirming the checkpoint is gone* — the proof
  is minted when the check is verifiably cleared, never on the model's say-so.
  Operator-gated, capability-separated, bound to the credentialed session.
- **`operator_attestations`** renders the signed audit trail (first-party read),
  each entry shown with its verify status — proof a human was in the loop for
  every verification.

### Added — Attested Human Handoff: signed attestations (moat, part 2/…)

The moat core: a cleared human-verification check becomes a **cryptographically
signed attestation** (`olympus/attest.py`), signed with the same Ed25519
root-of-trust that signs the decision log (`olympus.witness`).

- `attest(kind, domain)` mints a signed record ("a human cleared a
  `captcha`/`otp`/`step_up` verification on `domain` at `time`");
  `verify_attestation` is tamper-evident (forging the domain or kind breaks the
  signature) and **pin-bindable** (`OLYMPUS_ATTEST_PIN`) so a third-party
  verifier holding the expected key out-of-band can check it. Fails closed
  without the crypto backend — an unsigned "proof" is never minted.
- Attestations persist to an append-only ledger (`attestations.jsonl`);
  `latest_attestation(domain)` binds a later credentialed action to the
  human-check that preceded it, and `summary()` renders the audit trail. This is
  a proof a bypass-first agent structurally cannot produce — having defeated the
  human, it has nothing to attest.

### Added — Attested Human Handoff: checkpoint detection (moat, part 1/…)

Olympus turns the CAPTCHA/2FA boundary into a moat by refusing to defeat it and
instead *proving a human cleared it*. The first piece is **detection, not
defeat**.

- **`browser_checkpoint` / `detect_checkpoint()`** recognizes a human-verification
  checkpoint on the current authorized page — CAPTCHA (reCAPTCHA / hCaptcha /
  Cloudflare Turnstile), one-time-code / 2FA inputs, or a "verify it's you"
  step-up interstitial — by stable markers, and returns **only a type enum**
  (never page prose, never a solve attempt). Providers are matched by their own
  frame fingerprints, so a cross-origin challenge frame is *seen* without
  reaching into it (the origin boundary is honored, not defeated).
- Governed as **credentialed perception**: operator-gated, domain-authorized,
  capability-separated (in `ACTION_TOOLS`), refuses a blocked landing. The
  "never solve/bypass" stance is pinned to code by a test over the detector
  script. Subsequent parts wire the handoff and the witness-signed attestation.

### Improved — browser harness: transport auto-reconnect

A dropped WebSocket used to wedge the harness. `_RealTransport` now remembers its
target socket and, on a transport-level failure (a genuine CDP error is *not*
retried), transparently reconnects once and retries the call — serialized so
concurrent callers reconnect a single time, and re-arming Page/Network events on
the fresh connection. Verified against real Chromium (forcibly closing the socket
mid-session; the next call reconnects to the same tab and the page state is
intact).

### Improved — browser harness: element- and full-page screenshots

`browser_screenshot` was viewport-only; it now also captures **one element**
(pass a `selector` — clipped to its bounding box) or the **whole scrollable
page** (`full_page: true` — `captureBeyondViewport` over the full document
dimensions). Same INGESTION governance (untrusted pixels, wrapped,
capability-separated); no new tool. Verified against real Chromium (a 3000 px
page's full-page capture is larger than the viewport, and a single element's is
smaller).

### Added — browser harness: download capture

`set_download_dir` only *confined* where downloads land; now the harness can
actually **capture** one. `browser_download` (session `download()`) points
downloads at the workspace, optionally clicks a trigger, and waits for a **new,
complete** file to appear (ignoring Chrome's `.crdownload` temp and requiring a
stable size), returning its name. The file is untrusted external content, so it's
read separately via path-confined `read_file`/`analyze_image` — never surfaced as
page prose. Operator-gated (clicking is an action), workspace-confined, and
capability-separated. Verified against real Chromium (a download link click lands
the real file with correct contents in the sandbox).

### Improved — browser harness: true network-idle

`wait_idle` now uses **real** in-flight-request tracking from the CDP event
stream (the reader thread counts `Network.requestWillBeSent` vs
`loadingFinished`/`loadingFailed`), so "idle" means **zero open network
requests** — not a resource-count guess. It waits for genuinely-pending fetches
(XHR/fetch/beacon) to settle before proceeding, and falls back to the previous
heuristic only when a transport can't report in-flight counts. Verified against
real Chromium (a page holding a `fetch` open settles to zero, then proceeds).

### Fixed — browser harness: JS dialogs no longer wedge the harness

A page that pops `alert()`/`confirm()`/`prompt()` on a click used to **hang** the
harness — the click's CDP call blocked until the dialog was handled, and nothing
handled it.

- **Event-driven transport.** `_RealTransport` now runs a single background
  reader thread that demultiplexes the socket: id-matched replies wake their
  waiting `send()`, while unsolicited CDP **events** are routed to handlers. This
  is the foundation that also enables true network-idle and resilience.
- **Auto-handled dialogs.** A `Page.javascriptDialogOpening` event is answered
  automatically. **Safe default is dismiss** — an irreversible `confirm()` is
  never auto-accepted; the operator opts into accept (optionally with prompt
  text) via the new **`browser_dialog`** tool, which is operator-gated and
  capability-separated. Verified against real Chromium (a `confirm()` click that
  previously hung now completes, and honors dismiss vs. accept).

### Added — browser harness: cross-site patterns + template demotion

Two evolution mechanisms that make the skill store *converge* over time.

- **Template demotion** (`operator.demote_drifted`, in the review cycle) — the
  inverse of graduation. Each template now tracks its **own** run/success counts
  (`mark_template_outcome`, recorded on every `browser_operate`); a graduated
  template whose measured reliability craters is **demoted** — removed from the
  profile — so the operator stops auto-running a dead recipe. Promotion is now
  reversible, not a one-way ratchet.
- **Cross-site generalization** (`browser_pattern`, `suggest_pattern`) — a proven
  flow on one site can **seed** another: the tool returns the most reliable
  learned flow matching a goal (e.g. "login", "checkout") as a **generalized
  scaffold** — the op sequence and intent hints with **selectors omitted** (those
  are site-specific and never presented as applying elsewhere). A new site
  bootstraps from a proven shape instead of from scratch, then adapts and learns
  it locally. First-party read; no cross-site selector is ever asserted.

### Added — browser harness: gated self-heal retry

Self-healing now *completes* more runs, without ever taking a risky guess.

- When a **reversible (notable)** template step drifts and a confident
  replacement is found, the operator retries the flow from the failed step with
  the healed selector — continuing (not repeating) the completed steps — and the
  run succeeds, marked `healed`. It still files the human-review proposal so the
  fix can be made permanent.
- An **irreversible/financial** step is **never** auto-retried on a guessed
  selector — it stays propose-only, even when a candidate is present. The gate is
  the template's risk class, so healing can't quietly escalate a risky action.

### Added — browser harness: deterministic waits (`wait_for`)

Instead of racing a fixed sleep on a dynamic page, the harness can wait for a
specific condition:

- **`wait_for`** — a new `browser_act` verb *and* template op that blocks until a
  selector **appears** (or **disappears**, with `value='gone'` / `gone: true`),
  bounded by a timeout, resolving deep (shadow/iframe). A template `wait_for`
  that times out raises the typed `TemplateStepError`, so it feeds the same
  self-healing path as any other unresolved step.
- Verified against real Chromium (an element injected after 400 ms is waited for,
  not missed).

### Added — browser harness: saved auth state (session persistence)

The operator can now remember a signed-in session and restore it, instead of
logging in from scratch every run.

- **`browser_save_auth` / `browser_restore_auth`** — capture an authorized
  domain's cookies (CDP `Storage.getCookies`, filtered to the domain) into the
  **Fernet-encrypted vault**, and re-inject them (`Network.setCookies`) to
  restore the session. Cookies are session credentials, so both are credentialed
  actuators: operator-gated, domain-authorized, stripped from any prose-ingesting
  run, and never surfaced to the model.
- **Self-evolving** — a successful `browser_login` now auto-saves the fresh
  session (best-effort), so the operator's authenticated state persists across
  runs without extra prompting; `forget_site` drops the saved session too.
- Verified end-to-end against real Chromium (cookie set → read-back roundtrip)
  and offline through the vault.

### Improved — browser harness: rooted, unambiguous durable selectors

The durable-selector fallback for a control with no id/name/aria-label used to be
a bare `tag:nth-of-type(k)`, which can match the k-th sibling *anywhere* on the
page — so a promoted template or self-heal candidate could point at the wrong
element. `__olySel` now builds a **rooted path** up to the nearest ancestor with
an id (e.g. `#box>div:nth-of-type(1)>button:nth-of-type(2)`), so it resolves
uniquely. Verified against real Chromium (two ambiguous sibling groups resolve to
exactly the intended node). Directly strengthens template graduation and
self-healing, which both rely on durable selectors.

### Added — browser harness: richer action verbs

`browser_act` gains three interaction primitives, widening the flows the operator
can drive:

- **`rightclick`** (a.k.a. `contextmenu`) — opens context menus, by index/selector
  or x/y.
- **`drag`** — drag the source element onto a target selector (passed in `value`)
  via HTML5 drag events with a shared `DataTransfer` — reorderable lists, kanban,
  sliders.
- **Modifier chords in `press`** — `Control+a`, `Shift+Tab`, `Meta+c`, etc., parsed
  into the dispatched `KeyboardEvent`.

All resolve deep (shadow/iframe), stay on the credentialed-actuator side
(capability-separated), and are verified against real Chromium (right-click and
Ctrl+A fire genuine DOM events).

### Fixed — browser harness: real-transport upload and multi-tab

Two capabilities that the offline `FakeTransport` accepted but real Chrome
rejected — now wired correctly and verified against headless Chromium.

- **File upload** — `DOM.setFileInputFiles` operates on a node *handle*, not a
  selector, so `upload()` now resolves the input to a CDP `objectId`
  (`_resolve_object_id`, deep — works inside shadow roots / same-origin iframes)
  and attaches by handle. Confirmed the file actually lands on the input against
  real Chrome; confinement + operator gating unchanged.
- **Multi-tab** — `switch_tab()` now genuinely *drives* the tab it switches to:
  `_RealTransport.reattach()` re-binds the transport to the target tab's
  WebSocket (resolved via the DevTools HTTP base), so subsequent
  actions/observations run in the new tab's context — not just `activateTarget`.
  Confirmed evals run in the switched-to tab against real Chrome.

The real-Chromium smoke test now covers both.

### Added — browser harness: real-Chromium smoke test (ground truth)

`tests/test_browser_real.py` drives an actual headless Chromium over CDP through
the same `BrowserSession` the offline suite exercises with `FakeTransport` — the
ground truth the doubles stand in for, catching transport-level gaps (node
resolution, target attach, live network) the fakes can't. Opt-in and
self-skipping (`OLYMPUS_BROWSER_REAL=1` + a discoverable Chromium), so default CI
stays green. Confirms open/observe/act-by-index, **shadow-DOM reach**, fill/select,
real PNG screenshots, and tab listing against a live browser.

### Added — browser harness: multi-tab, uploads, network-idle waits

Governed plumbing that widens the range of flows the operator can automate.

- **Multi-tab** — `browser_tabs` lists the browser's open page tabs (bounded
  id/title/url) and `browser_switch_tab` activates one by index. Both are
  operator-gated credentialed actuators; after a switch, `browser_act`/
  `browser_observe` re-check the newly-current domain's authorization, so
  switching can never point the actuator at an unauthorized logged-in tab.
- **File upload** — `browser_upload` attaches a **workspace-confined** file to a
  file input on the current authorized page. Uploading a local file is data
  egress, so it's operator-gated (current domain authorized) and the path can
  never escape the sandbox (`_confine`).
- **Downloads confined** — `set_download_dir()` directs any browser download
  into the workspace, so a site can't drop a file outside the sandbox.
- **Network-idle wait** — `wait_idle()` (and the new `wait_idle` template op)
  waits for the page to load *and* its resource count to hold steady for a short
  quiet window — a dependency-free heuristic for dynamic pages whose content
  arrives after `readyState=complete`. Bounded and best-effort.

### Added — browser harness: visual perception (screenshot + describe)

For canvas/chart/image-heavy pages that a text map can't capture, the harness can
now *look* at the page.

- **`browser_screenshot`.** Captures the current page (CDP
  `Page.captureScreenshot`) and describes it with the vision model
  (`media.analyze_image_data`, a new in-memory path — no workspace file needed),
  optionally answering a question about it.
- **Governed as a reader, not an actuator.** The pixels are untrusted external
  content (text-in-image is still injection), so `browser_screenshot` is an
  INGESTION tool: its description is wrapped, and capability separation strips it
  from any run that also holds an actuator. It refuses a blocked landing (never
  captures internal content) and caps the decoded image size. Given to the
  reader (Argus), never the operator.

### Added — browser harness: self-healing templates

When a site is redesigned, a template no longer fails silently — it diagnoses the
drift and proposes the fix.

- **Drift is detected, not swallowed.** `run_template` now raises a typed
  `TemplateStepError` when a step can't resolve — including a failed *click*,
  which previously passed unnoticed.
- **Re-observe and locate the moved control.** On a step failure during
  `browser_operate`, the operator re-observes the page and finds the control
  that most likely *is* the moved one, matching the failed selector's intent
  (exact slug → substring containment → trigram similarity) against the current
  elements' labels and durable selectors.
- **Propose, never self-rewrite.** A candidate becomes a human-reviewed
  proposal (`site_template_record` to enact) — Olympus never auto-edits a
  credentialed template, so self-healing can't be turned into an injection
  primitive. The run still reports an honest FAILED, now with the likely fix
  attached, and the outcome feeds the reliability score that prunes dead
  templates.

### Added — browser harness: proven skills auto-graduate into templates

The evolution loop now closes fully: improvise → learn → prove → **formalize**.

- **Structured recipe.** As the harness acts, it captures a structured twin of
  its journal — `{op, selector, value?}` steps with **durable selectors**
  (id → `[name]` → `[aria-label]` → nth-of-type path via a new `__olySel`
  helper), never the typed text. `browser_learn` persists this recipe on the
  learned skill.
- **Auto-graduation.** A METIS review pass (`operator.promote_ready`, folded
  into `operator.review_profiles`) graduates a learned skill into a declarative
  action template once it has been tried enough times (`_PROMOTE_MIN_RUNS`) and
  lands reliably enough (`_PROMOTE_RELIABILITY`). The generated template is
  guarded by an `assert` on its first control so it fails fast if the page
  drifted, and it rides the existing governed `browser_operate` path — auto-run
  within scope for notable risk, approval for anything higher. Graduation
  **formalizes** a proven flow without widening the trust boundary.
- **Idempotent & bounded.** A skill whose template already exists is skipped;
  recipes are capped and credential-free by construction.

### Added — browser harness depth: shadow DOM + same-origin iframes

The harness now perceives and acts on controls that modern web apps hide behind
component boundaries, not just the top-level document.

- **Deep perception & resolution.** `observe()` deep-walks the light DOM plus
  every **open shadow root** and **same-origin iframe**, stamping controls found
  anywhere in that tree; a shared `__olyq` deep-query helper backs every
  `act`/`read`/`exists`/`fill`/template resolution, so an index stamped inside a
  shadow root or frame still resolves. Most componentized sites that previously
  showed a near-empty map now expose their real controls.
- **Boundary honored, not defeated.** Cross-origin frames throw on access and are
  skipped — Olympus works within the same-origin policy rather than trying to
  bypass it.
- **Hardened.** The shadow/iframe recursion is depth-bounded
  (`_DEEP_MAX_DEPTH`) so a pathological or hostile page can't hang the walk or
  overflow the stack; observe output stays capped at `_OBSERVE_MAX` with labels
  capped at `_LABEL_MAX`.
- **Evolves for free.** Because the perceive→act loop now reaches these controls,
  `browser_learn` captures shadow/iframe flows into the same reliability-scored
  skill store — deeper reach, same governance.

### Added — native browser harness (perceive → act, governed)

Olympus grows its **own** browser-agent working style, so no external browser
harness has to be plugged in. The harness perceives an arbitrary page as an
indexed map of its interactive elements and acts by index — the browser-use
working style — but built the Olympus way: capability-separated, operator-gated,
and wired to evolve.

- **Indexed perception** (`browser.BrowserSession.observe`, `browser_observe`
  tool) — a single sandboxed `Runtime.evaluate` pass finds the visible,
  enabled interactive elements (links, buttons, inputs, selects, ARIA
  roles, `contenteditable`, …), stamps each with `data-olympus-idx`, and
  returns a numbered map (`[2] button "Sign in"`). The model acts by index
  instead of guessing CSS selectors.
- **Richer action set** (`browser.BrowserSession.act`, `browser_act`) — now
  resolves an element by `index` from the map and supports click, type,
  scroll, press (keys), select (options), hover, and back, over the same
  audited CDP ledger.
- **Governed like an actuator, not a reader.** `browser_observe` maps a
  possibly logged-in tab, so it is classified in `ACTION_TOOLS`: stripped from
  any prose-ingesting run (capability separation), gated to an
  operator-enabled, authorized domain, and its element labels are hard-capped
  so a map row can't smuggle a paragraph of instructions. The operator
  (HERMES) holds the full `observe → act` loop; ingesting readers (Argus)
  hold neither half. Threat-modeled and count-bound in `docs/THREAT_MODEL.md`.
- **It evolves** (`browser_learn`) — the session keeps a bounded, first-party
  journal of the act steps that actually landed (never the typed text, so
  credentials never enter the store). When a task succeeds, HERMES crystallizes
  that proven flow into a reliability-scored skill with `browser_learn`. From
  there it rides the existing evolution machinery: future runs reuse it,
  `mark_outcome` refines its measured score, Metis prunes it if it goes flaky,
  and Prometheus proposes profile fixes — so the harness gets better on the
  sites you actually use, without a human writing a selector script.

### Changed — Olympus assumes no model (vendor-neutral by default)

Olympus no longer ships a baked-in default model. Previously an Anthropic
setup with no `OLYMPUS_MODEL` silently ran `claude-opus-4-8`, and the replay
gate silently used `claude-sonnet-4-6`; both assumptions are gone. The user
chooses a model explicitly — `olympus setup` (which lists your key's real
models), `OLYMPUS_MODEL`, or per-request BYOK — and a missing choice now fails
fast with an actionable message (`config.require_model`), is flagged by
`olympus doctor`, and reads as `models: down` in `olympus health`.
**Migration:** if you relied on the implicit default, set it explicitly once:
`olympus config set OLYMPUS_MODEL claude-opus-4-8` (or any model you prefer).
`OLYMPUS_GATE_MODEL` is now purely optional: unset, the replay gate runs on
your configured model.

### Added — workspace, operator, and gateway (Odysseus/Hermes/OpenClaw study)

A three-phase build extending Olympus toward the archetypes it was closest to.

- **Image editing** (`media.edit_image`, `edit_image` on Peitho/Iris,
  `olympus gallery edit`, an Edit button in the `🖼 gallery` panel) — AI-edit an
  existing workspace image by prompt, saving the result as a **new** file (the
  original is never overwritten). Reads the source path-confined (image types
  only, size-capped) and writes only to the confined workspace; degrades
  gracefully without a media key. Uses a dependency-free multipart POST to the
  images-edit endpoint — no new heavy image library.
- **Runtime health reporting** (`olympus/health.py`, `olympus health`,
  `/api/health`) — a live "what's degraded right now" view of the moving parts
  (models, memory, gateway channels, search, push, connections), each reported
  **ok / degraded / down**. Distinct from `doctor` (setup readiness): `health`
  is pollable, `olympus health` exits non-zero when anything is down, and an
  absent optional piece is *degraded*, not a failure, so a minimal install still
  reads healthy.
- **ntfy push channel** (`olympus/ntfy.py`) — a lightweight publish-to-a-topic
  delivery target for scheduled jobs and proactive alerts, alongside
  Telegram/Discord/Slack/Signal. Configure with `NTFY_TOPIC` (+ optional
  `NTFY_SERVER` for self-hosted and `NTFY_TOKEN` for a protected topic); wired
  into the scheduler (`--to ntfy`), the gateway fan-out, and the heartbeat, and
  reported by `olympus doctor`. Best-effort — a push failure never breaks the
  job that produced the result.
- **MCP server — workspace reads** (`olympus/mcp_server.py`, `olympus mcp-serve`)
  — the existing MCP server now also exposes three **read-only** workspace tools
  to any MCP client (Claude Desktop, IDEs, other agents): `olympus_search_documents`,
  `olympus_list_todos`, and `olympus_recall_memory`, scoped to `OLYMPUS_MCP_USER`
  (default the shared namespace). No write or actuator ever crosses the boundary.
- **Email spam triage** (`olympus/spamtriage.py`, `triage_inbox` on Angelos,
  `olympus triage`) — a read-only heuristic classifier that sorts the inbox into
  important / promotions / spam / other with a reason for each. No model call
  (works even where egress is locked down), never deletes or moves anything, and
  message content stays untrusted (the tool is wrapped like other inbox reads).
- **Notes / todos / reminders** (`olympus/todos.py`, `olympus todo`, web UI
  "Your list" in the `📅 agenda` panel) — a small per-user checklist store:
  notes (kept scraps), todos (tickable), and reminders (todos with a due time).
  Open items and overdue reminders surface in the agenda; `list_todos` /
  `add_todo` / `complete_todo` on Chronos are first-party and ungated (the
  user's own data, no external side effect).

- **Documents workspace** (`olympus/documents.py`, `olympus documents`, web UI
  `📄 docs` panel) — a per-user Markdown store the assistant can read and, with
  approval, write. The agent's `write_document` tool stages every save through
  the approval spine (reversible, never silent); the user editing in the web UI
  saves directly. `list_documents`/`read_document`/`write_document` on Peitho.
- **Personal-document RAG** (`olympus/docrag.py`, `search_documents` on Peitho)
  — retrieval over the user's own documents, grounded into the synthesis
  prompt. Semantic (cosine over embeddings) when an embeddings endpoint is
  configured, lexical overlap otherwise; a per-user index re-embeds only the
  documents whose mtime changed. First-party content (grounded like memory, not
  wrapped as untrusted).
- **Gallery** (`olympus/gallery.py`, `olympus gallery`, web UI `🖼 gallery`
  panel) — surfaces images generated into the confined workspace. Serving is
  path-confined (traversal refused via `_confine`), image-types only, and
  size-capped; the panel lazy-loads thumbnails and opens full images in a tab.
- **Agenda** (`_agenda_view` + `/api/agenda`, `olympus agenda`, web UI
  `📅 agenda` panel) — one view of the user's scheduled tasks (from the
  natural-language scheduler, filtered to the principal plus shared jobs) and
  their upcoming calendar events. Calendar reads are read-only, scoped to the
  connected Google account, and degrade to "not connected" instead of erroring
  when no account is linked or egress is blocked.
- **Blind multi-model compare** (`olympus/compare.py`, `olympus compare`, web UI
  `⚖ compare` panel) — the same prompt against every configured model with
  labels shuffled, revealed only after you pick, and picks accumulated into a
  per-user tally so preference reflects output rather than brand. Each model is
  called with no cross-member failover (`backend.complete_text_once`), so a
  failing model shows its own error under its own label instead of being
  silently answered by another. Needs ≥2 models (via `OLYMPUS_MODELS`).
- **Hermes operator, made usable** — `olympus operator`
  (status/enable/authorize/forget/list/history), a built-in site-profile
  catalog (`olympus/profiles/`), a plain-English review surface in CLI and chat
  (`operator_status`/`operator_history`), and `docs/OPERATOR_GUIDE.md`. Built on
  the existing governed operator; no bypass of its gates.
- **Unified gateway daemon** (`olympus gateway`) — runs every configured chat
  channel (Telegram/Discord/Slack/Signal/WhatsApp/email/webhook) in one
  supervised process with auto-restart + backoff and a cross-process health
  file (`gateway --status`). `docs/GATEWAY.md`.

### Added — capabilities studied from Odysseus

A batch distilled from analyzing the Odysseus self-hosted AI workspace
(`pewdiepie-archdaemon/odysseus`) and adopting its best agent ideas the
Olympus-native way — verified, gated, headless-first. See the release-tracking
analysis for the full feature comparison.

- **Deep Research pipeline** (`olympus/research.py`, `olympus research`) — an
  IterResearch-style engine: plan sub-questions → iterative search/read/extract
  loop the model drives → cited markdown report. Pool-aware staging (reasoning
  plans/synthesizes, general extracts, verify checks the draft against the
  evidence), a never-laundered verification-notes section, date-grounded
  queries, low-quality-domain and duplicate-query filtering, and every fetched
  page wrapped untrusted through the SSRF/rebinding-pinned path. On the
  Anthropic backend, search/fetch run through Anthropic's **server-side** web
  tools — so Deep Research reaches the open web even where the host's outbound
  egress is locked down to a proxy allowlist — falling back to the client-side
  provider layer for local/non-Anthropic models.
- **Code-navigation tools for Hephaestus** — `grep_files` (bounded regex
  search), `glob_files` (pathlib-style `**/`, newest-first), and `read_file`
  line-range slicing, plus `edit_file`: exact-string editing staged for
  explicit approval (always-hold — never auto-executes, since it rides on the
  always-ingesting Hephaestus) whose preview *is* the unified diff. A
  case-insensitive sensitive-file deny-list (`.env`, keys, `.ssh`/`.aws`, …)
  guards read/grep/glob/edit.
- **Per-turn dynamic tool selection** (`olympus/toolselect.py`) — ranks a
  specialist's loadout against the current task and drops the least-relevant
  tail of oversized loadouts, saving prompt tokens each round. Runs strictly
  after every security filter (it can only drop, never re-admit a stripped
  tool); BASE/server-side/`prepare_action`/`ask_user` are never dropped.
  `OLYMPUS_TOOL_SELECT_MAX` tunes the cap (set low for small-context models).
- **Teacher escalation** (`olympus/teacher.py`) — an Athena-ordered rework
  reruns on the strongest pool member for that role, and the fix is distilled
  into a provisional, benchmark-gated, specialist-scoped skill so the weaker
  model learns. No-op with a single-member pool or an already-strongest
  specialist. `OLYMPUS_TEACHER_ESCALATION=0` disables.
- **Pluggable web-search providers** (`olympus/websearch.py`) — SearXNG, Brave,
  Tavily, Serper, and Google PSE behind one seam with DDG as the keyless
  fallback; ordered try-through, 429 cooldown, and a shared result cache.
  Endpoints ride the sovereign egress choke. The `web_search` tool and Deep
  Research both use it.
- **Skill import from public GitHub URLs** (`skillpack.import_url`) — direct
  SKILL.md links, GitHub blob links, and whole repo/tree bundles (read in
  memory, never extracted to disk). Remote imports are always provisional and
  pass the injection/credential scan before the benchmark gate.
- **`ask_user` tool** (`olympus/interaction.py`) — specialists can ask a
  focused multiple-choice question mid-run through a thread-local provider;
  interactive surfaces prompt the terminal, headless runs proceed with a stated
  assumption instead of blocking. Available to every specialist and safe under
  capability separation.
- **Style-matched email drafting** (`olympus/emailstyle.py`) — Angelos distills
  the user's writing voice from sent mail (quotes/signatures stripped, bodies
  wrapped untrusted, guide scrubbed for injection) and drafts replies in it; a
  `refresh_email_style` tool rebuilds on demand.

### Security

- **DNS-rebinding defense for outbound fetches** — `security.resolve_pinned_ip`
  validates every resolved address and returns the exact IP to dial;
  `tools._PinnedHTTP(S)Connection` connect to that pinned IP (HTTPS keeps
  SNI/cert checks on the hostname), closing the validate-then-reconnect window
  the SSRF guard's own docstring had acknowledged. `_http_get` and webhook
  delivery use the pinned opener. (Adapted from Odysseus fix #704; the
  case-insensitive sensitive-file deny-list mirrors their #5097.)

## [0.24.0] — 2026-07-03

A trust-and-adaptation release distilled from a close study of the Hermes agent:
adopt the best of its UX, but build the Olympus-native step beyond — verified,
lightweight, cost-aware, headless-first. See `docs/VISION.md` for the full
screen-by-screen analysis and the backlog these changes came from.

### Added — trust core

- **Command security gate** (`olympus/cmdguard.py`) — a deterministic,
  dependency-free classifier that screens every shell command before
  `sandbox.run()` executes it, refusing catastrophic ones (`rm -rf /`, `mkfs`,
  fork bombs, disk writes, host power-off) **even after human approval**.
  Fail-closed: an unknown `OLYMPUS_EXEC_SECURITY` mode falls back to `enforce`.
  The shell analogue of the Aletheia hallucination gate.
- **`clarify` route** — Zeus can ask 1–2 crisp questions on a genuinely
  ambiguous request instead of guessing (gated in the prompt so it won't nag).
- **`analyze_image` tool** — vision analysis over an OpenAI-compatible endpoint
  (URL or workspace file; path-confined, size-capped, output treated as
  untrusted). Closes the one real capability gap vs. Hermes.

### Added — cost & pool intelligence

- **Pricing-aware routing** — a `$/Mtok` table (with live OpenRouter pricing via
  `providers.fetch_pricing`) breaks genuine capability ties toward the cheaper
  model; `ModelPool.assignment()` shows per-role price.
- **Per-provider credential rotation** — `Settings` carries a key pool; the
  OpenAI-compatible backend rotates to the next key on 429/quota and remembers
  the exhausted one, with masked provenance via `rotation_report()`.
- **Context-fraction history budget** — compaction now scales to each model's
  context window (`OLYMPUS_HISTORY_CONTEXT_FRACTION`), with the absolute
  `OLYMPUS_HISTORY_TOKEN_BUDGET` kept as an override.

### Added — CLI, readiness & UX

- **`olympus doctor`** — a readiness check (provider, sandbox, command gate,
  workspace, memory, optional media/gateways) with a ✓/⚠/✗ summary and a
  capability dashboard; reused at the end of `olympus setup`. Also `/doctor`.
- **`olympus config show|set|edit`** (secrets masked, never echoed) and
  **`olympus setup <model|terminal|gateway|tools>`** section editors.
- **Progress verbosity modes** — `OLYMPUS_PROGRESS` / in-chat `/progress`
  (`off|stages|all|verbose`); verification activity always shows from `stages`.
- **Distill-then-clear `/reset`** — folds a conversation into durable state,
  then starts fresh; idle gateway sessions are swept on a cadence.
- **Live DAG checklist**, a **context-usage status line**, and a smoother
  **setup wizard** (Quick/Full fork, env key auto-detection, merged Anthropic
  auth-mode entry, "get one at" key URLs + save-now note, trade-off labels).

### Added — personality, content & reach

- **`~/.olympus/soul.md`** — an editable owner personality directive injected
  into Zeus at routing and synthesis (`olympus soul [show|edit]`).
- **Cron-attached skills** — a scheduled job can name a skill to load before it
  runs (`schedule_task(..., skill=...)`).
- **Curated starter-skill pack** (`olympus skills-starter`, provisional/gated)
  and **email + inbound-webhook gateways** (`olympus email`, `olympus webhook`)
  reusing the shared gateway pipeline.

### Added

- **`/learn <anything>`** (`olympus distill`, `/learn` in chat) — distill a
  reusable skill on command from a URL (SSRF-guarded, wrapped untrusted), a
  local file/directory (operator-only: CLI/TUI — chat users cannot read
  server paths), or a described workflow. The distillate rides the existing
  safety machinery: created provisional (benchmark-gated), scanned with the
  same injection/credential scan as skill imports, sanitized before write.
- **`/journey`** (`olympus journey`) — the browsable timeline of everything
  learned: skills (with provisional status), lessons, corrections, feedback,
  prompt upgrades. `journey show <ref>` inspects an entry; `journey rm
  <ref>` removes one (skills archive recoverably) so a wrong lesson stops
  shaping future answers.
- **MoA polish** — `/moa <question>` (and `olympus moa`) runs one prompt
  through the ensemble regardless of the configured provider, showing each
  reference model's answer as a labelled block before the aggregate;
  `OLYMPUS_MOA_SAVE_TRACES=1` persists full ensemble traces to reports.
- **`/goal wait <id> <pid>`** — park a standing goal's work cycles while a
  long-running process (build, backtest) finishes; the heartbeat resumes
  the goal with a progress note when the process exits.
- **TUI conveniences** — `/prompt` composes a multi-line question in
  `$EDITOR`; `/reasoning` shows how the last answer was produced (the run's
  recorded pipeline trace: routing, specialist spans, verification
  decisions — also available in chat gateways); `/timestamps on|off`.
- **Voice notes on every gateway** — Signal (signal-cli attachments),
  Discord (slash-command audio attachments), and Slack (voice clips /
  audio uploads, fetched with the bot token) now transcribe into the text
  pipeline as `[voice note] …`, completing the Telegram/WhatsApp coverage.
  Non-audio attachments are ignored; oversized audio is skipped.
- **Session auto-resume on Slack and Discord** — both now journal the
  request they are processing. Slack re-runs it after a restart and
  delivers to the same channel (chat.postMessage doesn't expire). Discord
  interaction tokens DO expire, so the honest fallback re-runs the request
  and posts the answer via the notify webhook naming the requester — or,
  with no webhook configured, logs the loss visibly instead of silently.
- **Operator admin panel, Phase 3 (configuration from the browser)** — a
  strict allowlist of settings (channels, SMTP/webhooks, model pool,
  connector policy; `olympus/opconfig.py`) is now editable from `/admin`.
  Secrets are stored in the encrypted vault (`OLYMPUS_SECRET_KEY`; the panel
  refuses rather than ever writing a secret to disk in the clear, and evicts
  stale plaintext copies from config.env); non-secrets go to the setup
  wizard's `~/.olympus/config.env`. Values hydrate into every `olympus`
  process at start with env-wins precedence, and every change reports
  honestly where it's live now and which processes need a restart — a saved
  value shadowed by a real env var is flagged. MCP servers can be added
  (same security scan as `add-mcp`) and removed, live everywhere with no
  restart. The panel's own auth and process-weaponizing keys
  (plugins dir, exec backend, memory dir, sovereign policy) are deliberately
  NOT editable from the browser.
- **Operator admin panel, Phase 2 (act on running state)** — the panel can
  now drive what the CLI already exposes, via `POST /api/admin/act`:
  approve/deny held actions across users (the approval spine's human step,
  one click), add/complete/drop standing goals, add/enable/disable/remove
  scheduled tasks, trigger the skill gate/curation/backup in the background,
  and set a user's autonomy level. Mutations require an `X-Olympus-Admin`
  header on top of the operator auth (custom headers force a CORS preflight
  this server never approves, so cross-origin pages can't fire mutations at
  a loopback panel). Configuration stays CLI-only (Phase 3).
- **Operator admin panel, Phase 1 (read-only)** — `/admin` on the web server
  is a single-pane overview of a running instance: channels configured, model
  pool + role assignment, budget and per-model spend, every heartbeat cycle
  with last-run/next-due, standing goals, pending approvals across users,
  skills, scheduled tasks, connectors (MCP/plugins/hooks), security posture,
  and recent errors. Strictly read-only; secrets appear only as booleans and
  hosts. Data rides `GET /api/admin`, gated by `OLYMPUS_ACCESS_TOKEN` — or
  loopback-only when no token is set (never open on a public bind or behind
  a proxy). See docs/ADMIN_PANEL.md.

### Added — capabilities adopted from the Hermes-agent analysis (docs/HERMES_WATCH.md)

- **Standing goals with completion contracts** (`olympus/goals.py`) — `/goal`
  (chat) and `olympus goal add/list/check/work/drop/done` set objectives that
  outlive the conversation. The heartbeat works each active goal on a cadence
  (`OLYMPUS_GOALS_EVERY`, default 6h) and a verify-role judge closes it **only
  on concrete evidence in the progress log** — assertions are not evidence.
  Goals stall (with a push notification) instead of looping forever.
- **Skill-library curation** (`olympus/curator.py`, `olympus curate`,
  `OLYMPUS_CURATION_EVERY` default 7d) — the retirement half of
  self-improvement: a scoped, tool-free model call grades every proven skill;
  a prune candidate is archived (recoverably) only if hiding it doesn't lower
  the affected benchmark — the admission gate run in reverse; consolidations
  are queued as lessons for Metis.
- **Mixture-of-Agents provider** (`OLYMPUS_PROVIDER=moa`) — the configured
  model pool becomes an ensemble: every completion fans out to the members as
  reference models and the strongest member aggregates their drafts into one
  answer. Tool-using runs route to the aggregator member.
- **Provider failover chains** — a provider-side failure (auth/rate/outage) on
  an operator-pool member now retries the other pool members in order
  (`OLYMPUS_FALLBACK=0` disables). BYOK/per-request credentials never fail
  over; refusals and replay divergences surface unchanged.
- **Voice input** — `transcribe_audio` tool (OpenAI-compatible
  `/audio/transcriptions`, `OLYMPUS_STT_MODEL`) plus Telegram/WhatsApp voice
  notes transcribed into the normal text pipeline as `[voice note] …`.
  Registered as an INGESTION tool: spoken injection is still injection.
- **MCP server mode** (`olympus mcp-serve`) — Olympus as an MCP server on
  stdio, exposing `ask_olympus` (the full fact-checked pipeline) and
  `olympus_goals` to Claude Desktop, IDEs, and other MCP clients.
- **`/steer <note>`** — mid-run steering: gateways handle it outside the
  per-user serial worker so the note reaches the *running* pipeline after its
  next tool round (frozen per round, so replays stay byte-identical).
- **`/undo [N]`** — removes the last N exchanges from the persisted
  conversation (TUI, gateways, Telegram, WhatsApp), stopping at compaction
  boundaries.
- **Plugin lifecycle hooks** — `@hook("pre_tool")` can block or rewrite a tool
  call (composing with, never bypassing, the approval spine); `post_tool` can
  rewrite results; `session_start`/`run_start`/`run_end`/`pre_llm_call`/
  `post_llm_call` observe. Broken hooks are contained.
- **`@file` / `@url` context references** in the interactive TUI — expanded
  into delimited context blocks; URL content rides the SSRF-guarded fetcher
  and is wrapped as untrusted.
- **Cross-session prompt caching** — `OLYMPUS_CACHE_TTL=1h` extends the
  Anthropic cache breakpoints (system prompt + tool schemas) to the 1-hour
  tier, keeping the cache warm across heartbeat cycles and gateway chats.
- **Activity-based exec timeouts + watch patterns** — `sandbox.run` now kills
  on *silence*, not elapsed time (output extends the lease up to the 600s
  ceiling), returns partial output on timeout, and an optional `watch` regex
  collects matching output lines.
- **Post-write verification** — `write_file` verifies the bytes landed and
  parse-checks py/json/toml/yaml, surfacing the verdict to the agent in the
  action result (catches silent write failures in the same turn).
- **Session auto-resume** — Telegram/WhatsApp journal the request they are
  processing; after a gateway restart the stale entry is re-run once and the
  user is told, instead of the request vanishing silently.

### Security

- **Outbound secret-exfiltration scanning** (`security.secret_exfil_reason`)
  — the actual secrets this process holds (vault entries + key-shaped env
  vars) are detected leaving in outbound content, raw or base64/hex/
  url-encoded. Always-on floor in web fetches, `send_email`, `call_webhook`,
  the contribution pool, and `egress.guard` (HOLD on every channel; an
  explicit human approval can still release).
- **Skill imports are scanned, not trusted** — `skill-import` refuses
  SKILL.md files carrying prompt-injection markers or embedded credentials.
- **MCP connector definitions are validated on the way in** — non-https URLs,
  credentials-in-URL, SSRF-blocked hosts, and literal tokens in `auth_env`
  (instead of an env-var name) are refused before they persist.


### Security — close operator API-key exfiltration and BYOK bypass

- `Settings.merged` dropped inherited credentials only on a *provider* switch,
  so a same-provider `base_url` override kept the operator's env key and sent it
  to a visitor-supplied host; the Anthropic SDK also fell back to
  `ANTHROPIC_API_KEY` when the key was cleared. Now an endpoint change drops the
  inherited key, and `llm.client` passes an empty key to a custom `base_url` so
  it fails closed. `web._brought_own_key` counts only a *primary* `api_key` as
  BYOK (a bare `base_url` or an `extra`-model key no longer unlocks the free /
  `OLYMPUS_REQUIRE_BYOK` wall while the primary pipeline runs on the operator's
  key). `/api/login|register|logout` are now rate-limited (were dispatched
  before the limiter — password brute-force + PBKDF2 CPU-DoS).
  `tools._read_source_file` uses path-component containment (a string-prefix
  check accepted sibling dirs like `<root>-backup/`). `spawn_subagent` inherits
  the calling run's credentials so a BYOK visitor's subagent runs on their key.
- Packaging: added `.dockerignore` (keeps `.env`/`deploy/.env`/`keys.sh`, `.git`,
  and local `memory/` out of the image); the Dockerfile now installs the
  hash-pinned `requirements.lock` and the package itself; the Auto-Upgrade
  workflow is gated on `author_association` so an issue body can't drive its
  write-capable coding agent.

### Fixed — replay/verify, routing, and a batch of correctness bugs

- Replay now freezes and restores conversation history and fast-mode and records
  the model that actually ran each stage, so `olympus replay` / `verify --log`
  no longer diverge spuriously on multi-turn or fast-mode runs; `_plan` no
  longer swallows a `ReplayDivergence`.
- `olympus verify` now detects files *added* since signing (injected-file
  detection), and dev-posture runs verify against the default seed's key so they
  don't fail once the instance sets `OLYMPUS_SIGNING_SEED`.
- Added the server-side `web_fetch` tool on the Anthropic provider (the
  verifiers were told to call it but it wasn't declared); the router no longer
  reports unparseable route JSON as a safety refusal.
- Misc: prompt-restore no longer corrupts a prompt on a multi-line update
  reason; `operator.authorized` matches subdomains of an authorized site;
  schedule confirmations render the coarsest unit (`7d`, not `168h`); backup
  closes a leaked temp fd; `cli restore` reports failures instead of a raw
  traceback and distinguishes an invalid signature from unsigned;
  `codegraph_path` returns a message for unknown symbols; the LLM client cache
  is bounded and the final retry no longer sleeps; the web session cap
  FIFO-evicts instead of wiping every user's session; the per-session event
  buffer is bounded; and a rejected chat (400/402) no longer burns a daily-chat
  quota slot.

### Changed — documentation honesty and drift guards

- Corrected the "server-side sandbox, never on your machine" claim across README,
  `docs/THREAT_MODEL.md`, and the code-eval path — model code runs approval-gated
  as a local subprocess by default (opt-in `OLYMPUS_EXEC_BACKEND=docker` for OS
  isolation). Fixed the "26 tools" → 60 count and the "12/11 specialists" → live
  count, and added CI-enforced guards (`threatmodel.check`, interpolated UI
  count) so both can't drift again. Marked the shipped design docs as
  implemented (were "proposed"/"not wired up").

## [0.23.0] — unreleased (prepared; not tagged or published)

### Security — deep-diagnostic hardening pass

- **Capability separation closed on `extra_tools` ingestion.** A specialist that
  reads untrusted content only through its own tools (Angelos via
  `read_inbox`/`read_email`/`read_calendar`, Mnemosyne via `watch_youtube`) was
  treated as non-ingesting, so it kept action capability and received global
  action plugins/MCP servers. `Specialist._ingests` now derives from the full
  loadout, so any INGESTION tool denies action capability.
- **Prometheus no longer ingests the live web** while holding self-modifying
  tools (`update_prompt`, `restore_prompt`, `propose_upgrade`, …); its "scan
  outward" reads Argus's already-vetted reports from memory instead.
- **`spawn_subagent` can no longer reach privileged specialists.** Delegation is
  refused for system specialists and any that hold an actuator in a fresh run
  (Prometheus, Metis, Hermes, Chronos), so an injected page can't launder a
  self-modification or credentialed action through a sub-agent.
- **`send_email` / `call_webhook` route through the approval spine** (prepare →
  auto-or-hold); nothing world-affecting auto-sends.
- **SSRF gate** on `browse_page`/`_http_get`/`_call_webhook`, with every redirect
  hop re-validated (a public URL that 302s to an internal/metadata host is
  blocked).
- **`browser_act` is operator-authorization-gated**, and the operator gains a
  real `FINANCIAL_LEGAL` risk tier (no longer silently downgraded).
- **Path-traversal guards** on backup restore (manifest-driven moves) and
  `import_memory`, so a crafted archive can't write outside its target.
- **Backup restore is all-or-nothing** (verify every hash before moving any
  file); **backup create refuses to write plaintext** if encryption is
  configured but fails.
- **Decision-log verification fails closed** without a crypto backend and gains
  an explicit pinning path (`OLYMPUS_LOG_PIN`) for third-party verifiers.

### Added

- **`gate_prompt`** — a code-enforced, benchmark-gated prompt upgrade: applies a
  rewrite only if a before/after benchmark shows no regression and rolls it back
  automatically otherwise, making the "measured, with rollback" guarantee real
  (the enforced counterpart to `update_prompt`).

### Fixed

- `tools.py` used `json` with no module-level import → `NameError` on any
  operator call passing real JSON params.
- `olympus explain` read the model from the wrong field and printed `model=?`;
  it now shows the real model per decision.
- Streamed runs (`ask_stream`) now record their input, so they are replayable.
- Parallel-dispatch `contract`/`egress` decisions are order-stabilized, removing
  false replay-gate divergences when contracts or the egress guard are enabled.
- Atomicity/robustness: the usage ledger (budget could reset to 0 on a torn
  write) and the verified-facts cache (crash on a malformed line; non-atomic
  compaction) are now crash-safe; the companion interaction counter no longer
  loses updates under concurrent turns.
- `restore_prompt` is now a real rollback stack (was newest-only); the relgraph
  refuses new nodes at its cap instead of fabricating false edges; memory
  auto-supersede compares against decayed (not stored) confidence; the scheduler
  renders intervals in their coarsest unit (`7d`, not `168h`).
- Chat-gateway dedup evicts oldest-first instead of clearing the whole set (a
  retry past the cap could be answered twice); Discord/Slack gateways ack fast
  and run the pipeline off-thread with event de-duplication.
- A non-positive heartbeat cadence now means "off" instead of "run every tick";
  `Specialist.run` honors the specialist's configured effort; the default model
  is read from the environment live.

### Changed

- README: the "verification gate" claim is scoped to the code (the supervisor
  flags which answers are factual; only those are checked), the Install/Setup
  sections are de-duplicated, and the Discord/Slack/Signal gateways are
  documented. Several overstated docstrings corrected (sandbox "confinement",
  operator "second fence", `_maybe_compact`, OpenAI-compat `effort`).

### Added — Optional in-run tool-transcript compaction (`olympus/transcript.py`)

- Closes the last context boundary: the chat layer already compacts old
  *conversation* turns; this compacts within a **single agent run** so a
  tool-heavy loop (many/large `web_fetch`/`read_file`/codegraph results) doesn't
  drown in its own scrollback. When the run's messages exceed a budget, the
  **contents of older `tool_result` blocks are shrunk in place** while the most
  recent ones stay verbatim. **Off by default** (`OLYMPUS_INRUN_COMPACT`;
  `elide`/`1` = deterministic, `summarize` = LLM summary of old results;
  `OLYMPUS_INRUN_BUDGET`, `OLYMPUS_INRUN_KEEP_RECENT`).
- **Replay-safe by construction:** it never removes/reorders messages and never
  separates a `tool_use` from its `tool_result` (only the content string of old
  tool_results changes), so pairing/alternation invariants hold; and it is a
  pure function of the message stream, which is identical under replay (tool
  results are frozen) — so downstream request hashes match and replay stays
  byte-identical. The optional summarizer routes through the already-frozen
  `backend.complete_text`, so "summarize" mode replays deterministically too.
  Documented requirement: replay a recorded run with the same
  `OLYMPUS_INRUN_COMPACT` setting it was recorded with (moot at the default).
- Not needed today (the 16-iteration cap already bounds a run); this is for when
  you raise `MAX_AGENT_ITERATIONS` for deep autonomous single-runs. Covered by
  `tests/test_transcript.py`. No capability-count change.

### Changed — Strengthen the 13 specialists (three levers)

1. **Sharper prompts.** The seven thinnest specialist prompts (plutus, peitho,
   aegis, chiron, chronos, argus, mnemosyne) gain a focused "nail these" block
   tied to the concrete behaviors the deepened benchmark rewards — e.g. Plutus's
   match→high-APR-debt→buffer→invest ordering and break-even rule, Aegis's
   "never click the link / ordered incident response", Chronos's "no perfect
   cross-timezone time → rotate". Additive, identity-preserving sharpening.
2. **Per-specialist model role + effort (data-driven tiering).** `Specialist`
   gains `role` ("reasoning" | "coding" | "verify") and `effort` fields; model
   routing (`ModelPool.for_specialist`) now reads the role from the registry
   instead of a hardcoded map, and the orchestrator passes each specialist's
   effort. Defaults preserve today's behavior (single-model pools are a no-op,
   effort stays "high"); the value shows in a multi-model pool, where each
   specialist routes to the member strongest for its kind of work.
3. **Output contracts in use.** The previously-unused contract hook now carries
   real (off-by-default) guards: Iris caps reply length (concise social copy),
   Argus caps tool calls (runaway-scan ceiling). Enforced only when
   `OLYMPUS_CONTRACTS` is enabled; inert otherwise.

`tests/test_specialist_strength.py` covers all three. No capability-count change.

### Changed — Deeper specialist benchmark (strengthens the self-improvement loop)

- Expanded `olympus/benchmarks.json` from 17 items to **50** — **5 per
  user-facing specialist** (was as low as 1 for plutus/iris/chiron/chronos/
  argus/mnemosyne). The benchmark is the signal Prometheus's measured-training
  loop optimizes against and the basis for `olympus scores`; with one item per
  specialist the score was too noisy to tell a good prompt from a bad one and to
  catch regressions. Deeper, varied, harshly-gradeable items give every
  specialist real signal to be strengthened against — no engine change needed,
  the existing train-with-rollback loop now has something to push on.
- `tests/test_benchmarks.py` guards depth (≥5 per user-facing specialist),
  unique ids, valid specialist keys, and well-formed task/criteria.

### Added — "What Olympus learned on its own" readout (`olympus/digest.py`)

- A plain-language summary of the autonomous loop's recent activity: when each
  cycle last ran (world scan, video learning, daily skill distillation,
  training, self-audit), the skill count, and recent world reports / lessons /
  self-upgrades. Read-only over the heartbeat's persisted state + memory — no
  model calls. Surfaced three ways: the `olympus learned` CLI command, the
  `/learned` chat command, and the `recent_learning` agent tool (granted to
  Metis) so you can just ask "what did you learn while I was away?".
- New `memory.recent_titles(category, n)` helper. Tools 58 → 59; commands
  66 → 67. THREAT_MODEL row, `capabilities.json`, README counts, and
  `tests/test_digest.py` added.

### Changed — Always-on learning runs by default in the cloud deploy

- `deploy/docker-compose.yml` now enables the `heartbeat` service by default, so
  a standard `docker compose up -d` runs Olympus's self-learning loop (world
  scans, YouTube learning, daily skill distillation, weekly self-audit, and any
  scheduled operator jobs) around the clock — not just the chat server. It
  shares the memory volume, so what it learns is immediately available to chat.
- Background spend is bounded by `OLYMPUS_DAILY_BUDGET` (deploy `.env.example`
  ships `=20`; cycles skip once the cap is hit). Documented loudly that
  `OLYMPUS_DAILY_BUDGET=0` means *unlimited*, not off — to disable the loop you
  comment out the `heartbeat` service. `deploy/README.md` updated accordingly.

### Added — Operator interactive layer: secure capture, inline approval, auto-launch, advanced mode

- **Secure credential capture out of the model loop** (`olympus/securecapture.py`
  + `operator_remember_login`): a tool records a pending request (domain only);
  after the turn a private `getpass` prompt collects the credentials and stores
  them in the vault. The password never passes through the model or a tool arg.
- **Plain-English approval** (`olympus/approvals.py` + `interactive.after_turn`):
  a held action is shown as its preview and confirmed with a simple "yes" —
  mapped to `actions.approve`/`reject`. No `olympus approve <id>` needed.
- **Browser auto-launch** (`browser.launch_local` / `_find_chrome` /
  `_chrome_args`): finds Chrome (PATH or the bundled Playwright build) and starts
  it with remote debugging, headed by default so manual sign-in is visible.
  Opt-in via `OLYMPUS_BROWSER_AUTOLAUNCH=1`; `_build_transport` uses it.
- **Advanced-mode toggle** (`set_advanced_mode` → `operator.advanced`): off by
  default keeps everything plain-English; the Hermes prompt hides env/CLI/IDs
  unless it's on.
- New module `olympus/interactive.py` centralizes the post-turn interactions
  behind an injectable IO (fully unit-tested); `tui.run` calls it once per turn,
  guarded. Tools 56 → 58. THREAT_MODEL rows, `capabilities.json`, README, and
  `tests/test_interactive.py` added.

### Added — Operator for non-engineers: plain-English setup (`docs/DESIGN_OPERATOR_UX.md`)

- The operator can now be turned on and authorized **per site, through
  conversation** — no env vars, no CLI, no "vault" for a normal user. Settings
  persist per-user in `prefs`; `enabled(user)` is `env OR the user's opt-in` and
  `authorized(user, domain)` is `(env domains OR their authorized sites) AND the
  egress allowlist`. The `OLYMPUS_OPERATOR*` env vars remain as an additive
  engineer/admin override.
- **Manual sign-in is the default and works end to end with no password
  handling:** the person signs in themselves and Olympus reuses the session —
  it never sees or stores a password. An opt-in **remember** mode stores
  credentials in the vault via a secure local prompt (primitive shipped;
  in-chat secure capture is the next interactive step).
- **Three conversational tools** for HERMES (53 → 56): `operator_authorize_site`
  (manual | remember), `operator_forget_site`, `operator_status`. The Hermes
  prompt leads with manual sign-in and never tells a non-technical user to set
  env vars or run commands.
- All operator gating (`browser_login`, `browser_operate`, `operator_schedule`,
  scheduled jobs, and the spine `execute`) is now per-user; scheduled jobs are a
  silent no-op for users who haven't enabled the operator. THREAT_MODEL rows,
  `capabilities.json`, README count, and `tests/test_operator_ux.py` added.

### Added — HERMES operator, Phases 2-4: credentialed actions, always-on, self-healing

- **Credentialed actions on the approval spine (Phase 2).** `browser_operate`
  runs declarative action templates (`site_template_record`) as
  `actions.ActionType`s — two are registered: `browser_operate` (NOTABLE, can
  auto-run within a granted `browser.operate` scope + autonomy) and
  `browser_operate_irreversible` (IRREVERSIBLE, **always** requires explicit
  approval). They inherit the whole spine: deny-first scopes, daily runaway
  caps, and the immutable audit log. Templates are ordered assert/click/fill/
  wait steps — there is no "interpret the page" path.
- **Always-on operator jobs (Phase 3).** `operator_schedule` stores standing
  jobs that `heartbeat.tick()` runs via `operator.run_due()`. Every run goes
  back through the spine, so irreversible templates still wait for approval and
  everything is scope/budget gated. No-op unless `OLYMPUS_OPERATOR` is on.
- **METIS/Prometheus weave (Phase 4).** `operator_review` (Metis + daily
  heartbeat) prunes site profiles that fail consistently; `propose_site_profile`
  (Prometheus) files human-reviewable profile/selector patches that are never
  self-applied. Loadouts: Hermes gains operate/template/schedule, Metis gains
  the review tool, Prometheus the proposal tool.
- Tools 48 → 53; ActionTypes 11 → 13. THREAT_MODEL.md rows, `capabilities.json`,
  README counts, and `tests/test_operator_phases.py` added. Real-Chrome smoke
  still green.

### Added — HERMES browser operator, Phase 1 (`docs/DESIGN_OPERATOR.md`)

- **New specialist HERMES (Operator)** — the first agent that can perform
  **credentialed** browser actions. It is deliberately **non-ingesting**
  (`web=False`, and it has neither `browser_open` nor `browser_read`), so it
  legitimately keeps the actuator while capability separation still holds
  system-wide: the agent that reads the open web (Argus) never holds the
  actuator, and the agent that holds the actuator (Hermes) never reads open-web
  prose as instructions.
- **Four new tools** (44 → 48), all threat-modeled: `browser_login`
  (vault-backed login via a declarative site profile), `browser_exists` (a
  yes/no selector predicate — never page prose), and `site_profile_record` /
  `site_profiles` (provenance- and reliability-scored login recipes).
- **Site Profiles** (`browser.SiteProfile`) — declarative per-domain login
  recipes (login URL + selectors + success marker) with a content hash,
  provenance, and an outcome-derived reliability score. Stored at
  `MEMORY_DIR/site_profiles.json`. Credentials are **not** stored here.
- This is Phase 1 of the operator: login + structured inspection only, **no
  irreversible actions**. Always-on heartbeat playbooks and METIS/Prometheus
  weaving are later phases.

### Security — operator is off by default and fails closed

- Master switch `OLYMPUS_OPERATOR` (default off) disables the entire
  credentialed path. `browser_login` additionally requires the domain to be in
  `OLYMPUS_OPERATOR_DOMAINS` **and** on the egress allowlist, and a vault entry
  `site:<domain>` to exist — each missing gate fails closed.
- Credentials come from the encrypted vault (`vault.get`); the password is
  filled into the page but **never enters the model context or any output**.
- `browser_login` is a registered `ACTION_TOOL` (stripped from any ingesting
  run); a missing post-login success marker (2FA/CAPTCHA/selector drift) makes
  it stop and report rather than retry.

### Added — Governed browser harness (`olympus/browser.py`)

- A stateful Chrome-DevTools-Protocol harness that lets a specialist drive a
  **real** browser — Olympus's answer to the open-web "agent + CDP" pattern,
  built so the browser inherits Olympus's governance instead of bypassing it.
  The transport is pluggable: a lazy WebSocket transport attaches to Chrome
  (`OLYMPUS_BROWSER_CDP_URL`, optional `websockets` extra), while an in-memory
  `FakeTransport` keeps tests and headless CI fully offline — the core
  dependency set is unchanged.
- **Five named, threat-modeled tools** (39 → 44): `browser_open`,
  `browser_read`, `browser_act`, `browser_skill_record`, `browser_skills`.
  Granted to **Argus**.
- **Provenance-scored skill library.** A browser skill carries its source,
  author, creation time, a content hash, and an outcome-derived reliability
  score (`successes/runs`); `browser_skills` ranks by measured reliability, not
  blind trust. Stored at `MEMORY_DIR/browser_skills.json`.
- **Replayable session ledger.** Every CDP call is appended to a per-session
  ledger, so a browser session is auditable rather than a black box.
- **Real-browser attach + opt-in smoke test.** `OLYMPUS_BROWSER_CDP_URL` accepts
  a DevTools base (`http://host:port`, auto-discovering a page target) or a
  `ws://` page URL; `tests/test_browser_smoke.py` drives real headless Chrome
  through the live transport, skipped unless `OLYMPUS_BROWSER_SMOKE=1`.

### Security

- **Egress + SSRF gate on every navigation.** `browser_open` routes through
  `security.url_block_reason` — no internal/metadata address, and under
  sovereign mode no non-allowlisted host, can be reached. `browser_open` /
  `browser_read` join `INGESTION_TOOLS`, so their output is wrapped as
  untrusted.
- **Capability separation closes the credential kill-chain.** `browser_act`
  (click/type on a possibly logged-in session) is a registered `ACTION_TOOL`,
  so it is stripped from any run that also ingests untrusted page content. Argus
  reads/learns via the harness but, because it ingests the web, never holds the
  credentialed actuator in the same run — proven in `tests/test_browser.py`.
- **Hardening pass.** The SSRF/egress gate is re-run against the *landed* URL
  after navigation (a 3xx redirect or JS navigation onto an internal host is
  blocked and the tab is sent to `about:blank` instead of surfacing its
  content); the real transport bounds a single CDP frame (anti-OOM) and times
  out a stuck reply instead of wedging the agent; `browser_open` waits (bounded)
  for `readyState=complete` before reading; the CDP ledger is a bounded circular
  buffer; and the skill store caps field/step lengths, bounds the library
  (dropping lowest-reliability skills), and skips malformed entries.
- See [docs/BROWSER_HARNESS.md](docs/BROWSER_HARNESS.md) for the full
  strengths→moats / weaknesses→credibility-assets rationale.

## [0.22.0] — unreleased (prepared; not tagged or published)

### Added — OpenAI-compatible inbound endpoint (SPEC-01)

- **`olympus/openai_server.py`** (new) + the `web.py` handler — expose Olympus
  as an OpenAI Chat Completions API: `POST /v1/chat/completions` (streaming and
  non-streaming) and `GET /v1/models`, so any OpenAI client/IDE/app can drive
  the full council by changing only `base_url` + `model` + `api_key`. Pure
  stdlib request/response translation (messages→prompt, `chat.completion` /
  `chat.completion.chunk` shaping, SSE ending in `data: [DONE]`, a usage
  estimate); each request runs the existing `orchestrator` pipeline — no new
  framework, no parallel pipeline.
- **`config.api_keys()`** (`OLYMPUS_API_KEYS`) gates it: `/v1/*` is
  **loopback-only** when unset (never a silent open relay), `401` on a bad
  bearer token, `403` for a remote peer. `olympus serve` mounts the same
  handler. See [docs/OPENAI_ENDPOINT.md](docs/OPENAI_ENDPOINT.md).

### Added — provable zero-egress sovereignty mode (SPEC-02, off by default)

- **`security.py`** — a single egress choke point `assert_egress_allowed(host)`
  (typed `EgressBlocked`) with `host_on_allowlist()` / `egress_allowed()`.
  Loopback, local providers, and `OLYMPUS_EGRESS_ALLOWLIST` (hosts/IPs/CIDRs)
  are permitted; the existing SSRF guard routes through the same choke. A pure
  no-op when sovereign mode is off — behaviour byte-for-byte unchanged.
- **`config.py`** — `OLYMPUS_SOVEREIGN` / `OLYMPUS_EGRESS_ALLOWLIST`; the
  `ModelPool` filters non-local members **before** model selection and **fails
  closed** if none remain (no silent remote fallback), plus per-request
  data-class routing (`olympus ask --data-class`, the `X-Olympus-Data-Class`
  header). `olympus status` shows the mode, allowlist, and eligible models. See
  [docs/SOVEREIGNTY.md](docs/SOVEREIGNTY.md).

### Added — production-real audit & verification (SPEC-03)

- **`olympus verify --run <id>`** — one PASS/FAIL that combines `replay_run`
  (the byte-identical decision path) **and** the decision-log signature against
  the trusted key, exiting non-zero and naming the divergence/signature failure.
  `--require-production` fails a run signed under the public default seed, and a
  default-seed pass is loudly labelled `DEV / UNVERIFIED`.
- **`web.py`** — `/api/status` reports a `signing` posture object, and an OpenAI
  endpoint answer carries `X-Olympus-Run-Id` + `X-Olympus-Audit` so a caller can
  locate and verify the run behind it. New [docs/VERIFY.md](docs/VERIFY.md);
  `docs/SIGNING.md` gains seed generation, HSM/KMS guidance, and a key-rotation
  procedure.

### Security

- **Hardened the `/v1/*` loopback boundary** against header spoofing and the
  reverse-proxy open-relay trap. The remoteness decision reads only the kernel
  peer socket (never a forwarding header), unwraps IPv4-mapped IPv6, and — when
  no `OLYMPUS_API_KEYS` are set — refuses non-loopback peers and requires a key
  whenever a proxy forwarding header is present. Header values are never
  trusted, only their presence, and only to deny.

### Added — egress gateway, Phase A (boundary layer, off by default)

- **`olympus/egress.py`** — the unified egress chokepoint. `classify()` (pure,
  regex-based, reusing `security.py`'s `_KEYISH`/`_URL_CRED`/`_EMAIL`/`_PHONE`/
  `_LONGNUM`) labels a payload PUBLIC/OPERATIONAL/SENSITIVE; `guard()` checks it
  against a per-channel policy matrix and returns ALLOW / REDACT (POOLED only) /
  HOLD. No LLM, no new dependency.
- **`config.egress_guard_enabled()`** (`OLYMPUS_EGRESS_GUARD`, off by default,
  matching `require_byok`) wires the guard into the two raw actuators
  `tools._send_email` and `tools._call_webhook` (Phase A only). A SENSITIVE
  payload is HELD and routed to the existing actions spine via two new
  `IRREVERSIBLE` action types (`email_egress_held`, `webhook_egress_held`,
  reusing the existing executors); within-policy sends are unchanged.
- The approved-execution path bypasses the guard (`_approved=True`) so an
  approved HOLD sends exactly once (no held→execute→held loop).
- Each decision is recorded as a new `decision_type="egress"` record in the
  existing signed Trace (via a per-thread current-Trace accessor in `trace.py`);
  the mode is stamped into `tr.meta` and restored by `replay_run`. Off by
  default — zero change to a fresh install. See
  [docs/DESIGN_BOUNDARY_LAYER.md](docs/DESIGN_BOUNDARY_LAYER.md).

### Added — hard output contracts (primitive, off by default)

- **`olympus/contracts.py`** — a pure, dependency-free check of a specialist's
  final output against an optional `OutputContract` (size ceiling, must-be-JSON
  + shallow required-keys/top-level-type schema check, client-side tool-call
  cap). Returns a `ContractResult(ok, violations)`; no I/O, no config reads.
- **`config.contracts_enabled()`** (`OLYMPUS_CONTRACTS`, off by default,
  matching the `require_byok` convention) gates enforcement at
  `orchestrator._run_one`. A violation **fails closed** but degrades gracefully,
  returning the same "treat this part as missing" placeholder the existing
  failure path uses, so verify/synthesis tolerate it unchanged.
- Each check is recorded as a new `decision_type="contract"` record in the
  existing signed, re-executable decision log (`trace.py`) — no parallel log.
  The enforcement mode is stamped into `tr.meta` and restored by `replay_run`
  so a contracts-on run replays in the same mode. Ships with every specialist
  at `contract=None` (zero behaviour change). See
  [docs/DESIGN_OUTPUT_CONTRACTS.md](docs/DESIGN_OUTPUT_CONTRACTS.md).

## [0.21.0] — 2026-06-27

### Added — guided onboarding (Hermes-style)

- **`olympus/providers.py`** — a curated provider catalog (Anthropic, OpenAI,
  DeepSeek, GLM/Z.AI, Kimi/Moonshot, Groq, OpenRouter, Gemini, Mistral, Ollama,
  custom) with base URLs and auth styles, plus `fetch_models()` that lists the
  provider's real model IDs so users don't guess model names, and
  `build_pool_config()` to assemble the multi-key pool.
- **Guided `olympus setup` wizard** (numbered menus — robust over SSH/WSL):
  pick one or more providers, **auto-discover models**, compose them into the
  model pool, and optionally enable fast mode, choose an execution backend, and
  connect a messaging gateway — then it writes `config.env`. Subscription auth
  is first-class: **run on a Claude subscription** (the `claude-code` provider)
  with no API key.
- **Rich launch screen + status line** (`olympus/tui.py`): a branded welcome
  with the model-pool assignment and a capability overview from the manifest,
  and a per-turn status bar (model · seconds · today's spend · fast-mode).

## [0.20.0] — 2026-06-27

### Added — latency controls (fast mode)

- **`OLYMPUS_FAST=1`** — a latency dial: the lightweight pipeline stages
  (route/plan) run on the pool's **fastest** model (`ModelPool.fastest()`, picked
  by model-name hints like flash/air/mini/8k/haiku) and the optional Athena
  **review** stage is skipped — trading a little polish for markedly lower
  latency while keeping the strong model for the actual specialist work and the
  final synthesis.

### Fixed

- **Final-compose no longer crashes the run.** A provider error in the
  `synthesize` stage previously raised an unhandled traceback; it now degrades
  gracefully to the already-verified findings (both the blocking and streaming
  paths).
- **OpenAI-compatible backend now bounds output** with `max_tokens`
  (`config.MAX_TOKENS`). It previously sent no cap, so reasoning models could
  generate unboundedly — a significant latency and cost sink.

## [0.19.0] — 2026-06-27

### Added — per-user adaptive evolution (gets smarter the more you use it)

- **`olympus/companion.py`** — the personal counterpart to Metis's shared
  learning cycle. Every few conversations (`OLYMPUS_EVOLVE_EVERY`, default 6),
  Olympus re-distills *that user's own* history — durable facts, 👍/👎 feedback,
  and corrections — into a compact, private **working model** ("how to work well
  with this person") that is injected into every answer for them, and nobody
  else. A visible **growth level** (new → acquainted → familiar → attuned →
  trusted companion) deepens with use. Surfaced via `olympus growth` and the
  `/growth` chat command. Runs in the background, never delays a reply, and
  keeps the prior model on any failure so accumulated learning is never wiped.

## [0.18.0] — 2026-06-27

### Added — release tooling

- **`scripts/bump_version.py`** — one command bumps the version (patch/minor/
  major or `--set`) across `pyproject.toml` + `olympus/__init__.py` and cuts the
  CHANGELOG (`[Unreleased]` → dated section, fresh `[Unreleased]`, fixed compare
  links). `--dry-run` previews. Documented in RELEASING.md.

### Security

- **Per-specialist skill-index scoping.** Skills tagged for one specialist no
  longer appear in every other specialist's prompt. Previously a skill was
  benchmark-gated against only its own specialist yet was injected into the
  shared index every specialist sees, so it could silently degrade specialists
  it was never measured against. The index is now scoped per specialist (its own
  skills + untagged/`all` global skills); evaluation uses the same scoped view,
  and global/untagged skills are gated against the whole benchmark.
- **Pinned release-signing key.** The production Ed25519 public key is committed
  to `olympus/witness_pubkey.txt` (and shipped in the wheel), so `olympus verify`
  trust-pins releases to exactly this key — a manifest re-signed with any other
  key is rejected, not merely checked for internal consistency.

### Hardening

- Sandbox: empty commands are rejected, the timeout is clamped, and an unknown
  backend falls back to local; `docker` runs are network-isolated by default
  (opt in with `OLYMPUS_EXEC_NETWORK=1`). Added docker-command coverage.
- Scheduler: stored jobs are bounded (`MAX_JOBS`, keeping the most recent) so
  the schedule file can't grow without limit.

## [0.17.0] — 2026-06-27

### Added — install & updates (frictionless onboarding)

- **Top-of-README Install section** with copy-pasteable Linux / macOS / Windows
  one-liners, the pip/pipx path, and the upgrade commands.
- **`olympus upgrade`** (`olympus/selfupdate.py`) — one command to update to the
  latest release; detects whether you installed via pipx, the install-script
  venv, or plain pip and runs the right thing (`--git` forces a from-source
  upgrade). **`olympus version`** prints the installed version, and
  `__version__` now reflects the installed distribution's real version.

### Packaging

- Switched to the PEP 639 SPDX license declaration (`license = "MIT"` +
  `license-files`), so the built wheel/sdist pass `twine check` on current
  tooling and publish cleanly to PyPI. Requires `setuptools>=77` to build.
- Installers now **prefer the PyPI release and fall back to source**: they try
  `pip install olympus-council` first and only install from the GitHub repo if
  PyPI is unavailable — so they work before the package is published and switch
  to stable releases automatically once it is (`OLYMPUS_PACKAGE` overrides).
- CI now builds the wheel/sdist and runs `twine check` on every push/PR
  (new `package` job), catching packaging regressions before release.

### Added — operator capabilities (Hermes gap-closure)

A batch of capabilities closing the operator-axis gaps identified against
NousResearch/hermes-agent. Each ships with tests and is bound to the
CI-verified capability manifest.

- **Real execution environment** (`olympus/sandbox.py`). A workspace-confined
  shell + file surface with `local` and `docker` backends (timeout- and
  output-capped, path-escape-refused). Exposed as approval-gated actions
  `run_command` (irreversible) and `write_file` (reversible/undoable), plus
  read-only `read_file` / `list_dir` tools. Hephaestus gains the loadout.
- **Scriptable subagents** (`olympus/subagents.py` + `spawn_subagent` tool):
  ad-hoc, isolated, parallel specialist fan-out with per-branch failure
  containment.
- **Natural-language cron** (`olympus/scheduler.py`, `olympus schedule`,
  `schedule_task` tool): user-defined recurring tasks in plain English, run
  unattended by the heartbeat, results delivered to any platform.
- **Discord / Slack / Signal gateways** (`olympus/{discord,slack,signal}.py`)
  over a shared `gateway.py` router; Slack HMAC + Discord Ed25519 request
  verification.
- **Rich TUI** (`olympus/tui.py`): multiline input, `readline` slash-command
  autocomplete, streamed answers.
- **Cross-session search** (`olympus/search.py`, `olympus search`,
  `search_sessions` tool): SQLite FTS5 over all persisted conversations, with a
  LIKE fallback; indexed live on save.
- **Training-trajectory export** (`olympus/trajectories.py`,
  `olympus export-trajectories`): conversations → SFT pairs and traces →
  decision trajectories as JSONL.
- **Serverless / hibernation mode** (`olympus/hibernate.py`, `olympus tick`,
  `olympus next-wake`): run one tick and report the next-due time so an external
  scheduler can wake Olympus on demand.
- **agentskills.io interop** (`olympus/skillpack.py`, `olympus skill-import` /
  `skill-export`): import/export skills in the open SKILL.md standard.
- **Migration importer** (`olympus/migrate.py`, `olympus import-agent`): fold an
  OpenClaw/Hermes-style agent's memories, profile, and skills into Olympus;
  API keys are detected and reported, never silently stored (opt-in `--keys`).
- **Media tools** (`olympus/media.py`): `generate_image`, `text_to_speech`, and
  a link-extracting `browse_page`; generative tools degrade gracefully without a
  key and write only into the confined workspace.
- **Windows installer** (`install.ps1`): PowerShell one-liner mirroring the
  POSIX installer.

### Added — disaster recovery

- **Off-droplet data backups** (`olympus backup` / `olympus restore`,
  `olympus/backup.py`). Archives `MEMORY_DIR` (per-user memory, accounts, the
  encrypted OAuth tokens, the signed decision log), **encrypts** it at rest with
  the vault key, **signs** it with the witness Ed25519 root of trust, and hands
  it to `OLYMPUS_BACKUP_CMD` for off-machine delivery. Restore verifies the
  signature and every file's SHA-256, rejects path-traversal entries, and won't
  clobber a non-empty target. Runs on a cadence via the heartbeat (or the
  dedicated, token-free `backup` compose service). See
  [docs/BACKUPS.md](docs/BACKUPS.md).

## [0.16.0]

First formally catalogued release. Olympus is a provider-agnostic, multi-agent
assistant (Zeus routes, Athena supervises a council of specialists, Aletheia
fact-checks) with durable per-user memory, human-approved actions, and a web,
Telegram, and WhatsApp surface. This entry records the verifiability,
reliability, and public-launch work that defines the line; earlier history lives
in the git log and pull requests #1–#49.

### Added — verifiable reasoning & supply chain ("the moat")

- **Re-executable decision log.** Every run freezes its LLM requests/responses
  and pairs them with structured decision records, so a recorded run can be
  re-executed against the frozen responses and proven byte-identical, or the
  exact diverging request is pinpointed (`olympus replay`, `olympus explain`).
  See [docs/DECISION_LOG.md](docs/DECISION_LOG.md).
- **Signed releases & signed decision log.** One Ed25519 root of trust signs
  both a release manifest (`verification.json`, every tracked file's SHA-256)
  and each run's decision path. `olympus sign` / `olympus verify` /
  `olympus verify --log <run_id>`. See [docs/SIGNING.md](docs/SIGNING.md).
- **Capability manifest** generated from code and bound to the README numbers,
  enforced in CI (`olympus capabilities --check`). See
  [docs/CAPABILITIES.md](docs/CAPABILITIES.md).
- **Memory format contract** with a versioned on-disk `schema_version` and
  forward migration (`olympus memory migrate`). See
  [docs/MEMORY_FORMAT.md](docs/MEMORY_FORMAT.md).
- **Supply-chain integrity:** hash-pinned `requirements.lock`, a no-prerelease
  check, and a CycloneDX SBOM in CI. See [docs/SUPPLY_CHAIN.md](docs/SUPPLY_CHAIN.md).
- **Threat model** bound to the live tool handlers and enforced by
  `scripts/check_threat_model.py`. See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

### Added — reliability gate

- **Replay self-check tripwire:** the heartbeat re-runs real prompts on a
  cadence and a CI replay-gate workflow does the same on a schedule; a run that
  stops replaying byte-identically escalates (memory note, Telegram alert, and
  an auto-filed GitHub issue) instead of silently rotting the audit trail.
- **Operator reliability gate** (`scripts/reliability_gate.py`): runs three
  distinct prompts unattended end-to-end on a real key, proves each replays with
  zero new API calls, enforces a spend cap, and reports total spend.

### Added — public-launch safety kit

- **Problem-report channel:** a `📣 report` button and `/api/report` (works
  before login), captured durably and pushed to the operator (`olympus reports`).
- **Operator error capture:** unexpected failures are recorded to a durable
  log and rate-limited Telegram alert (`olympus errors`).
- **Cost protection:** bring-your-own-key as a *free allowance* — keyless users
  get `OLYMPUS_FREE_CHATS` operator-funded chats per day, then continue on their
  own key — alongside the all-or-nothing `OLYMPUS_REQUIRE_BYOK`, per-day and
  per-minute caps, and a daily budget.
- **Privacy & Terms pages** (`/privacy`, `/terms`) written to match real
  behaviour, with operator identity from `OLYMPUS_OPERATOR_NAME` /
  `OLYMPUS_OPERATOR_CONTACT`.

### Security — pre-release hardening

- Signing refuses to produce a release manifest under the public default seed
  (forgeable) unless explicitly marked `--dev`; verification trusts a manifest
  only against a pinned public key, and rejects dev manifests for release.
- The replay gate fails *loudly* (logged, never swallowed) on an unexpected
  internal error, distinguishing a genuine divergence from an infrastructure or
  account skip — so an empty wallet can't masquerade as a green gate.
- Load-bearing best-effort paths (memory extraction, connector token lookups)
  now log unexpected failures instead of failing silently; telemetry swallows
  are intentionally left untouched.
- `Trace.decision(status=...)` is mandatory, so a failure path can no longer
  silently record success and poison per-agent trust scoring.

[Unreleased]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.26.0...HEAD
[0.26.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.25.0...v0.26.0
[0.21.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/maadjiba24-afk/Olympus-/releases/tag/v0.16.0
