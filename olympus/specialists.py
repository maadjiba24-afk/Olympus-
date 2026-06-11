"""The specialist registry.

Each specialist is defined by a prompt file plus a tool loadout. Adding a new
specialist = add a prompt file + one entry here (Prometheus proposes these).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import agent, config, skills, tools


@dataclass(frozen=True)
class Specialist:
    key: str
    name: str           # Olympian codename
    title: str          # human-readable role
    description: str    # used by Zeus/Athena for routing
    web: bool = False   # grant web_search/web_fetch (server- or client-side)
    code_exec: bool = False  # Anthropic server-side code sandbox
    extra_tools: tuple[str, ...] = field(default_factory=tuple)

    def tool_defs(self, provider: str = "anthropic"):
        defs = list(tools.BASE_TOOLS)
        defs += [tools.EXTRA_TOOLS[name] for name in self.extra_tools]
        if self.web:
            defs += tools.web_tool_defs(provider)
        if self.code_exec and provider == "anthropic":
            defs.append(tools.CODE_EXECUTION_TOOL)
        return defs

    def system_prompt(self) -> str:
        return (agent.load_prompt(self.key)
                + "\n\n## Skill library (load with read_skill before "
                  "relevant tasks)\n" + skills.index())

    def run(self, task: str, settings: config.Settings | None = None,
            effort: str = "high") -> str:
        from . import backend  # local import: backend imports this module's peers
        settings = settings or config.Settings.from_env()
        return backend.run_agent(settings, self.system_prompt(), task,
                                 self.tool_defs(settings.provider), effort)


SPECIALISTS: dict[str, Specialist] = {
    s.key: s
    for s in [
        Specialist(
            key="plutus", name="Plutus", title="Financial Specialist",
            description="Personal finance, budgeting, investing concepts, "
                        "business finance, market analysis, pricing.",
            web=True,
        ),
        Specialist(
            key="peitho", name="Peitho", title="Marketing Specialist",
            description="Marketing strategy, branding, copywriting, growth, "
                        "audience research, campaigns, SEO.",
            web=True,
        ),
        Specialist(
            key="hephaestus", name="Hephaestus", title="Coding Specialist",
            description="Software design, writing and reviewing code, debugging, "
                        "architecture, DevOps questions. Can execute and test "
                        "code in a sandbox.",
            web=True, code_exec=True,
        ),
        Specialist(
            key="aegis", name="Aegis", title="Cybersecurity Specialist",
            description="Defensive security: hardening, threat awareness, secure "
                        "coding, privacy, incident response guidance.",
            web=True,
        ),
        Specialist(
            key="iris", name="Iris", title="Social Network Assistant",
            description="Social media content, posting strategy, community "
                        "management, platform best practices, trends. Can push "
                        "content to configured webhooks.",
            web=True,
            extra_tools=("call_webhook",),
        ),
        Specialist(
            key="chiron", name="Chiron", title="Coaching Specialist",
            description="Personal coaching: goals, habits, productivity, "
                        "motivation, career growth, accountability.",
        ),
        Specialist(
            key="chronos", name="Chronos", title="Scheduling Manager",
            description="Time management, planning, schedules, routines, "
                        "deadlines, prioritization. Can send reminder emails "
                        "and call configured webhooks.",
            extra_tools=("send_email", "call_webhook"),
        ),
        Specialist(
            key="argus", name="Argus", title="Opportunity Scout",
            description="Surfs the internet (no MCP needed — server-side web "
                        "search) to find business opportunities, emerging trends, "
                        "and what is happening in the world right now.",
            web=True,
        ),
        Specialist(
            key="mnemosyne", name="Mnemosyne", title="YouTube Learner",
            description="Watches YouTube videos via transcript, understands and "
                        "summarizes them, and stores durable lessons in memory.",
            extra_tools=("watch_youtube", "create_skill"),
        ),
        Specialist(
            key="metis", name="Metis", title="Learning Synthesizer",
            description="Runs Olympus's daily learning cycle: distills recent "
                        "lessons, corrections, and user feedback into reusable "
                        "skills so the whole council gets smarter every day.",
            extra_tools=("create_skill",),
        ),
        Specialist(
            key="prometheus", name="Prometheus", title="Evolution Specialist",
            description="Audits Olympus itself: reads its own source and prompts, "
                        "finds what is missing, upgrades agent prompts (measured "
                        "by benchmark, with rollback), and files upgrade "
                        "proposals so the product keeps improving.",
            web=True,
            extra_tools=("list_source_files", "read_source_file",
                         "update_prompt", "restore_prompt", "run_benchmark",
                         "propose_upgrade", "create_skill"),
        ),
    ]
}


def roster() -> str:
    """Routing card given to Zeus and Athena."""
    return "\n".join(
        f"- {s.key}: {s.name}, {s.title} — {s.description}"
        for s in SPECIALISTS.values()
    )
