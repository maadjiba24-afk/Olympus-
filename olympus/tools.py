"""Client-side tool definitions and handlers shared by Olympus agents.

Server-side tools (web_search / web_fetch) run on Anthropic's infrastructure —
no MCP servers, no extra plumbing. Client-side tools below run locally.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable

from . import config, facts, github, memory, security, skills, youtube

# --- server-side (Anthropic-hosted; this is how Olympus surfs the internet) --

# Use the stable web_search version WITHOUT dynamic filtering. The _20260209
# version filters results via server-side code execution, which spins up a
# container the API then requires us to track across turns — a fragile
# dependency for a multi-turn agent loop. Plain search is all the scouts need.
WEB_TOOLS: list[dict[str, Any]] = [
    {"type": "web_search_20250305", "name": "web_search"},
]

# --- client-side web fallback (used on non-Anthropic providers) -------------

WEB_SEARCH_CLIENT = {
    "name": "web_search",
    "description": "Search the web. Returns titles, URLs and snippets of the "
                   "top results. Use web_fetch to read a promising result.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

WEB_FETCH_CLIENT = {
    "name": "web_fetch",
    "description": "Fetch a web page and return its readable text content.",
    "input_schema": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
}


def web_tool_defs(provider: str) -> list[dict[str, Any]]:
    """Server-side web tools on Anthropic; client-side fallback elsewhere."""
    if provider == "anthropic":
        return list(WEB_TOOLS)
    return [WEB_SEARCH_CLIENT, WEB_FETCH_CLIENT]

# --- client-side tool schemas -------------------------------------------

RECALL_MEMORY = {
    "name": "recall_memory",
    "description": (
        "Search Olympus's long-term memory (lessons learned, past corrections, "
        "opportunity reports, upgrade notes). Call this before answering when "
        "prior knowledge, user context, or past mistakes could be relevant."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to search for"}
        },
        "required": ["query"],
    },
}

RECALL_FACT = {
    "name": "recall_fact",
    "description": (
        "Check Olympus's verified-facts cache before doing fresh research. If "
        "a claim was already verified recently (with a source), reuse it "
        "instead of re-searching."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

CACHE_FACT = {
    "name": "cache_fact",
    "description": (
        "Store a fact you just verified so future fact-checks are faster and "
        "cheaper. Include the source and your verdict (true/false/nuance)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claim": {"type": "string"},
            "verdict": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["claim", "verdict"],
    },
}

SAVE_LESSON = {
    "name": "save_lesson",
    "description": (
        "Persist a lesson to long-term memory so every future Olympus session "
        "can use it. Call this whenever you learn something durable: an insight "
        "from a video, a confirmed fact, a user preference, a mistake to avoid."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short descriptive title"},
            "content": {
                "type": "string",
                "description": "The lesson, written so a future agent with no "
                "other context can apply it",
            },
        },
        "required": ["title", "content"],
    },
}

WATCH_YOUTUBE = {
    "name": "watch_youtube",
    "description": (
        "Watch a YouTube video by fetching its full spoken transcript. Returns "
        "the transcript text so you can understand and summarize the video."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "YouTube URL or 11-char video id"}
        },
        "required": ["url"],
    },
}

READ_SKILL = {
    "name": "read_skill",
    "description": (
        "Load a skill from Olympus's self-built skill library (the index is "
        "in your system prompt). Read the relevant skill BEFORE doing a task "
        "it covers — skills encode what Olympus has already learned works."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Skill name"}},
        "required": ["name"],
    },
}

CREATE_SKILL = {
    "name": "create_skill",
    "description": (
        "Create or update a skill in Olympus's library. A skill is a reusable "
        "how-to distilled from experience, written so any specialist can apply "
        "it without other context. Use the same name to improve an existing "
        "skill rather than creating near-duplicates. New skills are provisional "
        "until a benchmark proves they help — so tag the specialist they serve."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short imperative name, "
                     "e.g. 'Evaluate a SaaS niche' or 'Debug a flaky test'"},
            "description": {"type": "string",
                            "description": "One line: when to use this skill"},
            "instructions": {"type": "string",
                             "description": "The full method: steps, checks, "
                             "pitfalls, examples"},
            "specialist": {"type": "string",
                           "description": "Key of the specialist this skill "
                           "primarily serves (e.g. 'plutus'), used to gate it "
                           "by benchmark"},
        },
        "required": ["name", "description", "instructions"],
    },
}

GATE_SKILLS = {
    "name": "gate_skills",
    "description": (
        "Prove all provisional skills with a before/after benchmark: keep the "
        "ones that hold or raise the affected specialist's score, revert the "
        "rest. Run this after creating skills to make the change safe."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

GENERATE_BENCHMARK = {
    "name": "generate_benchmark",
    "description": (
        "Generate and save a new benchmark item for a specialist's domain, so "
        "future skill/prompt changes there can be measured. Use when a domain "
        "is thinly covered by the current benchmark."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"specialist": {"type": "string"}},
        "required": ["specialist"],
    },
}

SEND_EMAIL = {
    "name": "send_email",
    "description": (
        "Send an email via the configured SMTP account. Only allowlisted "
        "recipients are permitted. Use for reminders, reports, and messages "
        "the user explicitly asked to send."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient address"},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body"},
        },
        "required": ["to", "subject", "body"],
    },
}

CALL_WEBHOOK = {
    "name": "call_webhook",
    "description": (
        "POST a JSON payload to one of the operator-configured webhooks "
        "(OLYMPUS_WEBHOOKS). Use this to push content into external systems "
        "the user has wired up — e.g. a posting queue, Zapier/Make/n8n flow, "
        "or notification channel."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Configured webhook name"},
            "payload": {"type": "object", "description": "JSON body to send"},
        },
        "required": ["name", "payload"],
    },
}

RUN_BENCHMARK = {
    "name": "run_benchmark",
    "description": (
        "Run Olympus's quality benchmark: fixed tasks per specialist, scored "
        "1-10 by a strict judge against explicit criteria. Run it BEFORE and "
        "AFTER prompt changes — if the average drops, roll the change back "
        "with restore_prompt. Results are saved to memory/evals."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

RUN_CODE_BENCHMARK = {
    "name": "run_code_benchmark",
    "description": (
        "Run the execution-scored coding benchmark: Hephaestus solves real "
        "coding tasks and the code is RUN against tests, scored by whether it "
        "actually passes (objective, not a judge's opinion). Use this to "
        "measure coding ability before/after changing Hephaestus's prompt."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

RESTORE_PROMPT = {
    "name": "restore_prompt",
    "description": (
        "Roll back an agent's prompt to its most recent backed-up version. "
        "Use when a benchmark shows a prompt change made things worse."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"agent": {"type": "string",
                                 "description": "Prompt file stem, e.g. 'argus'"}},
        "required": ["agent"],
    },
}

CURRENT_TIME = {
    "name": "current_time",
    "description": "Get the current date and time (ISO format, local timezone).",
    "input_schema": {"type": "object", "properties": {}},
}

LIST_SOURCE_FILES = {
    "name": "list_source_files",
    "description": (
        "List Olympus's own source files (code and prompt files). Use this to "
        "audit the system and find what is missing inside it."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

READ_SOURCE_FILE = {
    "name": "read_source_file",
    "description": "Read one of Olympus's own source or prompt files (read-only).",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the project root, e.g. "
                "'olympus/orchestrator.py' or 'olympus/prompts/argus.md'",
            }
        },
        "required": ["path"],
    },
}

UPDATE_PROMPT = {
    "name": "update_prompt",
    "description": (
        "Rewrite the system prompt of an Olympus agent. This is the system's "
        "self-upgrade mechanism: improve a prompt with lessons learned, sharper "
        "instructions, or missing capabilities. The previous version is backed "
        "up automatically. Only use it when the new prompt is strictly better."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "Prompt file stem, e.g. 'argus' or 'zeus' "
                "(see list_source_files for olympus/prompts/*.md)",
            },
            "new_prompt": {"type": "string", "description": "Complete new prompt text"},
            "reason": {"type": "string", "description": "Why this is an improvement"},
        },
        "required": ["agent", "new_prompt", "reason"],
    },
}

PREPARE_ACTION = {
    "name": "prepare_action",
    "description": (
        "Prepare a real-world action for the user to approve — do NOT perform "
        "sensitive or irreversible actions directly. This queues the action "
        "with a preview; the user explicitly approves before it executes. Use "
        "for sending email, posting, and similar. Available types come from "
        "the action registry (e.g. send_email, call_webhook, save_note)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string",
                     "description": "Action type, e.g. 'send_email'"},
            "title": {"type": "string",
                      "description": "Short human-readable label"},
            "payload": {"type": "object",
                        "description": "Fields the action needs, e.g. "
                        "{to, subject, body} for send_email"},
        },
        "required": ["type", "payload"],
    },
}

PROPOSE_UPGRADE = {
    "name": "propose_upgrade",
    "description": (
        "Record an upgrade proposal for Olympus — a missing capability, a code "
        "change, a new specialist, or an architectural improvement that a human "
        "or a coding agent should implement. When GitHub is configured, the "
        "proposal is automatically filed as an issue on the Olympus repository, "
        "so write the details as a complete, self-contained ticket."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "details": {
                "type": "string",
                "description": "What's missing, why it matters, and a concrete "
                "implementation sketch",
            },
        },
        "required": ["title", "details"],
    },
}

# --- handlers -------------------------------------------------------------

_SOURCE_SUFFIXES = {".py", ".md", ".txt", ".toml", ".cfg"}

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _http_get(url: str) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    import re as _re
    html = _re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = _re.sub(r"(?s)<[^>]+>", " ", html)
    text = _re.sub(r"&nbsp;?", " ", text)
    text = _re.sub(r"&amp;", "&", text)
    return _re.sub(r"\s+", " ", text).strip()


def _ddg_search(query: str) -> str:
    """Client-side web search via DuckDuckGo's HTML endpoint (no API key)."""
    import re as _re
    import urllib.parse
    html = _http_get(
        "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    )
    titles = _re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, _re.DOTALL
    )
    snippets = _re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>', html, _re.DOTALL
    )
    results = []
    for i, (href, title) in enumerate(titles[:8]):
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
        snippet = _strip_html(snippets[i]) if i < len(snippets) else ""
        results.append(f"{_strip_html(title)}\n{href}\n{snippet}")
    return "\n\n".join(results) or "No results found."


def _web_fetch(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "Error: only http(s) URLs can be fetched."
    return _strip_html(_http_get(url))[:20_000]


def _list_source_files() -> str:
    files = []
    for path in sorted(config.PROJECT_ROOT.rglob("*")):
        if path.is_file() and path.suffix in _SOURCE_SUFFIXES:
            rel = path.relative_to(config.PROJECT_ROOT)
            if any(part in {"memory", ".git", "__pycache__", ".venv", "venv"}
                   for part in rel.parts):
                continue
            files.append(str(rel))
    return "\n".join(files)


def _read_source_file(path: str) -> str:
    target = (config.PROJECT_ROOT / path).resolve()
    if not str(target).startswith(str(config.PROJECT_ROOT.resolve())):
        return "Error: path escapes the project root."
    if not target.is_file():
        return f"Error: no such file: {path}"
    if target.suffix not in _SOURCE_SUFFIXES:
        return "Error: only source/prompt files can be read."
    return target.read_text(encoding="utf-8", errors="replace")[:40_000]


def _update_prompt(agent: str, new_prompt: str, reason: str) -> str:
    stem = Path(agent).stem  # tolerate 'argus.md' or 'prompts/argus'
    path = config.PROMPTS_DIR / f"{stem}.md"
    if not path.is_file():
        return f"Error: unknown agent prompt '{stem}'. Use list_source_files."
    old = path.read_text(encoding="utf-8")
    # Body is the verbatim old prompt (restore_prompt depends on this);
    # the update reason rides in a trailing comment that restore strips.
    memory.save("prompt_backups", stem,
                f"{old}\n<!-- update reason: {reason} -->")
    path.write_text(new_prompt.strip() + "\n", encoding="utf-8")
    return f"Prompt '{stem}' updated. Previous version backed up to memory/prompt_backups."


def _restore_prompt(agent: str) -> str:
    stem = Path(agent).stem
    path = config.PROMPTS_DIR / f"{stem}.md"
    if not path.is_file():
        return f"Error: unknown agent prompt '{stem}'."
    backups = sorted(
        (config.MEMORY_DIR / "prompt_backups").glob(f"*-{stem}.md"),
        reverse=True,
    ) if (config.MEMORY_DIR / "prompt_backups").exists() else []
    if not backups:
        return f"Error: no backups exist for '{stem}'."
    text = backups[0].read_text(encoding="utf-8")
    # strip the "# <stem>" header memory.save added, and the trailing comment
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[2:] if len(lines) > 1 and not lines[1].strip() else lines[1:]
    body = "\n".join(l for l in lines if not l.startswith("<!-- update reason:"))
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return f"Prompt '{stem}' restored from {backups[0].name}."


def _send_email(to: str, subject: str, body: str) -> str:
    import os as _os
    import smtplib
    from email.message import EmailMessage

    host = _os.environ.get("SMTP_HOST")
    if not host:
        return ("Error: email is not configured. The operator must set "
                "SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/SMTP_FROM.")
    allow = {a.strip().lower()
             for a in _os.environ.get("OLYMPUS_EMAIL_ALLOWLIST", "").split(",")
             if a.strip()}
    if not allow:
        return ("Error: OLYMPUS_EMAIL_ALLOWLIST is empty — sending is "
                "disabled until the operator allowlists recipients.")
    if to.strip().lower() not in allow:
        return f"Error: '{to}' is not in the recipient allowlist."

    msg = EmailMessage()
    msg["From"] = _os.environ.get("SMTP_FROM", _os.environ.get("SMTP_USER", ""))
    msg["To"] = to.strip()
    msg["Subject"] = subject.strip()[:200]
    msg.set_content(body)
    port = int(_os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if _os.environ.get("SMTP_TLS", "1") != "0":
            smtp.starttls()
        user = _os.environ.get("SMTP_USER")
        if user:
            smtp.login(user, _os.environ.get("SMTP_PASS", ""))
        smtp.send_message(msg)
    return f"Email sent to {to}."


def _parse_webhooks() -> dict[str, str]:
    import os as _os
    hooks = {}
    for pair in _os.environ.get("OLYMPUS_WEBHOOKS", "").split(","):
        name, _, url = pair.strip().partition("=")
        if name and url.startswith(("http://", "https://")):
            hooks[name] = url
    return hooks


def _call_webhook(name: str, payload: dict | None = None) -> str:
    import json as _json
    import urllib.request
    hooks = _parse_webhooks()
    if name not in hooks:
        configured = ", ".join(hooks) or "none configured"
        return (f"Error: no webhook named '{name}'. Configured webhooks: "
                f"{configured}. The operator defines them via OLYMPUS_WEBHOOKS.")
    req = urllib.request.Request(
        hooks[name],
        data=_json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "olympus-agent"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return (f"Webhook '{name}' responded {resp.status}: "
                f"{resp.read(500).decode(errors='replace')}")


def _run_benchmark() -> str:
    from . import evals  # local import to avoid a cycle at module load
    return evals.run_and_save()


def _run_code_benchmark() -> str:
    from . import code_eval  # local import to avoid a cycle at module load
    return code_eval.run_and_save()


def _prepare_action(type: str, payload: dict, title: str = None) -> str:
    """Queue a real-world action for the user to approve (never executes)."""
    from . import actions, builtin_actions  # noqa: F401  (registers built-ins)
    user = memory.current_user()
    payload = dict(payload or {})
    payload.setdefault("_user", user)  # some actions need to know the owner
    try:
        action = actions.prepare(user, type, payload, title=title)
    except ValueError as err:
        return (f"Error: {err}. Registered types: "
                f"{', '.join(actions.registered())}")
    return (f"Prepared action {action.id} ({action.type}, "
            f"{action.risk_class}) — awaiting your approval.\n\n{action.preview}")


def _gate_skills() -> str:
    from . import orchestrator  # local import to avoid a cycle at module load
    return orchestrator.gate_skills()


def _generate_benchmark(specialist: str) -> str:
    from . import evals  # local import to avoid a cycle at module load
    return evals.generate_item(specialist)


def _propose_upgrade(title: str, details: str) -> str:
    path = memory.save("upgrades", title, details)
    issue_url = github.create_issue(
        f"[Olympus upgrade] {title}",
        details
        + "\n\n---\n_Filed automatically by Prometheus, the Olympus "
          "evolution agent._",
    )
    if issue_url:
        return f"Proposal saved to {path} and filed as GitHub issue: {issue_url}"
    return (f"Proposal saved to {path}. (GitHub auto-filing not active — set "
            "GITHUB_TOKEN and GITHUB_REPO to open issues automatically.)")


def _watch_youtube(url: str) -> str:
    try:
        return youtube.fetch_transcript(url)
    except Exception as err:  # transcript disabled, bad URL, network...
        return f"Error watching video: {err}"


HANDLERS: dict[str, Callable[..., str]] = {
    # web fallback — only dispatched on non-Anthropic providers (on Anthropic
    # these names are server-side tools and never reach the client loop)
    "web_search": _ddg_search,
    "web_fetch": _web_fetch,
    "recall_memory": lambda query: memory.search(query),
    "recall_fact": lambda query: facts.lookup(query),
    "cache_fact": lambda claim, verdict, source="": facts.record(claim, verdict, source),
    # content is sanitized so injection-shaped text can't poison future recall
    "save_lesson": lambda title, content: str(
        memory.save("lessons", title, security.sanitize_for_memory(content))),
    "watch_youtube": _watch_youtube,
    "prepare_action": _prepare_action,
    "current_time": lambda: datetime.datetime.now().astimezone().isoformat(),
    "list_source_files": _list_source_files,
    "read_source_file": _read_source_file,
    "update_prompt": _update_prompt,
    "restore_prompt": _restore_prompt,
    "propose_upgrade": _propose_upgrade,
    "read_skill": lambda name: skills.read(name),
    # autonomously-created skills are provisional until a benchmark proves them
    "create_skill": lambda name, description, instructions, specialist=None:
        skills.create(name, description,
                      security.sanitize_for_memory(instructions),
                      specialist=specialist, provisional=True),
    "gate_skills": lambda: _gate_skills(),
    "generate_benchmark": lambda specialist: _generate_benchmark(specialist),
    "send_email": _send_email,
    "call_webhook": _call_webhook,
    "run_benchmark": _run_benchmark,
    "run_code_benchmark": _run_code_benchmark,
}

# Tools every specialist gets by default.
BASE_TOOLS = [RECALL_MEMORY, RECALL_FACT, SAVE_LESSON, READ_SKILL, CURRENT_TIME]

# Extra client-side tools, referenced by name in the specialist registry.
EXTRA_TOOLS: dict[str, dict[str, Any]] = {
    "watch_youtube": WATCH_YOUTUBE,
    "list_source_files": LIST_SOURCE_FILES,
    "read_source_file": READ_SOURCE_FILE,
    "update_prompt": UPDATE_PROMPT,
    "restore_prompt": RESTORE_PROMPT,
    "propose_upgrade": PROPOSE_UPGRADE,
    "prepare_action": PREPARE_ACTION,
    "create_skill": CREATE_SKILL,
    "gate_skills": GATE_SKILLS,
    "generate_benchmark": GENERATE_BENCHMARK,
    "run_code_benchmark": RUN_CODE_BENCHMARK,
    "send_email": SEND_EMAIL,
    "call_webhook": CALL_WEBHOOK,
    "run_benchmark": RUN_BENCHMARK,
}

# Anthropic server-side code sandbox (Hephaestus runs and tests code in it).
CODE_EXECUTION_TOOL = {"type": "code_execution_20260120", "name": "code_execution"}


def resolve_handler(name: str):
    """Find a tool handler: built-in first, then custom plugins."""
    handler = HANDLERS.get(name)
    if handler is not None:
        return handler
    from . import connectors  # local import to avoid an import cycle
    return connectors.plugin_handler(name)
