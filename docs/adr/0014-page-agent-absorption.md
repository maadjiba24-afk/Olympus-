# ADR 0014: Absorb Page Agent's capabilities natively (in-page GUI-agent surface)

Status: accepted (in progress — decisions land per capability)
Date: 2026-07-23

## Context

A full inventory and security review of [alibaba/page-agent](https://github.com/alibaba/page-agent)
(a zero-install, in-page GUI agent — one `<script>` gives any web page its own
DOM-driving LLM agent; see `docs/PAGE_AGENT_TRACKING.md`) found a clean
capability surface — a numbered interactive-element map, index-addressed
click/type/select/scroll, a ReAct loop with a forced-reflection "MacroTool," a
strong malformed-response repair layer, human-fidelity synthetic input, and a
"let your MCP client drive your browser" bridge — sitting on a security model
Olympus already beats by construction:

- page HTML (with unredacted PII/secrets) streamed to a third-party LLM, the only
  mitigation an opt-in regex hook;
- an `eval()`-based JS tool running model code in the victim page's own origin;
- a headline MCP feature that is an **unauthenticated** localhost WebSocket (no
  `Origin`/`verifyClient` check) behind a single, reusable `window.confirm()`;
- no capability separation, no SSRF/egress boundary, no approval spine, no
  per-action gate, no token/cost budget, no loop detection, and a memory that is
  a flat verbatim transcript;
- OpenAI-protocol lock-in and a CDN script tag shipped without SRI.

This ADR records how Olympus absorbs the *capabilities* natively — in its own
idioms and safety spine — and turns each of those weaknesses into a structural
strength, rather than porting a foreign subsystem or its anti-patterns. It
follows the shared contract of ADR 0008/0010/0011: opt-in where it touches the
network or actuation, replay-safe, security-spine reuse, own tests, and an
explicit "NOT absorbed" list. Page Agent's own DOM-processing lineage is
browser-use, whose perception ideas Olympus's CDP harness (`olympus/browser.py`,
ADR — `docs/BROWSER_HARNESS.md`) already implements more completely (open shadow
roots + **cross-origin** OOPIF traversal, self-healing selectors, provenance-
scored skills). So the absorption is deliberately *narrow*: take the handful of
genuinely additive ergonomics/robustness patterns, and invert every weakness.

## The core inversion

Page Agent's reach comes from **removing every trust boundary** (it runs *as the
page*). Olympus keeps the same in-page DOM-automation surface but drives it from
a **governed process outside the victim origin** (CDP), so every actuation
already passes through `security.filter_tools` (capability separation), the
SSRF/egress guards, and the `actions.py` approval spine. The absorption program
adds page-agent's *good* parts to that spine — it never relaxes the spine to
match page-agent's frictionlessness. The four documented non-goals below are the
line no decision in this ADR crosses.

## Decision (a): resilient, refusal-safe tool-call repair — SHIPPED

Page Agent's `autoFixer.normalizeResponse` salvages malformed tool calls from
weak models (double-encoded arguments, ```json-fenced or prose-wrapped objects,
and — the important one — a tool call emitted as JSON in `content` with an empty
`tool_calls` array). Olympus's own `openai_compat` (Ollama/vLLM/LM Studio/
Mistral/DeepSeek/OpenRouter/Gemini) and `bedrock_converse` (Titan/Nova/Llama/
Mistral/Cohere) drive exactly those weak models and hit exactly those bugs.

New pure module `olympus/toolcall_repair.py`:

- `extract_json_object` — brace-balanced, string/escape-aware recovery of a
  single JSON object from a model string; tolerates fences and trailing prose and
  stops at the first complete object (strictly stronger than a
  `find('{')..rfind('}')` slice). It now backs `openai_compat.extract_json` and
  `bedrock_converse.complete_json` too, so structured-output and tool-call
  recovery share one hardened scanner (dead ad-hoc regexes removed).
- `repair_arguments` — coerces a tool call's `arguments` into a dict across the
  malformed shapes; unrecoverable input degrades to `{}` so the caller's handler
  errors on missing params exactly as before (no new failure mode — repair can
  only *add* a successful recovery, never remove a working one).
- `recover_tool_call(content, known_names)` — reconstructs an OpenAI-shaped tool
  call from `content` when `tool_calls` is empty.

**The inversion (refusal-safety).** Page Agent's fixer will reconstruct a tool
call from *any* content JSON, which can mask a model's refusal ("I can't do
that") or turn a legitimate final answer into a phantom action. Olympus's
`recover_tool_call` fires **only when the content names a tool the model was
actually offered** (`known_names`, passed from the live tool defs). A refusal or a
plain answer names no real tool, so it is returned untouched as text — never
laundered into an action. This is enforced structurally, not by prompt.

Wired into `openai_compat.run_agent` (recover before treating an empty
`tool_calls` as the final answer; repair every call's arguments). The module is
pure (no I/O, no logging), adds nothing to the three-dependency footprint, and is
covered by `tests/test_toolcall_repair.py` (27 cases incl. two `run_agent`
integration tests: a content-emitted call executes; a refusal never does).

## Decision (b): perception deltas + scroll geometry in `observe()` — SHIPPED

Page Agent enriches its element map with two model-useful signals Olympus's
`observe()` lacked: a `*[` marker on elements that are *new since the last step*
(the strongest signal that an input revealed a suggestion list / dialog / next
step), and scroll affordances (pages above/below + `data-scrollable` containers
with remaining distance) so the model scrolls the *right* region. Both are now
native, deterministic annotations on `browser.observe()`:

- **Perception delta.** `BrowserSession` remembers the durable selectors from the
  previous `observe()` and the URL they were seen on. The next `observe()` on the
  *same* URL prefixes any newly-appeared element with `*` (page-agent's idiom). A
  navigation (URL change) or the first look marks nothing — a new page replaces
  the whole set, so "new" would be noise. Keyed on the durable `__olySel`
  selector, not the ephemeral index, so it survives re-indexing.
- **Scroll geometry.** A one-line header (`Page WxH, viewport WxH at N%
  (…px above, …px below)`, or `fits in viewport`) from a single measurement eval
  (`_GEOMETRY_JS`), plus a bounded list of scrollable containers each as a durable
  selector + remaining down/right px (`_SCROLLABLES_JS`, ≤ `_SCROLLABLE_MAX`) so
  the model can `act(scroll, selector=…)` a specific pane.

**Safety / no-regression.** Both scroll-affordance evals are pure measurement (no
page text — not an ingestion surface) and **fail soft**: any read error omits the
header/footer, so perception never degrades below the bare map. When the
transport can't report them, `observe()` returns exactly the previous output
(verified — the whole existing browser suite passes unchanged). The delta and
geometry live only on the model-facing `observe()`, not on `_observe_raw` (which
also backs self-healing), so healing is unaffected; `observe_frame` (the governed
cross-origin path) stays a plain map by design. Covered by six new cases in
`tests/test_browser.py`.

## Decision (c): human-fidelity click + landing hit-test — PLANNED

Audit `browser.act('click')` against page-agent's full spec-ordered pointer/mouse
sequence and its `elementFromPoint` deepest-target hit-test, so clicks survive
invisible overlays. (Watchlist §3.4.)

## Decision (d): default-on pre-prompt redaction seam — PLANNED

Invert page-agent's biggest risk (unredacted page HTML → LLM, opt-in hook only):
a **default-on** `security.sanitize_for_prompt` applied in the browser/webctx
observe path, symmetric to the existing `sanitize_for_memory`. What page-agent
leaves to the user, Olympus enforces in code. (Watchlist §3.5.)

## Decision (e): governed `/llms.txt` consumption — PLANNED

Olympus already *produces* `llms.txt` (`generate_llmstxt`); add governed
*consumption* as page context via `webctx`/`domainlore`, egress-gated,
SSRF-checked, truncated, cached. (Watchlist §3.6.)

## NOT absorbed (deliberate non-goals)

These are structural to page-agent's frictionlessness and incompatible with the
Olympus spine; they stay declined (mirrors `DEFERRED.md`):

1. **Running the agent *as the page* (zero-permission origin sharing).** Olympus
   drives Chrome over CDP from a governed process outside the victim origin;
   adopting the in-page model would discard capability separation and the
   SSRF/egress boundary. Reach ≠ trust (`docs/VISION.md`: "no 240 MB browser
   download" *and* the governance stance).
2. **`eval()` of model-authored code in a live origin.** Incompatible with the
   sandbox / `cmdguard` posture; Olympus code-exec is confined and `--network
   none` by default.
3. **Unauthenticated localhost control socket + reusable dialog approval.**
   Inbound control stays bearer-auth'd and per-task governed (the A2A / MCP-server
   pattern, ADR — `a2a_server.py`, `mcp_server.py`).
4. **Sending unredacted page HTML to the model behind only an opt-in hook.**
   Redaction must be a default-on, code-enforced chokepoint (Decision (d)), not a
   user responsibility.

## Consequences

Weak-model tool-calling on the OpenAI-compat and Bedrock-Converse backends is now
materially more robust, and the robustness is refusal-safe by construction —
Olympus gains page-agent's most useful contribution without its ability to
launder a refusal into an action. Remaining decisions (b)–(e) land per iteration,
each opt-in/replay-safe/spine-reusing with its own tests; the four non-goals are
the fixed boundary.
