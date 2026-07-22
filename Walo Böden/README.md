# Walo Böden – Inventar

Inventarsystem für **Artikelstammdaten, Bestände und Buchungen**
(Stufe 1 + 2 des [Konzepts](./KONZEPT.md)). Python + SQLite, keine Installation
außer Python 3 nötig.

## Prinzip (Stufe 2)

Der Bestand wird **nicht mehr direkt gesetzt**, sondern ergibt sich aus
**Buchungen**:

- **Zugang** (Wareneingang) → Bestand +
- **Abgang** (Warenausgang) → Bestand −
- **Korrektur** (Inventur) → Bestand auf Zielwert
- **Anfangsbestand** → beim Anlegen automatisch gebucht

Jede Bewegung landet nachvollziehbar im **Journal**. Der Bestand ist der
laufende Saldo aller Buchungen und lässt sich jederzeit daraus neu berechnen.

## Schnellstart

```bash
cd "Walo Böden"

python3 inventar.py init                                    # einmalig
python3 inventar.py add --nummer A-100 --bezeichnung "Parkett Eiche" \
    --einheit m2 --bestand 50 --mindestbestand 10
python3 inventar.py zugang A-100 20 --beleg LS-2026-001     # Wareneingang
python3 inventar.py abgang A-100 8  --beleg AB-4711         # Warenausgang
python3 inventar.py journal A-100                           # Bewegungen ansehen
python3 inventar.py list                                    # Bestandsübersicht
```

## Befehle

### Stammdaten

| Befehl | Zweck |
|---|---|
| `init` | Datenbank/Tabellen anlegen |
| `add` | Artikel anlegen (`--nummer`, `--bezeichnung` Pflicht; `--bestand` = Anfangsbestand) |
| `list` | Artikel auflisten (`--suche TEXT`, `--alle` inkl. inaktive) |
| `show NUMMER` | Details eines Artikels |
| `edit NUMMER` | Stammdaten ändern (`--bezeichnung`, `--einheit`, …) |
| `deactivate NUMMER` / `activate NUMMER` | Artikel deaktivieren/aktivieren |

### Buchungen

| Befehl | Zweck |
|---|---|
| `zugang NUMMER MENGE` | Wareneingang buchen (`--beleg`, `--preis`, `--bemerkung`) |
| `abgang NUMMER MENGE` | Warenausgang buchen; blockiert negativen Bestand (`--erlaube-negativ` erzwingt) |
| `korrektur NUMMER ZIELBESTAND` | Bestand per Korrektur auf Zielwert setzen (Inventur) |
| `journal [NUMMER]` | Buchungen anzeigen (`--limit N`) |
| `neu-berechnen` | Bestände aus dem Journal neu berechnen (Prüfung/Reparatur) |

Hilfe zu jedem Befehl: `python3 inventar.py <befehl> --help`

## Daten

Alle Daten liegen in `inventar.db` (SQLite) im selben Ordner. Diese Datei wird
**nicht** ins Git eingecheckt (siehe `.gitignore`) und bleibt lokal.

## Nächste Ausbaustufen

- **Stufe 3:** Bestandsbewertung (FIFO/Durchschnitt), Lagerwert (Preis ist im
  Journal bereits erfassbar).
- **Stufe 4:** Auswertungen/Export (Mindestbestand-Report, Inventurliste, CSV/PDF).

Siehe [KONZEPT.md, Abschnitt 6](./KONZEPT.md).
