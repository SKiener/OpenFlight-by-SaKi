# OpenFlight V4 · Dual Camera F · Noctua

Aktuelle Dual-Camera-E-Konstruktion mit seitlichem **Noctua NF-A4x20 5V PWM**, versenkter Vierpunktbefestigung, integriertem Schutzgitter und zweigeteilter Luftführung neben dem Pi Active Cooler. Die Gehäusegröße bleibt **196 × 100 × 232 mm**. Beide Kameras, ihre bündige Lage und Verstellung bleiben unverändert.

## Druckdateien

`STL/` enthält alle **18 druckorientierten Teile** in Millimetern. Gegenüber E nur **01_Rear_shell** ersetzen und **17_Noctua_airguide_lower / 18_Noctua_airguide_upper** ergänzen. Die anderen15 Druckteile bleiben geometrisch unverändert; tatsächlicher Dateivergleich unter `WIEDERVERWENDUNG.md`. Zum Drucken nicht die Einbaupositionen aus `CAD/` verwenden.

`OpenFlightCaseV4_DualCamera_F_Noctua.sc` ist die SolidCraft-Baugruppe. Sie besteht überwiegend aus geschlossenen Netzen; die ausgeblendete Gehäuseformstudie ist native CAD-Geometrie. `Quellen/` enthält die aktualisierten Generatoren. Frühere E-/D-Pakete bleiben unverändert erhalten.

## Lüfter und Luftweg

Lüfterachse quer zur Gerätebreite, Abluft nach außen auf der X+-Seite. Fan-Mittelpunkt XYZ=(82,2;−54;51) mm. Modelliert sind40×40×20 mm Rahmen und22 mm einschließlich Gummipads. Vier von außen versenkte M3-Schrauben halten Lüfter und Kanalflansch; verstärkte seitliche Aufnahmen verteilen die Last. Die Schraubenköpfe liegen innerhalb der bisherigen Außenkontur.

Der Pi-seitige Sammler beginnt neben der rechten Kühlerkante, führt unterhalb der Kameras zum Noctua und lässt den vorhandenen Active Cooler frei. Beide Kanalhälften werden separat gedruckt und mit zwei M3-Schrauben verbunden. Der Kanal ist geometrisch durchgängig; das kleinste geprüfte Querschnittsmaß entspricht609 mm² freier Fläche. Auf beiden Gehäuseseiten liegen passende gerundete Schlitzgitter mit jeweils548 mm² freier Öffnungsfläche. Die alten drei niedrigen Schlitze werden durch die neuen Gitter ersetzt.

Das ist eine zusätzliche Gehäuseentlüftung. Der Pi Active Cooler bleibt für die direkte Prozessorkühlung montiert. Die Öffnung am Kühler ist ein seitlicher Sammler mit Abstand, kein luftdicht angeschlossener CPU-Kühler. Luftstrom und Temperatur sind nicht simuliert oder gemessen; die Kühlwirkung mit realen Kabeln im geschlossenen Gehäuse prüfen.

## Montage und Prüfung

`MONTAGE_NOCTUA.md` beschreibt die zusätzlichen Schrauben, Muttern, Einstellreserve und Einbaureihenfolge. `MONTAGE_KAMERAS.md` bleibt für beide Kameras gültig. `Vorschau/` zeigt die tatsächliche CAD-Geometrie samt Schnitt. `Pruefberichte/` enthält Einbau-/Werkzeugwege, Kameraprüfung, unabhängigen Review und aktuelle Druckbedingungen.

Drucklagen und Stützen laut F-Druckbericht übernehmen. Bestehende E-Druckbedingungen gelten für die unveränderten Teile; keine pauschale Supportfrei-Zusage. Es gab **keine Änderungen am Pi, an Firmware, Stromversorgung oder Kalibrierung**. Die zweite Kamera wird dadurch nicht softwareseitig eingerichtet.
