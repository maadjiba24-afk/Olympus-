# Threat model

Olympus exposes a **finite, named** tool surface — the 26 tools in
`tools.HANDLERS` — not a sprawl of hundreds of auto-registered tools. That makes
a real threat model tractable: every tool is listed below with its capability,
trust boundary, deny-first default, and the abuse case it's designed against.

This document is **enforced**: `scripts/check_threat_model.py` (run in CI) fails
if any exposed tool is missing here, or if this file documents a tool that no
longer exists. So the surface and its threat model can't drift apart.

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
| `web_search` | Server-side web search | ingests untrusted | Output treated as untrusted | Prompt injection from web results — wrapped, not trusted |
| `web_fetch` | Fetch a URL's contents | ingests untrusted | Output treated as untrusted | SSRF / injected page content — wrapped, not trusted |
| `watch_youtube` | Fetch a video transcript | ingests untrusted | Output treated as untrusted | Malicious transcript injection — wrapped |
| `read_inbox` | List inbox messages | ingests untrusted | Read-only; wrapped | Email-borne prompt injection — `should_wrap` wraps it |
| `read_email` | Read one email | ingests untrusted | Read-only; wrapped | Email-borne prompt injection — wrapped |
| `read_calendar` | Read calendar events | ingests untrusted | Read-only; wrapped | Event-text injection — wrapped |
| `prepare_action` | Stage an action for approval | first-party (gated) | **Never executes** — human approval required | Self-authorizing actions — execution is human-gated |
| `propose_playbook` | Propose a repeatable workflow | first-party (gated) | Proposal only — approval required | Malicious playbook — approval-gated before it can run |
| `create_skill` | Add a provisional skill | self-modifying | **Provisional** until benchmark-gated; sanitized | Malicious/weak skill — gated and reverted if it doesn't help |
| `gate_skills` | Benchmark-gate provisional skills | self-modifying | Internal evaluation | Promoting a weak skill — only measured wins survive |
| `generate_benchmark` | Generate a specialist benchmark | first-party write | Internal | Weak benchmark — bounded by the gate it feeds |
| `update_prompt` | Edit an agent prompt | self-modifying | Auto-backup + benchmark-gated + rollback | Prompt sabotage — reverted automatically if it regresses |
| `restore_prompt` | Restore a backed-up prompt | self-modifying | Reverts to a prior backup | Reverting to a weaker prompt — bounded to saved versions |
| `propose_upgrade` | File an upgrade proposal | first-party write | Proposal only | Proposal spam — bounded, human-reviewed |
| `run_benchmark` | Run the quality benchmark | first-party (cost) | Internal; budget-guarded | Token/cost burn — daily budget guard |
| `run_code_benchmark` | Execute code benchmarks | self-modifying (exec) | Server-side sandbox | Sandbox escape — code runs server-isolated |
| `send_email` | Send an email | external actuator | **Irreversible**: requires `gmail.send`/`email` scope; never auto; rate-limited | Spam / data exfil — scope-gated, human-approved, capped |
| `call_webhook` | POST to an external URL | external actuator | Requires scope; never auto; rate-limited | SSRF / exfil — scope-gated, human-approved, capped |

## The action spine (execution layer)

Tools that *act* (`send_email`, `call_webhook`) and the higher-level action
types (`gmail_send`, `gmail_draft`, `gmail_archive`, `calendar_create`,
`save_note`, …) all run through the Action spine, which enforces deny-first:

- irreversible actions **never** auto-execute (`can_auto_execute` is always
  false for them), regardless of autonomy level;
- scoped actions fail closed without the granted scope;
- every execution is daily-rate-limited and audited;
- reversible actions support `undo`.

This is proven in `tests/test_threat_model.py` (an ungranted action is blocked)
and `tests/test_gmail.py` (the full prepare → approve → execute / undo path).
