"""The Olympus pipeline.

    user
     │
     ▼
   Zeus (main agent) ── direct answer for casual chat
     │ delegates
     ▼
   Athena (supervisor) ── plans sub-tasks, dispatches specialists
     │                        │
     │                        ▼
     │                  specialist outputs
     │                        │
     ▼                        ▼
   Aletheia (hallucination controller) ── verifies claims, fixes/flags,
     │                                     records corrections to memory
     ▼
   Athena synthesis → Zeus final reply → user
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import agent, llm, memory
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

    def __init__(self, report: Reporter = _silent):
        self.history: list[dict[str, Any]] = []
        self.report = report

    # -- stage 1: Zeus ----------------------------------------------------

    def _route(self, user_message: str) -> dict[str, Any]:
        system = agent.load_prompt("zeus") + "\n\n## Specialist roster\n" + roster()
        messages = self.history + [{"role": "user", "content": user_message}]
        response = llm.complete(
            system, messages, effort="medium", output_schema=ROUTE_SCHEMA
        )
        if response.stop_reason == "refusal":
            return {"mode": "direct",
                    "direct_reply": "I can't help with that request.",
                    "specialists": [], "brief": None,
                    "needs_verification": False}
        return llm.json_of(response)

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
        response = llm.complete(system, [{"role": "user", "content": prompt}],
                                effort="medium", output_schema=schema)
        assignments = llm.json_of(response)["assignments"]
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
        from . import tools  # local import to avoid cycle at module load
        tool_defs = tools.WEB_TOOLS + [tools.SAVE_LESSON, tools.RECALL_MEMORY]
        return agent.run_agent(system, task, tool_defs=tool_defs, effort="high")

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
        response = llm.complete(system,
                                self.history + [{"role": "user", "content": prompt}],
                                effort="high")
        return llm.text_of(response)

    # -- public entry point --------------------------------------------------

    def ask(self, user_message: str) -> str:
        route = self._route(user_message)

        if route["mode"] == "direct" and route.get("direct_reply"):
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
                results = list(pool.map(
                    lambda item: (item["specialist"],
                                  SPECIALISTS[item["specialist"]].run(item["task"])),
                    assignments,
                ))
            outputs: list[tuple[str, str]] = results

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
        return reply


# --- one-shot autonomous routines (used by the heartbeat and CLI) -----------

def opportunity_scan() -> str:
    """Argus surfs the web for opportunities & world events; report → memory."""
    task = (
        "Run your full scan now: current world events that matter, emerging "
        "business opportunities, and anything actionable. Finish with the "
        "structured report described in your instructions."
    )
    report = SPECIALISTS["argus"].run(task)
    memory.save("reports", "Opportunity scan", report)
    return report


def watch_and_learn(url: str) -> str:
    """Mnemosyne watches one YouTube video and stores what it learned."""
    task = (
        f"Watch this YouTube video and learn from it: {url}\n"
        "Use watch_youtube to get the transcript, then produce the summary "
        "described in your instructions and persist the durable lessons with "
        "save_lesson."
    )
    summary = SPECIALISTS["mnemosyne"].run(task)
    memory.save("lessons", f"Video summary {url}", summary)
    return summary


def evolution_audit() -> str:
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
    report = SPECIALISTS["prometheus"].run(task)
    memory.save("reports", "Evolution audit", report)
    return report
