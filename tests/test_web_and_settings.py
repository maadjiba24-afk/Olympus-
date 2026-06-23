import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import config, web
from olympus.openai_compat import extract_json, _to_openai_tools
from olympus import tools


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _post(url, path, payload, headers=None):
    req = urllib.request.Request(
        url + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    return urllib.request.urlopen(req)


def test_settings_provider_switch_clears_credentials():
    s = config.Settings(provider="anthropic", model="claude-opus-4-8",
                        api_key="ant-key")
    switched = s.merged({"provider": "openai"})
    assert switched.model == "" and switched.api_key is None
    assert switched.validate() is not None


def test_extract_json_lenient():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('prose {"a": {"b": 2}} more') == {"a": {"b": 2}}


def test_tool_conversion():
    conv = _to_openai_tools([tools.SAVE_LESSON])
    assert conv[0]["function"]["name"] == "save_lesson"


def test_home_page_injects_config_and_onboarding(server, monkeypatch):
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "5")
    page = urllib.request.urlopen(server + "/").read().decode()
    # The server config placeholder is always substituted (never shipped raw).
    assert "__OLYMPUS_CFG__" not in page
    assert '"free_chats": 5' in page
    # First-run onboarding is present: welcome, example chips, BYOK guidance.
    assert 'id="welcome"' in page and 'class="chip"' in page
    assert "Bring your own model" in page


def test_web_invalid_settings_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server, "/api/chat", {"message": "hi", "session": "s",
                                    "settings": {"provider": "openai"}})
    assert err.value.code == 400


def test_web_rate_limit(server, monkeypatch):
    monkeypatch.setenv("OLYMPUS_RATE_LIMIT", "2")
    body = {"message": "hi", "session": "s",
            "settings": {"provider": "openai"}}  # 400s cheaply after limit
    codes = []
    for _ in range(4):
        try:
            _post(server, "/api/chat", body)
        except urllib.error.HTTPError as err:
            codes.append(err.code)
    assert 429 in codes


def test_web_access_token(server, monkeypatch):
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "secret")
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server, "/api/chat", {"message": "hi", "session": "s"})
    assert err.value.code == 401
    # correct token passes auth (then fails later for other reasons or works)
    try:
        _post(server, "/api/chat",
              {"message": "hi", "session": "s",
               "settings": {"provider": "openai"}},
              headers={"X-Olympus-Token": "secret"})
    except urllib.error.HTTPError as err:
        assert err.code != 401


def test_web_feedback_requires_history(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(server, "/api/feedback", {"session": "fresh", "verdict": "up"})
    assert err.value.code == 400


def test_session_event_isolation(server):
    web._session("alpha").events.append("only-alpha")
    a = json.loads(urllib.request.urlopen(
        f"{server}/api/status?since=0&session=alpha").read())
    b = json.loads(urllib.request.urlopen(
        f"{server}/api/status?since=0&session=beta").read())
    assert a["events"] == ["only-alpha"] and b["events"] == []
