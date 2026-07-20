"""GitHub, zero deps: issue auto-filing plus the read-only PR endpoints the
graph-aware PR dashboard (codegraph_prs) needs.

Configure with:
    GITHUB_TOKEN  a token with `repo` (classic) or Issues read/write access
    GITHUB_REPO   e.g. "maadjiba24-afk/Olympus-"

When unset, proposals simply stay in memory/upgrades/ — auto-filing is
best-effort and never blocks the audit — and the PR dashboard reports itself
unconfigured instead of failing.
"""

from __future__ import annotations

import json
import os
import urllib.request


def configured() -> bool:
    return bool(os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"))


def _get(path: str, repo: str | None = None) -> list | dict | None:
    """Authenticated GET against the GitHub API; None when unconfigured or
    unreachable (callers degrade, never crash)."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = repo or os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return None
    api = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
    req = urllib.request.Request(
        f"{api}/repos/{repo}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "olympus-agent"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as err:
        import logging
        logging.getLogger("olympus.github").warning("GET %s failed: %s",
                                                    path, err)
        return None


def list_pulls(base: str | None = None, repo: str | None = None) -> list[dict]:
    """Open pull requests (number, title, author, branches, draft flag)."""
    qs = "?state=open&per_page=50" + (f"&base={base}" if base else "")
    data = _get(f"pulls{qs}", repo)
    if not isinstance(data, list):
        return []
    return [{"number": p.get("number"), "title": p.get("title") or "",
             "author": (p.get("user") or {}).get("login") or "?",
             "base": (p.get("base") or {}).get("ref") or "?",
             "head": (p.get("head") or {}).get("ref") or "?",
             "draft": bool(p.get("draft"))} for p in data]


def pull_files(number: int, repo: str | None = None) -> list[str]:
    """Paths changed by one PR (first 300 files — the API page bound)."""
    data = _get(f"pulls/{int(number)}/files?per_page=100", repo)
    if not isinstance(data, list):
        return []
    return [f.get("filename") or "" for f in data if f.get("filename")]


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
    except Exception as err:
        import logging
        logging.getLogger("olympus.github").warning("create_issue failed: %s", err)
        return None  # best-effort: the proposal is already saved to memory
