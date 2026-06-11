"""Auto-file Prometheus's upgrade proposals as GitHub issues — zero deps.

Configure with:
    GITHUB_TOKEN  a token with `repo` (classic) or Issues read/write access
    GITHUB_REPO   e.g. "maadjiba24-afk/Olympus-"

When unset, proposals simply stay in memory/upgrades/ — auto-filing is
best-effort and never blocks the audit.
"""

from __future__ import annotations

import json
import os
import urllib.request


def configured() -> bool:
    return bool(os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"))


def create_issue(title: str, body: str) -> str | None:
    """Open a GitHub issue; return its URL, or None if unconfigured/unreachable."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return None
    api = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
    req = urllib.request.Request(
        f"{api}/repos/{repo}/issues",
        data=json.dumps({"title": title[:250], "body": body[:60_000]}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "olympus-agent",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()).get("html_url")
    except Exception:
        return None  # best-effort: the proposal is already saved to memory
