#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests für desktop_icon.py (läuft in einem Wegwerf-HOME):  python3 test_desktop_icon.py"""
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

import desktop_icon


class TestDesktopIcon(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, "Schreibtisch"))
        self.app = os.path.join(self.home, "lichtagent")
        os.makedirs(self.app)
        for name, content in (("lichtapp.py", "# meine App\n"), ("config.json", '{"geheim": 1}\n'), ("homebrain.py", "# alt\n")):
            with open(os.path.join(self.app, name), "w") as f:
                f.write(content)
        self.env = mock.patch.dict(os.environ, {"HOME": self.home, "PATH": "/nonexistent"})  # kein xdg-user-dir/gio
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_install_copies_project_and_creates_shortcuts(self):
        desktop_icon.main(["--url", "http://192.168.1.54:8099", "--app-dir", self.app])
        # Projektdateien kopiert, eigene Dateien unangetastet, homebrain.py gesichert
        for name in ("z2m.py", "z2m_api.py", "homebrain.py", "pwa.py", "README.md", "config.example.json"):
            self.assertTrue(os.path.exists(os.path.join(self.app, name)), name)
        self.assertEqual(open(os.path.join(self.app, "lichtapp.py")).read(), "# meine App\n")
        self.assertEqual(open(os.path.join(self.app, "config.json")).read(), '{"geheim": 1}\n')
        self.assertEqual(open(os.path.join(self.app, "homebrain.py.bak")).read(), "# alt\n")
        self.assertNotEqual(open(os.path.join(self.app, "homebrain.py")).read(), "# alt\n")
        self.assertTrue(os.path.isdir(os.path.join(self.app, "docs")))
        # Ordner-Verknüpfung mit Icon auf dem Schreibtisch
        link = os.path.join(self.home, "Schreibtisch", "ThomysHome")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(self.app))
        icon = os.path.join(self.app, "icons", "thomyshome-256.png")
        self.assertEqual(open(icon, "rb").read()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("Icon=" + icon, open(os.path.join(self.app, ".directory")).read())
        # Starter auf Schreibtisch + im Menü
        for p in (os.path.join(self.home, "Schreibtisch", "thomyshome.desktop"),
                  os.path.join(self.home, ".local", "share", "applications", "thomyshome.desktop")):
            self.assertTrue(os.path.exists(p), p)
            self.assertTrue(os.stat(p).st_mode & stat.S_IXUSR)
            txt = open(p, encoding="utf-8").read()
            self.assertIn("Exec=xdg-open http://192.168.1.54:8099", txt)
            self.assertIn("Icon=" + icon, txt)
        # zweiter Lauf ist harmlos (keine zweite Sicherung, Verknüpfung bleibt)
        desktop_icon.main(["--app-dir", self.app])
        self.assertEqual(open(os.path.join(self.app, "homebrain.py.bak")).read(), "# alt\n")
        self.assertTrue(os.path.islink(link))
        # entfernen
        desktop_icon.main(["--remove", "--app-dir", self.app])
        self.assertFalse(os.path.lexists(link))
        self.assertFalse(os.path.exists(os.path.join(self.home, "Schreibtisch", "thomyshome.desktop")))
        self.assertTrue(os.path.exists(os.path.join(self.app, "z2m.py")))   # Dateien bleiben

    def test_no_copy(self):
        desktop_icon.main(["--no-copy", "--app-dir", self.app])
        self.assertFalse(os.path.exists(os.path.join(self.app, "z2m.py")))
        self.assertTrue(os.path.islink(os.path.join(self.home, "Schreibtisch", "ThomysHome")))

    def test_desktop_dir_fallback(self):
        self.assertEqual(desktop_icon.desktop_dir(), os.path.join(self.home, "Schreibtisch"))


if __name__ == "__main__":
    unittest.main()
