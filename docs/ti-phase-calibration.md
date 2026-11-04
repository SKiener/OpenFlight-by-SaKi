# TI-Seitenwinkel: Phasennullpunkt mit Eigenbau-Reflektor kalibrieren

Stand 4. Oktober 2026. Werkzeug und Ablauf sind **nur mit synthetischen Daten
getestet**, nicht am Gerät. Erst wenn das Skript „ok“ meldet und eine
Vergleichssession das bestätigt, gehört ein Wert auf den Pi.

## Worum es geht

Der TI-Seitenwinkel kommt aus der Phase von TX2 gegenüber TX1/TX3. Diese
Phase enthält einen festen elektrischen Nullpunkt φ0, der bisher nicht gemessen
ist (`--iwr6843-horizontal-phase-reference-rad` fehlt im Launcher). Die
Range-Daten vom 3. Oktober passen zu φ0 ≈ −0,3…−0,55 rad, also einem
konstanten Versatz von etwa +5…+10°. Ein ruhender Reflektor an bekannter
Stelle misst φ0 direkt – ohne Toptracer als Fit-Eingang.

Zweiter, davon getrennter Effekt: Ohne Zusatzoption meldet OpenFlight die
**Peilrichtung** vom Radar zum Ball, nicht die Startrichtung. Weil das Radar
1,5 m hinter dem Ball steht, ist sie nur etwa halb so groß (Steigung ≈ 0,5).
Das behebt erst `--iwr6843-horizontal-trajectory-fit` (siehe unten).

## 1. Reflektor bauen (ca. 20 Minuten)

- Drei gleich große, **quadratische** Kartonplatten, Kantenlänge 15–20 cm,
  möglichst steifer Karton (Wellpappe doppelt geklebt).
- Jede Platte mit Küchen-Alufolie bekleben: glatt streichen, keine Falten oder
  Beulen, Folie vollflächig mit Sprühkleber oder doppelseitigem Klebeband.
  Glänzende oder matte Seite ist egal.
- Die drei Platten zu einer **Würfelecke** verkleben (wie die Innenecke eines
  Kartons): alle drei Winkel 90°, mit Geodreieck oder Buchecke prüfen. Kanten
  innen mit Klebeband sichern; Folie an den Kanten darf sich berühren.
- Die offene Seite zeigt später zum Radar, die Raumdiagonale der Ecke
  ungefähr auf das Radar.

## 2. Aufstellen

- **Entfernungen:** Das Aufnahmeprofil speichert nur Entfernungsbereiche bis
  etwa 4,6 m. 5 m oder 8 m sind damit **nicht messbar** (das Skript bricht mit
  einer klaren Meldung ab). Geeignet sind:
  - **2,5 m** und **3,2 m** (in allen 24 Frames enthalten) – Pflicht,
  - optional **4,2 m** (nur in den 8 späten Frames).
- Gemessen wird schräg von der Mitte der TI-Antenne bis zur Innenecke des
  Reflektors.
- **Seitlich:** auf die Ziellinie (die Linie durch den Ball). Den seitlichen
  Abstand dieser Linie zur Mitte der TI-Antenne messen; Vorzeichen: **positiv,
  wenn die Linie von hinten gesehen rechts der TI-Antenne liegt**. Bei diesem
  Aufbau läuft die Ziellinie genau durch die Mitte der TI-Antenne (seitlich
  0 m, gemessen am 2026-10-11). Nach jedem Umbau neu nachmessen.
- **Höhe:** Innenecke 0,3–0,6 m über dem Boden, auf einem Karton oder
  Holzhocker (kein Metall). Direkt am Boden stört die Bodenspiegelung.
- Umgebung: 1 m um den Reflektor kein Metall (Taschen, Schläger, Ballkorb,
  Zaun), während der Aufnahme niemand im Bereich zwischen Radar und Reflektor.
  Reflektor fest hinstellen, er darf nicht wackeln.

## 3. Aufnehmen

Die laufende App verwirft Auslöser ohne Ball und speichert dann keine TI-Datei.
Für die Reflektor-Aufnahme deshalb die App sauber beenden (Kiosk schließen bzw.
`curl -X POST http://127.0.0.1:8080/api/shutdown` und warten, bis kein
`openflight-server` mehr läuft) und das vorhandene Kalibrierskript benutzen, das
jede TI-Aufnahme speichert. Die Geometriewerte müssen zu `~/run-openflight.sh`
passen (Radarhöhe seit 04.10.2026 gemessen: 0,132 m):

```bash
cd ~/openflight
uv run python scripts/iwr6843/calibrate.py --debug --no-tilt-sweep --shots 1000 \
  --ops-port /dev/ttyAMA0 \
  --iwr6843-port /dev/serial/by-id/usb-Silicon_Labs_CP2105_Dual_USB_to_UART_Bridge_Controller_01311659-if00-port0 \
  --cfg config/iwr6843_l3dump_wide_24f3ms_53bin_iq16.cfg \
  --tee-m 1.50 --tilt-deg 10 --radar-height-m 0.132 --ball-height-m 0.02135 \
  --outdir ~/openflight_sessions/ti_reflektor/2_5m
```

- Seitlich hinter dem Radar stehen und **in die Hände klatschen**. Jeder
  Klatscher erzeugt eine Datei unter `.../2_5m/iwr6843/*.l3dump`. Nach jedem
  Klatscher die Meldung `Capture #n complete` abwarten (ca. 7 s).
- 5 Aufnahmen je Entfernung. Dann mit Strg+C beenden, Reflektor versetzen und
  mit neuem `--outdir` (z. B. `.../3_2m`) wiederholen.
- Optional: eine zweite Höhe an derselben Entfernung als eigene Gruppe
  aufnehmen. Weicht φ0 dort ab, stimmt die angenommene TX2-Lage nicht exakt.
- Danach die App wie gewohnt wieder starten.

## 4. Auswerten

Auf dem Pi oder nach Kopieren der Dateien auf den Mac:

```bash
uv run python scripts/iwr6843/horizontal_phase_cal.py \
  --position 2.5 ~/openflight_sessions/ti_reflektor/2_5m/iwr6843/*.l3dump \
  --position 3.2 ~/openflight_sessions/ti_reflektor/3_2m/iwr6843/*.l3dump \
  --reflector-lateral-m 0.0 \
  --output ~/openflight_sessions/ti_reflektor/ergebnis.json
```

`0.0`, weil der Ball bei diesem Aufbau genau vor der TI-Antenne liegt
(gemessen 2026-10-11). Der frühere Wert −0,072 m war nur aus der
R2-Halterung geschätzt.

Das Skript liest die Rohdaten **vor** der Bewegtziel-Filterung (die den
ruhenden Reflektor sonst löscht), sucht den Reflektor ±0,3 m um die
angegebene Entfernung und misst die TX2-Phase je Frame. Ausgabe:

| Feld | Bedeutung |
| --- | --- |
| `status` | `ok`, `warning` (z. B. nur eine Entfernung) oder `refused` |
| `recommended_horizontal_phase_reference_rad` | Kandidat für den Launcher |
| `angle_shift_deg` | So viel verschiebt sich jeder Seitenwinkel |
| `positions[].circular_std_rad` | Streuung je Entfernung, Grenze 0,10 rad |
| `position_spread_rad` | Unterschied der Entfernungen, Grenze 0,10 rad |
| `positions[].dumps[].dominance_db` | Reflektor gegen stärkstes anderes Ziel, ≥ 10 dB |

`refused` (Exit-Code 2) bedeutet: **keinen Wert eintragen.** Häufige Ursachen
sind Metall in der Nähe, Bewegung im Strahl, ein wackelnder Reflektor oder
falsch angegebene Entfernung/Seitenlage.

## 5. Eintragen

Nur bei `ok`: in `~/run-openflight.sh` unter `--iwr6843-tilt-deg 10` ergänzen

```bash
    # TX2-Phasennullpunkt, Reflektormessung <Datum>, siehe ergebnis.json
    --iwr6843-horizontal-phase-reference-rad -0.4321
```

und die App neu starten. Der Wert gilt für genau diesen Aufbau; nach
Umbau der Halterung oder Drehen der Platine neu messen.

**Stand 2026-10-11:** Die Reflektormessung vom 2026-10-09 (2,5 m, drei
Dumps, Phasenstreuung 0,0018 rad) wurde mit `--reflector-lateral-m 0.0` neu
ausgewertet: −0,1819 rad statt −0,3327 rad (mit −0,12 m), Winkelverschiebung
−3,32° statt −6,08°. Aktiv sind jetzt:

```bash
    --iwr6843-horizontal-phase-reference-rad -0.1819
    --iwr6843-azimuth-offset-deg -7.7
    --iwr6843-tee-lateral-m 0.0
```

Der Versatz −7,7° ist an die Kamera angeglichen: Mittel aus Kamera minus TI
über 8 Range-Schläge am 2026-10-11, Streuung 2,2°. Der TI liest fliegende
Bälle also etwa 7,7° weiter rechts als den ruhenden Reflektor. Die Ursache
ist noch offen. Die Trajektorien-Option bleibt aus: auch mit der korrigierten
Geometrie liegt sie im Mittel −14,6° neben der Kamera (Streuung 4,1°).

## 6. Prüfen

Vergleichssession mit Toptracer (gemischte Schläge, bewusst auch links/rechts),
dann lineare Ausgleichsgerade TI = a + b · Toptracer:

- **Mit Nullpunkt, ohne Trajektorien-Option:** der Versatz `a` sollte gegen 0
  gehen (bisher ca. +6,8°). Die Steigung `b` bleibt bei etwa 0,5 – das ist die
  Peilrichtung, kein Fehler des Nullpunkts.
- Toptracer dient nur der Bewertung. Den Nullpunkt nie aus Toptracer-Werten
  nachjustieren.

## 7. Trajektorien-Option (`--iwr6843-horizontal-trajectory-fit`)

Rechnet Entfernung und Peilung je Frame in eine seitliche Ballbahn um und
bestimmt die Startrichtung als Steigung durch den Ballort. Ohne gemessenen
Nullpunkt ist das Ergebnis verzerrt; die App warnt dann im Log.
Ballort seitlich: `--iwr6843-tee-lateral-m` (gleiche Messung und gleiches
Vorzeichen wie oben, Standard −0,072 m).

Offline-Prüfung mit den Aufnahmen vom 3. Oktober (36 Paare, Nullpunkt jeweils
aus der **anderen** Serie ohne Toptracer geschätzt – Entwicklungsdaten, keine
Feldvalidierung): Steigung 0,47 → 0,85, Versatz −2,5° → −2,0°, MAE 2,1° → 2,4°.
Der Versatz hängt stark von der angenommenen Seitenlage des Balls ab
(±72 mm ≈ ±3°).

Einschalten erst, wenn (1) der Reflektor-Nullpunkt eingetragen ist und (2)
eine Vergleichssession mit Option eine Steigung von etwa 1 (0,85–1,15) und
einen Versatz unter ±1,5° zeigt. Im Session-Log steht je Schlag
`horizontal_source` (`hlcmf_v1_trajectory_fit` oder Rückfall
`hlcmf_v1_bearing` mit `horizontal_trajectory_status` und halbierter Konfidenz).

## Rückweg

Die beiden Zeilen `--iwr6843-horizontal-phase-reference-rad …` bzw.
`--iwr6843-horizontal-trajectory-fit` aus `~/run-openflight.sh` löschen und die
App neu starten. Es wird nichts anderes verändert.
