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
    """Fetch a page as readable text and list its links — structural browsing."""
    from . import tools
    try:
        html = tools._http_get(url)
    except Exception as err:
        return f"Error fetching {url}: {str(err)[:200]}"
    text = tools._strip_html(html)[:8000]
    links = extract_links(html)
    out = [f"# {url}", "", text]
    if links:
        out.append("\n## Links on this page")
        out += [f"- {link}" for link in links]
    return "\n".join(out)
