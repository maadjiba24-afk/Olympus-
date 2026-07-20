"""Gallery — surface images generated into the workspace.

`generate_image` (media.py) writes PNGs into the confined sandbox workspace.
This lists them and serves their bytes (path-confined, image types only) so the
web UI can show a gallery and the CLI can list what's been made. The workspace
is a single confined directory (the operator surface), so the gallery reflects
that workspace — it is not per-user.
"""

from __future__ import annotations

import time

_IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
_MAX_SERVE_BYTES = 16 * 1024 * 1024        # never stream something absurd


def list_images() -> list[dict]:
    """Image files in the workspace, newest first: [{name, bytes, updated}].

    Union across the shared workspace + any per-worker roots (M1), so an image a
    specialist generated into its own root still shows in the gallery. With no
    per-worker roots this is exactly the single shared workspace."""
    from . import sandbox
    out, seen = [], set()
    for d in sandbox._read_roots():
        for p in sorted(d.glob("*")):
            if (p.is_file() and p.suffix.lower() in _IMAGE_EXTS
                    and p.name not in seen):
                try:
                    st = p.stat()
                except OSError:
                    continue
                seen.add(p.name)
                out.append({"name": p.name, "bytes": st.st_size,
                            "updated": st.st_mtime})
    out.sort(key=lambda d: d["updated"], reverse=True)
    return out


def read_image(name: str):
    """Return (bytes, content_type) for a workspace image, or None if it isn't a
    confined, readable image. Path-confined via the sandbox root — a traversal
    or a non-image is refused."""
    from . import sandbox
    target = sandbox._resolve_read(name)     # union across worker + shared roots
    if target is None:
        return None
    ext = target.suffix.lower()
    if not target.is_file() or ext not in _IMAGE_EXTS:
        return None
    try:
        if target.stat().st_size > _MAX_SERVE_BYTES:
            return None
        return target.read_bytes(), _IMAGE_EXTS[ext]
    except OSError:
        return None


def delete_image(name: str) -> bool:
    from . import sandbox
    target = sandbox._resolve_read(name)     # union across worker + shared roots
    if target is None:
        return False
    if not target.is_file() or target.suffix.lower() not in _IMAGE_EXTS:
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


def render_list() -> str:
    imgs = list_images()
    if not imgs:
        return "No images in the workspace yet. Ask for an image and it'll " \
               "appear here."
    lines = []
    for im in imgs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(im["updated"]))
        kb = im["bytes"] // 1024
        lines.append(f"- {im['name']}  ({kb}KB, {when})")
    return "\n".join(lines)
