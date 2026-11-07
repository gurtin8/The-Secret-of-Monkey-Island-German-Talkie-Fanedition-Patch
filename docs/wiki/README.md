# Dein Weg mit dem MI German Talkie Patch

Mit dem Patch bereitest du deine Spielkopie vor, gibst den Figuren ihre Stimmen und stellst daraus deine deutsche Sprachfassung zusammen. Die Anleitung [Vom ersten Start zum spielbaren Monkey Island](pages/wiki.start.md) begleitet dich durch die sechs Schritte des Patchs und zeigt dir anschließend, wie du den fertigen Ordner in ScummVM spielst.

Der Patch bereitet das Audio für die Spielbasis direkt auf dem Mac auf. Bei der Spracherzeugung passt GPU zu den derzeit abgestimmten Aussprachekorrekturen. ANE+GPU erzeugt die Aufnahmen am schnellsten, spricht aber etwas anders und braucht noch eigene Korrekturen. Die [Erklärung der Rendermodi](pages/launcher.render-mode.md) hilft dir bei der Wahl.

## Die passende Hilfe finden

Wenn du an einer bestimmten Stelle weiterkommen möchtest, kannst du hier direkt einsteigen:

- [Stimmanker, Prewarm, ID und andere Begriffe](pages/wiki.glossary.md)
- [Wie der Patch die Spielbasis vorbereitet](pages/launcher.audio-mode.md)
- [Stimmen wählen und Sprachausgabe erzeugen](pages/launcher.render-workflow.md)
- [Dialoge anhören und verbessern](pages/review.workflow.md)
- [Aussprache und Stimmklang gezielt verbessern](pages/review.pronunciation.md)
- [Gute Korrekturen mit anderen teilen](pages/review.recipe.md)
- [Deine spielbare Fassung erstellen](pages/review.build.md)
- [Alle Anleitungen und Einzelhilfen](pages/README.md)

## Beim Arbeiten nachschlagen

Öffne `site/index.html` im Browser, wenn du die Wiki mit Suche und Hilfedialogen lesen möchtest. Sie funktioniert lokal, auch ohne Internetverbindung. Die Hauptnavigation führt dich zu den zentralen Anleitungen; über die Suche, die Bereichswahl oder „Alle Einzelhilfen anzeigen“ findest du auch die kurzen Erklärungen zu Feldern und Tasten.

Im Patch erreichst du die passende Hilfe mit Shift+Klick auf ein Bedienelement. Benutzt du die Tastatur, wählst du eine Taste mit Tab aus und drückst F1 oder ?. Verlinkte Begriffe führen zu weiteren Erklärungen, und Escape bringt dich aus dem Hilfedialog zurück zur Arbeit.

## Stand dieser Fassung

Alle 125 Themen wurden am 19.10.2026 einzeln redaktionell durchgesehen. Der Ton der durchgehenden Anleitung gilt jetzt auch für Einzelhilfen, Statusanzeigen und Fehlererklärungen. Die [Aussprachehilfe](pages/review.pronunciation.md) bleibt der gemeinsame Anlaufpunkt für konkrete Hörversuche, damit die gleichen Versuchsfolgen nicht auf jeder Seite erneut erscheinen.

Die Hilfe in diesem Quellcodeexport gehört zu Version **0.26.10**. Die Quellen liegen in `content/` und `pages/`. Der [Wiki-Build](../../tools/wiki/build.py) prüft die mitgelieferte Fassung vom Quellcodeordner aus mit `python3 tools/wiki/build.py --check`.

Am 19.10.2026 wurden alle 125 Themen zusätzlich mit der aktuellen Bedienung abgeglichen. Die Anleitung zur Geschwindigkeitsmessung beschreibt nun den automatischen Ablauf und den Abbruch eines laufenden Tests; die zusätzliche Starttaste entfällt.
