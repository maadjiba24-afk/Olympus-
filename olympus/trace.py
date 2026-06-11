"""Per-run observability — a JSONL trace of every pipeline run.

Each run gets a trace id; stages (route, plan, specialist, verify, review,
synthesize) append timed events. Failures and slow stages become visible
instead of vanishing into a multi-minute black box.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager

from . import config


class Trace:
    def __init__(self, kind: str, user: str = "shared"):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.user = user
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._t0 = time.time()

    def event(self, stage: str, **fields) -> None:
        with self._lock:
            self.events.append({
                "t": round(time.time() - self._t0, 3),
                "stage": stage,
                **fields,
            })

    @contextmanager
    def span(self, stage: str, **fields):
        start = time.time()
        self.event(stage + ".start", **fields)
        try:
            yield
        except Exception as err:
            self.event(stage + ".error", error=str(err)[:300])
            raise
        finally:
            self.event(stage + ".end", secs=round(time.time() - start, 2))

    def flush(self) -> None:
        path = config.MEMORY_DIR / "traces" / f"{time.strftime('%Y%m%d')}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": self.id, "kind": self.kind, "user": self.user,
            "total_secs": round(time.time() - self._t0, 2),
            "events": self.events,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
