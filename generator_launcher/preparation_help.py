"""Read-only instructions for native audio preparation and original patch tools."""
from __future__ import annotations

import json
from pathlib import Path

from .contracts import ContractError, resolve_relative

FORMAT = 'mi1-preparation-help/1'
BUILDER_DIRECTORY = 'MI1_Ultimate_Talkie_Edition_Builder'
GAME_DIRECTORY = 'MI1_Ultimate_Talkie_Edition'


def _read(path):
    if not path.is_file(): return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict): raise ContractError('Lokale Vorbereitungskonfiguration ist ungültig')
    return value


def _wine_path(path, prefix):
    """Use the actual Z: mapping when available; otherwise label the convention."""
    drive = prefix / 'dosdevices/z:'
    if drive.is_symlink():
        mapped = drive.resolve()
        if not path.is_relative_to(mapped): return None, 'target_outside_z_drive'
        return 'Z:\\' + str(path.relative_to(mapped)).replace('/', '\\'), 'verified'
    if drive.exists(): return None, 'z_drive_not_symlink'
    return 'Z:\\' + str(path).lstrip('/').replace('/', '\\'), 'standard_mapping_not_verified'


def preparation_help(work_root, *, config=None, sources=None):
    """Return plain text and paths; never launch, download, install or write."""
    root = Path(work_root).expanduser().resolve(strict=True)
    home = root / 'MI1 Generator'
    config = _read(home / '.launcher/project.json') if config is None else config
    sources = _read(home / '.launcher/preparation-sources.json') if sources is None else sources
    if not isinstance(config, dict) or not isinstance(sources, dict): raise ContractError('Projektkonfiguration muss ein Objekt sein')
    stages = sources.get('stages', {})
    if not isinstance(stages, dict): raise ContractError('Vorbereitungsstufen sind ungültig')
    builder = resolve_relative(root, stages['builder']) if stages.get('builder') else None
    patch = resolve_relative(root, stages['patch']) if stages.get('patch') else None
    builder_name = builder.parent.name if builder else BUILDER_DIRECTORY
    english = home / 'generated/game-preparation/english'
    german = home / 'generated/game-preparation/german'
    target = german / builder_name / GAME_DIRECTORY
    prefix = home / 'foreign/preparation/wineprefix'
    windows, mapping = _wine_path(target, prefix)
    pc = config.get('pc_special_edition_directory')
    paths = dict(work_root=str(root), pc_special_edition=str(Path(pc).expanduser().resolve()) if pc else None,
        builder=str(builder) if builder else None, patch=str(patch) if patch else None,
        english_stage=str(english), english_builder=str(english / builder_name),
        english_game=str(english / builder_name / GAME_DIRECTORY), german_stage=str(german),
        patch_target_macos=str(target), patch_target_wine=windows, wineprefix=str(prefix),
        receipts={name: str(home / 'generated/game-preparation' / name) for name in
                  ('english.receipt.json', 'german.receipt.json', 'preparation.json')})
    prerequisites = [
        'Falls die App noch nach Werkzeugbedingungen fragt, lies und bestätige sie vor dem Start. Danach kannst du die Spielvorbereitung auf dieser Seite beginnen.',
    ]
    selection = [
        'Sobald sich der deutsche Patch öffnet, folge seinen Dialogen. Bleibt das Fenster verdeckt, hole es mit „Patchfenster in den Vordergrund holen“ nach vorn oder klicke im Dock auf Wine.',
        'Wenn der Patch nach einem Ordner fragt, verwende den hervorgehobenen Windows-Zielordner. Die App kopiert ihn beim Patchstart automatisch; mit „Zielordner in die Zwischenablage kopieren“ erreichst du ihn bei Bedarf erneut.',
        'Zum manuellen Kopieren klickst du hier ins Pfadfeld und drückst Command+C. Im Wine-Patch fügst du den Pfad mit Control+V ins Ordnerfeld ein; alternativ kannst du Rechtsklick → „Einfügen“ verwenden.',
    ]
    if not windows:
        selection.append('Die App kann derzeit keinen passenden Windows-Zielordner anzeigen. Beende den Patchdialog und prüfe die Laufwerkszuordnung in Wine, bevor du fortfährst.')
    if mapping == 'standard_mapping_not_verified':
        selection.append('Der angezeigte Z:-Pfad verwendet Wines übliche Laufwerkszuordnung. Prüfe im Patchdialog, ob der Zielordner tatsächlich MONKEY.000 und MONKEY.001 enthält, bevor du den Patch ausführst.')
    builder_instructions = [
        'Mit „Spieldaten vorbereiten und Patch starten“ beginnt die App mit der nativen Spielvorbereitung. Lass die geöffneten Wine-Fenster während der Arbeit offen. Danach führt dich der deutsche Patch weiter.',
    ]
    return dict(format=FORMAT, title='Audiovorbereitung und deutschen Patch bedienen', paths=paths,
        steps=[
            dict(id='prerequisites', title='Einrichtung abschließen', instructions=prerequisites),
            dict(id='builder', title='Spieldaten vorbereiten', instructions=builder_instructions),
            dict(id='patch', title='Deutschen Patch bedienen', instructions=selection,
                 path=str(target), wine_path=windows),
            dict(id='finish', title='Vorbereitung abschließen', instructions=[
                'Nach der Abschlussmeldung kannst du den Patch schließen und zur Generator-App zurückkehren. Warte dort auf „Vorbereitung abgeschlossen“ in der Konsole; anschließend geht es mit „Sprachausgabe rendern“ weiter.',
                'Ist ein Fehler aufgetreten, sichere seine Meldung und das Protokoll, bevor du einen weiteren Versuch beginnst.'
            ])
        ], evidence=dict(patch_instructions='Original-Liesmich v1.01: Ordner mit MONKEY.000 und MONKEY.001 auswählen und Anweisungen folgen',
            builder_instructions='UTE-Rezeptur mit nativen FFmpeg/SoX/WAV→SBL-Audiostufen und Wine-Ressourcenhelfern. Ausgabe MI1_Ultimate_Talkie_Edition.',
            installer_buttons='not_observed', z_drive=mapping))
