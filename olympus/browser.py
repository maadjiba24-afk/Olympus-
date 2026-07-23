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
_HTML_LIMIT = 400_000         # max chars of serialized page HTML returned
_LEDGER_MAX = 2_000           # bounded CDP-call ledger (circular)
_RECV_TIMEOUT = 30.0          # per-CDP-call deadline on the real transport
_WS_MAX_FRAME = 16 * 1024 * 1024   # cap a single CDP message (anti-OOM)
_LOAD_TIMEOUT = 12.0          # bounded wait for document.readyState=complete
_SKILL_STEPS_MAX = 10_000     # max chars of a recorded skill body
_SKILL_FIELD_MAX = 200        # max chars for domain/name/source/author
_SKILLS_MAX = 500             # cap the library; trim lowest-reliability beyond
_OBSERVE_MAX = 120            # max interactive elements returned by observe()
_AX_MAX = 200                 # max accessibility-tree nodes returned by read_ax()
_CONSOLE_MAX = 200            # max console messages returned by console_logs()
_LABEL_MAX = 80               # per-element label cap (anti-injection: no prose)
_JOURNAL_MAX = 80             # max first-party act steps kept for skill-learning

# Deep-query prelude: installs __olyAll(sel)/__olyq(sel) that walk not just the
# light DOM but every OPEN shadow root and SAME-ORIGIN iframe, so the harness can
# perceive and act on components that hide their controls behind those boundaries
# (most modern web apps). Cross-origin frames throw on access and are skipped —
# the same-origin policy is a boundary we honor, not one we try to defeat. This
# prelude is auto-prepended (see _eval/_eval_bool) to any expression that calls
# __olyq, so observe() stamps and act(index=N) resolve over the identical tree.
_DEEP_MAX_DEPTH = 12          # bound shadow/iframe recursion (anti-hang/overflow)
_DEEP_JS = (
    "window.__olyAll=function(sel){var out=[];"
    "function walk(root,depth){if(depth>DMAX)return;"
    "var m;try{m=root.querySelectorAll(sel);}catch(e){return;}"
    "for(var i=0;i<m.length;i++)out.push(m[i]);"
    "var all;try{all=root.querySelectorAll('*');}catch(e){all=[];}"
    "for(var j=0;j<all.length;j++){var el=all[j];"
    "if(el.shadowRoot)walk(el.shadowRoot,depth+1);"
    "if(el.tagName==='IFRAME'){var d=null;try{d=el.contentDocument;}catch(e){d=null;}"
    "if(d)walk(d,depth+1);}}}"
    "walk(document,0);return out;};"
    "window.__olyq=function(sel){return window.__olyAll(sel)[0]||null;};"
    # __olySel(e): a DURABLE selector for an element (id, then [name], then
    # [aria-label], then an nth-of-type path) — stable across reloads, unlike the
    # ephemeral data-olympus-idx stamp. Used to promote a proven flow into a
    # declarative template whose steps still resolve on a fresh page load.
    "window.__olySel=function(e){if(!e||!e.tagName)return '';"
    "var t=e.tagName.toLowerCase();"
    "if(e.id)return '#'+e.id;"
    "var n=e.getAttribute&&e.getAttribute('name');if(n)return t+'[name=\\''+n+'\\']';"
    "var a=e.getAttribute&&e.getAttribute('aria-label');"
    "if(a)return t+'[aria-label=\\''+a+'\\']';"
    # Otherwise build a ROOTED path up to the nearest ancestor with an id (or the
    # local root), so the selector resolves unambiguously instead of matching any
    # nth-of-type sibling anywhere on the page.
    "var parts=[],cur=e,depth=0;"
    "while(cur&&cur.tagName&&depth<8){"
    "if(cur.id){parts.unshift('#'+cur.id);break;}"
    "var tg=cur.tagName.toLowerCase(),i=1;"
    "for(var s=cur.previousElementSibling;s;s=s.previousElementSibling)"
    "{if(s.tagName===cur.tagName)i++;}"
    "parts.unshift(tg+':nth-of-type('+i+')');"
    "cur=cur.parentElement;depth++;}"
    "return parts.join('>');};"
).replace("DMAX", str(_DEEP_MAX_DEPTH))

# The perception step of the harness, as ONE Runtime.evaluate: deep-walk the light
# DOM plus open shadow roots and same-origin iframes, find the visible interactive
# elements, stamp each with a stable index (data-olympus-idx) that act(index=N)
# resolves, and return a compact JSON list. The __OLY_OBSERVE__ marker lets the
# offline transport route this without depending on incidental substrings. Element
# *labels* are page-controlled, so each is hard-capped to _LABEL_MAX chars — enough
# to pick the right control, too short to smuggle a paragraph of injected prose.
_OBSERVE_JS = (
    "(function(){/*__OLY_OBSERVE__*/"
    "var q='a[href],button,input,select,textarea,[role=button],[role=link],"
    "[role=checkbox],[role=tab],[role=menuitem],[onclick],"
    "[contenteditable=true],summary,[tabindex]';"
    "var out=[],idx=0;"
    "function vis(e){var r=e.getBoundingClientRect();"
    "if(!(r.width>0&&r.height>0))return false;if(e.disabled)return false;"
    "var st;try{st=window.getComputedStyle(e);}catch(x){return false;}"
    "if(st.visibility==='hidden'||st.display==='none')return false;return true;}"
    "function take(e){if(out.length>=LIMIT)return;if(!vis(e))return;"
    "e.setAttribute('data-olympus-idx',idx);"
    "var nm=(e.getAttribute('aria-label')||e.placeholder||e.value||"
    "(e.innerText||e.textContent||'')||e.getAttribute('alt')||e.title||'');"
    "nm=nm.replace(/\\s+/g,' ').trim().slice(0,CAP);"
    "var tg=e.tagName.toLowerCase();"
    "var ty=tg==='input'?('input:'+(e.getAttribute('type')||'text')):tg;"
    "out.push({i:idx,t:ty,n:nm,s:__olySel(e)});idx++;}"
    "function walk(root,depth){if(depth>DMAX)return;"
    "var all;try{all=root.querySelectorAll('*');}catch(x){return;}"
    "for(var i=0;i<all.length&&out.length<LIMIT;i++){var el=all[i];"
    "try{if(el.matches&&el.matches(q))take(el);}catch(x){}"
    "if(el.shadowRoot)walk(el.shadowRoot,depth+1);"
    "if(el.tagName==='IFRAME'){var d=null;try{d=el.contentDocument;}catch(x){d=null;}"
    "if(d)walk(d,depth+1);}}}"
    "walk(document,0);"
    "return JSON.stringify(out);})()"
).replace("DMAX", str(_DEEP_MAX_DEPTH))

# Human-verification checkpoint DETECTOR (the moat's "detect, don't defeat"
# step). It recognizes the common human-check widgets/flows by their stable
# markers and returns a TYPE ENUM only — never page prose — so nothing here is a
# defeat attempt or an injection surface. The __OLY_CHECKPOINT__ marker lets the
# offline transport route it. Providers are matched by their own iframe/src
# fingerprints (which are visible even on a cross-origin challenge frame, without
# reaching INTO it — we honor the origin boundary, we just see the wall).
_CHECKPOINT_JS = (
    "(function(){/*__OLY_CHECKPOINT__*/"
    "function has(s){try{return !!document.querySelector(s);}catch(e){return false;}}"
    "if(has('.g-recaptcha')||has(\"iframe[src*='recaptcha']\")||"
    "has(\"iframe[title*='recaptcha']\"))"
    "return JSON.stringify({type:'captcha',detail:'recaptcha'});"
    "if(has('.h-captcha')||has(\"iframe[src*='hcaptcha']\"))"
    "return JSON.stringify({type:'captcha',detail:'hcaptcha'});"
    "if(has('.cf-turnstile')||has(\"iframe[src*='challenges.cloudflare.com']\"))"
    "return JSON.stringify({type:'captcha',detail:'turnstile'});"
    "if(has(\"input[autocomplete='one-time-code']\")||"
    "has(\"input[name*='otp']\")||has(\"input[name*='onetime']\"))"
    "return JSON.stringify({type:'otp',detail:'one-time-code'});"
    "var t=((document.body?document.body.innerText:'')||'').toLowerCase();"
    "var cues=['verify it','two-factor','two factor','2-step','2 step',"
    "'enter the code we','confirm your identity','verify your identity'];"
    "for(var i=0;i<cues.length;i++){if(t.indexOf(cues[i])>=0)"
    "return JSON.stringify({type:'step_up',detail:'interstitial'});}"
    "return JSON.stringify({type:'none',detail:''});})()"
)

_CHECKPOINT_KINDS = ("captcha", "otp", "step_up", "none")

_SCROLLABLE_MAX = 8           # max scrollable regions surfaced by observe()
_SCROLL_WALK_MAX = 4000       # cap DOM nodes examined for scrollables (anti-O(page))

# Scroll affordances for observe() — page geometry (how much is off-screen) AND
# the scrollable containers — in ONE Runtime.evaluate, so both are measured at the
# SAME instant (a separate geometry eval and scrollables eval could disagree if
# the page scrolls between them). Returns {geom:{vw,vh,pw,ph,sx,sy}, scroll:[{s,
# db,rr}]}. Uses __olySel (auto-injected by _prep). The scrollable walk is bounded
# to _SCROLL_WALK_MAX nodes examined and _SCROLLABLE_MAX emitted, so a huge DOM
# can't make perception O(page). Pure measurement — no page text, not an ingestion
# surface. __OLY_PERCEPT__ routes it offline; fails soft to '' (→ omitted).
_PERCEPTION_JS = (
    "(function(){/*__OLY_PERCEPT__*/try{"
    "var de=document.documentElement,b=document.body;"
    "var vw=window.innerWidth||de.clientWidth||0;"
    "var vh=window.innerHeight||de.clientHeight||0;"
    "var pw=Math.max(de?de.scrollWidth:0,b?b.scrollWidth:0);"
    "var ph=Math.max(de?de.scrollHeight:0,b?b.scrollHeight:0);"
    "var sx=window.scrollX||de.scrollLeft||0;"
    "var sy=window.scrollY||de.scrollTop||0;"
    "var geom={vw:vw,vh:vh,pw:pw,ph:ph,sx:sx,sy:sy};"
    "var out=[],all=document.querySelectorAll('*'),seen=0;"
    "for(var i=0;i<all.length&&out.length<KMAX&&seen<WALKMAX;i++){var e=all[i];seen++;"
    "var st;try{st=window.getComputedStyle(e);}catch(x){continue;}"
    "var oy=st.overflowY,ox=st.overflowX;"
    "var dy=(oy==='auto'||oy==='scroll')?(e.scrollHeight-e.clientHeight):0;"
    "var dx=(ox==='auto'||ox==='scroll')?(e.scrollWidth-e.clientWidth):0;"
    "if(dy<=8&&dx<=8)continue;"
    "var r=e.getBoundingClientRect();if(r.width<40||r.height<40)continue;"
    "out.push({s:__olySel(e),db:Math.max(0,dy-(e.scrollTop||0)),"
    "rr:Math.max(0,dx-(e.scrollLeft||0))});}"
    "return JSON.stringify({geom:geom,scroll:out});}catch(e){return '';}})()"
).replace("KMAX", str(_SCROLLABLE_MAX)).replace("WALKMAX", str(_SCROLL_WALK_MAX))


def _click_probe_js(selector: str) -> str:
    """Resolve the element, scroll it to center, and report its click point plus
    whether that point is OBSCURED by an unrelated element (an overlay / cookie
    banner / modal intercepting the hit). Absorbs page-agent's elementFromPoint
    landing hit-test — but as a *guard*, not a silent retarget (ADR 0014 (c)).
    __OLY_CLICK__ routes it offline. Returns JSON {found,cx,cy,obstructed,tag}."""
    sel = json.dumps(selector)
    return (
        "(function(){/*__OLY_CLICK__*/var e=__olyq(" + sel + ");"
        "if(!e)return JSON.stringify({found:false});"
        "try{e.scrollIntoView({block:'center',inline:'center'});}catch(x){}"
        "var r=e.getBoundingClientRect();"
        "var cx=r.left+r.width/2,cy=r.top+r.height/2;"
        "var top=null;try{top=document.elementFromPoint(cx,cy);}catch(x){}"
        # Obscured when the topmost element at the point is neither the target,
        # an ancestor of it, nor a descendant of it (i.e. a foreign overlay), or
        # when the point isn't hittable at all (off-view / null).
        "var obstructed=!top||(top!==e&&!e.contains(top)&&!top.contains(e));"
        "return JSON.stringify({found:true,cx:Math.round(cx),cy:Math.round(cy),"
        "obstructed:obstructed,tag:(e.tagName||'').toLowerCase()});})()"
    )


def _dispatch_click_js(selector: str) -> str:
    """A faithful in-page click sequence on the INTENDED element (spec-ordered
    pointer→mouse events + focus + native click for the default action). Used
    only on the obscured/off-view fallback, so we actuate the observed control
    directly instead of firing a blind coordinate click that would land on the
    overlay. __OLY_CLICKDISPATCH__ routes it offline. Returns bool."""
    sel = json.dumps(selector)
    return (
        "(function(){/*__OLY_CLICKDISPATCH__*/var e=__olyq(" + sel + ");"
        "if(!e)return false;var r=e.getBoundingClientRect();"
        "var cx=r.left+r.width/2,cy=r.top+r.height/2;"
        "var po={bubbles:true,cancelable:true,clientX:cx,clientY:cy,pointerType:'mouse'};"
        "var mo={bubbles:true,cancelable:true,clientX:cx,clientY:cy,button:0};"
        "try{e.dispatchEvent(new PointerEvent('pointerover',po));}catch(x){}"
        "e.dispatchEvent(new MouseEvent('mouseover',mo));"
        "try{e.dispatchEvent(new PointerEvent('pointerdown',po));}catch(x){}"
        "e.dispatchEvent(new MouseEvent('mousedown',mo));"
        "try{if(e.focus)e.focus();}catch(x){}"
        "try{e.dispatchEvent(new PointerEvent('pointerup',po));}catch(x){}"
        "e.dispatchEvent(new MouseEvent('mouseup',mo));"
        "try{e.click();}catch(x){}return true;})()"
    )

# --- transport -----------------------------------------------------------


class Transport(Protocol):
    """The minimal CDP transport contract. A real transport speaks WebSocket to
    Chrome; the fake one answers in-memory. Either way the session only needs
    request/response and a close."""

    def send(self, method: str, params: dict | None = None,
             session_id: str | None = None) -> dict: ...
    def close(self) -> None: ...


class BrowserUnavailable(RuntimeError):
    """No browser is attached and none can be built from the environment."""


class TemplateStepError(RuntimeError):
    """A declarative template step failed to resolve (e.g. the site was
    redesigned and the selector no longer matches). Carries the failed step so
    the operator can attempt to self-heal by re-observing and proposing a fix."""

    def __init__(self, message: str, *, op: str = "", selector: str = "",
                 index: int = -1) -> None:
        super().__init__(message)
        self.op = op
        self.selector = selector
        self.index = index


class FakeTransport:
    """Deterministic, offline CDP stand-in for tests and headless CI.

    It records every call (so tests can assert the ledger) and answers a small
    set of CDP methods with scriptable page state. It performs no I/O — there is
    no real browser, so nothing here can navigate or act on anything.
    """

    def __init__(self, pages: dict[str, dict[str, str]] | None = None,
                 redirects: dict[str, str] | None = None,
                 present: list[str] | None = None,
                 elements: list[dict[str, str]] | None = None,
                 targets: list[dict] | None = None,
                 checkpoint: dict | None = None,
                 ax_nodes: list[dict] | None = None,
                 console: list[dict] | None = None,
                 geometry: dict | None = None,
                 scrollables: list[dict] | None = None,
                 click_obstructed: bool = False,
                 pdf_b64: str = "JVBERi0xLjQK") -> None:
        # Scriptable observe() scroll affordances. None → the eval returns ''
        # (the graceful-omit path, which is what every plain observe() test
        # exercises); set them to drive the geometry header / scrollable list.
        self.geometry = geometry
        self.scrollables = scrollables
        # Whether the click landing probe reports the click point as obscured by
        # an overlay — drives the obstructed-click fallback path in act('click').
        self.click_obstructed = click_obstructed
        # Scriptable Accessibility.getFullAXTree nodes (role/name/value/ignored).
        self.ax_nodes = ax_nodes or []
        # Console messages the page has "emitted" (level/text) — what a real
        # transport's event pump would accumulate; here it's scriptable.
        self.console = console or []
        # Deterministic base64 PDF payload for Page.printToPDF (default: a tiny
        # valid PDF header) so the capture path is exercisable offline.
        self.pdf_b64 = pdf_b64
        # url -> {"title": ..., "text": ...}
        self.pages = pages or {}
        # navigated-url -> landed-url, to simulate a server/JS redirect so the
        # post-navigation SSRF re-check can be exercised offline.
        self.redirects = redirects or {}
        # CSS selectors that "exist" on the page, so exists()/fill()/login() can
        # be driven offline. Tracks what fill() set, too.
        self.present = set(present or [])
        # Scriptable interactive elements returned by observe(); each becomes a
        # resolvable [data-olympus-idx="i"] selector so index actions work too.
        self.elements = elements or []
        for i in range(len(self.elements)):
            self.present.add(f'[data-olympus-idx="{i}"]')
        self.filled: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self._url = "about:blank"
        # Scriptable page tabs for list_tabs()/switch_tab() offline.
        self.targets = targets or []
        # An in-memory cookie jar so save_auth/restore_auth roundtrip offline.
        self.cookies: list[dict] = []
        # Dialog policy + a scriptable network-idle sequence for offline tests.
        self.dialog_accept = False
        self.dialog_text = ""
        self.inflight_seq: list[int] = []
        # Scriptable human-verification checkpoint for detect_checkpoint() offline.
        self.checkpoint: dict = checkpoint or {"type": "none", "detail": ""}
        # Scriptable cross-origin (OOPIF) frames: list of {sessionId, url}, plus
        # per-frame observe elements keyed by sessionId, so the governed
        # cross-origin observation path is exercisable offline.
        self.frames_list: list[dict] = []
        self.frame_elements: dict[str, list[dict]] = {}
        # Selectors that "exist" inside a given frame session, so frame acts
        # (click/fill/select in a cross-origin frame) resolve offline.
        self.frame_present: dict[str, set] = {}
        # A tiny valid base64 PNG so screenshot() has deterministic bytes offline.
        self.screenshot_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
            "nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII=")

    def frames(self) -> list[dict]:
        return list(self.frames_list)

    def set_dialog_policy(self, accept: bool, text: str = "") -> None:
        self.dialog_accept = bool(accept)
        self.dialog_text = text or ""

    def inflight(self) -> int:
        # Pop the scripted sequence so wait_idle() can watch it drain to zero.
        return self.inflight_seq.pop(0) if self.inflight_seq else 0

    def _matched_selector(self, expr: str) -> str | None:
        for sel in self.present:
            if json.dumps(sel) in expr:
                return sel
        return None

    def send(self, method: str, params: dict | None = None,
             session_id: str | None = None) -> dict:
        params = params or {}
        self.calls.append({"method": method, "params": params,
                           "session_id": session_id})
        # A sessionId-routed observe reads a scripted cross-origin frame's map.
        if session_id and method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "__OLY_OBSERVE__" in expr:
                els = self.frame_elements.get(session_id, [])
                lst = [{"i": i, "t": e.get("t", "button"), "n": e.get("n", ""),
                        "s": e.get("s", f'[data-olympus-idx="{i}"]')}
                       for i, e in enumerate(els)]
                return {"result": {"value": json.dumps(lst)}}
            if "__olyq" in expr:      # frame act: resolve against the frame's set
                pres = self.frame_present.get(session_id, set())
                sel = next((s for s in pres if json.dumps(s) in expr), None)
                return {"result": {"value": sel is not None}}
            # any other frame eval (e.g. document.location/title) → benign string
            return {"result": {"value": ""}}
        if method == "Page.navigate":
            target = params.get("url", self._url)
            self._url = self.redirects.get(target, target)   # follow redirect
            return {"frameId": "fake-frame"}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            page = self.pages.get(self._url, {})
            if "__OLY_CHECKPOINT__" in expr:      # detect_checkpoint()
                return {"result": {"value": json.dumps(self.checkpoint)}}
            if "__OLY_OBSERVE__" in expr:         # observe() → indexed elements
                lst = [{"i": i, "t": e.get("t", "button"), "n": e.get("n", ""),
                        "s": e.get("s", f'[data-olympus-idx="{i}"]')}
                       for i, e in enumerate(self.elements)]
                return {"result": {"value": json.dumps(lst)}}
            # observe() scroll affordances — checked BEFORE the __olyq branch
            # (the scrollables script carries __olyq via the injected deep helper).
            if "__OLY_PERCEPT__" in expr:         # geometry + scrollables, or '' → omit
                if self.geometry is None and self.scrollables is None:
                    return {"result": {"value": ""}}
                return {"result": {"value": json.dumps(
                    {"geom": self.geometry or {},
                     "scroll": self.scrollables or []})}}
            if "__OLY_CLICK__" in expr:           # click landing probe
                sel = self._matched_selector(expr)
                if sel is None:
                    return {"result": {"value": json.dumps({"found": False})}}
                return {"result": {"value": json.dumps(
                    {"found": True, "cx": 50, "cy": 35,
                     "obstructed": self.click_obstructed, "tag": "button"})}}
            if "__OLY_CLICKDISPATCH__" in expr:   # direct targeted dispatch
                return {"result": {"value": self._matched_selector(expr) is not None}}
            if "__olyq" in expr:                  # exists / fill / click / read
                sel = self._matched_selector(expr)
                if "getBoundingClientRect" in expr:   # element screenshot clip
                    box = json.dumps({"x": 10, "y": 20, "w": 80, "h": 30})
                    return {"result": {"value": box if sel else ""}}
                if params.get("returnByValue") is False:   # node handle wanted
                    return {"result": ({"objectId": f"obj-{sel}"} if sel else {})}
                if "__olySel" in expr:            # durable-selector lookup
                    value: Any = sel or ""        # offline: echo the matched css
                elif "innerText" in expr:         # selector read → text or ''
                    value = page.get("text", "") if sel else ""
                else:                             # predicate / mutator → bool
                    if sel and "value=" in expr:
                        self.filled[sel] = "set"
                    value = sel is not None
                return {"result": {"value": value}}
            if "scrollWidth" in expr:      # full-page screenshot dimensions
                return {"result": {"value": json.dumps(
                    {"x": 0, "y": 0, "w": 1200, "h": 3000})}}
            if "performance" in expr:      # wait_idle() → stable resource count
                value = "0"
            elif "readyState" in expr:
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
        if method == "Page.captureScreenshot":
            # A deterministic 1x1 PNG so the screenshot path is exercisable
            # offline without a real browser.
            return {"data": self.screenshot_b64}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": list(self.ax_nodes)}
        if method == "Page.printToPDF":
            return {"data": self.pdf_b64}
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        if method == "Target.activateTarget":
            tid = params.get("targetId")
            for t in self.targets:            # reflect the switch in the URL
                if t.get("targetId") == tid and t.get("url"):
                    self._url = t["url"]
            return {}
        if method in ("Network.getCookies", "Storage.getCookies"):
            return {"cookies": list(self.cookies)}
        if method in ("Network.setCookies", "Storage.setCookies"):
            self.cookies = list(params.get("cookies", []) or [])
            return {}
        if method in ("DOM.setFileInputFiles", "Page.setDownloadBehavior"):
            return {}
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

    A single background reader thread demultiplexes the socket: id-matched
    replies wake their waiting `send()`, while unsolicited CDP *events* are
    routed to handlers. That is what lets the harness (a) auto-handle JS dialogs
    so `confirm()`/`alert()` can't wedge a click, and (b) track in-flight network
    requests for a real network-idle wait — neither possible with a purely
    request/response loop.
    """

    def __init__(self, ws_url: str, http_base: str = "") -> None:  # pragma: no cover - needs a browser
        try:
            from websockets.sync.client import connect  # type: ignore
        except Exception as err:  # pragma: no cover - optional dependency
            raise BrowserUnavailable(
                "real browser attach needs the optional 'websockets' package: "
                "pip install websockets") from err
        self._connect = connect
        self._ws_url = ws_url                    # remembered for auto-reconnect
        # Bound a single CDP message so a hostile/huge page can't OOM us; we
        # truncate text to _TEXT_LIMIT anyway.
        self._conn = connect(ws_url, open_timeout=10, max_size=_WS_MAX_FRAME)
        self._id = 0
        self._reconnect_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._pending: dict[int, list] = {}     # id -> [Event, message]
        self._pending_lock = threading.Lock()
        self._http_base = (http_base or "").rstrip("/")
        # Console/log messages accumulated from CDP events (bounded). Populated
        # once Runtime/Log are enabled; drained by console_logs().
        self.console: deque[dict] = deque(maxlen=_CONSOLE_MAX)
        # Dialog policy: SAFE DEFAULT is to DISMISS (never auto-confirm an
        # irreversible prompt); the operator opts into accept for a known flow.
        self._dialog_accept = False
        self._dialog_text = ""
        self._inflight = 0                       # open network requests (idle wait)
        self._frames: dict[str, dict] = {}       # sessionId -> {url} for OOPIFs
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._arm()

    def _arm(self) -> None:  # pragma: no cover - needs a browser
        """Enable the domains whose events we consume (Page for dialogs, Network
        for in-flight tracking) and auto-attach to child frames so cross-origin
        (out-of-process) iframes surface with their own sessionId. Best-effort."""
        for dom in ("Page.enable", "Network.enable", "Runtime.enable",
                    "Log.enable"):
            try:
                self.send(dom)
            except Exception:
                pass
        try:
            self.send("Target.setAutoAttach", {"autoAttach": True,
                      "waitForDebuggerOnStart": False, "flatten": True})
        except Exception:
            pass

    def frames(self) -> list[dict]:  # pragma: no cover - needs a browser
        return [{"sessionId": sid, "url": info.get("url", "")}
                for sid, info in self._frames.items()]

    def set_dialog_policy(self, accept: bool, text: str = "") -> None:  # pragma: no cover
        self._dialog_accept = bool(accept)
        self._dialog_text = text or ""

    def inflight(self) -> int:  # pragma: no cover - needs a browser
        return max(0, self._inflight)

    def _read_loop(self) -> None:  # pragma: no cover - needs a browser
        while not self._closed:
            try:
                raw = self._conn.recv(timeout=1.0)
            except TimeoutError:
                continue
            except Exception:
                if self._closed:
                    return
                time.sleep(0.05)
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None:
                with self._pending_lock:
                    slot = self._pending.get(mid)
                if slot is not None:
                    slot[1] = msg
                    slot[0].set()
            else:
                self._on_event(msg)

    def _on_event(self, msg: dict) -> None:  # pragma: no cover - needs a browser
        method = msg.get("method", "")
        if method == "Page.javascriptDialogOpening":
            # Answer per policy WITHOUT waiting (we're on the reader thread;
            # blocking here would deadlock the very reply we'd wait for).
            self._send_nowait("Page.handleJavaScriptDialog",
                              {"accept": self._dialog_accept,
                               "promptText": self._dialog_text})
        elif method == "Network.requestWillBeSent":
            self._inflight += 1
        elif method in ("Network.loadingFinished", "Network.loadingFailed"):
            self._inflight = max(0, self._inflight - 1)
        elif method == "Target.attachedToTarget":
            p = msg.get("params", {})
            info = p.get("targetInfo", {})
            sid = p.get("sessionId")
            if sid and info.get("type") == "iframe":   # a cross-origin (OOPIF)
                self._frames[sid] = {"url": info.get("url", ""),
                                     "targetId": info.get("targetId", "")}
        elif method == "Target.detachedFromTarget":
            self._frames.pop(msg.get("params", {}).get("sessionId", ""), None)
        elif method == "Runtime.consoleAPICalled":
            p = msg.get("params", {}) or {}
            text = " ".join(str(a.get("value", a.get("description", "")))
                            for a in (p.get("args", []) or []))
            self.console.append({"level": p.get("type", "log"), "text": text})
        elif method == "Log.entryAdded":
            e = (msg.get("params", {}) or {}).get("entry", {}) or {}
            self.console.append({"level": e.get("level", "log"),
                                 "text": str(e.get("text", ""))})
        elif method == "Runtime.exceptionThrown":
            d = (msg.get("params", {}) or {}).get("exceptionDetails", {}) or {}
            self.console.append({"level": "error",
                                 "text": str(d.get("text", "exception"))})

    def _send_nowait(self, method: str, params: dict | None = None) -> None:  # pragma: no cover
        with self._id_lock:
            self._id += 1
            mid = self._id
        try:
            self._conn.send(json.dumps(
                {"id": mid, "method": method, "params": params or {}}))
        except Exception:
            pass

    def reattach(self, target_id: str) -> bool:  # pragma: no cover - needs a browser
        """Re-bind this transport to a different page target's WebSocket, so the
        session actually drives the tab it switched to (not just activates it)."""
        if not self._http_base:
            return False
        import urllib.request
        try:
            with urllib.request.urlopen(self._http_base + "/json", timeout=10) as r:
                targets = json.loads(r.read().decode("utf-8"))
        except Exception:
            return False
        ws = next((t.get("webSocketDebuggerUrl") for t in targets
                   if t.get("id") == target_id and t.get("webSocketDebuggerUrl")),
                  "")
        if not ws:
            return False
        self._closed = True                      # stop the old reader
        try:
            self._conn.close()
        except Exception:
            pass
        self._reader.join(timeout=2.0)
        with self._pending_lock:
            self._pending.clear()
        self._conn = self._connect(ws, open_timeout=10, max_size=_WS_MAX_FRAME)
        self._id = 0
        self._inflight = 0
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._arm()
        return True

    def _reconnect(self) -> bool:  # pragma: no cover - needs a browser
        """Re-open the WebSocket to the SAME target after a drop, so a transient
        disconnect doesn't wedge the whole harness. Serialized so concurrent
        callers reconnect once, not N times."""
        with self._reconnect_lock:
            # Another thread may have already reconnected while we waited.
            try:
                self._conn.ping()                # cheap liveness probe
                return True
            except Exception:
                pass
            self._closed = True
            try:
                self._conn.close()
            except Exception:
                pass
            self._reader.join(timeout=2.0)
            with self._pending_lock:
                self._pending.clear()
            try:
                self._conn = self._connect(self._ws_url, open_timeout=10,
                                           max_size=_WS_MAX_FRAME)
            except Exception:
                return False
            self._id = 0
            self._inflight = 0
            self._closed = False
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            self._arm()
            return True

    def send(self, method: str, params: dict | None = None,
             session_id: str | None = None) -> dict:  # pragma: no cover
        try:
            return self._send_once(method, params, session_id)
        except (ConnectionError, OSError) as err:
            # A dropped socket: reconnect once and retry. A genuine CDP error
            # (RuntimeError) is NOT retried — only transport-level failures.
            if self._closed or not self._reconnect():
                raise RuntimeError(f"CDP transport lost: {err}") from err
            return self._send_once(method, params, session_id)

    def _send_once(self, method: str, params: dict | None = None,
                   session_id: str | None = None) -> dict:  # pragma: no cover
        with self._id_lock:
            self._id += 1
            mid = self._id
        ev = threading.Event()
        slot = [ev, None]
        with self._pending_lock:
            self._pending[mid] = slot
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:                           # route into a child frame session
            msg["sessionId"] = session_id
        try:
            self._conn.send(json.dumps(msg))
        except Exception as err:
            with self._pending_lock:
                self._pending.pop(mid, None)
            raise ConnectionError(f"CDP send failed: {err}") from err
        if not ev.wait(timeout=_RECV_TIMEOUT):
            with self._pending_lock:
                self._pending.pop(mid, None)
            raise TimeoutError(f"CDP call {method} timed out")
        with self._pending_lock:
            self._pending.pop(mid, None)
        reply = slot[1] or {}
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", "CDP error"))
        return reply.get("result", {})

    def close(self) -> None:  # pragma: no cover
        self._closed = True
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


def launch_local(headless: bool = False, timeout: float | None = None) -> str:
    """Launch a local Chrome with remote debugging and return its DevTools HTTP
    base. Reuses an already-launched one. Best-effort: raises BrowserUnavailable
    if no binary is found or DevTools never comes up.

    The startup wait defaults to 45s (cold/loaded CI runners can be slow to bring
    up the DevTools endpoint) and is tunable via OLYMPUS_BROWSER_LAUNCH_TIMEOUT.
    If the Chrome process dies during the wait we fail FAST with its exit code and
    a tail of its stderr, instead of blocking the full timeout on a blind poll —
    so a real launch failure (missing --no-sandbox, missing libs) is diagnosable,
    not an opaque "never responded"."""
    global _launched
    if _launched is not None and _launched[0].poll() is None:
        return _launched[1]
    import os
    import subprocess
    import tempfile
    import time as _time
    import urllib.request
    if timeout is None:
        try:
            timeout = float(os.environ.get("OLYMPUS_BROWSER_LAUNCH_TIMEOUT", "45"))
        except (TypeError, ValueError):
            timeout = 45.0
    binary = _find_chrome()
    if not binary:
        raise BrowserUnavailable(
            "no Chrome/Chromium found to launch — install Chrome or set "
            "OLYMPUS_BROWSER_BIN")
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="olympus-browser-")
    # Capture Chrome's stderr so a startup crash is diagnosable (delete=False so
    # it survives the Popen; small, and left behind like the profile dir).
    err_log = tempfile.NamedTemporaryFile(
        prefix="olympus-browser-", suffix=".log", delete=False)
    proc = subprocess.Popen(_chrome_args(binary, port, profile, headless),
                            stdout=subprocess.DEVNULL, stderr=err_log)
    base = f"http://127.0.0.1:{port}"
    deadline = _time.monotonic() + timeout
    crashed = False
    while _time.monotonic() < deadline:
        if proc.poll() is not None:        # Chrome exited — stop waiting the full timeout
            crashed = True
            break
        try:
            with urllib.request.urlopen(base + "/json/version", timeout=2) as r:
                if r.status == 200:
                    _launched = (proc, base)
                    err_log.close()
                    return base
        except Exception:
            _time.sleep(0.2)
    if not crashed:
        proc.terminate()
    detail = ""
    try:
        err_log.flush()
        err_log.close()
        with open(err_log.name, "r", errors="replace") as fh:
            tail = fh.read()[-400:].strip()
        if tail:
            detail = f" — chrome stderr: {tail}"
    except Exception:
        pass
    code = proc.poll()
    reason = (f"process exited with code {code}" if code is not None
              else f"process alive but DevTools silent after {timeout:.0f}s")
    raise BrowserUnavailable(
        f"launched Chrome but its DevTools never responded ({reason}){detail}")


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
        # Keep the DevTools HTTP base (when the endpoint is one) so the transport
        # can reattach to another tab on switch_tab().
        http_base = endpoint if endpoint.startswith(("http://", "https://")) else ""
        return _RealTransport(_resolve_page_ws(endpoint), http_base=http_base)
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
        # The evolution substrate: a bounded, first-party journal of the act
        # steps that actually landed this session (NOT page prose, and never the
        # typed text — credentials must not enter the skill store). learn_steps()
        # crystallizes it into a reliability-scored skill that Olympus refines
        # over time. _labels remembers each observed index's label so a learned
        # step reads "click \"Sign in\"", not a raw selector.
        self.journal: list[str] = []
        self._labels: dict[int, str] = {}
        # The structured twin of the journal: {op, selector, value?} steps with
        # DURABLE selectors (never the typed text). Only the promotable verbs
        # (click, fill) are captured — enough to graduate a proven flow into a
        # declarative template that still resolves on a fresh page load.
        self.recipe: list[dict] = []
        # Whether the sub-resource egress gate has been installed on this
        # target yet (Network.setBlockedURLs persists once set, so apply once).
        self._subres_gated: bool = False
        # Perception-delta baseline: the durable selectors seen by the PREVIOUS
        # observe(), plus the URL they were seen on. Lets observe() flag elements
        # that are NEW since the last look (a *[i] marker) — the strongest signal
        # that an input/click revealed a suggestion list, dialog, or step — but
        # only when the URL is unchanged (a navigation replaces the whole set, so
        # "new" would be meaningless noise). Absorbed from page-agent's `*[`
        # marking; see docs/PAGE_AGENT_TRACKING.md §3.2 / ADR 0014 (b).
        self._prev_observe_sels: set[str] = set()
        self._prev_observe_url: str = ""
        # Last URL resolved by _blocked_landing() — reused by observe() for the
        # delta baseline instead of a second _current_url() eval (one fewer CDP
        # round-trip, and no TOCTOU between the landing check and the delta URL).
        self._landed_url: str = ""

    def _apply_subresource_gate(self) -> None:
        """Install the network-layer block on sub-resource requests to SSRF
        targets (see security.subresource_block_patterns). Best-effort and
        idempotent: a transport without the Network domain simply leaves the
        navigation gate as the hard guarantee, so this never breaks browsing.
        Recorded in the ledger like every other CDP call, so the enforcement
        is auditable."""
        if self._subres_gated:
            return
        self._subres_gated = True                # set first: never retry-loop
        try:
            self._call("Network.enable")
            self._call("Network.setBlockedURLs",
                       urls=security.subresource_block_patterns())
            # Enable console/log capture so console_logs() has real signal on a
            # live browser (best-effort; the fake transport ignores these).
            self._call("Runtime.enable")
            self._call("Log.enable")
        except Exception:
            self._subres_gated = False           # allow a later retry on reopen

    def _journal_step(self, step: str) -> None:
        """Append a landed, replayable step (bounded; oldest dropped)."""
        self.journal.append(step)
        if len(self.journal) > _JOURNAL_MAX:
            del self.journal[0]

    def _durable_selector(self, css: str) -> str:
        """Ask the page for a reload-stable selector of the element `css`
        resolves to (id / [name] / [aria-label] / nth-of-type). Falls back to
        the input selector if the element is gone or unnamed."""
        if not css:
            return ""
        out = self._eval(f"(function(){{var e=__olyq({json.dumps(css)});"
                         f"return e?__olySel(e):'';}})()")
        return out or css

    def _recipe_step(self, op: str, css: str, value: str | None = None) -> None:
        """Capture a promotable step with a durable selector (bounded)."""
        entry: dict = {"op": op, "selector": self._durable_selector(css)}
        if value is not None:
            entry["value"] = value
        self.recipe.append(entry)
        if len(self.recipe) > _JOURNAL_MAX:
            del self.recipe[0]

    def _call(self, method: str, *, _session_id: str | None = None,
              **params: Any) -> dict:
        self.ledger.append({"method": method, "params": params})
        return self._t.send(method, params, session_id=_session_id)

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
        self._landed_url = href or ""      # reused by observe()'s delta baseline
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

    def wait_idle(self, quiet: float = 0.5, timeout: float = _LOAD_TIMEOUT) -> None:
        """Wait for the page to load AND the network to go idle for a short quiet
        window. When the transport tracks in-flight requests from the CDP event
        stream (real browser), 'idle' means ZERO open requests — a true
        network-idle. Otherwise it falls back to a resource-count-stable
        heuristic. Bounded by `timeout`; best-effort (never raises)."""
        self._wait_ready(timeout)
        inflight = getattr(self._t, "inflight", None)
        deadline = time.monotonic() + timeout
        last, stable_since = -1, None
        while time.monotonic() < deadline:
            try:
                if inflight is not None:            # true idle: zero open requests
                    n = int(inflight())
                    now = time.monotonic()
                    if n == 0:
                        if stable_since is None:
                            stable_since = now
                        elif now - stable_since >= quiet:
                            return
                    else:
                        stable_since = None
                else:                               # heuristic: resource count stable
                    n = int(float(self._eval(
                        "(performance.getEntriesByType('resource')||[]).length")
                        or 0))
                    now = time.monotonic()
                    if n == last:
                        if stable_since is None:
                            stable_since = now
                        elif now - stable_since >= quiet:
                            return
                    else:
                        last, stable_since = n, None
            except Exception:
                return
            time.sleep(0.1)

    def set_dialog_policy(self, accept: bool, text: str = "") -> str:
        """Decide how native JS dialogs (alert/confirm/prompt) are answered, so
        they can't wedge a click. SAFE DEFAULT is dismiss (accept=False) — an
        irreversible confirm is never auto-accepted; the operator opts into
        accept for a known flow, optionally supplying prompt text."""
        setter = getattr(self._t, "set_dialog_policy", None)
        if setter is not None:
            setter(bool(accept), text or "")
        verb = "accept" if accept else "dismiss"
        extra = f" (answering prompts with {text!r})" if accept and text else ""
        return f"Dialogs will now {verb} automatically{extra}."

    def wait_for(self, selector: str, gone: bool = False,
                 timeout: float = _LOAD_TIMEOUT) -> bool:
        """Wait until `selector` appears (or, with gone=True, disappears) — the
        deterministic wait a dynamic UI needs between steps, instead of guessing
        with a fixed sleep. Bounded by `timeout`; returns whether the condition
        was met. Resolves deep (shadow/iframe) via exists()/__olyq."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.exists(selector) != gone:
                return True
            time.sleep(0.1)
        return self.exists(selector) != gone

    def list_tabs(self) -> list[dict]:
        """List the browser's open page tabs as bounded {id, title, url} dicts.
        Reveals the (possibly credentialed) browser's tabs, so the tool
        (browser_tabs) is operator-gated and capability-separated."""
        res = self._call("Target.getTargets")
        out = []
        for t in (res or {}).get("targetInfos", []) or []:
            if t.get("type") != "page":
                continue
            out.append({"id": str(t.get("targetId", "")),
                        "title": str(t.get("title", ""))[:_LABEL_MAX],
                        "url": str(t.get("url", ""))[:200]})
            if len(out) >= 50:
                break
        return out

    def switch_tab(self, index: int) -> bool:
        """Activate the page tab at `index` (from list_tabs). After switching,
        act()/observe() re-check the NEW current page's domain, so switching can
        never point the actuator at an unauthorized site without a fresh check."""
        tabs = self.list_tabs()
        if not (0 <= index < len(tabs)):
            return False
        target_id = tabs[index]["id"]
        self._call("Target.activateTarget", targetId=target_id)
        # Actually drive the switched-to tab: re-bind the transport to its socket
        # when it supports it (real browser). Offline the fake reflects the URL.
        reattach = getattr(self._t, "reattach", None)
        if reattach is not None:
            reattach(target_id)
        self.url = tabs[index]["url"]
        return True

    def upload(self, selector: str, path: str) -> str:
        """Attach a WORKSPACE-CONFINED file to a file input. Uploading a local
        file to a remote site is data egress, so this is a credentialed actuator
        (operator-gated) and the path can never escape the confined workspace."""
        from . import sandbox
        try:
            target = sandbox._confine(path)
        except ValueError as err:
            return f"Error: {err}"
        if not target.is_file():
            return f"Error: no such file in workspace: {path}"
        # DOM.setFileInputFiles operates on a node HANDLE, not a selector, so
        # resolve the input to a CDP objectId first (deep — works inside shadow
        # roots / same-origin iframes). Confinement + gating are enforced above.
        object_id = self._resolve_object_id(selector)
        if not object_id:
            return f"Error: no file input for {selector}."
        self._call("DOM.setFileInputFiles",
                   files=[str(target)], objectId=object_id)
        return f"Uploaded {target.name} to {selector}."

    def download(self, trigger_selector: str = "", timeout: float = 30.0) -> str:
        """Capture a browser download into the confined workspace: point downloads
        at the workspace, (optionally) click `trigger_selector` to start one, then
        wait for a NEW, complete file to appear (ignoring Chrome's `.crdownload`
        temp and requiring a stable size). Returns the captured filename. The file
        is untrusted external content — read it afterwards via read_file /
        analyze_image (path-confined), never surfaced here as page prose."""
        from . import sandbox
        root = sandbox.workdir()
        self.set_download_dir()                  # confine downloads to the workspace
        try:
            before = set(os.listdir(root))
        except OSError as err:
            return f"Error: workspace unavailable ({err})."
        if trigger_selector:
            out = self.act("click", selector=trigger_selector)
            if out.startswith("Error:"):
                return out
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                fresh = [f for f in set(os.listdir(root)) - before
                         if not f.endswith(".crdownload")]
            except OSError:
                fresh = []
            for name in fresh:
                p = root / name
                try:
                    size = p.stat().st_size
                    time.sleep(0.3)
                    if p.exists() and p.stat().st_size == size:   # settled
                        return (f"Downloaded {name} to the workspace "
                                f"({p.stat().st_size} bytes). Read it with "
                                "read_file or analyze_image.")
                except OSError:
                    continue
            time.sleep(0.3)
        return "Error: no download completed within the timeout."

    def get_cookies(self, domain: str = "") -> list[dict]:
        """Read the browser's cookies (CDP Storage.getCookies — all cookies, not
        just the current frame's), optionally filtered to `domain`. Cookies are
        session credentials — the caller (operator.save_auth) stores them ONLY in
        the encrypted vault and only for an authorized domain, never surfaced to
        the model."""
        res = self._call("Storage.getCookies")
        cookies = (res or {}).get("cookies", []) or []
        d = (domain or "").strip().lower().lstrip(".")
        if not d:
            return list(cookies)
        out = []
        for c in cookies:
            cd = str(c.get("domain", "")).lower().lstrip(".")
            if cd == d or cd.endswith("." + d) or d.endswith("." + cd):
                out.append(c)
        return out

    def set_cookies(self, cookies: list[dict]) -> int:
        """Inject cookies (CDP Network.setCookies) to restore a saved session, so
        the operator need not re-login. Returns how many were set."""
        clean = [c for c in (cookies or []) if isinstance(c, dict) and c.get("name")]
        if clean:
            self._call("Network.setCookies", cookies=clean)
        return len(clean)

    def set_download_dir(self, path: str = "") -> str:
        """Confine any browser download to the workspace (default: its root), so
        a site can't drop a file outside the sandbox. Best-effort."""
        from . import sandbox
        try:
            target = sandbox._confine(path or ".")
        except ValueError as err:
            return f"Error: {err}"
        self._call("Page.setDownloadBehavior", behavior="allow",
                   downloadPath=str(target))
        return str(target)

    def open(self, url: str) -> str:
        """Navigate to `url` through the SSRF + egress gate, return a snapshot."""
        reason = security.url_block_reason(url)
        if reason:
            return f"Error: {reason}."
        # Install the sub-resource network gate BEFORE the page can issue its
        # own requests, so a hostile/injected page's beacons to internal hosts
        # are blocked at the network layer, not just the navigation.
        self._apply_subresource_gate()
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
            expr = (f"(function(){{var e=__olyq("
                    f"{json.dumps(selector)});return e?e.innerText:'';}})()")
        else:
            expr = "document.body ? document.body.innerText : ''"
        text = self._eval(expr)
        # Redact secrets from page text before it can reach a model prompt
        # (default-on; ADR 0014 (d)). Defense-in-depth: the tool layer also
        # redacts via wrap_untrusted, but the raw method must never emit a secret.
        return security.sanitize_for_prompt((text or "")[:_TEXT_LIMIT])

    def html(self) -> str:
        """The current page's serialized HTML (post-render / post-action), for a
        structured scrape of a JS-driven page. Untrusted external content, same
        as read(); SSRF landing is re-checked. Byte-capped."""
        self.ingested_untrusted = True
        if self._blocked_landing():
            return ""
        raw = (self._eval("document.documentElement.outerHTML") or "")[:_HTML_LIMIT]
        return security.sanitize_for_prompt(raw)

    def read_ax(self, limit: int = 0) -> str:
        """Perceive the page through its ACCESSIBILITY TREE (CDP
        Accessibility.getFullAXTree) — role + accessible name for every
        meaningful node. This is the most redesign-resilient way to read a
        page: the AX tree is the semantic contract a site exposes to assistive
        tech, so it survives CSS/DOM churn that breaks selectors and pixel
        coordinates, and it is far cheaper than a screenshot. Untrusted, like
        every read (blocked-landing guard; bounded output; the tool layer
        wraps it). Falls back with a clear message if the browser doesn't
        expose the Accessibility domain."""
        self.ingested_untrusted = True
        if self._blocked_landing():
            return "Error: current page is a blocked address."
        cap = max(1, min(int(limit) or _AX_MAX, _AX_MAX))
        try:
            self._call("Accessibility.enable")
            res = self._call("Accessibility.getFullAXTree")
        except Exception as err:
            return f"Error: accessibility tree unavailable ({str(err)[:120]})."
        nodes = (res or {}).get("nodes", []) or []
        lines, seen = [], 0
        for n in nodes:
            if not isinstance(n, dict) or n.get("ignored"):
                continue
            role = ((n.get("role") or {}).get("value") or "").strip()
            name = ((n.get("name") or {}).get("value") or "").strip()
            if not role or role in ("none", "generic", "InlineTextBox") or not name:
                continue
            val = ((n.get("value") or {}).get("value") or "")
            label = f"{role}: {name[:_LABEL_MAX]}"
            if val:
                label += f" = {str(val)[:_LABEL_MAX]}"
            lines.append(label)
            seen += 1
            if seen >= cap:
                break
        if not lines:
            return "(no labelled accessibility nodes on this page)"
        return security.sanitize_for_prompt("\n".join(lines)[:_TEXT_LIMIT])

    def screenshot(self, selector: str = "", full_page: bool = False) -> str:
        """Capture the current page as a base64 PNG (CDP Page.captureScreenshot),
        for visual perception of canvas/image-heavy pages that observe() can't
        map. With `selector`, captures just that element's box; with `full_page`,
        the whole scrollable page (beyond the viewport); otherwise the viewport.
        Refuses a blocked landing (never captures internal content). Returns
        base64 data, or "" if nothing was captured. The pixels are untrusted
        external content — the caller (browser_screenshot) is an INGESTION tool
        and its description is wrapped, exactly like browser_read."""
        self.ingested_untrusted = True
        if self._blocked_landing():
            return ""
        params: dict[str, Any] = {"format": "png"}
        if selector:
            box = self._eval(
                f"(function(){{var e=__olyq({json.dumps(selector)});if(!e)"
                "return '';var r=e.getBoundingClientRect();return JSON.stringify("
                "{x:r.left+window.scrollX,y:r.top+window.scrollY,"
                "w:r.width,h:r.height});})()")
            clip = self._clip_from(box)
            if clip is None:
                return ""
            params["clip"] = clip
            params["captureBeyondViewport"] = True
        elif full_page:
            dim = self._eval(
                "JSON.stringify({x:0,y:0,"
                "w:Math.max(document.body?document.body.scrollWidth:0,"
                "document.documentElement.scrollWidth),"
                "h:Math.max(document.body?document.body.scrollHeight:0,"
                "document.documentElement.scrollHeight)})")
            clip = self._clip_from(dim)
            if clip is not None:
                params["clip"] = clip
                params["captureBeyondViewport"] = True
        res = self._call("Page.captureScreenshot", **params)
        return str((res or {}).get("data", "") or "")

    def save_pdf(self, name: str = "") -> str:
        """Print the current page to a PDF in the confined workspace (CDP
        Page.printToPDF) — a durable, verifiable artifact of what was on screen
        (a confirmation page, a receipt, a submitted form). Feeds the
        evidence-based verification loop: 'the operator did X' becomes a file
        an approver — or the goal judge — can open. Refuses a blocked landing
        (never archives internal content) and confines the path to the
        workspace, like every other file write."""
        import base64

        from . import sandbox
        self.ingested_untrusted = True
        if self._blocked_landing():
            return "Error: current page is a blocked address."
        fname = name or f"page-{int(time.time())}.pdf"
        if not fname.lower().endswith(".pdf"):
            fname += ".pdf"
        try:
            res = self._call("Page.printToPDF", printBackground=True)
        except Exception as err:
            return f"Error: PDF capture unavailable ({str(err)[:120]})."
        data = (res or {}).get("data", "")
        if not data:
            return "Error: the page produced no PDF."
        try:
            raw = base64.b64decode(data)
            written = sandbox.write_file(fname, "")     # confine + create path
            with open(written["path"], "wb") as f:
                f.write(raw)
        except Exception as err:
            return f"Error: could not save the PDF ({str(err)[:120]})."
        return f"Saved page PDF to workspace: {fname} ({len(raw)} bytes)."

    def console_logs(self, limit: int = 0) -> str:
        """Return the console messages the page has emitted this session
        (errors, warnings, logs) — real debugging signal for the coding
        specialist, and evidence of a page's behaviour. The buffer is drained
        by the transport's event pump when the real Console/Log domain is
        enabled; a transport without it returns an empty, honest result rather
        than failing. Untrusted: a page controls its own console output."""
        cap = max(1, min(int(limit) or _CONSOLE_MAX, _CONSOLE_MAX))
        logs = list(getattr(self._t, "console", []) or [])[-cap:]
        if not logs:
            return "(no console messages captured)"
        out = []
        for m in logs:
            level = str(m.get("level", "log"))[:12]
            text = str(m.get("text", ""))[:_LABEL_MAX * 4]
            out.append(f"[{level}] {text}")
        return security.sanitize_for_prompt("\n".join(out)[:_TEXT_LIMIT])

    @staticmethod
    def _clip_from(raw: str) -> dict | None:
        """Parse a {x,y,w,h} JSON box into a CDP clip, or None if unusable."""
        try:
            d = json.loads(raw or "")
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(d, dict) or (d.get("w", 0) < 1 or d.get("h", 0) < 1):
            return None
        return {"x": d.get("x", 0), "y": d.get("y", 0),
                "width": d["w"], "height": d["h"], "scale": 1}

    def observe(self, limit: int = _OBSERVE_MAX) -> str:
        """Perceive the page as a numbered map of its visible interactive
        elements — the harness's 'look' step. Each element gets a stable index
        (stamped as data-olympus-idx) that `act(..., index=N)` resolves, so the
        model can drive an *arbitrary* page by index instead of guessing CSS
        selectors. Because it maps a possibly credentialed tab, the tool
        (`browser_observe`) is governed as an actuator: kept out of any
        prose-ingesting run and gated to authorized domains. Labels are
        page-controlled (untrusted) and hard-capped, so a map row can carry no
        more than a short, bounded string — never a paragraph of instructions."""
        items = self._observe_raw(limit)
        if items is None:
            return "(could not read the page's interactive elements)"

        # Perception delta: which durable selectors are NEW since the last
        # observe() on this same URL. On a fresh page (URL changed or first look)
        # nothing is marked — a navigation replaces the whole set, so "new" would
        # be noise. Marked elements get a `*` prefix (page-agent's `*[` idiom).
        # Reuse the URL _observe_raw() already resolved via _blocked_landing()
        # (no extra eval, and no TOCTOU between the landing check and the delta).
        cur_url = self._landed_url
        cur_sels = {str(it.get("s") or "") for it in items if it.get("s")}
        same_page = bool(self._prev_observe_url) and cur_url == self._prev_observe_url
        new_sels = (cur_sels - self._prev_observe_sels) if same_page else set()
        self._prev_observe_sels = cur_sels
        self._prev_observe_url = cur_url

        lines = []
        for it in items:
            # observe() is an ACTION tool (browser_observe) and is NOT wrapped by
            # the ingestion path, so redact secrets in the label HERE — a token
            # sitting in an element's text/value is never a useful control
            # identifier, and this closes the one path a page secret could reach
            # the model unredacted (ADR 0014 (d) hardening).
            name = security.sanitize_for_prompt(
                str(it.get("n") or "").strip())[:_LABEL_MAX]
            label = f' "{name}"' if name else ""
            sel = str(it.get("s") or "")
            mark = "*" if (sel and sel in new_sels) else ""
            lines.append(f"{mark}[{it.get('i')}] {it.get('t')}{label}")
        body = "\n".join(lines) or "(no interactive elements found)"

        # Scroll affordances: a geometry header (how much is off-screen) and any
        # scrollable containers, so the model scrolls the right region instead of
        # guessing. ONE eval measures both atomically; both fail soft — omitted
        # entirely if the page can't report them — so perception never regresses
        # to worse than the bare map.
        geom, scroll = self._perception()
        header = self._geometry_line(geom)
        footer = self._scrollables_block(scroll)
        parts = [p for p in (header, body, footer) if p]
        return "\n".join(parts)

    def _perception(self) -> tuple[dict, list]:
        """One eval returning (geometry dict, scrollables list) measured at the
        same instant. ({}, []) on any read failure (affordances simply omitted)."""
        try:
            data = json.loads(self._eval(_PERCEPTION_JS) or "")
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}, []
        if not isinstance(data, dict):
            return {}, []
        geom = data.get("geom") if isinstance(data.get("geom"), dict) else {}
        scroll = data.get("scroll") if isinstance(data.get("scroll"), list) else []
        return geom, scroll

    def _geometry_line(self, geo: dict) -> str:
        """One-line page geometry for observe(): viewport, page size, and how much
        is above/below the fold. '' when geometry is unavailable (header omitted)."""
        if not isinstance(geo, dict) or not geo:
            return ""
        try:
            vw, vh = int(geo["vw"]), int(geo["vh"])
            pw, ph = int(geo["pw"]), int(geo["ph"])
            sy = int(geo.get("sy", 0))
        except (KeyError, TypeError, ValueError):
            return ""
        if vw <= 0 or vh <= 0:
            return ""
        max_scroll = max(0, ph - vh)
        if max_scroll <= 4:
            return f"Page {pw}x{ph}, viewport {vw}x{vh} (fits in viewport)"
        above = max(0, min(sy, max_scroll))
        below = max(0, max_scroll - above)
        pct = round(100 * above / max_scroll) if max_scroll else 0
        return (f"Page {pw}x{ph}, viewport {vw}x{vh} at {pct}% "
                f"({above}px above, {below}px below - scroll to see more)")

    def _scrollables_block(self, regions: list) -> str:
        """A short list of scrollable containers (durable selector + remaining
        px), so the model can scroll a specific region. '' when there are none."""
        if not isinstance(regions, list) or not regions:
            return ""
        rows = []
        for r in regions[:_SCROLLABLE_MAX]:
            if not isinstance(r, dict):
                continue
            sel = str(r.get("s") or "").strip()
            if not sel:
                continue
            try:
                db, rr = int(r.get("db", 0)), int(r.get("rr", 0))
            except (TypeError, ValueError):
                db, rr = 0, 0
            dirs = []
            if db > 8:
                dirs.append(f"{db}px below")
            if rr > 8:
                dirs.append(f"{rr}px right")
            if not dirs:
                continue
            rows.append(f'- "{sel}" ({", ".join(dirs)})')
        if not rows:
            return ""
        head = "Scrollable regions (act: scroll with this selector):"
        return "\n".join([head, *rows])

    def detect_checkpoint(self) -> dict:
        """Detect a human-verification checkpoint on the current page — the moat's
        'detect, don't defeat' step. Returns {"type", "detail"} where type is one
        of captcha / otp / step_up / none. It NEVER tries to solve or bypass the
        check and returns only a bounded enum (no page prose), so it is a
        structured predicate, not an ingestion or a defeat attempt. On a blocked
        landing it reports none rather than reading the page."""
        if self._blocked_landing():
            return {"type": "none", "detail": ""}
        try:
            raw = self._eval(_CHECKPOINT_JS)
            d = json.loads(raw or "")
        except (json.JSONDecodeError, TypeError, Exception):
            return {"type": "none", "detail": ""}
        kind = d.get("type") if isinstance(d, dict) else None
        if kind not in _CHECKPOINT_KINDS:
            return {"type": "none", "detail": ""}
        return {"type": kind, "detail": str(d.get("detail", ""))[:40]}

    def list_frames(self) -> list[dict]:
        """The cross-origin (out-of-process) iframes attached to this page, each
        as {sessionId, url, origin}. Same-origin frames are already reachable by
        the deep walk; these are the ones behind the origin boundary. The tool
        layer annotates each with whether its origin is an AUTHORIZED operator
        site — the governed-crossing gate."""
        fn = getattr(self._t, "frames", None)
        if fn is None:
            return []
        out = []
        for f in fn() or []:
            sid = f.get("sessionId", "")
            url = f.get("url", "")
            if not url and sid:      # attach-time url is often stale/empty — ask
                try:                 # the frame itself for its live location
                    url = self._eval_in(
                        sid, "document.location?document.location.href:''") or ""
                except Exception:
                    url = ""
            origin = _origin_of(url)
            # Only real, loaded web origins are authorizable/driveable. An
            # unloaded / blank / errored frame (about:blank, chrome-error://, a
            # data: sub-frame) has no authorizable origin — skip it so it can't be
            # listed as reachable or (mis)authorized.
            if not origin.startswith(("http://", "https://")):
                continue
            out.append({"sessionId": sid, "url": url, "origin": origin})
        return out

    def _eval_in(self, session_id: str, expression: str) -> str:
        """Evaluate an expression INSIDE a child frame's CDP session (OOPIF),
        routed by sessionId. Used only after the frame's origin is authorized."""
        res = self._call("Runtime.evaluate", _session_id=session_id,
                         expression=self._prep(expression), returnByValue=True)
        value = (res or {}).get("result", {}).get("value", "")
        return value if isinstance(value, str) else json.dumps(value)

    def _eval_bool_in(self, session_id: str, expression: str) -> bool:
        res = self._call("Runtime.evaluate", _session_id=session_id,
                         expression=self._prep(expression), returnByValue=True)
        return bool((res or {}).get("result", {}).get("value"))

    def fill_in(self, session_id: str, selector: str, value: str) -> bool:
        """fill(), scoped to a child frame session. `value` (possibly a secret)
        is sent to the frame, never returned to the model."""
        expr = (f"(function(){{var e=__olyq({json.dumps(selector)});"
                f"if(!e)return false;e.focus();e.value={json.dumps(value)};"
                f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                f"e.dispatchEvent(new Event('change',{{bubbles:true}}));"
                f"return true;}})()")
        return self._eval_bool_in(session_id, expr)

    def observe_frame(self, session_id: str, limit: int = _OBSERVE_MAX) -> str:
        """Perceive a cross-origin frame's interactive elements as a numbered map
        — the governed crossing. Gating (the frame origin must be authorized) is
        enforced by the caller; this only runs once permitted. Content is
        untrusted, like any page perception."""
        self.ingested_untrusted = True
        limit = max(1, min(int(limit), _OBSERVE_MAX))
        raw = self._eval_in(session_id, _OBSERVE_JS.replace("LIMIT", str(limit))
                                                   .replace("CAP", str(_LABEL_MAX)))
        try:
            items = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            return "(could not read the frame's interactive elements)"
        lines = []
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            name = str(it.get("n") or "").strip()[:_LABEL_MAX]
            label = f' "{name}"' if name else ""
            lines.append(f"[{it.get('i')}] {it.get('t')}{label}")
        return "\n".join(lines) or "(no interactive elements found)"

    def act_in_frame(self, session_id: str, action: str, *, selector: str = "",
                     text: str = "", value: str = "", key: str = "") -> str:
        """Act INSIDE an authorized cross-origin frame (the governed crossing's
        write half). Selector-based verbs only — click, type, select, press — the
        form interactions a payment/login frame needs; coordinate/scroll verbs
        are page-level and don't apply. The caller enforces per-origin
        authorization. Landed steps are journaled (with an 'in frame' marker) so a
        proven cross-frame flow can be learned like any other."""
        action = (action or "").lower()
        if action in ("click", "type", "select") and not selector:
            return f"Error: '{action}' in a frame needs a selector."
        if action == "click":
            ok = self._eval_bool_in(session_id,
                f"(function(){{var e=__olyq({json.dumps(selector)});"
                f"if(e){{e.click();return true;}}return false;}})()")
            if not ok:
                return f"Error: no element for {selector} in frame."
            self._journal_step(f"click {selector} (in frame)")
            return f"Clicked {selector} in frame."
        if action == "type":
            if not self.fill_in(session_id, selector, text):
                return f"Error: no element for {selector} in frame."
            self._journal_step(f"type into {selector} (in frame)")
            return f"Typed into {selector} in frame."
        if action == "select":
            v = value or text
            ok = self._eval_bool_in(session_id,
                f"(function(){{var e=__olyq({json.dumps(selector)});if(!e)"
                f"return false;e.value={json.dumps(v)};e.dispatchEvent("
                f"new Event('change',{{bubbles:true}}));return true;}})()")
            if not ok:
                return f"Error: no element for {selector} in frame."
            self._journal_step(f"select {v!r} in {selector} (in frame)")
            return f"Selected {v!r} in {selector} in frame."
        if action == "press":
            k = key or text or "Enter"
            tgt = (f"__olyq({json.dumps(selector)})" if selector
                   else "document.activeElement")
            self._eval_in(session_id,
                f"(function(){{var e={tgt}||document.body;"
                f"['keydown','keypress','keyup'].forEach(function(t){{"
                f"e.dispatchEvent(new KeyboardEvent(t,{{key:{json.dumps(k)},"
                f"bubbles:true}}));}});return true;}})()")
            self._journal_step(f"press {k} (in frame)")
            return f"Pressed {k} in frame."
        return (f"Error: '{action}' isn't supported inside a frame "
                "(use click/type/select/press).")

    def _observe_raw(self, limit: int = _OBSERVE_MAX) -> list[dict] | None:
        """The structured perception: a list of {i, t, n, s} dicts (index, type,
        label, durable selector) for the visible interactive elements across the
        deep tree. Backs both observe() (the model-facing map) and self-healing
        (which needs each candidate's selector). Returns None if the page can't
        be read; refuses a blocked landing. Also refreshes the index→label map."""
        self.ingested_untrusted = True
        if self._blocked_landing():
            return None
        limit = max(1, min(int(limit), _OBSERVE_MAX))
        raw = self._eval(_OBSERVE_JS.replace("LIMIT", str(limit))
                                    .replace("CAP", str(_LABEL_MAX)))
        try:
            items = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
        out, self._labels = [], {}
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            out.append(it)
            try:                       # remember for readable learned steps
                self._labels[int(it.get("i"))] = (
                    str(it.get("n") or "").strip()[:_LABEL_MAX]
                    or str(it.get("t") or ""))
            except (TypeError, ValueError):
                pass
        return out

    def act(self, action: str, *, selector: str = "", text: str = "",
            x: int = 0, y: int = 0, index: int | None = None,
            key: str = "", value: str = "") -> str:
        """Act on the page. Credentialed actuator — exposed only as the
        ACTION_TOOL `browser_act`, so capability separation keeps it out of any
        run that also ingests untrusted content. Targets can be given by
        `index` (from observe()), a CSS `selector`, or `x`/`y` coordinates.
        Verbs: click, type, scroll, press (modifier chords like 'Control+a'),
        select, hover, rightclick, drag (source → target selector in `value`),
        wait_for (an element to appear, or disappear when value='gone'), back."""
        action = (action or "").lower()
        # An index from observe() resolves to the stamped stable selector; the
        # observed label makes a learned step human-readable.
        label = ""
        if index is not None:
            label = self._labels.get(int(index), "")
            selector = f'[data-olympus-idx="{int(index)}"]'
        target = f'"{label}"' if label else (selector or f"({x}, {y})")

        if action == "click":
            if selector:
                # Probe first: resolve + scroll to center + hit-test the landing
                # point. Absorbs page-agent's elementFromPoint check, but as a
                # GUARD (ADR 0014 (c)): when the point is clear we fire a TRUSTED,
                # coordinate-accurate CDP click (stronger than page-agent's
                # untrusted in-page dispatch); when an overlay obscures the point
                # we refuse the blind coordinate click (it would hit the overlay)
                # and instead dispatch straight to the intended element, flagging
                # the obstruction so the operator can dismiss the modal first.
                try:
                    probe = json.loads(self._eval(_click_probe_js(selector)) or "")
                except (json.JSONDecodeError, TypeError, ValueError):
                    probe = {}
                if not isinstance(probe, dict) or not probe.get("found"):
                    return f"Error: no element for {selector}."
                cx, cy = probe.get("cx"), probe.get("cy")
                clear = (not probe.get("obstructed")
                         and isinstance(cx, (int, float))
                         and isinstance(cy, (int, float)))
                note = ""
                if clear:
                    self._call("Input.dispatchMouseEvent", type="mouseMoved",
                               x=cx, y=cy)
                    self._call("Input.dispatchMouseEvent", type="mousePressed",
                               x=cx, y=cy, button="left", clickCount=1)
                    self._call("Input.dispatchMouseEvent", type="mouseReleased",
                               x=cx, y=cy, button="left", clickCount=1)
                else:
                    if not self._eval_bool(_dispatch_click_js(selector)):
                        return f"Error: no element for {selector}."
                    note = (" (click point was obscured or off-screen; dispatched "
                            "directly to the element — an overlay/cookie banner "
                            "may need dismissing first)")
                self._journal_step(f"click {target}")
                self._recipe_step("click", selector)
                return f"Clicked {selector}.{note}"
            self._call("Input.dispatchMouseEvent", type="mousePressed",
                       x=x, y=y, button="left", clickCount=1)
            self._call("Input.dispatchMouseEvent", type="mouseReleased",
                       x=x, y=y, button="left", clickCount=1)
            self._journal_step(f"click at ({x}, {y})")
            return f"Clicked at ({x}, {y})."
        if action == "type":
            # Never journal the typed text — it may be a credential. The learned
            # step records only that a field was filled, not with what.
            if selector:
                if not self.fill(selector, text):
                    return f"Error: no element for {selector}."
                self._journal_step(f"type into {target}")
                self._recipe_step("fill", selector, "$" + _slug(label or selector))
                return f"Typed into {selector}."
            self._call("Input.insertText", text=text)
            self._journal_step("type into focused field")
            return f"Typed {len(text)} chars."
        if action == "scroll":
            if selector:
                self._eval(f"(function(){{var e=__olyq("
                           f"{json.dumps(selector)});if(e)e.scrollIntoView("
                           f"{{block:'center'}});}})()")
                self._journal_step(f"scroll to {target}")
                return f"Scrolled to {selector}."
            amount = int(y) or int(x) or 600
            self._eval(f"window.scrollBy(0,{amount});")
            self._journal_step(f"scroll {amount}px")
            return f"Scrolled {amount}px."
        if action == "press":
            # Supports modifier chords like "Control+a" / "Shift+Tab" / "Meta+c".
            raw = key or text or "Enter"
            parts = [p for p in raw.split("+") if p]
            main = parts[-1] if parts else "Enter"
            mods = {p.lower() for p in parts[:-1]}
            ctrl = "true" if (mods & {"control", "ctrl"}) else "false"
            alt = "true" if (mods & {"alt", "option"}) else "false"
            shift = "true" if "shift" in mods else "false"
            meta = "true" if (mods & {"meta", "cmd", "command"}) else "false"
            init = (f"{{key:{json.dumps(main)},ctrlKey:{ctrl},altKey:{alt},"
                    f"shiftKey:{shift},metaKey:{meta},bubbles:true}}")
            expr_target = (f"__olyq({json.dumps(selector)})"
                           if selector else "document.activeElement")
            self._eval(
                f"(function(){{var e={expr_target}||document.body;"
                f"['keydown','keypress','keyup'].forEach(function(t){{"
                f"e.dispatchEvent(new KeyboardEvent(t,{init}));}});"
                f"return true;}})()")
            self._journal_step(f"press {raw}")
            return f"Pressed {raw}."
        if action == "select":
            if not selector:
                return "Error: select needs an index or selector."
            v = value or text
            ok = self._eval_bool(
                f"(function(){{var e=__olyq("
                f"{json.dumps(selector)});if(!e)return false;"
                f"e.value={json.dumps(v)};e.dispatchEvent("
                f"new Event('change',{{bubbles:true}}));return true;}})()")
            if not ok:
                return f"Error: no element for {selector}."
            self._journal_step(f"select {v!r} in {target}")
            self._recipe_step("fill", selector, "$" + _slug(label or selector))
            return f"Selected {v!r} in {selector}."
        if action == "hover":
            if selector:
                self._eval(f"(function(){{var e=__olyq("
                           f"{json.dumps(selector)});if(e)e.dispatchEvent("
                           f"new MouseEvent('mouseover',{{bubbles:true}}));}})()")
                self._journal_step(f"hover {target}")
                return f"Hovered {selector}."
            self._call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
            self._journal_step(f"hover at ({x}, {y})")
            return f"Hovered at ({x}, {y})."
        if action in ("rightclick", "contextmenu"):
            if selector:
                ok = self._eval_bool(
                    f"(function(){{var e=__olyq({json.dumps(selector)});"
                    f"if(!e)return false;e.dispatchEvent(new MouseEvent("
                    f"'contextmenu',{{bubbles:true}}));return true;}})()")
                if not ok:
                    return f"Error: no element for {selector}."
                self._journal_step(f"right-click {target}")
                return f"Right-clicked {selector}."
            self._call("Input.dispatchMouseEvent", type="mousePressed",
                       x=x, y=y, button="right", clickCount=1)
            self._call("Input.dispatchMouseEvent", type="mouseReleased",
                       x=x, y=y, button="right", clickCount=1)
            self._journal_step(f"right-click at ({x}, {y})")
            return f"Right-clicked at ({x}, {y})."
        if action == "drag":
            # Drag the source element onto a target selector (passed in `value`),
            # via HTML5 drag events with a shared DataTransfer. Both resolve deep.
            dst = value or text
            if not selector or not dst:
                return "Error: drag needs a source selector/index and a target " \
                       "selector in 'value'."
            ok = self._eval_bool(
                f"(function(){{var s=__olyq({json.dumps(selector)}),"
                f"d=__olyq({json.dumps(dst)});if(!s||!d)return false;"
                f"var dt=new DataTransfer();"
                f"s.dispatchEvent(new DragEvent('dragstart',"
                f"{{bubbles:true,dataTransfer:dt}}));"
                f"d.dispatchEvent(new DragEvent('dragover',"
                f"{{bubbles:true,dataTransfer:dt}}));"
                f"d.dispatchEvent(new DragEvent('drop',"
                f"{{bubbles:true,dataTransfer:dt}}));"
                f"s.dispatchEvent(new DragEvent('dragend',"
                f"{{bubbles:true,dataTransfer:dt}}));return true;}})()")
            if not ok:
                return f"Error: could not drag {selector} → {dst}."
            self._journal_step(f"drag {target} to {dst}")
            return f"Dragged {selector} to {dst}."
        if action == "back":
            self._eval("history.back();")
            self._wait_ready()
            self._journal_step("go back")
            return "Navigated back."
        if action == "wait_for":
            if not selector:
                return "Error: wait_for needs an index or selector."
            gone = (value or key or "").strip().lower() in (
                "gone", "absent", "hidden", "disappear")
            if self.wait_for(selector, gone=gone):
                return f"{'Gone' if gone else 'Appeared'}: {selector}."
            return (f"Error: timed out waiting for {selector} to "
                    f"{'disappear' if gone else 'appear'}.")
        return f"Error: unknown browser action '{action}'."

    def learned_steps(self) -> str:
        """The proven flow so far: the journal of act steps that landed this
        session, as a replayable, human-readable recipe. First-party (Olympus's
        own actions) and credential-free — safe to crystallize into a skill."""
        return "; ".join(self.journal)

    def learned_recipe(self) -> list[dict]:
        """The structured, promotable twin of learned_steps(): {op, selector,
        value?} steps with durable selectors, ready to graduate into a template."""
        return [dict(s) for s in self.recipe]

    @staticmethod
    def _prep(expression: str) -> str:
        """Auto-install the deep-query helper for any expression that uses it, so
        selector resolution (and observe()'s durable-selector stamping) cross
        shadow/iframe boundaries. Expressions that call neither helper are
        untouched, so plain evals (readyState, title) route unchanged."""
        if "__olyq" in expression or "__olySel" in expression:
            return _DEEP_JS + expression
        return expression

    def _eval(self, expression: str) -> str:
        res = self._call("Runtime.evaluate", expression=self._prep(expression),
                         returnByValue=True)
        value = (res or {}).get("result", {}).get("value", "")
        return value if isinstance(value, str) else json.dumps(value)

    def _eval_bool(self, expression: str) -> bool:
        res = self._call("Runtime.evaluate", expression=self._prep(expression),
                         returnByValue=True)
        return bool((res or {}).get("result", {}).get("value"))

    def _resolve_object_id(self, selector: str) -> str:
        """A CDP objectId (remote handle) for the element `selector` resolves to,
        crossing shadow/iframe boundaries via __olyq. Empty if it isn't found.
        Needed by CDP calls that operate on a node handle rather than a value
        (e.g. DOM.setFileInputFiles for uploads)."""
        res = self._call("Runtime.evaluate",
                         expression=self._prep(f"__olyq({json.dumps(selector)})"),
                         returnByValue=False)
        return (res or {}).get("result", {}).get("objectId", "") or ""

    def exists(self, selector: str) -> bool:
        """Structured predicate: is the selector present? Returns a bool, never
        page prose — so the actuator-holder can branch without ingesting text."""
        return self._eval_bool(
            f"!!__olyq({json.dumps(selector)})")

    def fill(self, selector: str, value: str) -> bool:
        """Set an input's value and fire input/change. `value` is sent to Chrome
        to type, never returned to the model (used for vault-sourced secrets)."""
        expr = (f"(function(){{var e=__olyq("
                f"{json.dumps(selector)});if(!e)return false;e.focus();"
                f"e.value={json.dumps(value)};"
                f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                f"e.dispatchEvent(new Event('change',{{bubbles:true}}));"
                f"return true;}})()")
        return self._eval_bool(expr)

    def run_template(self, steps: list[dict], params: dict | None = None) -> dict:
        """Execute a declarative action template step by step. Supported ops:
        `assert` (selector must exist), `click` (selector), `fill`
        (selector+value; value '$name' pulls from params), `wait`, `wait_idle`
        (settle dynamic content), `wait_for` (selector appears, or disappears
        with `gone: true`). Raises TemplateStepError on an unresolved or
        timed-out step (so the operator can self-heal) or RuntimeError on an
        unknown op. Page prose is never read as instructions; only selectors are
        touched."""
        params = params or {}
        done: list[str] = []
        for i, step in enumerate(steps or []):
            op = (step.get("op") or "").lower()
            sel = step.get("selector", "")
            if op == "assert":
                if not self.exists(sel):
                    raise TemplateStepError(
                        f"step {i}: required element {sel!r} is missing",
                        op=op, selector=sel, index=i)
                done.append(f"assert {sel}")
            elif op == "click":
                # A failed click was previously silent; detect the missing
                # target so a drifted template surfaces (and can self-heal).
                if self.act("click", selector=sel).startswith("Error:"):
                    raise TemplateStepError(
                        f"step {i}: click target {sel!r} is missing",
                        op=op, selector=sel, index=i)
                done.append(f"click {sel}")
            elif op == "fill":
                val = step.get("value", "")
                if isinstance(val, str) and val.startswith("$"):
                    val = str(params.get(val[1:], ""))
                if not self.fill(sel, val):
                    raise TemplateStepError(
                        f"step {i}: fill target {sel!r} is missing",
                        op=op, selector=sel, index=i)
                done.append(f"fill {sel}")
            elif op == "wait":
                self._wait_ready()
                done.append("wait")
            elif op == "wait_idle":
                self.wait_idle()
                done.append("wait_idle")
            elif op == "wait_for":
                if not self.wait_for(sel, gone=bool(step.get("gone"))):
                    raise TemplateStepError(
                        f"step {i}: {sel!r} never "
                        f"{'disappeared' if step.get('gone') else 'appeared'}",
                        op=op, selector=sel, index=i)
                done.append(f"wait_for {sel}")
            else:
                raise RuntimeError(f"step {i}: unknown op {op!r}")
        return {"steps": done}

    def heal_candidate(self, failed_selector: str,
                       min_similarity: float = 0.34) -> dict | None:
        """Self-healing lookup: after a step failed to resolve, re-observe the
        page and find the control that most likely IS the moved one, matching the
        failed selector's intent (its slug) against the current elements' labels
        and selectors. Returns {"selector", "label", "score"} for the best match
        above `min_similarity`, else None. This never acts and never rewrites the
        template — it only surfaces a candidate for a human-reviewed proposal."""
        target = _slug(failed_selector)
        if not target:
            return None
        items = self._observe_raw()
        if not items:
            return None
        best, best_score = None, 0.0
        for it in items:
            cand = str(it.get("s") or "")
            if not cand or cand == failed_selector:
                continue
            score = _match_score(target, str(it.get("n") or ""), cand)
            if score > best_score:
                best, best_score = it, score
        if best is None or best_score < min_similarity:
            return None
        return {"selector": str(best.get("s") or ""),
                "label": str(best.get("n") or ""), "score": round(best_score, 3)}

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
    # Structured, durable-selector steps ({op, selector, value?}) captured when
    # the skill was learned. Empty for a hand-written skill; when present and the
    # skill proves reliable, it can auto-graduate into a declarative template.
    recipe: list[dict] = field(default_factory=list)

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
            "recipe": self.recipe,
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
            recipe=d.get("recipe") if isinstance(d.get("recipe"), list) else [],
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


def _slug(text: str) -> str:
    """A safe param-name from a label or selector (alnum only), for a template
    fill step's `$name` placeholder. Never carries the typed value."""
    s = "".join(ch for ch in (text or "") if ch.isalnum())
    return s[:40] or "value"


def _origin_of(url: str) -> str:
    """scheme://host[:port] of a URL — the unit cross-origin frame authorization
    is granted against."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url or "")
    except ValueError:
        return ""
    if not p.scheme or not p.hostname:
        return ""
    host = p.hostname + (f":{p.port}" if p.port else "")
    return f"{p.scheme}://{host}"


def _similarity(a: str, b: str) -> float:
    """Cheap character-trigram Jaccard similarity in [0,1], for matching a
    drifted selector's intent against the labels/selectors now on the page."""
    a, b = (a or "").lower(), (b or "").lower()

    def grams(s: str) -> set[str]:
        s = f"  {s} "
        return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _match_score(target_slug: str, cand_label: str, cand_selector: str) -> float:
    """How likely is (label, selector) the moved control for `target_slug`?
    Exact slug match scores highest, then substring containment (a short intent
    like 'buy' inside 'buynow'), then trigram similarity as a soft fallback."""
    best = 0.0
    for h in (_slug(cand_label), _slug(cand_selector)):
        if not h:
            continue
        if target_slug == h:
            return 1.0
        if target_slug in h or h in target_slug:
            best = max(best, 0.9)
        else:
            best = max(best, _similarity(target_slug, h))
    return best


def record_skill(domain: str, name: str, steps: str, *, source: str = "agent",
                 author: str = "olympus", base_score: float = 0.0,
                 recipe: list[dict] | None = None) -> BrowserSkill:
    """Insert or replace a skill (keyed by domain+name), preserving outcome
    counts on replacement so a re-recorded skill keeps its measured score.

    Inputs are length-capped and the library is bounded (lowest-reliability
    skills are dropped past _SKILLS_MAX) so an over-eager agent can't bloat the
    on-disk store without limit. `recipe` (structured, durable-selector steps)
    enables later auto-graduation into a declarative template.
    """
    domain = _clip(domain, _SKILL_FIELD_MAX)
    name = _clip(name, _SKILL_FIELD_MAX)
    if not domain or not name:
        raise ValueError("a browser skill needs a non-empty domain and name")
    skill = BrowserSkill(
        domain=domain, name=name, steps=_clip(steps, _SKILL_STEPS_MAX),
        source=_clip(source, _SKILL_FIELD_MAX) or "agent",
        author=_clip(author, _SKILL_FIELD_MAX) or "olympus",
        base_score=max(0.0, min(1.0, base_score)),
        recipe=[s for s in (recipe or []) if isinstance(s, dict)][:_JOURNAL_MAX])
    skills = _load_skills()
    out, replaced = [], False
    for existing in skills:
        if _key(existing.domain, existing.name) == _key(domain, name):
            skill.runs, skill.successes = existing.runs, existing.successes
            skill.created = existing.created
            if not skill.recipe:          # keep a prior recipe if none re-supplied
                skill.recipe = existing.recipe
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
    prior = prof.templates.get(name, {})
    prof.templates[name] = {
        "risk": risk, "steps": steps[:64],
        "success_selector": _clip(success_selector, _SKILL_FIELD_MAX),
        # Preserve per-template outcome counts across a re-record, so a drift
        # demotion can be measured (the inverse of graduation).
        "runs": int(prior.get("runs", 0)),
        "successes": int(prior.get("successes", 0))}
    _store_profiles(profiles)
    return prof


def mark_template_outcome(domain: str, name: str, success: bool) -> None:
    """Record a run outcome for ONE template (not just the whole profile), so a
    template that drifts can be demoted on its own measured reliability."""
    domain = (domain or "").strip().lower()
    profiles = _load_profiles()
    for p in profiles:
        if p.domain == domain and name in (p.templates or {}):
            t = dict(p.templates[name])
            t["runs"] = int(t.get("runs", 0)) + 1
            if success:
                t["successes"] = int(t.get("successes", 0)) + 1
            p.templates = {**p.templates, name: t}
            _store_profiles(profiles)
            return


def demote_template(domain: str, name: str) -> bool:
    """Remove a template from a profile (the inverse of promote_skill): a
    graduated flow that has stopped working loses its promotion, so the operator
    stops auto-running a dead recipe."""
    domain = (domain or "").strip().lower()
    profiles = _load_profiles()
    for p in profiles:
        if p.domain == domain and name in (p.templates or {}):
            t = dict(p.templates)
            t.pop(name, None)
            p.templates = t
            _store_profiles(profiles)
            return True
    return False


def suggest_pattern(goal: str, exclude_domain: str = "") -> dict | None:
    """Cross-site generalization: find the most reliable learned skill on ANOTHER
    domain whose intent matches `goal`, and return its flow as a GENERALIZED
    scaffold — the op sequence with intent hints but selectors blanked (those are
    site-specific). Lets a new site bootstrap from a proven pattern (e.g. a
    login/checkout shape) instead of from scratch. Returns None if nothing close
    is found. First-party (own skills); never surfaces another site's selectors
    as if they applied here."""
    goal_slug = _slug(goal)
    if not goal_slug:
        return None
    ex = (exclude_domain or "").strip().lower()
    best, best_score = None, 0.0
    for s in list_skills():
        if not s.recipe or (ex and s.domain.strip().lower() == ex):
            continue
        # match on the skill's intent, weighted by its measured reliability
        score = _match_score(goal_slug, s.name, s.name) * (0.5 + 0.5 * s.reliability)
        if score > best_score:
            best, best_score = s, score
    if best is None or best_score < 0.30:
        return None
    steps = []
    for st in best.recipe:
        op = st.get("op", "")
        hint = str(st.get("value", "")).lstrip("$") or op
        steps.append({"op": op, "hint": hint})   # selector intentionally omitted
    return {"from_domain": best.domain, "name": best.name,
            "reliability": best.reliability, "steps": steps}


def promote_skill(domain: str, name: str, *, risk: str = "notable"
                  ) -> tuple["SiteProfile", str] | None:
    """Graduate a proven learned skill into a declarative action template.

    Builds the template from the skill's structured `recipe` (durable-selector
    click/fill steps), guarded by an `assert` on the first control so it fails
    fast if the page drifted. Returns (profile, template_name), or None if the
    skill has no promotable recipe. The template then rides the governed
    `browser_operate` path — auto-run within scope for notable risk, approval
    for anything higher — so graduation never widens the trust boundary."""
    skill = next((s for s in _load_skills()
                  if _key(s.domain, s.name) == _key(domain, name)), None)
    if skill is None:
        return None
    steps = [dict(s) for s in skill.recipe
             if isinstance(s, dict) and s.get("op") in ("click", "fill")
             and s.get("selector")]
    if not steps:
        return None
    guarded = [{"op": "assert", "selector": steps[0]["selector"]}] + steps
    prof = set_template(skill.domain, skill.name, risk, guarded)
    return prof, skill.name


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
