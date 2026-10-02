"""Bounded, fully decoded gallery images. No signature-only fallback.

Pillow is an optional ``media`` dependency. Its absence disables gallery image
admission before any paid provider request, rather than weakening validation.
"""
from __future__ import annotations

import hashlib
import io
import threading
import warnings

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_FRAME_PIXELS = 16_000_000
MAX_FRAMES = 32
MAX_TOTAL_PIXELS = 32_000_000
_FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"),
            "GIF": ("image/gif", ".gif"), "WEBP": ("image/webp", ".webp"),
            "BMP": ("image/bmp", ".bmp")}
_LOCK = threading.RLock()


def require_decoder():
    from .gallery_state import GalleryError
    try:
        import PIL
        from PIL import Image, ImageFile
        version = tuple(int(p) for p in PIL.__version__.split(".")[:2])
        if not (version >= (12, 3) and version < (13, 0)):
            raise ImportError("unsupported decoder version")
    except (ImportError, ValueError):
        raise GalleryError("unavailable", "Image validation requires Pillow>=12.3,<13 (install the media extra).", 503) from None
    # Never alter shared global decoder policy, or allow another subsystem to
    # silently enable truncated image admission for the gallery.
    if ImageFile.LOAD_TRUNCATED_IMAGES:
        raise GalleryError("unavailable", "Strict image decoding is unavailable: truncated-image loading is enabled.", 503)
    return Image


def _dimensions(size):
    from .gallery_state import GalleryError
    width, height = size
    if width <= 0 or height <= 0 or max(size) > MAX_DIMENSION or width * height > MAX_FRAME_PIXELS:
        raise GalleryError("oversized", "Image dimensions or decoded pixels exceed the gallery limit.", 413)
    return width * height


def _jpeg_envelope(data):
    """Walk marker/entropy boundaries, never decode pixels or metadata."""
    if not data.startswith(b'\xff\xd8'):
        return False
    offset, entropy, size = 2, False, len(data)
    while offset < size:
        if entropy:
            offset = data.find(b'\xff', offset)
            if offset < 0:
                return False
        elif data[offset] != 0xff:
            return False
        offset += 1
        # Marker fill bytes are legal both between segments and in scans.
        while offset < size and data[offset] == 0xff:
            offset += 1
        if offset >= size:
            return False
        marker = data[offset]
        offset += 1
        if entropy and (marker == 0 or 0xd0 <= marker <= 0xd7 or marker == 1):
            continue  # byte stuffing, restart markers, or TEM
        if marker == 0xd9:
            return offset == size
        if marker == 0 or marker == 0xd8 or 0xd0 <= marker <= 0xd7:
            return False
        if marker == 1:
            continue
        if offset + 2 > size:
            return False
        length = int.from_bytes(data[offset:offset + 2], 'big')
        if length < 2 or offset + length > size:
            return False
        offset += length
        # DNL may appear within a scan; SOS starts each sequential/progressive
        # scan. All other length-bearing markers end the prior scan.
        entropy = marker == 0xda or (entropy and marker == 0xdc)
    return False


def _gif_envelope(data):
    """Walk bounded GIF tables, extensions, and image subblocks to the trailer."""
    size = len(data)
    if size < 13 or data[:6] not in (b'GIF87a', b'GIF89a'):
        return False
    offset = 13
    if data[10] & 0x80:
        offset += 3 * (2 << (data[10] & 7))
    while offset < size:
        kind = data[offset]
        offset += 1
        if kind == 0x3b:
            return offset == size
        if kind == 0x21:
            if offset >= size:
                return False
            offset += 1  # extension label, followed by size-prefixed subblocks
        elif kind == 0x2c:
            if offset + 9 > size:
                return False
            packed = data[offset + 8]
            offset += 9
            if packed & 0x80:
                offset += 3 * (2 << (packed & 7))
            if offset >= size:
                return False
            offset += 1  # LZW minimum code size; Pillow validates the stream
        else:
            return False
        while True:
            if offset >= size:
                return False
            count = data[offset]
            offset += 1
            if count == 0:
                break
            offset += count
            if offset > size:
                return False
    return False


def _png_envelope(data):
    """Locate the first IEND by chunk boundaries; Pillow verifies chunk CRCs."""
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return False
    offset, size = 8, len(data)
    while offset + 12 <= size:
        count = int.from_bytes(data[offset:offset + 4], 'big')
        kind = data[offset + 4:offset + 8]
        offset += 12 + count
        if offset > size:
            return False
        if kind == b'IEND':
            return count == 0 and offset == size and data[-4:] == b'\xaeB`\x82'
    return False


def _complete_envelope(data, fmt):
    """Strict container completion in addition to the real decoder.

    Pillow legitimately tolerates missing GIF trailers, PNG IEND CRC bytes and
    BMP row padding. Gallery policy refuses these incomplete containers. These
    bounded boundary walkers do not decode pixels or replace full decoding.
    Require the first terminal marker at EOF or exact declared file lengths. This does not
    certify arbitrary metadata or provide a second independent format parser.
    """
    from .gallery_state import GalleryError
    complete = {
        "PNG": lambda: _png_envelope(data),
        "JPEG": lambda: _jpeg_envelope(data),
        "GIF": lambda: _gif_envelope(data),
        "WEBP": lambda: len(data) >= 12 and int.from_bytes(data[4:8], "little") + 8 == len(data),
        "BMP": lambda: len(data) >= 14 and int.from_bytes(data[2:6], "little") == len(data),
    }
    if fmt not in complete or not complete[fmt]():
        raise GalleryError("unsupported_type", "Incomplete image container or trailing content refused.", 415)


def validate_image(data: bytes) -> dict:
    from .gallery_state import GalleryError
    if type(data) is not bytes or not data:
        raise GalleryError("unsupported_type", "Image content must be nonempty bytes.", 415)
    if len(data) > MAX_IMAGE_BYTES:
        raise GalleryError("oversized", "Image exceeds the 16 MiB gallery limit.", 413)
    Image = require_decoder()
    try:
        with _LOCK, warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=list(_FORMATS)) as image:
                fmt = image.format
                _complete_envelope(data, fmt)
                width, height = image.size
                _dimensions(image.size)
                image.verify()
            total = frames = 0
            # Reopen after verify, then decode every frame. Do not ask n_frames:
            # some plugins compute it by scanning an attacker-controlled stream.
            with Image.open(io.BytesIO(data), formats=list(_FORMATS)) as image:
                while True:
                    if frames >= MAX_FRAMES:
                        raise GalleryError("oversized", "Image frame count exceeds the gallery limit.", 413)
                    total += _dimensions(image.size)
                    if total > MAX_TOTAL_PIXELS:
                        raise GalleryError("oversized", "Aggregate decoded image pixels exceed the gallery limit.", 413)
                    image.load()
                    frames += 1
                    try:
                        image.seek(frames)
                    except EOFError:
                        break
            mime, extension = _FORMATS[fmt]
            return {"mime": mime, "extension": extension, "width": width,
                    "height": height, "frames": frames, "sha256": hashlib.sha256(data).hexdigest()}
    except GalleryError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise GalleryError("oversized", "Image decompression bomb refused.", 413) from None
    except Exception:
        raise GalleryError("unsupported_type", "Malformed, truncated or unsupported image content.", 415) from None
