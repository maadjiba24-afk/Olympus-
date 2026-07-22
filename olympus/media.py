"""Media tools — image generation, text-to-speech, and richer browsing.

Olympus could read the web but couldn't *produce* media or browse a page
structurally. These tools close that gap (the kind of toolset Hermes ships:
image gen, TTS, browser):

  * ``generate_image`` — create an image from a prompt (OpenAI-compatible
    images API) and save it into the confined workspace.
  * ``text_to_speech`` — synthesize speech to an audio file in the workspace.
  * ``browse_page``    — fetch a page as readable text *and* extract its links,
    so a specialist can navigate, not just read one URL.

Everything is urllib-only and **degrades gracefully**: with no API key the
generative tools return a clear, non-fatal message instead of raising, so a run
never crashes just because media credentials aren't configured. Generated files
land in the sandbox workspace (confined, same as run_command/write_file).
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.request

from . import sandbox

IMAGE_MODEL = os.environ.get("OLYMPUS_IMAGE_MODEL", "gpt-image-1")
TTS_MODEL = os.environ.get("OLYMPUS_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("OLYMPUS_TTS_VOICE", "alloy")
VISION_MODEL = os.environ.get("OLYMPUS_VISION_MODEL", "gpt-4o-mini")
STT_MODEL = os.environ.get("OLYMPUS_STT_MODEL", "whisper-1")
MAX_AUDIO_BYTES = 25 * 1024 * 1024      # OpenAI transcription upload limit

# Cap how big an image we'll inline as a data URL (base64 bloats ~1.33x, and a
# huge upload just wastes tokens and time). 8 MB of source bytes is generous.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}


def _api_key() -> str:
    return (os.environ.get("OPENAI_API_KEY")
            or os.environ.get("OLYMPUS_MEDIA_API_KEY") or "")


def _base() -> str:
    return os.environ.get("OLYMPUS_MEDIA_BASE_URL",
                          "https://api.openai.com/v1").rstrip("/")


def _post(path: str, payload: dict, timeout: int = 120) -> bytes:
    req = urllib.request.Request(
        f"{_base()}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_api_key()}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def generate_image(prompt: str, filename: str = "") -> str:
    """Generate an image and save it in the workspace. Returns a status string."""
    if not _api_key():
        return ("Error: image generation needs an API key "
                "(set OPENAI_API_KEY or OLYMPUS_MEDIA_API_KEY).")
    try:
        raw = _post("/images/generations",
                    {"model": IMAGE_MODEL, "prompt": prompt,
                     "n": 1, "size": "1024x1024"})
        data = json.loads(raw)
        b64 = data["data"][0]["b64_json"]
    except Exception as err:
        return f"Error generating image: {str(err)[:200]}"
    name = filename or f"image-{int(time.time())}.png"
    res = sandbox.write_file(name, "")             # confine + create the path
    with open(res["path"], "wb") as f:
        f.write(base64.b64decode(b64))
    return f"Image saved to workspace: {name}"


def _post_multipart_files(path: str, fields: dict, files: list[tuple],
                          timeout: int = 180) -> bytes:
    """POST multipart/form-data with one or more typed file parts (for the
    images/edits endpoint). `files` is [(field, filename, content_type, bytes)].
    Distinct from `_post_multipart` (the single-octet-stream audio uploader) —
    they must not share a name or the later definition would shadow this one.
    urllib only — no deps."""
    boundary = "----olympus" + base64.urlsafe_b64encode(
        os.urandom(9)).decode().rstrip("=")
    crlf = b"\r\n"
    body = bytearray()
    for k, v in fields.items():
        body += b"--" + boundary.encode() + crlf
        body += f'Content-Disposition: form-data; name="{k}"'.encode() + crlf + crlf
        body += str(v).encode() + crlf
    for field, fname, ctype, blob in files:
        body += b"--" + boundary.encode() + crlf
        body += (f'Content-Disposition: form-data; name="{field}"; '
                 f'filename="{fname}"').encode() + crlf
        body += f"Content-Type: {ctype}".encode() + crlf + crlf
        body += blob + crlf
    body += b"--" + boundary.encode() + b"--" + crlf
    req = urllib.request.Request(
        f"{_base()}{path}", data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {_api_key()}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def edit_image(prompt: str, source: str, filename: str = "") -> str:
    """Edit an existing workspace image by prompt (AI image edit) and save the
    result as a new workspace image. `source` is a workspace image name; the
    original is left untouched. Returns a status string; degrades gracefully
    without a key or a valid source."""
    if not _api_key():
        return ("Error: image editing needs an API key "
                "(set OPENAI_API_KEY or OLYMPUS_MEDIA_API_KEY).")
    try:
        src = sandbox._confine(source)
    except ValueError:
        return f"Error: '{source}' is outside the workspace."
    ext = src.suffix.lower()
    if not src.is_file() or ext not in _IMAGE_EXTS:
        return f"Error: no workspace image named '{source}'."
    try:
        blob = src.read_bytes()
        if len(blob) > _MAX_IMAGE_BYTES:
            return f"Error: '{source}' is too large to edit."
        raw = _post_multipart_files("/images/edits", {
            "model": IMAGE_MODEL, "prompt": prompt, "n": 1,
        }, [("image", src.name, _IMAGE_EXTS[ext], blob)])
        data = json.loads(raw)
        b64 = data["data"][0]["b64_json"]
    except Exception as err:
        return f"Error editing image: {str(err)[:200]}"
    name = filename or f"{src.stem}-edited-{int(time.time())}.png"
    res = sandbox.write_file(name, "")
    with open(res["path"], "wb") as f:
        f.write(base64.b64decode(b64))
    return f"Edited image saved to workspace: {name}"


def _post_multipart(path: str, fields: dict[str, str],
                    file_field: str, filename: str, blob: bytes,
                    timeout: int = 120) -> bytes:
    """multipart/form-data POST (stdlib-only) — audio uploads need it."""
    import uuid
    boundary = f"olympus-{uuid.uuid4().hex}"
    body = bytearray()
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{k}"\r\n\r\n{v}\r\n').encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="{file_field}"; filename="{filename}"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n").encode()
    body += blob + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{_base()}{path}", data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {_api_key()}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def transcribe_audio(path: str) -> str:
    """Transcribe an audio file (voice note, recording) from the workspace.
    Returns the transcript text, or a clear non-fatal error message."""
    if not _api_key():
        return ("Error: transcription needs an API key "
                "(set OPENAI_API_KEY or OLYMPUS_MEDIA_API_KEY).")
    blob = _read_audio(path)
    if isinstance(blob, str):
        return blob                                    # error message
    try:
        raw = _post_multipart("/audio/transcriptions", {"model": STT_MODEL},
                              "file", os.path.basename(path) or "audio.ogg",
                              blob)
        data = json.loads(raw)
    except Exception as err:
        return f"Error transcribing audio: {str(err)[:200]}"
    text = (data.get("text") or "").strip()
    return text or "Error transcribing audio: the provider returned no text."


def _read_audio(path: str) -> bytes | str:
    """Read an audio file from the confined workspace (bytes), or an error."""
    try:
        target = sandbox._confine(path)
    except ValueError as err:
        return f"Error: {err}"
    if not target.is_file():
        return f"Error: no such audio file in workspace: {path}"
    blob = target.read_bytes()
    if len(blob) > MAX_AUDIO_BYTES:
        return (f"Error: audio file is {len(blob) // (1024 * 1024)} MB — the "
                f"transcription limit is {MAX_AUDIO_BYTES // (1024 * 1024)} MB.")
    return blob


def transcribe_bytes(blob: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe raw audio bytes (a gateway voice note) without touching the
    workspace. Same graceful degradation as transcribe_audio."""
    if not _api_key():
        return ("Error: transcription needs an API key "
                "(set OPENAI_API_KEY or OLYMPUS_MEDIA_API_KEY).")
    if len(blob) > MAX_AUDIO_BYTES:
        return "Error: voice note is too large to transcribe."
    try:
        raw = _post_multipart("/audio/transcriptions", {"model": STT_MODEL},
                              "file", filename, blob)
        data = json.loads(raw)
    except Exception as err:
        return f"Error transcribing audio: {str(err)[:200]}"
    text = (data.get("text") or "").strip()
    return text or "Error transcribing audio: the provider returned no text."


def text_to_speech(text: str, filename: str = "") -> str:
    """Synthesize speech to an audio file in the workspace."""
    if not _api_key():
        return ("Error: text-to-speech needs an API key "
                "(set OPENAI_API_KEY or OLYMPUS_MEDIA_API_KEY).")
    try:
        audio = _post("/audio/speech",
                      {"model": TTS_MODEL, "voice": TTS_VOICE, "input": text})
    except Exception as err:
        return f"Error synthesizing speech: {str(err)[:200]}"
    name = filename or f"speech-{int(time.time())}.mp3"
    res = sandbox.write_file(name, "")
    with open(res["path"], "wb") as f:
        f.write(audio)
    return f"Audio saved to workspace: {name}"


def _image_source(image: str) -> dict | str:
    """Resolve an image reference to an OpenAI-compat `image_url` value.

    * http(s) URL → passed through by reference (the provider fetches it).
    * workspace file → read, size-checked, and inlined as a base64 data URL.
    Returns a `{"url": ...}` dict on success, or an error string to surface.
    """
    ref = (image or "").strip()
    if ref.startswith(("http://", "https://")):
        return {"url": ref}
    try:
        target = sandbox._confine(ref)
    except ValueError as err:
        return f"Error: {err}"
    if not target.is_file():
        return f"Error: no such image in workspace: {ref}"
    mime = _IMAGE_EXTS.get(target.suffix.lower())
    if not mime:
        return (f"Error: unsupported image type '{target.suffix}' "
                f"(expected one of {', '.join(sorted(_IMAGE_EXTS))}).")
    raw = target.read_bytes()
    if len(raw) > _MAX_IMAGE_BYTES:
        return (f"Error: image is {len(raw) // 1024} KB, over the "
                f"{_MAX_IMAGE_BYTES // (1024 * 1024)} MB inline limit.")
    b64 = base64.b64encode(raw).decode()
    return {"url": f"data:{mime};base64,{b64}"}


def analyze_image(image: str, question: str = "") -> str:
    """Describe or answer a question about an image using a vision-capable model.

    `image` is either an http(s) URL or a filename in the confined workspace.
    Fills the one real capability gap vs Hermes: Olympus could *generate* images
    but never *read* them. The model's answer is external content, so callers
    wrap it as untrusted (analyze_image is an INGESTION tool).
    """
    if not _api_key():
        return ("Error: image analysis needs an API key "
                "(set OPENAI_API_KEY or OLYMPUS_MEDIA_API_KEY).")
    src = _image_source(image)
    if isinstance(src, str):        # an error message
        return src
    return _vision_describe(src, question)


def analyze_image_data(b64: str, question: str = "",
                       mime: str = "image/png") -> str:
    """Describe a base64-encoded image passed in memory (e.g. a browser
    screenshot) — no workspace file needed. Same untrusted-content contract as
    analyze_image: the answer is external content and callers wrap it."""
    if not _api_key():
        return ("Error: image analysis needs an API key "
                "(set OPENAI_API_KEY or OLYMPUS_MEDIA_API_KEY).")
    if not b64:
        return "Error: no image data to analyze."
    try:
        nbytes = len(base64.b64decode(b64, validate=False))
    except Exception:
        return "Error: image data is not valid base64."
    if nbytes > _MAX_IMAGE_BYTES:
        return (f"Error: image is {nbytes // 1024} KB, over the "
                f"{_MAX_IMAGE_BYTES // (1024 * 1024)} MB inline limit.")
    return _vision_describe({"url": f"data:{mime};base64,{b64}"}, question)


def _vision_describe(src: dict, question: str = "") -> str:
    """Shared vision call: send `src` (an OpenAI-compat image_url value) to the
    vision model with a describe prompt. Returns the model's text or an error."""
    prompt = (question or "").strip() or (
        "Describe this image in detail: what it shows, any text present, and "
        "anything notable.")
    try:
        raw = _post("/chat/completions", {
            "model": VISION_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": src},
            ]}],
            "max_tokens": 1000,
        })
        data = json.loads(raw)
        answer = (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as err:
        return f"Error analyzing image: {str(err)[:200]}"
    return answer or "(the vision model returned no description)"


_LINK_RE = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\']', re.IGNORECASE)


def extract_links(html: str, limit: int = 30) -> list[str]:
    """Pull http(s) hrefs from raw HTML, de-duplicated, in document order."""
    seen, out = set(), []
    for href in _LINK_RE.findall(html or ""):
        if href.startswith(("http://", "https://")) and href not in seen:
            seen.add(href)
            out.append(href)
        if len(out) >= limit:
            break
    return out


def browse_page(url: str) -> str:
    """Fetch a page as clean readable markdown and list its links — structural
    browsing. Content quality comes from `webctx.to_markdown` (readability-grade,
    zero-dependency); the fetch still routes through the SSRF/egress-gated
    `tools._http_get`, and the result is wrapped untrusted like any ingestion."""
    from . import tools, webctx
    try:
        html = tools._http_get(url)
    except Exception as err:
        return f"Error fetching {url}: {str(err)[:200]}"
    md, links, title = webctx.to_markdown(html, base_url=url)
    out = [f"# {title or url}", f"<{url}>", "", md[:8000]]
    if links:
        out.append("\n## Links on this page")
        out += [f"- {link}" for link in links]
    return "\n".join(out)


# --- deep crawl (bounded BFS over the gated fetcher) ------------------------

_CRAWL_MAX_DEPTH = 3
_CRAWL_MAX_PAGES = 25
_CRAWL_BYTE_CAP = 200_000
_CRAWL_PER_PAGE = 6000


def crawl_site(url: str, depth: int = 1, max_pages: int = 10,
               same_domain: bool = True, include: list | None = None,
               exclude: list | None = None) -> str:
    """Recursively crawl from `url` and return a concatenated markdown digest of
    the pages visited. The traversal + clean per-page markdown now come from
    `webctx.crawl`; EVERY hop still routes through tools._http_get, so the
    SSRF/egress + redirect re-check gate applies per URL. Bounded by page count,
    link depth and aggregate bytes; optional `include`/`exclude` glob-filter the
    followed links."""
    from . import webctx
    r = webctx.crawl(url, depth=depth, max_pages=max_pages, include=include,
                     exclude=exclude, same_domain=same_domain,
                     formats=("markdown",))
    header = (f"Crawled {r['count']} page(s) from {url} "
              f"(depth≤{r['depth']}, same_domain={r['same_domain']}).")
    if r.get("truncated"):
        header += (" Stopped early at a bound (max_pages / depth / byte cap); "
                   "not every reachable page was visited.")
    pages = []
    for p in r["pages"]:
        if p.get("error"):
            pages.append(f"## {p['url']}\n[error: {p['error']}]")
        else:
            pages.append(f"## {p['url']}\n\n{p.get('markdown', '')}")
    return "\n\n---\n\n".join([header] + pages)


# --- data visualization (pure-Python SVG; zero new dependency) --------------

_CHART_TYPES = ("bar", "line", "scatter", "pie")
_CHART_MAX_POINTS = 500      # enough for any readable chart; bounds SVG size


def _parse_csv(data: str):
    """Return (header, rows) from inline CSV text or a workspace CSV file. A
    bare filename (or a workspace-relative path that exists) is read from the
    confined workspace; anything else is treated as inline CSV."""
    import csv
    import io

    text = data
    looks_like_path = ("\n" not in data.strip()) and (
        data.strip().endswith(".csv") or "/" in data.strip())
    if looks_like_path:
        try:
            target = sandbox._confine(data.strip())
            if target.is_file():
                text = target.read_text(encoding="utf-8")
        except Exception:
            text = data
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _svg_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_CHART_PALETTE = ("#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1",
                  "#76b7b2", "#edc948", "#ff9da7", "#9c755f", "#bab0ac")


def _render_chart_svg(chart_type, labels, values, title, xlabel, ylabel):
    """Render a minimal, self-contained SVG chart. No external libraries."""
    W, H = 720, 420
    pad_l, pad_r, pad_t, pad_b = 60, 24, 48 if title else 24, 60
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="12">',
             f'<rect width="{W}" height="{H}" fill="white"/>']
    if title:
        parts.append(f'<text x="{W/2}" y="24" text-anchor="middle" '
                     f'font-size="16" font-weight="bold">{_svg_escape(title)}</text>')

    if chart_type == "pie":
        import math
        total = sum(v for v in values if v > 0) or 1.0
        cx, cy, r = W / 2, pad_t + plot_h / 2, min(plot_w, plot_h) / 2
        angle = -math.pi / 2
        for i, (lab, val) in enumerate(zip(labels, values)):
            if val <= 0:
                continue
            sweep = 2 * math.pi * (val / total)
            x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
            angle += sweep
            x2, y2 = cx + r * math.cos(angle), cy + r * math.sin(angle)
            large = 1 if sweep > math.pi else 0
            color = _CHART_PALETTE[i % len(_CHART_PALETTE)]
            parts.append(f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} '
                         f'A{r:.1f},{r:.1f} 0 {large},1 {x2:.1f},{y2:.1f} Z" '
                         f'fill="{color}"/>')
            ly = pad_t + i * 18
            parts.append(f'<rect x="{W-pad_r-120}" y="{ly}" width="10" '
                         f'height="10" fill="{color}"/>')
            parts.append(f'<text x="{W-pad_r-104}" y="{ly+9}">'
                         f'{_svg_escape(lab)} ({val:g})</text>')
        parts.append("</svg>")
        return "\n".join(parts)

    # cartesian charts (bar / line / scatter)
    vmax = max(values) if values else 1.0
    vmin = min(values + [0.0]) if values else 0.0
    span = (vmax - vmin) or 1.0
    x0, y0 = pad_l, pad_t + plot_h
    # axes
    parts.append(f'<line x1="{x0}" y1="{pad_t}" x2="{x0}" y2="{y0}" stroke="#888"/>')
    parts.append(f'<line x1="{x0}" y1="{y0}" x2="{pad_l+plot_w}" y2="{y0}" stroke="#888"/>')
    # y gridlines/labels (0 and max)
    for frac in (0.0, 0.5, 1.0):
        gy = y0 - frac * plot_h
        val = vmin + frac * span
        parts.append(f'<line x1="{x0}" y1="{gy:.1f}" x2="{pad_l+plot_w}" '
                     f'y2="{gy:.1f}" stroke="#eee"/>')
        parts.append(f'<text x="{x0-6}" y="{gy+4:.1f}" text-anchor="end">{val:g}</text>')
    n = len(values)
    if n:
        slot = plot_w / n
        def px(i):
            return x0 + slot * (i + 0.5)
        def py(v):
            return y0 - ((v - vmin) / span) * plot_h
        if chart_type == "bar":
            bw = slot * 0.6
            for i, (lab, val) in enumerate(zip(labels, values)):
                bh = ((val - vmin) / span) * plot_h
                color = _CHART_PALETTE[i % len(_CHART_PALETTE)]
                parts.append(f'<rect x="{px(i)-bw/2:.1f}" y="{y0-bh:.1f}" '
                             f'width="{bw:.1f}" height="{bh:.1f}" fill="{color}"/>')
        elif chart_type == "line":
            pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(values))
            parts.append(f'<polyline points="{pts}" fill="none" '
                         f'stroke="{_CHART_PALETTE[0]}" stroke-width="2"/>')
        elif chart_type == "scatter":
            for i, v in enumerate(values):
                parts.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3.5" '
                             f'fill="{_CHART_PALETTE[0]}"/>')
        # x labels (thinned to avoid overlap)
        step = max(1, n // 12)
        for i, lab in enumerate(labels):
            if i % step:
                continue
            # Truncate the RAW label before escaping — slicing an escaped string
            # could cut an entity like &lt; into invalid XML.
            parts.append(f'<text x="{px(i):.1f}" y="{y0+16}" text-anchor="middle">'
                         f'{_svg_escape(lab[:14])}</text>')
    if xlabel:
        parts.append(f'<text x="{pad_l+plot_w/2}" y="{H-6}" text-anchor="middle">'
                     f'{_svg_escape(xlabel)}</text>')
    if ylabel:
        parts.append(f'<text x="14" y="{pad_t+plot_h/2}" text-anchor="middle" '
                     f'transform="rotate(-90 14 {pad_t+plot_h/2})">'
                     f'{_svg_escape(ylabel)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def chart_from_data(data: str, chart_type: str = "bar", x: str = "", y: str = "",
                    title: str = "", filename: str = "") -> str:
    """Render a chart from tabular data and save it into the workspace as SVG.

    `data` is inline CSV (a header row + rows) or the name of a CSV file in the
    workspace. `x`/`y` name the label and value columns (default: first two
    columns). Pure-Python SVG — no matplotlib/pandas needed — so it works on the
    minimal install. Returns a status string naming the saved file."""
    ctype = (chart_type or "bar").lower()
    if ctype not in _CHART_TYPES:
        return (f"Error: unknown chart_type '{chart_type}'. "
                f"Use one of: {', '.join(_CHART_TYPES)}.")
    header, rows = _parse_csv(data)
    if not header or not rows:
        return "Error: no tabular data found (expected CSV with a header row)."

    def _col_index(name, default):
        if name and name in header:
            return header.index(name)
        return default
    xi = _col_index(x, 0)
    yi = _col_index(y, 1 if len(header) > 1 else 0)

    import math
    labels, values = [], []
    dropped_nonfinite = 0
    for r in rows:
        if len(labels) >= _CHART_MAX_POINTS:
            break
        if xi >= len(r) or yi >= len(r):
            continue
        try:
            v = float(str(r[yi]).replace(",", "").strip())
        except ValueError:
            continue
        # Reject NaN/±inf — they poison the axis math and emit invalid SVG
        # coordinates that break renderers.
        if not math.isfinite(v):
            dropped_nonfinite += 1
            continue
        values.append(v)
        labels.append(str(r[xi]).strip())
    if not values:
        extra = (" (all values were non-finite)" if dropped_nonfinite else "")
        return (f"Error: column '{y or header[yi]}' has no finite numeric "
                f"values to plot{extra}.")
    capped = len(rows) > _CHART_MAX_POINTS

    svg = _render_chart_svg(ctype, labels, values, title,
                            x or header[xi], y or (header[yi] if yi < len(header) else ""))
    name = filename or f"chart-{int(time.time())}.svg"
    if not name.lower().endswith(".svg"):
        name += ".svg"
    sandbox.write_file(name, svg)                    # confine + write
    note = (f" (capped at the first {_CHART_MAX_POINTS} rows)" if capped else "")
    return (f"Chart ({ctype}, {len(values)} points) saved to workspace: "
            f"{name}{note}")
