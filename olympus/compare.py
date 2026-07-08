"""Blind multi-model compare — the same prompt, every configured model, judged
without brand labels.

Odysseus lets you pit models against each other; the value is *blindness* —
you pick the better answer before you know which model wrote it, so the choice
reflects the output, not the brand. This runs a prompt once against each usable
pool member with labels shuffled (so label order never tracks pool order),
stores the label→model mapping server-side, and only reveals it after you pick.
Picks accumulate into a per-user tally, so over time you learn which model
actually serves *you* best.

Each model is called with NO cross-member failover (`complete_text_once`): a
model that errors shows its error under its own label rather than being
silently answered by another — otherwise the comparison would measure the wrong
thing.
"""

from __future__ import annotations

import json
import random
import string
import time
import uuid
from pathlib import Path

from . import backend, config, memory

_LABELS = string.ascii_uppercase
_MAX_STORED = 50                 # bound stored comparisons per user
_DEFAULT_SYSTEM = ("You are a helpful assistant. Answer the user's message "
                   "directly, accurately, and concisely.")

# The copy-paste that turns a one-model pool into a two-model one. A second
# *Anthropic* model reuses the existing key via a SecretRef (`env:...`), so no
# secret is embedded and it stays on the same egress allowlist as the primary.
_SNIPPET = ('OLYMPUS_MODELS=[{"provider":"anthropic","model":"claude-sonnet-5",'
            '"api_key":"env:ANTHROPIC_API_KEY"}]')


def setup_hint() -> str:
    """One-liner shown wherever compare can't run for lack of a second model."""
    return ("Blind compare needs at least two configured models. Add one to "
            "OLYMPUS_MODELS (a second Anthropic model reuses your existing key "
            "and egress), e.g.:\n  " + _SNIPPET)


def _dir(user: str) -> Path:
    safe = memory.safe_id(user)
    base = (config.MEMORY_DIR / "users" / safe) if safe != "shared" \
        else config.MEMORY_DIR
    d = base / "compares"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tally_path(user: str) -> Path:
    return _dir(user) / "_tally.json"


def model_label(s: config.Settings) -> str:
    return f"{s.provider}/{s.model}" if s.model else s.provider


def available_models() -> list[config.Settings]:
    """Usable, de-duplicated pool members (by provider+model)."""
    try:
        pool = config.ModelPool.from_env()
    except Exception:
        return []
    out, seen = [], set()
    for m in pool.members:
        if not m.usable():
            continue
        fp = (m.provider, m.model)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(m)
    return out


def _save(user: str, cid: str, rec: dict) -> None:
    (_dir(user) / f"{cid}.json").write_text(json.dumps(rec, indent=2),
                                            encoding="utf-8")
    _prune(user)


def _load(user: str, cid: str) -> dict | None:
    p = _dir(user) / f"{cid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _prune(user: str) -> None:
    files = sorted((p for p in _dir(user).glob("*.json")
                    if p.name != "_tally.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[_MAX_STORED:]:
        try:
            p.unlink()
        except OSError:
            pass


def run(user: str, prompt: str, *, effort: str = "high",
        system: str = "") -> dict:
    """Run `prompt` against every usable model, blind. Returns
    {"id", "prompt", "answers": [{"label", "text"}]} with labels shuffled; the
    label→model mapping is stored, never returned here. Needs ≥2 models."""
    models = available_models()
    if len(models) < 2:
        return {"error": "Blind compare needs at least two configured models.",
                "hint": setup_hint(), "snippet": _SNIPPET,
                "models": [model_label(m) for m in models]}
    sys = system or _DEFAULT_SYSTEM
    order = list(range(len(models)))
    random.shuffle(order)                  # blind: label order ≠ pool order
    answers, mapping = [], {}
    for i, idx in enumerate(order):
        m, label = models[idx], _LABELS[i]
        try:
            text = backend.complete_text_once(
                m, sys, [{"role": "user", "content": prompt}], effort=effort)
        except Exception as err:
            text = f"[{model_label(m)} failed to answer: {str(err)[:200]}]"
        answers.append({"label": label, "text": text})
        mapping[label] = model_label(m)
    cid = uuid.uuid4().hex[:12]
    _save(user, cid, {"id": cid, "prompt": prompt, "created": time.time(),
                      "mapping": mapping, "revealed": False, "choice": None})
    return {"id": cid, "prompt": prompt, "answers": answers}


def reveal(user: str, cid: str, choice: str = "") -> dict | None:
    """Reveal which model wrote each label and, if `choice` is a label, record
    the blind pick into the per-user tally. Returns the mapping + chosen model,
    or None if the comparison isn't found."""
    rec = _load(user, cid)
    if rec is None:
        return None
    rec["revealed"] = True
    chosen_model = None
    choice = (choice or "").strip().upper()
    if choice and choice in rec["mapping"]:
        rec["choice"] = choice
        chosen_model = rec["mapping"][choice]
        _record_vote(user, chosen_model)
    _save(user, cid, rec)
    return {"id": cid, "mapping": rec["mapping"], "choice": rec.get("choice"),
            "chosen_model": chosen_model, "tally": tally(user)}


def _record_vote(user: str, model: str) -> None:
    t = tally(user)
    t[model] = t.get(model, 0) + 1
    try:
        _tally_path(user).write_text(json.dumps(t, indent=2), encoding="utf-8")
    except OSError:
        pass


def tally(user: str) -> dict:
    """Running count of blind picks per model, for this user."""
    p = _tally_path(user)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def render_tally(user: str) -> str:
    t = tally(user)
    if not t:
        return "No blind picks yet."
    lines = ["Your blind picks so far:"]
    for model, n in sorted(t.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"  {model}: {n}")
    return "\n".join(lines)
