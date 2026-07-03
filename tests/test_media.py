"""Media tools: graceful no-key behavior, file output, link extraction, and
that browse_page is treated as untrusted ingestion."""

from olympus import media, security, tools


def test_generate_image_without_key_is_graceful(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLYMPUS_MEDIA_API_KEY", raising=False)
    out = media.generate_image("a logo")
    assert out.startswith("Error") and "API key" in out


def test_tts_without_key_is_graceful(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLYMPUS_MEDIA_API_KEY", raising=False)
    out = media.text_to_speech("hello")
    assert out.startswith("Error") and "API key" in out


def test_generate_image_saves_file(monkeypatch, tmp_path):
    import base64
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    png = base64.b64encode(b"\x89PNG fake").decode()
    monkeypatch.setattr(media, "_post",
                        lambda path, payload, timeout=120:
                        b'{"data":[{"b64_json":"%s"}]}' % png.encode())
    out = media.generate_image("a cat", filename="cat.png")
    assert "cat.png" in out
    assert (tmp_path / "ws" / "cat.png").read_bytes().startswith(b"\x89PNG")


def test_extract_links():
    html = ('<a href="https://a.com/x">A</a> <a href="/rel">rel</a> '
            '<a href="https://a.com/x">dup</a> <a href="https://b.com">B</a>')
    links = media.extract_links(html)
    assert links == ["https://a.com/x", "https://b.com"]


def test_browse_page_includes_text_and_links(monkeypatch):
    monkeypatch.setattr("olympus.tools._http_get",
                        lambda url: "<html><body>Hello <a href='https://x.com'>x</a>"
                                    "</body></html>")
    out = media.browse_page("https://site.com")
    assert "Hello" in out and "https://x.com" in out and "Links on this page" in out


def test_browse_page_handles_fetch_error(monkeypatch):
    def boom(url):
        raise RuntimeError("dns fail")
    monkeypatch.setattr("olympus.tools._http_get", boom)
    assert media.browse_page("https://x").startswith("Error fetching")


def test_browse_page_is_ingestion_and_wrapped():
    assert "browse_page" in security.INGESTION_TOOLS
    assert security.should_wrap("browse_page") is True


def test_media_tools_registered():
    for name in ("generate_image", "text_to_speech", "browse_page",
                 "analyze_image"):
        assert name in tools.HANDLERS
        assert name in tools.EXTRA_TOOLS


# --- analyze_image (the vision gap we closed vs Hermes) -------------------

def test_analyze_image_without_key_is_graceful(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLYMPUS_MEDIA_API_KEY", raising=False)
    out = media.analyze_image("https://x/pic.png")
    assert out.startswith("Error") and "API key" in out


def test_analyze_image_url_calls_vision_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    seen = {}

    def fake_post(path, payload, timeout=120):
        seen["path"] = path
        seen["content"] = payload["messages"][0]["content"]
        return b'{"choices":[{"message":{"content":"a red circle"}}]}'

    monkeypatch.setattr(media, "_post", fake_post)
    out = media.analyze_image("https://x/pic.png", "what shape?")
    assert out == "a red circle"
    assert seen["path"] == "/chat/completions"
    # URL is passed by reference (provider fetches it), not inlined.
    img = [c for c in seen["content"] if c["type"] == "image_url"][0]
    assert img["image_url"] == {"url": "https://x/pic.png"}


def test_analyze_image_workspace_file_is_inlined(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    (tmp_path / "shot.png").write_bytes(b"\x89PNG realish bytes")
    captured = {}

    def fake_post(path, payload, timeout=120):
        captured["content"] = payload["messages"][0]["content"]
        return b'{"choices":[{"message":{"content":"ok"}}]}'

    monkeypatch.setattr(media, "_post", fake_post)
    out = media.analyze_image("shot.png")
    assert out == "ok"
    img = [c for c in captured["content"] if c["type"] == "image_url"][0]
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


def test_analyze_image_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    out = media.analyze_image("nope.png")
    assert out.startswith("Error") and "no such image" in out


def test_analyze_image_rejects_unsupported_type(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    (tmp_path / "data.txt").write_text("not an image")
    out = media.analyze_image("data.txt")
    assert out.startswith("Error") and "unsupported image type" in out


def test_analyze_image_is_ingestion_and_wrapped():
    assert "analyze_image" in security.INGESTION_TOOLS
    assert security.should_wrap("analyze_image") is True
