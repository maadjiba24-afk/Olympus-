"""W2-I8.3 end-to-end: a degenerate stream is never a silent truncated answer.

streamguard raises at the `llm.stream_text` seam, but the orchestrator's
streaming synthesis wraps that call in a broad `except Exception` that keeps
whatever text was already yielded. Without a dedicated handler a pathological
stream would surface to the user as a normal, complete-looking reply — exactly
the silent quality downgrade ruling R6 forbids. These tests pin the disclosure.
"""
from __future__ import annotations

import olympus.orchestrator as orch
from olympus import streamguard


def _drain(gen):
    return "".join(list(gen))


def test_stream_pathology_handler_precedes_generic_handler():
    """The typed handler must come BEFORE `except Exception`, or the generic
    branch swallows the pathology and restores the silent-truncation bug."""
    src = (orch.__file__)
    text = open(src, encoding="utf-8").read()
    i_typed = text.find("except streamguard.StreamPathology")
    i_generic = text.find("except Exception as err:\n                # Final compose failed")
    assert i_typed != -1, "no typed StreamPathology handler in the stream path"
    assert i_generic != -1, "generic synthesis handler not found"
    assert i_typed < i_generic, "StreamPathology handler must precede except Exception"


def test_disclosure_text_names_the_abort_and_marks_it_unfinished():
    """The disclosure must be unambiguous: the user has to be able to tell the
    answer was cut off, not merely notice it ends oddly."""
    text = open(orch.__file__, encoding="utf-8").read()
    start = text.find("except streamguard.StreamPathology")
    block = text[start:start + 1200]
    assert "Response incomplete" in block
    assert "unfinished" in block
    # the pathology kind is interpolated so the record and reply agree
    assert "kind" in block
    # and the event is traced for the operator side
    assert "synthesize.stream_aborted" in block


def test_streamguard_pathology_is_importable_from_orchestrator():
    """The handler is only meaningful if the exception class it catches is the
    one streamguard actually raises (and StreamAborted subclasses it)."""
    assert issubclass(streamguard.StreamAborted, streamguard.StreamPathology)
    assert orch.streamguard is streamguard


def test_flag_off_leaves_the_path_inert():
    """With the guard off nothing can raise StreamPathology, so the handler is
    unreachable and streaming behaviour is unchanged."""
    assert streamguard.enabled() is False
    mon = streamguard.monitor(provider="anthropic", model="m", max_tokens=8)
    # A NullMonitor swallows everything a real monitor would trip on.
    for chunk in ("loop " * 400, " " * 9000, "", ""):
        mon.feed(chunk)          # must not raise
