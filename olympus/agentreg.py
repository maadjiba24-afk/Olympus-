"""Dynamic agent registry — operator-defined specialists from files.

Olympus's 13 specialists are compile-time data in `specialists.SPECIALISTS`.
Ruflo's counterpart is ~100 Markdown agent definitions an operator can drop in.
This module gives Olympus that extensibility NATIVELY and SAFELY: an operator
writes a `<key>.md` file (a small frontmatter header + the agent's system
prompt), and it becomes a first-class specialist that Zeus/Athena can route to —
without editing source.

The load path is deterministic (files sorted, no clock/rng) and SAFETY-BOUNDED
by construction, mirroring `subagents._is_privileged`:

  * a file agent is ALWAYS `system=False` and `code_exec=False` — a file can
    never mint a self-modifying or sandbox-exec agent;
  * its requested tools are filtered against `security.ACTION_TOOLS`, so a file
    agent can read/ingest but never send an email, drive a browser, or rewrite a
    prompt — capability separation holds even for agents that ingest the web;
  * it can never shadow a built-in specialist (a key collision keeps the
    built-in).

Opt-in via `OLYMPUS_AGENTS` (default OFF): with the flag off, `install()` is a
no-op and the registry is exactly the built-in 13, so the default install is
byte-identical. `install()` merges into the live `SPECIALISTS` dict in place, so
every consumer (`roster()`, Athena's plan enum, dispatch) picks the new agents up
with no call-site changes; `uninstall()` reverses it for tests.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config, security

_VALID_ROLES = ("reasoning", "coding", "verify")
_VALID_EFFORT = ("low", "medium", "high")
_MAX_AGENTS = 100
_MAX_PROMPT = 20_000


def enabled() -> bool:
    """Whether file-defined agents load. Default OFF — the registry is the
    built-in specialists until an operator opts in."""
    return os.environ.get("OLYMPUS_AGENTS", "").strip().lower() in (
        "1", "on", "true", "yes")


def agents_dir() -> Path:
    """Where file agents live: `OLYMPUS_AGENTS_DIR`, else `<MEMORY_DIR>/agents`."""
    override = os.environ.get("OLYMPUS_AGENTS_DIR")
    if override:
        return Path(override)
    return config.MEMORY_DIR / "agents"


# --- frontmatter parsing (dependency-free) -------------------------------

def _parse(text: str) -> tuple[dict, str]:
    """Split a `---`-delimited `key: value` frontmatter header from the prompt
    body. No YAML dependency — flat scalars only, which is all an agent card
    needs. Missing/blank frontmatter yields ({}, whole-text)."""
    meta: dict = {}
    body = text or ""
    lines = body.splitlines()
    if lines and lines[0].strip() == "---":
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            for line in lines[1:end]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip().lower()] = v.strip()
            body = "\n".join(lines[end + 1:])
    return meta, body.strip()


def _safe_tools(raw: str) -> tuple[str, ...]:
    """Requested tools minus any action tool (capability separation at load
    time) and minus any tool Olympus doesn't define."""
    from . import tools
    known = set(tools.EXTRA_TOOLS)
    out = []
    for name in (raw or "").replace(",", " ").split():
        name = name.strip()
        if name and name in known and name not in security.ACTION_TOOLS:
            out.append(name)
    return tuple(dict.fromkeys(out))          # dedupe, order-stable


def _build_one(key: str, text: str, static_keys) -> "object | None":
    """Build a safety-bounded Specialist from one file's text, or None if it is
    malformed or would shadow a built-in."""
    from .specialists import Specialist
    key = "".join(c for c in key.lower() if c.isalnum() or c == "_")[:32]
    if not key or key in static_keys:
        return None                            # never shadow a built-in
    meta, body = _parse(text)
    if not body:
        return None
    role = meta.get("role", "reasoning")
    if role not in _VALID_ROLES:
        role = "reasoning"
    effort = meta.get("effort", "medium")
    if effort not in _VALID_EFFORT:
        effort = "medium"
    web = meta.get("web", "").strip().lower() in ("1", "true", "yes", "on")
    return Specialist(
        key=key,
        name=(meta.get("name") or key.title())[:40],
        title=(meta.get("title") or "Custom Agent")[:60],
        description=(meta.get("description") or body[:160])[:400],
        web=web,
        code_exec=False,                       # never from a file
        system=False,                          # never from a file
        extra_tools=_safe_tools(meta.get("tools", "")),
        role=role,
        effort=effort,
        prompt_text=body[:_MAX_PROMPT],
    )


def load(static_keys=None) -> dict:
    """Every valid file-defined agent, keyed by agent key. Deterministic (files
    sorted). `static_keys` names the built-ins to never shadow; defaults to the
    live built-in set."""
    if static_keys is None:
        from .specialists import SPECIALISTS
        static_keys = set(SPECIALISTS)
    else:
        static_keys = set(static_keys)
    d = agents_dir()
    if not d.is_dir():
        return {}
    out: dict = {}
    for path in sorted(d.glob("*.md")):
        if len(out) >= _MAX_AGENTS:
            break
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        spec = _build_one(path.stem, text, static_keys)
        if spec is not None and spec.key not in out:
            out[spec.key] = spec
    return out


# --- installation into the live registry ---------------------------------

_installed: list = []


def install(into: dict | None = None) -> list[str]:
    """Merge file-defined agents into the live SPECIALISTS dict in place (so
    roster/plan/dispatch see them). No-op when disabled. Idempotent per process.
    Returns the keys installed."""
    if not enabled():
        return []
    if into is None:
        from .specialists import SPECIALISTS as into
    loaded = load(static_keys=set(into))
    added = []
    for key, spec in loaded.items():
        if key not in into:                    # never overwrite a built-in
            into[key] = spec
            added.append(key)
    _installed[:] = added
    return added


def uninstall(into: dict | None = None) -> None:
    """Remove previously-installed file agents (test aid)."""
    if into is None:
        from .specialists import SPECIALISTS as into
    for key in _installed:
        into.pop(key, None)
    _installed[:] = []


def installed_keys() -> list[str]:
    return list(_installed)
