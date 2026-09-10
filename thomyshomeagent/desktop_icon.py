#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desktop_icon.py — ThomysHome-Projekt mit Icon auf dem Schreibtisch
==================================================================
Bringt alle Projektdateien in den App-Ordner (~/lichtagent) und legt auf dem
Schreibtisch des Laptops (Linux) an:

  ThomysHome/          Verknüpfung auf den Projektordner ~/lichtagent, mit Projekt-Icon
                       (darin liegen alle Dateien: lichtapp.py, z2m.py, pwa.py, README, docs/ …)
  ThomysHome           Starter, der das Dashboard im Browser öffnet (auch im Anwendungsmenü)

    cd <Ordner mit diesen Dateien>            # z. B. der heruntergeladene Ordner ThomysHomeAgent
    python3 desktop_icon.py                   # kopiert nach ~/lichtagent, legt Icons + Verknüpfungen an
    python3 desktop_icon.py --url http://192.168.1.54:8099
    python3 desktop_icon.py --app-dir ~/lichtagent --no-copy   # nur Verknüpfungen
    python3 desktop_icon.py --remove          # Verknüpfungen entfernen (Dateien bleiben)

Beim Kopieren werden NIE angefasst: lichtapp.py, lichtagent.py, config.json, settings.json,
scenes.json, layout.json (deine App und deine Zugangsdaten). Eine bestehende homebrain.py
wird vorher als homebrain.py.bak gesichert.

Funktioniert mit GNOME, KDE, XFCE, Cinnamon usw. (freedesktop .desktop-Dateien);
unter Windows wird der Ordner auf den Desktop kopiert und eine .url-Verknüpfung angelegt.
Nur Python-Standardbibliothek.
"""
import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pwa  # noqa: E402

NEVER_TOUCH = {"lichtapp.py", "lichtagent.py", "config.json", "settings.json", "scenes.json", "layout.json",
               "homebrain.py.bak", "ca.key", "server.key", "ca.crt", "server.crt"}
PROJECT_FILES = ("z2m.py", "z2m_api.py", "z2m_page.py", "homebrain.py", "pwa.py", "desktop_icon.py",
                 "thomyshome_proxy.py", "thomyshome-proxy.service",
                 "test_z2m.py", "test_pwa.py", "test_desktop_icon.py", "test_proxy.py", "README.md", "config.example.json")
PROJECT_DIRS = ("docs",)


def run_quiet(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def desktop_dir():
    """Schreibtisch-Ordner (berücksichtigt deutsche Namen wie ~/Schreibtisch über xdg-user-dir)."""
    r = run_quiet(["xdg-user-dir", "DESKTOP"], 5)
    if r and r.stdout.strip() and os.path.isdir(r.stdout.strip()):
        return r.stdout.strip()
    for name in ("Schreibtisch", "Desktop"):
        p = os.path.join(os.path.expanduser("~"), name)
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


def copy_project(app_dir):
    """Projektdateien nach app_dir kopieren (Quelle: Ordner dieses Skripts)."""
    os.makedirs(app_dir, exist_ok=True)
    if os.path.realpath(app_dir) == os.path.realpath(HERE):
        print("Projektdateien liegen bereits in", app_dir)
        return
    for name in PROJECT_FILES:
        src, dst = os.path.join(HERE, name), os.path.join(app_dir, name)
        if not os.path.exists(src) or name in NEVER_TOUCH:
            continue
        if name == "homebrain.py" and os.path.exists(dst):
            bak = dst + ".bak"
            if not os.path.exists(bak):
                shutil.copy2(dst, bak)
                print("gesichert:", bak)
        shutil.copy2(src, dst)
        print("kopiert:", dst)
    for d in PROJECT_DIRS:
        src, dst = os.path.join(HERE, d), os.path.join(app_dir, d)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
            print("kopiert:", dst + "/")


def write_icons(icon_dir):
    os.makedirs(icon_dir, exist_ok=True)
    paths = {}
    for size in (256, 512):
        p = os.path.join(icon_dir, "thomyshome-%d.png" % size)
        if not os.path.exists(p):
            with open(p, "wb") as f:
                f.write(pwa.icon_png(size))
        paths[size] = p
    return paths


def desktop_entry(name, url, icon_path, comment):
    return (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        "Name=%s\n"
        "Comment=%s\n"
        "Exec=xdg-open %s\n"
        "Icon=%s\n"
        "Terminal=false\n"
        "Categories=Utility;HomeAutomation;\n"
        "StartupNotify=false\n"
    ) % (name, comment, url, icon_path)


def set_folder_icon(folder, icon_path):
    """Projekt-Icon für den Ordner: GNOME/Nautilus per gio-Metadaten, KDE/XFCE per .directory."""
    run_quiet(["gio", "set", folder, "metadata::custom-icon", "file://" + icon_path], 5)
    try:
        with open(os.path.join(folder, ".directory"), "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nIcon=%s\n" % icon_path)
    except OSError:
        pass


def install_linux(name, url, comment, app_dir, copy=True, remove=False):
    desk = desktop_dir()
    launcher_name = "thomyshome.desktop"
    link = os.path.join(desk, name)                                   # Ordner-Verknüpfung
    launchers = [os.path.join(desk, launcher_name),
                 os.path.join(os.path.expanduser("~/.local/share/applications"), launcher_name)]
    if remove:
        for t in launchers:
            if os.path.exists(t):
                os.remove(t)
                print("entfernt:", t)
        if os.path.islink(link):
            os.remove(link)
            print("entfernt:", link)
        return
    if copy:
        copy_project(app_dir)
    icons = write_icons(os.path.join(app_dir, "icons"))
    # 1) Projektordner auf dem Schreibtisch (Verknüpfung auf ~/lichtagent) mit Icon
    if os.path.islink(link) or not os.path.exists(link):
        if os.path.islink(link):
            os.remove(link)
        os.symlink(os.path.realpath(app_dir), link)
        print("angelegt:", link, "→", app_dir)
    else:
        print("übersprungen (existiert bereits und ist keine Verknüpfung):", link)
    set_folder_icon(app_dir, icons[256])
    # 2) Starter fürs Dashboard: Schreibtisch + Anwendungsmenü
    entry = desktop_entry(name, url, icons[256], comment)
    for t in launchers:
        os.makedirs(os.path.dirname(t), exist_ok=True)
        with open(t, "w", encoding="utf-8") as f:
            f.write(entry)
        os.chmod(t, os.stat(t).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        run_quiet(["gio", "set", t, "metadata::trusted", "true"], 5)   # sonst „nicht vertrauenswürdig“ in GNOME
        print("angelegt:", t)
    run_quiet(["update-desktop-database", os.path.expanduser("~/.local/share/applications")])
    print("Icon:", icons[256])
    print("Dashboard:", url)


def install_windows(name, url, app_dir, copy=True, remove=False):
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    target = os.path.join(desktop, name + ".url")
    folder = os.path.join(desktop, name)
    if remove:
        for p in (target,):
            if os.path.exists(p):
                os.remove(p)
                print("entfernt:", p)
        return
    if copy:
        copy_project(folder)              # unter Windows: Projektordner direkt auf dem Desktop
    icons = write_icons(os.path.join(folder, "icons"))
    with open(target, "w", encoding="utf-8") as f:
        f.write("[InternetShortcut]\nURL=%s\nIconFile=%s\nIconIndex=0\n" % (url, icons[256]))
    print("angelegt:", folder, "und", target)


def main(argv=None):
    ap = argparse.ArgumentParser(description="ThomysHome-Projekt mit Icon auf dem Schreibtisch anlegen")
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="Adresse des Dashboards (Standard: http://127.0.0.1:8099)")
    ap.add_argument("--name", default="ThomysHome", help="Name von Ordner-Verknüpfung und Starter")
    ap.add_argument("--comment", default="ThomysHomeAgent – Licht- und Zigbee-Steuerung", help="Beschreibung des Starters")
    ap.add_argument("--app-dir", default=os.path.expanduser("~/lichtagent"), help="Projekt-/App-Ordner (Standard: ~/lichtagent)")
    ap.add_argument("--no-copy", action="store_true", help="Projektdateien nicht kopieren, nur Verknüpfungen anlegen")
    ap.add_argument("--remove", action="store_true", help="Verknüpfungen entfernen (Dateien bleiben)")
    a = ap.parse_args(argv)
    app_dir = os.path.expanduser(a.app_dir)
    if platform.system() == "Windows":
        install_windows(a.name, a.url, app_dir, not a.no_copy, a.remove)
    else:
        install_linux(a.name, a.url, a.comment, app_dir, not a.no_copy, a.remove)


if __name__ == "__main__":
    main()
