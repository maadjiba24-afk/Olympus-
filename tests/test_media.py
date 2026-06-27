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
    for name in ("generate_image", "text_to_speech", "browse_page"):
        assert name in tools.HANDLERS
        assert name in tools.EXTRA_TOOLS
