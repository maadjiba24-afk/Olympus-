"""End-to-end browser tests against real Playwright Firefox and WebKit.

These tests are opt-in locally and mandatory in the dedicated CI browser
matrix. They exercise Olympus BrowserSession, not Playwright directly.

Run one engine with:

    $env:OLYMPUS_BROWSER_PLAYWRIGHT_REAL="1"
    $env:OLYMPUS_BROWSER_ENGINE="firefox"   # or webkit
    python -m pytest -q tests/test_browser_playwright_real.py
"""

from __future__ import annotations

import base64
import os
import urllib.parse

import pytest

from olympus import browser, security


_ENABLED = os.environ.get(
    "OLYMPUS_BROWSER_PLAYWRIGHT_REAL", ""
).strip().lower() in {"1", "true", "yes"}

_ENGINE = os.environ.get(
    "OLYMPUS_BROWSER_ENGINE", ""
).strip().lower()

pytestmark = pytest.mark.skipif(
    not _ENABLED or _ENGINE not in {"firefox", "webkit"},
    reason=(
        "set OLYMPUS_BROWSER_PLAYWRIGHT_REAL=1 and "
        "OLYMPUS_BROWSER_ENGINE=firefox|webkit"
    ),
)


def _page(body: str) -> str:
    html = f"<!doctype html><html><head><title>initial</title></head><body>{body}</body></html>"
    return "data:text/html," + urllib.parse.quote(html)


@pytest.fixture
def real_session(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_BROWSER_ENGINE", _ENGINE)
    monkeypatch.setenv("OLYMPUS_BROWSER_AUTOLAUNCH", "1")
    monkeypatch.setenv("OLYMPUS_BROWSER_HEADLESS", "1")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))

    # Test fixtures use data URLs. URL-policy behavior is tested separately.
    monkeypatch.setattr(security, "url_block_reason", lambda _url: None)

    browser.set_transport_factory(None)
    browser.reset()

    try:
        yield browser.session()
    finally:
        browser.reset()


def test_real_playwright_core_browser_flow(real_session, tmp_path):
    real_session.open(
        _page(
            """
            <button id="go"
                    onclick="document.title='clicked'">Go</button>
            <input id="query"
                   onkeydown="if(event.ctrlKey && event.key === 'a')
                   this.dataset.chord='yes'">
            <select id="choice">
              <option value="a">A</option>
              <option value="b">B</option>
            </select>
            <input id="upload" type="file">
            <a id="download"
               download="result.txt"
               href="data:text/plain,olympus-download">Download</a>
            """
        )
    )

    observed = real_session.observe()
    assert 'button "Go"' in observed

    assert "Clicked" in real_session.act("click", selector="#go")
    assert real_session._eval("document.title") == "clicked"

    real_session.act("type", selector="#query", text="hello")
    assert (
        real_session._eval("document.querySelector('#query').value")
        == "hello"
    )

    real_session.act("select", selector="#choice", value="b")
    assert (
        real_session._eval("document.querySelector('#choice').value")
        == "b"
    )

    real_session.act("press", selector="#query", key="Control+a")
    assert (
        real_session._eval("document.querySelector('#query').dataset.chord")
        == "yes"
    )

    png = base64.b64decode(real_session.screenshot())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

    upload_path = tmp_path / "upload.txt"
    upload_path.write_text("uploaded through Olympus", encoding="utf-8")
    assert "Uploaded upload.txt" in real_session.upload("#upload", "upload.txt")
    assert (
        real_session._eval(
            "document.querySelector('#upload').files[0].name"
        )
        == "upload.txt"
    )

    assert "Downloaded result.txt" in real_session.download(
        "#download",
        timeout=15.0,
    )
    assert (tmp_path / "result.txt").read_text(
        encoding="utf-8"
    ) == "olympus-download"


def test_real_playwright_dialog_console_and_network_events(real_session):
    real_session.open(
        _page(
            """
            <script>
              console.log('olympus-console-message');
              fetch('data:text/plain,network-event').then(r => r.text());
            </script>
            <button id="dialog"
                    onclick="document.title =
                      window.confirm('continue?') ? 'YES' : 'NO'">
              Dialog
            </button>
            """
        )
    )

    real_session.wait_idle(quiet=0.2, timeout=5.0)
    assert real_session._t.inflight() == 0

    assert "olympus-console-message" in real_session.console_logs()

    real_session.set_dialog_policy(True)
    real_session.act("click", selector="#dialog")
    assert real_session._eval("document.title") == "YES"


def test_real_playwright_tabs_are_drivable(real_session):
    real_session.open(
        _page(
            """
            <button id="new-tab"
                    onclick="
                      var popup = window.open('about:blank');
                      popup.document.write(
                        '<title>SECOND</title><h1 id=target>SECOND</h1>'
                      );
                      popup.document.close();
                    ">
              Open tab
            </button>
            """
        )
    )

    real_session.act("click", selector="#new-tab")
    tabs = real_session.list_tabs()

    assert len(tabs) >= 2
    second = next(
        index
        for index, tab in enumerate(tabs)
        if tab["title"] == "SECOND"
    )

    assert real_session.switch_tab(second)
    assert (
        real_session._eval("document.getElementById('target').innerText")
        == "SECOND"
    )
