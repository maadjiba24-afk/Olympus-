import sys
import types

from olympus import browser


def test_playwright_transport_launches_firefox_and_translates_core_calls(
    monkeypatch,
):
    events = []

    class FakePage:
        def goto(self, url, **kwargs):
            events.append(("goto", url))
            return None

        def evaluate(self, expression):
            events.append(("evaluate", expression))
            return 2

    class FakeContext:
        def __init__(self):
            self.pages = []

        def new_page(self):
            events.append("new_page")
            page = FakePage()
            self.pages.append(page)
            return page

        def close(self):
            events.append("context_close")

    class FakeBrowser:
        def new_context(self, **kwargs):
            events.append(("new_context", kwargs))
            return FakeContext()

        def close(self):
            events.append("browser_close")

    class FakeBrowserType:
        def launch(self, *, headless):
            events.append(("launch", headless))
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self):
            self.firefox = FakeBrowserType()
            self.webkit = FakeBrowserType()

        def stop(self):
            events.append("playwright_stop")

    class FakeStarter:
        def start(self):
            events.append("playwright_start")
            return FakePlaywright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakeStarter()

    package = types.ModuleType("playwright")
    package.__path__ = []

    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    transport = browser._PlaywrightTransport("firefox", headless=True)

    assert transport.send(
        "Page.navigate",
        {"url": "https://example.com"},
    ) == {}

    assert transport.send(
        "Runtime.evaluate",
        {"expression": "1+1", "returnByValue": True},
    ) == {"result": {"value": 2}}

    transport.close()

    assert events == [
        "playwright_start",
        ("launch", True),
        (
            "new_context",
            {
                "accept_downloads": True,
                "service_workers": "block",
            },
        ),
        "new_page",
        ("goto", "https://example.com"),
        ("evaluate", "1+1"),
        "context_close",
        "browser_close",
        "playwright_stop",
    ]


def test_playwright_transport_installs_subresource_gate(monkeypatch):
    handlers = []
    events = []

    class FakePage:
        pass

    class FakeContext:
        def new_page(self):
            return FakePage()

        def route(self, pattern, handler):
            events.append(("route", pattern))
            handlers.append(handler)

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kwargs):
            return FakeContext()

        def close(self):
            pass

    class FakeBrowserType:
        def launch(self, *, headless):
            return FakeBrowser()

    class FakePlaywright:
        firefox = FakeBrowserType()
        webkit = FakeBrowserType()

        def stop(self):
            pass

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakeStarter()

    package = types.ModuleType("playwright")
    package.__path__ = []

    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    transport = browser._PlaywrightTransport("firefox", headless=True)

    assert transport.send("Network.enable", {}) == {}
    assert transport.send(
        "Network.setBlockedURLs",
        {"urls": ["*://127.*", "*://169.254.*"]},
    ) == {}

    assert events == [("route", "**/*")]
    assert len(handlers) == 1

    class FakeRoute:
        def __init__(self, url):
            self.request = types.SimpleNamespace(url=url)

        def abort(self, error_code=None):
            events.append(("abort", error_code))

        def continue_(self):
            events.append(("continue", self.request.url))

    handlers[0](FakeRoute("http://127.0.0.1/private"))
    handlers[0](FakeRoute("https://example.com/public"))

    assert events[-2:] == [
        ("abort", "blockedbyclient"),
        ("continue", "https://example.com/public"),
    ]

    transport.close()


def test_playwright_transport_translates_cookies_and_screenshot(monkeypatch):
    events = []

    class FakePage:
        def screenshot(self, **kwargs):
            events.append(("screenshot", kwargs))
            return b"PNG-BYTES"

    class FakeContext:
        def new_page(self):
            return FakePage()

        def cookies(self):
            events.append("cookies")
            return [
                {
                    "name": "sid",
                    "value": "abc",
                    "domain": "example.com",
                    "path": "/",
                }
            ]

        def add_cookies(self, cookies):
            events.append(("add_cookies", cookies))

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kwargs):
            return FakeContext()

        def close(self):
            pass

    class FakeBrowserType:
        def launch(self, *, headless):
            return FakeBrowser()

    class FakePlaywright:
        firefox = FakeBrowserType()
        webkit = FakeBrowserType()

        def stop(self):
            pass

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakeStarter()

    package = types.ModuleType("playwright")
    package.__path__ = []

    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    transport = browser._PlaywrightTransport("webkit", headless=True)

    assert transport.send("Storage.getCookies", {}) == {
        "cookies": [
            {
                "name": "sid",
                "value": "abc",
                "domain": "example.com",
                "path": "/",
            }
        ]
    }

    cookies = [
        {
            "name": "saved",
            "value": "value",
            "domain": "example.com",
            "path": "/",
        }
    ]
    assert transport.send(
        "Network.setCookies",
        {"cookies": cookies},
    ) == {}

    result = transport.send(
        "Page.captureScreenshot",
        {
            "format": "png",
            "clip": {
                "x": 1,
                "y": 2,
                "width": 30,
                "height": 40,
                "scale": 1,
            },
            "captureBeyondViewport": True,
        },
    )

    import base64

    assert result == {
        "data": base64.b64encode(b"PNG-BYTES").decode("ascii")
    }
    assert events == [
        "cookies",
        ("add_cookies", cookies),
        (
            "screenshot",
            {
                "type": "png",
                "clip": {
                    "x": 1,
                    "y": 2,
                    "width": 30,
                    "height": 40,
                },
            },
        ),
    ]

    transport.close()


def test_playwright_transport_translates_input_operations(monkeypatch):
    events = []

    class FakeMouse:
        def move(self, x, y):
            events.append(("mouse_move", x, y))

        def down(self, **kwargs):
            events.append(("mouse_down", kwargs))

        def up(self, **kwargs):
            events.append(("mouse_up", kwargs))

    class FakeKeyboard:
        def insert_text(self, text):
            events.append(("insert_text", text))

    class FakePage:
        def __init__(self):
            self.mouse = FakeMouse()
            self.keyboard = FakeKeyboard()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kwargs):
            return FakeContext()

        def close(self):
            pass

    class FakeBrowserType:
        def launch(self, *, headless):
            return FakeBrowser()

    class FakePlaywright:
        firefox = FakeBrowserType()
        webkit = FakeBrowserType()

        def stop(self):
            pass

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakeStarter()

    package = types.ModuleType("playwright")
    package.__path__ = []

    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    transport = browser._PlaywrightTransport("firefox", headless=True)

    assert transport.send("Runtime.enable", {}) == {}
    assert transport.send("Log.enable", {}) == {}

    assert transport.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": 10, "y": 20},
    ) == {}

    assert transport.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": 10,
            "y": 20,
            "button": "left",
            "clickCount": 1,
        },
    ) == {}

    assert transport.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": 10,
            "y": 20,
            "button": "left",
            "clickCount": 1,
        },
    ) == {}

    assert transport.send(
        "Input.insertText",
        {"text": "hello"},
    ) == {}

    assert events == [
        ("mouse_move", 10, 20),
        ("mouse_down", {"button": "left", "click_count": 1}),
        ("mouse_up", {"button": "left", "click_count": 1}),
        ("insert_text", "hello"),
    ]

    transport.close()


def test_playwright_transport_lists_and_switches_tabs(monkeypatch):
    events = []

    class FakePage:
        def __init__(self, title, url):
            self._title = title
            self.url = url

        def title(self):
            return self._title

        def bring_to_front(self):
            events.append(("bring_to_front", self._title))

        def evaluate(self, expression):
            events.append(("evaluate", self._title, expression))
            return self._title

    class FakeContext:
        def __init__(self):
            self.pages = [
                FakePage("First", "https://first.example/"),
                FakePage("Second", "https://second.example/"),
            ]

        def new_page(self):
            return self.pages[0]

        def close(self):
            pass

    context = FakeContext()

    class FakeBrowser:
        def new_context(self, **kwargs):
            return context

        def close(self):
            pass

    class FakeBrowserType:
        def launch(self, *, headless):
            return FakeBrowser()

    class FakePlaywright:
        firefox = FakeBrowserType()
        webkit = FakeBrowserType()

        def stop(self):
            pass

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakeStarter()

    package = types.ModuleType("playwright")
    package.__path__ = []

    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    transport = browser._PlaywrightTransport("firefox", headless=True)

    result = transport.send("Target.getTargets", {})
    targets = result["targetInfos"]

    assert [
        (target["title"], target["url"], target["type"])
        for target in targets
    ] == [
        ("First", "https://first.example/", "page"),
        ("Second", "https://second.example/", "page"),
    ]

    second_id = targets[1]["targetId"]

    assert transport.send(
        "Target.activateTarget",
        {"targetId": second_id},
    ) == {}

    transport.reattach(second_id)

    assert transport.send(
        "Runtime.evaluate",
        {"expression": "document.title", "returnByValue": True},
    ) == {"result": {"value": "Second"}}

    assert events == [
        ("bring_to_front", "Second"),
        ("evaluate", "Second", "document.title"),
    ]

    transport.close()


def test_playwright_transport_resolves_handle_and_uploads_file(monkeypatch):
    events = []

    class FakeElementHandle:
        def set_input_files(self, files):
            events.append(("set_input_files", files))

    class FakeJSHandle:
        def __init__(self):
            self.element = FakeElementHandle()

        def as_element(self):
            events.append("as_element")
            return self.element

        def dispose(self):
            events.append("dispose")

    class FakePage:
        def evaluate_handle(self, expression):
            events.append(("evaluate_handle", expression))
            return FakeJSHandle()

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kwargs):
            return FakeContext()

        def close(self):
            pass

    class FakeBrowserType:
        def launch(self, *, headless):
            return FakeBrowser()

    class FakePlaywright:
        firefox = FakeBrowserType()
        webkit = FakeBrowserType()

        def stop(self):
            pass

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakeStarter()

    package = types.ModuleType("playwright")
    package.__path__ = []

    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    transport = browser._PlaywrightTransport("firefox", headless=True)

    result = transport.send(
        "Runtime.evaluate",
        {
            "expression": "document.querySelector('#file')",
            "returnByValue": False,
        },
    )

    object_id = result["result"]["objectId"]
    assert object_id.startswith("playwright-handle-")

    assert transport.send(
        "DOM.setFileInputFiles",
        {
            "objectId": object_id,
            "files": [r"C:\workspace\document.txt"],
        },
    ) == {}

    assert events == [
        ("evaluate_handle", "document.querySelector('#file')"),
        "as_element",
        ("set_input_files", [r"C:\workspace\document.txt"]),
        "dispose",
    ]

    transport.close()


def test_playwright_transport_persists_downloads(monkeypatch, tmp_path):
    events = []
    handlers = {}

    class FakePage:
        def on(self, event, handler):
            events.append(("on", event))
            handlers[event] = handler

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kwargs):
            return FakeContext()

        def close(self):
            pass

    class FakeBrowserType:
        def launch(self, *, headless):
            return FakeBrowser()

    class FakePlaywright:
        firefox = FakeBrowserType()
        webkit = FakeBrowserType()

        def stop(self):
            pass

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakeStarter()

    package = types.ModuleType("playwright")
    package.__path__ = []

    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    transport = browser._PlaywrightTransport("firefox", headless=True)

    assert transport.send(
        "Page.setDownloadBehavior",
        {
            "behavior": "allow",
            "downloadPath": str(tmp_path),
        },
    ) == {}

    assert events == [("on", "download")]

    class FakeDownload:
        suggested_filename = "report.txt"

        def save_as(self, destination):
            events.append(("save_as", destination))

    handlers["download"](FakeDownload())

    assert events == [
        ("on", "download"),
        ("save_as", str(tmp_path / "report.txt")),
    ]

    transport.close()
