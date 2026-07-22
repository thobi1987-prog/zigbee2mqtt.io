#!/usr/bin/env python3
"""Walo Böden – Lagerbuchhaltung / Inventar (Stufe 1).

Einfaches Inventarsystem für Artikelstammdaten und Bestände.
Speicherung in einer lokalen SQLite-Datei (inventar.db), kein Server nötig.

Verwendung (Beispiele):
    python3 inventar.py init
    python3 inventar.py add --nummer A-100 --bezeichnung "Parkett Eiche" --einheit m2 --bestand 50
    python3 inventar.py list
    python3 inventar.py show A-100
    python3 inventar.py set-bestand A-100 42
    python3 inventar.py edit A-100 --bezeichnung "Parkett Eiche natur"
    python3 inventar.py deactivate A-100
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventar.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS artikel (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    artikelnummer  TEXT    NOT NULL UNIQUE,
    bezeichnung    TEXT    NOT NULL,
    beschreibung   TEXT,
    einheit        TEXT    NOT NULL DEFAULT 'Stück',
    bestand        REAL    NOT NULL DEFAULT 0,
    mindestbestand REAL    NOT NULL DEFAULT 0,
    aktiv          INTEGER NOT NULL DEFAULT 1,
    erstellt_am    TEXT    NOT NULL,
    geaendert_am   TEXT    NOT NULL
);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def cmd_init(args):
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Datenbank bereit: {DB_PATH}")


def cmd_add(args):
    ts = now()
    conn = connect()
    conn.executescript(SCHEMA)  # sicherstellen, dass Tabelle existiert
    try:
        conn.execute(
            """INSERT INTO artikel
               (artikelnummer, bezeichnung, beschreibung, einheit, bestand,
                mindestbestand, aktiv, erstellt_am, geaendert_am)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (args.nummer, args.bezeichnung, args.beschreibung, args.einheit,
             args.bestand, args.mindestbestand, ts, ts),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        print(f"Fehler: Artikelnummer '{args.nummer}' existiert bereits.", file=sys.stderr)
        conn.close()
        sys.exit(1)
    conn.close()
    print(f"Artikel '{args.nummer}' angelegt.")


def _find(conn, nummer):
    return conn.execute(
        "SELECT * FROM artikel WHERE artikelnummer = ?", (nummer,)
    ).fetchone()


def cmd_list(args):
    conn = connect()
    conn.executescript(SCHEMA)
    query = "SELECT * FROM artikel"
    params = []
    conds = []
    if not args.alle:
        conds.append("aktiv = 1")
    if args.suche:
        conds.append("(artikelnummer LIKE ? OR bezeichnung LIKE ?)")
        params += [f"%{args.suche}%", f"%{args.suche}%"]
    if conds:
        query += " WHERE " + " AND ".join(conds)
    query += " ORDER BY artikelnummer"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("Keine Artikel gefunden.")
        return

    print(f"{'Nummer':<12} {'Bezeichnung':<30} {'Bestand':>10} {'Einheit':<8} {'Aktiv':<5}")
    print("-" * 70)
    for r in rows:
        warn = "  ⚠" if r["mindestbestand"] and r["bestand"] < r["mindestbestand"] else ""
        aktiv = "ja" if r["aktiv"] else "nein"
        print(f"{r['artikelnummer']:<12} {r['bezeichnung'][:30]:<30} "
              f"{_num(r['bestand']):>10} {r['einheit']:<8} {aktiv:<5}{warn}")


def cmd_show(args):
    conn = connect()
    conn.executescript(SCHEMA)
    r = _find(conn, args.nummer)
    conn.close()
    if not r:
        print(f"Artikel '{args.nummer}' nicht gefunden.", file=sys.stderr)
        sys.exit(1)
    print(f"Artikelnummer : {r['artikelnummer']}")
    print(f"Bezeichnung   : {r['bezeichnung']}")
    print(f"Beschreibung  : {r['beschreibung'] or '-'}")
    print(f"Einheit       : {r['einheit']}")
    print(f"Bestand       : {_num(r['bestand'])}")
    print(f"Mindestbestand: {_num(r['mindestbestand'])}")
    print(f"Aktiv         : {'ja' if r['aktiv'] else 'nein'}")
    print(f"Erstellt      : {r['erstellt_am']}")
    print(f"Geändert      : {r['geaendert_am']}")


def cmd_edit(args):
    conn = connect()
    conn.executescript(SCHEMA)
    r = _find(conn, args.nummer)
    if not r:
        print(f"Artikel '{args.nummer}' nicht gefunden.", file=sys.stderr)
        conn.close()
        sys.exit(1)
    updates, params = [], []
    for field, value in (
        ("bezeichnung", args.bezeichnung),
        ("beschreibung", args.beschreibung),
        ("einheit", args.einheit),
        ("mindestbestand", args.mindestbestand),
    ):
        if value is not None:
            updates.append(f"{field} = ?")
            params.append(value)
    if not updates:
        print("Nichts zu ändern (keine Felder angegeben).")
        conn.close()
        return
    updates.append("geaendert_am = ?")
    params.append(now())
    params.append(args.nummer)
    conn.execute(f"UPDATE artikel SET {', '.join(updates)} WHERE artikelnummer = ?", params)
    conn.commit()
    conn.close()
    print(f"Artikel '{args.nummer}' aktualisiert.")


def cmd_set_bestand(args):
    conn = connect()
    conn.executescript(SCHEMA)
    r = _find(conn, args.nummer)
    if not r:
        print(f"Artikel '{args.nummer}' nicht gefunden.", file=sys.stderr)
        conn.close()
        sys.exit(1)
    conn.execute(
        "UPDATE artikel SET bestand = ?, geaendert_am = ? WHERE artikelnummer = ?",
        (args.menge, now(), args.nummer),
    )
    conn.commit()
    conn.close()
    print(f"Bestand von '{args.nummer}' auf {_num(args.menge)} gesetzt.")


def cmd_deactivate(args):
    _set_aktiv(args.nummer, 0)


def cmd_activate(args):
    _set_aktiv(args.nummer, 1)


def _set_aktiv(nummer, aktiv):
    conn = connect()
    conn.executescript(SCHEMA)
    r = _find(conn, nummer)
    if not r:
        print(f"Artikel '{nummer}' nicht gefunden.", file=sys.stderr)
        conn.close()
        sys.exit(1)
    conn.execute(
        "UPDATE artikel SET aktiv = ?, geaendert_am = ? WHERE artikelnummer = ?",
        (aktiv, now(), nummer),
    )
    conn.commit()
    conn.close()
    print(f"Artikel '{nummer}' {'aktiviert' if aktiv else 'deaktiviert'}.")


def _num(value):
    """Zeigt ganze Zahlen ohne Nachkommastellen, sonst mit."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def build_parser():
    p = argparse.ArgumentParser(
        description="Walo Böden – Inventar (Artikelstammdaten & Bestände)."
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Datenbank/Tabellen anlegen").set_defaults(func=cmd_init)

    a = sub.add_parser("add", help="Artikel anlegen")
    a.add_argument("--nummer", required=True, help="Artikelnummer (eindeutig)")
    a.add_argument("--bezeichnung", required=True)
    a.add_argument("--beschreibung", default=None)
    a.add_argument("--einheit", default="Stück")
    a.add_argument("--bestand", type=float, default=0)
    a.add_argument("--mindestbestand", type=float, default=0)
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="Artikel auflisten")
    l.add_argument("--suche", default=None, help="Filter nach Nummer/Bezeichnung")
    l.add_argument("--alle", action="store_true", help="Auch inaktive Artikel zeigen")
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="Artikel-Details anzeigen")
    s.add_argument("nummer")
    s.set_defaults(func=cmd_show)

    e = sub.add_parser("edit", help="Stammdaten ändern")
    e.add_argument("nummer")
    e.add_argument("--bezeichnung", default=None)
    e.add_argument("--beschreibung", default=None)
    e.add_argument("--einheit", default=None)
    e.add_argument("--mindestbestand", type=float, default=None)
    e.set_defaults(func=cmd_edit)

    sb = sub.add_parser("set-bestand", help="Bestand direkt setzen")
    sb.add_argument("nummer")
    sb.add_argument("menge", type=float)
    sb.set_defaults(func=cmd_set_bestand)

    d = sub.add_parser("deactivate", help="Artikel deaktivieren")
    d.add_argument("nummer")
    d.set_defaults(func=cmd_deactivate)

    ac = sub.add_parser("activate", help="Artikel wieder aktivieren")
    ac.add_argument("nummer")
    ac.set_defaults(func=cmd_activate)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
