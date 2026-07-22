"""Progressive Web App layer — make the Olympus web UI installable.

Turns `olympus web` into an app you can add to a phone's home screen: a web
app manifest, a service worker for an offline shell, and app icons. No native
code, no app store, no new dependencies — the icons are rendered by a tiny
pure-Python PNG encoder (stdlib zlib only), and everything is served by the
existing web server.

The web handler calls `inject(html)` to add the manifest link, theme-color,
iOS meta tags, and the service-worker registration into the page it already
serves, and serves `manifest()`, `service_worker()`, and `icon(size)` at
their routes.
"""

from __future__ import annotations

import json
import struct
import zlib

APP_NAME = "Olympus"
THEME = "#0e0e12"        # dark chrome
ACCENT = (217, 180, 74)  # brand gold #d9b44a
BG = (14, 14, 18)        # #0e0e12

# --- manifest ---------------------------------------------------------------

def manifest() -> str:
    return json.dumps({
        "name": "Olympus — your AI council",
        "short_name": APP_NAME,
        "description": "A verified council of AI specialists, in your pocket.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": THEME,
        "theme_color": THEME,
        "icons": [
            {"src": "/icons/olympus-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/icons/olympus-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": "/icons/olympus-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }, separators=(",", ":"))


# --- service worker ---------------------------------------------------------

SW_VERSION = "olympus-pwa-v1"

def service_worker() -> str:
    """Network-first for navigations (so a live server always wins), falling
    back to a cached shell offline. Static assets (icons/manifest) are
    cache-first. Kept deliberately small and safe."""
    return (
        "const C='" + SW_VERSION + "';\n"
        "const SHELL=['/','/manifest.webmanifest','/icons/olympus-192.png'];\n"
        "self.addEventListener('install',e=>{e.waitUntil("
        "caches.open(C).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()));});\n"
        "self.addEventListener('activate',e=>{e.waitUntil("
        "caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C)"
        ".map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});\n"
        "self.addEventListener('fetch',e=>{const r=e.request;"
        "if(r.method!=='GET')return;const u=new URL(r.url);"
        "if(u.origin!==location.origin)return;"
        "if(r.mode==='navigate'){e.respondWith("
        "fetch(r).then(res=>{const cp=res.clone();"
        "caches.open(C).then(c=>c.put('/',cp));return res;})"
        ".catch(()=>caches.match('/')));return;}"
        "if(u.pathname.startsWith('/icons/')||u.pathname==='/manifest.webmanifest'){"
        "e.respondWith(caches.match(r).then(m=>m||fetch(r).then(res=>{"
        "const cp=res.clone();caches.open(C).then(c=>c.put(r,cp));return res;})));}"
        "});\n"
    )


# --- HTML injection ---------------------------------------------------------

def head_tags() -> str:
    return (
        '<link rel="manifest" href="/manifest.webmanifest">'
        f'<meta name="theme-color" content="{THEME}">'
        '<meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        f'<meta name="apple-mobile-web-app-title" content="{APP_NAME}">'
        '<link rel="apple-touch-icon" href="/icons/olympus-192.png">'
    )


_SW_REGISTER = (
    "<script>if('serviceWorker' in navigator){window.addEventListener('load',"
    "function(){navigator.serviceWorker.register('/sw.js').catch(function(){});"
    "});}</script>"
)


def inject(html: str) -> str:
    """Insert the PWA head tags and the SW registration into a served page.
    Idempotent-ish: only injects when the manifest link isn't already present."""
    if 'rel="manifest"' in html:
        return html
    if "</head>" in html:
        html = html.replace("</head>", head_tags() + "</head>", 1)
    else:
        html = head_tags() + html
    if "</body>" in html:
        html = html.replace("</body>", _SW_REGISTER + "</body>", 1)
    else:
        html = html + _SW_REGISTER
    return html


# --- icons: a tiny pure-Python PNG encoder + a bolt glyph -------------------

def _png(size: int, pixels: "list[tuple[int,int,int,int]]") -> bytes:
    """Encode `size`×`size` RGBA pixels (row-major) as a PNG. stdlib only."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    raw = bytearray()
    for y in range(size):
        raw.append(0)                                  # filter type 0
        for x in range(size):
            r, g, b, a = pixels[y * size + x]
            raw += bytes((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


# A lightning bolt as a normalized polygon in a 0..1 square.
_BOLT = [(0.55, 0.06), (0.30, 0.56), (0.46, 0.56), (0.38, 0.94),
         (0.72, 0.42), (0.54, 0.42), (0.66, 0.06)]


def _in_poly(px: float, py: float, poly) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and \
                (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


_icon_cache: dict[tuple[int, bool], bytes] = {}


def icon(size: int, maskable: bool = False) -> bytes:
    """A branded app icon: gold lightning bolt on the dark chrome color.
    `maskable` insets the glyph into the safe zone for adaptive masks."""
    key = (size, maskable)
    if key in _icon_cache:
        return _icon_cache[key]
    inset = 0.18 if maskable else 0.0        # maskable safe zone
    scale = 1.0 - 2 * inset
    px = []
    for y in range(size):
        for x in range(size):
            nx = (x / size - inset) / scale
            ny = (y / size - inset) / scale
            if 0 <= nx <= 1 and 0 <= ny <= 1 and _in_poly(nx, ny, _BOLT):
                px.append((*ACCENT, 255))
            else:
                px.append((*BG, 255))
    data = _png(size, px)
    _icon_cache[key] = data
    return data
