#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests für thomyshome_proxy.py:  python3 test_proxy.py  (simuliertes Dashboard + simuliertes Zigbee2MQTT)"""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import test_z2m
import thomyshome_proxy
from thomyshome_proxy import inject_html

DASHBOARD = "<!doctype html><html><head><title>ThomysHomeAgent</title></head><body><h1>Grundriss</h1></body></html>"


class FakeLichtapp(BaseHTTPRequestHandler):
    """Ersatz für lichtapp.py: liefert das Dashboard und ein paar /api-Routen."""
    calls = []

    def log_message(self, *a):
        pass

    def _out(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-App", "lichtapp")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeLichtapp.calls.append(("GET", self.path, None))
        if self.path == "/":
            return self._out(200, "text/html; charset=utf-8", DASHBOARD.encode())
        if self.path.startswith("/api/state"):
            return self._out(200, "application/json", json.dumps({"lights": 3, "bar": "ON"}).encode())
        return self._out(404, "text/plain", b"nicht gefunden")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeLichtapp.calls.append(("POST", self.path, body))
        return self._out(200, "application/json", json.dumps({"answer": "✓ Bar blau", "got": body.decode()}).encode())


def get(url, method="GET", data=None):
    req = urllib.request.Request(url, data=data, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        r = e
    return r.getcode(), r.headers, r.read()


class TestProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.broker = test_z2m.FakeBroker(user="smhub", password="geheim").start()
        cls.fake = test_z2m.FakeZ2M(cls.broker, user="smhub", password="geheim").start()
        cls.app = HTTPServer(("127.0.0.1", 0), FakeLichtapp)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cfg = dict(test_z2m.CFG, mqtt_port=cls.broker.port)
        cls.proxy = thomyshome_proxy.make_server(cfg, port=0, upstream="http://127.0.0.1:%d" % cls.app.server_address[1], host="127.0.0.1")
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.proxy.server_address[1]
        assert test_z2m.wait_for(lambda: cls.proxy.z2m.info and cls.proxy.z2m.devices)

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.z2m.stop()
        cls.app.shutdown()
        cls.fake.stop()
        cls.broker.stop()

    def test_dashboard_forwarded_with_injection(self):
        status, headers, body = get(self.base + "/")
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn("<title>ThomysHomeAgent</title>", html)
        self.assertIn('rel="manifest"', html)                        # Handy-App-Kopfzeilen
        self.assertLess(html.index('rel="manifest"'), html.index("<title>"))
        self.assertIn('href="/zigbee"', html)                        # Zigbee-Knopf vor </body>
        self.assertLess(html.index("thomyshome-zigbee"), html.index("</body>"))
        self.assertEqual(headers["X-App"], "lichtapp")
        self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_api_forwarded(self):
        status, headers, body = get(self.base + "/api/state?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"lights": 3, "bar": "ON"})
        self.assertEqual(FakeLichtapp.calls[-1], ("GET", "/api/state?x=1", None))
        status, headers, body = get(self.base + "/api/ask?text=bar%20auf%20blau", "POST", b"")
        self.assertEqual(json.loads(body)["answer"], "✓ Bar blau")
        self.assertEqual(FakeLichtapp.calls[-1][:2], ("POST", "/api/ask?text=bar%20auf%20blau"))
        status, headers, body = get(self.base + "/api/place", "POST", b'{"id": 1}')
        self.assertEqual(json.loads(body)["got"], '{"id": 1}')      # Body kommt an
        status, headers, body = get(self.base + "/gibtsnicht")
        self.assertEqual(status, 404)                                # Fehler 1:1 weitergereicht

    def test_local_routes(self):
        status, headers, body = get(self.base + "/zigbee")
        self.assertEqual(status, 200)
        self.assertIn("Zigbee · ThomysHomeAgent", body.decode())
        status, headers, body = get(self.base + "/api/z2m/info")
        self.assertEqual(json.loads(body)["result"]["version"], "2.13.0")
        status, headers, body = get(self.base + "/api/z2m/lights")
        self.assertEqual(len(json.loads(body)["result"]), 3)
        status, headers, body = get(self.base + "/manifest.webmanifest")
        self.assertEqual(json.loads(body)["display"], "standalone")
        status, headers, body = get(self.base + "/icon-192.png")
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
        n = len(self.broker.messages("zigbee2mqtt/ThomysHomeBar/set"))
        status, headers, body = get(self.base + "/api/z2m/toggle?name=ThomysHomeBar", "POST", b"")
        self.assertEqual(status, 200)
        self.assertTrue(test_z2m.wait_for(lambda: len(self.broker.messages("zigbee2mqtt/ThomysHomeBar/set")) == n + 1))
        self.assertFalse(any(c[1].startswith("/api/z2m") or c[1] == "/zigbee" for c in FakeLichtapp.calls))   # nie weitergereicht

    def test_upstream_down(self):
        cfg = dict(test_z2m.CFG, mqtt_port=self.broker.port)
        srv = thomyshome_proxy.make_server(cfg, port=0, upstream="http://127.0.0.1:1", host="127.0.0.1", z2m=self.proxy.z2m)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            status, headers, body = get(base + "/")
            self.assertEqual(status, 502)
            self.assertIn("nicht erreichbar", json.loads(body)["error"])
            status, headers, body = get(base + "/zigbee")
            self.assertEqual(status, 200)                            # Zigbee-Seite läuft trotzdem
        finally:
            srv.shutdown()

    def test_inject_html_edge_cases(self):
        head = "<link rel=manifest>\n"
        self.assertEqual(inject_html(b"<p>ohne head</p>", head), (head + "<p>ohne head</p>" + thomyshome_proxy.ZIGBEE_BUTTON).encode())
        out = inject_html("<html><HEAD lang=de><title>ä</title></HEAD><body>x</body></html>".encode(), head).decode()
        self.assertIn("<HEAD lang=de>\n" + head, out)
        self.assertIn(thomyshome_proxy.ZIGBEE_BUTTON + "</body>", out)
        once = inject_html(b"<head></head><body></body>", head)
        self.assertEqual(inject_html(once, head), once)             # nicht doppelt einfügen


if __name__ == "__main__":
    unittest.main()
