"""Real-Chrome smoke test for the governed browser harness.

Opt-in only: skipped unless OLYMPUS_BROWSER_SMOKE=1, because it launches a real
headless Chrome and needs the optional `websockets` transport dependency. When
enabled it exercises the *actual* CDP path end to end — launch Chrome, discover
a page target, drive `browser.session("cli")` (the real `_RealTransport`) at a
loopback page, and read it back — so the live transport can't silently rot.

Run it with:

    pip install websockets
    OLYMPUS_BROWSER_SMOKE=1 pytest tests/test_browser_smoke.py -q

The SSRF gate is tested in tests/test_browser.py; here we point the harness at a
loopback test server and bypass that one check (only) so we can assert the
transport itself, not the gate.
"""

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from olympus import browser, browserlease, security

pytestmark = pytest.mark.skipif(
    not os.environ.get("OLYMPUS_BROWSER_SMOKE"),
    reason="set OLYMPUS_BROWSER_SMOKE=1 to run the real-Chrome smoke test",
)

_PAGE = (b"<!doctype html><html><head><title>Olympus Smoke</title></head>"
         b"<body><main>hello from olympus</main></body></html>")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_chrome() -> str | None:
    """Use Olympus's shared cross-platform Chromium browser detection."""
    return browser._find_chrome()



def _wait_devtools(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception as err:  # not up yet
            last = err
            time.sleep(0.2)
    raise RuntimeError(f"DevTools never came up on :{port} ({last})")


@pytest.fixture
def chrome_and_server(tmp_path, monkeypatch):
    chrome = _find_chrome()
    if not chrome:
        pytest.skip("no Chrome/Chromium binary found")
    pytest.importorskip("websockets", reason="real transport needs websockets")

    # Olympus refuses to attach to a remote-CDP browser that has no lease unless
    # an owner is configured, because such a browser is normally someone's own
    # and may already hold their sessions. That gate is correct and stays.
    # THIS fixture is the narrow exception: it launches a dedicated headless
    # Chrome on a throwaway --user-data-dir under tmp_path, so the endpoint
    # below holds nothing but what this test puts there. We therefore assign
    # that specific endpoint to the trusted CLI principal. This says nothing
    # about arbitrary remote browsers, which still require a deliberate
    # OLYMPUS_BROWSER_OWNER from the operator.
    monkeypatch.setenv(browserlease.OWNER_ENV, "cli")
    assert browserlease.configured_owner() == "cli"

    # Loopback page server.
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_PAGE)

        def log_message(self, *a):  # silence
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", _free_port()), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{srv.server_address[1]}/"

    dport = _free_port()
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={dport}", "--remote-allow-origins=*",
         f"--user-data-dir={tmp_path / 'profile'}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_devtools(dport)
        yield dport, page_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.shutdown()


def test_real_chrome_open_and_read(chrome_and_server, monkeypatch):
    dport, page_url = chrome_and_server
    monkeypatch.setenv("OLYMPUS_BROWSER_CDP_URL", f"http://127.0.0.1:{dport}")
    browser.set_transport_factory(None)  # use the real env-driven transport

    # Bypass the SSRF gate for this loopback URL only — we're testing transport.
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    try:
        out = browser.session("cli").open(page_url)
        assert "Olympus Smoke" in out
        assert "hello from olympus" in out

        assert browser.session("cli").read("main") == "hello from olympus"

        methods = [c["method"] for c in browser.session("cli").ledger]
        assert "Page.navigate" in methods and "Runtime.evaluate" in methods
    finally:
        browser.reset()


def test_real_transport_still_enforces_ssrf(chrome_and_server, monkeypatch):
    # With the real transport attached, the SSRF gate must still refuse an
    # internal address before any navigate reaches Chrome.
    dport, _ = chrome_and_server
    monkeypatch.setenv("OLYMPUS_BROWSER_CDP_URL", f"http://127.0.0.1:{dport}")
    browser.set_transport_factory(None)
    try:
        out = browser.session("cli").open("http://169.254.169.254/latest/meta-data/")
        assert out.startswith("Error:")
        assert not any(c["method"] == "Page.navigate"
                       for c in browser.session("cli").ledger)
    finally:
        browser.reset()
