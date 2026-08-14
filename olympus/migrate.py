"""Migrate from another agent framework into Olympus.

Peer assistants advertise automated importers ("import your settings,
memories, skills, and API keys"). This is the Olympus equivalent: point it at
another agent's data directory and it folds what it understands into Olympus —

  * ``MEMORY.md``           → durable lessons in Olympus memory
  * ``USER.md`` / ``user``  → the user's profile ("about" card)
  * ``skills/**/SKILL.md``  → the skill library (via agentskills.io import)
  * ``.env`` / keys         → **detected and reported, never silently stored**

Secrets are deliberately *not* imported by default — the function lists which
keys it found so you can move them across deliberately (or pass
``with_keys=True`` to write recognised provider keys into Olympus's config).
That mirrors the project's "secrets never silently land on disk" posture.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import memory, profile, security, skillpack

# Recognised provider key names → Olympus's expected env var.
_KEY_MAP = {
    "ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY": "OPENAI_API_KEY",
    "OPENROUTER_API_KEY": "OLYMPUS_API_KEY",
}


def _find(root: Path, *names: str) -> Path | None:
    for name in names:
        for cand in (root / name, root / name.lower(), root / name.upper()):
            if cand.is_file():
                return cand
    # case-insensitive scan of the top level as a fallback
    lowered = {n.lower() for n in names}
    for p in root.iterdir() if root.exists() else []:
        if p.is_file() and p.name.lower() in lowered:
            return p
    return None


def _parse_env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"\'')
    return out


def import_dir(root: str, user: str = "cli", *,
               with_keys: bool = False) -> dict:
    """Import a foreign agent directory. Returns a summary of what was migrated."""
    base = Path(root)
    summary = {"memories": 0, "skills": 0, "profile": False,
               "keys_found": [], "keys_imported": [], "notes": []}
    if not base.exists():
        summary["notes"].append(f"path not found: {root}")
        return summary

    # --- memories (MEMORY.md) ------------------------------------------
    mem = _find(base, "MEMORY.md")
    if mem:
        text = security.sanitize_for_memory(
            mem.read_text(encoding="utf-8", errors="replace"))
        # split on markdown headings / bullet groups into individual lessons
        chunks = [c.strip() for c in re.split(r"\n#{1,6} |\n\n", text) if c.strip()]
        memory.set_user("shared")
        for i, chunk in enumerate(chunks):
            memory.save("lessons", f"Imported memory {i + 1}", chunk[:4000])
            summary["memories"] += 1

    # --- profile (USER.md) ---------------------------------------------
    usr = _find(base, "USER.md", "user.md")
    if usr:
        about = usr.read_text(encoding="utf-8", errors="replace").strip()
        if about:
            profile.set_about(user, about[:4000])
            summary["profile"] = True

    # --- skills (agentskills.io) ---------------------------------------
    for skills_root in (base / "skills", base):
        if skills_root.exists():
            msgs = skillpack.import_dir(str(skills_root), provisional=True)
            if msgs:
                summary["skills"] += len(msgs)
                break

    # --- keys (detect; import only on request) -------------------------
    env_file = _find(base, ".env", "env", "keys.env")
    if env_file:
        found = _parse_env(env_file.read_text(encoding="utf-8", errors="replace"))
        for key in found:
            if key in _KEY_MAP or key.endswith("_API_KEY"):
                summary["keys_found"].append(key)
        if with_keys:
            from . import firstrun
            for key, target in _KEY_MAP.items():
                if found.get(key):
                    firstrun.save_env_value(target, found[key])
                    summary["keys_imported"].append(target)
        elif summary["keys_found"]:
            summary["notes"].append(
                "API keys were found but NOT imported. Re-run with --keys to "
                "import recognised provider keys, or set them yourself.")
    return summary


def render(summary: dict) -> str:
    lines = ["Migration complete:",
             f"  memories imported : {summary['memories']}",
             f"  skills imported   : {summary['skills']}",
             f"  profile imported  : {'yes' if summary['profile'] else 'no'}"]
    if summary["keys_found"]:
        lines.append(f"  keys found        : {', '.join(summary['keys_found'])}")
    if summary["keys_imported"]:
        lines.append(f"  keys imported     : {', '.join(summary['keys_imported'])}")
    for note in summary["notes"]:
        lines.append(f"  note: {note}")
    return "\n".join(lines)
