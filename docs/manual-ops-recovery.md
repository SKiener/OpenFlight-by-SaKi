# OPS-Radar manuell wiederherstellen

Im deutschen Menü unter **Diagnose → Status → OPS-Radar wiederherstellen**.
Nur für den echten OPS243 im Mikrofon-/Sound-Trigger-Modus; nicht für Demo,
Mock oder Swing-Speed-/Speed-Trigger-Modus.

1. Erst die laufende Schlagverarbeitung abwarten. Während der Wiederherstellung
   nicht schlagen oder klatschen.
2. Den Button einmal antippen. Er ist bis zur Antwort gesperrt.
3. „Befehle gesendet“ bestätigt nur den Softwareablauf, noch keine Messung.
   Einmal klatschen und anschließend einen Testschlag ausführen. Klatschen allein
   kann mangels Ballbewegung als Fehltrigger verworfen werden.
4. Bei Fehler Verbindung prüfen und erneut versuchen. Bei fehlender Serverantwort
   kann der Vorgang noch laufen. Verkabelung nur nach sicherem Herunterfahren und
   vollständiger Stromtrennung prüfen, einschließlich Akkuversorgung.

Die Wiederherstellung stoppt zuerst den bisherigen OPS-Lesethread. Solange er
noch arbeitet, werden keine Wiederherstellungsbefehle geschickt. Danach werden
die bestehende Abtastrate und der bestehende Pre-/Post-Trigger-Split erneut
gesetzt und der Puffer aktiviert. Es werden weder Moduswechsel (GS/GC) noch
Flash-Schreibbefehle (A!) gesendet. Eine gescheiterte Vorbereitung lässt den
OPS-Monitor angehalten; ein erneuter manueller Versuch ist möglich.
Die Ausgabe-Pufferprüfung ist bei der manuellen Vorbereitung zeitlich begrenzt;
ein hängender serieller Flush darf das Herunterfahren nicht endlos blockieren.

Session, Schlägerauswahl, Schlagnummern, Gate, TI-Dump-/Ablehnungs-Callbacks,
Kamera und Kalibrierung bleiben bestehen. Auch die aktuelle Sendeleistung bleibt
unverändert. Der Button ersetzt keinen Firmware-Fix und kann einen physisch
getrennten oder vollständig blockierten Radar nicht garantiert wiederbeleben.

Diese Änderung wurde selektiv nach dem Konzept von
[Upstream PR #312](https://github.com/open-flight/openflight/pull/312) umgesetzt,
nicht durch ein Zusammenführen des gesamten Upstream-Branches. Zusätzliche
Sicherung: deutsche Rückmeldungen, Erhalt unserer TI/Gate-Verbindungen,
Fehlerprüfung beim Re-arm, serialisierte Bedienbefehle und Wiederholbarkeit
nach fehlgeschlagener Vorbereitung. Kein automatischer Watchdog wurde ergänzt.

## Prüfstand vom 24.10.2026

- 21 neue Backend-Fälle einschließlich echter Socket.IO-Testclients; abschließende
  Radar-/Server-/Gate-Regression: 594 bestanden, 1 übersprungen.
- UI: 1.263 Tests bestanden; Produktionsbuild und Bundle-Prüfung bestanden.
- Chrome, 1024 × 600: 3 Tests bestanden (Hell/Dunkel, Mehrfachtipp-Sperre,
  Fehlerrückmeldung, erneuter Versuch, Zurücksetzen abgewiesener Regleränderungen).
- Unabhängiges Code-Review nach Korrekturen ohne offene Befunde.
- Vollständiger Python-Lauf vor der letzten Änderung der Antwortadressierung:
  3.248 bestanden, 12 übersprungen, 1 bereits vorhandener Fehler in
  `test_startup_splash_asset_has_branding_redirect_and_failure_ui` (erwartet das
  alte `openflightlogo.svg`). Startbildschirm und dieser Test wurden nicht geändert.
- Ruff und ESLint für die geänderten Dateien sowie `git diff --check` bestanden.
  Der Gesamtlint meldet weiterhin zwei vorhandene Fast-Refresh-Fehler in den
  Fixtures `automatic-replay.tsx` und `camera-layout.tsx`. Pylint meldet weiterhin
  Typinferenzfehler in unveränderten Serverpfaden (Shot-Löschen und Mock-Start).

Kein Hardwaretest und keine Installation: Der direkte SSH-Versuch an
`openflight.local` meldete „Host is down“. Vor dem Aufspielen den aktuellen
Pi-Branch und lokale Änderungen erneut prüfen und sichern. Nur diese Ergänzung
übernehmen; weder einen gesamten Checkout ersetzen noch Kalibrierung, Launcher
oder Hardware-Konfiguration überschreiben. Der lokale Stand liegt in
`openflight-fixes/wt-range2`, Branch `feat/range-stage2`, noch uncommittet.
