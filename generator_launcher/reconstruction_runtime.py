"""Rebuild private dialogue text from authenticated, locally prepared games."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

from .contracts import ContractError, resolve_relative
from .local_preparation import verify_preparation_receipt
from .original_audio import english_archive, scan_archive
from .text_reconstruction import MARKER, ReconstructionError, reconstruct


def game_directory(work_root, outputs, locale):
    """Find both script resources in one authenticated game directory."""
    parents = []
    for name in ('monkey.000', 'monkey.001'):
        matches = [value for key, value in outputs.items()
                   if key.lower() == locale + ':' + name]
        if len(matches) != 1:
            raise ContractError('Textrekonstruktion: eindeutige Spielressource fehlt: ' + locale + ':' + name)
        path = resolve_relative(work_root, matches[0]['path'], must_exist=True)
        if path.name.lower() != name:
            raise ContractError('Textrekonstruktion: Spielressourcenname stimmt nicht')
        parents.append(path.parent)
    if parents[0] != parents[1]:
        raise ContractError('Textrekonstruktion: Spielressourcen liegen in verschiedenen Ordnern')
    return parents[0]


def text_index(extractor, export, samples):
    rows, bad, _ = extractor.parse_scummtr(export, samples)
    if bad:
        # Do not expose original text from parser diagnostics in the UI/log.
        raise ContractError('Textrekonstruktion: ungültiger ScummTR-Export (' + str(len(bad)) + ' Befunde)')
    index = {}
    for row in rows:
        occurrence = dict(text=row.text, location={key: getattr(row, key) for key in
            ('room_id', 'resource_type', 'resource_id', 'script_id', 'opcode', 'source_line', 'sequence')})
        index.setdefault(str(row.speech_id), []).append(occurrence)
    return index


def reconstruct_local_manifest(root, manifest, config, installation):
    if MARKER not in manifest:
        return manifest
    root = Path(root).resolve(strict=True)
    try:
        evidence = json.loads((root / 'generated/game-preparation/preparation.json').read_text())
        receipt = evidence['preparation_receipt']
        project_id = config['project_state']['project_id']
        if (evidence.get('development_only') or receipt['inputs'].get('development_only') or
                not verify_preparation_receipt(receipt, installation, project_id=project_id,
                    inputs=receipt['inputs'], work_root=root.parent)):
            raise ContractError('Textrekonstruktion: lokale Spielvorbereitung ist nicht authentifiziert')
        if receipt['inputs'].get('selected_manifest_sha256') != config['project_state']['version']['manifest_sha256']:
            raise ContractError('Textrekonstruktion: Spielvorbereitung gehört zu einem anderen Manifest; erneut vorbereiten')
        outputs = receipt['outputs']
        tool = resolve_relative(root, 'bin/scummtr', must_exist=True)
        loader = importlib.machinery.SourceFileLoader('launcher_text_extractor',
            str(Path(__file__).resolve().parents[1] / 'mi1-dubbing-extractor'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        extractor = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = extractor
        loader.exec_module(extractor)
        samples = {row.speech_id: row for row in scan_archive(english_archive(root.parent, outputs), extractor)}
        destination = resolve_relative(root, 'generated/text-reconstruction')
        destination.mkdir(parents=True, exist_ok=True)
        indexes = {}
        # Exports and indexes contain private game texts; they never enter the
        # distribution. Temporary exports are removed even on failure.
        with tempfile.TemporaryDirectory(prefix='local-', dir=destination) as temporary:
            for locale, stage in (('en', 'english'), ('de', 'german')):
                export = Path(temporary) / (locale + '.txt')
                try:
                    extractor.export_scummtr(str(tool), game_directory(root.parent, outputs, stage), export)
                except RuntimeError as exc:
                    raise ContractError('Textrekonstruktion: ScummTR-Export fehlgeschlagen; lokale Werkzeuge und Spielvorbereitung prüfen') from exc
                indexes[locale] = text_index(extractor, export, samples)
            return reconstruct(manifest, indexes)
    except ContractError:
        raise
    except (OSError, ValueError, KeyError, ReconstructionError) as exc:
        raise ContractError('Textrekonstruktion fehlgeschlagen: ' + str(exc)) from exc
