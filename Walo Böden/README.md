# Walo Böden – Inventar

Kleines Inventarsystem für **Artikelstammdaten und Bestände** (Stufe 1 des
[Konzepts](./KONZEPT.md)). Python + SQLite, keine Installation nötig außer Python 3.

## Schnellstart

```bash
cd "Walo Böden"

# 1. Datenbank anlegen (einmalig)
python3 inventar.py init

# 2. Artikel anlegen
python3 inventar.py add --nummer A-100 --bezeichnung "Parkett Eiche natur" \
    --einheit m2 --bestand 50 --mindestbestand 10

# 3. Übersicht anzeigen
python3 inventar.py list
```

## Befehle

| Befehl | Zweck |
|---|---|
| `init` | Datenbank/Tabelle anlegen |
| `add` | Artikel anlegen (`--nummer`, `--bezeichnung` Pflicht) |
| `list` | Artikel auflisten (`--suche TEXT`, `--alle` inkl. inaktive) |
| `show NUMMER` | Details eines Artikels |
| `edit NUMMER` | Stammdaten ändern (`--bezeichnung`, `--einheit`, …) |
| `set-bestand NUMMER MENGE` | Bestand direkt setzen |
| `deactivate NUMMER` | Artikel deaktivieren (statt löschen) |
| `activate NUMMER` | Artikel wieder aktivieren |

Hilfe zu jedem Befehl: `python3 inventar.py <befehl> --help`

## Daten

Alle Daten liegen in `inventar.db` (SQLite) im selben Ordner. Diese Datei wird
**nicht** ins Git eingecheckt (siehe `.gitignore`) – sie enthält deine echten
Bestandsdaten und bleibt lokal.

## Nächste Ausbaustufe

Aktuell wird der Bestand direkt gesetzt. In **Stufe 2** kommen Buchungen
(Wareneingang/-ausgang) hinzu, aus denen der Bestand berechnet wird – siehe
[KONZEPT.md, Abschnitt 6](./KONZEPT.md).
