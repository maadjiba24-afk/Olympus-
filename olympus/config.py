"""Central configuration for Olympus."""

import os
from dataclasses import dataclass
from pathlib import Path

# Default model for the Anthropic backend. Opus 4.8 supports adaptive
# thinking, effort control, and the server-side web_search/web_fetch tools.
MODEL = os.environ.get("OLYMPUS_MODEL", "claude-opus-4-8")


@dataclass(frozen=True)
class Settings:
    """Provider settings — resolvable from env, or brought per-request (BYOK).

    provider:
        "anthropic"  Claude via the official SDK (full capability: adaptive
                     thinking, effort, server-side web search, caching).
        "openai"     any OpenAI-compatible /chat/completions endpoint —
                     OpenAI, Gemini, DeepSeek, Groq, OpenRouter, Ollama, ...
                     Web access falls back to a built-in client-side search.
    """

    provider: str = "anthropic"
    model: str = "claude-opus-4-8"
    api_key: str | None = None
    base_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        provider = os.environ.get("OLYMPUS_PROVIDER", "anthropic").lower()
        if provider == "anthropic":
            model = os.environ.get("OLYMPUS_MODEL", "claude-opus-4-8")
            key = os.environ.get("ANTHROPIC_API_KEY")
        else:
            model = os.environ.get("OLYMPUS_MODEL", "")
            key = (os.environ.get("OLYMPUS_API_KEY")
                   or os.environ.get("OPENAI_API_KEY"))
        return cls(
            provider=provider,
            model=model,
            api_key=key,
            base_url=os.environ.get("OLYMPUS_BASE_URL"),
        )

    def merged(self, overrides: dict) -> "Settings":
        """Apply non-empty override fields (used by BYOK interfaces)."""
        clean = {k: v.strip() for k, v in (overrides or {}).items()
                 if k in {"provider", "model", "api_key", "base_url"}
                 and isinstance(v, str) and v.strip()}
        if not clean:
            return self
        merged = {**self.__dict__, **clean}
        merged["provider"] = merged["provider"].lower()
        if merged["provider"] != self.provider:
            # Provider switch: never carry the old provider's model, key, or
            # endpoint across — that would send credentials to the wrong host.
            if "model" not in clean:
                merged["model"] = ("claude-opus-4-8"
                                   if merged["provider"] == "anthropic" else "")
            if "api_key" not in clean:
                merged["api_key"] = None
            if "base_url" not in clean:
                merged["base_url"] = None
        return Settings(**merged)

    def validate(self) -> str | None:
        """Return an error message if unusable, else None."""
        if self.provider not in ("anthropic", "openai"):
            return f"Unknown provider '{self.provider}' (use anthropic or openai)."
        if self.provider == "openai" and not self.model:
            return "Set a model for OpenAI-compatible providers (OLYMPUS_MODEL)."
        return None

# Project root (the directory containing the `olympus` package).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Editable prompt files. Prometheus (the evolution specialist) is allowed to
# rewrite these — that is the system's safe self-modification surface.
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Persistent memory lives outside the package so it survives upgrades.
# Git checkout -> ./memory next to the code; pip install -> ~/.olympus/memory.
_default_memory = (
    PROJECT_ROOT / "memory"
    if (PROJECT_ROOT / "pyproject.toml").exists()
    else Path.home() / ".olympus" / "memory"
)
MEMORY_DIR = Path(os.environ.get("OLYMPUS_MEMORY_DIR", _default_memory))

# Per-call output ceiling. Streaming is used everywhere, so this can be large.
MAX_TOKENS = int(os.environ.get("OLYMPUS_MAX_TOKENS", "16000"))

# Max tool-use iterations for a single specialist run (guards against loops).
MAX_AGENT_ITERATIONS = 16

# Process-wide cap on concurrent model calls (backpressure vs rate limits).
MAX_CONCURRENT_CALLS = int(os.environ.get("OLYMPUS_MAX_CONCURRENT_CALLS", "6"))

# Heartbeat cadence (seconds).
HEARTBEAT_TICK = 60                  # main loop resolution
OPPORTUNITY_SCAN_EVERY = 6 * 3600    # Argus scans the world every 6 hours
WATCHLIST_EVERY = 3600               # Mnemosyne checks the YouTube queue hourly
EVOLUTION_AUDIT_EVERY = 7 * 86400    # Prometheus self-audit weekly

DAILY_LEARNING_EVERY = 86400         # Metis distills experience into skills
TRAIN_EVERY = int(os.environ.get("OLYMPUS_TRAIN_EVERY", str(3 * 86400)))
# Prometheus trains the weakest specialists on a cadence (0 disables)

# Benchmark judge model (kept different from the model being tuned, so
# Prometheus can't game the scorer). Only used on the Anthropic backend.
JUDGE_MODEL = os.environ.get("OLYMPUS_JUDGE_MODEL", "claude-sonnet-4-6")

# Prometheus also self-audits after this many conversations (0 disables).
AUDIT_EVERY_CHATS = int(os.environ.get("OLYMPUS_AUDIT_EVERY_CHATS", "20"))

# Retain per-day trace and usage files for this many days (older are deleted).
RETAIN_DAYS = int(os.environ.get("OLYMPUS_RETAIN_DAYS", "30"))
MAINTENANCE_EVERY = 86400            # housekeeping sweep cadence
