"""Central configuration for Olympus."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
        if self.provider not in ("anthropic", "openai", "claude-code"):
            return (f"Unknown provider '{self.provider}' "
                    "(use anthropic, openai, or claude-code).")
        if self.provider == "openai" and not self.model:
            return "Set a model for OpenAI-compatible providers (OLYMPUS_MODEL)."
        return None

    def usable(self) -> bool:
        if self.validate() is not None:
            return False
        # anthropic reads its key from the env; claude-code authenticates via the
        # local `claude` CLI (your subscription); others need a key/url.
        return (self.provider in ("anthropic", "claude-code")
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
    "glm":      {"reasoning": 8.5, "coding": 8,   "general": 8,   "verify": 8},
    "kimi":     {"reasoning": 8,  "coding": 8.5, "general": 8.5, "verify": 8.5},
    "moonshot": {"reasoning": 8,  "coding": 8.5, "general": 8.5, "verify": 8.5},
    "qwen":     {"reasoning": 7,  "coding": 8,  "general": 6,  "verify": 6},
    "mistral":  {"reasoning": 6,  "coding": 6,  "general": 7,  "verify": 6},
    "llama":    {"reasoning": 6,  "coding": 6,  "general": 6,  "verify": 6},
}
_DEFAULT_CAP = {"reasoning": 5, "coding": 5, "general": 5, "verify": 5}

# Which capability each pipeline role / specialist wants.
SPECIALIST_ROLE = {"hephaestus": "coding"}  # legacy fallback; registry wins


def specialist_role(key: str) -> str:
    """The model role a specialist routes on. Read from the specialist registry
    (data-driven, per-specialist); falls back to the legacy map / 'reasoning'.
    Lazy import keeps config free of a specialists import cycle."""
    try:
        from . import specialists
        spec = specialists.SPECIALISTS.get(key)
        if spec is not None:
            return spec.role
    except Exception:
        pass
    return SPECIALIST_ROLE.get(key, "reasoning")


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
        # Sovereign mode constrains *which members are eligible* before the
        # existing capability-score selection runs — it never touches scoring.
        # A remote frontier model can never be selected (not even as a tie-break
        # or fallback); if no local member remains we FAIL CLOSED rather than
        # reaching for a remote one.
        if sovereign_mode():
            eligible = [m for m in members if member_is_local(m)]
            if not eligible:
                from . import security
                raise security.NoLocalModelError(
                    "sovereign mode is on but no local model is configured — "
                    "no pool member's host is on the egress allowlist "
                    "(loopback + OLYMPUS_EGRESS_ALLOWLIST + local providers). "
                    "Refusing to fall back to a remote model.")
            members = eligible
        if not members:                       # fall back to the first given/env
            members = [settings[0] if settings else Settings.from_env()]
        return cls(tuple(members))

    @classmethod
    def _env_members(cls) -> list[Settings]:
        """The raw member list from env (primary + OLYMPUS_MODELS pool), BEFORE
        any sovereign eligibility filtering — used by status surfaces to report
        every configured member and which of them are local."""
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
        return [primary, *extra]

    @classmethod
    def from_env(cls) -> "ModelPool":
        return cls.of(*cls._env_members())

    def local_only(self) -> "ModelPool":
        """A pool restricted to local/allowlisted members — used to honor a
        data class that must stay local (e.g. `restricted`) even when the global
        sovereign flag is OFF. Fails closed if no eligible member remains."""
        eligible = tuple(m for m in self.members if member_is_local(m))
        if not eligible:
            from . import security
            raise security.NoLocalModelError(
                "this request's data class requires a local model, but none is "
                "configured (no member's host is on the egress allowlist).")
        return ModelPool(eligible)

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
        return self.for_role(specialist_role(key))

    def fastest(self) -> Settings:
        """The member most likely to respond quickly — by model-name hints
        (flash/air/mini/8k/haiku/turbo/…), else the primary. Used for the
        latency-sensitive light stages in fast mode."""
        if len(self.members) == 1:
            return self.members[0]
        hints = ("flash", "air", "mini", "nano", "haiku", "turbo", "lite",
                 "fast", "instant", "small", "8k", "scout")
        def speed(s: Settings) -> int:
            return 1 if any(h in (s.model or "").lower() for h in hints) else 0
        return max(self.members, key=speed)  # ties keep the first (primary-ish)

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

# In-run tool-transcript compaction (olympus/transcript.py). OFF by default.
# When on, once a single agent run's messages exceed INRUN_COMPACT_BUDGET chars,
# the contents of OLDER tool_result blocks are shrunk in place (recent ones kept
# verbatim) so a tool-heavy run doesn't drown in its own scrollback. Set to
# "elide" / "1" (deterministic) or "summarize" (LLM summary of old results).
INRUN_COMPACT = os.environ.get("OLYMPUS_INRUN_COMPACT", "").strip().lower()
INRUN_COMPACT_BUDGET = int(os.environ.get("OLYMPUS_INRUN_BUDGET", "24000"))
INRUN_KEEP_RECENT = int(os.environ.get("OLYMPUS_INRUN_KEEP_RECENT", "2"))

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

# Off-droplet data backups: the heartbeat archives MEMORY_DIR (user memory,
# accounts, encrypted OAuth tokens, the signed decision log), encrypts and signs
# it, and hands the archive to OLYMPUS_BACKUP_CMD for off-machine delivery on
# this cadence. 0 disables the scheduled backup (you can still run it by hand
# with `olympus backup`). Daily by default.
BACKUP_EVERY = int(os.environ.get("OLYMPUS_BACKUP_EVERY", str(86400)))


def backup_command() -> str:
    """Shell command that delivers a finished backup archive off-droplet — this
    is what makes a backup survive losing the machine. The literal token {path}
    is replaced with the archive path, e.g.
    `rclone copy {path} spaces:olympus-backups/` or `aws s3 cp {path} s3://...`.
    Empty = keep backups local only (guards against data corruption, but NOT
    against losing the droplet)."""
    return os.environ.get("OLYMPUS_BACKUP_CMD", "").strip()


def backup_keep() -> int:
    """How many local backup archives to retain (OLYMPUS_BACKUP_KEEP, default 7).
    Off-droplet copies are retained by your storage provider's own policy."""
    try:
        return max(1, int(os.environ.get("OLYMPUS_BACKUP_KEEP", "7")))
    except ValueError:
        return 7


def backup_allow_plaintext() -> bool:
    """Off-droplet delivery of an UNENCRYPTED archive is refused by default
    (it would put user PII and OAuth tokens on third-party storage in the
    clear). Set OLYMPUS_BACKUP_ALLOW_PLAINTEXT=1 only if the destination is
    itself trusted/encrypted."""
    return os.environ.get("OLYMPUS_BACKUP_ALLOW_PLAINTEXT", "").strip().lower() in (
        "1", "true", "yes", "on")


def fast_mode() -> bool:
    """Latency mode: run the lightweight pipeline stages (route/plan) on the
    pool's fastest model and skip the optional Athena review stage. Trades a
    little polish for markedly lower latency (OLYMPUS_FAST=1)."""
    return os.environ.get("OLYMPUS_FAST", "").strip().lower() in (
        "1", "true", "yes", "on")


# --- sovereignty: provable zero-egress mode (SPEC-02) --------------------
# Sovereign mode turns Olympus's *capability* to run fully local into an
# enforced, fail-closed *guarantee*: remote models are excluded from selection,
# every egress is funneled through security.assert_egress_allowed, and a blocked
# destination raises rather than leaking. OFF by default → behavior unchanged.

DATA_CLASSES = ("public", "internal", "restricted")


def sovereign_mode() -> bool:
    """Whether zero-egress sovereignty is enforced (OLYMPUS_SOVEREIGN). When on,
    the egress invariant holds: data leaves only to allowlisted hosts, remote
    models are never selected, and a forbidden egress fails closed."""
    return os.environ.get("OLYMPUS_SOVEREIGN", "").strip().lower() in (
        "1", "true", "yes", "on")


def egress_allowlist() -> list[str]:
    """Hosts/CIDRs permitted to receive data under sovereign mode
    (OLYMPUS_EGRESS_ALLOWLIST, comma-separated). Loopback and known local
    providers are always allowed implicitly and need not be listed."""
    raw = os.environ.get("OLYMPUS_EGRESS_ALLOWLIST", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def member_host(s: "Settings") -> str:
    """The egress host a pool member would talk to: the base_url host if set,
    else the provider's default endpoint. (claude-code shells to the `claude`
    CLI, which itself egresses to Anthropic, so it counts as remote.)"""
    if s.base_url:
        return (urlparse(s.base_url).hostname or "").lower()
    return {"anthropic": "api.anthropic.com",
            "claude-code": "api.anthropic.com",
            "openai": "api.openai.com"}.get(s.provider, "")


def member_is_local(s: "Settings") -> bool:
    """Whether a member is sovereign-eligible: its egress host is on the
    allowlist (loopback / local provider / OLYMPUS_EGRESS_ALLOWLIST). Independent
    of the global sovereign flag so data-class routing can use it too."""
    from . import security
    return security.host_on_allowlist(member_host(s))


def normalize_data_class(value) -> str | None:
    """Return a valid data class (public/internal/restricted) or None."""
    v = (value or "").strip().lower() if isinstance(value, str) else ""
    return v if v in DATA_CLASSES else None


def default_data_class() -> str:
    """Class for an unspecified request: most-permissive (`public`) only when
    sovereign mode is OFF; when ON, default to at least `internal` (local-only).
    """
    return "internal" if sovereign_mode() else "public"


def data_class_local_only(value) -> bool:
    """Policy table → whether a request of this data class must stay local:
      restricted ⇒ local-only regardless of the global sovereign flag;
      internal   ⇒ local-only when sovereign is on;
      public     ⇒ may use remote only when sovereign is off.
    An unspecified class resolves via default_data_class()."""
    dc = normalize_data_class(value) or default_data_class()
    if dc == "restricted":
        return True
    return sovereign_mode()


def sovereign_status() -> dict:
    """Auditor-facing snapshot: mode, the active allowlist, every configured
    member, and which members are eligible (local). Never raises — it reports
    the raw configuration even when sovereign mode would fail closed."""
    members = ModelPool._env_members()
    usable = [m for m in members if m.usable()]

    def desc(m: "Settings") -> str:
        host = member_host(m) or "local"
        return f"{m.provider}/{m.model or '(default)'} @ {host}"

    eligible = [m for m in usable if member_is_local(m)]
    return {
        "sovereign": sovereign_mode(),
        "allowlist": egress_allowlist(),
        "default_data_class": default_data_class(),
        "members": [desc(m) for m in usable],
        "eligible_local": [desc(m) for m in eligible],
    }


def require_byok() -> bool:
    """When set, every web chat must carry the user's own API key — so a public
    instance never spends the operator's key on visitors (OLYMPUS_REQUIRE_BYOK)."""
    return os.environ.get("OLYMPUS_REQUIRE_BYOK", "").strip().lower() in (
        "1", "true", "yes", "on")


def api_keys() -> list[str]:
    """Bearer keys that authorize the OpenAI-compatible `/v1/*` endpoints
    (OLYMPUS_API_KEYS, comma-separated). When this is empty the `/v1/*` routes
    answer on loopback only — never a silent open relay: a remote caller with no
    configured key is refused outright."""
    raw = os.environ.get("OLYMPUS_API_KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def contracts_enabled() -> bool:
    """Enforce hard output contracts on specialist outputs (OLYMPUS_CONTRACTS=1).
    OFF BY DEFAULT: contracts are inert until an operator opts in, so the
    feature can't surprise a fresh install or a public BYOK instance."""
    return os.environ.get("OLYMPUS_CONTRACTS", "").strip().lower() in (
        "1", "true", "yes", "on")


def egress_guard_enabled() -> bool:
    """Route outbound data through the egress gateway (OLYMPUS_EGRESS_GUARD=1).
    OFF BY DEFAULT — inert until an operator opts in, so it can't surprise a
    fresh install or a public BYOK instance."""
    return os.environ.get("OLYMPUS_EGRESS_GUARD", "").strip().lower() in (
        "1", "true", "yes", "on")


def daily_chat_limit() -> int:
    """Max chats per user per day (OLYMPUS_DAILY_CHATS; 0 = unlimited). Bounds
    cost and abuse on a public instance, independent of the per-minute limit."""
    try:
        return int(os.environ.get("OLYMPUS_DAILY_CHATS", "0"))
    except ValueError:
        return 0


def free_chats() -> int:
    """Free, operator-funded chats per user per day before they must bring their
    own key (OLYMPUS_FREE_CHATS; 0 = none). This makes BYOK a *free allowance*
    rather than all-or-nothing: offer a taste on your key, then users continue
    'as much as they bring' on their own. When > 0 it governs keyless users
    regardless of OLYMPUS_REQUIRE_BYOK."""
    try:
        return max(0, int(os.environ.get("OLYMPUS_FREE_CHATS", "0")))
    except ValueError:
        return 0

# The gate proves *replay determinism*, which is model-independent — so it runs
# on a cheaper model by default (≈5x less than Opus) to keep the weekly CI /
# heartbeat tripwire affordable. Override for a full-fidelity run on your main
# model: OLYMPUS_GATE_MODEL=claude-opus-4-8.
GATE_MODEL = os.environ.get("OLYMPUS_GATE_MODEL", "claude-sonnet-4-6")
