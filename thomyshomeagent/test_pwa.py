#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests für pwa.py:  python3 test_pwa.py"""
import json
import struct
import unittest
import zlib

import pwa


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "kein PNG"
    w, h, depth, ctype = struct.unpack("!IIBB", data[16:26])
    return w, h, depth, ctype


def png_pixels(data):
    """Dekodiert unser eigenes (filterloses) PNG zurück in Zeilen."""
    pos, idat = 8, b""
    while pos < len(data):
        ln, tag = struct.unpack("!I", data[pos:pos + 4])[0], data[pos + 4:pos + 8]
        if tag == b"IDAT":
            idat += data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
    w, h, _, _ = png_size(data)
    raw = zlib.decompress(idat)
    return [raw[i * (w * 4 + 1) + 1:(i + 1) * (w * 4 + 1)] for i in range(h)]


class TestPwa(unittest.TestCase):
    def test_manifest(self):
        status, ctype, body = pwa.handle("/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("application/manifest+json"))
        m = json.loads(body)
        self.assertEqual(m["display"], "standalone")
        self.assertEqual(m["start_url"], "/")
        self.assertEqual({i["sizes"] for i in m["icons"]}, {"192x192", "512x512"})
        self.assertIn("maskable", [i["purpose"] for i in m["icons"]])
        for i in m["icons"]:
            self.assertIsNotNone(pwa.handle(i["src"]), i["src"])

    def test_icons(self):
        for path, size, ctype in (("/icon-192.png", 192, 0), ("/icon-512.png", 512, 0), ("/icon-maskable-512.png", 512, 0)):
            status, ct, body = pwa.handle(path)
            self.assertEqual(ct, "image/png")
            w, h, depth, ctyp = png_size(body)
            self.assertEqual((w, h, depth, ctyp), (size, size, 8, 6))
        rows = png_pixels(pwa.handle("/icon-192.png")[2])
        corner = rows[0][0:4]
        self.assertEqual(corner[3], 0, "Ecke muss transparent sein (abgerundet)")
        centre = rows[int(192 * 0.42)][96 * 4:96 * 4 + 4]
        self.assertGreater(centre[0], 200)              # Lampe: warm/gelb
        self.assertEqual(centre[3], 255)
        rows_m = png_pixels(pwa.handle("/icon-maskable-512.png")[2])
        self.assertEqual(rows_m[0][3], 255, "maskable: keine transparenten Ecken")

    def test_service_worker_and_head(self):
        status, ctype, body = pwa.handle("/sw.js")
        self.assertTrue(ctype.startswith("application/javascript"))
        self.assertIn(("thomyshome-v%s" % pwa.SW_VERSION).encode(), body)
        self.assertIn(b"startsWith('/api/')", body)       # Steuerung nie aus dem Cache
        head = pwa.head_tags()
        self.assertIn('rel="manifest"', head)
        self.assertIn('serviceWorker', head)
        self.assertIn('theme-color', head)
        self.assertIsNone(pwa.handle("/api/state"))
        self.assertIsNone(pwa.handle("/"))


if __name__ == "__main__":
    unittest.main()
