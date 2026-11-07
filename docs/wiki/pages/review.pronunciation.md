# Aussprache und Stimmklang gezielt verbessern

Wähle den nächsten Hörversuch danach, ob Akzent, Stimmklang, einzelne Wörter oder die Betonung stören.

Höre zuerst die auffällige Stelle an und vergleiche sie mit dem gewünschten Text. Ändere für den ersten Vergleich möglichst nur eine Einstellung und nutze „Vorschau mit gleichem [Seed](review.seed.md)“. So lässt sich ihre Wirkung besser beurteilen. Wenn der einzelne Versuch nicht reicht, kannst du die Einstellungen anschließend bewusst kombinieren. Ist eine vorgeschlagene Kombination bereits eingestellt und klingt noch nicht gut, geh zum nächsten Versuch über. Entscheidend ist jeweils, ob sich die Aufnahme für dich verbessert.

## Deutscher Text klingt mit englischem Akzent

Stelle zuerst „Prewarm“ auf „An“ und lass „Stimmanker“ auf „Beibehalten“. Erzeuge mit „Vorschau mit gleichem Seed“ eine Aufnahme und höre, ob der deutsche Sprachklang besser passt. Der [Prewarm](review.prewarm.md) gibt der KI ein kurzes deutsches Sprachbeispiel mit.

Bleibt der Akzent, lass „Prewarm: An“ stehen und aktiviere zusätzlich „Stimmanker: An“. Vergleiche erneut mit gleichem Seed. Erst danach probierst du bei unveränderten Einstellungen „Vorschau mit neuem Seed“, um eine andere Sprechweise zu erhalten. Auch eine andere Referenzaufnahme derselben Stimme kann helfen; dafür gilt der unten beschriebene Weg über die Stimmenwahl.

## Ein kurzer Satz klingt nach einer anderen Stimme

Bei erzeugten Sätzen unter drei Sekunden kann der Stimmklang leichter wechseln. Stelle zunächst „Stimmanker“ auf „An“, lass „Prewarm“ auf „Beibehalten“ und vergleiche mit gleichem Seed. Die Referenzaufnahme soll der KI helfen, die Stimme der Figur gleich zu halten. Die drei Sekunden beziehen sich hier auf die erzeugte Spielzeile, nicht auf die Mindestlänge deiner Sprechprobe.

Hörst du zugleich einen englischen Akzent, teste bewusst „Stimmanker: An“ zusammen mit „Prewarm: An“, weiterhin mit gleichem Seed. Passt das Ergebnis noch nicht, lass beide Einstellungen stehen und versuche anschließend einen neuen Seed.

## Ein Name oder einzelnes Wort wird falsch gesprochen

Prüfe zuerst, ob das Wort im [Sprachausgabetext](review.text.md) richtig steht. Ist die Schreibweise korrekt, die Aussprache aber falsch, nähere die Schreibweise gezielt dem gewünschten Klang an, etwa bei einem englischen Namen. Behalte die Bedeutung des Satzes bei und ändere nicht mehrere Wörter auf einmal.

Erzeuge die Vorschau mit gleichem Seed und höre die ganze Zeile an: Die Korrektur soll das Wort verbessern, ohne den Satz unnatürlich zu machen. Wird ein richtig geschriebenes Wort nur in diesem Versuch verschluckt, kann stattdessen ein neuer Seed genügen.

## Die Wörter stimmen, aber die Betonung passt nicht

Wenn die Figur die richtigen Wörter sagt, der Satz aber nicht zur Szene passt, lass Text, Stimmanker und Prewarm zunächst unverändert und wähle „Vorschau mit neuem Seed“. Damit probierst du eine andere Sprechweise, ohne die Aussage des Dialogs zu verändern. Soll zum Beispiel „Da liegt der Schatz!“ überrascht statt beiläufig klingen, höre besonders auf den Ausruf. Bleibt die Betonung unpassend, prüfe die Zeichensetzung im Sprachausgabetext und vergleiche eine gezielte Änderung wieder mit gleichem Seed.

## Eine andere Referenz derselben Stimme verwenden

„Stimmanker: An“ aktiviert die vorhandene Aufnahme; in der Review gibt es keinen separaten Dateiwähler für eine andere Referenz. In der [Stimmenwahl](launcher.voices.md) kannst du bei einer eigenen Stimme „Diese Datei als zweite Probe hinzufügen“ nutzen. Diese zweite Aufnahme derselben Stimme steht dem ausdrücklich gestarteten [automatischen Reparaturversuch](launcher.repair-workflow.md) zur Verfügung.

Die Hauptprobe wird dagegen je Rolle gewählt. Sie zu ersetzen ist eine Änderung der Rollenstimme und kein eigener Stimmwechselknopf für diese eine Reviewzeile. Wenn du nur eine weitere Referenz für Reparaturversuche bereitstellen möchtest, verwende deshalb die optionale zweite Probe.

## Die passende Aufnahme festhalten

Höre eine gelungene Vorschau vollständig an und wähle „Vorschau als gut bestätigen“. Erst damit wird sie zur [aktuellen Auswahl](review.adopt.md). Du kannst gute Verbesserungen als [Rezept speichern](review.recipe.md), damit andere sie lokal ausprobieren und weiter verbessern können.

## Weiterführende Hilfe

- [Dialoge anhören, verbessern und ins Spiel übernehmen](review.workflow.md)
- [Mit gleichem oder neuem Seed vergleichen](review.seed.md)
- [Den Sprachausgabetext ändern](review.text.md)
- [Den Stimmanker für einen Versuch ändern](review.anchor.md)
- [Prewarm für einen Versuch ändern](review.prewarm.md)
- [Schritt 5: eine Stimme für jede Rolle wählen](launcher.voices.md)
- [Schritt 6: automatische Reparatur ausprobieren](launcher.repair-workflow.md)
- [Ein Rezept speichern](review.recipe.md)
