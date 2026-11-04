# Noctua und Pi-Luftführung montieren

## Zusätzliche Teile

- Ein Noctua **NF-A4x20 5V PWM**,40×40×20 mm; mit Original-Gummipads22 mm.
- Gedruckte Rückschale01 aus F, Kanalhälften17 und18. Alle anderen E-Druckteile weiterverwenden.
- **Vier M3×35-Senkschrauben** mit sechsseitigem Innensechskantkopf, vier M3-Muttern für den Lüfterflansch.
- **Zwei M3×12-Senkschrauben** und zwei M3-Muttern für die Kanalhälften.
- Bei Bedarf dünnes weiches Dichtband, an die realen Spalte angepasst. Es reduziert Nebenluft; keine harte Distanz oder Druck auf Platinen erzeugen.

Schraubenlänge einschließlich Senkkopf. Die Modellhüllen verwenden etwaØ6,5-mm-Senkköpfe und2,4-mm-Muttern. Reale Schrauben-/Kopfhöhen abgleichen, damit die Köpfe vollständig versenkt sitzen. Die Lüfteraufnahme benötigt keine zusätzlichen Einschmelzeinsätze.

## Auf der Werkbank

1. Die beiden Kanalhälften17/18 säubern, auf freie Luftwege und Grate prüfen. Die gemeinsamen Trennflächen liegen in der Montage bei Z51 mm.
2. Zwei M3-Muttern von unten in die zugänglichen Sechskantführungen einsetzen, Kanalhälften zusammenlegen und mit zwei M3×12 von oben verschrauben. Diese Verbindung wird **außerhalb des Gehäuses** montiert. Nur so weit anziehen, dass beide Hälften satt anliegen.
3. Die vier Flanschmuttern von der Pi-Seite durch die offenen Sechskantführungen bis zu den vorderen Lastsitzen schieben. Die Führungen verhindern das Mitdrehen. Die Muttern werden später durch die äußeren Lüfterschrauben gehalten.
4. Noctua mit Original-Pads an den Flansch legen, **Luftrichtungspfeil auf dem Lüfter nach außen zur Gehäuseseite**. Die breite Sammleröffnung zeigt zum Pi. Der Kanal liegt in der Montage unter den Kameras und berührt den Pi-Kühler nicht.

## In der Rückschale

5. Pi-/Geekworm-Verbund kann bereits montiert sein. **Radarträger samt beiden Radaren und Sensorfront bleiben für diesen Einbau entfernt.** Kameraaufnahmen und die höheren Anschlüsse dürfen bleiben. Kabel für den Einbauweg beiseitehalten.
6. Lüfter und Luftführung zusammen von vorne einsetzen, zunächst leicht von der Seitenwand nach innen versetzt. Dann zur seitlichen Aufnahme führen. Im CAD sind62 Positionen dieses Wegs geprüft: frontaler Weg mit0,8 mm Versatz nach innen, anschließend seitliches Ansetzen um0,8 mm. Das ist eine Einbauhilfe, keine Aufforderung zum gewaltsamen Biegen.
7. Vier M3×35 von außen durch die versenkten Seitenlöcher, Lüfter und Flanschmuttern einschrauben. Über Kreuz vorsichtig anziehen. Der Flansch hat rund0,5 mm Reserve (im exportierten Modell0,49 mm) zu den Lüfterpads und setzt sich beim Anziehen an diese; dieser Weg ist im CAD freigeprüft. Nicht weiter anziehen, sobald Lüfter und Kanal sicher sitzen.
8. Der verstärkte Auslasskragen steht nominal0,6 mm von der ebenen Lüfter-Rahmenfläche entfernt; am inneren Flansch bleibt je nach Padbereich etwa0,5–1,5 mm. Falls nötig dort passend dünnes weiches Dichtband verwenden, ohne Lüfter oder Luftführung zu verschieben. Rotor, Schraubenlöcher und Öffnungen frei lassen.
9. Prüfen, dass keine Schraubenspitze oder Leitung in den Rotorraum ragt und alle Muttern sitzen. Kabel von Lüfter und Kameras außerhalb der Luftöffnung zugentlasten. Die Kunststoffführung darf Pi, UPS und Radarplatinen nicht belasten.
10. Radarträger/Radare montieren, Sensorfront einsetzen und verriegeln. Für spätere Wartung Luftführung und Lüfter nach Lösen der äußeren vier Schrauben als Einheit entnehmen; die innere Trennverbindung auf der Werkbank öffnen.

## Versorgung und Betrieb

Es handelt sich um die **5-V-Version** des Noctua. Der vorhandene Pi Active Cooler bleibt an seinem vorgesehenen Anschluss. Die zusätzliche Stromversorgung und gegebenenfalls PWM-Steuerung des Noctua wurden durch die CAD-Arbeit nicht eingerichtet; Steckerbelegung nach Noctua-Unterlagen prüfen. Keine12 V an den5-V-Lüfter anschließen.

Beide seitlichen Gitter offen halten. Das passive Gitter auf der Gegenseite lässt Frischluft nachströmen; die Luft verteilt sich um reale Stecker und Bauteile. Der Kanal sammelt Luft neben dem Pi-Kühler und führt sie zum Abluftlüfter. Beim ersten Betrieb mit geschlossenem Gehäuse und aktiven Kameras/Radaren CPU-Temperatur und Drosselung kontrollieren. Eine Temperaturverbesserung wurde noch nicht gemessen.
