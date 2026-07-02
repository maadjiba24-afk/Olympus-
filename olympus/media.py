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
STT_MODEL = os.environ.get("OLYMPUS_STT_MODEL", "whisper-1")
MAX_AUDIO_BYTES = 25 * 1024 * 1024      # OpenAI transcription upload limit


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
