"""Per-provider credential rotation — several keys compose into one allowance.

When the active key hits a rate limit / quota wall, the backend rotates to the
next key instead of failing the request.
"""

import io
import urllib.error

import pytest

from olympus import config, openai_compat as oc


@pytest.fixture(autouse=True)
def _reset_rotation_state():
    """Rotation state is process-wide; isolate each test."""
    oc._key_cursor.clear()
    oc._key_stats.clear()
    oc._exhausted.clear()
    yield
    oc._key_cursor.clear()
    oc._key_stats.clear()
    oc._exhausted.clear()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_429():
    return urllib.error.HTTPError(
        "http://x/v1/chat/completions", 429, "Too Many Requests", {},
        io.BytesIO(b'{"error":{"message":"rate limit exceeded"}}'))


def _ok_body(model="m"):
    return _Resp(b'{"choices":[{"message":{"content":"hi"}}],'
                 b'"usage":{"prompt_tokens":1,"completion_tokens":1}}')


# --- Settings key handling ------------------------------------------------

def test_all_keys_dedupes_and_orders():
    s = config.Settings(provider="openai", model="m",
                        api_key="k1", api_keys=("k1", "k2", "k2", "k3"))
    assert s.all_keys() == ("k1", "k2", "k3")


def test_from_env_reads_key_pool(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PROVIDER", "openai")
    monkeypatch.setenv("OLYMPUS_MODEL", "m")
    monkeypatch.setenv("OPENAI_API_KEY", "primary")
    monkeypatch.setenv("OLYMPUS_API_KEYS", "primary, backup1  backup2")
    s = config.Settings.from_env()
    assert s.all_keys() == ("primary", "backup1", "backup2")


def test_mask_key_never_leaks_secret():
    assert config.mask_key("sk-supersecret1234") == "…1234"
    assert "supersecret" not in config.mask_key("sk-supersecret1234")
    # Short keys (≤8 chars) are fully bulleted rather than half-revealed.
    assert config.mask_key("abc12345")[-4:] != "2345"


# --- rotation behavior ----------------------------------------------------

def test_rotates_to_next_key_on_quota(monkeypatch):
    s = config.Settings(provider="openai", model="m", base_url="http://x/v1",
                        api_key="dead", api_keys=("dead", "live"))
    used = []

    def fake_urlopen(req, timeout=600):
        auth = req.headers.get("Authorization", "")
        used.append(auth)
        if "dead" in auth:
            raise _http_429()
        return _ok_body()

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    out = oc._post(s, {"model": "m", "messages": []})
    assert out["choices"][0]["message"]["content"] == "hi"
    # It tried the dead key, then rotated to the live one.
    assert any("dead" in a for a in used) and used[-1].endswith("live")


def test_exhausted_key_is_remembered(monkeypatch):
    s = config.Settings(provider="openai", model="m", base_url="http://x/v1",
                        api_key="dead", api_keys=("dead", "live"))

    def fake_urlopen(req, timeout=600):
        if "dead" in req.headers.get("Authorization", ""):
            raise _http_429()
        return _ok_body()

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake_urlopen)
    oc._post(s, {"model": "m", "messages": []})
    # Second call should start straight from the healthy key (cursor advanced).
    calls = []
    monkeypatch.setattr(oc.urllib.request, "urlopen",
                        lambda req, timeout=600: (
                            calls.append(req.headers.get("Authorization")),
                            _ok_body())[1])
    oc._post(s, {"model": "m", "messages": []})
    assert calls[0].endswith("live")           # didn't re-hit the dead key first


def test_report_masks_and_counts(monkeypatch):
    s = config.Settings(provider="openai", model="m", base_url="http://x/v1",
                        api_key="sk-aaaaaa1111", api_keys=("sk-aaaaaa1111",
                                                           "sk-bbbbbb2222"))
    monkeypatch.setattr(oc.urllib.request, "urlopen",
                        lambda req, timeout=600: _ok_body())
    oc._post(s, {"model": "m", "messages": []})
    report = oc.rotation_report(s)
    assert "2 keys" in report
    assert "1111" in report and "sk-aaaaaa1111" not in report   # masked, not raw


def test_single_key_has_no_report():
    s = config.Settings(provider="openai", model="m", api_key="solo")
    assert oc.rotation_report(s) == ""


def test_all_keys_exhausted_raises(monkeypatch):
    s = config.Settings(provider="openai", model="m", base_url="http://x/v1",
                        api_key="k1", api_keys=("k1", "k2"))
    monkeypatch.setattr(oc.urllib.request, "urlopen",
                        lambda req, timeout=600: (_ for _ in ()).throw(_http_429()))
    with pytest.raises(RuntimeError):
        oc._post(s, {"model": "m", "messages": []})
