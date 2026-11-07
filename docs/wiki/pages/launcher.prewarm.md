# Was der Prewarm praktisch bewirkt

Wenn die deutsche Aufnahme einen englischen Akzent hat, lohnt sich ein Versuch mit eingeschaltetem Prewarm.

Ein Prewarm gibt der KI vor dem Dialog ein kurzes Beispiel für deutsche Sprache. Der Patch erzeugt dieses Beispiel selbst, damit du keine zusätzliche Aufnahme dafür vorbereiten musst. Im fertigen Spiel wird es nicht als weiterer Satz abgespielt.

Um seine Wirkung zu hören, schaltest du in der Review den Prewarm ein und erzeugst zunächst eine Vorschau mit demselben [Seed](review.seed.md). So vergleichst du die Aussprache bei möglichst unveränderten übrigen Einstellungen. Bleibt der Akzent, kannst du den Prewarm mit einem eingeschalteten [Stimmanker](review.anchor.md) und anschließend einem neuen Seed kombinieren. Den genauen Ablauf findest du in der [Aussprachehilfe](review.pronunciation.md).

Bei der ersten Vorbereitung kann die Konsole dafür GPU-Arbeit melden, obwohl du später auf der ANE erzeugen lässt. Das gehört zur Stimmvorbereitung. Das ebenfalls angezeigte „Modell aufwärmen“ meint das Bereitmachen des Rechenmodells und hat eine andere Aufgabe als dieses Sprachbeispiel.

## Weiterführende Hilfe

- [Stimmanker, Prewarm, ID und andere Begriffe](wiki.glossary.md)
- [Prewarm für einen Versuch ändern](review.prewarm.md)
- [Ein Prewarm ist zu kurz oder zu lang](launcher.prewarm-error.md)
