# Page Agent Analysis & Adoption Tracking

Analysis-only tracker for [alibaba/page-agent](https://github.com/alibaba/page-agent).
This document is a complete inventory of Page Agent's features and capabilities,
a critique of its weaknesses / security risks / design gaps, and a watchlist of
what is worth turning to Olympus's own agent framework. **No Page Agent code is
used here** — this is competitive/inspiration analysis only, read from a shallow
clone.

- **Last checked:** 2026-07-23 (commit `b7401a0`, `main`, npm `page-agent` 1.12.2)
- **Adoption status:** 🔄 **ABSORBING** (ADR 0014). Page Agent's entire
  capability surface is already matched or exceeded by Olympus's governed browser
  harness (`olympus/browser.py`) and web-context suite (`olympus/webctx.py`), and
  its *distribution model* (zero-install in-page JS, page-origin sharing) is a
  deliberate Olympus non-goal. The genuinely additive ergonomics/robustness
  patterns from the watchlist (§3) are being absorbed natively — each one built on
  the Olympus spine with the corresponding page-agent weakness inverted into a
  structural strength:
    - **§3.1 — resilient tool-call repair → SHIPPED** as `olympus/toolcall_repair.py`
      (ADR 0014 (a)): wired into `openai_compat.run_agent` + `bedrock_converse`,
      **refusal-safe by construction** (recovers a call only when the content names
      an offered tool, so a refusal is never laundered into an action — inverting
      page-agent's `autoFixer`, which can). `tests/test_toolcall_repair.py`.
    - **§3.2/§3.3 — perception deltas + scroll geometry → SHIPPED** in
      `browser.observe()` (ADR 0014 (b)): a `*[i]` marker on elements new since the
      last look on the same URL (keyed on the durable selector), a geometry header
      (pages above/below, % scrolled), and a bounded list of scrollable containers
      with remaining distance — all pure measurement, fail-soft, no ingestion
      surface. `tests/test_browser.py`.
    - **§3.4 — human-fidelity click + landing hit-test → SHIPPED** in
      `browser.act('click')` (ADR 0014 (c)): a scroll-to-center +
      `elementFromPoint` probe drives a **trusted** coordinate CDP click on a clear
      point (stronger than page-agent's untrusted dispatch), and — the inversion —
      refuses a blind coordinate click when an overlay obscures the point,
      dispatching to the intended element and flagging the obstruction instead of
      clicking whatever is on top. `tests/test_browser.py`.
    - §3.5 default-on pre-prompt redaction → ADR 0014 (d), planned.
    - §3.6 governed `/llms.txt` consumption → ADR 0014 (e), planned.
  The four non-goals in §5 stay declined (ADR 0014 "NOT absorbed").
- **What it is:** a client-side, **zero-install in-page GUI agent** — "one
  script gives any web page its own AI agent." Drop a `<script>` tag (or `npm
  install page-agent`) and the page gains a natural-language agent that reads its
  own DOM as text and drives clicks/typing/scrolling with synthetic events. No
  browser extension, no Python, no headless browser, no screenshots. ~28k GitHub
  stars, MIT, TypeScript.
- **Shape:** a TypeScript monorepo (npm workspaces) of 7 published packages:
  `page-controller` (DOM perception + actuation), `llms` (OpenAI-compatible
  client), `core` (the ReAct agent loop), `page-agent` (the public entry + UI
  panel), `ui`, `mcp` (a localhost bridge so external MCP clients can drive the
  browser), and `extension` (an optional Manifest-V3 Chrome extension for
  multi-tab tasks). The DOM-processing pipeline and system prompt are **derived
  from [`browser-use`](https://github.com/browser-use/browser-use)** (credited).
- **Relevance to Olympus:** Page Agent is the *inverse bet* to Olympus on the
  same problem. Both do **text-based in-page DOM automation** — a numbered map of
  interactive elements, index-addressed click/type/select/scroll, a ReAct loop.
  But Page Agent optimizes for **frictionless reach** (runs inside the page's own
  JS context, no install, no permissions, no backend) at the cost of **zero
  governance**: the agent shares the page's origin, sends cleaned page HTML to a
  third-party LLM with no guaranteed PII redaction, ships an `eval()`-based JS
  tool, and its "let an external app control your real logged-in browser" MCP
  bridge is a single `window.confirm()` over an unauthenticated localhost socket.
  Olympus makes the opposite trade: it drives Chrome over CDP from a governed
  process, and every actuation passes through capability-separation
  (`security.filter_tools`), the SSRF/egress guards, and the approval spine
  (`actions.py`). Page Agent is worth studying for its **perception ergonomics
  and distribution model**, and as a sharp illustration of *why Olympus keeps
  enforcement structural rather than prompt-/dialog-level.*

---

## 1. Complete feature & capability inventory

### 1.1 Product surface & distribution

- **One-line CDN integration** — a single `<script src=".../page-agent.demo.js">`
  from jsDelivr (or a `registry.npmmirror.com` China mirror) auto-mounts a demo
  agent wired to a **free testing LLM** hosted on Alibaba Cloud China Function
  Compute (`packages/page-agent/src/demo.ts`: `DEMO_BASE_URL =
  'https://page-ag-testing-…​.cn-shanghai.fcapp.run'`). `?autoInit=false`,
  `?model=`, `?baseURL=`, `?apiKey=`, `?lang=`, `?showPanel=` query params
  reconfigure it.
- **NPM library** — `import { PageAgent } from 'page-agent'`; construct with
  `{ model, baseURL, apiKey, language }` and `await agent.execute('Click the
  login button')`.
- **Built-in UI panel** (`packages/ui`) — a draggable in-page chat panel with
  activity cards, i18n (en-US / zh-CN), light/dark.
- **Optional Chrome extension** (`packages/extension`, WXT/Manifest V3) — adds
  multi-tab reach (`open_new_tab` / `switch_to_tab` / `close_tab`) and a "hub"
  tab that an external process can drive.
- **MCP server (beta)** (`packages/mcp`) — an `npx @page-agent/mcp` stdio server
  + localhost HTTP/WebSocket bridge that lets Claude Desktop / Cursor / Copilot
  issue `execute_task` / `get_status` / `stop_task` against the extension's hub
  tab. This is the "let your agent client control your browser" story.

### 1.2 Agent architecture — a single-page ReAct loop

- **`PageAgentCore`** (`packages/core/src/PageAgentCore.ts`, 661 LOC) is an
  `EventTarget` running a classic **observe → think → act** loop
  (`execute()`:210): each step calls `pageController.getBrowserState()`, assembles
  a system + user prompt, invokes the LLM, executes one action, loops. Default
  `maxSteps = 40`, `stepDelay = 0.4s`.
- **Forced "MacroTool" (`AgentOutput`) pattern** (`#packMacroTool()`:386) — all
  tools are folded into **one** tool whose schema is a Zod `union` over each
  tool's input, plus reflection fields (`evaluation_previous_goal`, `memory`,
  `next_goal`). The LLM is forced via `tool_choice: {name:'AgentOutput'}` to emit
  exactly one action per step (`parallel_tool_calls: false`). This is the "reflect
  before act" mental model, encoded structurally.
- **Three information streams** (documented at `PageAgentCore.ts:50-59`):
  persistent **history events** (steps, observations, user-takeover, errors — the
  agent's memory), transient **activity events** (thinking/executing/executed/
  retrying/error, for UI only, *not* in LLM context), and **observations** pushed
  into history (URL-change detection, accumulated-wait warnings, remaining-step
  warnings — `#handleObservations()`:538).
- **Event API** — `statuschange` / `historychange` / `activity` / `dispose`;
  cooperative cancellation through a single `AbortController` reaching the LLM
  fetch and every tool via `ctx.signal`.
- **Extensibility hooks** (`packages/core/src/types.ts`): `customTools`
  (add/override/remove tools by name), `customSystemPrompt`, `instructions`
  (`system` + dynamic per-URL `getPageInstructions(url)`), lifecycle hooks
  (`onBeforeTask/Step`, `onAfterStep/Task`, `onDispose`), `transformPageContent`
  (the sole data-masking seam), `onAskUser`, and two `experimental*` flags.

### 1.3 Tool surface (`packages/core/src/tools/index.ts`)

Built-in tools: `done`, `wait` (1–10s, subtracts elapsed LLM time),
`ask_user` (disabled unless `onAskUser` is set), `click_element_by_index`,
`input_text`, `select_dropdown_option`, `scroll` (page or a `data-scrollable`
container, by pages/pixels), `scroll_horizontally`, and **`execute_javascript`**
(gated behind `experimentalScriptExecutionTool`, off by default). Tab tools
(`open_new_tab` / `switch_to_tab` / `close_tab`) are injected by the extension via
`customTools` (`packages/extension/src/agent/tabTools.ts`). `send_keys`,
`upload_file`, and `extract_structured_data` are explicit `@todo`s
(`tools/index.ts:200-202`) — **not implemented**.

### 1.4 DOM perception (`packages/page-controller`, browser-use-derived)

- **`getBrowserState()`** (`PageController.ts:129`) returns `{ url, title, header,
  content, footer }` where `content` is a **simplified, indexed HTML** of visible
  interactive elements in the `[index]<type>text</type>` format, with `\t`
  indentation for DOM nesting and `*[` marking elements new since the last step.
- **Flat DOM tree** (`dom/dom_tree/index.js`, 1745 LOC) deep-walks the DOM,
  including **same-origin iframes** (`node.contentDocument ||
  node.contentWindow?.document`, `:1674`) with coordinate-offset math, and
  builds a `selectorMap: index → element`. Interactivity is heuristic (roles,
  handlers, tags); React roots are marked non-interactive to cut false positives
  (`patches/react.ts`); an antd patch exists (`patches/antd.ts`).
- **Page-geometry hints** (`dom/getPageInfo.ts`) — viewport size, total page
  size, pages above/below, scroll %, injected into the prompt header/footer so
  the model knows when to scroll.
- **`viewportExpansion`** config — `-1` = whole page, else a px band around the
  viewport; `data-page-agent-not-interactive` / blacklist / whitelist to
  exclude/include elements; `keepSemanticTags` to retain nav/header/footer/dialog.

### 1.5 Actuation (`packages/page-controller/src/actions.ts`, 554 LOC)

- **Human-like synthetic input** — `clickElement()` (`:64`) scrolls the element
  into view, **moves a simulated pointer** to the element center, dispatches the
  full spec-ordered `pointerover/enter → mouseover → … → pointerdown → mousedown →
  pointerup → mouseup → click` sequence, and **hit-tests** with
  `elementFromPoint` (temporarily disabling the mask/pass-through) to target the
  deepest real element — matching real browser event targeting. `input_text`
  focuses + sets value + fires `input`/`change`; `select_dropdown_option` matches
  by visible option text.
- **Visual mask** (`mask/SimulatorMask.ts`) — an overlay + animated cursor that
  blocks the human during automation and is bypassed during DOM extraction.
- **`executeJavascript()`** (`PageController.ts:383`) — runs LLM-generated code
  via `eval("(async (signal) => { … })")` with the abort signal in scope.

### 1.6 LLM abstraction (`packages/llms`)

- **OpenAI-compatible only** (`OpenAIClient.ts`) — one `/chat/completions` path,
  BYOK, named tool-choice, schema-validated tool args (Zod), typed error taxonomy
  (`errors.ts`: auth/rate-limit/server/context-length/content-filter/…).
- **Per-model quirk patching** (`utils.ts: modelPatch`) — disables thinking for
  Qwen/DeepSeek, drops `tool_choice` for DeepSeek, sets GPT-5 verbosity/reasoning,
  handles Gemini's `function_call` finish reason. Works with local models
  (Ollama) via any OpenAI-compatible endpoint.
- **Response auto-repair** (`core/src/utils/autoFixer.ts: normalizeResponse`) —
  fixes six-plus common malformed-tool-call shapes (JSON in `content`, action-name
  as tool name, double-encoded args, nested function-call, missing action →
  fallback to `wait`, primitive single-field args). This is a genuinely useful
  robustness layer for weaker/local models.
- **Hooks** — `transformRequestBody`, `customFetch`, optional `temperature`
  (omitted by default since new models reject it), retry with a `retry` event.

### 1.7 Multi-tab & external control

- **`MultiPageAgent`** + **`TabsController`** — cross-tab orchestration; a
  `RemotePageController` proxies DOM ops across the content/background/main-world
  boundary so the same `PageAgentCore` can act on a tab other than the one it
  lives in.
- **MCP bridge** (`packages/mcp/src/hub-bridge.js`) — Node HTTP + `ws`
  `WebSocketServer` on `localhost:38401`; serves a launcher page that triggers the
  extension to open a hub tab; the hub connects back as a WS client and proxies
  `execute`/`stop`. Approval is client-side in the hub
  (`hub-ws.ts: #checkApproval` — a `window.confirm()`, or a persisted
  `allowAllHubConnection` flag).

---

## 2. Critique — weaknesses, security risks, design gaps

Each item was checked against source. Severity is Page-Agent-internal (how much
it matters *to a Page Agent user*), not relative to Olympus.

### 2.1 Security & privacy risks

- **[HIGH] Page HTML → third-party LLM with no guaranteed PII redaction.** Every
  step ships the cleaned interactive-element HTML (including visible text and form
  values) to the configured LLM endpoint. The project's own privacy doc admits
  "The HTML cleaning process … **does not guarantee removal of sensitive
  information**" (`docs/terms-and-privacy.md`). The only mitigation is a
  user-supplied `transformPageContent` regex hook (`types.ts:148`) — opt-in,
  best-effort, and off by default. On a logged-in ERP/CRM/email page this is a
  standing exfiltration surface. *Olympus contrast:* `security.secret_exfil_reason`
  + `sanitize_for_memory` + capability separation are code-enforced and on by
  default.
- **[HIGH] `execute_javascript` is `eval()` of model-authored code in the page
  origin** (`PageController.ts:386`). It is off by default
  (`PageAgentCore.ts:144`, requires `experimentalScriptExecutionTool`) and the doc
  warns it "may bypass some safeguards and data-masking mechanisms" — but once on,
  a prompt-injected page can steer the model into arbitrary same-origin JS
  (token theft, request forgery with the user's cookies). *Olympus contrast:*
  code exec is sandboxed (`sandbox.py`, `--network none`, `cmdguard` fail-closed),
  never same-origin-as-a-victim-site.
- **[HIGH] The MCP bridge lets an external process drive your real, logged-in
  browser, guarded only by a dialog.** The localhost `WebSocketServer`
  (`hub-bridge.js`) sets **no `verifyClient` / Origin check** (confirmed: no
  `origin`/`verifyClient` in the file) — any local process, and plausibly any web
  page via `ws://localhost:38401`, can connect. Authorization is a single
  `window.confirm()` in the hub tab, and once approved (or with
  `allowAllHubConnection`) the session stays open with **no per-task scoping and
  no origin allowlist** (`hub-ws.ts: #checkApproval`, `#approved = true`). This is
  a CSRF/DNS-rebinding-shaped surface onto a fully-authenticated browser.
  *Olympus contrast:* inbound control (A2A server, MCP server) is bearer-auth'd
  and every tool it exposes is governed + capability-filtered.
- **[MEDIUM] Broad extension permissions.** `host_permissions: ['<all_urls>']`
  plus `tabs`/`tabGroups`/`sidePanel`/`storage` and **main-world script
  injection** (`wxt.config.js`, `entrypoints/main-world.ts`). Necessary for a
  universal agent, but it means one compromised/injected page can influence
  cross-tab actions with no per-site consent gate.
- **[MEDIUM] Prompt injection is unmitigated by design.** The page content *is*
  the untrusted input and it flows straight into the model context with no
  trust boundary marker, no capability stripping when ingesting untrusted content,
  and action tools always available in the same turn. The system prompt has
  behavioral guidance ("don't log in if you don't have credentials," "tell the
  user if a captcha appears") but nothing structural. *Olympus contrast:*
  `security.should_wrap` / `wrap_untrusted` + `ACTION_TOOLS` stripping.
- **[MEDIUM] Supply-chain: CDN script tag has no SRI.** The documented one-liner
  uses `crossorigin="anonymous"` but **no `integrity=` hash** (`README.md:60`), so
  a jsDelivr/mirror compromise executes arbitrary JS in every embedding page. The
  China `npmmirror` mirror widens the trust set.
- **[MEDIUM] Demo endpoint data residency.** The default demo routes page HTML to
  Alibaba Cloud **China (cn-shanghai)** servers (`demo.ts`); the terms warn EU/EEA
  users not to use it. Easy to leave on inadvertently during evaluation.
- **[LOW] `apiKey` lives in client-side JS.** Inherent to the BYOK in-page model
  (the doc calls this out and recommends a proxy), but it means the pattern
  encourages shipping keys to the browser.

### 2.2 Design gaps & capability limits

- **[HIGH] Single-page-app only.** The system prompt hard-constrains the agent:
  "You can only handle single page app. Do not jump out of current page. Do not
  click on link if it will open in a new page" (`prompts/system_prompt.md:88`).
  Full-navigation, multi-page flows require the extension. The core library cannot
  follow a normal `<a href>` to another document.
- **[MEDIUM] Cross-origin iframes are opaque.** DOM traversal only descends
  *same-origin* iframes (`contentDocument` access, `dom_tree/index.js:1674`);
  cross-origin payment/login/embed frames are invisible and unactionable.
  *Olympus contrast:* `browser.observe_frame` / `act_in_frame` do governed
  cross-origin OOPIF crossing.
- **[MEDIUM] Memory is a flat text transcript.** History is concatenated verbatim
  into every prompt (`#assembleUserPrompt`); there is **no summarization,
  compaction, vector recall, or long-horizon memory** — long tasks blow context
  and lose early detail. *Olympus contrast:* `memory.py`/`recall.py`/`wiki.py`/
  `embed.py`/`trajectories.py`.
- **[MEDIUM] No planner / subgoal decomposition.** "Planning" is the per-step
  `next_goal` string; there is no plan tree, no task graph, no re-planning
  primitive. Open-ended tasks rely entirely on the model's step-by-step judgment.
- **[MEDIUM] No loop / stuck detection.** `#handleObservations` carries an
  explicit `@todo loop detection` / `@todo console error` (`PageAgentCore.ts:537`);
  the only guardrail is a prompt rule ("don't repeat an action more than 3 times")
  and the `maxSteps` cap. Thrash is caught by the model or not at all.
- **[MEDIUM] No token/cost budget.** Usage is *reported* per step
  (`AgentStepEvent.usage`) but never *enforced* — no budget ceiling, no
  cost-based stop. *Olympus contrast:* per-agent budgets + runaway caps.
- **[MEDIUM] No per-action approval gate.** Once `execute()` runs, the agent
  clicks/types/submits autonomously to completion; there is no
  irreversible/financial-action confirmation step. `ask_user` exists but is
  model-initiated, not a policy checkpoint.
- **[LOW] Errors are dropped from LLM context.** Error events are kept for the UI
  but deliberately excluded from the prompt ("to avoid polluting reasoning",
  `PageAgentCore.ts:626`) — which also means the model can't learn from a failed
  action's error text within the run.
- **[LOW] OpenAI-protocol lock-in.** No native Anthropic/Bedrock/Vertex client;
  everything must be OpenAI-compatible-shaped. No streaming to the user, no
  prompt-caching hooks.
- **[LOW] Tables/structured extraction unsupported.** `scroll_horizontally` is
  annotated "Tables need a dedicated parser… This tool is useless"
  (`tools/index.ts:163`); `extract_structured_data` is an unimplemented `@todo`.
- **[LOW] Thin test coverage** — 7 `*.test.ts` files across the monorepo; the
  DOM/actuation core is lightly covered and hard to test outside a real browser.

---

## 3. Watchlist — what's worth turning to Olympus (ranked)

Olympus already has the hard parts (governed CDP harness, self-healing selectors,
scored skills, SSRF/egress, approval spine). The genuinely borrowable items are
**ergonomic and robustness** patterns, not architecture.

1. **LLM response auto-repair layer** (`autoFixer.ts: normalizeResponse`). A
   provider-agnostic normalizer that salvages six-plus malformed-tool-call shapes
   (JSON-in-content, action-name-as-tool, double-encoded args, missing action →
   safe fallback). High value for Olympus's **weaker/local `openai_compat` and
   `bedrock_converse` models**, where tool-call adherence is shaky. *Fit:* a
   normalize step in `backend.py`/`openai_compat.py` before tool dispatch, guarded
   so it never masks a real refusal.
2. **"New since last step" element marking (`*[index]`)** in the observation.
   Diffing the interactive-element set between steps and flagging *newly appeared*
   controls (post-input suggestions, dialogs) is a cheap, strong signal that Page
   Agent bakes into the prompt. *Fit:* an optional annotation in
   `browser.observe()`'s numbered map (Olympus already tracks a selector map;
   adding an appeared/disappeared delta is small).
3. **Geometry-aware scroll affordances.** Page Agent surfaces pages-above/below,
   scroll-%, and marks scrollable containers with `data-scrollable` + remaining
   scroll distance, so the model scrolls the *right* container. *Fit:* enrich the
   `browser.observe()` header/footer and expose per-container scroll bounds.
4. **Human-fidelity click sequence + `elementFromPoint` hit-testing**
   (`actions.ts: clickElement`). The full spec-ordered pointer/mouse event
   sequence plus deepest-element hit-test defeats overlay/interception bugs that
   trip up naive `element.click()`. *Fit:* audit `browser.act('click')` against
   this ordering; adopt the hit-test-the-landing-target trick for robustness on
   sites with invisible overlays.
5. **`transformPageContent` / per-step content hook as a *masking* seam.** Page
   Agent's hook is opt-in and weak, but the *placement* (post-extraction,
   pre-LLM) is the right chokepoint. Olympus already redacts for memory; a
   symmetric **pre-prompt** redaction pass on observed page text (before it ever
   reaches the model) would close a gap. *Fit:* `security.sanitize_for_prompt`
   applied in the browser/webctx observe path.
6. **`llms.txt` as page context** (`experimentalLlmsTxt`, fetch `/llms.txt` once
   per origin, truncate to 1000 chars). Olympus already has `generate_llmstxt`
   (producing them); *consuming* a site's own `/llms.txt` as a navigation hint is
   a small, symmetric addition to `webctx`/`domainlore`.
7. **Zero-friction "panel-in-the-page" UX** as an *operator* delivery option.
   Not the security model — but the idea that an operator surface can live as an
   in-page panel (vs a separate app) is worth noting for Hermes/operator UX
   (`docs/DESIGN_OPERATOR_UX.md`), if ever exposed through a *governed* channel.

---

## 4. One-paragraph verdict

Page Agent is an elegant, genuinely useful **distribution innovation** — a
zero-install, no-permission, no-backend GUI agent that lives inside the page's own
JavaScript and reads its DOM as text — with a clean ReAct loop, a nice
forced-reflection MacroTool, strong malformed-response repair, and human-fidelity
synthetic input, all derived thoughtfully from browser-use. But its reach comes
from **removing every trust boundary**: page HTML (with unredacted PII) streams to
a third-party LLM, an `eval()` JS tool runs model code in the victim origin, and
its headline "let your MCP client drive your browser" feature is an unauthenticated
localhost socket behind a single reusable `window.confirm()`. For Olympus the
architectural lesson is confirmatory, not aspirational: Olympus already ships the
same in-page DOM-automation surface (numbered element map, index/selector/AX
targeting, shadow-DOM + **cross-origin** iframe traversal, self-healing selectors,
provenance-scored skills) but behind CDP, capability separation, SSRF/egress
guards, and the approval spine — precisely the layers Page Agent trades away for
frictionlessness. **Adopt nothing structural; borrow the auto-repair normalizer,
the "new element" delta, geometry-aware scroll hints, and the human-click
hit-test.** Page Agent's real contribution to the Olympus roadmap is a crisp,
public demonstration of *why the governance is the product.*

---

## 5. Feature → Olympus mapping (already covered)

| Page Agent capability | Olympus equivalent | Verdict |
| --- | --- | --- |
| Numbered interactive-element map (`[index]<type>text`) | `browser.observe()` numbered map (`browser.py:1284`) | Covered |
| Index / text targeting of click/type/select | `browser.act()` verbs by index / CSS / xy (`:1474`) | Covered + more verbs (drag, press-chords, hover, back) |
| Same-origin iframe descent | `observe_frame` / `act_in_frame` incl. **cross-origin OOPIF** (`:1323`) | Exceeded |
| Shadow DOM | `observe()` deep-walks **open shadow roots** (`_DEEP_JS`) | Covered |
| Accessibility signal | `browser.read_ax()` full AX tree (`:1143`) | Exceeded |
| Human-like synthetic events | `browser.act('click'/'type')` CDP input | Covered (audit vs §3.4) |
| Multi-tab | `browser` tab list/switch tools | Covered |
| Persisted reusable flows | `BrowserSkill` / `SiteProfile` — provenance-stamped, reliability-scored, content-hashed (`:1828`,`:2062`) | Exceeded |
| Self-healing when a control moves | `heal_candidate()` trigram re-observe (`:1746`) | Exceeded (Page Agent has none) |
| Captcha handling | `detect_checkpoint()` "detect don't defeat" (`:1304`) | Exceeded |
| `execute_javascript` (`eval`) | sandboxed `code_exec` (`sandbox.py`, `--network none`) | Governed alternative |
| BYOK / OpenAI-compatible / local models | `openai_compat` provider + `moa` + Bedrock/Anthropic/claude-code | Exceeded |
| MCP server (drive browser) | `mcp_server.py` (bearer-auth, governed, capability-filtered) | Governed alternative |
| `/llms.txt` produce | `generate_llmstxt` (`webctx.py`) — *consume* is the §3.6 watch item | Covered (produce) |
| Page scrape → markdown | `web_scrape` markdown/links/jsonld/etc. (`webctx.py`) | Exceeded |

### Non-goals — deliberately **not** to adopt

- **Zero-permission in-page origin sharing.** Page Agent's frictionlessness comes
  from running *as the page*; Olympus's whole thesis is a governed process
  *outside* the victim origin. Adopting the in-page model would discard
  capability separation and the SSRF/egress boundary. (Ties to `docs/VISION.md`
  "no 240 MB browser download" *and* the governance stance — reach ≠ trust.)
- **`eval()` of model code in a live origin.** Structurally incompatible with the
  sandbox / `cmdguard` posture.
- **Unauthenticated localhost control socket + reusable dialog approval.** Inbound
  control must stay bearer-auth'd and per-task governed (A2A / MCP server pattern).
- **Sending unredacted page HTML to the model with only an opt-in regex hook.**
  Redaction must be a default-on, code-enforced chokepoint, not a user
  responsibility.
