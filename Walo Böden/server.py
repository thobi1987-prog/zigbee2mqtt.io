#!/usr/bin/env python3
"""Walo Böden – Lager: Webserver (Raspberry-Pi-tauglich, ohne Fremdpakete).

Startet einen kleinen Webserver mit Python-Bordmitteln (http.server). Liefert
die Weboberfläche (static/index.html) aus und stellt eine JSON-API bereit, die
auf dieselbe SQLite-Datenbank wie das CLI-Programm (inventar.py) zugreift.

Start:
    python3 server.py                 # http://<pi-ip>:8000
    python3 server.py --port 8080
    PORT=8080 python3 server.py

Danach im Browser (im selben Netzwerk) öffnen:
    http://raspberrypi.local:8000   oder   http://<IP-des-Pi>:8000
"""

import argparse
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import inventar  # DB-Logik wird wiederverwendet

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")


# --------------------------------------------------------------------------- #
# Datenzugriff (nutzt inventar.connect / inventar._post_buchung / inventar._find)
# --------------------------------------------------------------------------- #

def get_state():
    conn = inventar.connect()
    artikel = [dict(r) for r in conn.execute(
        "SELECT * FROM artikel ORDER BY artikelnummer")]
    journal = [dict(r) for r in conn.execute(
        """SELECT b.id, b.typ, b.menge, b.beleg, b.bemerkung, b.gebucht_am,
                  a.artikelnummer, a.einheit
           FROM buchung b JOIN artikel a ON a.id = b.artikel_id
           ORDER BY b.gebucht_am DESC, b.id DESC
           LIMIT 200""")]
    conn.close()
    return {"artikel": artikel, "journal": journal}


def create_artikel(data):
    nummer = (data.get("nummer") or "").strip()
    bezeichnung = (data.get("bezeichnung") or "").strip()
    if not nummer or not bezeichnung:
        raise ValueError("Artikelnummer und Bezeichnung sind Pflichtfelder.")
    einheit = (data.get("einheit") or "Stk").strip()
    beschreibung = (data.get("beschreibung") or "").strip() or None
    bestand = _pos(data.get("bestand"), "Anfangsbestand")
    mindest = _pos(data.get("mindestbestand"), "Mindestbestand")
    preis = _pos(data.get("preis"), "Einkaufspreis")
    ts = inventar.now()

    conn = inventar.connect()
    try:
        cur = conn.execute(
            """INSERT INTO artikel
               (artikelnummer, bezeichnung, beschreibung, einheit, bestand,
                mindestbestand, einkaufspreis, aktiv, erstellt_am, geaendert_am)
               VALUES (?, ?, ?, ?, 0, ?, ?, 1, ?, ?)""",
            (nummer, bezeichnung, beschreibung, einheit, mindest, preis, ts, ts),
        )
    except sqlite3.IntegrityError:
        conn.close()
        raise ValueError(f"Artikelnummer „{nummer}“ existiert bereits.")
    if bestand > 0:
        artikel = conn.execute("SELECT * FROM artikel WHERE id = ?", (cur.lastrowid,)).fetchone()
        inventar._post_buchung(conn, artikel, "anfangsbestand", bestand,
                               bemerkung="Anfangsbestand bei Anlage")
    conn.commit()
    conn.close()


def buchen(data):
    nummer = (data.get("nummer") or "").strip()
    typ = (data.get("typ") or "").strip()
    if typ not in ("zugang", "abgang", "korrektur"):
        raise ValueError("Ungültiger Buchungstyp.")
    menge = data.get("menge")
    if menge is None:
        raise ValueError("Menge fehlt.")
    menge = float(menge)
    beleg = (data.get("beleg") or "").strip() or None

    conn = inventar.connect()
    artikel = inventar._find(conn, nummer)
    if not artikel:
        conn.close()
        raise ValueError(f"Artikel „{nummer}“ nicht gefunden.")

    if typ == "korrektur":
        # menge = Zielbestand
        delta = menge - artikel["bestand"]
        if delta == 0:
            conn.close()
            return
        inventar._post_buchung(conn, artikel, "korrektur", delta, beleg=beleg,
                               bemerkung="Bestandskorrektur / Inventur")
    else:
        if menge <= 0:
            conn.close()
            raise ValueError("Menge muss größer als 0 sein.")
        signed = menge if typ == "zugang" else -menge
        if typ == "abgang" and artikel["bestand"] + signed < 0:
            conn.close()
            raise ValueError(
                f"Abgang von {inventar._num(menge)} nicht möglich – "
                f"Bestand ist nur {inventar._num(artikel['bestand'])}.")
        inventar._post_buchung(conn, artikel, typ, signed, beleg=beleg)
    conn.commit()
    conn.close()


def _pos(value, name):
    try:
        v = float(value if value not in (None, "") else 0)
    except (TypeError, ValueError):
        raise ValueError(f"{name} muss eine Zahl sein.")
    if v < 0:
        raise ValueError(f"{name} darf nicht negativ sein.")
    return v


# --------------------------------------------------------------------------- #
# HTTP-Handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "WaloLager/1.0"

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ValueError("Ungültige Anfrage (kein gültiges JSON).")

    def _serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self.send_error(404, "Nicht gefunden")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            try:
                self._json(get_state())
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)
            return
        self._serve_static(self.path)

    def do_POST(self):
        try:
            data = self._read_json()
            if self.path == "/api/artikel":
                create_artikel(data)
            elif self.path == "/api/buchung":
                buchen(data)
            else:
                self.send_error(404, "Unbekannter Endpunkt")
                return
            self._json(get_state())
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": "Serverfehler: " + str(exc)}, 500)

    def log_message(self, fmt, *args):
        # ruhigeres Log
        pass


def main():
    parser = argparse.ArgumentParser(description="Walo Böden – Lager-Webserver")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind-Adresse (Standard 0.0.0.0 = im ganzen Netz erreichbar)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args()

    inventar.connect().close()  # DB/Tabellen sicherstellen
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Walo Böden – Lager läuft auf http://{args.host}:{args.port}")
    print(f"Datenbank: {inventar.DB_PATH}")
    print("Beenden mit STRG+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")
        httpd.server_close()


if __name__ == "__main__":
    main()
