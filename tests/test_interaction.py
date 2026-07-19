"""The ask_user seam: headless runs never block (proceed-with-assumption),
interactive runs get the chosen option, answers are wrapped untrusted, the
provider is thread-local, and the tool stays available under capability
separation (it neither ingests nor acts).
"""

import pytest

from olympus import interaction, security, specialists, tools


@pytest.fixture(autouse=True)
def clear_provider():
    interaction.set_provider(None)
    yield
    interaction.set_provider(None)


def test_headless_returns_proceed_instruction():
    out = interaction.ask("Pick a format", ["pdf", "html"])
    assert "No interactive user" in out
    assert "pdf; html" in out          # options surfaced for the model


def test_provider_answer_is_wrapped_untrusted():
    interaction.set_provider(lambda q, opts: "the user's choice")
    out = interaction.ask("Which one?", ["a", "b"])
    assert "the user's choice" in out
    assert "<untrusted_external_content" in out


def test_dismissal_is_handled():
    def boom(q, opts):
        raise KeyboardInterrupt

    interaction.set_provider(boom)
    assert "dismissed" in interaction.ask("Q?", ["a"])


def test_reporting_provider_surfaces_then_proceeds():
    """Async surfaces (web/gateway): the question is surfaced through the report
    channel and the run proceeds with an assumption — not silently absorbed."""
    surfaced = []
    interaction.set_provider(
        interaction.reporting_provider(surfaced.append))
    out = interaction.ask("Which region?", ["us", "eu"])
    # the user saw the question + options
    assert surfaced and "Which region?" in surfaced[0]
    assert "us; eu" in surfaced[0]
    # and the run got proceed-with-assumption guidance (not a fake answer)
    assert "proceed" in out.lower() and "options were: us; eu" in out


def test_reporting_provider_survives_a_broken_report_channel():
    def boom(_msg):
        raise RuntimeError("channel down")

    interaction.set_provider(interaction.reporting_provider(boom))
    out = interaction.ask("Q?", ["a", "b"])
    assert "proceed" in out.lower()          # still proceeds, never raises


def test_cli_provider_numeric_choice():
    class FakeIO:
        def __init__(self):
            self.shown = []

        def out(self, text):
            self.shown.append(text)

        def line(self, prompt):
            return "2"

    io = FakeIO()
    provider = interaction.cli_provider(io=io)
    assert provider("Pick", ["alpha", "beta"]) == "beta"
    assert any("Pick" in s for s in io.shown)


def test_cli_provider_free_text_passthrough():
    class FakeIO:
        def out(self, text):
            pass

        def line(self, prompt):
            return "something else entirely"

    provider = interaction.cli_provider(io=FakeIO())
    assert provider("Pick", ["a", "b"]) == "something else entirely"


def test_provider_is_thread_local():
    import threading
    interaction.set_provider(lambda q, o: "main")
    seen = {}

    def worker():
        seen["worker"] = interaction.current()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["worker"] is None          # not inherited across threads


# --- tool wiring ------------------------------------------------------------

def test_ask_user_is_a_base_tool():
    names = {d["name"] for d in tools.BASE_TOOLS}
    assert "ask_user" in names
    assert "ask_user" in tools.HANDLERS


def test_ask_user_handler_uses_the_provider():
    interaction.set_provider(lambda q, opts: opts[0] if opts else "x")
    out = tools.HANDLERS["ask_user"]("Which?", ["first", "second"])
    assert "first" in out


def test_ask_user_survives_capability_separation():
    """It's neither ingestion nor action, so an ingesting specialist keeps it."""
    argus = specialists.SPECIALISTS["argus"]        # web=True → ingests
    names = {d.get("name") for d in argus.tool_defs("anthropic")}
    assert "ask_user" in names
