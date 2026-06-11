"""Client-side tool definitions and handlers shared by Olympus agents.

Server-side tools (web_search / web_fetch) run on Anthropic's infrastructure —
no MCP servers, no extra plumbing. Client-side tools below run locally.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable

from . import config, github, memory, youtube

# --- server-side (Anthropic-hosted; this is how Olympus surfs the internet) --

WEB_TOOLS: list[dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
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
    memory.save("prompt_backups", f"{stem} (before update)",
                f"Reason for update: {reason}\n\n{old}")
    path.write_text(new_prompt.strip() + "\n", encoding="utf-8")
    return f"Prompt '{stem}' updated. Previous version backed up to memory/prompt_backups."


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
    "save_lesson": lambda title, content: str(memory.save("lessons", title, content)),
    "watch_youtube": _watch_youtube,
    "current_time": lambda: datetime.datetime.now().astimezone().isoformat(),
    "list_source_files": _list_source_files,
    "read_source_file": _read_source_file,
    "update_prompt": _update_prompt,
    "propose_upgrade": _propose_upgrade,
}

# Tools every specialist gets by default.
BASE_TOOLS = [RECALL_MEMORY, SAVE_LESSON, CURRENT_TIME]

# Extra client-side tools, referenced by name in the specialist registry.
EXTRA_TOOLS: dict[str, dict[str, Any]] = {
    "watch_youtube": WATCH_YOUTUBE,
    "list_source_files": LIST_SOURCE_FILES,
    "read_source_file": READ_SOURCE_FILE,
    "update_prompt": UPDATE_PROMPT,
    "propose_upgrade": PROPOSE_UPGRADE,
}
