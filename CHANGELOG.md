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

> **Release-state note.** As of this writing the latest *published* release is
> **0.21.0** (git tag `v0.21.0`, PyPI `olympus-council 0.21.0`). The `0.22.0`
> through `0.24.0` sections below were prepared and dated but **never tagged or
> published** — so they are not releases yet, and everything from `0.22.0`
> down to this note is effectively unreleased pending a tagging decision (see
> RELEASING.md). The dated headers are kept for review; re-date and tag them
> when a release is actually cut. `pyproject.toml` currently reads `0.24.0` as
> the in-development version, not a shipped one.

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

[Unreleased]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.21.0...HEAD
[0.21.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/maadjiba24-afk/Olympus-/releases/tag/v0.16.0
