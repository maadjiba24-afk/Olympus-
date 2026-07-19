"""Teacher escalation — a failed rework runs on the strongest pool member, and
the fix becomes a provisional skill the regular model learns from.

Adopted from Odysseus's teacher-escalation loop (a SOTA "teacher" endpoint
fixes what the local "student" model failed at, then writes the lesson down as
a skill so the student does it itself next time), fitted to Olympus's pieces:

* the *failure signal* is Athena's quality review ordering a retry — the one
  place the pipeline already says "this specific specialist's answer was not
  good enough";
* the *teacher* is whatever pool member scores highest for that specialist's
  role (config.capability_score) — no new configuration, the pool the operator
  already composed IS the teacher bench. With a single-member pool, or when
  the specialist already runs on the strongest member, there is nothing to
  escalate to and the rework proceeds exactly as before;
* the *lesson* lands through the existing skill machinery: provisional,
  scoped to the failing specialist, benchmark-gated by Metis's `gate_skills`
  cycle — so a bad teacher lesson is measured and rolled back, never trusted.

Distillation runs in a background thread (the user's reply is never delayed
by homework) and is best-effort throughout.

Config: OLYMPUS_TEACHER_ESCALATION=0 disables both the escalation and the
distillation (default on — reworks are rare and are exactly the moment
quality is worth the strongest model's price).
"""

from __future__ import annotations

import os
import threading

from . import config

_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "worth_saving": {
            "type": "boolean",
            "description": "true only if the fix teaches a REUSABLE technique "
                           "— not a one-off fact about this request",
        },
        "name": {"type": "string", "description": "short kebab-case skill name"},
        "description": {"type": "string",
                        "description": "one line: when to reach for this skill"},
        "instructions": {
            "type": "string",
            "description": "the procedure: when to use / steps / pitfalls / "
                           "how to verify, written for the weaker model",
        },
    },
    "required": ["worth_saving"],
}


def enabled() -> bool:
    return os.environ.get("OLYMPUS_TEACHER_ESCALATION", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def teacher_for(pool: config.ModelPool, key: str) -> config.Settings | None:
    """The pool member a failed `key` rework should escalate to: the highest
    capability score for the specialist's role, and only if that genuinely
    outranks what the specialist already runs on (equal strength = no point
    paying for a re-roll on a sibling model).

    Returning None is NOT a dead end: the orchestrator's rework dispatch runs
    with retry_index=1, which the effort scorer maps to the top tier — so a
    single-model pool escalates same-model-more-compute instead of not at
    all (ADR 0005)."""
    if not enabled() or len(pool.members) < 2:
        return None
    role = config.specialist_role(key)
    current = pool.for_specialist(key)
    best = max(pool.members,
               key=lambda m: config.capability_score(m.model, role))
    if config.capability_score(best.model, role) \
            <= config.capability_score(current.model, role):
        return None
    return best


def distill(key: str, task: str, feedback: str, fix: str,
            teacher: config.Settings) -> str | None:
    """Ask the teacher to write the lesson down. Returns the created skill
    name, or None when there was nothing generalizable (or on any failure —
    distillation must never break the pipeline)."""
    from . import backend, security, skills
    prompt = (
        "A weaker model failed this task and you (the stronger model) fixed "
        "it. If — and only if — the fix teaches a reusable technique, distill "
        "it into a skill the weaker model can follow next time.\n\n"
        f"## The task\n{task[:4000]}\n\n"
        f"## Why the first attempt failed (supervisor feedback)\n"
        f"{feedback[:2000]}\n\n"
        f"## The good answer\n{fix[:6000]}\n"
    )
    system = ("You distill failures into concise, reusable skills for a "
              "weaker model. Be honest: most one-off fixes are NOT worth "
              "saving; set worth_saving=false for anything request-specific.")
    try:
        data = backend.complete_json(teacher, system,
                                     [{"role": "user", "content": prompt}],
                                     _SKILL_SCHEMA, effort="medium")
        if not data.get("worth_saving"):
            return None
        name = str(data.get("name") or "").strip()
        instructions = str(data.get("instructions") or "").strip()
        if not name or not instructions:
            return None
        skills.create(name, str(data.get("description") or "").strip(),
                      security.sanitize_for_memory(instructions),
                      specialist=key, provisional=True)
        return name
    except Exception:
        return None


def distill_async(key: str, task: str, feedback: str, fix: str,
                  teacher: config.Settings) -> threading.Thread:
    """Fire-and-forget distillation — the reply is never delayed by homework."""
    t = threading.Thread(target=distill,
                         args=(key, task, feedback, fix, teacher),
                         daemon=True)
    t.start()
    return t
