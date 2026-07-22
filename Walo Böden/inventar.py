#!/usr/bin/env python3
"""Walo Böden – Lagerbuchhaltung / Inventar (Stufe 1 + 2).

Inventarsystem für Artikelstammdaten, Bestände und Buchungen.
Speicherung in einer lokalen SQLite-Datei (inventar.db), kein Server nötig.

Prinzip (Stufe 2): Der Bestand wird nicht mehr frei gesetzt, sondern ergibt
sich aus allen Buchungen (Zugang +, Abgang −, Korrektur ±, Anfangsbestand).
Jede Bewegung wird im Journal festgehalten. Die Spalte artikel.bestand ist der
laufende Saldo und wird bei jeder Buchung in derselben Transaktion fortgeschrieben.

Verwendung (Beispiele):
    python3 inventar.py init
    python3 inventar.py add --nummer A-100 --bezeichnung "Parkett Eiche" --einheit m2 --bestand 50
    python3 inventar.py zugang A-100 20 --beleg LS-2026-001
    python3 inventar.py abgang A-100 8  --beleg AB-4711
    python3 inventar.py journal A-100
    python3 inventar.py list
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

CREATE TABLE IF NOT EXISTS buchung (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    artikel_id  INTEGER NOT NULL REFERENCES artikel(id) ON DELETE CASCADE,
    typ         TEXT    NOT NULL,   -- anfangsbestand | zugang | abgang | korrektur
    menge       REAL    NOT NULL,   -- vorzeichenbehaftet: Zugang +, Abgang −
    preis       REAL,               -- optionaler Einzelpreis (für spätere Bewertung)
    beleg       TEXT,               -- optionale Referenz (Lieferschein, Auftrag …)
    bemerkung   TEXT,
    gebucht_am  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_buchung_artikel ON buchung(artikel_id);
"""

# Für welche typ-Werte gibt der Nutzer eine positive Menge an, die abgezogen wird
TYP_LABELS = {
    "anfangsbestand": "Anfangsbestand",
    "zugang": "Zugang",
    "abgang": "Abgang",
    "korrektur": "Korrektur",
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _find(conn, nummer):
    return conn.execute(
        "SELECT * FROM artikel WHERE artikelnummer = ?", (nummer,)
    ).fetchone()


def _num(value):
    """Zeigt ganze Zahlen ohne Nachkommastellen, sonst mit zwei Stellen."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def _post_buchung(conn, artikel, typ, menge_signed, preis=None, beleg=None,
                  bemerkung=None, datum=None):
    """Bucht eine Bewegung und schreibt den Saldo in derselben Transaktion fort."""
    ts = datum or now()
    conn.execute(
        """INSERT INTO buchung
           (artikel_id, typ, menge, preis, beleg, bemerkung, gebucht_am)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (artikel["id"], typ, menge_signed, preis, beleg, bemerkung, ts),
    )
    conn.execute(
        "UPDATE artikel SET bestand = bestand + ?, geaendert_am = ? WHERE id = ?",
        (menge_signed, ts, artikel["id"]),
    )


# --------------------------------------------------------------------------- #
# Stammdaten-Befehle
# --------------------------------------------------------------------------- #

def cmd_init(args):
    connect().close()
    print(f"Datenbank bereit: {DB_PATH}")


def cmd_add(args):
    ts = now()
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO artikel
               (artikelnummer, bezeichnung, beschreibung, einheit, bestand,
                mindestbestand, aktiv, erstellt_am, geaendert_am)
               VALUES (?, ?, ?, ?, 0, ?, 1, ?, ?)""",
            (args.nummer, args.bezeichnung, args.beschreibung, args.einheit,
             args.mindestbestand, ts, ts),
        )
    except sqlite3.IntegrityError:
        print(f"Fehler: Artikelnummer '{args.nummer}' existiert bereits.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Startbestand als saubere Buchung erfassen (statt Feld direkt zu setzen)
    if args.bestand:
        artikel = conn.execute("SELECT * FROM artikel WHERE id = ?", (cur.lastrowid,)).fetchone()
        _post_buchung(conn, artikel, "anfangsbestand", float(args.bestand),
                      bemerkung="Anfangsbestand bei Anlage")
    conn.commit()
    conn.close()
    print(f"Artikel '{args.nummer}' angelegt"
          + (f" (Anfangsbestand {_num(args.bestand)})." if args.bestand else "."))


def cmd_list(args):
    conn = connect()
    query = "SELECT * FROM artikel"
    params, conds = [], []
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
    params += [now(), args.nummer]
    conn.execute(f"UPDATE artikel SET {', '.join(updates)} WHERE artikelnummer = ?", params)
    conn.commit()
    conn.close()
    print(f"Artikel '{args.nummer}' aktualisiert.")


def cmd_deactivate(args):
    _set_aktiv(args.nummer, 0)


def cmd_activate(args):
    _set_aktiv(args.nummer, 1)


def _set_aktiv(nummer, aktiv):
    conn = connect()
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


# --------------------------------------------------------------------------- #
# Buchungs-Befehle (Stufe 2)
# --------------------------------------------------------------------------- #

def _buchen(nummer, typ, menge_positiv, preis, beleg, bemerkung, erlaube_negativ=False):
    if menge_positiv <= 0:
        print("Fehler: Menge muss größer als 0 sein.", file=sys.stderr)
        sys.exit(1)
    conn = connect()
    artikel = _find(conn, nummer)
    if not artikel:
        print(f"Artikel '{nummer}' nicht gefunden.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    signed = menge_positiv if typ == "zugang" else -menge_positiv
    neuer_bestand = artikel["bestand"] + signed
    if signed < 0 and neuer_bestand < 0 and not erlaube_negativ:
        print(f"Fehler: Abgang von {_num(menge_positiv)} nicht möglich – "
              f"Bestand ist nur {_num(artikel['bestand'])} "
              f"(würde {_num(neuer_bestand)} ergeben). "
              f"Mit --erlaube-negativ trotzdem buchen.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    _post_buchung(conn, artikel, typ, signed, preis, beleg, bemerkung)
    conn.commit()
    conn.close()
    print(f"{TYP_LABELS[typ]} {_num(menge_positiv)} auf '{nummer}' gebucht. "
          f"Neuer Bestand: {_num(neuer_bestand)}.")


def cmd_zugang(args):
    _buchen(args.nummer, "zugang", args.menge, args.preis, args.beleg, args.bemerkung)


def cmd_abgang(args):
    _buchen(args.nummer, "abgang", args.menge, args.preis, args.beleg,
            args.bemerkung, erlaube_negativ=args.erlaube_negativ)


def cmd_korrektur(args):
    """Setzt den Bestand per Korrektur-Buchung auf einen Zielwert."""
    conn = connect()
    artikel = _find(conn, args.nummer)
    if not artikel:
        print(f"Artikel '{args.nummer}' nicht gefunden.", file=sys.stderr)
        conn.close()
        sys.exit(1)
    delta = args.zielbestand - artikel["bestand"]
    if delta == 0:
        print(f"Bestand ist bereits {_num(args.zielbestand)} – keine Korrektur nötig.")
        conn.close()
        return
    _post_buchung(conn, artikel, "korrektur", delta, beleg=args.beleg,
                  bemerkung=args.bemerkung or "Bestandskorrektur / Inventur")
    conn.commit()
    conn.close()
    vorz = "+" if delta > 0 else ""
    print(f"Korrektur {vorz}{_num(delta)} auf '{args.nummer}' gebucht. "
          f"Neuer Bestand: {_num(args.zielbestand)}.")


def cmd_journal(args):
    conn = connect()
    params = []
    query = """SELECT b.*, a.artikelnummer, a.einheit
               FROM buchung b JOIN artikel a ON a.id = b.artikel_id"""
    if args.nummer:
        artikel = _find(conn, args.nummer)
        if not artikel:
            print(f"Artikel '{args.nummer}' nicht gefunden.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        query += " WHERE b.artikel_id = ?"
        params.append(artikel["id"])
    query += " ORDER BY b.gebucht_am, b.id"
    if args.limit:
        query += " LIMIT ?"
        params.append(args.limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("Keine Buchungen gefunden.")
        return

    print(f"{'Datum':<20} {'Artikel':<10} {'Typ':<14} {'Menge':>10} {'Beleg':<14}")
    print("-" * 74)
    for r in rows:
        menge = _num(r["menge"])
        if r["menge"] > 0:
            menge = "+" + menge
        print(f"{r['gebucht_am']:<20} {r['artikelnummer']:<10} "
              f"{TYP_LABELS.get(r['typ'], r['typ']):<14} {menge:>10} {r['beleg'] or '':<14}")


def cmd_neu_berechnen(args):
    """Rechnet den Bestand aller Artikel neu aus dem Journal (Konsistenzprüfung)."""
    conn = connect()
    artikel = conn.execute("SELECT * FROM artikel").fetchall()
    korrigiert = 0
    for a in artikel:
        summe = conn.execute(
            "SELECT COALESCE(SUM(menge), 0) AS s FROM buchung WHERE artikel_id = ?",
            (a["id"],),
        ).fetchone()["s"]
        if summe != a["bestand"]:
            print(f"  {a['artikelnummer']}: {_num(a['bestand'])} → {_num(summe)}")
            conn.execute("UPDATE artikel SET bestand = ? WHERE id = ?", (summe, a["id"]))
            korrigiert += 1
    conn.commit()
    conn.close()
    if korrigiert:
        print(f"{korrigiert} Bestand/Bestände aus dem Journal korrigiert.")
    else:
        print("Alle Bestände stimmen mit dem Journal überein.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        description="Walo Böden – Inventar (Artikelstammdaten, Bestände & Buchungen)."
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Datenbank/Tabellen anlegen").set_defaults(func=cmd_init)

    a = sub.add_parser("add", help="Artikel anlegen")
    a.add_argument("--nummer", required=True, help="Artikelnummer (eindeutig)")
    a.add_argument("--bezeichnung", required=True)
    a.add_argument("--beschreibung", default=None)
    a.add_argument("--einheit", default="Stück")
    a.add_argument("--bestand", type=float, default=0, help="Anfangsbestand (als Buchung erfasst)")
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

    d = sub.add_parser("deactivate", help="Artikel deaktivieren")
    d.add_argument("nummer")
    d.set_defaults(func=cmd_deactivate)

    ac = sub.add_parser("activate", help="Artikel wieder aktivieren")
    ac.add_argument("nummer")
    ac.set_defaults(func=cmd_activate)

    # --- Buchungen ---
    z = sub.add_parser("zugang", help="Wareneingang buchen (Bestand +)")
    z.add_argument("nummer")
    z.add_argument("menge", type=float)
    z.add_argument("--preis", type=float, default=None, help="Einzelpreis (optional)")
    z.add_argument("--beleg", default=None, help="z.B. Lieferschein-Nr.")
    z.add_argument("--bemerkung", default=None)
    z.set_defaults(func=cmd_zugang)

    ab = sub.add_parser("abgang", help="Warenausgang buchen (Bestand −)")
    ab.add_argument("nummer")
    ab.add_argument("menge", type=float)
    ab.add_argument("--preis", type=float, default=None)
    ab.add_argument("--beleg", default=None, help="z.B. Auftrags-Nr.")
    ab.add_argument("--bemerkung", default=None)
    ab.add_argument("--erlaube-negativ", action="store_true",
                    help="Abgang auch zulassen, wenn der Bestand negativ würde")
    ab.set_defaults(func=cmd_abgang)

    k = sub.add_parser("korrektur", help="Bestand per Korrektur auf Zielwert setzen (Inventur)")
    k.add_argument("nummer")
    k.add_argument("zielbestand", type=float)
    k.add_argument("--beleg", default=None)
    k.add_argument("--bemerkung", default=None)
    k.set_defaults(func=cmd_korrektur)

    j = sub.add_parser("journal", help="Buchungen anzeigen (optional je Artikel)")
    j.add_argument("nummer", nargs="?", default=None)
    j.add_argument("--limit", type=int, default=None)
    j.set_defaults(func=cmd_journal)

    nb = sub.add_parser("neu-berechnen",
                        help="Bestände aus dem Journal neu berechnen (Prüfung/Reparatur)")
    nb.set_defaults(func=cmd_neu_berechnen)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
