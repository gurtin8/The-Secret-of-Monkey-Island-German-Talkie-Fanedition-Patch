"""Explicit local role choices; imports perform audio normalization, never TTS."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import wave

from .contracts import ContractError, canonical_json, digest, resolve_relative

FORMAT = 'mi1-launcher-voice-choices/1'


def required_roles(manifest):
    return {d['role_id'] for d in manifest['dialogues'] if d.get('processing') == 'render'}


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical_json(value) + b'\n')
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _semantic(choice):
    return {key: choice[key] for key in ('source', 'sha256', 'transcript', 'language', 'speech_confirmed')}


def verify_custom(root, role_id, choice):
    """Recheck retained bytes and metadata before each capture and render."""
    from manifest_renderer.audio import sha256_file
    if choice.get('source') != 'custom' or choice.get('speech_confirmed') is not True:
        raise ContractError('Mindestens drei Sekunden gesprochene Sprache müssen bestätigt werden')
    if choice.get('language') not in ('en', 'de') or not isinstance(choice.get('transcript'), str) or not choice['transcript'].strip():
        raise ContractError('Eigener Stimmanker benötigt Sprache und exaktes Transkript')
    try:
        audio = resolve_relative(root, choice['path'], must_exist=True)
        metadata = resolve_relative(root, choice['metadata_path'], must_exist=True)
        imports = (Path(root) / 'imports/custom-anchors').resolve()
        if not audio.is_relative_to(imports) or not metadata.is_relative_to(imports) or audio.parent != metadata.parent:
            raise ContractError('Eigener Stimmanker liegt außerhalb der lokalen Importe')
        if sha256_file(audio) != choice['sha256'] or sha256_file(metadata) != choice['metadata_sha256']:
            raise ContractError('Eigener Stimmanker oder Metadaten wurden verändert')
        data = json.loads(metadata.read_text(encoding='utf-8'))
        if (data.get('format') != 'mi1-custom-voice-anchor/1' or data.get('role') != role_id or
                any(data.get(k) != choice[k] for k in ('sha256', 'transcript', 'language')) or data.get('audio_path') != audio.name):
            raise ContractError('Importmetadaten passen nicht zum gewählten Stimmanker')
        with wave.open(str(audio), 'rb') as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 48000):
                raise ContractError('Stimmanker benötigt 48-kHz-Mono-PCM16-WAV')
            duration = wav.getnframes() / wav.getframerate()
        if not 3.0 <= duration <= 15.0 or data.get('duration_seconds') != duration or choice.get('duration_ms') != round(duration * 1000):
            raise ContractError('Stimmanker benötigt drei bis fünfzehn Sekunden Audio')
        if choice.get('semantic_sha256') != digest(_semantic(choice)):
            raise ContractError('Stimmankeridentität passt nicht zu Audio und Transkript')
    except (KeyError, OSError, ValueError, EOFError, wave.Error) as error:
        if isinstance(error, ContractError): raise
        raise ContractError('Lokaler Stimmanker ist unvollständig oder ungültig') from error
    return dict(path=choice['path'], sha256=choice['sha256'], media_type='audio/wav',
                duration_ms=choice['duration_ms'], sample_rate_hz=48000, channels=1)


def verify_choices(root, manifest, selected):
    if not isinstance(selected, dict) or selected.get('format') != FORMAT or not isinstance(selected.get('roles'), dict):
        raise ContractError('Explizite Stimmenauswahl fehlt')
    roles = selected['roles']; known = {r['role_id'] for r in manifest['roles']}
    if set(roles) - known: raise ContractError('Stimmenauswahl enthält unbekannte Rollen')
    for role_id in required_roles(manifest):
        choice = roles.get(role_id)
        if not isinstance(choice, dict) or choice.get('source') not in ('custom', 'original_demo'):
            raise ContractError('Stimme für Rolle noch nicht gewählt: ' + role_id)
        if choice['source'] == 'custom':
            verify_custom(root, role_id, choice)
            if choice.get('alternative') is not None: verify_custom(root, role_id, choice['alternative'])
        elif choice != {'source': 'original_demo'}:
            raise ContractError('Originalstimmen-Auswahl enthält fremde Ankerdaten')
    return copy.deepcopy(selected)


class VoiceChoices:
    def __init__(self, root, manifest, *, runner=None):
        self.root = Path(root).resolve(strict=True)
        self.manifest = manifest
        self.runner = runner
        self.path = self.root / '.launcher/voice-choices.json'
        if self.path.exists():
            self.selected = json.loads(self.path.read_text(encoding='utf-8'))
            if self.selected.get('format') != FORMAT or not isinstance(self.selected.get('roles'), dict):
                raise ContractError('Lokale Stimmenauswahl hat ein unbekanntes Format')
            known = {r['role_id'] for r in manifest['roles']}
            self.selected['roles'] = {rid: value for rid, value in self.selected['roles'].items() if rid in known}
        else:
            self.selected = {'format': FORMAT, 'roles': {}}

    def _role(self, role_id):
        role = next((r for r in self.manifest['roles'] if r['role_id'] == role_id), None)
        if role is None: raise ContractError('Unbekannte Rolle: ' + str(role_id))
        return role

    def _import(self, role_id, audio_path, transcript, language, speech_confirmed, name):
        from voice_anchor_wizard.custom_anchors import import_custom_anchor
        from manifest_renderer.audio import sha256_file
        if speech_confirmed is not True:
            raise ContractError('Mindestens drei Sekunden gesprochene Sprache müssen bestätigt werden')
        if not isinstance(transcript, str) or not isinstance(language, str):
            raise ContractError('Exaktes Transkript und Sprache fehlen')
        args = dict(ffmpeg=resolve_relative(self.root, 'bin/ffmpeg', must_exist=True),
                    ffprobe=resolve_relative(self.root, 'bin/ffprobe', must_exist=True), minimum_duration_seconds=3.0)
        if self.runner is not None: args['runner'] = self.runner
        record = import_custom_anchor(self.root / 'imports', role_id, audio_path,
                                      name or self._role(role_id).get('name', role_id), language, transcript, **args)
        choice = dict(source='custom', path=Path(record['audio_path']).relative_to(self.root).as_posix(),
                      metadata_path=Path(record['metadata_path']).relative_to(self.root).as_posix(),
                      metadata_sha256=sha256_file(Path(record['metadata_path'])), sha256=record['sha256'],
                      duration_ms=round(record['duration_seconds'] * 1000), transcript=transcript,
                      language=language, name=record['name'], speech_confirmed=True)
        choice['semantic_sha256'] = digest(_semantic(choice))
        verify_custom(self.root, role_id, choice)
        return choice

    def set_choice(self, role_id, *, source, audio_path=None, transcript=None, language=None, speech_confirmed=False, name=None):
        self._role(role_id)
        if source == 'original_demo': choice = {'source': source}
        elif source == 'custom': choice = self._import(role_id, audio_path, transcript, language, speech_confirmed, name)
        else: raise ContractError('Stimme muss eigener Anker oder Originalstimmen-Tech-Demo sein')
        self.selected['roles'][role_id] = choice
        _atomic(self.path, self.selected)
        return self.snapshot()

    def set_alternative(self, role_id, *, audio_path, transcript, language, speech_confirmed=False, name=None):
        self._role(role_id)
        primary = self.selected['roles'].get(role_id)
        if not primary or primary['source'] != 'custom': raise ContractError('Zweiter eigener Anker benötigt einen eigenen Hauptanker')
        verify_custom(self.root, role_id, primary)
        alternative = self._import(role_id, audio_path, transcript, language, speech_confirmed, name)
        if alternative['sha256'] == primary['sha256']:
            raise ContractError('Zweiter Anker muss eine andere Aufnahme derselben Stimme sein')
        primary['alternative'] = alternative
        _atomic(self.path, self.selected)
        return self.snapshot()

    def set_original_demo_all(self):
        for role_id in required_roles(self.manifest): self.selected['roles'][role_id] = {'source': 'original_demo'}
        _atomic(self.path, self.selected)
        return self.snapshot()

    def snapshot(self):
        rows = []; needed = required_roles(self.manifest)
        for role in self.manifest['roles']:
            rid = role['role_id']; choice = self.selected['roles'].get(rid); ready = bool(choice); error = None
            if choice:
                try: verify_choices(self.root, {'roles': [role], 'dialogues': [{'role_id': rid, 'processing': 'render'}]}, {'format': FORMAT, 'roles': {rid: choice}})
                except ContractError as exc: ready = False; error = str(exc)
            rows.append(dict(role_id=rid, name=role.get('name', rid), choice=copy.deepcopy(choice),
                             required=rid in needed, ready=ready, error=error))
        missing = [r['role_id'] for r in rows if r['required'] and not r['ready']]
        return dict(format=FORMAT, roles=rows, ready=not missing, missing_role_ids=missing)

    def capture(self):
        return verify_choices(self.root, self.manifest, self.selected)
