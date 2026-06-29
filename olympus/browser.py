"""Governed browser harness — Olympus's answer to "let the agent drive a real
browser" without inheriting the open-web harness threat model.

The popular pattern (e.g. browser-use/browser-harness) wires an LLM straight to
Chrome over CDP, `exec`s agent-written code into a namespace that can touch a
*credentialed* browser, auto-registers hundreds of unreviewed "skills", and
phones home by default. Each of those is a real capability — and a real risk.

This module keeps the capability and refuses the risk by making the browser a
first-class citizen of Olympus's existing governance instead of a bypass of it:

  * **Egress + SSRF gate.** Every navigation passes `security.url_block_reason`,
    so the harness can never reach an internal/metadata address, and under
    sovereign mode can only reach hosts you allowlisted. (credibility asset:
    the telemetry/egress flaw answered with default-deny.)

  * **Capability separation.** The one verb that can act on a *logged-in*
    session — `browser_act` (click/type/submit) — is a registered ACTION_TOOL.
    `security.filter_tools` strips it from any run that also ingests untrusted
    page content, so a prompt-injected page can never reach the actuator that
    operates your authenticated tabs. (credibility asset: the
    `exec`-into-live-session account-takeover kill-chain, structurally closed.)

  * **Replayable ledger.** Every CDP call is appended to a per-session ledger,
    so a session is auditable and replayable rather than a black box.
    (credibility asset: "self-improving == unpredictable" answered with a
    record you can diff.)

  * **Provenance-scored skills.** A browser "skill" carries its source, author,
    creation time, a content hash, and a *measured* reliability score. The
    library is ranked and sourced, not an unreviewed pile, and the count is
    bound to code like every other Olympus capability. (moat: a verified,
    scored corpus is a data network effect a copier starts at zero on.)

The CDP transport is pluggable. The real transport talks to Chrome over a
WebSocket and is built lazily (optional `websockets` dependency); tests and any
host without a browser use the in-memory FakeTransport. Nothing here imports
`websockets` at module load, so Olympus's core dependency set is unchanged.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import config, security

# --- transport -----------------------------------------------------------


class Transport(Protocol):
    """The minimal CDP transport contract. A real transport speaks WebSocket to
    Chrome; the fake one answers in-memory. Either way the session only needs
    request/response and a close."""

    def send(self, method: str, params: dict | None = None) -> dict: ...
    def close(self) -> None: ...


class BrowserUnavailable(RuntimeError):
    """No browser is attached and none can be built from the environment."""


class FakeTransport:
    """Deterministic, offline CDP stand-in for tests and headless CI.

    It records every call (so tests can assert the ledger) and answers a small
    set of CDP methods with scriptable page state. It performs no I/O — there is
    no real browser, so nothing here can navigate or act on anything.
    """

    def __init__(self, pages: dict[str, dict[str, str]] | None = None) -> None:
        # url -> {"title": ..., "text": ...}
        self.pages = pages or {}
        self.calls: list[dict[str, Any]] = []
        self._url = "about:blank"

    def send(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        self.calls.append({"method": method, "params": params})
        if method == "Page.navigate":
            self._url = params.get("url", self._url)
            return {"frameId": "fake-frame"}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            page = self.pages.get(self._url, {})
            if "document.title" in expr:
                value = page.get("title", "")
            elif "innerText" in expr or "textContent" in expr:
                value = page.get("text", "")
            else:
                value = page.get("text", "")
            return {"result": {"type": "string", "value": value}}
        if method in ("Input.dispatchMouseEvent", "Input.insertText",
                      "Input.dispatchKeyEvent"):
            return {}
        return {}

    def close(self) -> None:  # nothing to release
        self.calls.append({"method": "_close", "params": {}})


class _RealTransport:
    """CDP over WebSocket against a running Chrome (`--remote-debugging-port`).

    Built lazily so `websockets` stays an optional extra; importing this module
    never pulls it in. Not exercised in CI (no browser), but kept honest and
    minimal so a real attach works when configured.
    """

    def __init__(self, ws_url: str) -> None:
        try:
            from websockets.sync.client import connect  # type: ignore
        except Exception as err:  # pragma: no cover - optional dependency
            raise BrowserUnavailable(
                "real browser attach needs the optional 'websockets' package: "
                "pip install websockets") from err
        self._conn = connect(ws_url, open_timeout=10, max_size=None)
        self._id = 0

    def send(self, method: str, params: dict | None = None) -> dict:  # pragma: no cover
        self._id += 1
        self._conn.send(json.dumps(
            {"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._conn.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "CDP error"))
                return msg.get("result", {})

    def close(self) -> None:  # pragma: no cover
        try:
            self._conn.close()
        except Exception:
            pass


# A test/embedder hook: install a factory that builds the transport.
_TRANSPORT_FACTORY: Callable[[], Transport] | None = None


def set_transport_factory(factory: Callable[[], Transport] | None) -> None:
    """Install (or clear) the transport factory used by the global session.
    Tests pass a FakeTransport factory; clearing falls back to env detection."""
    global _TRANSPORT_FACTORY
    _TRANSPORT_FACTORY = factory
    reset()


def _build_transport() -> Transport:
    if _TRANSPORT_FACTORY is not None:
        return _TRANSPORT_FACTORY()
    ws_url = os.environ.get("OLYMPUS_BROWSER_CDP_URL", "").strip()
    if ws_url:
        return _RealTransport(ws_url)
    raise BrowserUnavailable(
        "no browser attached — start Chrome with --remote-debugging-port and "
        "set OLYMPUS_BROWSER_CDP_URL=ws://… (or inject a transport in tests).")


# --- session -------------------------------------------------------------


_TEXT_LIMIT = 20_000


class BrowserSession:
    """A stateful CDP session over a transport, with the governance baked in.

    The transport is the daemon-like persistent connection; the session adds the
    egress gate, the untrusted-ingestion flag, and the auditable ledger.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport
        self.url: str = "about:blank"
        # Every CDP call, in order — the replayable/auditable record.
        self.ledger: list[dict[str, Any]] = []
        # Set once the session has loaded any external page: from then on its
        # content is untrusted. Surfaced so the tool layer / orchestrator can
        # reason about capability separation.
        self.ingested_untrusted: bool = False

    def _call(self, method: str, **params: Any) -> dict:
        self.ledger.append({"method": method, "params": params})
        return self._t.send(method, params)

    def open(self, url: str) -> str:
        """Navigate to `url` through the SSRF + egress gate, return a snapshot."""
        reason = security.url_block_reason(url)
        if reason:
            return f"Error: {reason}."
        self._call("Page.navigate", url=url)
        self.url = url
        self.ingested_untrusted = True
        title = self._eval("document.title")
        text = self._eval("document.body ? document.body.innerText : ''")
        return self._snapshot(title, text)

    def read(self, selector: str = "") -> str:
        """Read readable text from the current page (or a CSS `selector`)."""
        if selector:
            expr = (f"(function(){{var e=document.querySelector("
                    f"{json.dumps(selector)});return e?e.innerText:'';}})()")
        else:
            expr = "document.body ? document.body.innerText : ''"
        self.ingested_untrusted = True
        text = self._eval(expr)
        return (text or "")[:_TEXT_LIMIT]

    def act(self, action: str, *, selector: str = "", text: str = "",
            x: int = 0, y: int = 0) -> str:
        """Act on the page (click / type). Credentialed actuator — exposed only
        as the ACTION_TOOL `browser_act`, so capability separation keeps it out
        of any run that also ingests untrusted content."""
        action = (action or "").lower()
        if action == "click":
            if selector:
                self._eval(f"(function(){{var e=document.querySelector("
                           f"{json.dumps(selector)});if(e)e.click();"
                           f"return !!e;}})()")
                return f"Clicked {selector}."
            self._call("Input.dispatchMouseEvent", type="mousePressed",
                       x=x, y=y, button="left", clickCount=1)
            self._call("Input.dispatchMouseEvent", type="mouseReleased",
                       x=x, y=y, button="left", clickCount=1)
            return f"Clicked at ({x}, {y})."
        if action == "type":
            self._call("Input.insertText", text=text)
            return f"Typed {len(text)} chars."
        return f"Error: unknown browser action '{action}'."

    def _eval(self, expression: str) -> str:
        res = self._call("Runtime.evaluate", expression=expression,
                         returnByValue=True)
        value = (res or {}).get("result", {}).get("value", "")
        return value if isinstance(value, str) else json.dumps(value)

    @staticmethod
    def _snapshot(title: str, text: str) -> str:
        body = (text or "")[:_TEXT_LIMIT]
        return f"# {title}\n\n{body}" if title else body

    def close(self) -> None:
        self._t.close()


_session: BrowserSession | None = None
_lock = threading.Lock()


def session() -> BrowserSession:
    """The process-global session (one persistent connection, daemon-style)."""
    global _session
    with _lock:
        if _session is None:
            _session = BrowserSession(_build_transport())
        return _session


def reset() -> None:
    """Drop the global session (used by tests and on reconfiguration)."""
    global _session
    with _lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
        _session = None


# --- provenance-scored skill registry ------------------------------------


@dataclass
class BrowserSkill:
    """A site-specific automation pattern with provenance and a measured score.

    Unlike an auto-accreted skill pile, every field that lets a human (or the
    orchestrator) trust or rank this skill is explicit: where it came from, who
    authored it, when, a content hash for integrity, and an outcome-derived
    reliability score."""

    domain: str
    name: str
    steps: str
    source: str = "agent"          # "agent" | "user" | "imported:<url>" | …
    author: str = "olympus"
    created: str = ""
    runs: int = 0
    successes: int = 0
    base_score: float = 0.0        # asserted prior before any runs (0..1)

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.datetime.now(
                datetime.timezone.utc).replace(microsecond=0).isoformat()

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(f"{self.domain}\n{self.name}\n{self.steps}".encode("utf-8"))
        return "sha256:" + h.hexdigest()[:16]

    @property
    def reliability(self) -> float:
        """Outcome-derived if we have runs, else the asserted prior."""
        if self.runs > 0:
            return round(self.successes / self.runs, 3)
        return round(self.base_score, 3)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "domain": self.domain, "name": self.name, "steps": self.steps,
            "source": self.source, "author": self.author,
            "created": self.created, "runs": self.runs,
            "successes": self.successes, "base_score": self.base_score,
        }
        d["content_hash"] = self.content_hash
        d["reliability"] = self.reliability
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BrowserSkill":
        return cls(
            domain=d["domain"], name=d["name"], steps=d["steps"],
            source=d.get("source", "agent"), author=d.get("author", "olympus"),
            created=d.get("created", ""), runs=int(d.get("runs", 0)),
            successes=int(d.get("successes", 0)),
            base_score=float(d.get("base_score", 0.0)),
        )


def _skills_path() -> "config.Path":  # type: ignore[name-defined]
    return config.MEMORY_DIR / "browser_skills.json"


def _load_skills() -> list[BrowserSkill]:
    path = _skills_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [BrowserSkill.from_dict(d) for d in raw if isinstance(d, dict)]


def _store_skills(skills: list[BrowserSkill]) -> None:
    path = _skills_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([s.to_dict() for s in skills], indent=2)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _key(domain: str, name: str) -> tuple[str, str]:
    return (domain.strip().lower(), name.strip().lower())


def record_skill(domain: str, name: str, steps: str, *, source: str = "agent",
                 author: str = "olympus", base_score: float = 0.0) -> BrowserSkill:
    """Insert or replace a skill (keyed by domain+name), preserving outcome
    counts on replacement so a re-recorded skill keeps its measured score."""
    skills = _load_skills()
    skill = BrowserSkill(domain=domain, name=name, steps=steps, source=source,
                         author=author, base_score=max(0.0, min(1.0, base_score)))
    out, replaced = [], False
    for existing in skills:
        if _key(existing.domain, existing.name) == _key(domain, name):
            skill.runs, skill.successes = existing.runs, existing.successes
            skill.created = existing.created
            out.append(skill)
            replaced = True
        else:
            out.append(existing)
    if not replaced:
        out.append(skill)
    _store_skills(out)
    return skill


def mark_outcome(domain: str, name: str, success: bool) -> BrowserSkill | None:
    """Record a run outcome; reliability becomes successes/runs."""
    skills = _load_skills()
    hit = None
    for s in skills:
        if _key(s.domain, s.name) == _key(domain, name):
            s.runs += 1
            if success:
                s.successes += 1
            hit = s
    if hit is not None:
        _store_skills(skills)
    return hit


def list_skills(domain: str = "") -> list[BrowserSkill]:
    """Skills ranked by measured reliability, then by number of runs."""
    skills = _load_skills()
    if domain:
        d = domain.strip().lower()
        skills = [s for s in skills if d in s.domain.strip().lower()]
    return sorted(skills, key=lambda s: (s.reliability, s.runs), reverse=True)
