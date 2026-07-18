"""Gmail adapter — dependency-free, over the Gmail REST API.

Reads the inbox (so agents can triage and draft replies) and performs send /
draft / archive operations that the Action spine gates. No SDK — just urllib +
an OAuth bearer token, matching the rest of Olympus.

Auth: set a Google OAuth access token in GMAIL_ACCESS_TOKEN. Access tokens
expire (~1h); for unattended use also set GMAIL_REFRESH_TOKEN, GMAIL_CLIENT_ID,
and GMAIL_CLIENT_SECRET and the adapter refreshes automatically.

Reading is privacy-sensitive but has no side effect, so it's a normal tool
(gated only by having connected an account). SENDING, DRAFTING, and ARCHIVING
are side effects, so they are Action types that go through the approval gate —
sending is irreversible (always needs approval); drafts/archive are reversible.
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# cached access token + expiry (refreshed on demand)
_token: dict = {"value": None, "expires": 0.0}


class GmailError(RuntimeError):
    pass


def _access_token() -> str:
    """Return a valid access token.

    Order of preference: (1) the per-user Google account connected through the
    OAuth flow (encrypted vault), so each user uses their own mailbox;
    (2) environment-configured token/refresh, for single-user/dev setups.
    """
    # 1. Per-user OAuth token (multi-user, self-serve connect)
    try:
        from . import google_oauth, memory
        user = memory.current_user()
        if google_oauth.connected(user):
            return google_oauth.access_token(user)
    except Exception as err:
        logging.getLogger("olympus.gmail").warning(
            "OAuth token lookup failed; falling back to env auth: %s", err)

    now = time.time()
    if _token["value"] and now < _token["expires"] - 60:
        return _token["value"]

    refresh = os.environ.get("GMAIL_REFRESH_TOKEN")
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    secret = os.environ.get("GMAIL_CLIENT_SECRET")
    if refresh and client_id and secret:
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": client_id, "client_secret": secret,
        }).encode()
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(TOKEN_URL, data=data), timeout=30) as r:
                payload = json.loads(r.read())
        except urllib.error.URLError as err:
            raise GmailError(f"token refresh failed: {err}") from None
        _token["value"] = payload["access_token"]
        _token["expires"] = now + int(payload.get("expires_in", 3600))
        return _token["value"]

    direct = (os.environ.get("GMAIL_ACCESS_TOKEN")
              or os.environ.get("GOOGLE_ACCESS_TOKEN"))
    if direct:
        return direct
    raise GmailError(
        "Gmail not connected — set GMAIL_ACCESS_TOKEN (and optionally "
        "GMAIL_REFRESH_TOKEN/GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET to auto-refresh).")


def configured() -> bool:
    return bool(os.environ.get("GMAIL_ACCESS_TOKEN")
                or os.environ.get("GMAIL_REFRESH_TOKEN"))


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:300]
        raise GmailError(f"Gmail API {err.code}: {detail}") from None
    except urllib.error.URLError as err:
        raise GmailError(f"Gmail unreachable: {err}") from None


def _b64url_mime(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# --- read (tool; no side effect) ----------------------------------------

def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _received(msg: dict) -> str:
    """A reliable received-time from Gmail's internalDate (epoch ms), falling
    back to the Date header. Returns '' if neither is present."""
    ms = msg.get("internalDate")
    if ms:
        try:
            return datetime.datetime.fromtimestamp(
                int(ms) / 1000, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError, OverflowError):
            pass
    return _header(msg, "Date")


def list_inbox(query: str = "in:inbox", max_results: int = 10) -> str:
    """Summarize recent inbox messages (date, sender, subject, snippet)."""
    q = urllib.parse.urlencode({"q": query, "maxResults": max_results})
    listing = _request("GET", f"/messages?{q}")
    ids = [m["id"] for m in listing.get("messages", [])]
    if not ids:
        return "Inbox query returned no messages."
    out = []
    for mid in ids:
        m = _request("GET", f"/messages/{mid}?format=metadata"
                            "&metadataHeaders=From&metadataHeaders=Subject"
                            "&metadataHeaders=Date")
        out.append(f"[{mid}] {_received(m)} — From: {_header(m, 'From')}\n"
                   f"  Subject: {_header(m, 'Subject')}\n"
                   f"  {m.get('snippet', '')[:200]}")
    return "\n\n".join(out)


def _plain_body(m: dict) -> str:
    payload = m.get("payload", {})
    parts = payload.get("parts", [payload])
    for p in parts:
        data = p.get("body", {}).get("data")
        if data and p.get("mimeType", "").startswith("text/plain"):
            return base64.urlsafe_b64decode(data + "===").decode(
                "utf-8", errors="replace")
    return m.get("snippet", "")


def get_message(message_id: str) -> str:
    m = _request("GET", f"/messages/{message_id}?format=full")
    body = _plain_body(m)
    return (f"From: {_header(m, 'From')}\nDate: {_received(m)}\n"
            f"Subject: {_header(m, 'Subject')}\n\n{body[:4000]}")


def parse_address(from_header: str) -> str:
    """Extract the bare email address from a 'Name <addr@x>' From header."""
    from email.utils import parseaddr
    return parseaddr(from_header or "")[1].lower()


def _authenticated(m: dict) -> bool:
    """Whether Gmail's own SPF/DKIM/DMARC evaluation vouches for the sender.

    The From: header is trivially spoofable; Authentication-Results is Gmail's
    verdict on whether the message really came from the domain it claims. The
    email gateway requires this when a sender allowlist is in force — an
    allowlist that trusts a spoofable header is not an allowlist."""
    results = _header(m, "Authentication-Results").lower()
    return "dmarc=pass" in results or ("spf=pass" in results
                                       and "dkim=pass" in results)


def list_unread(max_results: int = 10) -> list[dict]:
    """Structured list of unread inbox messages, for the email gateway.
    Each dict: {id, from, sender, subject, body, authenticated}."""
    q = urllib.parse.urlencode({"q": "in:inbox is:unread",
                                "maxResults": max_results})
    listing = _request("GET", f"/messages?{q}")
    out: list[dict] = []
    for ref in listing.get("messages", []):
        mid = ref.get("id")
        if not mid:
            continue
        m = _request("GET", f"/messages/{mid}?format=full")
        frm = _header(m, "From")
        out.append({
            "id": mid,
            "from": frm,
            "sender": parse_address(frm),
            "subject": _header(m, "Subject"),
            "body": _plain_body(m)[:4000],
            "authenticated": _authenticated(m),
        })
    return out


def mark_read(message_id: str) -> dict:
    """Remove the UNREAD label so the gateway doesn't re-answer a message."""
    return _request("POST", f"/messages/{message_id}/modify",
                    {"removeLabelIds": ["UNREAD"]})


# --- operations (used by gated Action types) ----------------------------

def send(to: str, subject: str, body: str) -> dict:
    return _request("POST", "/messages/send",
                    {"raw": _b64url_mime(to, subject, body)})


def create_draft(to: str, subject: str, body: str) -> dict:
    return _request("POST", "/drafts",
                    {"message": {"raw": _b64url_mime(to, subject, body)}})


def delete_draft(draft_id: str) -> dict:
    return _request("DELETE", f"/drafts/{draft_id}")


def archive(message_id: str) -> dict:
    return _request("POST", f"/messages/{message_id}/modify",
                    {"removeLabelIds": ["INBOX"]})


def unarchive(message_id: str) -> dict:
    return _request("POST", f"/messages/{message_id}/modify",
                    {"addLabelIds": ["INBOX"]})
