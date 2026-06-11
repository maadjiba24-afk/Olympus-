"""The Olympus pipeline.

    user
     │
     ▼
   Zeus (main agent) ── direct answer for casual chat
     │ delegates
     ▼
   Athena (supervisor) ── plans sub-tasks, dispatches specialists (parallel)
     │                        │
     │                        ▼
     │                  specialist outputs
     │                        │
     ▼                        ▼
   Aletheia (hallucination controller) ── verifies claims, fixes/flags,
     │                                     records corrections to memory
     ▼
   Athena synthesis → Zeus final reply → user

Provider-agnostic: every model call goes through `backend`, which dispatches
to Claude (full capability) or any OpenAI-compatible endpoint (BYOK).
"""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import agent, backend, config, memory, tools
from .specialists import SPECIALISTS, roster

ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["direct", "delegate"]},
        "direct_reply": {
            "type": ["string", "null"],
            "description": "The complete reply when mode is 'direct', else null",
        },
        "specialists": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specialist keys to involve when mode is 'delegate'",
        },
        "brief": {
            "type": ["string", "null"],
            "description": "Task brief for the supervisor when mode is 'delegate'",
        },
        "needs_verification": {
            "type": "boolean",
            "description": "True when the answer will contain factual claims "
            "worth checking",
        },
    },
    "required": ["mode", "direct_reply", "specialists", "brief",
                 "needs_verification"],
    "additionalProperties": False,
}

Reporter = Callable[[str], None]


def _silent(_: str) -> None:
    pass


class Olympus:
    """Stateful conversation handler running the full pipeline."""

    def __init__(self, report: Reporter = _silent,
                 settings: config.Settings | None = None):
        self.history: list[dict[str, Any]] = []
        self.report = report
        self.settings = settings or config.Settings.from_env()

    # -- stage 1: Zeus ----------------------------------------------------

    def _route(self, user_message: str) -> dict[str, Any]:
        system = agent.load_prompt("zeus") + "\n\n## Specialist roster\n" + roster()
        messages = self.history + [{"role": "user", "content": user_message}]
        try:
            return backend.complete_json(self.settings, system, messages,
                                         ROUTE_SCHEMA, effort="medium")
        except ValueError:
            return {"mode": "direct",
                    "direct_reply": "I can't help with that request.",
                    "specialists": [], "brief": None,
                    "needs_verification": False}
        except Exception:
            # Provider couldn't produce routable JSON — degrade gracefully to
            # a full delegation with the raw message as the brief.
            return {"mode": "delegate", "direct_reply": None,
                    "specialists": [], "brief": user_message,
                    "needs_verification": True}

    # -- stage 2: Athena dispatch ------------------------------------------

    def _plan(self, brief: str, keys: list[str]) -> list[dict[str, str]]:
        valid = [k for k in keys if k in SPECIALISTS] or ["argus"]
        schema = {
            "type": "object",
            "properties": {
                "assignments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "specialist": {"type": "string",
                                           "enum": list(SPECIALISTS)},
                            "task": {"type": "string"},
                        },
                        "required": ["specialist", "task"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["assignments"],
            "additionalProperties": False,
        }
        system = agent.load_prompt("athena") + "\n\n## Specialist roster\n" + roster()
        prompt = (
            f"Task brief from Zeus:\n{brief}\n\n"
            f"Suggested specialists: {', '.join(valid)}\n\n"
            "Write one precise, self-contained task per specialist you decide "
            "to use (you may drop or add specialists from the roster). Each "
            "task must contain all context the specialist needs."
        )
        try:
            assignments = backend.complete_json(
                self.settings, system, [{"role": "user", "content": prompt}],
                schema, effort="medium",
            )["assignments"]
            assignments = [a for a in assignments
                           if a.get("specialist") in SPECIALISTS and a.get("task")]
        except Exception:
            assignments = []
        return assignments or [{"specialist": valid[0], "task": brief}]

    # -- stage 3: Aletheia -------------------------------------------------

    def _verify(self, brief: str, outputs: list[tuple[str, str]]) -> str:
        system = agent.load_prompt("aletheia")
        bundle = "\n\n".join(
            f"### Output from {SPECIALISTS[k].name} ({SPECIALISTS[k].title})\n{v}"
            for k, v in outputs
        )
        task = (
            f"Original task brief:\n{brief}\n\n"
            f"Specialist outputs to verify:\n{bundle}\n\n"
            "Verify the factual claims (use web_search when a claim is "
            "checkable and consequential). Produce the corrected, "
            "confidence-annotated version of the content. If you correct "
            "anything, record it with save_lesson so Olympus never repeats "
            "the mistake."
        )
        tool_defs = (tools.web_tool_defs(self.settings.provider)
                     + [tools.SAVE_LESSON, tools.RECALL_MEMORY])
        return backend.run_agent(self.settings, system, task, tool_defs,
                                 effort="high")

    # -- stage 4: synthesis -------------------------------------------------

    def _synthesize(self, user_message: str, brief: str, verified: str) -> str:
        system = agent.load_prompt("zeus")
        prompt = (
            f"The user asked:\n{user_message}\n\n"
            f"Task brief:\n{brief}\n\n"
            "Verified specialist findings (already fact-checked by the "
            f"hallucination controller):\n{verified}\n\n"
            "Compose the final reply to the user. Keep every confidence flag "
            "or caveat the controller attached to uncertain claims."
        )
        return backend.complete_text(
            self.settings,
            system,
            self.history + [{"role": "user", "content": prompt}],
            effort="high",
        )

    # -- public entry point --------------------------------------------------

    def ask(self, user_message: str) -> str:
        error = self.settings.validate()
        if error:
            return f"Configuration problem: {error}"

        route = self._route(user_message)

        if route.get("mode") == "direct" and route.get("direct_reply"):
            reply = route["direct_reply"]
        else:
            brief = route.get("brief") or user_message
            self.report(f"⚡ Zeus delegates → Athena (brief: {brief[:80]}...)")
            assignments = self._plan(brief, route.get("specialists", []))

            # Specialists run in parallel — total latency is the slowest
            # single agent, not the sum of all of them.
            for item in assignments:
                spec = SPECIALISTS[item["specialist"]]
                self.report(f"🦉 Athena dispatches {spec.name} ({spec.title})")
            with ThreadPoolExecutor(max_workers=min(4, len(assignments))) as pool:
                outputs = list(pool.map(
                    lambda item: (
                        item["specialist"],
                        SPECIALISTS[item["specialist"]].run(
                            item["task"], settings=self.settings),
                    ),
                    assignments,
                ))

            if route.get("needs_verification", True):
                self.report("🔍 Aletheia verifies the findings...")
                verified = self._verify(brief, outputs)
            else:
                verified = "\n\n".join(
                    f"### {SPECIALISTS[k].name}\n{v}" for k, v in outputs
                )

            self.report("⚡ Zeus composes the final answer...")
            reply = self._synthesize(user_message, brief, verified)

        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": reply})
        # keep the rolling window bounded
        if len(self.history) > 24:
            self.history = self.history[-24:]
        note_conversation(self.report)
        return reply


# --- conversation-triggered self-audit ---------------------------------------

_AUDIT_LOCK = threading.Lock()


def _auto_audit(report: Reporter) -> None:
    if not _AUDIT_LOCK.acquire(blocking=False):
        return  # an audit is already running
    try:
        audit_report = evolution_audit()
        from . import telegram  # local import to avoid a cycle at module load
        telegram.notify("🔧 Olympus self-audit (conversation-triggered):\n\n"
                        + audit_report)
        report("🔧 Prometheus finished the background self-audit.")
    except Exception:
        traceback.print_exc()
    finally:
        _AUDIT_LOCK.release()


def note_conversation(report: Reporter = _silent) -> None:
    """Count one finished conversation; every N of them, Prometheus audits.

    The counter persists across sessions and interfaces (CLI, web, Telegram).
    The audit runs in the background on the *server's* configured credentials
    only — a web visitor's bring-your-own key is never spent on system work.
    """
    threshold = config.AUDIT_EVERY_CHATS
    if threshold <= 0:
        return
    state = memory.load_state()
    state["conversation_count"] = state.get("conversation_count", 0) + 1
    if state["conversation_count"] < threshold:
        memory.save_state(state)
        return
    state["conversation_count"] = 0
    memory.save_state(state)

    env = config.Settings.from_env()
    if env.validate() is not None or not (env.api_key or env.base_url):
        return  # no server-side credentials — skip rather than fail in the dark
    report("🔧 Conversation threshold reached — Prometheus will self-audit "
           "in the background.")
    threading.Thread(target=_auto_audit, args=(report,), daemon=True).start()


# --- one-shot autonomous routines (used by the heartbeat and CLI) -----------

def opportunity_scan(settings: config.Settings | None = None) -> str:
    """Argus surfs the web for opportunities & world events; report → memory."""
    task = (
        "Run your full scan now: current world events that matter, emerging "
        "business opportunities, and anything actionable. Finish with the "
        "structured report described in your instructions."
    )
    report = SPECIALISTS["argus"].run(task, settings=settings)
    memory.save("reports", "Opportunity scan", report)
    return report


def watch_and_learn(url: str, settings: config.Settings | None = None) -> str:
    """Mnemosyne watches one YouTube video and stores what it learned."""
    task = (
        f"Watch this YouTube video and learn from it: {url}\n"
        "Use watch_youtube to get the transcript, then produce the summary "
        "described in your instructions and persist the durable lessons with "
        "save_lesson."
    )
    summary = SPECIALISTS["mnemosyne"].run(task, settings=settings)
    memory.save("lessons", f"Video summary {url}", summary)
    return summary


def evolution_audit(settings: config.Settings | None = None) -> str:
    """Prometheus audits Olympus, upgrades prompts, files proposals."""
    task = (
        "Run a full self-audit of Olympus now:\n"
        "1. list_source_files and read the parts that matter.\n"
        "2. Review recent memory (recall_memory: corrections, upgrades, lessons).\n"
        "3. Find what is missing inside the system — capabilities, specialists, "
        "weak prompts, recurring mistakes.\n"
        "4. Apply safe improvements directly with update_prompt.\n"
        "5. File everything that needs code changes with propose_upgrade.\n"
        "Finish with an audit report: what you checked, what you changed, what "
        "you proposed."
    )
    report = SPECIALISTS["prometheus"].run(task, settings=settings)
    memory.save("reports", "Evolution audit", report)
    return report
