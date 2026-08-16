"""Gallery — surface images generated into the workspace, PER PRINCIPAL.

`generate_image` / `edit_image` (media.py) write PNGs into the confined sandbox
workspace. This lists them and serves their bytes (path-confined, image types
only) for the web UI and CLI.

SCOPED PER PRINCIPAL, WITH ITS OWN RESOLVER (W2-1b). Every function here used to
call `sandbox._read_roots()`, which deliberately UNIONS the current worker root,
the shared workspace and every sibling worker root. That union is correct for
what it was built for — specialists collaborating on one user's task — and it is
exactly the cross-tenant bug once images belong to different people: user B
could list, read and delete user A's images.

So this module does NOT use `_read_roots()`, and must not start: `_owned` below
resolves inside ONE principal's directory and never unions. A test asserts the
absence, because reintroducing the union would look like a harmless refactor.

Ownership is decided at WRITE time from `memory.current_user()` — which the
orchestrator threads through worker threads — not inferred at read time from
whoever happens to be looking.

WHAT THIS DOES AND DOES NOT SCOPE. It scopes the GALLERY SURFACE: the web
endpoints and the CLI listing. It does not re-root the sandbox file tools, which
still share one workspace by deliberate design (see `sandbox.workdir`, and ADR
0005 on why per-worker re-rooting was rejected). A principal who can run file
tools can still reach another principal's gallery directory through those tools.
Closing that is a workspace-model change, not a gallery change.
"""

from __future__ import annotations

import time
from pathlib import Path

_IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
_MAX_SERVE_BYTES = 16 * 1024 * 1024        # never stream something absurd

#: Per-principal image directories live under `workdir()/<_GALLERY_DIR>/<owner>`.
_GALLERY_DIR = "gallery"


def _safe_owner(user: str | None) -> str:
    from . import memory
    return memory.safe_id(user or memory.current_user())


def owner_root(user: str | None = None, *, create: bool = False) -> Path:
    """The one directory this principal's images live in. Never unioned."""
    from . import sandbox
    root = sandbox.workdir() / _GALLERY_DIR / _safe_owner(user)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _legacy_root() -> Path:
    """Where images written before per-principal scoping still live: flat in
    the shared workspace, belonging to nobody."""
    from . import sandbox
    return sandbox.workdir()


def _legacy_visible_to(user: str | None) -> bool:
    """Whether this principal may see the pre-upgrade flat images.

    MIGRATION DECISION (W2-1b). Images written before this change sit flat in
    the shared workspace and have no owner. Assigning them to an arbitrary
    account would be wrong on a multi-user instance, and hiding them from
    everyone would silently empty a single-user install's gallery on upgrade —
    the case that actually bites a real person.

    So: when accounts are OFF (`OLYMPUS_REQUIRE_LOGIN` unset) there is exactly
    one human using the instance, and the legacy images are shown to them —
    nothing is lost by upgrading. When accounts are ON they belong to nobody,
    are shown to NO ONE through this surface, and the operator claims them
    deliberately with `claim_legacy()`. See deploy/README.md.
    """
    from . import accounts
    return not accounts.require_login()


def _images_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [p for p in sorted(directory.glob("*"))
            if p.is_file() and p.suffix.lower() in _IMAGE_EXTS]


def _owned(user: str | None, name: str) -> Path | None:
    """Resolve `name` INSIDE one principal's gallery. Never unions, and refuses
    anything that escapes the owner's directory (traversal, absolute path)."""
    raw = str(name or "").strip()
    if not raw or raw in (".", ".."):
        return None
    root = owner_root(user).resolve()
    try:
        target = (root / raw).resolve()
        target.relative_to(root)          # confinement: must stay under root
    except (ValueError, OSError):
        return None
    if not target.is_file() or target.suffix.lower() not in _IMAGE_EXTS:
        return None
    return target


def _legacy(user: str | None, name: str) -> Path | None:
    """Resolve a legacy flat image, only for a principal permitted to see them
    and only at the top level of the shared workspace (never a subdirectory,
    which is where another principal's gallery lives)."""
    if not _legacy_visible_to(user):
        return None
    raw = str(name or "").strip()
    if not raw or "/" in raw or "\\" in raw or raw in (".", ".."):
        return None
    root = _legacy_root().resolve()
    try:
        target = (root / raw).resolve()
        if target.parent != root:         # top level only
            return None
    except OSError:
        return None
    if not target.is_file() or target.suffix.lower() not in _IMAGE_EXTS:
        return None
    return target


def list_images(user: str | None = None) -> list[dict]:
    """This principal's images, newest first: [{name, bytes, updated}]."""
    out, seen = [], set()
    directories = [owner_root(user)]
    if _legacy_visible_to(user):
        directories.append(_legacy_root())
    for d in directories:
        for p in _images_in(d):
            if p.name in seen:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            seen.add(p.name)
            out.append({"name": p.name, "bytes": st.st_size,
                        "updated": st.st_mtime})
    out.sort(key=lambda d: d["updated"], reverse=True)
    return out


def read_image(name: str, user: str | None = None):
    """(bytes, content_type) for one of THIS principal's images, else None."""
    target = _owned(user, name) or _legacy(user, name)
    if target is None:
        return None
    try:
        if target.stat().st_size > _MAX_SERVE_BYTES:
            return None
        return target.read_bytes(), _IMAGE_EXTS[target.suffix.lower()]
    except OSError:
        return None


def delete_image(name: str, user: str | None = None) -> bool:
    target = _owned(user, name) or _legacy(user, name)
    if target is None:
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


def claim_legacy(user: str | None = None) -> int:
    """Move the pre-upgrade flat images into this principal's gallery.

    The documented migration path for a multi-user instance: the operator runs
    it once to take ownership of images that predate scoping. Returns how many
    moved. Never overwrites an existing owned image of the same name."""
    root = owner_root(user, create=True)
    moved = 0
    for p in _images_in(_legacy_root()):
        dest = root / p.name
        if dest.exists():
            continue
        try:
            p.replace(dest)
            moved += 1
        except OSError:
            continue
    return moved


def render_list(user: str | None = None) -> str:
    imgs = list_images(user)
    if not imgs:
        return "No images in the workspace yet. Ask for an image and it'll " \
               "appear here."
    lines = []
    for im in imgs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(im["updated"]))
        kb = im["bytes"] // 1024
        lines.append(f"- {im['name']}  ({kb}KB, {when})")
    return "\n".join(lines)
