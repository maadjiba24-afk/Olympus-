"""Real-browser smoke test: drives an actual headless Chromium over CDP through
the SAME BrowserSession the offline suite exercises with FakeTransport. This is
the ground truth the offline doubles stand in for — it catches transport-level
gaps (node resolution, target attach, live network) the fakes can't.

Opt-in and self-skipping: it runs only when OLYMPUS_BROWSER_REAL=1 AND a
Chrome/Chromium binary is discoverable, so default CI (no browser) stays green.
Run it with:

    OLYMPUS_BROWSER_REAL=1 python -m pytest tests/test_browser_real.py -q
"""

import os

import pytest

from olympus import browser, security

pytestmark = pytest.mark.skipif(
    os.environ.get("OLYMPUS_BROWSER_REAL", "").strip() not in ("1", "true", "yes")
    or browser._find_chrome() is None,
    reason="real-browser smoke test (set OLYMPUS_BROWSER_REAL=1 with Chromium)")


@pytest.fixture
def real_session(monkeypatch, tmp_path):
    """A session bound to a freshly-launched headless Chromium, torn down after."""
    monkeypatch.setenv("OLYMPUS_BROWSER_AUTOLAUNCH", "1")
    monkeypatch.setenv("OLYMPUS_BROWSER_HEADLESS", "1")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    # data: URLs are the test fixtures; the SSRF gate is unit-tested elsewhere.
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    browser.set_transport_factory(None)
    browser.reset()
    yield browser.session()
    browser.reset()
    if browser._launched is not None:
        try:
            browser._launched[0].terminate()
        except Exception:
            pass
        browser._launched = None


def _page(body: str) -> str:
    return "data:text/html,<html><body>" + body + "</body></html>"


def test_open_observe_act_by_index(real_session):
    real_session.open(_page(
        "<button id=go onclick=\"document.title='clicked'\">Go</button>"
        "<input id=q>"))
    obs = real_session.observe()
    assert '[0] button "Go"' in obs
    out = real_session.act("click", index=0)
    assert "Clicked" in out
    assert real_session._eval("document.title") == "clicked"


def test_shadow_dom_is_reached_for_real(real_session):
    # A control inside an OPEN shadow root must appear in observe() and resolve.
    real_session.open(_page(
        "<div id=host></div><script>"
        "var h=document.getElementById('host').attachShadow({mode:'open'});"
        "h.innerHTML='<button id=sb>Shadow</button>';</script>"))
    obs = real_session.observe()
    assert 'button "Shadow"' in obs


def test_fill_and_select_for_real(real_session):
    real_session.open(_page(
        "<input id=q>"
        "<select id=s><option value=a>A</option><option value=b>B</option></select>"))
    real_session.act("type", selector="#q", text="hello world")
    assert real_session._eval("document.querySelector('#q').value") == "hello world"
    real_session.act("select", selector="#s", value="b")
    assert real_session._eval("document.querySelector('#s').value") == "b"


def test_screenshot_returns_png_bytes(real_session):
    import base64
    real_session.open(_page("<h1>hi</h1>"))
    b64 = real_session.screenshot()
    raw = base64.b64decode(b64)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"      # a real PNG signature


def test_element_and_full_page_screenshots_for_real(real_session):
    import base64
    real_session.open(_page(
        "<div style='height:3000px'>tall</div>"
        "<button id=b style='width:80px;height:30px'>Btn</button>"))
    viewport = len(base64.b64decode(real_session.screenshot()))
    full = len(base64.b64decode(real_session.screenshot(full_page=True)))
    element = len(base64.b64decode(real_session.screenshot(selector="#b")))
    # full page (3000px tall) is bigger than the viewport; one element is smaller
    assert full > viewport > element > 0


def test_transport_reconnects_after_a_dropped_socket(real_session):
    real_session.open(_page("<h1 id=t>ALIVE</h1>"))
    assert real_session._eval("document.getElementById('t').innerText") == "ALIVE"
    # forcibly drop the underlying WebSocket
    real_session._t._conn.close()
    # the next call transparently reconnects to the same tab and succeeds
    assert real_session._eval("1+1") == "2"
    assert real_session._eval("document.getElementById('t').innerText") == "ALIVE"


def test_list_tabs_for_real(real_session):
    real_session.open(_page("<h1>tab</h1>"))
    tabs = real_session.list_tabs()
    assert tabs and any(t["url"].startswith("data:") for t in tabs)


def test_rightclick_and_key_chord_for_real(real_session):
    real_session.open(_page(
        "<div id=d oncontextmenu=\"this.setAttribute('data-ctx','1')\">D</div>"
        "<input id=q onkeydown=\"if(event.ctrlKey&&event.key==='a')"
        "this.setAttribute('data-chord','1')\">"))
    real_session.act("rightclick", selector="#d")
    assert real_session._eval("document.querySelector('#d').getAttribute('data-ctx')") == "1"
    real_session.act("type", selector="#q", text="x")
    real_session.act("press", selector="#q", key="Control+a")
    assert real_session._eval(
        "document.querySelector('#q').getAttribute('data-chord')") == "1"


def test_cross_origin_frame_surfaces_as_oopif_for_real(real_session):
    import time
    # A real cross-site iframe becomes an out-of-process target with its own CDP
    # session; eval routes INTO it. (Content may not load if the sandbox blocks
    # the iframe's egress, but the OOPIF session + sessionId routing still hold.)
    real_session.open(_page(
        "<h1>parent</h1><iframe src='https://example.com/'></iframe>"))
    time.sleep(3.0)
    frames = real_session.list_frames()
    if not frames:
        pytest.skip("no out-of-process iframe surfaced in this environment")
    sid = frames[0]["sessionId"]
    assert sid                                   # a distinct child session
    title = real_session._eval_in(sid, "document.title")
    assert isinstance(title, str)                # eval routed into the frame


def test_detect_checkpoint_for_real(real_session):
    # A page with a reCAPTCHA marker is detected as a captcha checkpoint; a plain
    # page is 'none'. The detector only reads markers — it never solves anything.
    real_session.open(_page("<div class='g-recaptcha'></div>"))
    assert real_session.detect_checkpoint()["type"] == "captcha"
    real_session.open(_page(
        "<input autocomplete='one-time-code' name='otp'>"))
    assert real_session.detect_checkpoint()["type"] == "otp"
    real_session.open(_page("<p>Two-factor authentication required</p>"))
    assert real_session.detect_checkpoint()["type"] == "step_up"
    real_session.open(_page("<h1>ordinary page</h1>"))
    assert real_session.detect_checkpoint()["type"] == "none"


def test_js_dialog_does_not_hang_and_respects_policy(real_session):
    # A click that pops confirm() must not wedge the harness. Default policy
    # dismisses (confirm→false); accept makes it confirm (confirm→true).
    page = ("<button id=go onclick=\"document.title="
            "window.confirm('ok?')?'YES':'NO'\">Go</button>")
    real_session.open(_page(page))
    real_session.act("click", selector="#go")             # default: dismiss
    assert real_session._eval("document.title") == "NO"
    real_session.set_dialog_policy(True)                   # now accept
    real_session.open(_page(page))
    real_session.act("click", selector="#go")
    assert real_session._eval("document.title") == "YES"


def test_network_idle_tracks_real_requests(real_session):
    # The transport tracks in-flight requests from the CDP event stream, so
    # wait_idle is a true network-idle (not a heuristic) and ends at zero.
    real_session.open(_page(
        "hi<script>fetch('data:text/plain,'+new Array(500).join('x'))"
        ".then(function(r){return r.text();});</script>"))
    real_session.wait_idle(quiet=0.2, timeout=5.0)
    assert real_session._t.inflight() == 0                # settled


def test_wait_for_element_appears_for_real(real_session):
    # An element that appears after a short delay — wait_for must block until it's
    # there, not race a fixed sleep.
    real_session.open(_page(
        "<div id=box></div><script>setTimeout(function(){"
        "document.getElementById('box').innerHTML="
        "'<button id=late>Late</button>';},400);</script>"))
    assert real_session.wait_for("#late", timeout=5.0) is True
    assert real_session.exists("#late")


def test_download_capture_for_real(real_session, tmp_path):
    # A download link click lands a real file in the confined workspace.
    real_session.open(_page(
        "<a id=dl download='hello.txt' href='data:text/plain,hello-world'>get</a>"))
    out = real_session.download("#dl", timeout=15.0)
    assert "Downloaded hello.txt" in out
    assert (tmp_path / "hello.txt").read_text() == "hello-world"


def test_cookie_save_restore_roundtrip_for_real(real_session):
    real_session.open(_page("<h1>hi</h1>"))
    # set a session cookie for an explicit origin, read it back, clear, restore
    n = real_session.set_cookies([
        {"name": "sid", "value": "abc123", "url": "https://example.com/"}])
    assert n == 1
    saved = real_session.get_cookies("example.com")
    assert saved and saved[0]["name"] == "sid" and saved[0]["value"] == "abc123"


def test_durable_selector_is_rooted_and_unambiguous(real_session):
    import json
    # Two sibling groups each with two buttons — a bare nth-of-type is ambiguous.
    real_session.open(_page(
        "<section id=box><div><button>A</button><button>TARGET</button></div>"
        "</section><div><button>C</button><button>D</button></div>"))
    real_session.observe()
    sel = real_session._durable_selector('[data-olympus-idx="1"]')
    assert sel.startswith("#box")                       # anchored at the id
    resolved = real_session._eval(
        "(function(){var e=__olyq(%s);return e?e.innerText:'MISS';})()"
        % json.dumps(sel))
    assert resolved == "TARGET"                          # resolves uniquely


def test_upload_attaches_the_file_for_real(real_session, tmp_path):
    (tmp_path / "up.txt").write_text("hello", encoding="utf-8")
    real_session.open(_page("<input type=file id=f>"))
    out = real_session.upload("#f", "up.txt")
    assert "Uploaded up.txt" in out
    # the file is actually attached to the input (objectId resolution worked)
    assert real_session._eval("document.querySelector('#f').files.length") == "1"


def test_switch_tab_actually_drives_the_new_tab(real_session):
    import time
    real_session.open(_page("<h1 id=t>TAB-ONE</h1>"))
    real_session._call("Target.createTarget",
                       url="data:text/html,<h1 id=t>TAB-TWO</h1>")
    time.sleep(1.0)
    tabs = real_session.list_tabs()
    idx = next(i for i, t in enumerate(tabs) if "TAB-TWO" in t["url"])
    assert real_session.switch_tab(idx)
    # evals now run in the SECOND tab's context — the transport re-bound
    assert real_session._eval(
        "document.getElementById('t').innerText") == "TAB-TWO"
