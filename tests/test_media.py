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


def test_edit_image_without_key_is_graceful(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLYMPUS_MEDIA_API_KEY", raising=False)
    out = media.edit_image("make it blue", "cat.png")
    assert out.startswith("Error") and "API key" in out


def test_edit_image_missing_source_is_graceful(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    out = media.edit_image("make it blue", "nope.png")
    assert out.startswith("Error") and "no workspace image" in out


def test_edit_image_rejects_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    out = media.edit_image("x", "../secret.png")
    assert out.startswith("Error") and "outside the workspace" in out


def test_edit_image_saves_new_file_leaving_source(monkeypatch, tmp_path):
    # Patch at the urlopen level (NOT the multipart helper) so the REAL call
    # path runs end-to-end — this is what catches a _post_multipart signature
    # collision, which patching the helper by name would mask.
    import base64
    import urllib.request
    ws = tmp_path / "ws"
    ws.mkdir()
    src = ws / "cat.png"
    src.write_bytes(b"\x89PNG original")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(ws))
    png = base64.b64encode(b"\x89PNG edited").decode()
    captured = {}

    class Resp:
        def __init__(self, body): self._b = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._b

    def fake_urlopen(req, timeout=180):
        captured["url"] = req.full_url
        captured["ctype"] = req.headers.get("Content-type", "")
        captured["len"] = len(req.data)
        return Resp(b'{"data":[{"b64_json":"%s"}]}' % png.encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = media.edit_image("make it blue", "cat.png", filename="blue.png")
    assert "blue.png" in out, out            # NOT an "Error editing image: ..." string
    assert captured["url"].endswith("/images/edits")
    assert captured["ctype"].startswith("multipart/form-data; boundary=")
    assert captured["len"] > len(b"\x89PNG original")   # multipart wrapped the file
    # source untouched, new file written
    assert src.read_bytes() == b"\x89PNG original"
    assert (ws / "blue.png").read_bytes() == b"\x89PNG edited"


def test_post_multipart_names_do_not_collide():
    # Regression for the shadowing bug: the images uploader and the audio
    # uploader must be distinct callables with their own signatures.
    import inspect
    assert media._post_multipart_files is not media._post_multipart
    files_params = list(inspect.signature(media._post_multipart_files).parameters)
    audio_params = list(inspect.signature(media._post_multipart).parameters)
    assert "files" in files_params            # list-of-typed-files variant
    assert "blob" in audio_params             # single-octet-stream variant


def test_edit_image_tool_registered_and_on_specialists():
    from olympus import tools, specialists
    assert "edit_image" in tools.EXTRA_TOOLS and "edit_image" in tools.HANDLERS
    assert "edit_image" in specialists.SPECIALISTS["peitho"].extra_tools
    assert "edit_image" in specialists.SPECIALISTS["iris"].extra_tools


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


# --- speech-to-text (Hermes v0.3/v0.9 voice input) --------------------------

def test_transcribe_needs_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLYMPUS_MEDIA_API_KEY", raising=False)
    from olympus import media
    assert "API key" in media.transcribe_audio("note.ogg")
    assert "API key" in media.transcribe_bytes(b"xxx")


def test_transcribe_audio_from_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    from olympus import media
    (tmp_path / "note.ogg").write_bytes(b"fake-audio")
    seen = {}

    def fake_multipart(path, fields, file_field, filename, blob, timeout=120):
        seen.update(path=path, model=fields["model"], blob=blob)
        return b'{"text": "hello from the voice note"}'

    monkeypatch.setattr(media, "_post_multipart", fake_multipart)
    out = media.transcribe_audio("note.ogg")
    assert out == "hello from the voice note"
    assert seen["path"] == "/audio/transcriptions"
    assert seen["blob"] == b"fake-audio"


def test_transcribe_audio_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    from olympus import media
    assert "no such audio file" in media.transcribe_audio("nope.ogg")


def test_transcribe_audio_confined(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    from olympus import media
    assert "escapes the workspace" in media.transcribe_audio("../../etc/passwd")


def test_transcribe_tool_registered_and_ingestion():
    from olympus import security, tools
    assert "transcribe_audio" in tools.HANDLERS
    assert "transcribe_audio" in tools.EXTRA_TOOLS
    assert "transcribe_audio" in security.INGESTION_TOOLS


def test_whatsapp_voice_note_becomes_text():
    from olympus import whatsapp
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"type": "audio", "from": "15550001111",
         "audio": {"id": "MEDIA1"}, "id": "wamid.1"}]}}]}]}
    out = whatsapp.extract_messages(
        payload, transcribe=lambda mid: f"[voice note] hi ({mid})")
    assert out == [("15550001111", "[voice note] hi (MEDIA1)", "wamid.1")]


# --- crawl_site: bounded BFS over the gated fetcher -------------------------

def _fake_web(pages):
    """Return an _http_get stand-in serving a fixed {url: html} map."""
    def _get(url, timeout=30):
        if url not in pages:
            raise ValueError(f"404 {url}")
        return pages[url]
    return _get


def test_crawl_site_follows_links_within_bounds(monkeypatch):
    pages = {
        "https://site.test/": '<a href="https://site.test/a">a</a>'
                              '<a href="https://site.test/b">b</a> home',
        "https://site.test/a": "page A body",
        "https://site.test/b": "page B body",
    }
    monkeypatch.setattr(tools, "_http_get", _fake_web(pages))
    out = media.crawl_site("https://site.test/", depth=1, max_pages=10)
    assert "page A body" in out and "page B body" in out
    assert "https://site.test/a" in out


def test_crawl_site_respects_same_domain(monkeypatch):
    pages = {
        "https://site.test/": '<a href="https://evil.test/x">x</a> home',
        "https://evil.test/x": "SHOULD NOT BE FETCHED",
    }
    monkeypatch.setattr(tools, "_http_get", _fake_web(pages))
    out = media.crawl_site("https://site.test/", depth=1, same_domain=True)
    assert "SHOULD NOT BE FETCHED" not in out


def test_crawl_site_honors_max_pages(monkeypatch):
    pages = {"https://s.test/": "".join(
        f'<a href="https://s.test/{i}">{i}</a>' for i in range(20))}
    for i in range(20):
        pages[f"https://s.test/{i}"] = f"body {i}"
    monkeypatch.setattr(tools, "_http_get", _fake_web(pages))
    out = media.crawl_site("https://s.test/", depth=1, max_pages=3)
    # start page + at most 2 more = 3 fetched
    assert out.count("\n## ") <= 3
    assert "Stopped early" in out


def test_crawl_site_bad_hop_is_nonfatal(monkeypatch):
    pages = {"https://s.test/": '<a href="https://s.test/missing">m</a> ok'}
    monkeypatch.setattr(tools, "_http_get", _fake_web(pages))
    out = media.crawl_site("https://s.test/", depth=1)
    assert "[error:" in out and "ok" in out          # start page still returned


def test_crawl_site_is_untrusted_ingestion():
    assert "crawl_site" in security.INGESTION_TOOLS
    assert security.should_wrap("crawl_site") is True


# --- chart_from_data: pure-Python SVG ---------------------------------------

def test_chart_from_data_writes_svg(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    from olympus import sandbox
    csv = "fruit,count\napples,5\nbananas,9\ncherries,3\n"
    out = media.chart_from_data(csv, chart_type="bar", title="Fruit", filename="f")
    assert "f.svg" in out
    svg = sandbox.read_file("f.svg")
    assert svg.startswith("<svg") and "</svg>" in svg


def test_chart_from_data_all_types(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    csv = "k,v\na,1\nb,2\nc,3\n"
    for ct in ("bar", "line", "scatter", "pie"):
        assert "saved" in media.chart_from_data(csv, chart_type=ct, filename=ct)


def test_chart_from_data_escapes_labels(monkeypatch, tmp_path):
    # A crafted label must not inject raw markup into the SVG (XSS-in-SVG).
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    from olympus import sandbox
    csv = 'name,v\n<script>x</script>,5\nok,3\n'
    media.chart_from_data(csv, chart_type="bar", filename="x")
    svg = sandbox.read_file("x.svg")
    assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_chart_from_data_error_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    assert media.chart_from_data("", ).startswith("Error")
    assert "unknown chart_type" in media.chart_from_data("a,b\n1,2\n", chart_type="pyramid")
    assert "numeric" in media.chart_from_data("a,b\nx,y\n")


def test_chart_from_data_reads_workspace_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    from olympus import sandbox
    sandbox.write_file("data.csv", "m,v\njan,10\nfeb,20\n")
    out = media.chart_from_data("data.csv", chart_type="line", filename="c")
    assert "2 points" in out


def test_chart_from_data_is_trusted_first_party():
    assert "chart_from_data" in security.TRUSTED_TOOLS
    assert security.should_wrap("chart_from_data") is False


def test_chart_rejects_non_finite_values(monkeypatch, tmp_path):
    # NaN/inf must not reach the SVG (they emit invalid coordinates).
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    from olympus import sandbox
    out = media.chart_from_data("k,v\na,inf\nb,nan\nc,3\n", chart_type="bar",
                                filename="nf")
    assert "1 points" in out                       # only the finite row survives
    svg = sandbox.read_file("nf.svg")
    assert "nan" not in svg.lower() and "inf" not in svg.lower()
    assert media.chart_from_data("k,v\na,inf\nb,nan\n").startswith("Error")


def test_chart_caps_row_count(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    big = "k,v\n" + "\n".join(f"r{i},{i}" for i in range(2000))
    out = media.chart_from_data(big, chart_type="line", filename="big")
    assert f"{media._CHART_MAX_POINTS} points" in out and "capped" in out
