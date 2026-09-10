#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pwa.py — ThomysHomeAgent als Startbildschirm-App (PWA) auf dem Handy
====================================================================
Macht das Dashboard von lichtapp.py auf Android (Chrome) als App installierbar:
eigenes Icon auf dem Startbildschirm, Vollbild ohne Browserleiste, Start direkt
ins Dashboard. Es bleibt dieselbe Webseite vom Laptop — nur „verpackt".

Einbau in lichtapp.py (2 Stellen):

    import pwa
    # 1) im HTTP-Handler, vor den eigenen Routen (GET):
    hit = pwa.handle(path)                    # None, wenn nicht /manifest.webmanifest, /sw.js, /icon-*.png
    if hit is not None:
        status, content_type, body = hit
        ...Antwort mit diesem Content-Type senden...
    # 2) im HTML des Dashboards, innerhalb von <head>:
    pwa.head_tags()                           # Manifest-Link, Farben, Service-Worker-Registrierung

Wichtig (Android/Chrome): Installieren als App geht nur von einer „sicheren" Adresse.
Für das Heimnetz gibt es zwei Wege (siehe README, Abschnitt „Als App auf dem Handy"):
  a) Chrome-Flag chrome://flags/#unsafely-treat-insecure-origin-as-secure mit
     http://192.168.1.54:8099 — schnell, nur auf diesem Handy wirksam.
  b) HTTPS mit eigener CA (openssl) + CA-Zertifikat auf dem Handy installieren —
     sauber; dafür gibt es hier wrap_https().

Nur Python-Standardbibliothek; die Icons werden beim Import erzeugt (kein PIL nötig).
"""
import json
import math
import struct
import zlib

APP_NAME = "ThomysHomeAgent"
SHORT_NAME = "ThomysHome"
THEME_COLOR = "#111111"
BACKGROUND_COLOR = "#111111"
SW_VERSION = "1"          # bei Änderungen am Service Worker erhöhen → Handy lädt ihn neu

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def manifest(name=APP_NAME, short_name=SHORT_NAME, start_url="/"):
    return {
        "name": name,
        "short_name": short_name,
        "description": "Licht- und Zigbee-Steuerung für Thomys Zuhause",
        "start_url": start_url,
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "lang": "de",
        "background_color": BACKGROUND_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


# ---------------------------------------------------------------------------
# Service Worker: App-Hülle wird gecacht, /api/* geht IMMER direkt ans Netz
# ---------------------------------------------------------------------------

SERVICE_WORKER = """// ThomysHomeAgent Service Worker (Version %s)
const V = 'thomyshome-v%s';
const SHELL = ['/', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;   // Steuerung nie aus dem Cache
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(V).then(c => c.put(e.request, copy)); }
      return r;
    }).catch(() => caches.match(e.request).then(m => m || (e.request.mode === 'navigate' ? caches.match('/') : undefined)))
  );
});
""" % (SW_VERSION, SW_VERSION)


def head_tags():
    """HTML-Schnipsel für den <head> des Dashboards."""
    return (
        '<link rel="manifest" href="/manifest.webmanifest">\n'
        '<meta name="theme-color" content="%s">\n'
        '<meta name="mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-title" content="%s">\n'
        '<link rel="apple-touch-icon" href="/icon-192.png">\n'
        '<script>if ("serviceWorker" in navigator) { navigator.serviceWorker.register("/sw.js").catch(function () {}); }</script>\n'
    ) % (THEME_COLOR, SHORT_NAME)


# ---------------------------------------------------------------------------
# PNG-Icons ohne PIL
# ---------------------------------------------------------------------------

def _png(width, height, rows):
    """rows: Liste von bytes-Zeilen (RGBA, width*4 Bytes)."""
    def chunk(tag, data):
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _blend(dst, src, a):
    return tuple(int(round(d + (s - d) * a)) for d, s in zip(dst, src))


def _smooth(edge0, edge1, x):
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


def icon_png(size, maskable=False):
    """Dunkles Quadrat (abgerundet, bei maskable randlos) mit warm leuchtender Lampe."""
    bg = (27, 31, 42)
    glow = (255, 176, 32)
    lamp = (255, 214, 96)
    base = (154, 160, 170)
    aa = 1.0 / size
    radius = 0.0 if maskable else 0.22
    scale = 0.8 if maskable else 1.0          # maskable: Motiv in die sichere Zone (mittlere 80 %)
    rows = []
    for y in range(size):
        row = bytearray()
        py = (y + 0.5) / size
        for x in range(size):
            px = (x + 0.5) / size
            # Hintergrund mit abgerundeten Ecken
            if radius > 0:
                dx = max(abs(px - 0.5) - (0.5 - radius), 0.0)
                dy = max(abs(py - 0.5) - (0.5 - radius), 0.0)
                d = math.hypot(dx, dy) - radius
                alpha = 1.0 - _smooth(-aa, aa, d)
            else:
                alpha = 1.0
            col = bg
            # Motiv (in Icon-Koordinaten, ggf. verkleinert)
            mx = (px - 0.5) / scale + 0.5
            my = (py - 0.5) / scale + 0.5
            dl = math.hypot(mx - 0.5, my - 0.42)
            g = 1.0 - _smooth(0.20, 0.36, dl)          # weiches Leuchten
            col = _blend(col, glow, 0.55 * g)
            l = 1.0 - _smooth(0.19 - aa, 0.19 + aa, dl)  # Lampenkugel
            col = _blend(col, lamp, l)
            if 0.43 <= mx <= 0.57:                       # Sockel
                b = (1.0 - _smooth(0.64 - aa, 0.64 + aa, my)) if my < 0.64 else 1.0
                b = b * (1.0 - _smooth(0.74 - aa, 0.74 + aa, my))
                if my >= 0.64 - aa:
                    col = _blend(col, base, b if my >= 0.64 else 0.0)
            row += bytes(col) + bytes([int(round(255 * alpha))])
        rows.append(bytes(row))
    return _png(size, size, rows)


_ICON_SPECS = {"/icon-192.png": (192, False), "/icon-512.png": (512, False), "/icon-maskable-512.png": (512, True)}
_ICONS = {}                      # werden beim ersten Abruf erzeugt (ca. 1–3 s für 512 px) und dann gecacht


def _icon(path):
    if path not in _ICONS:
        size, maskable = _ICON_SPECS[path]
        _ICONS[path] = icon_png(size, maskable)
    return _ICONS[path]


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

def handle(path):
    """(status, content_type, body) für die PWA-Dateien, sonst None."""
    if path == "/manifest.webmanifest":
        return 200, "application/manifest+json; charset=utf-8", json.dumps(manifest(), ensure_ascii=False).encode("utf-8")
    if path == "/sw.js":
        return 200, "application/javascript; charset=utf-8", SERVICE_WORKER.encode("utf-8")
    if path in _ICON_SPECS:
        return 200, "image/png", _icon(path)
    return None


def wrap_https(httpd, certfile, keyfile):
    """HTTPServer nachträglich auf HTTPS umstellen (für Weg b, eigene CA)."""
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


if __name__ == "__main__":   # python3 pwa.py → Icons als Dateien ablegen und <head>-Schnipsel zeigen
    for p in _ICON_SPECS:
        data = _icon(p)
        with open(p.lstrip("/"), "wb") as f:
            f.write(data)
        print("geschrieben:", p.lstrip("/"), len(data), "Bytes")
    print("\n<head>-Schnipsel:\n" + head_tags())
