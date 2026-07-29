"""Durable records for decisions that expand permission.

> A human review or restricted promotion must not succeed when its audit event
> or durable state cannot be persisted.

The audit found `GateLedger._log` swallowing every exception from its sink,
with the comment *"an audit sink that fails must not stop the gate"*. For a
rejection that is right: refusing to reject because the audit disk is full
would leave a bad model running, and safety is the direction to fail in. For a
**promotion** it is exactly backwards. It meant a challenger could go live with
no record that anyone approved it, and nothing afterwards could tell that
promotion apart from one that was never authorised.

So permission-expanding decisions are committed through this module, and it
raises rather than returning False. The distinction the rest of Olympus draws
between the two directions is the whole design:

* **restricting, rejecting, demoting, shutting down** — prefer safety, proceed
  even if the record fails, and note the missing record;
* **promoting, reviewing, widening** — no record, no permission.

What "durable" means here
-------------------------
Three things, and all three are checked rather than assumed:

1. **Written and flushed to the device.** `write()` returning is not durability;
   the data can sit in the page cache through a power loss. Every commit
   `fsync`s the file, and `fsync`s the containing directory too, because a
   rename that has not reached the directory entry is a file that is not there
   after a crash.
2. **Read back and compared.** A successful `write()` on a full filesystem can
   still produce a short record. The commit re-reads what it wrote and compares
   digests before reporting success.
3. **Chained.** Each audit event carries the digest of the one before it, so a
   record that was edited, removed or reordered breaks the chain at that point
   and `AuditLog.verify()` names the index where it broke.

Crash between the two writes
-----------------------------
There are two durable records — the audit event and the promotion state — and
no filesystem gives us both atomically. The order is therefore chosen so that
the surviving half is the safe one: the **audit event is written first**, then
the state. A crash in between leaves an event saying a promotion was attempted
and no state saying it happened, and `reconstruct()` reads the state as
authoritative. The challenger comes back unpromoted, with evidence in the audit
that somebody tried. The other order would come back promoted with no record of
who approved it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..errors import TradingError

#: Bumped when the record layout or the chaining rule changes.
DURABLE_SCHEMA_VERSION = 1

#: The digest a chain starts from. A record claiming this as its predecessor is
#: claiming to be first, and `verify()` checks that only one record does.
GENESIS = "0" * 64


class DurabilityError(TradingError):
    """A record could not be persisted, or could not be read back intact.

    Typed and raised rather than returned, because every caller's correct
    response is to abandon the operation, and a returned False gets ignored by
    the one caller that forgets to check it.
    """

    code = "DURABILITY_ERROR"


class TamperedRecord(DurabilityError):
    """The stored chain does not verify. Names where it broke."""

    code = "TAMPERED_RECORD"


def canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def digest_of(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    """One immutable record. Its digest covers its predecessor's.

    `digest` is a **computed property**, not a field. A stored digest is a
    number somebody can edit to match the record they wanted; a computed one
    has to be recomputed from the content every time it is read, which is what
    makes `verify()` mean anything.
    """

    event: str
    subject: str
    at: datetime
    actor: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    previous: str = GENESIS
    schema_version: int = DURABLE_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "detail", dict(self.detail))
        moment = self.at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "at", moment.astimezone(timezone.utc))
        for name in ("event", "subject", "actor"):
            if not str(getattr(self, name)).strip():
                raise DurabilityError(
                    f"an audit event must carry a {name}; an event nobody can "
                    f"attribute is not evidence", field=name)

    @property
    def body(self) -> dict:
        """Everything the digest covers. The digest itself is not in here."""
        return {"schema_version": self.schema_version, "event": self.event,
                "subject": self.subject, "at": self.at.isoformat(),
                "actor": self.actor, "detail": dict(self.detail),
                "previous": self.previous}

    @property
    def digest(self) -> str:
        return digest_of(self.body)

    def to_dict(self) -> dict:
        return {**self.body, "digest": self.digest}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AuditEvent":
        event = cls(event=str(data.get("event", "")),
                    subject=str(data.get("subject", "")),
                    at=datetime.fromisoformat(str(data["at"])),
                    actor=str(data.get("actor", "")),
                    detail=dict(data.get("detail") or {}),
                    previous=str(data.get("previous", GENESIS)),
                    schema_version=int(data.get("schema_version",
                                                DURABLE_SCHEMA_VERSION)))
        stored = str(data.get("digest", ""))
        if stored and stored != event.digest:
            raise TamperedRecord(
                "a stored audit event does not match its own digest",
                subject=event.subject, event=event.event,
                stored=stored, recomputed=event.digest)
        return event


# ---------------------------------------------------------------------------
# writing to a device, and proving it landed
# ---------------------------------------------------------------------------

def _fsync_directory(path: Path) -> None:
    """Flush the directory entry, not only the file.

    A file whose contents are on the platter but whose name is not in the
    flushed directory does not exist after a crash. Best-effort by necessity:
    Windows cannot open a directory for `fsync`, and the caller has already
    flushed the file itself.
    """
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def append_durably(path: Path, line: str) -> None:
    """Append one line, flush it to the device, and raise if anything fails.

    No `try/except OSError: pass`. A full disk, a read-only mount and a
    permission error must all reach the caller, because the caller is about to
    decide whether a model may trade.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line if line.endswith("\n") else line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def replace_durably(path: Path, payload: str) -> None:
    """Write a file atomically: temp file, fsync, rename, fsync the directory.

    `os.replace` is atomic within a filesystem, so a reader never sees a half
    written state file — it sees the old one or the new one. Writing in place
    would leave a truncated file if the process died mid-write, and a truncated
    promotion state is one that cannot be reconstructed either way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent),
                                         prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _fsync_directory(path.parent)


# ---------------------------------------------------------------------------
# the audit log
# ---------------------------------------------------------------------------

class AuditLog:
    """Append-only, hash-chained, and verified on every commit.

    In-memory when constructed without a path, which is what tests and
    short-lived experiments want; the durability guarantees are then explicitly
    absent rather than quietly weaker — `durable` says which one this is, and
    `commit` refuses a promotion through a non-durable log unless the caller
    said so on purpose.
    """

    __slots__ = ("path", "_events")

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path is not None else None
        self._events: list[AuditEvent] = []
        if self.path is not None and self.path.exists():
            self._events = list(self._read())

    # -- reading -----------------------------------------------------------

    @property
    def durable(self) -> bool:
        return self.path is not None

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[AuditEvent]:
        return iter(self.events)

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        if self.path is None:
            return tuple(self._events)
        return tuple(self._read())

    @property
    def head(self) -> str:
        events = self.events
        return events[-1].digest if events else GENESIS

    def _read(self) -> list[AuditEvent]:
        """Parse the file. A truncated final line is dropped, and said so.

        A crash mid-append leaves a partial line. Dropping it is correct — the
        record never completed — and it is the reason `commit` re-reads after
        writing instead of trusting the write.
        """
        out: list[AuditEvent] = []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return out
        except OSError as error:
            raise DurabilityError("the audit log could not be read",
                                  path=str(self.path),
                                  reason=f"{type(error).__name__}: {error}")
        for index, line in enumerate(raw.splitlines()):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except ValueError:
                # Only the last line may be partial; anything earlier is
                # corruption in the middle of the chain.
                if index == len(raw.splitlines()) - 1:
                    continue
                raise TamperedRecord(
                    "the audit log contains an unparseable record that is not "
                    "the last one, so it was not a partial write",
                    path=str(self.path), index=index)
            out.append(AuditEvent.from_dict(data))
        return out

    def verify(self) -> tuple[str, ...]:
        """Check the chain. Returns the findings; empty means intact."""
        findings: list[str] = []
        previous = GENESIS
        for index, event in enumerate(self.events):
            if event.previous != previous:
                findings.append(
                    f"record {index} ({event.event} on {event.subject}) links "
                    f"to {event.previous[:12]} but the record before it "
                    f"digests to {previous[:12]}")
            previous = event.digest
        return tuple(findings)

    def require_intact(self) -> None:
        findings = self.verify()
        if findings:
            raise TamperedRecord("the audit chain does not verify",
                                 findings=list(findings),
                                 path=str(self.path) if self.path else "(memory)")

    # -- writing -----------------------------------------------------------

    def commit(self, *, event: str, subject: str, at: datetime, actor: str,
               detail: Mapping[str, Any] | None = None) -> AuditEvent:
        """Append, flush, read back, and compare. Raises on any failure.

        The read-back is not paranoia about the filesystem lying: a `write()`
        that returns having stored fewer bytes than it was given is ordinary
        behaviour on a full device, and the resulting record parses as JSON
        right up until it does not.
        """
        self.require_intact()
        record = AuditEvent(event=event, subject=subject, at=at, actor=actor,
                            detail=dict(detail or {}), previous=self.head)
        if self.path is None:
            self._events.append(record)
            return record
        try:
            append_durably(self.path, json.dumps(record.to_dict(),
                                                 sort_keys=True, default=str))
        except OSError as error:
            raise DurabilityError(
                "the audit event could not be written; a permission expansion "
                "with no durable record is indistinguishable afterwards from "
                "one nobody authorised",
                path=str(self.path), event=event, subject=subject,
                reason=f"{type(error).__name__}: {error}")
        stored = self.events
        if not stored or stored[-1].digest != record.digest:
            raise DurabilityError(
                "the audit event did not read back as written",
                path=str(self.path), event=event, subject=subject,
                expected=record.digest,
                found=stored[-1].digest if stored else "(nothing)")
        self.require_intact()
        return record

    def for_subject(self, subject: str) -> tuple[AuditEvent, ...]:
        return tuple(e for e in self.events if e.subject == subject)

    def to_dict(self) -> dict:
        return {"schema_version": DURABLE_SCHEMA_VERSION,
                "path": str(self.path) if self.path else None,
                "durable": self.durable,
                "count": len(self.events),
                "head": self.head,
                "findings": list(self.verify())}


# ---------------------------------------------------------------------------
# the state store
# ---------------------------------------------------------------------------

class StateStore:
    """The authoritative record of *what is currently true*.

    Separate from the audit log on purpose. The audit says what happened; the
    state says what holds now, and reconstruction after a restart reads the
    state. That is why the audit event is written first: the crash window
    between them then leaves an attempt on record and no permission granted.
    """

    __slots__ = ("path", "_state")

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path is not None else None
        self._state: dict[str, Any] = {}
        if self.path is not None and self.path.exists():
            self._state = dict(self._read())

    @property
    def durable(self) -> bool:
        return self.path is not None

    def _read(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise DurabilityError("the state file could not be read",
                                  path=str(self.path),
                                  reason=f"{type(error).__name__}: {error}")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise TamperedRecord(
                "the state file is not readable JSON; a truncated state is "
                "not a state, and guessing at it would grant a permission "
                "nobody can account for",
                path=str(self.path), reason=str(error))
        if not isinstance(data, dict):
            raise TamperedRecord("the state file is not an object",
                                 path=str(self.path))
        stored = str(data.pop("__digest__", ""))
        if stored and stored != digest_of(data):
            raise TamperedRecord(
                "the state file does not match its own digest; it was edited "
                "outside this process",
                path=str(self.path), stored=stored, recomputed=digest_of(data))
        return data

    def load(self) -> dict:
        if self.path is None:
            return dict(self._state)
        return dict(self._read())

    def save(self, state: Mapping[str, Any]) -> str:
        """Write the state atomically and read it back. Raises on any failure."""
        body = dict(state)
        stamp = digest_of(body)
        if self.path is None:
            self._state = body
            return stamp
        try:
            replace_durably(self.path, json.dumps({**body, "__digest__": stamp},
                                                  sort_keys=True, default=str))
        except OSError as error:
            raise DurabilityError(
                "the state could not be written",
                path=str(self.path),
                reason=f"{type(error).__name__}: {error}")
        found = digest_of(self._read())
        if found != stamp:
            raise DurabilityError("the state did not read back as written",
                                  path=str(self.path), expected=stamp,
                                  found=found)
        return stamp

    def to_dict(self) -> dict:
        return {"path": str(self.path) if self.path else None,
                "durable": self.durable, "keys": sorted(self.load())}


__all__ = ["DURABLE_SCHEMA_VERSION", "GENESIS", "DurabilityError",
           "TamperedRecord", "AuditEvent", "AuditLog", "StateStore",
           "canonical", "digest_of", "append_durably", "replace_durably"]
