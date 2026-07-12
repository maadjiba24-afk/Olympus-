# Threat model

Olympus exposes a **finite, named** tool surface — the 87 tools in
`tools.HANDLERS` — not a sprawl of hundreds of auto-registered tools. That makes
a real threat model tractable: every tool is listed below with its capability,
trust boundary, deny-first default, and the abuse case it's designed against.

This document is **enforced**: `scripts/check_threat_model.py` (run in CI) fails
if any exposed tool is missing here, if this file documents a tool that no
longer exists, or if the count in this sentence drifts from the live tool
surface. So the surface and its threat model can't drift apart.

## Principles

- **Deny-first.** Sensitive or irreversible actions are *prepared*, never
  performed, until a human approves them. Olympus may prepare actions by itself,
  but it must not perform sensitive or irreversible actions without explicit
  user approval.
- **Scoped actuators.** Anything that touches the outside world (email,
  webhooks) requires an explicitly granted permission scope and is refused,
  failing closed, without it (`actions._execute` → `blocked_no_scope`). It is
  also daily-rate-limited and never auto-executes.
- **Capability separation.** A specialist that ingests untrusted content (e.g.
  Angelos reading email) is **not** given direct actuators — only
  `prepare_action`, which is human-gated. Untrusted input can request an action,
  never perform one.
- **Untrusted input is wrapped.** Tool output that originates outside the system
  (web pages, emails, transcripts) is treated as untrusted and wrapped/sanitized
  (`security.should_wrap`, `security.sanitize_for_memory`) so injection-shaped
  text can't poison memory or hijack the agent.

## Trust boundaries

| Boundary | Meaning |
| --- | --- |
| pure | No input, no side effects. |
| first-party read | Reads Olympus's own state (memory, skills, source). |
| ingests untrusted | Brings external, attacker-influencable content into context. |
| self-modifying | Changes Olympus's own prompts/skills (gated + reversible). |
| external actuator | Acts on the outside world (irreversible/scoped). |

## The tool surface

| Tool | Capability | Trust boundary | Deny-first default | Abuse case defended |
| --- | --- | --- | --- | --- |
| `current_time` | Return the local time | pure | n/a | None meaningful; excluded from replay as nondeterministic |
| `recall_memory` | Search durable memory | first-party read | Read-only | Poisoned recall — memory is sanitized at write time |
| `recall_fact` | Look up a cached fact | first-party read | Read-only | Cache poisoning — facts sanitized at write time |
| `read_skill` | Read a skill from the library | first-party read | Read-only | None significant (own content) |
| `list_source_files` | List Olympus's own source files | first-party read | Read-only | Recon — limited to Olympus's own (public, MIT) source |
| `read_source_file` | Read one of Olympus's source files | first-party read | Read-only | Exfil of own source — source is public anyway |
| `query_codegraph` | Look up symbols in the code graph of Olympus's own source | first-party read | Read-only; trusted local code | Recon — limited to Olympus's own (public, MIT) source |
| `codegraph_neighbors` | Show a symbol's callers/callees/imports | first-party read | Read-only | Recon of own public source structure |
| `codegraph_impact` | Reverse-dependency closure for a symbol | first-party read | Read-only | Recon of own public source structure |
| `codegraph_path` | Shortest dependency path between two symbols | first-party read | Read-only | Recon of own public source structure |
| `verify_code_claim` | Check a structural claim against EXTRACTED graph edges | first-party read | Read-only; ground-truth (EXTRACTED) only, INFERRED never authoritative | None significant — verifies, never asserts on a guess |
| `cache_fact` | Store a fact in memory | first-party write | Sanitized at write | Cache poisoning via injection-shaped text |
| `save_lesson` | Store a lesson in memory | first-party write | Sanitized at write | Memory poisoning — `sanitize_for_memory` strips injections |
| `web_search` | Web search (server-side on Anthropic; client-side DuckDuckGo elsewhere) | ingests untrusted | Output treated as untrusted | Prompt injection from web results — wrapped, not trusted |
| `web_fetch` | Fetch a URL's contents | ingests untrusted | Output treated as untrusted | SSRF / injected page content — wrapped, not trusted |
| `watch_youtube` | Fetch a video transcript | ingests untrusted | Output treated as untrusted | Malicious transcript injection — wrapped |
| `read_inbox` | List inbox messages | ingests untrusted | Read-only; wrapped | Email-borne prompt injection — `should_wrap` wraps it |
| `triage_inbox` | Classify inbox messages into important/promotions/spam/other | ingests untrusted | Read-only — classifies, never deletes/moves; heuristic (no model call required); wrapped | Email-borne prompt injection — `should_wrap` wraps it; classification is derived data, not instructions |
| `read_email` | Read one email | ingests untrusted | Read-only; wrapped | Email-borne prompt injection — wrapped |
| `read_calendar` | Read calendar events | ingests untrusted | Read-only; wrapped | Event-text injection — wrapped |
| `read_file` | Read a file from the confined workspace | first-party read | Read-only; path-confined to the workspace root | Path traversal — `_confine` refuses paths escaping the root |
| `list_dir` | List a workspace directory | first-party read | Read-only; path-confined | Recon outside the workspace — confined to the root |
| `browse_page` | Fetch a page as text + extract links | ingests untrusted | SSRF/egress gate on the URL and every redirect (`_http_get`); output wrapped | SSRF (incl. redirect-to-internal) — refused by `url_block_reason`; injected content — wrapped |
| `analyze_image` | Describe / answer about an image (URL or workspace file) via a vision model | ingests untrusted | Output treated as untrusted and wrapped; workspace files path-confined via `_confine`; size-capped; needs a media API key | Injected instructions inside an image (text-in-image) or a hostile URL — result is enveloped by `should_wrap`; SSRF limited to the provider's own fetch |
| `browser_open` | Navigate the attached browser to a URL | ingests untrusted | SSRF + egress allowlist gate (`url_block_reason`); output wrapped | Internal-host/metadata reach + injected page — gated and wrapped |
| `browser_read` | Read text from the current browser page | ingests untrusted | Output treated as untrusted; wrapped | Injected page content steering the agent — wrapped, not trusted |
| `browser_screenshot` | Capture the current page and describe it via a vision model | ingests untrusted | Refuses a blocked landing; decoded-size capped; output treated as untrusted and wrapped; a READER (never an actuator), capability-separated | Text-in-image injection or reconnaissance via pixels — the description is enveloped by `should_wrap`, and the tool is stripped from any run that also holds an actuator |
| `browser_observe` | Map the current (possibly logged-in) page's interactive elements by index | external actuator | **Credentialed perception** — returns bounded, label-capped structure (not prose); stripped from any ingesting run (capability separation) AND gated: operator enabled and the current domain authorized | Injection reconnaissance of your authenticated tabs — unreachable from an ingesting run, refused on an unauthorized domain, labels hard-capped so a map row can't carry a paragraph of instructions |
| `browser_act` | Click/type/scroll/press (chords)/select/hover/rightclick/drag on the current (possibly logged-in) page, by index or selector | external actuator | **Credentialed action** — stripped from any ingesting run (capability separation) AND gated: operator must be enabled and the current page's domain authorized | Injection-driven action on your authenticated tabs — actuator unreachable from an ingesting run and refused on an unauthorized domain |
| `browser_tabs` | List the browser's open page tabs (bounded id/title/url) | external actuator | Reveals the credentialed browser's tabs — operator-gated and stripped from any ingesting run; titles/urls length-capped | Reconnaissance of your logged-in sessions from an injected run — unreachable from an ingesting run and needs the operator enabled |
| `browser_switch_tab` | Switch the active browser tab by index | external actuator | Operator-gated; after switching, `browser_act`/`browser_observe` re-check the newly-current domain's authorization | Redirecting the actuator onto an unauthorized logged-in tab — the post-switch domain re-check refuses it |
| `browser_upload` | Attach a workspace file to a file input on the current page | external actuator | **Data egress** — operator-gated (current domain authorized) AND the path is confined to the workspace (`_confine`) | Exfiltrating a local file to an unauthorized site — refused off-domain and unable to escape the workspace |
| `browser_save_auth` | Save an authorized domain's session cookies into the encrypted vault | external actuator | **Credentials** — operator-gated + domain-authorized; cookies stored only in the Fernet-encrypted vault, filtered to the domain, never surfaced to the model; stripped from any ingesting run | Harvesting session tokens via an injected run — unreachable from an ingesting run, refused off-domain, and only the authorized domain's cookies are touched |
| `browser_restore_auth` | Restore a saved session by injecting a domain's vault-stored cookies | external actuator | **Credentials** — operator-gated + domain-authorized; reads only the vault; stripped from any ingesting run | Planting a forged session from an injected run — unreachable from an ingesting run and refused off-domain |
| `browser_learn` | Crystallize the current session's proven observe→act flow into a reliability-scored skill | first-party write | Records only Olympus's own landed steps (never the typed text, so credentials never enter the store); sanitized at write; gated to the authorized session | Credential leak or skill poisoning — typed text is excluded by construction and steps pass `sanitize_for_memory`; learned skills ride the same measured-reliability scoring/pruning as any other |
| `browser_skill_record` | Save a browser skill with provenance + score | first-party write | Steps sanitized at write; provenance + content hash recorded | Skill poisoning via injection-shaped steps — `sanitize_for_memory` |
| `browser_skills` | List browser skills ranked by reliability | first-party read | Read-only; own recorded skills | None significant — own content, scored not trusted blindly |
| `browser_pattern` | Suggest a generalized scaffold for a goal from the most reliable learned flow on another site | first-party read | Read-only; own skills; returns op sequence + intent hints with selectors omitted (a site's selectors are never presented as applying elsewhere) | None significant — own content; no cross-site selector is asserted, only a shape to adapt |
| `browser_exists` | Probe whether a CSS selector is present | structured predicate | Returns a bool, never page prose | Not an ingestion vector — no page text crosses into instructions |
| `browser_login` | Log in via a site profile + vaulted credentials | credentialed actuator | Off by default (`OLYMPUS_OPERATOR`); domain must be allowlisted *and* on the egress allowlist; vault entry required; password never enters model context; fails closed on a missing success marker (2FA/CAPTCHA) | Unauthorized login / credential leak — deny-first gates + vault isolation; injected page can't redirect it (declarative profile only) |
| `site_profile_record` | Save a domain's declarative login recipe | first-party write | Selectors only (no credentials); length-capped; provenance + reliability tracked | Profile poisoning — no secrets stored here; credentials live in the vault |
| `site_profiles` | List saved site profiles by reliability | first-party read | Read-only; own profiles | None significant — own content |
| `browser_operate` | Run a declarative action template on an authorized site | credentialed actuator | Runs as an ActionType on the approval spine: `browser.operate` scope + autonomy gate; IRREVERSIBLE templates always need explicit approval; daily runaway cap; full audit; off unless `OLYMPUS_OPERATOR` + domain authorized | Unauthorized/runaway credentialed action — deny-first spine + per-domain authorization + templates only (no "do what the page says") |
| `site_template_record` | Define a declarative action template | first-party write | Steps only (selectors); risk label required; length-capped | Template poisoning — declarative steps only, executed under the spine with its risk gate |
| `operator_schedule` | Schedule a recurring operator job | first-party (gated) | Stored only; runs later through the spine (approval/scope/budget still apply); off unless operator enabled + domain authorized | Unattended runaway — every run re-gated by the spine; irreversible templates still wait for approval |
| `operator_review` | Prune drifted/flaky site profiles | first-party write | Operates on own profiles; removes consistently-failing ones | None significant — own content; conservative thresholds |
| `propose_site_profile` | File a site-profile patch proposal | first-party (proposal) | Proposal only — never applies; a human enacts it | Self-authorizing credentials recipe — proposals never auto-apply |
| `operator_authorize_site` | Authorize the operator for a site (per user) | first-party (gated) | Records the user's explicit opt-in in their prefs; still bounded by the egress allowlist; defaults to manual sign-in (no password handling); credentialed actions still spine-gated | Self-granting authority — only on explicit user request; irreversible actions still need approval; reversible via `operator_forget_site` |
| `operator_forget_site` | De-authorize a site and delete its saved sign-in | first-party write | Removes the prefs entry and any vault credentials for the domain | None significant — strictly reduces capability |
| `operator_status` | Show which sites the operator is set up for | first-party read | Read-only; own settings; no secrets shown | None significant — own settings |
| `operator_history` | Report what the operator did on the user's accounts | first-party read | Read-only over the user's own action audit trail; no secrets shown | None significant — own audit data, per-user namespaced |
| `operator_remember_login` | Start saving a site sign-in for auto-login | first-party (gated) | Records a pending request only; the password is captured out-of-band by a private prompt and stored in the vault — it never passes through the model or this tool | Password exposure to the model — the secret never enters the model loop by construction |
| `set_advanced_mode` | Toggle plain-English vs. engineer surface | first-party write | Per-user UI preference; no capability change | None — purely a presentation setting |
| `recent_learning` | Summarize the autonomous loop's recent activity | first-party read | Read-only over heartbeat state + own memory; no model calls | None significant — own content, no secrets |
| `search_sessions` | Full-text search past conversations | first-party read | Read-only; this user's own history | None significant — own content; per-user namespaced |
| `spawn_subagent` | Delegate a sub-task to another specialist | first-party (orchestration) | Runs a known specialist; gated by that specialist's own loadout | Delegation loop / cost — isolated per branch, budget-guarded |
| `schedule_task` | Schedule a recurring unattended task | first-party (gated) | Runs later through the full pipeline on the server's own key | Cost/abuse via runaway schedules — min interval + budget guard |
| `generate_image` | Generate an image into the workspace | external actuator | Writes only to the confined workspace; needs a media API key | Cost burn / disallowed content — key-gated, confined output |
| `edit_image` | AI-edit an existing workspace image, saving the result as a NEW file | external actuator | Reads the source path-confined via `_confine` (image types only, size-capped) and writes only to the confined workspace; the source is never overwritten; needs a media API key | Cost burn / disallowed content — key-gated, confined I/O, non-destructive (original preserved) |
| `text_to_speech` | Synthesize audio into the workspace | external actuator | Writes only to the confined workspace; needs a media API key | Cost burn — key-gated, confined output |
| `transcribe_audio` | Transcribe a workspace audio file to text | ingestion | Reads only from the confined workspace; key-gated; transcript is wrapped as untrusted content | Injection via spoken/recorded content — transcript is data, capability separation applies |
| `prepare_action` | Stage an action for approval | first-party (gated) | **Never executes** — human approval required | Self-authorizing actions — execution is human-gated |
| `propose_playbook` | Propose a repeatable workflow | first-party (gated) | Proposal only — approval required | Malicious playbook — approval-gated before it can run |
| `create_skill` | Add a provisional skill | self-modifying | **Provisional** until benchmark-gated; sanitized | Malicious/weak skill — gated and reverted if it doesn't help |
| `gate_skills` | Benchmark-gate provisional skills | self-modifying | Internal evaluation | Promoting a weak skill — only measured wins survive |
| `generate_benchmark` | Generate a specialist benchmark | first-party write | Internal | Weak benchmark — bounded by the gate it feeds |
| `update_prompt` | Edit an agent prompt (raw) | self-modifying | Auto-backup of the prior version; caller must measure (see `gate_prompt`) | Prompt sabotage — recoverable via the backup; measure/rollback is the caller's duty |
| `gate_prompt` | Benchmark-gated prompt upgrade | self-modifying | Before/after benchmark; applied only on non-regression, else auto-rolled-back | Prompt sabotage — a regressing change is reverted automatically by code |
| `restore_prompt` | Restore a backed-up prompt | self-modifying | Reverts to a prior backup | Reverting to a weaker prompt — bounded to saved versions |
| `propose_upgrade` | File an upgrade proposal | first-party write | Proposal only | Proposal spam — bounded, human-reviewed |
| `run_benchmark` | Run the quality benchmark | first-party (cost) | Internal; budget-guarded | Token/cost burn — daily budget guard |
| `run_code_benchmark` | Execute code benchmarks | self-modifying (exec) | Runs model-written code as a local subprocess (temp dir + wall-clock timeout) — NOT an OS sandbox on the default backend | Arbitrary code execution — bound the benchmark inputs; use `OLYMPUS_EXEC_BACKEND=docker` for OS isolation |
| `send_email` | Send an email | external actuator | **Irreversible**: requires `gmail.send`/`email` scope; never auto; rate-limited | Spam / data exfil — scope-gated, human-approved, capped |
| `call_webhook` | POST to an external URL | external actuator | Requires scope; never auto; rate-limited | SSRF / exfil — scope-gated, human-approved, capped |
| `grep_files` | Regex-search file contents in the workspace | first-party read | Read-only; path-confined via `_confine`; sensitive-file deny-list; binary/size bounded | Credential harvest / recon — deny-list skips `.env`/keys/`.ssh`, confined to the root |
| `glob_files` | List workspace files matching a glob | first-party read | Read-only; path-confined; sensitive-file deny-list; result-capped | Recon outside the workspace / secret discovery — confined and deny-listed |
| `edit_file` | Exact-string edit of a workspace file | first-party (gated) | **Always prepares for explicit approval — never auto-executes**, even at autonomy L4 (it stages via the always-hold `prepare_action` path, not `auto_or_hold`), because it lives on the always-ingesting Hephaestus; preview is the unified diff (truncation is announced); sensitive-file deny-list; path-confined; `undo` restores prior contents | Injection-driven code change in a web-reading run — the edit only stages and a human approves the full diff before it lands; deny-listed; reversible |
| `ask_user` | Ask the user a focused multiple-choice question | first-party (interaction) | Neither ingests nor acts; headless runs never block (proceed-with-assumption); the answer is wrapped untrusted | None significant — no external content, no world effect; the returned answer is treated as data |
| `refresh_email_style` | Rebuild the user's email writing-style profile from sent mail | first-party (learning) | Distills style (not content) from the user's own sent mail; bodies wrapped untrusted while profiling; the stored guide is scrubbed for injection markers | Injection via a hostile sent message steering the profiler — content is wrapped and the guide is sanitized before caching |
| `list_documents` | List the user's workspace documents | first-party read | Read-only; per-user namespaced | None significant — the user's own content |
| `read_document` | Read one workspace document | first-party read | Read-only; per-user namespaced | None significant — the user's own content |
| `search_documents` | Retrieve relevant passages from the user's own documents | first-party read | Read-only; per-user namespaced; first-party content (like memory, not wrapped) | None significant — the user's own documents; retrieval only |
| `write_document` | Create/overwrite a workspace document | first-party (gated) | **Always staged for approval** (via prepare_action, never auto-executes) and reversible (`undo`); confined to the user's own document dir; size-capped | Silent/unwanted document writes — human approves every save; reversible; per-user isolated |
| `list_todos` | List the user's notes/todos/reminders | first-party read | Read-only; per-user namespaced | None significant — the user's own list |
| `add_todo` | Add a note/todo/reminder to the user's list | first-party (ungated) | Writes only to the user's own todos file; per-user namespaced; item/text-count capped; no external side effect | Unwanted list entries — the user's own data, trivially deletable; no actuator, nothing leaves the machine |
| `complete_todo` | Mark one of the user's todos done | first-party (ungated) | Per-user namespaced; toggles a boolean on the user's own item | None significant — the user's own list |
| `trigger_research` | Run a multi-round Deep Research pass and return a cited report | ingests untrusted | Every fetched page is wrapped untrusted during extraction and fetched through the SSRF/rebinding-pinned path; the tool is an INGESTION tool, so it's in-scope for capability separation (any run that can invoke it loses action tools) and its report is re-wrapped; rounds are clamped (≤6) so an agent can't spin an unbounded loop | Injection via researched pages / SSRF / cost runaway — content wrapped, actuators stripped, fetch gated, rounds bounded |

## The action spine (execution layer)

Tools that *act* (`send_email`, `call_webhook`) and the higher-level action
types (`gmail_send`, `gmail_draft`, `gmail_archive`, `calendar_create`,
`save_note`, and the workspace-execution types `run_command` (irreversible),
`write_file` and `edit_file` (reversible — `undo` restores prior contents), …)
all run through
the Action spine, which enforces deny-first:

- irreversible actions **never** auto-execute (`can_auto_execute` is always
  false for them), regardless of autonomy level;
- scoped actions fail closed without the granted scope;
- every execution is daily-rate-limited and audited;
- reversible actions support `undo`.

This is proven in `tests/test_threat_model.py` (an ungranted action is blocked)
and `tests/test_gmail.py` (the full prepare → approve → execute / undo path).
