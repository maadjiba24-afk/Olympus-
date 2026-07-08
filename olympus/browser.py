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
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import config, security

# Hardening limits.
_TEXT_LIMIT = 20_000          # max chars of page text returned to the model
_LEDGER_MAX = 2_000           # bounded CDP-call ledger (circular)
_RECV_TIMEOUT = 30.0          # per-CDP-call deadline on the real transport
_WS_MAX_FRAME = 16 * 1024 * 1024   # cap a single CDP message (anti-OOM)
_LOAD_TIMEOUT = 12.0          # bounded wait for document.readyState=complete
_SKILL_STEPS_MAX = 10_000     # max chars of a recorded skill body
_SKILL_FIELD_MAX = 200        # max chars for domain/name/source/author
_SKILLS_MAX = 500             # cap the library; trim lowest-reliability beyond

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

    def __init__(self, pages: dict[str, dict[str, str]] | None = None,
                 redirects: dict[str, str] | None = None,
                 present: list[str] | None = None) -> None:
        # url -> {"title": ..., "text": ...}
        self.pages = pages or {}
        # navigated-url -> landed-url, to simulate a server/JS redirect so the
        # post-navigation SSRF re-check can be exercised offline.
        self.redirects = redirects or {}
        # CSS selectors that "exist" on the page, so exists()/fill()/login() can
        # be driven offline. Tracks what fill() set, too.
        self.present = set(present or [])
        self.filled: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self._url = "about:blank"

    def _matched_selector(self, expr: str) -> str | None:
        for sel in self.present:
            if json.dumps(sel) in expr:
                return sel
        return None

    def send(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        self.calls.append({"method": method, "params": params})
        if method == "Page.navigate":
            target = params.get("url", self._url)
            self._url = self.redirects.get(target, target)   # follow redirect
            return {"frameId": "fake-frame"}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            page = self.pages.get(self._url, {})
            if "querySelector" in expr:           # exists / fill / click / read
                sel = self._matched_selector(expr)
                if "innerText" in expr:           # selector read → text or ''
                    value: Any = page.get("text", "") if sel else ""
                else:                             # predicate / mutator → bool
                    if sel and "value=" in expr:
                        self.filled[sel] = "set"
                    value = sel is not None
                return {"result": {"value": value}}
            if "readyState" in expr:
                value = "complete"
            elif "document.title" in expr:
                value = page.get("title", "")
            elif "location" in expr or "href" in expr:
                value = self._url          # the *landed* URL (after redirects)
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
        # Bound a single CDP message so a hostile/huge page can't OOM us; we
        # truncate text to _TEXT_LIMIT anyway.
        self._conn = connect(ws_url, open_timeout=10, max_size=_WS_MAX_FRAME)
        self._id = 0

    def send(self, method: str, params: dict | None = None) -> dict:  # pragma: no cover
        self._id += 1
        self._conn.send(json.dumps(
            {"id": self._id, "method": method, "params": params or {}}))
        # Read replies until ours arrives, but never block past the deadline
        # (a hung tab or a dropped reply must not wedge the whole agent).
        deadline = time.monotonic() + _RECV_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP call {method} timed out")
            msg = json.loads(self._conn.recv(timeout=remaining))
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


def _resolve_page_ws(value: str) -> str:  # pragma: no cover - needs a browser
    """Turn `OLYMPUS_BROWSER_CDP_URL` into a *page-target* WebSocket URL.

    Accepts either a ready ws(s):// URL (used as-is) or a DevTools HTTP base
    like `http://127.0.0.1:9222` — in which case we discover an existing page
    target via `/json` (opening one via `/json/new` only if none exist). A page
    target's socket accepts `Page.navigate`/`Runtime.evaluate` directly, with no
    session plumbing.
    """
    value = value.strip()
    if value.startswith(("ws://", "wss://")):
        return value
    import urllib.request
    base = value.rstrip("/")
    with urllib.request.urlopen(base + "/json", timeout=10) as resp:
        targets = json.loads(resp.read().decode("utf-8"))
    pages = [t for t in targets
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if pages:
        return pages[0]["webSocketDebuggerUrl"]
    with urllib.request.urlopen(base + "/json/new", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))["webSocketDebuggerUrl"]


def _find_chrome() -> str | None:
    """Locate a Chrome/Chromium binary to launch (env override, PATH, then the
    bundled Playwright Chromium)."""
    import os
    import shutil
    from glob import glob
    if os.environ.get("OLYMPUS_BROWSER_BIN"):
        return os.environ["OLYMPUS_BROWSER_BIN"]
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    hits = sorted(glob(os.path.join(root, "chromium-*/chrome-linux/chrome")))
    return hits[-1] if hits else None


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chrome_args(binary: str, port: int, user_data_dir: str,
                 headless: bool = False) -> list[str]:
    """Command line for a remote-debuggable Chrome. Headed by default so a
    person can see it and sign in (manual mode); headless for automated runs."""
    args = [binary, f"--remote-debugging-port={port}",
            "--remote-allow-origins=*", f"--user-data-dir={user_data_dir}",
            "--no-first-run", "--no-default-browser-check"]
    if headless:
        args += ["--headless=new", "--disable-gpu", "--no-sandbox"]
    args.append("about:blank")
    return args


_launched: Any = None      # (Popen, base_url) of a browser we started


def launch_local(headless: bool = False, timeout: float = 20.0) -> str:
    """Launch a local Chrome with remote debugging and return its DevTools HTTP
    base. Reuses an already-launched one. Best-effort: raises BrowserUnavailable
    if no binary is found or DevTools never comes up."""
    global _launched
    if _launched is not None and _launched[0].poll() is None:
        return _launched[1]
    import subprocess
    import tempfile
    import time as _time
    import urllib.request
    binary = _find_chrome()
    if not binary:
        raise BrowserUnavailable(
            "no Chrome/Chromium found to launch — install Chrome or set "
            "OLYMPUS_BROWSER_BIN")
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="olympus-browser-")
    proc = subprocess.Popen(_chrome_args(binary, port, profile, headless),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/json/version", timeout=2) as r:
                if r.status == 200:
                    _launched = (proc, base)
                    return base
        except Exception:
            _time.sleep(0.2)
    proc.terminate()
    raise BrowserUnavailable("launched Chrome but its DevTools never responded")


def _autolaunch_enabled() -> bool:
    return os.environ.get("OLYMPUS_BROWSER_AUTOLAUNCH", "").strip().lower() in (
        "1", "true", "yes", "on")


def _build_transport() -> Transport:
    if _TRANSPORT_FACTORY is not None:
        return _TRANSPORT_FACTORY()
    endpoint = os.environ.get("OLYMPUS_BROWSER_CDP_URL", "").strip()
    if not endpoint and _autolaunch_enabled():
        # Consumer convenience: bring a browser up automatically so the user
        # never starts Chrome with debug flags themselves. Headed so manual
        # sign-in is visible (OLYMPUS_BROWSER_HEADLESS forces headless).
        headless = os.environ.get("OLYMPUS_BROWSER_HEADLESS", "").strip().lower() \
            in ("1", "true", "yes", "on")
        endpoint = launch_local(headless=headless)
    if endpoint:
        return _RealTransport(_resolve_page_ws(endpoint))
    raise BrowserUnavailable(
        "no browser attached — set OLYMPUS_BROWSER_AUTOLAUNCH=1 to let Olympus "
        "open one, or OLYMPUS_BROWSER_CDP_URL to attach to your own.")


# --- session -------------------------------------------------------------


class BrowserSession:
    """A stateful CDP session over a transport, with the governance baked in.

    The transport is the daemon-like persistent connection; the session adds the
    egress gate, the untrusted-ingestion flag, and the auditable ledger.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport
        self.url: str = "about:blank"
        # Every CDP call, in order — the replayable/auditable record. Bounded so
        # a long-lived session can't grow it without limit (like a browser's own
        # circular event buffer); the most recent _LEDGER_MAX calls are kept.
        self.ledger: deque[dict[str, Any]] = deque(maxlen=_LEDGER_MAX)
        # Set once the session has loaded any external page: from then on its
        # content is untrusted. Surfaced so the tool layer / orchestrator can
        # reason about capability separation.
        self.ingested_untrusted: bool = False

    def _call(self, method: str, **params: Any) -> dict:
        self.ledger.append({"method": method, "params": params})
        return self._t.send(method, params)

    def _current_url(self) -> str:
        """The URL actually loaded right now (after any redirect / JS nav)."""
        try:
            return self._eval("document.location ? document.location.href : ''")
        except Exception:
            return ""

    def _blocked_landing(self) -> str | None:
        """Re-run the SSRF/egress gate against the *landed* URL. The initial
        check only covers the URL we asked for; a 3xx redirect or a JS
        navigation can move the tab onto an internal host before we read it.
        Returns a reason if the current page must not be exposed, else None."""
        href = self._current_url()
        if not href or href.startswith("about:"):
            return None
        return security.url_block_reason(href)

    def _wait_ready(self, timeout: float = _LOAD_TIMEOUT) -> None:
        """Bounded poll for document.readyState == 'complete' so reads don't
        race a still-loading page. Best-effort: returns on timeout, never raises."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._eval("document.readyState") == "complete":
                    return
            except Exception:
                return
            time.sleep(0.25)

    def open(self, url: str) -> str:
        """Navigate to `url` through the SSRF + egress gate, return a snapshot."""
        reason = security.url_block_reason(url)
        if reason:
            return f"Error: {reason}."
        self._call("Page.navigate", url=url)
        self.url = url
        self.ingested_untrusted = True
        self._wait_ready()
        # Defend against redirect/JS-nav SSRF: if the tab landed somewhere the
        # gate forbids, navigate away and refuse to surface its content.
        landed = self._blocked_landing()
        if landed:
            self._call("Page.navigate", url="about:blank")
            self.url = "about:blank"
            return f"Error: navigation landed on a blocked address ({landed})."
        title = self._eval("document.title")
        text = self._eval("document.body ? document.body.innerText : ''")
        return self._snapshot(title, text)

    def read(self, selector: str = "") -> str:
        """Read readable text from the current page (or a CSS `selector`)."""
        self.ingested_untrusted = True
        landed = self._blocked_landing()
        if landed:
            return f"Error: current page is a blocked address ({landed})."
        if selector:
            expr = (f"(function(){{var e=document.querySelector("
                    f"{json.dumps(selector)});return e?e.innerText:'';}})()")
        else:
            expr = "document.body ? document.body.innerText : ''"
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

    def _eval_bool(self, expression: str) -> bool:
        res = self._call("Runtime.evaluate", expression=expression,
                         returnByValue=True)
        return bool((res or {}).get("result", {}).get("value"))

    def exists(self, selector: str) -> bool:
        """Structured predicate: is the selector present? Returns a bool, never
        page prose — so the actuator-holder can branch without ingesting text."""
        return self._eval_bool(
            f"!!document.querySelector({json.dumps(selector)})")

    def fill(self, selector: str, value: str) -> bool:
        """Set an input's value and fire input/change. `value` is sent to Chrome
        to type, never returned to the model (used for vault-sourced secrets)."""
        expr = (f"(function(){{var e=document.querySelector("
                f"{json.dumps(selector)});if(!e)return false;e.focus();"
                f"e.value={json.dumps(value)};"
                f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                f"e.dispatchEvent(new Event('change',{{bubbles:true}}));"
                f"return true;}})()")
        return self._eval_bool(expr)

    def run_template(self, steps: list[dict], params: dict | None = None) -> dict:
        """Execute a declarative action template step by step. Supported ops:
        `assert` (selector must exist), `click` (selector), `fill`
        (selector+value; value '$name' pulls from params), `wait`. Raises on a
        failed assert or unknown op — the spine turns that into a FAILED action.
        Page prose is never read as instructions; only selectors are touched."""
        params = params or {}
        done: list[str] = []
        for i, step in enumerate(steps or []):
            op = (step.get("op") or "").lower()
            sel = step.get("selector", "")
            if op == "assert":
                if not self.exists(sel):
                    raise RuntimeError(f"step {i}: required element {sel!r} "
                                       "is missing")
                done.append(f"assert {sel}")
            elif op == "click":
                self.act("click", selector=sel)
                done.append(f"click {sel}")
            elif op == "fill":
                val = step.get("value", "")
                if isinstance(val, str) and val.startswith("$"):
                    val = str(params.get(val[1:], ""))
                self.fill(sel, val)
                done.append(f"fill {sel}")
            elif op == "wait":
                self._wait_ready()
                done.append("wait")
            else:
                raise RuntimeError(f"step {i}: unknown op {op!r}")
        return {"steps": done}

    def login(self, profile: "SiteProfile", creds: dict) -> bool:
        """Drive a declarative login: navigate to the profile's login URL (SSRF/
        egress gated), fill username/password from `creds`, submit, and verify
        the success marker. Returns True iff the marker appears. No page prose is
        read as instructions; the password never leaves this method."""
        if self.open(profile.login_url).startswith("Error:"):
            return False
        if profile.username_selector:
            self.fill(profile.username_selector, str(creds.get("username", "")))
        if profile.password_selector:
            self.fill(profile.password_selector, str(creds.get("password", "")))
        if profile.submit_selector:
            self.act("click", selector=profile.submit_selector)
        self._wait_ready()
        if not profile.success_selector:
            return True
        return self.exists(profile.success_selector)

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
    if not isinstance(raw, list):
        return []
    out: list[BrowserSkill] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:                       # tolerate a hand-edited/partial entry
            out.append(BrowserSkill.from_dict(d))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _store_skills(skills: list[BrowserSkill]) -> None:
    path = _skills_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([s.to_dict() for s in skills], indent=2)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _key(domain: str, name: str) -> tuple[str, str]:
    return (domain.strip().lower(), name.strip().lower())


def _clip(text: str, limit: int) -> str:
    return (text or "").strip()[:limit]


def record_skill(domain: str, name: str, steps: str, *, source: str = "agent",
                 author: str = "olympus", base_score: float = 0.0) -> BrowserSkill:
    """Insert or replace a skill (keyed by domain+name), preserving outcome
    counts on replacement so a re-recorded skill keeps its measured score.

    Inputs are length-capped and the library is bounded (lowest-reliability
    skills are dropped past _SKILLS_MAX) so an over-eager agent can't bloat the
    on-disk store without limit.
    """
    domain = _clip(domain, _SKILL_FIELD_MAX)
    name = _clip(name, _SKILL_FIELD_MAX)
    if not domain or not name:
        raise ValueError("a browser skill needs a non-empty domain and name")
    skill = BrowserSkill(
        domain=domain, name=name, steps=_clip(steps, _SKILL_STEPS_MAX),
        source=_clip(source, _SKILL_FIELD_MAX) or "agent",
        author=_clip(author, _SKILL_FIELD_MAX) or "olympus",
        base_score=max(0.0, min(1.0, base_score)))
    skills = _load_skills()
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
    if len(out) > _SKILLS_MAX:      # keep the most reliable, drop the long tail
        out = sorted(out, key=lambda s: (s.reliability, s.runs),
                     reverse=True)[:_SKILLS_MAX]
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


# --- operator: site profiles + gating (HERMES, Phase 1) ------------------
#
# A Site Profile is the declarative, per-domain spec the operator acts through:
# how to log in (selectors) and how to tell it worked (success marker). It is a
# provenance-stamped, reliability-scored skill — never free-form "do what the
# page says". See docs/DESIGN_OPERATOR.md.


@dataclass
class SiteProfile:
    domain: str
    login_url: str = ""
    username_selector: str = ""
    password_selector: str = ""
    submit_selector: str = ""
    success_selector: str = ""        # present iff the login succeeded
    source: str = "agent"
    author: str = "olympus"
    created: str = ""
    runs: int = 0
    successes: int = 0
    # Declarative action templates: name -> {"risk", "steps":[{op,selector,value}],
    # "success_selector"?}. Steps are the ONLY thing the operator will do on a
    # site — there is no "interpret the page" path.
    templates: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.datetime.now(
                datetime.timezone.utc).replace(microsecond=0).isoformat()

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update("\n".join([
            self.domain, self.login_url, self.username_selector,
            self.password_selector, self.submit_selector,
            self.success_selector,
            json.dumps(self.templates, sort_keys=True)]).encode("utf-8"))
        return "sha256:" + h.hexdigest()[:16]

    @property
    def reliability(self) -> float:
        return round(self.successes / self.runs, 3) if self.runs else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = {f: getattr(self, f) for f in (
            "domain", "login_url", "username_selector", "password_selector",
            "submit_selector", "success_selector", "source", "author",
            "created", "runs", "successes", "templates")}
        d["content_hash"] = self.content_hash
        d["reliability"] = self.reliability
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SiteProfile":
        return cls(
            domain=d["domain"], login_url=d.get("login_url", ""),
            username_selector=d.get("username_selector", ""),
            password_selector=d.get("password_selector", ""),
            submit_selector=d.get("submit_selector", ""),
            success_selector=d.get("success_selector", ""),
            source=d.get("source", "agent"), author=d.get("author", "olympus"),
            created=d.get("created", ""), runs=int(d.get("runs", 0)),
            successes=int(d.get("successes", 0)),
            templates=d.get("templates") or {})


def _profiles_path() -> "config.Path":  # type: ignore[name-defined]
    return config.MEMORY_DIR / "site_profiles.json"


def _load_profiles() -> list[SiteProfile]:
    path = _profiles_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[SiteProfile] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        try:
            out.append(SiteProfile.from_dict(d))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _store_profiles(profiles: list[SiteProfile]) -> None:
    path = _profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([p.to_dict() for p in profiles], indent=2)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def record_profile(domain: str, *, login_url: str = "",
                   username_selector: str = "", password_selector: str = "",
                   submit_selector: str = "", success_selector: str = "",
                   source: str = "agent", author: str = "olympus") -> SiteProfile:
    """Insert or replace a site profile (keyed by domain), preserving outcome
    counts on replacement. Fields are length-capped."""
    domain = _clip(domain, _SKILL_FIELD_MAX).lower()
    if not domain:
        raise ValueError("a site profile needs a non-empty domain")
    prof = SiteProfile(
        domain=domain, login_url=_clip(login_url, 2048),
        username_selector=_clip(username_selector, _SKILL_FIELD_MAX),
        password_selector=_clip(password_selector, _SKILL_FIELD_MAX),
        submit_selector=_clip(submit_selector, _SKILL_FIELD_MAX),
        success_selector=_clip(success_selector, _SKILL_FIELD_MAX),
        source=_clip(source, _SKILL_FIELD_MAX) or "agent",
        author=_clip(author, _SKILL_FIELD_MAX) or "olympus")
    out, replaced = [], False
    for existing in _load_profiles():
        if existing.domain == domain:
            prof.runs, prof.successes = existing.runs, existing.successes
            prof.created = existing.created
            out.append(prof)
            replaced = True
        else:
            out.append(existing)
    if not replaced:
        out.append(prof)
    _store_profiles(out)
    return prof


def _builtin_profiles_dir() -> "config.Path":  # type: ignore[name-defined]
    return config.Path(__file__).resolve().parent / "profiles"


def builtin_profiles() -> list[SiteProfile]:
    """Curated site profiles shipped with Olympus (olympus/profiles/*.json),
    so the operator can act on common sites without the user authoring CSS
    selectors. Read-only seeds: marked source='builtin'; a user's own recorded
    profile for the same domain always wins (see _merged_profiles). Malformed
    or unreadable seed files are skipped, never fatal."""
    out: list[SiteProfile] = []
    d = _builtin_profiles_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for entry in (raw if isinstance(raw, list) else [raw]):
            if not isinstance(entry, dict):
                continue
            try:
                prof = SiteProfile.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                continue
            prof.source = "builtin"
            out.append(prof)
    return out


def _merged_profiles() -> list[SiteProfile]:
    """User-recorded profiles overlaid on the built-in catalog, keyed by
    domain — the user's own recipe for a domain always shadows the seed."""
    by_domain: dict[str, SiteProfile] = {p.domain: p for p in builtin_profiles()}
    for p in _load_profiles():                 # user recordings win
        by_domain[p.domain] = p
    return list(by_domain.values())


def get_profile(domain: str) -> SiteProfile | None:
    d = (domain or "").strip().lower()
    for p in _merged_profiles():
        if p.domain == d:
            return p
    return None


def list_profiles() -> list[SiteProfile]:
    return sorted(_merged_profiles(), key=lambda p: (p.reliability, p.runs),
                  reverse=True)


def mark_profile_outcome(domain: str, success: bool) -> SiteProfile | None:
    profiles = _load_profiles()
    hit = None
    d = (domain or "").strip().lower()
    for p in profiles:
        if p.domain == d:
            p.runs += 1
            if success:
                p.successes += 1
            hit = p
    if hit is not None:
        _store_profiles(profiles)
    return hit


def set_template(domain: str, name: str, risk: str, steps: list[dict],
                 success_selector: str = "") -> SiteProfile:
    """Add/replace a declarative action template on a domain's site profile,
    creating the profile if needed. Outcome counts are preserved."""
    domain = (domain or "").strip().lower()
    name = _clip(name, _SKILL_FIELD_MAX)
    if not domain or not name:
        raise ValueError("a template needs a non-empty domain and name")
    if risk not in ("notable", "irreversible", "financial_legal"):
        raise ValueError(f"unknown template risk: {risk!r}")
    if not isinstance(steps, list):
        raise ValueError("template steps must be a list")
    profiles = _load_profiles()
    prof = next((p for p in profiles if p.domain == domain), None)
    if prof is None:
        prof = SiteProfile(domain=domain)
        profiles.append(prof)
    prof.templates = dict(prof.templates or {})
    prof.templates[name] = {
        "risk": risk, "steps": steps[:64],
        "success_selector": _clip(success_selector, _SKILL_FIELD_MAX)}
    _store_profiles(profiles)
    return prof


def operator_enabled() -> bool:
    """Master switch. The whole credentialed-operator path is off by default."""
    return os.environ.get("OLYMPUS_OPERATOR", "").strip().lower() in (
        "1", "true", "yes", "on")


def operator_domains() -> list[str]:
    raw = os.environ.get("OLYMPUS_OPERATOR_DOMAINS", "")
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def domain_allowed(domain: str) -> bool:
    """An operator may only touch domains explicitly listed *and* on the egress
    allowlist — two independent fences."""
    d = (domain or "").strip().lower()
    if not d or d not in operator_domains():
        return False
    return security.egress_allowed(d)
