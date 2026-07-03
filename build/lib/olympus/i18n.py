"""Multilingual support.

Olympus's coordination layer (routing, planning, verification) runs in English
internally — it's cheaper and the JSON schemas are most reliable that way — but
every word the *user* sees must be in the user's own language, generated
natively rather than translated. This module builds the directives that enforce
that, and resolves the target language from an explicit per-user preference or
falls back to "match the user's message".
"""

from __future__ import annotations

from . import prefs

# Common preference phrasings → canonical language name, for the set-language
# command/tool. The model handles everything else; this is just a friendly map.
_ALIASES = {
    "english": "English", "en": "English",
    "spanish": "Spanish", "español": "Spanish", "es": "Spanish",
    "french": "French", "français": "French", "francais": "French", "fr": "French",
    "german": "German", "deutsch": "German", "de": "German",
    "portuguese": "Portuguese", "português": "Portuguese", "pt": "Portuguese",
    "italian": "Italian", "italiano": "Italian", "it": "Italian",
    "arabic": "Arabic", "العربية": "Arabic", "ar": "Arabic",
    "chinese": "Chinese", "中文": "Chinese", "mandarin": "Chinese", "zh": "Chinese",
    "japanese": "Japanese", "日本語": "Japanese", "ja": "Japanese",
    "korean": "Korean", "한국어": "Korean", "ko": "Korean",
    "hindi": "Hindi", "हिन्दी": "Hindi", "hi": "Hindi",
    "russian": "Russian", "русский": "Russian", "ru": "Russian",
    "turkish": "Turkish", "türkçe": "Turkish", "tr": "Turkish",
    "dutch": "Dutch", "nederlands": "Dutch", "nl": "Dutch",
    "swahili": "Swahili", "kiswahili": "Swahili", "sw": "Swahili",
    "auto": None, "automatic": None, "match": None, "detect": None,
}


def normalize(value: str) -> str | None:
    """Map a user-typed language to a canonical name; None means auto-detect."""
    v = (value or "").strip().lower()
    if v in _ALIASES:
        return _ALIASES[v]
    # Unknown but non-empty: trust the user's spelling (title-cased).
    return value.strip().title() or None


def get_preference(user: str) -> str | None:
    return prefs.get(user, "language")


def set_preference(user: str, value: str) -> str:
    lang = normalize(value)
    prefs.set(user, "language", lang)
    if lang is None:
        return "Language set to automatic — I'll match the language you write in."
    return f"Language preference set to {lang}. I'll reply in {lang} from now on."


def directive(user: str) -> str:
    """The instruction appended to every user-facing generation step."""
    lang = get_preference(user)
    if lang:
        return (f"\n\nLANGUAGE: Write your entire reply to the user in {lang}. "
                "Generate it natively in that language (do not write in English "
                "then translate). Keep code, commands, and proper nouns in their "
                "conventional form.")
    return ("\n\nLANGUAGE: Write your entire reply in the SAME language the user "
            "used in their most recent message (and the conversation so far). "
            "Generate it natively in that language — never answer in English if "
            "the user wrote in another language. Keep code, commands, and proper "
            "nouns in their conventional form.")


def content_directive(user: str) -> str:
    """Shorter directive for specialist tasks that produce user-facing content
    (marketing copy, social posts, messages) so they're authored natively."""
    lang = get_preference(user)
    target = lang or "the user's own language (match the brief / conversation)"
    return (f"\n\nIf this task produces text the end user will read, write that "
            f"text in {target}, authored natively for that language and its "
            "cultural context — not translated from English.")
