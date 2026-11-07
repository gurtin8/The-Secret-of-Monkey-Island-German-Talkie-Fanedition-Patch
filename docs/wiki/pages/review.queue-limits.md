# Warum die Warteliste gesperrt ist

Prüfe Platz, laufende Aufträge und Voraussetzungen, wenn sich die Warteliste nicht starten lässt.

Die optionale Warteliste wird nur in dafür eingerichteten Umgebungen mit höchstens 8 GiB Arbeitsspeicher angeboten. Sie fasst acht Dialoge. Ist sie voll, entferne einen Eintrag, bevor du den nächsten hinzufügst.

Läuft bereits ein Auftrag zur Spracherzeugung, warte auf seinen Abschluss: In dieser Zeit kannst du die Warteliste weder ändern noch starten. Für den Start muss die Spracherzeugung verfügbar sein. Bei CPU-Versuchen brauchst du außerdem die [Bestätigung an jedem betroffenen Dialog](review.backend.md).

Nach dem Lauf verschwinden erfolgreiche Einträge aus der Liste. Bleibt ein Problem, lies die Meldung am jeweiligen Dialog; sie zeigt dir, was vor einem weiteren Versuch fehlt.

## Weiterführende Hilfe

- [Mehrere Vorschauen nacheinander erzeugen](review.queue.md)
- [Den Rendermodus für einen Dialog wählen](review.backend.md)
- [Bei einer Fehlermeldung weiterarbeiten](review.messages.md)
