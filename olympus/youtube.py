"""Fetch YouTube transcripts so Mnemosyne can 'watch' videos and learn."""

from __future__ import annotations

import re

VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})"
)

MAX_TRANSCRIPT_CHARS = 60_000


def video_id(url: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    match = VIDEO_ID_RE.search(url)
    if not match:
        raise ValueError(f"Could not extract a YouTube video id from: {url}")
    return match.group(1)


def fetch_transcript(url: str) -> str:
    """Return the full spoken transcript of a YouTube video as plain text."""
    vid = video_id(url)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as err:
        raise RuntimeError(
            "youtube-transcript-api is not installed; run `pip install -r requirements.txt`"
        ) from err

    try:
        # v1.x API
        fetched = YouTubeTranscriptApi().fetch(vid)
        parts = [snippet.text for snippet in fetched]
    except AttributeError:
        # pre-1.0 API
        data = YouTubeTranscriptApi.get_transcript(vid)  # type: ignore[attr-defined]
        parts = [item["text"] for item in data]

    text = " ".join(p.strip() for p in parts if p.strip())
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS] + "\n\n[transcript truncated]"
    return text or "(no transcript available for this video)"
