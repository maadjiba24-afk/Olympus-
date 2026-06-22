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

    def usable(self) -> bool:
        if self.validate() is not None:
            return False
        # anthropic may read its key from the environment; others need a key/url
        return (self.provider == "anthropic"
                or bool(self.api_key) or bool(self.base_url))


# --- multi-model pools: use the best of several frontier keys, as one --------

# Rough, defensible capability scores per role (by model-name substring). The
# point is relative strength so each provided key is used where it's strongest —
# not a precise leaderboard. Users can override role assignments explicitly.
_CAPABILITIES: dict[str, dict[str, float]] = {
    "fable":    {"reasoning": 10, "coding": 10, "general": 10, "verify": 10},
    "mythos":   {"reasoning": 10, "coding": 10, "general": 10, "verify": 10},
    "opus":     {"reasoning": 9,  "coding": 9,  "general": 9,  "verify": 9},
    "sonnet":   {"reasoning": 8,  "coding": 8,  "general": 8,  "verify": 8.5},
    "haiku":    {"reasoning": 6,  "coding": 6,  "general": 7,  "verify": 6},
    "gpt-5":    {"reasoning": 9,  "coding": 9,  "general": 9,  "verify": 8},
    "o3":       {"reasoning": 9,  "coding": 9,  "general": 8,  "verify": 8},
    "o1":       {"reasoning": 9,  "coding": 8,  "general": 8,  "verify": 8},
    "gpt-4o":   {"reasoning": 8,  "coding": 8,  "general": 9,  "verify": 7.5},
    "gpt-4":    {"reasoning": 8,  "coding": 8,  "general": 8,  "verify": 7.5},
    "gemini":   {"reasoning": 8,  "coding": 8,  "general": 9,  "verify": 8},
    "deepseek": {"reasoning": 8,  "coding": 9,  "general": 7,  "verify": 7},
    "qwen":     {"reasoning": 7,  "coding": 8,  "general": 6,  "verify": 6},
    "mistral":  {"reasoning": 6,  "coding": 6,  "general": 7,  "verify": 6},
    "llama":    {"reasoning": 6,  "coding": 6,  "general": 6,  "verify": 6},
}
_DEFAULT_CAP = {"reasoning": 5, "coding": 5, "general": 5, "verify": 5}

# Which capability each pipeline role / specialist wants.
SPECIALIST_ROLE = {"hephaestus": "coding"}  # everyone else: "reasoning"


def capability_score(model: str, role: str) -> float:
    m = (model or "").lower()
    for key, caps in _CAPABILITIES.items():
        if key in m:
            return caps.get(role, caps.get("general", 5))
    return _DEFAULT_CAP.get(role, 5)


@dataclass(frozen=True)
class ModelPool:
    """A set of frontier-model credentials used together — each pipeline role
    runs on whichever provided model is strongest for it. With one key this is
    just that key everywhere; with two (e.g. Claude + GPT) Olympus composes
    their strengths into one system instead of switching between them."""

    members: tuple[Settings, ...]

    @classmethod
    def of(cls, *settings: Settings) -> "ModelPool":
        seen, members = set(), []
        for s in settings:
            if s and s.usable():
                fp = (s.provider, s.model, s.api_key, s.base_url)
                if fp not in seen:
                    seen.add(fp)
                    members.append(s)
        if not members:                       # fall back to the first given/env
            members = [settings[0] if settings else Settings.from_env()]
        return cls(tuple(members))

    @classmethod
    def from_env(cls) -> "ModelPool":
        primary = Settings.from_env()
        extra = []
        raw = os.environ.get("OLYMPUS_MODELS")
        if raw:
            import json
            try:
                for d in json.loads(raw):
                    extra.append(Settings(
                        provider=(d.get("provider") or "anthropic").lower(),
                        model=d.get("model", ""),
                        api_key=d.get("api_key"),
                        base_url=d.get("base_url")))
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
        return cls.of(primary, *extra)

    def primary(self) -> Settings:
        return self.members[0]

    def _role_map(self) -> dict[str, Settings]:
        """Assign each role to a member: highest capability wins, and TIES are
        broken toward the least-used member so comparable frontier models split
        the work (both keys get used) rather than one hogging everything. A
        strictly stronger model still wins outright — quality first."""
        roles = ("reasoning", "coding", "verify")
        if len(self.members) == 1:
            return {r: self.members[0] for r in roles}
        used = {id(m): 0 for m in self.members}
        out: dict[str, Settings] = {}
        for role in roles:
            best = max(capability_score(m.model, role) for m in self.members)
            tied = [m for m in self.members
                    if capability_score(m.model, role) == best]
            pick = min(tied, key=lambda m: used[id(m)])  # least-used among tied
            out[role] = pick
            used[id(pick)] += 1
        return out

    def for_role(self, role: str) -> Settings:
        if len(self.members) == 1:
            return self.members[0]
        return self._role_map().get(role) or max(
            self.members, key=lambda s: capability_score(s.model, role))

    def for_specialist(self, key: str) -> Settings:
        return self.for_role(SPECIALIST_ROLE.get(key, "reasoning"))

    def is_multi(self) -> bool:
        return len(self.members) > 1

    def assignment(self) -> str:
        """Human-readable view of which model handles what."""
        if not self.is_multi():
            s = self.members[0]
            return f"Single model: {s.provider}/{s.model or '(env default)'}"
        rmap = self._role_map()
        lines = ["Model pool (best of each, used together):"]
        for role in ("reasoning", "coding", "verify"):
            s = rmap[role]
            lines.append(f"  {role:9s} → {s.provider}/{s.model}")
        lines.append("  members: "
                     + ", ".join(f"{m.provider}/{m.model}" for m in self.members))
        return "\n".join(lines)


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

# Conversation state compaction: when the verbatim history exceeds this many
# estimated tokens, older turns are folded into a compact running "state" block
# and only the most recent turns are replayed verbatim. Token-based (not turn-
# count) because cost tracks context size, not the number of messages.
HISTORY_TOKEN_BUDGET = int(os.environ.get("OLYMPUS_HISTORY_TOKEN_BUDGET", "3000"))
HISTORY_KEEP_TURNS = int(os.environ.get("OLYMPUS_HISTORY_KEEP_TURNS", "8"))

# Durable per-user memory: extract durable facts from turns (cheap model, in the
# background), gate them, and retrieve the relevant ones into context.
MEMORY_ENABLED = os.environ.get("OLYMPUS_MEMORY", "1").lower() not in ("0", "false", "no")
MEMORY_CONFIDENCE_FLOOR = float(os.environ.get("OLYMPUS_MEMORY_FLOOR", "0.6"))
MEMORY_RETRIEVAL_FLOOR_CONF = 0.25     # decayed-confidence floor for retrieval
MEMORY_RETRIEVAL_BUDGET_TOKENS = int(os.environ.get("OLYMPUS_MEMORY_BUDGET", "800"))
MEMORY_MIN_CHARS = 40                  # skip extraction for trivial turns
# Hybrid retrieval: when lexical match yields fewer than this many hits AND an
# embeddings endpoint is configured, fall back to semantic search (cosine).
MEMORY_SEMANTIC_FALLBACK_MIN = 2
MEMORY_SEMANTIC_THRESHOLD = 0.55       # min cosine to count as semantically relevant

# Max tool-use iterations for a single specialist run (guards against loops).
MAX_AGENT_ITERATIONS = 16

# Process-wide cap on concurrent model calls (backpressure vs rate limits).
MAX_CONCURRENT_CALLS = int(os.environ.get("OLYMPUS_MAX_CONCURRENT_CALLS", "6"))

# Budget guard: max estimated USD/day on the user's own API key before Olympus
# pauses new requests (0 = no cap). Protects the BYOK bill; can also be set at
# runtime with `olympus budget <amount>`, which takes precedence.
DAILY_BUDGET = float(os.environ.get("OLYMPUS_DAILY_BUDGET", "0") or 0)

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

# Replay self-check (decision-log tripwire): the heartbeat re-runs the gate on
# this cadence and escalates if a live run no longer replays byte-identically.
# It makes a few real model calls (budget-guarded); 0 disables it.
REPLAY_GATE_EVERY = int(os.environ.get("OLYMPUS_REPLAY_GATE_EVERY", str(7 * 86400)))

# The gate proves *replay determinism*, which is model-independent — so it runs
# on a cheaper model by default (≈5x less than Opus) to keep the weekly CI /
# heartbeat tripwire affordable. Override for a full-fidelity run on your main
# model: OLYMPUS_GATE_MODEL=claude-opus-4-8.
GATE_MODEL = os.environ.get("OLYMPUS_GATE_MODEL", "claude-sonnet-4-6")
