# Beide Kameras montieren

## Teile und zusätzliche Hardware

Rechts: Wippe **05**, fester Halter **12**, Einstelllehre **13**. Links: Wippe **14**, fester Halter **15**, Einstelllehre **16**. Die Lehren und deren Schrauben werden nach dem Einstellen entfernt.

Pro Kamera: drei M2×8 mit drei M2-Muttern für das Original-28×28-mm-Lochbild; zwei M3×8-Senkschrauben für den festen Halter; eine M3×10-Senkschraube als Pivot; eine Metall-Anlaufscheibe 0,5 mm, Ø7/Ø3,2; eine M3×16-Zylinderkopfschraube als Feinsteller. Zum temporären Befestigen der Lehre eine weitere M3×16-Zylinderkopfschraube. Die modellierten Einsatzaufnahmen entsprechen den bisherigen V4-Parametern; eigene Einsätze und reale Gewindelängen abgleichen.

Für beide Kameras zusammen sind sechs M2-Schrauben/Muttern vorgesehen. Die **innere untere Ecke** bekommt keine Schraube: An der neuen vorderen Eckauflage anlegen, dann die anderen drei Originalbohrungen verschrauben. Nicht durch die Platine bohren. Nur leicht anziehen und Bauteile/Lötstellen prüfen.

## Einbau und Einstellung

1. Bei stromlosem Gerät Kameraplatinen in die Wippen setzen, **J1 bei beiden unten**. Es werden zwei gleiche, aufrechte Kameras verwendet. Die Öffnung rechts/links ist aus Sicht auf die Gehäusefront bezeichnet.
2. Feste Halter 12/15 an den kurzen seitlichen Rückschalenaufnahmen befestigen. Wippen mit Scheibe und M3-Pivot montieren; die Feinsteller von unten einsetzen. Die Spitze stützt die hintere Lasche.
3. **Beide Kameras für die Displaymontage auf −1° stellen.** Displayeinheit gerade von vorne einsetzen und 2 mm nach oben einhängen. Danach ist der ganze Betriebswinkelbereich frei.
4. Sensorfront zum Einstellen abgenommen lassen. Die Handy-Lehren einzeln benutzen; nie mit beiden Lehren und zwei Handys gleichzeitig arbeiten.
5. Rechts Lehre 13 befestigen: 2,5-mm-L-Schlüssel mit etwa 9 mm kurzem Schenkel, langer Schenkel nach unten. Links Lehre 16 **vor Anschluss der beiden Pi-USB-Kabel** befestigen. Falls bereits angeschlossen, diese beiden Stecker samt Kabelenden vorübergehend herausnehmen. Die fest verlöteten Pi-Anschlüsse bleiben vollständig eingebaut. Links passt ein etwa 15 mm kurzer Schenkel mit 60 mm langem Schenkel nach vorne; der TI-USB-Kabelraum bleibt erhalten.
6. Handy auf die zugängliche 30×20-mm-Referenz legen, Gerät auf seiner Standfläche waagerecht ausrichten. Jeweils mit dem Feinsteller −3,5° einstellen. Etwa eine halbe Umdrehung entspricht 0,5°; maßgeblich ist die gemessene Neigung. Anschläge begrenzen −6…−1°, die Strichmarken kennzeichnen die Nennlage.
7. Zum linken Feinsteller den langen Schenkel eines rechtwinkligen L-Schlüssels waagerecht nach hinten richten; geprüft sind **20 mm gerader kurzer Schenkel und 50 mm langer Schenkel**. Rechts zeigt der lange Schenkel nach innen. Die Werkzeuge bleiben nur während des Einstellens im Gehäuse.
8. Beide Pivot-Schrauben durch die seitlichen Zugangsöffnungen mit 2-mm-Schlüssel festklemmen, Winkel erneut prüfen. **Lehren 13/16 und deren Schrauben entfernen**, erst dann die Pi-USB-Kabel wieder anschließen. Reale Klemmwirkung gegen Transport/Stativvibration testen; kein ungeprüftes Drehmoment vorgegeben.
9. Sensorfront gerade einsetzen, 2 mm nach oben einhängen und unten verriegeln. Objektivfrontmitten sind nominal bündig. Geprüfter Wechselraum: Ø14,4 mm und 80 mm axial nach vorne. Fokus und Objektiv sind ohne Ausbau der Wippe erreichbar; größere/längere M12-Objektive separat prüfen.

## Zwei CSI-Kabel

Beide Flachbandkabel treten hinten aus J1 aus und laufen durch die unteren Wippenöffnungen nach hinten/unten. Modellierte Reserve: **16 mm Breite, 0,6 mm Dicke, R6 am ersten Bogen**. Bewegungsreserve im gesamten Neigungsbereich lassen.

Rechts führt der freie untere Korridor bei etwa X36/Y−43/Z82 mm nach innen (58×4×12 mm). Links zunächst in der seitlichen Lücke bei X−63,15 bleiben, unter die Kamera gehen und **vor** den Pi-IO-Körpern zum Korridor X−36/Y−33/Z80 mm führen (58×4×8 mm). Der Übergang zum linken Korridor wurde für drei Neigungen gegen die Einbauhüllen geprüft; die feste Netzwerkbuchse wurde dabei nie entfernt. Die beiden Kabel sollen auf getrennten Wegen liegen und sich nicht gegenseitig knicken.

Danach an die jeweiligen freien CAM/DISP-Anschlüsse des Pi führen. Die endgültigen Pi-seitigen Stecker, Kabellängen, Steckorientierungen und zulässigen Biegeradien sind nicht exakt modelliert: am echten Aufbau abgleichen und an beiden Enden zugentlasten. Keine Kabel vor TI-/OPS-Antennen verlegen, keine Spannung auf J1. Das Anschließen aktiviert keinen zweiten Softwarekanal; dieser Schritt ist separat nötig.
