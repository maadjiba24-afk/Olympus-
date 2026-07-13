"""Google Calendar adapter — dependency-free, over the Calendar REST API.

Reads upcoming events (so the assistant can propose times) and creates events /
invitations that the Action spine gates. Reuses the same Google OAuth token as
the Gmail adapter (one connected Google account).

Reading the calendar is privacy-sensitive but has no side effect, so it's a
normal (untrusted-data) tool. CREATING an event — especially with attendees,
which emails them an invitation — is a side effect, so it's an Action type that
goes through the approval gate. The blueprint rule holds: propose times and
prepare the invite, but ask before sending it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import gmail

API = "https://www.googleapis.com/calendar/v3/calendars/primary"


class CalendarError(RuntimeError):
    pass


def configured() -> bool:
    return gmail.configured()


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {gmail._access_token()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:300]
        raise CalendarError(f"Calendar API {err.code}: {detail}") from None
    except urllib.error.URLError as err:
        raise CalendarError(f"Calendar unreachable: {err}") from None


# --- read (tool; no side effect) ----------------------------------------

def list_events(time_min: str, time_max: str, max_results: int = 20) -> str:
    """Summarize events between two ISO-8601 timestamps."""
    q = urllib.parse.urlencode({
        "timeMin": time_min, "timeMax": time_max, "maxResults": max_results,
        "singleEvents": "true", "orderBy": "startTime",
    })
    data = _request("GET", f"/events?{q}")
    items = data.get("items", [])
    if not items:
        return "No events in that range."
    out = []
    for e in items:
        start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "?")
        end = e.get("end", {}).get("dateTime") or e.get("end", {}).get("date", "")
        attendees = ", ".join(a.get("email", "") for a in e.get("attendees", []))
        out.append(f"[{e.get('id', '?')}] {e.get('summary', '(no title)')}\n"
                   f"  {start} → {end}"
                   + (f"\n  with: {attendees}" if attendees else ""))
    return "\n\n".join(out)


def upcoming(days: int = 14, max_results: int = 20) -> list[dict]:
    """Structured upcoming events (for the agenda UI), from now to `days` out.
    Read-only. Returns [] on any Calendar error so a UI can degrade gracefully
    rather than surfacing a stack trace."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    time_min = now.isoformat()
    time_max = (now + datetime.timedelta(days=max(1, days))).isoformat()
    q = urllib.parse.urlencode({
        "timeMin": time_min, "timeMax": time_max,
        "maxResults": max(1, min(max_results, 50)),
        "singleEvents": "true", "orderBy": "startTime",
    })
    try:
        data = _request("GET", f"/events?{q}")
    except CalendarError:
        return []
    out = []
    for e in data.get("items", []):
        start = e.get("start", {})
        end = e.get("end", {})
        out.append({
            "id": e.get("id", ""),
            "summary": e.get("summary", "(no title)"),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end.get("dateTime") or end.get("date", ""),
            "all_day": "date" in start and "dateTime" not in start,
            "attendees": [a.get("email", "") for a in e.get("attendees", [])],
        })
    return out


# --- operations (used by gated Action types) ----------------------------

def create_event(summary: str, start: str, end: str,
                 attendees: list[str] | None = None,
                 description: str = "") -> dict:
    body = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "description": description,
    }
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    # sendUpdates=all emails the invitation to attendees.
    return _request("POST", "/events?sendUpdates=all", body)


def delete_event(event_id: str) -> dict:
    # sendUpdates=all sends a cancellation to attendees (best-effort undo).
    return _request("DELETE", f"/events/{event_id}?sendUpdates=all")
