#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
thomyshome_proxy.py — Zigbee-Seite, /api/z2m/* und Handy-App VOR lichtapp.py, ohne lichtapp.py zu ändern
=========================================================================================================
Ein kleiner Vorschalt-Server. Er beantwortet selbst:

    /zigbee                    Zigbee-Oberfläche (z2m_page.py)
    /api/z2m/*                 Zigbee2MQTT-API (z2m_api.py)
    /manifest.webmanifest, /sw.js, /icon-*.png   Handy-App (pwa.py)

und reicht ALLES andere unverändert an das bestehende Dashboard weiter (lichtapp.py auf
http://127.0.0.1:8099): Grundriss, Szenen, /api/ask, /api/state … Bei HTML-Antworten des
Dashboards fügt er die App-Kopfzeilen (installierbar auf dem Handy) und einen kleinen
„Zigbee“-Knopf unten rechts ein.

    cd ~/lichtagent
    python3 thomyshome_proxy.py                       # Port 8098 → weiter an http://127.0.0.1:8099
    python3 thomyshome_proxy.py --port 8098 --upstream http://127.0.0.1:8099 --config config.json
    python3 thomyshome_proxy.py --cert server.crt --key server.key     # optional HTTPS (siehe README)

Als Dienst: thomyshome-proxy.service (siehe README). Nur Python-Standardbibliothek.
Die Sprachbefehle über /api/ask laufen weiter in lichtapp.py — dessen HomeBrain verbindet sich
mit der neuen homebrain.py selbst mit Zigbee2MQTT (z2m_autoconnect).
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from z2m import Zigbee2MQTT, load_config
from z2m_api import Z2MApi
import pwa

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}

ZIGBEE_BUTTON = (
    '<a href="/zigbee" id="thomyshome-zigbee" title="Zigbee-Geräte" style="position:fixed;right:14px;bottom:14px;z-index:9999;'
    'background:#1f232b;color:#e8eaf0;border:1px solid #2c313b;border-radius:999px;padding:.5em .9em;font:600 14px system-ui,sans-serif;'
    'text-decoration:none;box-shadow:0 2px 8px #0008">⚡ Zigbee</a>\n'
)


def inject_html(data, head_extra, button=ZIGBEE_BUTTON):
    """App-Kopfzeilen nach <head> und den Zigbee-Knopf vor </body> einfügen (Bytes rein, Bytes raus)."""
    text = data.decode("utf-8", "surrogateescape")
    low = text.lower()
    if "thomyshome-zigbee" in low:        # schon vorhanden (z. B. lichtapp.py selbst angepasst)
        return data
    i = low.find("<head")
    if i >= 0:
        j = low.find(">", i)
        text = text[:j + 1] + "\n" + head_extra + text[j + 1:]
    else:
        text = head_extra + text
    k = text.lower().rfind("</body>")
    text = (text[:k] + button + text[k:]) if k >= 0 else text + button
    return text.encode("utf-8", "surrogateescape")


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "ThomysHomeProxy/1.0"
    api = None          # Z2MApi
    upstream = "http://127.0.0.1:8099"
    head_extra = ""
    timeout_s = 60

    def log_message(self, fmt, *args):   # ruhig bleiben, systemd-Journal nicht fluten
        pass

    def _send(self, status, ctype, body, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))

    def _route(self):
        u = urllib.parse.urlsplit(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if self.command in ("GET", "HEAD"):
            hit = self.api.page(u.path, self.head_extra)
            if hit is not None:
                return self._send(*hit)
            hit = pwa.handle(u.path)
            if hit is not None:
                return self._send(*hit)
        hit = self.api.handle(self.command, u.path, params)
        if hit is not None:
            return self._json(*hit)
        return self._forward()

    def _forward(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        req = urllib.request.Request(self.upstream + self.path, data=body, method=self.command, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout_s)
        except urllib.error.HTTPError as e:
            resp = e                                   # Fehlerstatus des Dashboards 1:1 weiterreichen
        except (urllib.error.URLError, OSError) as e:
            return self._json(502, {"ok": False, "error": "ThomysHomeAgent (lichtapp.py) nicht erreichbar unter %s: %s" % (self.upstream, getattr(e, "reason", e))})
        with resp:
            data = resp.read()
            status = resp.getcode() if hasattr(resp, "getcode") else resp.code
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
            out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP | {"content-type"}]
        if "text/html" in ctype.lower() and not resp.headers.get("Content-Encoding"):
            data = inject_html(data, self.head_extra)
        self._send(status, ctype, data, out_headers)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _route


def make_server(cfg, port=8098, upstream="http://127.0.0.1:8099", host="0.0.0.0", z2m=None):
    z2m = z2m or Zigbee2MQTT(cfg, client_id="thomyshomeagent-proxy")
    if not z2m.mqtt._running:
        z2m.start(block_until_connected=False)

    class Handler(ProxyHandler):
        pass
    Handler.api = Z2MApi(z2m)
    Handler.upstream = upstream.rstrip("/")
    Handler.head_extra = pwa.head_tags()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.z2m = z2m
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description="ThomysHome-Proxy: Zigbee-Seite und Handy-App vor lichtapp.py")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--upstream", default="http://127.0.0.1:8099", help="Adresse von lichtapp.py")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cert"), ap.add_argument("--key")
    a = ap.parse_args(argv)
    srv = make_server(load_config(a.config), a.port, a.upstream, a.host)
    scheme = "http"
    if a.cert and a.key:
        pwa.wrap_https(srv, a.cert, a.key)
        scheme = "https"
    print("ThomysHome-Proxy auf %s://%s:%d → %s  (Zigbee-Seite: /zigbee)" % (scheme, a.host, a.port, a.upstream))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.z2m.stop()


if __name__ == "__main__":
    main()
