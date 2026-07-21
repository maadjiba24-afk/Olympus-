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


_MAX_RESPONSE_BYTES = 8 * 1024 * 1024      # cap a hostile/huge API body


def _get(path: str, repo: str | None = None) -> list | dict | None:
    """Authenticated GET against the GitHub API; None when unconfigured or
    unreachable (callers degrade, never crash). HTTPS-only (the bearer token
    must never ride a plaintext request) and size-capped."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = repo or os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return None
    api = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
    if not api.lower().startswith("https://"):
        import logging
        logging.getLogger("olympus.github").warning(
            "refusing non-HTTPS GITHUB_API %r — not sending the token", api)
        return None
    req = urllib.request.Request(
        f"{api}/repos/{repo}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "olympus-agent"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # a redirect to a non-HTTPS host would drop the token onto plaintext
            if not resp.geturl().lower().startswith("https://"):
                return None
            return json.loads(resp.read(_MAX_RESPONSE_BYTES + 1)
                              [:_MAX_RESPONSE_BYTES])
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
    """Paths changed by one PR (up to 300 files, paginated 100/page)."""
    out: list[str] = []
    for page in range(1, 4):                       # 3 pages × 100 = 300
        data = _get(f"pulls/{int(number)}/files?per_page=100&page={page}", repo)
        if not isinstance(data, list) or not data:
            break
        out.extend(f.get("filename") or "" for f in data if f.get("filename"))
        if len(data) < 100:
            break
    return out


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
