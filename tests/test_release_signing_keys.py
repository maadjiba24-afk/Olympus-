"""The release trust anchor holds exactly one active key, and the ledger
explains every key that ever held it (Step 1L v9).

TWO SIGNING DOMAINS, TWO POLICIES
---------------------------------
Runtime/instance signing MAY trust several keys at once — that is what makes
an overlap-window rotation possible for decision logs. RELEASE signing may
not: `olympus/witness_pubkey.txt` is the published-artifact trust anchor and
`scripts/release_pipeline.py::pinned_key` refuses to sign, verify, or check
distributions unless it lists exactly one key.

Those two rules lived in one document until v9, and the release doc told an
operator to "append the NEW key while the old one still verifies history".
Following that instruction puts two keys in the anchor, which does not create
an overlap window — it fails `pinned_key()` and takes the required `test`
check red. These tests make that contradiction unrepeatable: the anchor is
held to one key, the append-only ledger must agree with it, and the release
documentation may not instruct an overlap, a workflow enable, or a disposable
tag.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ANCHOR = _ROOT / "olympus" / "witness_pubkey.txt"
_LEDGER = _ROOT / "docs" / "RELEASE_SIGNING_KEYS.md"
_RELEASING = _ROOT / "RELEASING.md"
_SIGNING = _ROOT / "docs" / "SIGNING.md"

_HEXKEY_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_EVENTS = ("ACTIVATED", "RETIRED", "CORRECTION")
_EM_DASH = "—"


# --- reading the two sources of truth ------------------------------------------

def _anchor_keys() -> list[str]:
    """Every non-comment, non-blank line of the release trust anchor."""
    return [line.strip()
            for line in _ANCHOR.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


class LedgerError(AssertionError):
    """A malformed ledger is a build failure, never a skipped row."""


_HEADER = ["#", "Date (UTC)", "Event", "Public key", "Commit", "Evidence"]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(c and set(c) <= set("-: ") for c in cells)


def _looks_like_an_event_row(line: str) -> bool:
    """STRUCTURALLY event-like, judged without trusting the event name.

    Defining this by "cells[2] is a recognised event name" let a row saying
    REVOKED, or a five-cell row, or one with a malformed key sit after the
    table unexamined - which is exactly where a smuggled or half-deleted
    event would hide. Structure alone decides: a pipe-delimited row with
    five or more cells is an event row's shape, whatever it contains.
    """
    if not line.strip().startswith("|"):
        return False
    cells = _cells(line)
    return len(cells) >= 5 and not _is_separator(cells)


_AMENDS_RE = re.compile(r"\bamends\s+event\s+(\d+)\b", re.I)


def _correction_target(evidence: str, lineno: int) -> int:
    """The event number a CORRECTION amends, parsed explicitly.

    Stored on the event rather than merely pattern-matched, so the reference
    can be validated against the rows that actually precede it.
    """
    match = _AMENDS_RE.search(evidence)
    if match is None:
        raise LedgerError(
            f"line {lineno}: a CORRECTION must name the earlier event it "
            f"amends, spelled 'amends event N'")
    return int(match.group(1))


def _parse_event_row(cells: list[str], lineno: int) -> dict:
    if len(cells) != 6:
        raise LedgerError(
            f"line {lineno}: event row has {len(cells)} cells, expected 6")
    n, date, event, key, commit, evidence = cells
    key = key.strip("`").strip()
    commit = commit.strip("`").strip()
    if event not in _EVENTS:
        raise LedgerError(
            f"line {lineno}: unrecognised event {event!r}; expected one of "
            f"{_EVENTS}")
    if not n.isdigit():
        raise LedgerError(
            f"line {lineno}: event number {n!r} is not a positive integer")
    # Shape first, then a REAL calendar check: a regex alone accepts
    # 2026-99-99, and 2026-02-30 too.
    if not re.match(r"\A\d{4}-\d{2}-\d{2}\Z", date):
        raise LedgerError(f"line {lineno}: date {date!r} is not YYYY-MM-DD")
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise LedgerError(
            f"line {lineno}: date {date!r} is not a real calendar date")
    target = None
    if event == "CORRECTION":
        # Exactly the em dash. ASCII "-" and an empty cell are rejected so
        # the key column has one unambiguous spelling for "no key".
        if key != _EM_DASH:
            raise LedgerError(
                f"line {lineno}: a CORRECTION must carry exactly the em dash "
                f"in the key column, not {key!r}")
        target = _correction_target(evidence, lineno)
    elif not _HEXKEY_RE.match(key):
        raise LedgerError(
            f"line {lineno}: key is not 64 lowercase hex characters")
    if not re.match(r"\A[0-9a-f]{40}\Z", commit):
        raise LedgerError(
            f"line {lineno}: commit {commit!r} is not a full 40-character SHA")
    if not evidence.strip():
        raise LedgerError(f"line {lineno}: event carries no evidence")
    return {"n": n, "date": date, "event": event, "key": key,
            "commit": commit, "evidence": evidence, "line": lineno,
            "amends": target}


def _ledger_events() -> list[dict]:
    """Parse the single, contiguous event table - STRICTLY.

    Boundary rules, all enforced rather than assumed:

    * exactly ONE header row exists in the file;
    * it is followed immediately by a separator of exactly six cells;
    * event rows are CONTIGUOUS below that separator;
    * the table ends at the first line that is not a table row;
    * after it ends, any STRUCTURALLY event-like row is an ERROR - judged by
      shape, not by whether its event name is one we recognise.

    A silently-ignored row is exactly how a rewritten or smuggled history
    would hide, so nothing here is skipped quietly.
    """
    lines = _LEDGER.read_text(encoding="utf-8").splitlines()

    header_positions = [i for i, raw in enumerate(lines)
                        if raw.strip().startswith("|")
                        and _cells(raw) == _HEADER]
    if len(header_positions) != 1:
        raise LedgerError(
            f"expected exactly one event-table header, found "
            f"{len(header_positions)}")
    start = header_positions[0]

    if start + 1 >= len(lines):
        raise LedgerError("the header row is not followed by a separator")
    sep = _cells(lines[start + 1])
    if not _is_separator(sep):
        raise LedgerError(
            f"line {start + 2}: the header must be followed immediately by a "
            f"separator row")
    if len(sep) != len(_HEADER):
        raise LedgerError(
            f"line {start + 2}: the separator has {len(sep)} cells, expected "
            f"{len(_HEADER)} - the table shape must match its header")

    events: list[dict] = []
    seen: set[int] = set()
    i = start + 2
    while i < len(lines) and lines[i].strip().startswith("|"):
        cells = _cells(lines[i])
        if _is_separator(cells):
            raise LedgerError(
                f"line {i + 1}: a second separator interrupts the event rows")
        event = _parse_event_row(cells, i + 1)
        number = int(event["n"])
        if event["event"] == "CORRECTION":
            target = event["amends"]
            if target == 0:
                raise LedgerError(
                    f"line {i + 1}: a CORRECTION cannot amend event 0")
            if target == number:
                raise LedgerError(
                    f"line {i + 1}: a CORRECTION cannot amend itself")
            if target not in seen:
                raise LedgerError(
                    f"line {i + 1}: CORRECTION amends event {target}, which "
                    f"is not an earlier event in this table")
        seen.add(number)
        events.append(event)
        i += 1
    end = i                                    # first non-table line

    for j in range(end, len(lines)):
        if _looks_like_an_event_row(lines[j]):
            raise LedgerError(
                f"line {j + 1}: a structurally event-like row appears after "
                f"the event table ended (line {end}); new events belong "
                f"immediately below the last existing row")

    if not events:
        raise LedgerError("the ledger contains no events")
    return events


def _replay(events=None) -> list[str]:
    """Replay the ledger, enforcing the invariants at EVERY step.

    Never more than one active key — not just at the end, but at no point in
    the history. `RETIRED` must remove the key that is currently active, and
    a retired key is never reactivated.
    """
    events = _ledger_events() if events is None else events
    active: list[str] = []
    retired: set[str] = set()
    for event in events:
        n, kind, key = event["n"], event["event"], event["key"]
        if kind == "CORRECTION":
            continue                       # provenance only: no state change
        if kind == "ACTIVATED":
            if key in retired:
                raise LedgerError(
                    f"event {n}: reactivates a retired key; rotation is "
                    f"forward-only")
            if active:
                raise LedgerError(
                    f"event {n}: activates a second key while {active[0][:8]}"
                    f"… is still active — retire first")
            active.append(key)
        elif kind == "RETIRED":
            if not active:
                raise LedgerError(
                    f"event {n}: retires a key when none is active")
            if key != active[0]:
                raise LedgerError(
                    f"event {n}: retires a key that is not the active one")
            active.remove(key)
            retired.add(key)
        if len(active) > 1:                # belt and braces
            raise LedgerError(f"event {n}: more than one key active")
    return active


def _replayed_active_keys() -> list[str]:
    return _replay()


# --- 1. exactly one active release key -----------------------------------------

def test_the_anchor_holds_exactly_one_active_release_key():
    keys = _anchor_keys()
    assert len(keys) == 1, (
        f"witness_pubkey.txt lists {len(keys)} keys; the release anchor holds "
        f"exactly one. Multi-key overlap is a runtime facility "
        f"(OLYMPUS_PINNED_PUBKEY), not a release one.")


def test_the_active_key_is_valid_lowercase_hex():
    key = _anchor_keys()[0]
    assert _HEXKEY_RE.match(key), (
        "the active release key must be 64 lowercase hex characters")


def test_the_release_pipeline_agrees_the_anchor_is_loadable():
    """`pinned_key()` is the gate the signer actually runs; if it rejects the
    committed anchor, no release can be signed."""
    import sys
    sys.path.insert(0, str(_ROOT / "scripts"))
    import release_pipeline as rp
    assert rp.pinned_key(_ROOT) == _anchor_keys()[0].lower()


# --- 2. the anchor matches the latest ledger state -----------------------------

def test_the_ledger_replays_to_exactly_one_active_key():
    active = _replayed_active_keys()
    assert len(active) == 1, (
        f"replaying the ledger leaves {len(active)} active keys; the release "
        f"policy permits exactly one")


def test_the_active_key_matches_the_latest_ledger_state():
    assert _replayed_active_keys() == [_anchor_keys()[0].lower()], (
        "olympus/witness_pubkey.txt and docs/RELEASE_SIGNING_KEYS.md disagree "
        "about which key is active — a rotation edited one but not the other")


# --- event 1 is frozen ---------------------------------------------------------
#
# Established from `git log --follow -- olympus/witness_pubkey.txt` at
# 596f0760: commit 74ba2144 created the anchor with this key, in "Release
# 0.18.0: hardening + pinned signing key + strengthened modules". Pinned here
# literally so an append-only ledger cannot be quietly rewritten — a changed
# date, number, key or commit fails this test, not a review.

_EVENT_1 = {
    "n": "1",
    "date": "2026-06-27",
    "event": "ACTIVATED",
    "key": "350f970ac5159b30f6736c124a1e468cd1cc82ddd73cb24799057c5c3b0b0336",
    "commit": "74ba2144cf6f0a7305181f73e7d6ac4c111cca6d",
}


# The genesis row FROZEN WHOLE, evidence included. The ledger promises that
# historical evidence is never edited; a field-by-field check would let the
# evidence cell be quietly reworded. This digest is over the exact row text.
_EVENT_1_ROW_SHA256 = (
    "2524bb2ff0d9ca9a376d75fed642c62d10b37e4a483331e9d883c45fff476da4")


@pytest.mark.parametrize("field", sorted(_EVENT_1))
def test_event_one_is_immutable(field):
    first = _ledger_events()[0]
    assert first[field] == _EVENT_1[field], (
        f"ledger event 1 {field} was rewritten: {first[field]!r} != "
        f"{_EVENT_1[field]!r}. Historical events are append-only.")


def test_the_whole_genesis_row_is_frozen_including_its_evidence():
    lines = _LEDGER.read_text(encoding="utf-8").splitlines()
    rows = [ln for ln in lines if ln.strip().startswith("| 1 |")]
    assert len(rows) == 1, "expected exactly one genesis row"
    digest = hashlib.sha256(rows[0].encode("utf-8")).hexdigest()
    assert digest == _EVENT_1_ROW_SHA256, (
        "the genesis event row changed. Historical events — evidence cell "
        "included — are append-only and must never be edited. If an earlier "
        "row is genuinely wrong, append a CORRECTION event instead.")


def test_event_one_evidence_still_names_its_provenance():
    evidence = _ledger_events()[0]["evidence"].lower()
    assert "0.18.0" in evidence, "event 1 must name the first release tag"
    assert "v0.17.0" in evidence, (
        "event 1 must record that the preceding tag had no anchor")


def test_the_ledger_records_the_current_key_activation():
    """The key in use must have an ACTIVATED event carrying real evidence."""
    active = _anchor_keys()[0].lower()
    activations = [e for e in _ledger_events()
                   if e["event"] == "ACTIVATED" and e["key"] == active]
    assert len(activations) == 1, (
        f"expected exactly one ACTIVATED event for the active key, "
        f"found {len(activations)}")
    event = activations[0]
    assert re.match(r"\A\d{4}-\d{2}-\d{2}\Z", event["date"]), (
        "an activation date must be a real ISO date from git history")
    assert re.match(r"\A[0-9a-f]{40}\Z", event["commit"]), (
        "an activation must cite the full commit SHA that established it")
    assert len(event["evidence"]) > 40, "an activation needs stated evidence"


# --- 3. ledger keys are unique and well-formed ---------------------------------

def test_every_ledger_key_is_valid_lowercase_hex():
    for event in _ledger_events():
        if event["event"] == "CORRECTION" and event["key"] in ("", "—", "-"):
            continue
        assert _HEXKEY_RE.match(event["key"]), (
            f"ledger event {event['n']} carries a malformed key")


def test_no_key_is_activated_twice():
    """Re-activating a retired key would silently re-trust it."""
    activated = [e["key"] for e in _ledger_events()
                 if e["event"] == "ACTIVATED"]
    assert len(activated) == len(set(activated)), (
        "a public key appears in more than one ACTIVATED event")


def test_no_key_is_retired_twice():
    retired = [e["key"] for e in _ledger_events() if e["event"] == "RETIRED"]
    assert len(retired) == len(set(retired)), (
        "a public key appears in more than one RETIRED event")


def test_a_key_is_never_retired_before_it_is_activated():
    seen: set[str] = set()
    for event in _ledger_events():
        if event["event"] == "ACTIVATED":
            seen.add(event["key"])
        elif event["event"] == "RETIRED":
            assert event["key"] in seen, (
                f"ledger event {event['n']} retires a key that was never "
                f"activated")


def test_event_numbers_are_sequential_from_one():
    """Append-only: a renumbered or removed row shows up here."""
    numbers = [e["n"] for e in _ledger_events()]
    assert numbers == [str(i) for i in range(1, len(numbers) + 1)], (
        f"ledger event numbers are not sequential: {numbers}")


def test_event_dates_never_go_backwards():
    dates = [e["date"] for e in _ledger_events()]
    assert dates == sorted(dates), (
        f"ledger dates are not non-decreasing: {dates}")


# --- the parser and replay actually bite ---------------------------------------
#
# Synthetic histories, so a weakened check cannot pass silently on a ledger
# that currently holds one well-formed event.

_K1 = "1" * 64
_K2 = "2" * 64
_K3 = "3" * 64
_C = "a" * 40


def _ev(n, kind, key, date="2026-01-01", commit=_C):
    return {"n": str(n), "date": date, "event": kind, "key": key,
            "commit": commit, "evidence": "synthetic", "line": n}


def test_replay_rejects_two_simultaneously_active_keys():
    with pytest.raises(LedgerError, match="second key"):
        _replay([_ev(1, "ACTIVATED", _K1), _ev(2, "ACTIVATED", _K2)])


def test_replay_rejects_retiring_a_key_that_is_not_active():
    with pytest.raises(LedgerError, match="not the active one"):
        _replay([_ev(1, "ACTIVATED", _K1), _ev(2, "RETIRED", _K2)])


def test_replay_rejects_retiring_when_nothing_is_active():
    with pytest.raises(LedgerError, match="none is active"):
        _replay([_ev(1, "RETIRED", _K1)])


def test_replay_rejects_reactivating_a_retired_key():
    """Forward-only: the retired key is the over-shared one."""
    with pytest.raises(LedgerError, match="forward-only"):
        _replay([_ev(1, "ACTIVATED", _K1), _ev(2, "RETIRED", _K1),
                 _ev(3, "ACTIVATED", _K2), _ev(4, "RETIRED", _K2),
                 _ev(5, "ACTIVATED", _K1)])


def test_replay_accepts_a_well_formed_forward_rotation():
    assert _replay([_ev(1, "ACTIVATED", _K1), _ev(2, "RETIRED", _K1),
                    _ev(3, "ACTIVATED", _K2)]) == [_K2]


def test_replay_accepts_two_successive_forward_rotations():
    """Losing a new seed means rotating forward again, not reverting."""
    assert _replay([_ev(1, "ACTIVATED", _K1), _ev(2, "RETIRED", _K1),
                    _ev(3, "ACTIVATED", _K2), _ev(4, "RETIRED", _K2),
                    _ev(5, "ACTIVATED", _K3)]) == [_K3]


def test_a_correction_never_changes_replay_state():
    without = _replay([_ev(1, "ACTIVATED", _K1)])
    with_correction = _replay([_ev(1, "ACTIVATED", _K1),
                               _ev(2, "CORRECTION", "—")])
    assert without == with_correction == [_K1]


# --- ledger doctoring: rows go INSIDE the contiguous table ---------------------

def _doctored_ledger(tmp_path, monkeypatch, *, inside=(), after=()):
    """Build a synthetic history rooted at the immutable genesis event.

    Live post-genesis rotations are replaced by ``inside`` so these parser and
    replay tests remain deterministic as the real append-only ledger grows.
    """
    lines = _LEDGER.read_text(encoding="utf-8").splitlines()
    parsed = _ledger_events()
    first = next(i for i, ln in enumerate(lines)
                 if ln.strip().startswith("| 1 |"))
    last = parsed[-1]["line"] - 1
    assert last >= first
    out = lines[:first + 1] + list(inside) + lines[last + 1:] + list(after)
    fake = tmp_path / "RELEASE_SIGNING_KEYS.md"
    fake.write_text("\n".join(out) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_LEDGER", fake)
    return fake


_BAD_ROWS = {
    "short key":       f"| 2 | 2026-01-01 | ACTIVATED | deadbeef | {_C} | x |",
    "bad event":       f"| 2 | 2026-01-01 | REVOKED | {_K1} | {_C} | x |",
    "bad date shape":  f"| 2 | 01/01/2026 | ACTIVATED | {_K1} | {_C} | x |",
    "impossible date": f"| 2 | 2026-99-99 | ACTIVATED | {_K1} | {_C} | x |",
    "feb 30":          f"| 2 | 2026-02-30 | ACTIVATED | {_K1} | {_C} | x |",
    "short sha":       f"| 2 | 2026-01-01 | ACTIVATED | {_K1} | abc123 | x |",
    "five cells":      f"| 2 | 2026-01-01 | ACTIVATED | {_K1} | {_C} |",
    "non-numeric n":   f"| two | 2026-01-01 | ACTIVATED | {_K1} | {_C} | x |",
    "keyed correction": (f"| 2 | 2026-01-01 | CORRECTION | {_K1} | {_C} "
                         f"| amends event 1 |"),
    "uppercase key":   (f"| 2 | 2026-01-01 | ACTIVATED | {('ab' * 32).upper()}"
                        f" | {_C} | x |"),
    "uppercase sha":   (f"| 2 | 2026-01-01 | ACTIVATED | {_K1} | {_C.upper()}"
                        f" | x |"),
    "empty evidence":  f"| 2 | 2026-01-01 | ACTIVATED | {_K1} | {_C} |  |",
    "correction without target": (f"| 2 | 2026-01-01 | CORRECTION | — "
                                  f"| {_C} | tidied the wording |"),
}


@pytest.mark.parametrize("label", sorted(_BAD_ROWS))
def test_a_malformed_row_inside_the_table_is_rejected(tmp_path, monkeypatch,
                                                      label):
    """v9 silently `continue`d past rows it could not parse."""
    _doctored_ledger(tmp_path, monkeypatch, inside=[_BAD_ROWS[label]])
    with pytest.raises(LedgerError):
        _ledger_events()


@pytest.mark.parametrize("label", ["short key", "impossible date",
                                   "uppercase key", "short sha"])
def test_a_malformed_row_after_the_table_is_also_rejected(tmp_path,
                                                          monkeypatch, label):
    """The table ends at the first non-table line; nothing later may look
    like an event row.

    Only rows whose event NAME is recognised count as event-like — a row
    saying `REVOKED` after the table is not a smuggled event, it is prose
    that happens to contain pipes, and is correctly left alone.
    """
    _doctored_ledger(tmp_path, monkeypatch, after=["", _BAD_ROWS[label]])
    with pytest.raises(LedgerError):
        _ledger_events()


def test_a_well_formed_event_row_after_the_table_still_fails(tmp_path,
                                                             monkeypatch):
    """A perfectly-formed row in the wrong PLACE is the dangerous case: it
    would read as history while sitting outside the parsed table."""
    row = f"| 2 | 2026-09-01 | RETIRED | {_K1} | {_C} | smuggled |"
    _doctored_ledger(tmp_path, monkeypatch,
                     after=["", "## Later section", row])
    with pytest.raises(LedgerError, match="after the event table ended"):
        _ledger_events()


def test_a_second_event_table_is_rejected(tmp_path, monkeypatch):
    second = ["", "## Another table", "",
              "| # | Date (UTC) | Event | Public key | Commit | Evidence |",
              "|---|---|---|---|---|---|",
              f"| 2 | 2026-09-01 | ACTIVATED | {_K1} | {_C} | second |"]
    _doctored_ledger(tmp_path, monkeypatch, after=second)
    with pytest.raises(LedgerError, match="exactly one event-table header"):
        _ledger_events()


def test_rows_detached_from_the_table_are_not_silently_absorbed(tmp_path,
                                                                monkeypatch):
    """A blank line ends the table. A row after it is out of bounds."""
    row = f"| 2 | 2026-09-01 | ACTIVATED | {_K1} | {_C} | detached |"
    _doctored_ledger(tmp_path, monkeypatch, inside=["", row])
    with pytest.raises(LedgerError, match="after the event table ended"):
        _ledger_events()


def test_a_second_separator_inside_the_rows_is_rejected(tmp_path, monkeypatch):
    _doctored_ledger(tmp_path, monkeypatch,
                     inside=["|---|---|---|---|---|---|"])
    with pytest.raises(LedgerError, match="second separator"):
        _ledger_events()


def test_a_forward_rotation_appended_in_place_is_accepted(tmp_path,
                                                          monkeypatch):
    """Guard the guard: the strict parser must not reject everything."""
    active = _EVENT_1['key']
    rows = [f"| 2 | 2026-09-01 | RETIRED | `{active}` | `{_C}` | rotated |",
            f"| 3 | 2026-09-01 | ACTIVATED | `{_K2}` | `{_C}` | replacement |"]
    _doctored_ledger(tmp_path, monkeypatch, inside=rows)
    events = _ledger_events()
    assert len(events) == 3
    assert _replay(events) == [_K2]


def test_a_correction_naming_an_earlier_event_is_accepted(tmp_path,
                                                          monkeypatch):
    row = (f"| 2 | 2026-09-01 | CORRECTION | — | `{_C}` "
           f"| amends event 1: the commit SHA was mistyped |")
    _doctored_ledger(tmp_path, monkeypatch, inside=[row])
    events = _ledger_events()
    assert len(events) == 2
    # Provenance only: replay state is untouched.
    assert _replay(events) == [_EVENT_1['key']]


@pytest.mark.parametrize("date,ok", [
    ("2026-06-27", True), ("2024-02-29", True),
    ("2026-99-99", False), ("2026-02-30", False), ("2026-13-01", False),
    ("2026-00-10", False), ("2023-02-29", False), ("2026-06-31", False),
])
def test_dates_are_validated_against_a_real_calendar(date, ok):
    """A YYYY-MM-DD regex alone accepts 2026-99-99."""
    cells = ["2", date, "ACTIVATED", _K1, _C, "x"]
    if ok:
        assert _parse_event_row(cells, 1)["date"] == date
    else:
        with pytest.raises(LedgerError, match="date"):
            _parse_event_row(cells, 1)


# --- 1. CORRECTION references are parsed and validated -------------------------

def test_the_correction_target_is_parsed_and_stored():
    """Not merely pattern-matched: the number is kept on the event so it can
    be checked against the rows that actually precede it."""
    assert _correction_target("amends event 7: mistyped SHA", 1) == 7
    assert _correction_target("Amends Event 12", 1) == 12
    event = _parse_event_row(
        ["3", "2026-09-01", "CORRECTION", _EM_DASH, _C, "amends event 2"], 1)
    assert event["amends"] == 2


@pytest.mark.parametrize("evidence", [
    "tidied the wording",
    "amends the earlier row",
    "see event two",
    "amends event",
    "amendsevent 1",
    "corrects entry 1",
])
def test_a_correction_without_a_parsable_target_is_rejected(evidence):
    with pytest.raises(LedgerError, match="amends event N"):
        _parse_event_row(
            ["2", "2026-09-01", "CORRECTION", _EM_DASH, _C, evidence], 1)


def _row(n, kind, key, evidence, date="2026-09-01"):
    return f"| {n} | {date} | {kind} | {key} | `{_C}` | {evidence} |"


def test_a_correction_amending_an_earlier_event_is_accepted(tmp_path,
                                                            monkeypatch):
    _doctored_ledger(tmp_path, monkeypatch, inside=[
        _row(2, "CORRECTION", _EM_DASH, "amends event 1: SHA was mistyped")])
    events = _ledger_events()
    assert events[1]["amends"] == 1
    assert _replay(events) == [_EVENT_1['key']]      # provenance only


def test_a_correction_amending_event_zero_is_rejected(tmp_path, monkeypatch):
    _doctored_ledger(tmp_path, monkeypatch, inside=[
        _row(2, "CORRECTION", _EM_DASH, "amends event 0")])
    with pytest.raises(LedgerError, match="cannot amend event 0"):
        _ledger_events()


def test_a_correction_amending_itself_is_rejected(tmp_path, monkeypatch):
    _doctored_ledger(tmp_path, monkeypatch, inside=[
        _row(2, "CORRECTION", _EM_DASH, "amends event 2")])
    with pytest.raises(LedgerError, match="cannot amend itself"):
        _ledger_events()


def test_a_correction_amending_a_future_event_is_rejected(tmp_path,
                                                          monkeypatch):
    """Event 3 exists, but it comes AFTER this correction."""
    _doctored_ledger(tmp_path, monkeypatch, inside=[
        _row(2, "CORRECTION", _EM_DASH, "amends event 3"),
        _row(3, "RETIRED", f"`{_EVENT_1['key']}`", "rotated"),
    ])
    with pytest.raises(LedgerError, match="not an earlier event"):
        _ledger_events()


def test_a_correction_amending_a_nonexistent_event_is_rejected(tmp_path,
                                                               monkeypatch):
    _doctored_ledger(tmp_path, monkeypatch, inside=[
        _row(2, "CORRECTION", _EM_DASH, "amends event 99")])
    with pytest.raises(LedgerError, match="not an earlier event"):
        _ledger_events()


def test_a_later_correction_may_amend_any_earlier_event(tmp_path, monkeypatch):
    active = _EVENT_1['key']
    _doctored_ledger(tmp_path, monkeypatch, inside=[
        _row(2, "RETIRED", f"`{active}`", "rotated"),
        _row(3, "ACTIVATED", f"`{_K2}`", "replacement"),
        _row(4, "CORRECTION", _EM_DASH, "amends event 1: date corrected"),
    ])
    events = _ledger_events()
    assert events[3]["amends"] == 1
    assert _replay(events) == [_K2]


# --- 2. detached rows are judged STRUCTURALLY ----------------------------------

_AFTER_TABLE_ROWS = {
    "unknown event REVOKED": f"| 2 | 2026-09-01 | REVOKED | {_K1} | {_C} | x |",
    "five cells":            f"| 2 | 2026-09-01 | ACTIVATED | {_K1} | {_C} |",
    "invalid number":        f"| two | 2026-09-01 | ACTIVATED | {_K1} | {_C} | x |",
    "invalid date":          f"| 2 | 2026-99-99 | ACTIVATED | {_K1} | {_C} | x |",
    "malformed key":         f"| 2 | 2026-09-01 | ACTIVATED | deadbeef | {_C} | x |",
    "malformed sha":         f"| 2 | 2026-09-01 | ACTIVATED | {_K1} | abc123 | x |",
    "well formed":           f"| 2 | 2026-09-01 | RETIRED | {_K1} | {_C} | x |",
    "empty cells":           "| | | | | | |",
    "lowercase event":       f"| 2 | 2026-09-01 | activated | {_K1} | {_C} | x |",
}


@pytest.mark.parametrize("label", sorted(_AFTER_TABLE_ROWS))
def test_any_structurally_event_like_row_after_the_table_is_rejected(
        tmp_path, monkeypatch, label):
    """AUDIT (v12): v11 called a row "event-like" only when its third cell
    was a RECOGNISED event name, so `REVOKED`, a five-cell row, or a row with
    a malformed key sat after the table unexamined — exactly where a smuggled
    or half-deleted event would hide."""
    _doctored_ledger(tmp_path, monkeypatch,
                     after=["", "## Later prose", _AFTER_TABLE_ROWS[label]])
    with pytest.raises(LedgerError, match="after the event table ended"):
        _ledger_events()


def test_structural_detection_does_not_flag_ordinary_prose():
    """Guard the guard: a pipe in a sentence is not an event row."""
    for benign in ("Use `a | b` to pipe.", "", "## Heading",
                   "- a bullet with | a pipe", "| two | cells |"):
        assert not _looks_like_an_event_row(benign), benign


def test_structural_detection_ignores_separators():
    assert not _looks_like_an_event_row("|---|---|---|---|---|---|")


# --- 3. exact table syntax -----------------------------------------------------

def test_the_separator_must_have_exactly_six_cells(tmp_path, monkeypatch):
    lines = _LEDGER.read_text(encoding="utf-8").splitlines()
    sep = next(i for i, ln in enumerate(lines)
               if _is_separator(_cells(ln)) and ln.strip().startswith("|"))
    lines[sep] = "|---|---|---|"
    fake = tmp_path / "RELEASE_SIGNING_KEYS.md"
    fake.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_LEDGER", fake)
    with pytest.raises(LedgerError, match="separator has 3 cells"):
        _ledger_events()


def test_the_real_separator_has_exactly_six_cells():
    lines = _LEDGER.read_text(encoding="utf-8").splitlines()
    sep = next(_cells(ln) for ln in lines
               if ln.strip().startswith("|") and _is_separator(_cells(ln)))
    assert len(sep) == 6


@pytest.mark.parametrize("key,label", [
    ("-", "ascii hyphen"),
    ("", "empty"),
    ("--", "double hyphen"),
    ("–", "en dash"),
    ("n/a", "prose"),
])
def test_a_correction_key_column_must_be_exactly_the_em_dash(key, label):
    with pytest.raises(LedgerError, match="em dash"):
        _parse_event_row(
            ["2", "2026-09-01", "CORRECTION", key, _C, "amends event 1"], 1)


def test_the_em_dash_is_accepted_for_a_correction():
    event = _parse_event_row(
        ["2", "2026-09-01", "CORRECTION", _EM_DASH, _C, "amends event 1"], 1)
    assert event["key"] == _EM_DASH


# --- 4. commit A's UTC date is captured with its SHA ---------------------------

_DATE_DOCS = ("RELEASING.md", "docs/RELEASE_SIGNING_KEYS.md")


@pytest.mark.parametrize("relpath", _DATE_DOCS)
def test_the_capture_command_reads_both_sha_and_utc_date(relpath):
    """A ledger row needs commit A's SHA *and* its UTC date. v11 captured
    only the SHA, leaving the date to whatever day commit B was written."""
    flowed = _flow(_ROOT / relpath)
    assert "tz=utc0" in flowed, (
        f"{relpath} must force UTC when rendering the committer date")
    assert "git show -s" in flowed
    assert "%h %cd" in flowed, (
        f"{relpath} must capture the SHA and committer date together")
    assert "format-local:%y-%m-%d" in flowed


@pytest.mark.parametrize("relpath", _DATE_DOCS)
def test_both_rotation_rows_must_carry_the_captured_utc_date(relpath):
    flowed = _flow(_ROOT / relpath)
    assert "utc date" in flowed
    assert "both" in flowed, (
        f"{relpath} must say BOTH rows carry the captured date")


def test_the_rotation_section_forbids_dating_rows_by_commit_b():
    section = _rotation_section()
    assert "not today's date" in section or "not the date commit b" in section, (
        "the procedure must rule out dating the rows by when B was written")


@pytest.mark.parametrize("relpath", _DATE_DOCS)
def test_the_utc_requirement_is_explained_not_just_asserted(relpath):
    flowed = _flow(_ROOT / relpath)
    assert "local" in flowed and "day" in flowed, (
        f"{relpath} must explain why TZ=UTC0 matters (local zone can shift "
        f"the date by a day)")


# --- 4. release docs never instruct an overlap ---------------------------------

_RELEASE_DOCS = ("RELEASING.md", "olympus/witness_pubkey.txt",
                 "docs/RELEASE_SIGNING_KEYS.md")


def _flow(path: Path) -> str:
    """Lowercased, unwrapped, with markdown emphasis stripped, so a phrase
    split across lines or written as *before* still matches."""
    text = path.read_text(encoding="utf-8").lower()
    text = re.sub(r"\s+", " ", text.replace("\n#", " "))
    # Only `*` and backticks: underscores are load-bearing in these docs
    # (release_signing_keys.md, OLYMPUS_PINNED_PUBKEY, disabled_manually).
    return re.sub(r"[*`]", "", text)


_NEGATIONS = ("do not", "don't", "never", "must not", "cannot", "no ")


def _positive_clauses(section: str) -> str:
    """The section with every negated clause removed.

    A prohibition ("do not enable the workflow") contains the same words as
    the instruction it forbids. Scanning raw text for "enable the workflow"
    therefore flags the very sentence that bans it. Drop the negated
    sentences, so what remains is only what the document tells an operator
    to DO.

    Split on SENTENCE boundaries only, never on "and"/"or": a single
    prohibition often governs a coordinated list ("do not enable the
    workflow, and do not create a disposable tag or test release"), and
    splitting at the conjunction would strand "test release" without its
    negation.
    """
    clauses = re.split(r"[.;:]| — ", section)
    return " ".join(c for c in clauses
                    if not any(n in c for n in _NEGATIONS))


@pytest.mark.parametrize("relpath", _RELEASE_DOCS)
def test_release_documentation_never_instructs_a_two_key_overlap(relpath):
    """AUDIT (v9): the anchor's own header used to say "append the NEW key
    while the old one still verifies history" — an instruction that breaks
    the pipeline it was written for."""
    flowed = _flow(_ROOT / relpath)
    for forbidden in (
        "append the new key",
        "append the new public key to `olympus/witness_pubkey.txt`",
        "keep the old line",
        "both keys are now pinned",
        "one key per line",
        "overlap window, not a flag day",
    ):
        assert forbidden not in flowed, (
            f"{relpath} instructs a release-key overlap: {forbidden!r}")


@pytest.mark.parametrize("relpath", _RELEASE_DOCS)
def test_release_documentation_states_the_one_active_key_rule(relpath):
    flowed = _flow(_ROOT / relpath)
    assert "exactly one active" in flowed or "one active key" in flowed, (
        f"{relpath} must state the single-active-key rule")


# --- 5. rotation never enables the workflow or cuts a throwaway tag ------------

def _rotation_section() -> str:
    text = _RELEASING.read_text(encoding="utf-8")
    start = text.lower().index("## rotating the release signing key")
    end = text.lower().index("## release checklist", start)
    section = re.sub(r"\s+", " ", text[start:end].lower())
    return re.sub(r"[*`]", "", section)


def test_a_release_rotation_procedure_exists():
    assert "## Rotating the release signing key" in _RELEASING.read_text(
        encoding="utf-8")


def test_rotation_never_instructs_enabling_the_workflow():
    section = _rotation_section()
    positive = _positive_clauses(section)
    for forbidden in ("enable the workflow", "enable publish.yml",
                      "re-enable the workflow", "enable_workflow",
                      "gh workflow enable"):
        assert forbidden not in positive, (
            f"the rotation procedure instructs enabling publishing: "
            f"{forbidden!r}")
    # …and says so explicitly, rather than merely omitting it.
    assert "do not enable the workflow" in section, (
        "the procedure must say explicitly not to enable publishing")


def test_rotation_never_instructs_a_disposable_release_or_tag():
    section = _rotation_section()
    positive = _positive_clauses(section)
    for forbidden in ("test release", "disposable tag", "throwaway tag",
                      "test tag", "git tag", "gh workflow run",
                      "pre-release tag"):
        assert forbidden not in positive, (
            f"the rotation procedure instructs cutting a tag/release: "
            f"{forbidden!r}")
    assert "do not create a disposable tag" in section, (
        "the procedure must forbid a disposable tag or test release")


def test_the_negation_filter_would_catch_a_real_instruction():
    """Guard the guard: if _positive_clauses swallowed everything, the two
    tests above would pass vacuously."""
    # A genuine instruction survives the filter…
    assert "enable the workflow" in _positive_clauses(
        "first enable the workflow, then dispatch")
    assert "git tag" in _positive_clauses(
        "run git tag v9.9.9 to exercise the pipeline")
    # …and a prohibition does not, including across a coordinated list.
    assert "enable the workflow" not in _positive_clauses(
        "do not enable the workflow at any point")
    assert "test release" not in _positive_clauses(
        "do not create a disposable tag or test release to exercise it")
    # A real instruction in a LATER sentence is still caught.
    assert "git tag" in _positive_clauses(
        "do not enable the workflow. then run git tag v9.9.9")


def test_rotation_requires_publishing_to_be_disabled_first():
    section = _rotation_section()
    assert "disabled_manually" in section
    assert "no release in flight" in section


def test_rotation_orders_pin_before_secret_and_verifies_by_metadata():
    section = _rotation_section()
    assert "reviewed pr" in section, "the pin change must go through review"
    assert "before installing the secret" in section, (
        "the pin must be merged before the secret is installed")
    assert "metadata" in section, "verification must be metadata-only"
    assert "404" in section, "the removal check must be the 404 on repo scope"
    assert "fails closed" in section or "fail closed" in section


def test_rotation_records_events_in_the_ledger():
    section = _rotation_section()
    assert "release_signing_keys.md" in section
    assert "retired" in section and "activated" in section
    assert "do not edit existing ledger events" in section


# --- the two-commit evidence chain (a commit cannot cite its own SHA) ----------

_EVIDENCE_DOCS = ("RELEASING.md", "docs/RELEASE_SIGNING_KEYS.md",
                  "olympus/witness_pubkey.txt")


@pytest.mark.parametrize("relpath", _EVIDENCE_DOCS)
def test_no_document_asks_an_event_to_cite_its_own_commit(relpath):
    """AUDIT (v10): v9 said each ledger row should cite "this PR's commit".
    The ledger row IS in that PR, so the SHA it must cite does not exist when
    the row is written — the instruction was impossible to follow."""
    flowed = _flow(_ROOT / relpath)
    for impossible in ("citing this pr", "this pr's commit", "this pr’s commit",
                       "the pr's own commit", "the pr’s own commit"):
        assert impossible not in flowed, (
            f"{relpath} asks an event to cite its own commit: {impossible!r}")


def test_the_rotation_uses_two_commits_with_the_ledger_citing_the_pin():
    section = _rotation_section()
    assert "commit a" in section and "commit b" in section, (
        "the procedure must name the two commits explicitly")
    assert "cannot cite its own hash" in section, (
        "the procedure must explain WHY two commits are required")
    assert "git show -s" in section, (
        "the procedure must show how to read commit A's SHA and date")
    assert "sha of commit a" in section or "sha and its utc" in section, (
        "commit B must cite commit A's full SHA")


def test_commit_a_sha_is_captured_with_head_not_head_tilde_one():
    """AUDIT (v11): v10 said commit A's SHA "can be read with
    `git rev-parse HEAD~1`". At the moment commit B is being written, B does
    not exist yet — HEAD *is* A, and HEAD~1 is the commit BEFORE A. Following
    v10 literally would have put the wrong SHA in both ledger rows."""
    section = _rotation_section()
    positive = _positive_clauses(section)

    # HEAD~1 must not be presented as the way to OBTAIN A's SHA.
    for wrong in ("read with git rev-parse head~1",
                  "can be read with git rev-parse head~1",
                  "sha of commit a, which now exists and can be read with "
                  "git rev-parse head~1"):
        assert wrong not in positive, (
            f"HEAD~1 is offered as the source of commit A's SHA: {wrong!r}")

    # The capture step exists, happens before commit B, and reads plain HEAD.
    capture = [ln for ln in section.split(" ") if ln]
    assert "git show -s" in section, (
        "the procedure must capture from commit A via `git show -s`")
    idx = section.index("git show -s")
    command = section[idx:idx + 120]
    assert command.rstrip().endswith("head") or " head" in command, (
        f"the capture command must read HEAD, not HEAD~1: {command!r}")
    assert "head~1" not in command, (
        "the capture command itself must not use HEAD~1")
    assert "before writing commit b" in section, (
        "the capture must be ordered before commit B is written")


def test_head_tilde_one_is_only_a_post_hoc_verification():
    section = _rotation_section()
    if "head~1" not in section:
        pytest.fail("HEAD~1 should still appear, as a verification step")
    idx = section.index("head~1")
    window = section[max(0, idx - 260):idx + 260]
    assert "verif" in window or "confirms" in window, (
        "HEAD~1 must be framed as a check that B's parent is A")
    assert "once commit b exists" in window or "after" in window, (
        "the verification must be explicitly after commit B exists")


@pytest.mark.parametrize("relpath", ("RELEASING.md",
                                     "docs/RELEASE_SIGNING_KEYS.md"))
def test_no_document_obtains_commit_a_from_head_tilde_one(relpath):
    flowed = _flow(_ROOT / relpath)
    idx = flowed.find("head~1")
    while idx != -1:
        window = flowed[max(0, idx - 300):idx + 120]
        assert "verif" in window or "confirms" in window or \
               "not how" in window or "is not how" in window, (
            f"{relpath} uses HEAD~1 without framing it as a verification")
        idx = flowed.find("head~1", idx + 1)


def test_the_rotation_forbids_squash_and_rebase_merges():
    section = _rotation_section()
    assert "never squash" in section and "never rebase" in section, (
        "a squash merge rewrites commit A away and dangles every citation")
    assert "merge commit" in section
    assert "ancestor of" in section, (
        "the procedure must state why commit A has to survive")


def test_the_ledger_itself_documents_the_two_commit_shape():
    flowed = _flow(_LEDGER)
    assert "commit a" in flowed and "commit b" in flowed
    assert "never squash" in flowed


# --- forward-only rotation -----------------------------------------------------

def test_rotation_is_declared_forward_only():
    section = _rotation_section()
    assert "forward-only" in section


def test_abandoning_before_merge_is_documented_as_safe():
    section = _rotation_section()
    assert "abandoned safely" in section or "abandon" in section
    assert "close the pr" in section


def test_merging_the_pr_is_the_point_of_no_return():
    """v9 put the point of no return at deleting the secret. It is earlier:
    once the pin merges, the old key is retired and must not come back."""
    section = _rotation_section()
    assert "point of no return" in section
    idx = section.index("point of no return")
    window = section[max(0, idx - 120):idx + 60]
    assert "merg" in window, (
        "the point of no return must be tied to MERGING the pin PR")


def test_a_retired_key_is_never_reactivated():
    section = _rotation_section()
    assert "never reactivated" in section or "never be reactivated" in section
    assert "over-shared" in section, (
        "the doc must say WHY the retired key must not return")


def test_recovery_paths_are_forward_only():
    section = _rotation_section()
    assert "retry step 5" in section or "retry" in section, (
        "a failed secret install must be a retry, not a revert")
    assert "securely retained" in section
    assert "another forward rotation" in section, (
        "a lost seed must lead to another forward rotation")


def test_the_invalid_revert_rollback_is_gone():
    """v9 told the operator to revert the PR with a CORRECTION event."""
    section = _rotation_section()
    for gone in ("revert the step-3 pr", "revert the pr",
                 "appending a further correction event rather than deleting"):
        assert gone not in section, (
            f"the invalid rollback survives: {gone!r}")


@pytest.mark.parametrize("relpath", ("RELEASING.md",
                                     "docs/RELEASE_SIGNING_KEYS.md"))
def test_corrections_are_provenance_only(relpath):
    flowed = _flow(_ROOT / relpath)
    assert "provenance" in flowed
    assert "never change which key is active" in flowed or \
           "must not change which key is active" in flowed, (
        f"{relpath} must state a CORRECTION cannot alter active-key state")


# --- secret installation -------------------------------------------------------

def test_the_seed_is_streamed_from_the_file_not_pasted():
    section = _rotation_section()
    assert "gh secret set olympus_signing_seed --env release-signing <" in \
        section, "the seed must be streamed from the 0600 file via stdin"


def test_no_unsafe_seed_handling_is_instructed():
    section = _rotation_section()
    positive = _positive_clauses(section)
    for unsafe in ("--body", "clipboard", "paste it", "echo the seed",
                   "cat the seed", "shell history"):
        assert unsafe not in positive, (
            f"the procedure instructs unsafe seed handling: {unsafe!r}")
    # …and forbids them explicitly.
    assert "never use --body" in section
    assert "clipboard" in section, "the clipboard must be named and forbidden"


def test_a_secure_backup_is_required_before_the_merge():
    section = _rotation_section()
    assert "back the new seed up securely" in section or "backup" in section
    assert "offline backup" in section
    assert "before" in section and "merge" in section


def test_metadata_verification_is_described_honestly():
    """Metadata proves a NAME exists in a SCOPE. It cannot prove the stored
    value derives the pinned key."""
    section = _rotation_section()
    assert "cannot show the stored value" in section or \
           "cannot prove" in section, (
        "the doc must state metadata cannot prove the value")
    assert "only cryptographic proof" in section or \
           "cryptographic proof" in section, (
        "the sign job must be named as the actual proof")


# --- current-state wording -----------------------------------------------------

def test_releasing_says_the_seed_must_move_and_has_not():
    lowered = _RELEASING.read_text(encoding="utf-8").lower()
    assert "must be moved" in lowered or "must move" in lowered
    assert "not there yet" in lowered or "still repository-scoped" in lowered, (
        "the doc must not imply the seed already lives in release-signing")
    assert "zero" in lowered or "0 secrets" in lowered, (
        "the doc should record that release-signing holds no secrets yet")


def test_no_release_doc_claims_the_seed_already_lives_in_the_environment():
    for relpath in ("RELEASING.md", "docs/SIGNING.md",
                    "olympus/witness_pubkey.txt"):
        flowed = _flow(_ROOT / relpath)
        for claim in ("is a secret of the protected release-signing",
                      "sets olympus_signing_seed in the sign job from a "
                      "secret of the protected"):
            assert claim not in flowed, (
                f"{relpath} claims the seed is already relocated: {claim!r}")


def test_runtime_is_described_as_a_separate_trust_domain():
    flowed = _flow(_SIGNING)
    assert "separate deployment trust domain" in flowed
    assert "may use an instance-specific key" in flowed, (
        "runtime signing MAY use its own key — it is not always a different "
        "key from the release key")


def test_signing_doc_scopes_only_its_overlap_section_to_runtime():
    """v9 said "everything below describes runtime signing", which is false —
    seed custody applies to both domains."""
    flowed = _flow(_SIGNING)
    assert "everything below describes" not in flowed
    assert "that section, and only that section" in flowed, (
        "the banner must scope the split to the overlap-rotation section")


# --- the ledger does not restate derivable state -------------------------------

def test_the_ledger_does_not_hand_maintain_the_active_key():
    """AUDIT (v11): v10 restated the active key in a fenced block and claimed
    "No release key has ever been rotated." Both go stale the moment a
    rotation lands, and neither is checked by anything."""
    active = _anchor_keys()[0]
    event_lines = {e["line"] for e in _ledger_events()}
    lines = _LEDGER.read_text(encoding="utf-8").splitlines()
    stray = [i for i, ln in enumerate(lines, start=1)
             if active in ln and i not in event_lines]
    assert not stray, (
        f"the active key is restated outside the event table at line(s) "
        f"{stray}; current state must be derived by replaying the table, "
        f"not copied")


def test_the_ledger_makes_no_rotation_count_claim():
    flowed = _flow(_LEDGER)
    for stale in ("no release key has ever been rotated",
                  "there is exactly one activation event and no retirement",
                  "replaying the events above leaves one active release key"):
        assert stale not in flowed, (
            f"the ledger hand-maintains a claim that will go stale: {stale!r}")


def test_the_ledger_says_current_state_is_derived_and_enforced():
    flowed = _flow(_LEDGER)
    assert "derived" in flowed
    assert "replay" in flowed
    assert "witness_pubkey.txt" in flowed
    assert "test_release_signing_keys.py" in flowed, (
        "the ledger should name the test that enforces the agreement")


def test_the_ledger_scopes_runtime_as_a_separate_trust_domain():
    """v10 said runtime signing "is a different key with a different policy".
    Whether it is a different key depends on deployment configuration."""
    flowed = _flow(_LEDGER)
    assert "separate deployment trust domain" in flowed
    assert "may use an instance-specific key" in flowed
    for overclaim in ("is a different key with a different policy",
                      "always a different key"):
        assert overclaim not in flowed, (
            f"the ledger overclaims the runtime domain: {overclaim!r}")


def test_the_ledger_documents_the_strict_table_boundary():
    flowed = _flow(_LEDGER)
    assert "exactly one" in flowed and "event table" in flowed
    assert "contiguous" in flowed
    assert "real calendar date" in flowed
    for phrase in ("ends at the first line that is not an event row",
                   "immediately after the last existing event row"):
        assert phrase in flowed, f"the parse contract omits: {phrase!r}"


# --- secret handling is ordered install → verify → delete → verify -------------

def test_the_environment_secret_is_verified_before_the_repo_secret_is_deleted():
    """AUDIT (v11): v10 deleted repository scope first and verified after. If
    the install had silently failed, that order leaves NO signing material."""
    section = _rotation_section()
    install = section.index("gh secret set olympus_signing_seed")
    confirm = section.index("confirm the new environment secret")
    delete = section.index("now delete the repository-scoped secret")
    assert install < confirm < delete, (
        "order must be install → confirm environment → delete repository "
        f"(got install={install}, confirm={confirm}, delete={delete})")


def test_the_pre_delete_check_blocks_progress():
    section = _rotation_section()
    assert "do not proceed until" in section, (
        "the pre-delete verification must be a gate, not a suggestion")


def test_the_final_verification_covers_all_three_scopes():
    section = _rotation_section()
    tail = section[section.index("verify the final placement"):]
    assert "404" in tail, "repository scope must be checked for 404"
    assert "environments/release-signing/secrets" in tail, (
        "environment scope must still show the name")
    assert "environments/pypi/secrets" in tail, (
        "pypi must be confirmed empty")


def test_metadata_limits_are_restated_at_the_final_check():
    section = _rotation_section()
    tail = section[section.index("verify the final placement"):]
    assert "cannot show the stored value" in tail
    assert "wrong bytes" in tail or "cannot prove" in tail, (
        "the doc must say a right-named secret with wrong contents passes")


# --- 6. runtime multi-key support stays documented, separately -----------------

def test_runtime_multi_key_overlap_is_still_documented():
    flowed = _flow(_SIGNING)
    assert "olympus_pinned_pubkey" in flowed
    assert "overlap window" in flowed, (
        "runtime rotation by overlap must remain documented")


def test_signing_doc_scopes_its_overlap_to_runtime_and_points_elsewhere():
    flowed = _flow(_SIGNING)
    assert "runtime" in flowed and "instance" in flowed
    assert "releasing.md" in flowed, (
        "docs/SIGNING.md must point release-key rotation at RELEASING.md")
    assert "release_signing_keys.md" in flowed, (
        "docs/SIGNING.md must point at the release-key ledger")
    assert "does not apply to the release key" in flowed or \
           "not apply to the release key" in flowed, (
        "the overlap section must disclaim the release key")


def test_the_two_domains_are_not_conflated_in_the_anchor():
    flowed = _flow(_ANCHOR)
    assert "runtime" in flowed, (
        "the anchor should name where multi-key overlap does belong")
    assert "do not add a second line" in flowed


def test_witness_module_still_supports_multiple_runtime_pins():
    """The code capability must survive the documentation split."""
    import sys
    sys.path.insert(0, str(_ROOT))
    from olympus import witness
    source = (_ROOT / "olympus" / "witness.py").read_text(encoding="utf-8")
    assert "def pinned_pubkeys" in source
    assert callable(witness.pinned_pubkeys)
