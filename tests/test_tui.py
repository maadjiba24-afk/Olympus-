"""Rich TUI: slash-command autocomplete, multiline input, command dispatch."""

from olympus import tui


def test_complete_slash_prefix():
    assert tui.complete("/sc") == ["/scan"]
    assert "/good" in tui.complete("/g")
    assert tui.complete("hello") == []           # not a slash command
    assert set(tui.complete("/")) == set(tui.COMMANDS)


def test_help_text_lists_every_command():
    text = tui.help_text()
    for cmd in tui.COMMANDS:
        assert cmd in text


def _reader(lines):
    it = iter(lines)

    def read(_prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return read


def test_read_block_single_line():
    assert tui.read_block(_reader(["just one line"])) == "just one line"


def test_read_block_backslash_continuation():
    out = tui.read_block(_reader(["first \\", "second \\", "third"]))
    assert out == "first \nsecond \nthird"


def test_read_block_fenced_multiline():
    out = tui.read_block(_reader(["```", "line a", "line b", "```", "ignored"]))
    assert out == "line a\nline b"


def test_read_block_eof_returns_none():
    assert tui.read_block(_reader([])) is None


class _FakeBot:
    def feedback(self, verdict, comment=""):
        return f"fb:{verdict}:{comment}"

    def set_language(self, lang):
        return f"lang:{lang}"

    def set_contribute(self, on):
        return f"contrib:{on}"


def test_dispatch_help_and_exit():
    handled, out, exit_ = tui.dispatch_command(_FakeBot(), "/help")
    assert handled and "Commands:" in out and not exit_
    handled, out, exit_ = tui.dispatch_command(_FakeBot(), "/exit")
    assert handled and exit_ is True


def test_dispatch_feedback_and_lang():
    assert tui.dispatch_command(_FakeBot(), "/good nice")[1] == "fb:up:nice"
    assert tui.dispatch_command(_FakeBot(), "/bad")[1] == "fb:down:"
    assert tui.dispatch_command(_FakeBot(), "/lang French")[1] == "lang:French"


def test_dispatch_contribute_parsing():
    assert tui.dispatch_command(_FakeBot(), "/contribute on")[1] == "contrib:True"
    assert tui.dispatch_command(_FakeBot(), "/contribute off")[1] == "contrib:False"


def test_non_command_is_not_handled():
    handled, out, exit_ = tui.dispatch_command(_FakeBot(), "what is 2+2")
    assert not handled and out is None and not exit_


def test_queue_command(monkeypatch):
    captured = {}
    monkeypatch.setattr("olympus.memory.watchlist_add",
                        lambda url: captured.update({"url": url}))
    handled, out, _ = tui.dispatch_command(_FakeBot(), "/queue http://x")
    assert handled and captured["url"] == "http://x"
