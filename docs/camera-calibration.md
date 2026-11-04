# Kamera kalibrieren (Objektiv und Einbaulage)

Ohne Kalibrierung schätzt OpenFlight die Brennweite aus dem unscharfen
Balldurchmesser und nimmt die Bildmitte als optische Achse an. Die drei
Schritte unten ersetzen das durch gemessene Werte. Alle neuen Optionen sind
abschaltbar; ohne sie rechnet die Software wie bisher.

## 1. Schachbrett vorbereiten

- Muster mit **10 × 7 Feldern (9 × 6 innere Ecken)**, Feldgröße **25 mm**,
  auf A4 quer drucken, Druckdialog auf 100 % bzw. „Tatsächliche Größe“
  (nicht „An Seite anpassen“).
- Nachmessen: 10 Felder müssen genau 250 mm ergeben, 7 Felder 175 mm. Andere
  Maße bei `solve` mit `--square-mm` angeben (z. B. A3 mit 40-mm-Feldern).
- Nicht aus zwei Blättern zusammenkleben: Versatz oder Wölbung an der Naht
  verfälscht die Eckpositionen.
- Vollflächig und blasenfrei auf eine starre, ebene Platte kleben (Forex,
  Alu-Verbund, Holz). Matt, ohne Spiegelungen. Rundherum weißer Rand.

## 2. Objektiv kalibrieren (auf dem Pi)

Voraussetzungen: OpenFlight läuft mit `--camera-capture` im aktiven Modus
**640 × 400 (full-300)**, mit derselben Drehung/Spiegelung/ScalerCrop wie im
Betrieb und **ohne** `--camera-capture-roll-deg` (die Vorschau wäre sonst
gedreht). Fokus und Objektiv danach nicht mehr verändern; nach jeder Änderung
an Objektiv, Fokus oder Kameramodus neu kalibrieren.

Ansichten aufnehmen (das Skript speichert nur Bilder, auf denen das ganze
Brett erkannt wird, und wartet 3 s für die nächste Position):

```bash
cd ~/openflight
UV_PYTHON=/usr/bin/python3 uv run --no-sync python \
  scripts/hardware-test/calibrate_camera_intrinsics.py grab \
  --out ~/openflight_calibration/views --count 25
```

- 25–30 Ansichten in **0,6–1,0 m** Abstand; eher nah, solange das Bild
  scharf bleibt und das ganze Brett mit weißem Rand im Bild ist.
- Das Brett nacheinander in alle Bildbereiche halten, besonders in die
  **Ecken und Ränder**, nicht nur in die Mitte.
- **Mindestens 8–10 Ansichten deutlich schräg (30–45°)**: abwechselnd nach
  links und rechts gedreht sowie oben nach hinten und nach vorn gekippt.
  Frontale Ansichten allein bestimmen die Brennweite nicht. Sie passen mit
  einer falschen Brennweite genauso gut (kleiner RMS), das Ergebnis ist
  trotzdem falsch. Beim ersten Versuch am Gerät waren die Ansichten im Median
  nur 7° geneigt, und eine einzelne Ansicht verschob die Brennweite um 14 %.
- Brett während jeder Aufnahme ruhig halten; gleichmäßiges Licht.

Danach eine beliebige Kameraaufnahme im selben Modus auslösen (z. B. Klatschen)
und deren `metadata.json` als Modusnachweis angeben:

```bash
UV_PYTHON=/usr/bin/python3 uv run --no-sync python \
  scripts/hardware-test/calibrate_camera_intrinsics.py solve \
  ~/openflight_calibration/views \
  --square-mm 25 \
  --capture-metadata ~/openflight_sessions/range/camera/<neueste Aufnahme>/metadata.json \
  --out ~/.config/openflight/camera-intrinsics.json
```

Alternativ können Aufnahmeordner (`camera_*`) direkt übergeben werden; sie
bringen ihre `metadata.json` selbst mit. Immer abgelehnt werden weniger als
10 brauchbare Ansichten und widersprüchliche Kameramodi; ohne `--force`
außerdem RMS über 1,0 px, unter 15 Ansichten, unter 75 % Bildabdeckung und
eine **unsichere Brennweite**: Unsicherheit über 1 %, oder eine einzelne
Ansicht verschiebt sie beim Weglassen um mehr als 2 %. Die Meldung nennt dann
die Zahl der Ansichten mit mindestens 30° Neigung. Ziel: **RMS unter 0,5 px**.

## 3. Einbaulage mit dem Maßband messen

Bezugspunkte: **Radar-Mitte** = Mitte der TI-Antennenfläche (derselbe Punkt
wie bei `--iwr6843-radar-height-m`), **Kamera** = Mitte der
Objektiv-Frontlinse. Genauigkeit etwa ±2 mm; 1 cm seitlich entspricht bei
1,5 m rund 0,4°.

| Maß | Messung | Option |
| --- | --- | --- |
| Kamerahöhe | Schlagfläche (wo der Ball liegt) senkrecht bis Linsenmitte | `--camera-capture-mount-height-m` |
| Seitlich | quer zur Ziellinie, Linsenmitte gegenüber Radar-Mitte; + = rechts (hinter dem Gerät Richtung Ziel gesehen) | `--camera-capture-lateral-offset-m` |
| Vor/zurück | entlang der Ziellinie, Linsenmitte gegenüber Antennenfläche; + = Kamera näher am Ziel | `--camera-capture-forward-offset-m` |

Die Kamerahöhe steht bisher auf dem Standard 0,20955 m (8,25 in) und wurde
nie gemessen. Für `--camera-capture-yaw-from-ball` muss der Ball **genau auf
der Mittellinie** vor der Radar-Mitte liegen (Abstand `--iwr6843-tee-m`);
die Stelle markieren. Die Kamera-Gierung wird dann aus dem liegenden Ball
bestimmt statt eine exakt parallel ausgerichtete Kamera anzunehmen.

## 4. Startskript ergänzen

In `~/run-openflight.sh` innerhalb von `openflight_args` nach
`--camera-capture` eintragen (Werte durch die Messungen ersetzen):

```bash
    --camera-intrinsics /home/pi/.config/openflight/camera-intrinsics.json
    --camera-capture-mount-height-m 0.xxx
    --camera-capture-lateral-offset-m 0.xxx
    --camera-capture-forward-offset-m 0.xxx
    --camera-capture-yaw-from-ball
```

`--camera-capture-horizontal-offset-deg` bleibt weg bzw. 0. Beim Start steht
im Log `Camera intrinsics: fx=… (RMS … px)`. Die Meldung `Camera intrinsics
not applied, mode differs` bedeutet, dass der Kameramodus nicht mehr zur
Kalibrierung passt.

## 5. Prüfen

Eine Vergleichssession mit Toptracer schlagen: mindestens 30 Schläge,
gemischte Schläger, bewusst auch nach links und rechts. Zuordnung wie bisher
über Schläger, Reihenfolge und Ballgeschwindigkeit, nie über Winkel.

Beurteilt wird je Achse die **lineare Ausgleichsgerade
Kamera = a · Toptracer + b**: Ziel **a ≈ 1** (0,95–1,05) und **b ≈ 0**
(|b| < 1°). Stand ohne Kalibrierung (36 Paare): seitlich a ≈ 0,72,
b ≈ +1,7°; vertikal a ≈ 1,10, b ≈ −2,4°. Der Median-Fehler allein ist kein
Maßstab, weil sich Steigungs- und Offsetfehler darin gegenseitig verdecken.
Nichts an Toptracer anpassen (keine Offsets oder Skalierungen eintragen);
bei Abweichung Maße und Kalibrierung prüfen.

Hinweis: Die Kamera rechnet mit den Belichtungszeitstempeln des Sensors. Den
Abflugzeitpunkt rechnet sie aus der Ballspur zurück: Bilder, in denen der Ball
schon deutlich vom liegenden Ball getrennt ist, werden bis zur Ruheposition
zurückverfolgt. Das gilt nur, wenn es zum Schall passt (Trigger minus Laufzeit
über `--iwr6843-tee-m` bei 343 m/s, bei 1,5 m etwa 4,4 ms; höchstens 5 ms
früher oder 1,5 ms später), sonst gilt diese Schallschätzung, nie ein Zeitpunkt
nach dem Trigger. Quelle und alle Kandidaten stehen im Sessionlog unter
`experimental_camera_time_zero`. In der Nachrechnung der 41 Archivschläge (ohne
Kalibrierung) kam der Zeitpunkt 38-mal aus der Ballspur, im Median 1,0 ms vor
der Schallschätzung; der vertikale Kamerawinkel liegt wieder auf dem Stand vor
der Zeitstempel-Umstellung. Bestätigt ist das erst durch die Vergleichssession
aus Schritt 5 mit kalibrierter Kamera.
