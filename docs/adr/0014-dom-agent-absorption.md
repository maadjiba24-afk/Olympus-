# ADR 0014: Absorb the surveyed DOM agent's capabilities natively (in-page GUI-agent surface)

Status: accepted (complete — all five decisions shipped)
Date: 2026-07-23

## Context

A full inventory and security review of [alibaba/the surveyed DOM agent](https://github.com/alibaba/the surveyed DOM agent)
(a zero-install, in-page GUI agent — one `<script>` gives any web page its own
DOM-driving LLM agent) found a clean
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
explicit "NOT absorbed" list. The surveyed DOM agent's own DOM-processing lineage is
browser-use, whose perception ideas Olympus's CDP harness (`olympus/browser.py`,
ADR — `docs/BROWSER_HARNESS.md`) already implements more completely (open shadow
roots + **cross-origin** OOPIF traversal, self-healing selectors, provenance-
scored skills). So the absorption is deliberately *narrow*: take the handful of
genuinely additive ergonomics/robustness patterns, and invert every weakness.

## The core inversion

The surveyed DOM agent's reach comes from **removing every trust boundary** (it runs *as the
page*). Olympus keeps the same in-page DOM-automation surface but drives it from
a **governed process outside the victim origin** (CDP), so every actuation
already passes through `security.filter_tools` (capability separation), the
SSRF/egress guards, and the `actions.py` approval spine. The absorption program
adds the surveyed DOM agent's *good* parts to that spine — it never relaxes the spine to
match the surveyed DOM agent's frictionlessness. The four documented non-goals below are the
line no decision in this ADR crosses.

## Decision (a): resilient, refusal-safe tool-call repair — SHIPPED

The surveyed DOM agent's `autoFixer.normalizeResponse` salvages malformed tool calls from
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

**The inversion (refusal-safety).** the surveyed DOM agent's fixer will reconstruct a tool
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

The surveyed DOM agent enriches its element map with two model-useful signals Olympus's
`observe()` lacked: a `*[` marker on elements that are *new since the last step*
(the strongest signal that an input revealed a suggestion list / dialog / next
step), and scroll affordances (pages above/below + `data-scrollable` containers
with remaining distance) so the model scrolls the *right* region. Both are now
native, deterministic annotations on `browser.observe()`:

- **Perception delta.** `BrowserSession` remembers the durable selectors from the
  previous `observe()` and the URL they were seen on. The next `observe()` on the
  *same* URL prefixes any newly-appeared element with `*` (the surveyed DOM agent's idiom). A
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

## Decision (c): human-fidelity click + landing hit-test as a GUARD — SHIPPED

The surveyed DOM agent scrolls the target to view, computes its center, and dispatches a
full spec-ordered pointer/mouse sequence — then uses `elementFromPoint` to
retarget to whatever element is actually on top (matching real browser event
targeting). The hit-test is the valuable idea; the *silent retarget* is a risk:
an overlay, cookie banner, or interstitial covering the button means the surveyed DOM agent
clicks the overlay.

`browser.act('click', selector=…)` previously fired a bare `e.click()` (untrusted,
single event, no scroll, no hit-test). It now:

1. **Probes** (`_click_probe_js`): resolves the element, scrolls it to center,
   computes the click point, and runs `elementFromPoint` to decide whether that
   point is **obscured** — i.e. the topmost element there is neither the target,
   an ancestor, nor a descendant of it (a foreign overlay), or the point isn't
   hittable at all.
2. **Clear point → a TRUSTED, coordinate-accurate CDP click** (`Input.dispatch
   MouseEvent` mouseMoved→Pressed→Released at the probed point). This is
   *stronger* than the surveyed DOM agent's untrusted in-page `dispatchEvent` (`isTrusted`
   true → drives handlers that ignore synthetic events), and it lands exactly
   where the hit-test verified.
3. **Obscured / off-view → the inversion.** Olympus does **not** fire a blind
   coordinate click (which would hit the overlay). It dispatches a faithful
   pointer→mouse→click sequence straight to the *intended* element
   (`_dispatch_click_js`) and returns a note that the point was obscured so the
   operator/model can dismiss the modal first. Where the surveyed DOM agent clicks whatever
   is on top, Olympus actuates the observed control and surfaces the obstruction.

So Olympus takes the surveyed DOM agent's hit-test and makes it a **safety guard** rather than
a hijack surface, while upgrading the happy path from untrusted to trusted input.
FakeTransport gains a scriptable landing probe (`__OLY_CLICK__`) and a
`click_obstructed` switch; four new tests in `tests/test_browser.py` cover the
trusted-coordinate path, the obscured fallback (no blind click fired), journaling,
and the missing-element error.

## Decision (d): default-on pre-prompt secret redaction — SHIPPED

The surveyed DOM agent's biggest risk: it streams cleaned page HTML to the LLM every step and
its own privacy doc admits the cleaning "does not guarantee removal of sensitive
information" — redaction is left to an opt-in `transformPageContent` regex hook
that most integrators never set. The inversion is the moat: what the surveyed DOM agent
leaves to the user, Olympus enforces in code, on by default.

New `security.sanitize_for_prompt(text, *, redact_pii=None)`:

- **Secrets — always redacted.** Private-key PEM blocks (the *whole* block, key
  material and all — a new `_PEM_BLOCK_RE`, stronger than the header-only regex
  the memory path uses), JWTs, API-key-shaped tokens, and credentials embedded in
  URLs. Idempotent and structure-preserving (labeled placeholders, never
  deletion), so surrounding text stays task-usable.
- **PII — gated.** Emails / phone numbers / long id numbers redact only under
  `OLYMPUS_REDACT_PII`, because redacting them unconditionally would break
  legitimate "read the contact details" tasks. A privacy-strict deployment flips
  one flag; the dangerous class (secrets) needs no flag.

**The chokepoint.** `wrap_untrusted` — the fail-closed envelope every piece of
untrusted content passes through before a model prompt — now runs
`sanitize_for_prompt` on the body. Because `should_wrap` is fail-closed (even an
*unregistered* ingesting tool still wraps), redaction is fail-closed too: a new
ingesting tool nobody classified is still both wrapped and secret-redacted. This
single seam covers browser reads, `webctx` scrape/crawl/extract, `web_fetch`,
and every connector data plugin at once. As defense-in-depth, `browser.read` /
`html` / `read_ax` / `console_logs` also redact at the source, so the raw method
never emits a secret even outside the wrap path. 14 new tests
(`tests/test_prompt_redaction.py`); the whole wrap-dependent suite (270 cases)
passes unchanged.

## Decision (e): governed `/llms.txt` consumption — SHIPPED

The surveyed DOM agent's `experimentalLlmsTxt` fetches `{origin}/llms.txt` — a site's
author-curated guidance for agents — with a raw `fetch()` (no SSRF check, no
egress confinement, no secret scan) and folds it into the prompt unwrapped.
Olympus already *produces* `llms.txt` (`generate_llmstxt`); this adds the
*consumption* side, governed:

- New `webctx.fetch_llmstxt(url)` fetches `{origin}/llms.txt` through the same
  SSRF/egress-gated, DNS-rebinding-pinned `tools._http_get` every other web fetch
  uses, port-allowlisted, body-capped (≤8k), and cached per origin (one
  round-trip per site per process). A missing file degrades to a bounded "not
  found" — never raises.
- New INGESTION tool `web_llms_txt` (handler + schema + `INGESTION_TOOLS`
  classification + THREAT_MODEL.md row + capabilities manifest). Because it is
  ingestion-classified, its output is **wrapped untrusted AND secret-redacted**
  (decision (d)) before any model sees it — so author-controlled llms.txt text
  can't inject or smuggle a secret, and actuators are stripped from any run that
  reads it.

Every layer that the surveyed DOM agent skips — SSRF/egress gate, rebinding pin, port
allowlist, untrusted envelope, secret redaction, threat-model binding — is
present here. Seven new tests (`tests/test_webctx.py`).

## Decision (f): hardening pass over the absorbed capabilities — SHIPPED

An adversarial self-review of decisions (a)–(e) surfaced five weak spots, each
now closed:

- **(H1) Broader secret detection.** `sanitize_for_prompt` gained a
  low-false-positive `_EXTRA_SECRET_RES` set — Google API keys (`AIza…`) and
  OAuth (`ya29.…`), GitHub app tokens (`ghu_/ghs_/ghr_`) and fine-grained PATs
  (`github_pat_…`), Stripe live/test keys, hyphenated Slack tokens, and
  `Authorization: Bearer …` headers — beyond the original `sk-/pk-/ghp/xox/AKIA`
  shapes. Deliberately *no* blind high-entropy matching (it would redact a hash
  or id the user asked about); only distinctive provider shapes.
- **(H2) The observe() unwrapped-actuator gap.** `browser_observe` is an ACTION
  tool and is *not* run through `wrap_untrusted`, so a secret sitting in an
  element label could reach the model unredacted. `observe()` now
  `sanitize_for_prompt`s each label at the source (a token is never a useful
  control identifier), closing the one path decision (d) didn't cover.
- **(H3) observe() round-trips + a measurement race.** Geometry and scrollables
  were two separate evals that could disagree if the page scrolled between them;
  they're now one `_PERCEPTION_JS` eval measured atomically. The delta baseline
  reuses the URL `_blocked_landing()` already resolved (`self._landed_url`)
  instead of a second `_current_url()` — one fewer CDP round-trip and no TOCTOU.
  The scrollable DOM walk is bounded (`_SCROLL_WALK_MAX`) so a huge page can't
  make perception O(page).
- **(H4) llms.txt cache.** `fetch_llmstxt`'s per-origin cache gained a TTL (a 404
  no longer sticks for the whole process — a site that later publishes an
  llms.txt is picked up) and an entry bound with oldest-eviction (can't grow
  without limit).
- **(H5) Adversarial tests** for each: broadened-secret shapes + a zero-width
  split-token evasion, secret-in-label, single-atomic-perception-eval, and cache
  expiry/bound.

## Status: program complete + hardened

All five borrowable watchlist items (§3.1, §3.2/§3.3, §3.4, §3.5, §3.6) are now
native Olympus capabilities, each on the security spine with the surveyed DOM agent's
corresponding weakness inverted, and each hardened per decision (f). The four
non-goals below remain the fixed boundary. Nothing further from the surveyed DOM agent is
planned for absorption.

## NOT absorbed (deliberate non-goals)

These are structural to the surveyed DOM agent's frictionlessness and incompatible with the
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
Olympus gains the surveyed DOM agent's most useful contribution without its ability to
launder a refusal into an action. Remaining decisions (b)–(e) land per iteration,
each opt-in/replay-safe/spine-reusing with its own tests; the four non-goals are
the fixed boundary.
