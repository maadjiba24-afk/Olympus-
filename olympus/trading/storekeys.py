"""Composite keys over `olympus.store`, which only accepts simple names.

`store._safe()` rewrites anything outside `[A-Za-z0-9_.-]` to an underscore and
truncates at 128 characters. That is correct for a filesystem-backed KV, and it
quietly breaks any caller that builds a structured key — `"outcome|abc:1"` and
`"outcome/abc/1"` land on the same file, and a prefix scan for `"outcome|"`
matches nothing at all because the stored name no longer contains the
separator. Several Phase 10 registries need structured keys (a knowledge id
plus a version, an arena plus a contender), so they go through here.

The encoding keeps both properties that matter:

* **Reversible.** The logical key is written *inside* the payload under
  `_key`, so listing and prefix-filtering read it back exactly rather than
  guessing from a lossy filename.
* **Readable on disk.** The physical name is a slug of the logical key plus a
  short digest of it. `knowledge-btc-halving-1-9f2c1a04b6d3` is still greppable
  in a memory directory, and the digest is what makes two logical keys that
  slugify identically stay distinct.

The digest is a collision guard, not a security measure: nothing here defends
against an attacker with write access to the store, and the audit ledger is
where tamper-evidence lives.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterator, Mapping, Sequence

#: Separator for logical key parts. Chosen because `store._safe` mangles it,
#: which guarantees it can never survive into a physical name and be confused
#: with a part boundary there.
SEPARATOR = "|"

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_SLUG = 96


def logical_key(parts: Sequence[Any]) -> str:
    """Join key parts. A part containing the separator is refused, not escaped.

    Escaping would make `("a|b",)` and `("a", "b")` distinguishable only by a
    convention every caller has to remember. Refusing makes the ambiguity
    impossible.
    """
    out = []
    for part in parts:
        text = str(part)
        if SEPARATOR in text:
            raise ValueError(
                f"key part {text!r} contains the reserved separator {SEPARATOR!r}")
        out.append(text)
    return SEPARATOR.join(out)


def physical_key(parts: Sequence[Any]) -> str:
    """Store-safe name: readable slug plus a digest of the full logical key."""
    logical = logical_key(parts)
    slug = _SLUG_RE.sub("-", logical).strip("-")[:_MAX_SLUG]
    digest = hashlib.sha256(logical.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}" if slug else digest


class KeyedStore:
    """A namespace of JSON documents addressed by tuple keys.

    Deliberately thin: no caching, no write-back, no lazy handles. Every read
    hits the backing store, so two processes sharing a namespace see each
    other's writes without a coherence protocol — matching how the rest of the
    trading domain uses `olympus.store`.
    """

    __slots__ = ("namespace",)

    def __init__(self, namespace: str):
        self.namespace = str(namespace)

    def _backend(self):
        from olympus import store
        return store.backend()

    def put(self, parts: Sequence[Any], payload: Mapping[str, Any]) -> dict:
        """Write a document, stamping its logical key into the body."""
        body = {**dict(payload), "_key": logical_key(parts)}
        self._backend().put(self.namespace, physical_key(parts),
                            json.dumps(body, sort_keys=True).encode("utf-8"))
        return body

    def get(self, parts: Sequence[Any]) -> dict | None:
        raw = self._backend().get(self.namespace, physical_key(parts))
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def exists(self, parts: Sequence[Any]) -> bool:
        return self.get(parts) is not None

    def delete(self, parts: Sequence[Any]) -> None:
        """Present for completeness; the Phase 10 registries do not call it.

        Their records are append-only by design — a knowledge base or an
        evolution ledger that could delete would not be auditable.
        """
        backend = self._backend()
        if hasattr(backend, "delete"):
            backend.delete(self.namespace, physical_key(parts))

    def items(self, prefix: Sequence[Any] = ()
              ) -> list[tuple[tuple[str, ...], dict]]:
        """Every document whose key starts with `prefix`, ordered by key.

        Ordering is by the *logical* key, so callers get a stable, meaningful
        sequence rather than one determined by how the slug happened to hash.
        """
        wanted = tuple(str(p) for p in prefix)
        out: list[tuple[tuple[str, ...], dict]] = []
        for name in self._backend().keys(self.namespace):
            raw = self._backend().get(self.namespace, name)
            if not raw:
                continue
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):     # pragma: no cover
                continue
            key = body.get("_key")
            if not isinstance(key, str):
                continue
            parts = tuple(key.split(SEPARATOR))
            if parts[:len(wanted)] != wanted:
                continue
            out.append((parts, body))
        return sorted(out, key=lambda item: item[0])

    def keys(self, prefix: Sequence[Any] = ()) -> list[tuple[str, ...]]:
        return [parts for parts, _ in self.items(prefix)]

    def values(self, prefix: Sequence[Any] = ()) -> list[dict]:
        return [body for _, body in self.items(prefix)]

    def __repr__(self) -> str:                          # pragma: no cover
        return f"KeyedStore(namespace={self.namespace!r})"


__all__ = ["SEPARATOR", "logical_key", "physical_key", "KeyedStore"]
