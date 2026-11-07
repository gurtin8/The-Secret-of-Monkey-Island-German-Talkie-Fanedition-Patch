"""Explicit asynchronous patch builds from a frozen complete review selection."""
from __future__ import annotations

import copy
import argparse
import json
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from collections import OrderedDict

from mi1_patcher import (TOTAL_DIALOGUES, build_patch, discover_basis, verify_basis, speech_id, sha256_file,
                         export_root, next_export_name, verified_export)
from tools.patch_mi1_intro import resolve_scummrp
from .evidence import Evidence, digest, read
from .service import atomic_json

_EXPORT_REGISTRY_LOCK = threading.RLock()


class PatchBuildController:
    def __init__(self, service, game_root=None, ffmpeg=None, *, defer_basis=False):
        self.service = service
        self.path = service.root / 'patch-build-state.json'
        self.default_output_root = service.evidence.root / 'Monkey Island German Talkie Fanedition'
        self.output_root = self.default_output_root
        self.registry_path = service.evidence.root / 'work' / 'patch-export-registry.json'
        self.thread = None
        self._availability_cache = None
        self._basis_validation_cache = {}
        self._output_listing_cache = {}
        source_hash=getattr(service.evidence,'source_sha256',None)
        if source_hash is None:source_hash=digest(service.evidence.source)
        self.state = read(self.path) if self.path.is_file() else dict(format='patch-build-state/1',
            source_manifest_sha256=source_hash, game_root=None, job=self.idle())
        if self.state.get('source_manifest_sha256') != source_hash:
            raise ValueError('Patchbuildzustand gehört zu einem anderen Quellmanifest')
        self.output_root = export_root(self._registry().get('last_output_root') or self.state.get('output_root') or self.default_output_root)
        if self.state['job']['status'] == 'running':
            self.state['job'].update(status='failed', phase='interrupted', error='Server wurde während des Patchbuilds beendet; kein automatischer Neustart.', output_dir=None)
            atomic_json(self.path, self.state)
        configured = None
        config = service.evidence.root / 'work' / 'mi1-patcher-config.json'
        if config.is_file() and not config.is_symlink():
            value = read(config)
            configured = value.get('game_root')
            if configured is not None and not isinstance(configured, str):
                raise ValueError('Patcher-Konfiguration game_root muss ein Pfadtext sein')
        self._configured_game_root=configured
        # An explicit caller path remains authoritative. Auto-discovered roots
        # are re-resolved when a preparation restart archives the old tree.
        self._manual_game_root = str(Path(game_root).expanduser().resolve()) if game_root else None
        self._basis_checked = not defer_basis
        self.game_root = self._manual_game_root or (None if defer_basis else self._resolve_game_root(configured))
        if self.game_root:
            self.game_root = str(Path(self.game_root).expanduser().resolve())
        bundled = service.evidence.root / 'bin' / 'ffmpeg'
        self.ffmpeg = (shutil.which(str(ffmpeg)) or str(Path(ffmpeg).expanduser().resolve())) if ffmpeg else str(bundled) if bundled.is_file() else shutil.which('ffmpeg')
        try:
            self.scummrp = resolve_scummrp(service.evidence.root)
        except FileNotFoundError:
            self.scummrp = None

    @staticmethod
    def idle():
        return dict(id=None, status='idle', phase=None, completed=0, total=TOTAL_DIALOGUES,
                    error=None, output_dir=None, report_path=None)

    def _verified_basis(self, candidate):
        if not candidate:return None
        root=Path(candidate).expanduser()
        try:
            relevant={'monkey.000','monkey.001','monster.sou','monkey.sof'}
            signature=tuple(sorted((p.name,p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns)
                            for p in root.iterdir() if p.name.lower() in relevant))
            resolved=str(root.resolve())
        except OSError:return None
        descriptor=getattr(self.service,'prepared_game_basis',None)
        descriptor=descriptor if descriptor and descriptor.get('root')==resolved else None
        # Loading the authenticated preparation can change SOF authorization
        # without changing any file timestamps.
        signature=(signature,digest(descriptor) if descriptor else None)
        cached=self._basis_validation_cache.get(resolved)
        if cached and cached[0]==signature:return resolved if cached[1] else None
        try:verify_basis(root,signed_archive=descriptor);verified=resolved
        except (OSError,ValueError):verified=None
        self._basis_validation_cache[resolved]=(signature,bool(verified))
        return verified

    def _resolve_game_root(self, configured=None):
        # A saved or configured path wins only while its UTE files still
        # authenticate. Then consult the current signed preparation receipt.
        if configured is None:configured=self._configured_game_root
        for candidate in (self.state.get('game_root'),configured):
            verified=self._verified_basis(candidate)
            if verified:return verified
        resolver=getattr(self.service,'prepared_game_root_resolver',None)
        prepared=resolver() if callable(resolver) else getattr(self.service,'prepared_game_root',None)
        verified=self._verified_basis(prepared)
        if verified:return verified
        for candidate in (Path(__file__).resolve().parents[1]/'input'/'ute_de',self.service.evidence.root/'input'/'ute_de'):
            verified=self._verified_basis(candidate)
            if verified:return verified
        return None

    def _refresh_game_root(self):
        if self._manual_game_root:
            verified=self._verified_basis(self._manual_game_root)
            if verified:return verified
            self._manual_game_root=None
        verified=self._verified_basis(self.game_root)
        if verified:return verified
        self.game_root=self._resolve_game_root()
        return self.game_root

    @staticmethod
    def _metadata_selection(service, did):
        """Cheap preflight; does not treat sparse patch-selection as full coverage."""
        saved = service.state.get('rows', {}).get(did)
        current = service.base[did].get('current')
        if saved:
            selection = saved.get('selection')
            if selection:
                if selection.get('fingerprint') == 'original':
                    original = service.original_output(did)
                    if (not original['verified'] or original['binding'] != selection or
                            not any(r.get('binding') == selection and r.get('processing') == 'copy_original' for r in saved.get('releases', []))):
                        return None, 'Gespeicherte Originaltonauswahl ist ungültig: ' + did
                    current = original
                elif current and current.get('binding') == selection:
                    pass
                else:
                    candidate = next((c for c in saved.get('candidates', [])
                        if (c.get('binding') == selection or
                            (c.get('attempt', {}).get('fingerprint') == selection.get('fingerprint') and
                             c.get('record', {}).get('wav_sha256') == selection.get('wav_sha256')))), None)
                    if candidate is None or not any(r.get('binding') == selection for r in saved.get('releases', [])):
                        return None, 'Freigegebene Auswahl fehlt oder ist nicht belegt: ' + did
                    current = dict(id=selection['fingerprint'], binding=selection, verified=True)
        if not current or not current.get('verified') or not current.get('binding', {}).get('wav_sha256'):
            return None, 'Keine gültige ausgewählte Audioausgabe: ' + did
        return current, None

    def availability(self, *, validate_basis=True, output_root=None):
        with self.service.lock:
            if validate_basis and self.state['job']['status']!='running':
                self._refresh_game_root()
                self._basis_checked = True
            revision = self.service.state['revision']
            if self._availability_cache is None or self._availability_cache[0] != revision:
                count = 0
                reason = None
                for did in self.service.dialogues:
                    _, error = self._metadata_selection(self.service, did)
                    if error:
                        reason = reason or error
                    else:
                        count += 1
                if not self.service.evidence.valid:
                    reason = 'Die Manifest-/Runbelege sind ungültig.'
                elif (self.service.manifest.get('game', {}).get('game_id') != 'mi1:ute' or
                      self.service.manifest.get('game', {}).get('game_version') != '1.02'):
                    reason = 'Der Patcher unterstützt ausschließlich MI1 Ultimate Talkie Edition 1.02.'
                elif len(self.service.dialogues) != TOTAL_DIALOGUES:
                    reason = 'Ein Patch benötigt das vollständige UTE-1.02-Manifest mit 4.393 Dialogen.'
                self._availability_cache = (revision, count, reason)
            _, count, reason = self._availability_cache
            external_busy = self.service.availability_callback() if self.service.availability_callback else None
            busy = (self.service.state['job']['status'] == 'running' or self.state['job']['status'] == 'running' or
                    bool(external_busy and external_busy.get('busy')))
            if self.service.state['job']['status'] == 'running':
                reason = 'Ein Renderauftrag läuft; Patchbuild erst nach Abschluss starten.'
            elif self.state['job']['status'] == 'running':
                reason = 'Ein Patchbuild läuft bereits.'
            elif external_busy and external_busy.get('busy'):
                reason = external_busy.get('reason') or 'Wizardrenderer belegt.'
            elif not self.ffmpeg or not Path(self.ffmpeg).is_file():
                reason = 'Lokales FFmpeg fehlt.'
            elif not self.scummrp or not self.scummrp.is_file():
                reason = 'Lokales ScummRP für den Standard-Intro-Patch fehlt; Runtime einrichten.'
            elif not self._basis_checked:
                reason = 'Spielbasis wird beim Öffnen der Exportansicht geprüft.'
            elif not self.game_root:
                prepared=getattr(self.service,'prepared_game_root_diagnostic',{}) or {}
                detail=prepared.get('reason')
                reason=('Automatische Spielbasisübernahme fehlgeschlagen: '+detail if detail else
                        'Keine validierte deutsche UTE 1.02-Spielbasis gefunden.')
            destination = export_root(output_root if output_root is not None else self.output_root, self.game_root)
            outputs = self.existing_outputs(destination)
            return dict(available=reason is None and count == TOTAL_DIALOGUES, reason=reason,
                        busy=busy,
                        audio_available=count == TOTAL_DIALOGUES and len(self.service.dialogues) == TOTAL_DIALOGUES and self._availability_cache[2] is None,
                        source_required=not bool(self.game_root),
                        ffmpeg_available=bool(self.ffmpeg and Path(self.ffmpeg).is_file()),
                        available_dialogues=count, total_dialogues=len(self.service.dialogues),
                        game_root=self.game_root, output_root=str(destination),
                        existing_outputs=outputs, last_output=outputs[-1] if outputs else None,
                        target_name=next_export_name(destination),
                        prepared_receipt_path=(getattr(self.service,'prepared_game_root_diagnostic',{}) or {}).get('receipt_path'))

    def status(self, *, validate_basis=True, output_root=None):
        with self.service.lock:
            return {**self.availability(validate_basis=validate_basis, output_root=output_root), 'job':copy.deepcopy(self.state['job'])}

    def _registry(self):
        export_root(self.registry_path.parent)
        if self.registry_path.is_symlink():
            raise ValueError('Exportkatalog darf kein symbolischer Link sein')
        if not self.registry_path.exists():
            return {'format':'mi1-patch-export-registry/1', 'outputs':[]}
        registry = read(self.registry_path)
        if registry.get('format') != 'mi1-patch-export-registry/1' or not isinstance(registry.get('outputs'), list):
            raise ValueError('Exportkatalog ist ungültig')
        return registry

    def _registered_output(self, candidate, registry=None):
        path = Path(candidate).expanduser().absolute()
        registry = registry or self._registry()
        entry = next((item for item in registry['outputs'] if item.get('path') == str(path)), None)
        # Upgrade a previous app version's completed job only from its saved
        # UUID/path association, never by trusting arbitrary scanned reports.
        if entry is None:
            job = self.state.get('job', {})
            if (job.get('status') == 'completed' and job.get('output_dir') == str(path) and
                    re.fullmatch(r'[a-f0-9]{32}', job.get('id') or '') and
                    job.get('report_path') == str(path / 'build-report.json')):
                report_path = path / 'build-report.json'
                if not report_path.is_symlink() and report_path.is_file():
                    entry = {'path':str(path), 'build_id':job['id'], 'report_sha256':sha256_file(report_path)}
        if entry is None:
            raise ValueError('Nur eine von dieser App belegte Spielausgabe kann ersetzt oder geöffnet werden')
        report = verified_export(path, build_id=entry['build_id'], report_sha256=entry['report_sha256'])
        return {**entry, 'name':path.name, 'created_at':report.get('created_at', '')}

    def existing_outputs(self, destination):
        registry = self._registry()
        candidates = [entry.get('path') for entry in registry['outputs'] if entry.get('path')]
        job = self.state.get('job', {})
        if job.get('status') == 'completed' and job.get('output_dir'):
            candidates.append(job['output_dir'])
        outputs = []
        for candidate in dict.fromkeys(candidates):
            if Path(candidate).parent != destination:
                continue
            try:
                path = Path(candidate)
                report_path = path / 'build-report.json'
                signature = tuple((item.stat().st_size,item.stat().st_mtime_ns,item.stat().st_ctime_ns)
                                  for item in (path, report_path))
                record = next((entry for entry in registry['outputs'] if entry.get('path') == candidate), None)
                signature = (signature, digest(record) if record else None)
                cached = self._output_listing_cache.get(candidate)
                if cached and cached[0] == signature and not path.is_symlink() and not report_path.is_symlink():
                    entry = cached[1]
                else:
                    entry = self._registered_output(candidate, registry)
                    self._output_listing_cache[candidate] = (signature,entry)
            except (ValueError, OSError, KeyError, TypeError):
                continue
            outputs.append({key:entry[key] for key in ('path', 'name', 'build_id', 'created_at')})
        return sorted(outputs, key=lambda entry:(entry['created_at'], entry['name']))

    def _record_output(self, result):
        path = Path(result['output_dir'])
        with _EXPORT_REGISTRY_LOCK:
            registry = self._registry()
            registry['outputs'] = [entry for entry in registry['outputs'] if entry.get('path') != str(path)]
            registry['outputs'].append(dict(path=str(path), build_id=result['build_id'],
                created_at=result.get('created_at'), report_sha256=sha256_file(path / 'build-report.json')))
            atomic_json(self.registry_path, registry)

    def _complete_output(self, result):
        # Called while the writer still holds the old replacement backup.
        # A persistence failure therefore rolls publication back as well.
        with self.service.lock, _EXPORT_REGISTRY_LOCK:
            previous_registry = self._registry()
            previous_job = copy.deepcopy(self.state['job'])
            try:
                self._record_output(result)
                self.state['job'].update(status='completed', phase='completed', completed=TOTAL_DIALOGUES,
                    output_dir=result['output_dir'], report_path=result['report_path'])
                atomic_json(self.path, self.state)
            except Exception:
                self.state['job'] = previous_job
                atomic_json(self.registry_path, previous_registry)
                raise


    def report(self):
        with self.service.lock:
            path = self.state['job'].get('report_path')
            if not path:
                raise FileNotFoundError('Noch kein Patchbuildbericht vorhanden')
            path = Path(path)
            job = self.state['job']
            base = export_root(job.get('output_root') or self.output_root)
            if path.is_symlink() or not path.resolve().is_relative_to(base):
                raise ValueError('Buildberichtpfad ist ungültig')
            if job.get('status') == 'completed':
                entry = self._registered_output(path.parent)
                if entry['build_id'] != job.get('id') or path.name != 'build-report.json':
                    raise ValueError('Buildbericht besitzt keine passende Buildbindung')
            elif path != base / (str(job.get('id')) + '.failed-report.json'):
                raise ValueError('Buildberichtpfad ist ungültig')
            return read(path)

    def open_output(self):
        job=self.state.get('job',{})
        if job.get('status')!='completed' or not job.get('output_dir'):
            raise ValueError('Es gibt noch keinen fertigen Spielordner zum Öffnen.')
        candidate=Path(job['output_dir'])
        if candidate.is_symlink():raise ValueError('Der fertige Spielordner darf kein symbolischer Link sein.')
        try:
            path=candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError('Der gespeicherte Spielordner ist nicht mehr verfügbar oder liegt außerhalb des erlaubten Exportziels.') from exc
        base=export_root(job.get('output_root') or self.output_root)
        if path.parent!=base or not path.is_dir():
            raise ValueError('Der gespeicherte Spielordner liegt außerhalb des erlaubten Exportziels.')
        entry=self._registered_output(path)
        if entry['build_id'] != job.get('id'):
            raise ValueError('Der Spielordner gehört nicht zum gespeicherten Build.')
        subprocess.run(['/usr/bin/open','-R',str(path)],check=True)
        return {'opened':True,'path':str(path)}

    @staticmethod
    def _freeze(service):
        frozen = copy.copy(service)
        frozen.state = copy.deepcopy(service.state)
        frozen.manifest = copy.deepcopy(service.manifest)
        frozen.dialogues = {d['dialog_id']: d for d in frozen.manifest['dialogues']}
        frozen.roles = copy.deepcopy(service.roles)
        frozen.base = copy.deepcopy(service.base)
        frozen.attempts = copy.deepcopy(service.attempts)
        frozen.lock = threading.RLock()
        frozen.evidence = copy.copy(service.evidence)
        for name in ('source', 'manifest', 'plan', 'report', 'plans', 'reports', 'bindings', 'paths'):
            setattr(frozen.evidence, name, copy.deepcopy(getattr(service.evidence, name)))
        frozen.evidence.hash_cache = {}
        frozen.evidence._external_run_plan_cache = {}
        frozen._json_cache = OrderedDict()
        frozen._json_cache_bytes = 0
        frozen._snapshot_identity_cache = OrderedDict()
        frozen._overlay_signatures = copy.deepcopy(service._overlay_signatures)
        # This object is only a resolver, never a renderer or state writer.
        frozen.executor = None
        frozen.sync_callback = None
        frozen.selection_callback = None
        return frozen

    def start(self, payload):
        with self.service.lock:
            if set(payload) - {'revision', 'game_root', 'output_root', 'output_policy', 'replace_output'}:
                raise ValueError('Unbekannte Patchbuildfelder')
            self.service.check(payload)
            if self.state['job']['status'] == 'running' or self.service.state['job']['status'] == 'running':
                raise RuntimeError('Ein Build- oder Renderauftrag läuft bereits.')
            availability = self.availability()
            if not availability['audio_available']:
                raise ValueError(availability['reason'] or 'Vollständige belegte Dialogauswahl fehlt')
            if not self.ffmpeg or not Path(self.ffmpeg).is_file():
                raise ValueError('Lokales FFmpeg fehlt')
            if not self.scummrp or not self.scummrp.is_file():
                raise ValueError('Lokales ScummRP für den Standard-Intro-Patch fehlt')
            chosen = self.game_root or payload.get('game_root')
            if not isinstance(chosen, str) or not chosen.strip():
                raise ValueError('Spielbasis als Pfad erforderlich')
            chosen = str(Path(chosen).expanduser().resolve())
            descriptor=copy.deepcopy(getattr(self.service,'prepared_game_basis',None))
            verify_basis(chosen, signed_archive=descriptor)
            destination = export_root(payload.get('output_root', self.output_root), chosen)
            outputs = self.existing_outputs(destination)
            policy = payload.get('output_policy')
            if policy is None and not outputs:
                policy = 'new'
            if policy not in ('new', 'replace'):
                raise ValueError('Bitte weitere Spielausgabe oder Ersetzen ausdrücklich wählen')
            replacement = payload.get('replace_output')
            replacement_identity = None
            if policy == 'replace':
                if not isinstance(replacement, str) or not replacement.strip():
                    raise ValueError('Zu ersetzende Spielausgabe auswählen')
                replacement = str(Path(replacement).expanduser().absolute())
                if Path(replacement).parent != destination:
                    raise ValueError('Zu ersetzende Spielausgabe liegt außerhalb des Exportziels')
                entry = self._registered_output(replacement)
                replacement_identity = {key:entry[key] for key in ('build_id', 'report_sha256')}
            elif replacement is not None:
                raise ValueError('Ersetzenziel ist nur bei ausdrücklichem Ersetzen erlaubt')
            callback = self.service.availability_callback
            if callback:
                busy = callback()
                if busy and busy.get('busy'):
                    raise RuntimeError(busy.get('reason') or 'Renderer belegt')
            previous=self.state.get('job', {})
            if previous.get('status') == 'completed' and previous.get('report_path'):
                try:self._record_output(self.report())
                except (OSError, ValueError):pass
            frozen = self._freeze(self.service)
            jid = uuid.uuid4().hex
            self.game_root = chosen
            self.state['game_root'] = chosen
            with _EXPORT_REGISTRY_LOCK:
                registry = self._registry()
                registry['last_output_root'] = str(destination)
                atomic_json(self.registry_path, registry)
            self.output_root = destination
            self.state['output_root'] = str(destination)
            self.state['job'] = dict(id=jid, status='running', phase='validating_evidence', completed=0,
                total=TOTAL_DIALOGUES, error=None, output_dir=None, report_path=None,
                review_revision=frozen.state['revision'], output_root=str(destination),
                output_policy=policy, replace_output=replacement)
            atomic_json(self.path, self.state)
            self.thread = threading.Thread(target=self._run, args=(frozen, chosen, jid, descriptor, destination, replacement, replacement_identity), daemon=True)
            self.thread.start()
            return self.status()

    def _progress(self, phase, completed, total):
        with self.service.lock:
            self.state['job'].update(phase=phase, completed=completed, total=total)
            # Polling sees in-memory progress; persist phase changes and a small
            # number of checkpoints instead of writing 4,393 state files.
            if completed % 100 == 0 or completed == total:
                atomic_json(self.path, self.state)

    @staticmethod
    def _verify_source(frozen):
        ev = frozen.evidence
        if not ev.valid:
            raise ValueError('Quellbelegbindung ungültig')
        if hasattr(ev, 'source_path') and read(ev.source_path) != ev.source:
            raise ValueError('Quellmanifest wurde nach Serverstart verändert')
        for name, expected in (('manifest.json', ev.manifest), ('run-plan.json', ev.plan), ('render-report.json', ev.report)):
            path = ev.run / name
            if not path.is_file() or path.is_symlink() or read(path) != expected:
                raise ValueError('Unveränderlicher Runbeleg wurde verändert: ' + name)
        bindings = ev.run / 'backend-bindings.json'
        if ev.bindings and (bindings.is_symlink() or not bindings.is_file() or
                {b['attempt_fingerprint']:b for b in read(bindings).get('bindings', [])} != ev.bindings):
            raise ValueError('Quell-Backendbindungen wurden verändert')

    def _snapshot(self, frozen):
        self._verify_source(frozen)
        rows = []
        for n, did in enumerate(frozen.dialogues):
            self._progress('validating_evidence', n, TOTAL_DIALOGUES)
            expected, error = self._metadata_selection(frozen, did)
            if error:
                raise ValueError(error)
            row = frozen.row(did)
            current = row.get('current')
            if not current or current.get('binding') != expected['binding']:
                raise ValueError('Explizite Auswahl wurde nicht exakt aufgelöst: ' + did)
            current = frozen.validate_output(did, current)
            if current.get('binding') != expected['binding']:
                raise ValueError('Tief geprüfte Ausgabe weicht vom eingefrorenen Beleg ab: ' + did)
            saved = frozen.saved(did)
            query = parse_qs(urlparse(current.get('audio_url') or '').query)
            if len(query.get('key', [])) != 1 or len(query.get('sha256', [])) != 1:
                raise ValueError('Audio-URL ist nicht exakt belegt: ' + did)
            key, wanted = query['key'][0], query['sha256'][0]
            path = frozen.evidence.paths.get((key, wanted))
            if path is None or wanted != current['binding']['wav_sha256']:
                raise ValueError('Audio-Pfad-/Hashbindung fehlt: ' + did)
            # Deep output validation has checked all receipt bytes; staging will
            # additionally hash actual audio bytes against this exact wanted hash.
            rows.append(dict(dialog_id=did, speech_id=speech_id(frozen.dialogues[did]),
                processing='copy_original' if current['id'] == 'original' else 'render',
                input_path=str(path), input_sha256=wanted, binding=copy.deepcopy(current['binding']),
                tts_text=current['tts_text'], qa_expected_text=current['qa_expected_text'],
                qa_status=current.get('qa_status'), qa_reason_codes=current.get('qa_reason_codes', []),
                qa_findings=current.get('qa_evidence_findings', []), findings=current.get('findings', []),
                stt=copy.deepcopy(current.get('stt')), preset=copy.deepcopy(current.get('preset')),
                good_confirmed=saved.get('good_binding') == current['binding'],
                pending_changes_excluded=frozen.next_action(saved) != 'none',
                pending_changes_policy='Ausstehende Text-/Preset-/Originaltonvorgaben werden nicht übernommen; ausgewählte belegte Ausgabe wird verwendet.',
                releases=[copy.deepcopy(r) for r in saved.get('releases', []) if r.get('binding') == current['binding']]))
        preparation = copy.deepcopy(frozen.manifest.get('source_evidence', {}).get('launcher_game_preparation', {}))
        return dict(format='mi1-patch-selected-snapshot/1', review_revision=frozen.state['revision'],
            manifest_id=frozen.manifest['manifest_id'], manifest_sha256=frozen.evidence.manifest_hash,
            source_manifest_sha256=frozen.evidence.source_sha256, run_plan_sha256=frozen.evidence.plan['sha256'],
            source_render_report_sha256=digest(frozen.evidence.report),
            source_evidence_files=[dict(path=str(frozen.evidence.run / name), sha256=sha256_file(frozen.evidence.run / name))
                for name in ('manifest.json', 'run-plan.json', 'render-report.json')],
            source_run=str(frozen.evidence.run), dialogues=rows,
            development_only=preparation.get('development_only') is True, game_preparation=preparation)

    def _run(self, frozen, game_root, jid, signed_archive=None, destination=None, replacement=None, replacement_identity=None):
        destination = destination or self.output_root
        try:
            snapshot = self._snapshot(frozen)
            if signed_archive and signed_archive.get('root')==str(Path(game_root).resolve()):
                snapshot['game_preparation']={**snapshot.get('game_preparation',{}),
                    'basis_source':'locally_signed_german_ute_preparation',
                    'basis_archive_name':signed_archive.get('archive_name'),
                    'basis_archive_sha256':signed_archive.get('archive_sha256'),
                    'preparation_receipt_sha256':signed_archive.get('preparation_receipt_sha256'),
                    'preparation_manifest_sha256':signed_archive.get('preparation_manifest_sha256')}
            result = build_patch(snapshot, game_root, destination, self.ffmpeg, self._progress, jid,
                                 scummrp=self.scummrp,signed_archive=signed_archive,
                                 replace_output=replacement, replace_identity=replacement_identity,
                                 on_publish=self._complete_output)
        except Exception as exc:
            failure = destination / (jid + '.failed-report.json')
            if not failure.exists():
                atomic_json(failure, dict(format='mi1-patch-build-report/1', build_id=jid, status='failed',
                    playable=False, game_runtime_tested=False, review_revision=frozen.state['revision'], error=str(exc), output_dir=None,
                    report_path=str(failure), dialogues=[]))
            with self.service.lock:
                self.state['job'].update(status='failed', phase='failed', error=str(exc),
                                        output_dir=None, report_path=str(failure))
                atomic_json(self.path, self.state)


def read_only_service(manifest_path, asset_root, run_root, state_root):
    """CLI resolver without ReviewService.__init__ migrations/selection writes."""
    from .service import ReviewService
    service = ReviewService.__new__(ReviewService)
    service.evidence = Evidence(manifest_path, asset_root, run_root)
    service.manifest = service.evidence.manifest
    service.mode = 'patch'
    service.root = Path(state_root).expanduser().resolve(strict=True)
    service.path = service.root / 'review-state.json'
    service.state = read(service.path) if service.path.is_file() else dict(format='patch-review-state/1',
        manifest_id=service.manifest['manifest_id'], source_manifest_sha256=service.evidence.source_sha256,
        revision=0, rows={}, job=ReviewService.idle())
    if (service.state.get('format') != 'patch-review-state/1' or
            service.state.get('manifest_id') != service.manifest['manifest_id'] or
            service.state.get('source_manifest_sha256') != service.evidence.source_sha256):
        raise ValueError('Reviewzustand passt nicht zum Quellmanifest')
    service.lock = threading.RLock()
    service.thread = None
    service._patch_builder = None
    for attr in ('executor', 'text_callback', 'sync_callback', 'prepare_callback', 'selection_callback',
                 'processing_callback', 'availability_callback'):
        setattr(service, attr, None)
    service.dialogues = {d['dialog_id']:d for d in service.manifest['dialogues']}
    service.roles = {r['role_id']:r for r in service.manifest.get('roles', [])}
    service.base = {}
    service.attempts = {}
    service._json_cache = OrderedDict()
    service._json_cache_limit = 256
    service._json_cache_bytes = 0
    service._json_cache_max_bytes = 64 * 1024 * 1024
    service._json_cache_max_file_bytes = 16 * 1024 * 1024
    service._snapshot_identity_cache = OrderedDict()
    service._snapshot_identity_limit = 256
    service._code_receipt_cache = None
    service._overlay_signatures = {}
    for did, dialogue in service.dialogues.items():
        service._overlay_signatures[did] = service._overlay_signature(did)
        service.base[did] = service.base_row(dialogue)
    # Old states must be opened in review UI for deliberate migration first.
    for saved in service.state.get('rows', {}).values():
        required = {'next_preset', 'action_baseline', 'anchor_ref', 'generation_mode', 'processing_binding', 'accepted_probe'}
        if not required <= saved.keys():
            raise ValueError('Alter Reviewzustand: zuerst im Review öffnen und speichern')
        service.validate_settings(saved)
    return service


def main(argv=None):
    parser = argparse.ArgumentParser(description='Expliziter UTE-1.02-Patchbuild aus vollständig belegtem Reviewstand; Originale bleiben unverändert.')
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--asset-root', required=True)
    parser.add_argument('--run-root', required=True)
    parser.add_argument('--state-root', required=True, help='Vorhandener Reviewzustand; Ausgabe in eigenem builds-Unterordner')
    parser.add_argument('--game-root', help='Deutsche UTE 1.02, nur lesend; sonst konfigurierte/geprüfte lokale Basis')
    parser.add_argument('--output-root', help='Elternordner für die fertige Spielausgabe')
    parser.add_argument('--output-policy', choices=('new','replace'), default='new')
    parser.add_argument('--replace-output', help='Belegte vorherige Spielausgabe ausdrücklich ersetzen')
    parser.add_argument('--ffmpeg', help='Lokaler FFmpeg-Pfad; sonst asset-root/bin/ffmpeg oder PATH')
    args = parser.parse_args(argv)
    service = read_only_service(args.manifest, args.asset_root, args.run_root, args.state_root)
    controller = PatchBuildController(service, game_root=args.game_root, ffmpeg=args.ffmpeg)
    payload = dict(revision=service.state['revision'], output_policy=args.output_policy)
    if args.output_root:payload['output_root'] = args.output_root
    if args.replace_output:payload['replace_output'] = args.replace_output
    controller.start(payload)
    controller.thread.join()
    job = controller.status()['job']
    if job['status'] != 'completed':
        parser.exit(1, 'Patchbuild fehlgeschlagen: ' + str(job['error']) + '\nBericht: ' + str(job['report_path']) + '\n')
    print(job['output_dir'])
    return 0


if __name__ == '__main__':
    main()
