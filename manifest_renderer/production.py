"""Local MI1 production executor with manifest-bound provisional Audio QA v2."""
from __future__ import annotations

from collections import Counter
import copy
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote
import wave

from .audio import atomic_install, concatenate, normalize, resolve_manifest_path, sha256_file, validate_wav
from .audio_cache import AudioPreparationCache, pcm_info
from .backend import ANE_RUNTIME_ASSETS, bind_attempt, profile as backend_profile, profile_assets
from .manifest import canonical_json, load_manifest, resolve_manifest, sha256
from .plan import build_run_plan
from .runner import AttemptStore, ArtifactError, RenderOutputError, _exclusive_json
from .pipeline import run_pipeline, restart_needed
from .session import RenderRequest, SessionPool
from audio_qa_v1 import audit as qa1
from audio_qa_v2 import rescore as qa2
from validate_dubbing_manifest import validate

POLICY = 'mi1:audio-qa-v2-provisional-base-medium-async'
DEFAULT_HASH_LIMIT = 8 * 1024 * 1024
LEXICAL_REASONS = ('missing_content_words','short_text_mismatch','number_unverified','number_mismatch',
                   'low_expected_coverage','many_extra_words','asr_loop')
STT_PATHS = ('models/asr/ggml-base.bin','models/asr/ggml-medium.bin')
CASCADE = {'primary':'base','secondary':'medium','secondary_trigger':list(LEXICAL_REASONS),
           'final_text_decision':'medium_when_run','scheduling':'durable_async_single_qa_worker'}
THRESHOLDS = {'minimum_duration_ms': 150, 'short_words_max': 2, 'short_max_duration_ms': 15000,
              'long_words_min': 25, 'long_min_duration_ms': 500, 'warning_ms_per_word_min': 90,
              'warning_ms_per_word_max': 2200, 'warning_duration_floor_min_ms': 180,
              'warning_duration_floor_max_ms': 6000, 'reference_ratio_min': .4,
              'reference_ratio_max': 2.5}
DECODER = {'language': 'de', 'beam_size': 1, 'best_of': 1, 'temperature': 0,
           'temperature_increment': 0, 'temperature_fallback': False, 'vad': False,
           'prompt_supplied': False, 'threads': 4}
CODE_FILES = ('render_dubbing_manifest.py', 'validate_dubbing_manifest.py',
              'start_manifest_dubbing.command', 'prepare_plan07_recipe.py', 'run_plan07_hardware.py', 'tools/setup_ane_runtime.py',
              'docs/schema/dubbing-manifest-v1.schema.json', 'manifest_renderer/manifest.py',
              'manifest_renderer/plan.py', 'manifest_renderer/recipes.py', 'manifest_renderer/production.py', 'manifest_renderer/runner.py',
              'manifest_renderer/backend.py', 'audio_qa_v1/date_equivalence.py',
              'patch_review/__init__.py', 'patch_review/evidence.py', 'patch_review/stt_overlay.py', 'patch_review/findings.py',
              'patch_review/service.py', 'patch_review/http.py', 'patch_review/render.py', 'patch_review/__main__.py',
              'voice_anchor_wizard/text_rules.py', 'patch_review/wizard.py',
              'voice_anchor_wizard/app.py', 'patch_review/app.js', 'patch_review/styles.css', 'patch_review/index.html',
              'manifest_renderer/pipeline.py', 'manifest_renderer/adaptive_ane.py',
              'manifest_renderer/render_event_log.py', 'manifest_renderer/timing.py',
              'manifest_renderer/voxcpm2.py', 'manifest_renderer/plan.py',
              'manifest_renderer/hardware.py',
              'manifest_renderer/ane_worker.py', 'manifest_renderer/ane_worker_process.py',
              'manifest_renderer/audio.py', 'manifest_renderer/audio_cache.py',
              'manifest_renderer/session.py', 'audio_qa_v1/audit.py', 'audio_qa_v2/rescore.py')
CODE_FILES = (*CODE_FILES, 'manifest_renderer/json_memory.py', 'manifest_renderer/preparation_core.py', 'voice_anchor_wizard/anchor_selection.py', 'mi1_patcher.py',
              'patch_review/build.py', 'tools/patch_mi1_intro.py')
QA_RESUME_COMPATIBILITY_ID = 'whisper-json-qa-deferral-v2'
QA_RESUME_CHANGED_CODE = {'audio_qa_v1/audit.py', 'manifest_renderer/production.py',
                          'manifest_renderer/render_event_log.py', 'render_dubbing_manifest.py'}


def immutable_json(path: Path, value: dict):
    if path.exists():
        if path.is_symlink() or json.loads(path.read_text(encoding='utf-8')) != value:
            raise ArtifactError(f'Abweichendes bestehendes Artefakt: {path}')
        return
    _exclusive_json(path, value)


def write_resume_compatibility(run_root: Path, record: dict) -> Path:
    """Preserve earlier compatibility receipts while versioning a newer one."""
    path=run_root/'qa-resume-compatibility.json'
    if path.exists():
        try:
            previous=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,UnicodeError,json.JSONDecodeError) as exc:
            raise ArtifactError('Vorhandener QA-Resume-Kompatibilitätsbeleg ist unlesbar') from exc
        if previous!=record:
            version=record.get('compatibility_id','').rsplit('-v',1)
            if len(version)!=2 or not version[1].isdigit():
                raise ArtifactError('Neue QA-Resume-Kompatibilität benötigt eine versionierte Identität')
            path=run_root/f'qa-resume-compatibility-v{version[1]}.json'
    immutable_json(path,record)
    return path


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.summary-', dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def require_m1(root: Path):
    from .hardware import host_facts
    facts = host_facts(disk_path=root)
    if (facts['os_family'] != 'macos' or facts['architecture'] != 'arm64'
            or not (facts['chip'] or '').startswith('Apple ')
            or not str(root.resolve()).startswith('/Volumes/')):
        raise ValueError('Gesamtlauf benötigt Apple Silicon unter macOS auf dem externen Projektlaufwerk')


def validate_host_selection(backend: str, operating_mode: str, facts=None, *,
                            diagnostic_ane_single_low_ram: bool = False):
    """Fail before worker creation when the selected mode lacks a supported host."""
    from .hardware import host_facts
    facts = facts or host_facts()
    if facts.get('os_family') != 'macos' or facts.get('architecture') != 'arm64' or not (facts.get('chip') or '').startswith('Apple '):
        raise ValueError('Ausgewähltes Backend benötigt belegtes Apple Silicon unter macOS')
    if facts.get('system_ram_bytes') is None or facts.get('available_disk_bytes') is None:
        raise ValueError('RAM und freier Speicher müssen vor dem Start messbar sein')
    if diagnostic_ane_single_low_ram and (backend != 'ane' or operating_mode != 'single'):
        raise ValueError('Low-RAM-Diagnose ist nur für isoliertes ANE-Single zulässig')
    minimum_ram = 0 if operating_mode == 'adaptive_ane' else (8 * 1024 ** 3 if backend in {'gpu', 'cpu'} else 16 * 1024 ** 3)
    if backend == 'ane' and operating_mode == 'single' and diagnostic_ane_single_low_ram:
        minimum_ram = 0
    if facts['system_ram_bytes'] < minimum_ram:
        raise ValueError(f"{backend}-Profil benötigt mindestens {minimum_ram // 1024 ** 3} GiB RAM")
    if facts['available_disk_bytes'] < 20 * 1024 ** 3:
        raise ValueError('Mindestens 20 GiB freier Projektspeicher als vorläufiger Arbeitsbuffer erforderlich')
    if backend == 'ane' or operating_mode in {'parallel_gpu_ane', 'adaptive_gpu_ane'}:
        version = tuple(int(part) for part in (facts.get('os_version') or '').split('.')[:2] if part.isdigit())
        if version < (15, 0):
            raise ValueError('ANE benötigt laut Runtime-Metadaten macOS 15 oder neuer')
        if operating_mode != 'adaptive_ane' and facts['system_ram_bytes'] < 16 * 1024 ** 3 and not (
                backend == 'ane' and operating_mode == 'single' and diagnostic_ane_single_low_ram):
            raise ValueError('ANE/Parallelbetrieb benötigt mindestens 16 GiB bekannten Arbeitsspeicher')
    if operating_mode in {'parallel_gpu_ane', 'adaptive_gpu_ane'} and backend != 'gpu':
        raise ValueError('Parallelbetrieb muss über das GPU-Profil ausgewählt werden')
    return True


def validate_ane_runtime_installation():
    from importlib.metadata import PackageNotFoundError, version
    try:
        runtime_version = version('voxcpmane2')
        coreml_version = version('coremltools')
    except PackageNotFoundError as exc:
        raise ValueError('ANE-Runtime fehlt; tools/setup_ane_runtime.py --install-python-runtime ausführen und ANE-venv verwenden') from exc
    if runtime_version != '0.1.3b1' or coreml_version != '9.0':
        raise ValueError(f'ANE-Runtimeversion weicht ab: voxcpmane2={runtime_version}, coremltools={coreml_version}; Plan 06 verlangt 0.1.3b1/9.0')
    return True


def prepare_production(source: Path, root: Path, *, backend: str = 'gpu', operating_mode: str = 'single',
                       ane_worker_config: dict | None = None, full_integrity: bool = False,
                       routing_policy: str | None = None, timing=None) -> dict:
    """Derive runtime/QA configuration; preserve the user's export and all decisions."""
    original = load_manifest(source)['manifest']
    if original.get('manifest_profile') != 'local_render':
        raise ValueError('Dieser MI1-Starter benötigt das lokale Wizard-Rendermanifest')
    m = copy.deepcopy(original)
    root = root.resolve(strict=True)
    source_digest=sha256(canonical_json(original))
    receipt_index_started=time.monotonic()
    if timing:
        timing.event('saved_receipt_index',state='start',
                     counts={'source_dialogues':len(original.get('scope',{}).get('included_dialog_ids',[]))})
    receipts: dict[str, set[str]] = {}
    def index_receipts(value):
        if isinstance(value,dict):
            relative=value.get('path'); digest=value.get('sha256')
            if isinstance(relative,str) and isinstance(digest,str):
                receipts.setdefault(relative,set()).add(digest)
            for child in value.values(): index_receipts(child)
        elif isinstance(value,list):
            for child in value: index_receipts(child)
    index_receipts(original)
    # Renderer-produced STT and code receipts are reusable only when tied to
    # this exact source export. Index prior run manifests once, not once/asset.
    previous_root=root/'output/manifest-runs'
    receipt_sources_scanned=0
    if previous_root.is_dir():
        for previous in previous_root.glob('*/manifest.json'):
            receipt_sources_scanned+=1
            if previous.is_symlink(): continue
            try: old=json.loads(previous.read_text(encoding='utf-8'))
            except (OSError,ValueError): continue
            evidence=old.get('source_evidence',{}).get('production',{})
            if evidence.get('source_manifest_sha256')!=source_digest: continue
            index_receipts(evidence.get('stt_cli',{}))
            index_receipts(evidence.get('code_files',[]))
            index_receipts(old.get('models',{}).get('stt',{}).get('files',[]))
            index_receipts(old.get('render_execution',{}).get('assets',[]))
            # The VoxCPM CLI receipt is stored in runtime.artifacts, not in
            # source_evidence.production. Reuse it only from runs tied to this
            # exact source export, alongside the STT and profile receipts.
            index_receipts(old.get('runtime',{}).get('artifacts',{}))
    if timing:
        timing.event('saved_receipt_index',state='end',
            counts={'unique_paths':len(receipts),'receipt_sources_scanned':receipt_sources_scanned},
            phase_elapsed_seconds=round(time.monotonic()-receipt_index_started,6))

    def receipt_hash(relative):
        matches=receipts.get(relative,set())
        return next(iter(matches)) if len(matches)==1 else None
    asset_counters={'lookups':0,'saved_receipts':0,'full_content_hashes':0}
    def asset(relative):
        asset_counters['lookups']+=1
        p = resolve_manifest_path(root, relative)
        saved=receipt_hash(relative)
        if full_integrity:
            asset_counters['full_content_hashes']+=1
            digest=sha256_file(p)
            if saved is not None and digest != saved:
                raise ValueError(f'Inhalts-Hash weicht vom gespeicherten Beleg ab: {relative}')
        elif relative in CODE_FILES or relative in {
                'bin/voxcpm2_cli_speed', 'work/whisper.cpp/build/bin/whisper-cli'}:
            # Code and executable identities can change independently of the
            # immutable source export. Hash these small files directly so a
            # new renderer revision never inherits an old code receipt.
            if p.stat().st_size <= DEFAULT_HASH_LIMIT:
                digest=sha256_file(p)
                asset_counters['small_file_hashes']=asset_counters.get('small_file_hashes',0)+1
            elif saved is not None:
                digest=saved
                asset_counters['saved_receipts']+=1
            else:
                raise ValueError(f'Große Code-/Executable-Datei ohne gespeicherten Hashbeleg: {relative}')
        else:
            digest=saved
            if digest is None:
                raise ValueError(f'Asset ohne eindeutigen gespeicherten Hashbeleg: {relative}; einmalig --full-integrity-check ausführen')
            asset_counters['saved_receipts']+=1
        return {'path': relative, 'sha256': digest, 'media_type': 'application/octet-stream'}
    selected = backend_profile(backend, operating_mode=operating_mode)
    if routing_policy is not None:
        if routing_policy not in {'free-cue-v1','fixed-character-v1'}:
            raise ValueError(f'Unbekannte Routingpolicy: {routing_policy}')
        if routing_policy == 'free-cue-v1' and operating_mode != 'parallel_gpu_ane':
            raise ValueError('Freie Cuevergabe benötigt den parallelen GPU+ANE-Modus')
        selected['routing_policy'] = routing_policy
    if selected['profile_id']=='ane' or selected['operating_mode'] in {'parallel_gpu_ane', 'adaptive_gpu_ane'}:
        if full_integrity:
            selected['assets'] = profile_assets(root, selected)
            asset_counters['full_content_hashes']+=len(selected['assets'])
        else:
            profile_receipts=[]
            for relative in ANE_RUNTIME_ASSETS:
                target=resolve_manifest_path(root,relative)
                if not target.is_file(): raise ValueError(f'ANE-Profilasset fehlt: {relative}')
                digest=receipt_hash(relative)
                if digest is None:
                    raise ValueError(f'Backendprofilasset ohne eindeutigen gespeicherten Hashbeleg: {relative}; einmalig --full-integrity-check ausführen')
                profile_receipts.append({'path':relative,'sha256':digest})
                asset_counters['saved_receipts']+=1
            selected['assets']=profile_receipts
    else:
        selected['assets']=[]
    if ane_worker_config is not None:
        if backend != 'ane' and not (backend == 'gpu' and operating_mode in {'parallel_gpu_ane', 'adaptive_gpu_ane'}):
            raise ValueError('ANE-Worker-Konfiguration benötigt ANE oder den parallelen GPU+ANE-Modus')
        selected['ane_worker'] = copy.deepcopy(ane_worker_config)
    cli = asset('bin/voxcpm2_cli_speed') if backend in {'gpu', 'cpu'} or operating_mode in {'parallel_gpu_ane', 'adaptive_gpu_ane'} else None
    stt_cli = asset('work/whisper.cpp/build/bin/whisper-cli')
    stt_models = [asset(path) for path in STT_PATHS]
    code = [asset(name) for name in CODE_FILES]
    if backend in {'gpu', 'cpu'}:
        tts = m['models']['tts']
        selected['model_identity'] = {'model_id': tts['model_id'], 'version': tts['version'],
                                      'assets_sha256': sha256(canonical_json(tts['files'])),
                                      'weights_present': True}
        if ane_worker_config is not None:
            preview=json.loads(resolve_manifest_path(root, 'generator_launcher/provenance/models/preview.json').read_text(encoding='utf-8'))
            debug=json.loads(resolve_manifest_path(root, 'generator_launcher/provenance/models/fallback.json').read_text(encoding='utf-8'))
            tokenizer=json.loads(resolve_manifest_path(root, 'generator_launcher/provenance/models/tokenizer.json').read_text(encoding='utf-8'))
            revisions={'model_id':preview['id'],'version':preview['sha'],'fallback_model_id':debug['id'],
                       'fallback_version':debug['sha'],'tokenizer_version':tokenizer['sha']}
            if ane_worker_config.get('model_revisions') != revisions:
                raise ValueError('ANE-Modellpfade müssen an Preview-, Split- und Tokenizerrevision gebunden sein')
            selected['model_identity']['parallel_ane']={**revisions,'weights_present':True,
                'component_receipts_sha256':sha256(canonical_json(ane_worker_config))}
    else:
        preview=json.loads(resolve_manifest_path(root, 'generator_launcher/provenance/models/preview.json').read_text(encoding='utf-8'))
        debug=json.loads(resolve_manifest_path(root, 'generator_launcher/provenance/models/fallback.json').read_text(encoding='utf-8'))
        tokenizer=json.loads(resolve_manifest_path(root, 'generator_launcher/provenance/models/tokenizer.json').read_text(encoding='utf-8'))
        selected['model_identity'] = {'model_id': preview['id'], 'version': preview['sha'],
            'fallback_model_id': debug['id'], 'fallback_version': debug['sha'],
            'tokenizer_version': tokenizer['sha'], 'weights_present': False}
        if ane_worker_config is not None:
            expected_revisions={key:selected['model_identity'][key] for key in
                ('model_id','version','fallback_model_id','fallback_version','tokenizer_version')}
            if ane_worker_config.get('model_revisions') != expected_revisions:
                raise ValueError('ANE-Modellpfade müssen an Preview-, Split- und Tokenizerrevision gebunden sein')
            selected['model_identity']['weights_present'] = True
            selected['model_identity']['component_receipts_sha256'] = sha256(canonical_json(ane_worker_config))
    m['runtime']['artifacts'] = {'ffmpeg': m['runtime']['artifacts']['ffmpeg']}
    if cli:
        m['runtime']['artifacts'].update(isolated_cli=cli, combined_cli=cli)
    m['runtime']['backend'] = {'gpu': 'metal', 'cpu': 'cpu', 'ane': 'ane'}[backend]
    if backend == 'ane':
        m['runtime']['runtime_id'] = 'voxcpmane2:0.1.3b1'
    m['render_execution'] = selected
    m['runtime']['runtime_version'] = 'sha256:' + sha256(canonical_json(
        {'profile': selected, 'tts': cli, 'stt': stt_cli, 'code': code}))
    if m['runtime']['tts_parameters']:
        # No silently ignored parameters from an unrelated future runtime profile.
        raise ValueError('Nichtleere TTS-Parameter benötigen eine ausdrücklich unterstützte Runtimekonfiguration')
    m['models']['stt'] = {'model_id': 'mi1:whisper-base-medium', 'component_id': 'mi1:whisper-base-medium-models',
                          'version': 'sha256:' + sha256(canonical_json(stt_models)), 'files': stt_models}
    qa_code = [entry for entry in code if entry['path'].startswith('audio_qa_')]
    m['qa'] = {'status': 'configured', 'policy_id': POLICY,
               'policy_version': qa2.VERSION + ':sha256:' + sha256(canonical_json(qa_code)),
               'normalization_version': 'nfkc-casefold-sharp-s-named-date-v2',
               'decoder_parameters': DECODER, 'thresholds': THRESHOLDS}
    m['source_evidence']['production'] = {'source_manifest_sha256': source_digest,
                                          'stt_cli': stt_cli, 'code_files': code,
                                          'stt_cascade':CASCADE,
                                          'backend_profile': selected,
                                          'integrity_policy': {'mode':'full' if full_integrity else 'fast',
                                              'max_hash_bytes':None if full_integrity else DEFAULT_HASH_LIMIT,
                                              'large_hashes_skipped':not full_integrity},
                                          'qa_is_provisional': True, 'game_patch_built': False}
    identity = sha256(canonical_json(m))
    m['manifest_revision'] = int(identity[:12], 16)
    m['output']['root'] = 'output/manifest-runs/' + identity
    m['output']['render_report'] = 'render-report.json'
    m['output']['qa_summary'] = 'qa-summary.json'
    resolve_manifest(m)
    if timing:
        timing.event('asset_receipt_resolution',counts={**asset_counters,
            'unique_manifest_receipts':len(receipts),'full_integrity':full_integrity})
    return m


class ManifestAuditor:
    def __init__(self, root: Path, manifest: dict, run_root: Path, stt=qa1.run_stt, *, asset_root: Path | None = None):
        self.root, self.manifest, self.run_root = root, manifest, run_root
        self.asset_root = asset_root or root
        self.stop_event=threading.Event()
        self.stt=(lambda *args:qa1.run_stt(*args,cancel_event=self.stop_event)) if stt is qa1.run_stt else stt
        self.dialogues = {row['dialog_id']: row for row in manifest['dialogues']}
        self.cli = resolve_manifest_path(self.asset_root, manifest['source_evidence']['production']['stt_cli']['path'])
        self.models={name:receipt for name,receipt in zip(('base','medium'),manifest['models']['stt']['files'])}
        self.progress_callback=None
        self.qa_deferrals=self._load_qa_deferrals()
        self.used_qa_deferrals=set()

    def _load_qa_deferrals(self) -> dict[str,dict]:
        path=self.run_root/'qa-open-deferrals.json'
        if not path.exists():return {}
        if path.is_symlink() or not path.is_file():
            raise ArtifactError('QA-Deferral muss eine reguläre Run-Datei sein')
        def unique_pairs(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError(f'duplicate JSON key: {key}')
                result[key]=value
            return result
        try:
            control=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=unique_pairs)
            progress=json.loads((self.run_root/'progress.json').read_text(encoding='utf-8'))
        except (OSError,UnicodeError,ValueError,json.JSONDecodeError) as exc:
            raise ArtifactError('QA-Deferral oder zugehöriger Fortschritt ist unlesbar') from exc
        expected_run=resolve_manifest_path(self.root,self.manifest['output']['root'])
        manifest_hash=sha256(canonical_json(self.manifest))
        if (expected_run.resolve(strict=True)!=self.run_root.resolve(strict=True) or
                control.get('schema_version')!=1 or control.get('policy_id')!='exact-attempt-qa-deferral-v1' or
                control.get('run_root')!=self.manifest['output']['root'] or
                control.get('manifest_sha256')!=manifest_hash or
                control.get('run_plan_sha256')!=progress.get('run_plan_sha256') or
                progress.get('manifest_sha256')!=manifest_hash or
                progress.get('status') not in {'interrupted','running'}):
            raise ArtifactError('QA-Deferral gehört nicht exakt zu diesem unterbrochenen Run')
        if not (self.run_root/'.run.lock').is_file():
            raise ArtifactError('QA-Deferral darf nur innerhalb eines gesperrten Resume-Laufs angewendet werden')
        entries=control.get('deferrals')
        if set(control)!={'schema_version','policy_id','run_root','manifest_sha256','run_plan_sha256',
                          'source_manifest_sha256','authorization','deferrals'} or not isinstance(entries,list) or len(entries)!=1:
            raise ArtifactError('QA-Deferral muss genau eine explizite Cue-/Attempt-Ausnahme enthalten')
        if control.get('source_manifest_sha256')!=self.manifest['source_evidence']['production']['source_manifest_sha256']:
            raise ArtifactError('QA-Deferral Quellmanifest weicht ab')
        entry=entries[0]
        required={'dialog_id','attempt_fingerprint','role_id','variant_id','original_text','display_text',
                  'tts_text','qa_expected_text','wav_path','wav_sha256','wav_size_bytes','status',
                  'qa_status','stt_status','manual_review_required','reason_code','reason','failure',
                  'prior_failure','user_action'}
        if not isinstance(entry,dict) or set(entry)!=required:
            raise ArtifactError('QA-Deferral enthält unbekannte oder fehlende Pflichtfelder')
        if (entry.get('status')!='open' or entry.get('qa_status')!='warn' or
                entry.get('stt_status')!='not_run' or entry.get('manual_review_required') is not True or
                entry.get('reason_code')!='whisper_json_invalid_utf8' or
                entry.get('user_action')!='manual-review-required-before-release'):
            raise ArtifactError('QA-Deferral darf QA nicht als bestanden markieren')
        failure=entry.get('failure',{})
        if (not isinstance(failure,dict) or failure.get('source')!='whisper-json' or
                failure.get('error_type')!='UnicodeDecodeError' or failure.get('byte_hex')!='0xc3' or
                failure.get('byte_offset')!=1038 or failure.get('phase')!='whisper_json_decode'):
            raise ArtifactError('QA-Deferral enthält keinen belegten Whisper-JSON-UTF-8-Fehler')
        prior_failure=entry.get('prior_failure',{})
        if (not isinstance(prior_failure,dict) or prior_failure.get('source')!='whisper-stderr' or
                prior_failure.get('error_type')!='UnicodeDecodeError' or prior_failure.get('byte_hex')!='0xc3'):
            raise ArtifactError('QA-Deferral enthält keinen Beleg zur vorherigen Diagnostikstörung')
        source=self.dialogues.get(entry['dialog_id'])
        if source is None or source.get('role_id')!=entry['role_id']:
            raise ArtifactError('QA-Deferral Dialog oder Rolle weicht vom Runmanifest ab')
        variant=next((v for v in source.get('text_variants',[])
                      if v.get('variant_id')==source.get('selected_variant_id')),None)
        if (variant is None or variant.get('variant_id')!=entry['variant_id'] or
                source.get('original_text',{}).get('text')!=entry['original_text'] or
                variant.get('display_text')!=entry['display_text'] or
                variant.get('tts_text')!=entry['tts_text'] or
                variant.get('qa_expected_text')!=entry['qa_expected_text']):
            raise ArtifactError('QA-Deferral Textbelege weichen vom gespeicherten Manifest ab')
        return {entry['attempt_fingerprint']:entry}

    def cancel(self):
        self.stop_event.set()

    def recognize(self, attempt, wav, record, name):
        if self.progress_callback is not None:
            stage='qa_medium' if name=='medium' else 'qa'
            detail='Whisper-Medium prüft den WAV-Text.' if name=='medium' else 'Whisper-Base prüft den WAV-Text.'
            self.progress_callback(stage,detail)
        model=self.models[name]
        path=resolve_manifest_path(self.asset_root,model['path'])
        binding={'attempt_fingerprint':attempt['fingerprint'],'wav_sha256':record['wav_sha256'],
                 'model_sha256':model['sha256'],'decoder':record['decoder'],'qa_policy':record['qa_policy'],
                 'qa_expected_text':record['qa_expected_text'],'accepted_forms':record['accepted_forms']}
        target=resolve_manifest_path(self.root,self.manifest['output']['root']+'/stt/'+attempt['fingerprint']+'/'+name+'.json')
        if target.exists():
            try:
                prior=json.loads(target.read_text(encoding='utf-8'))
                if (target.is_symlink() or prior.get('binding')!=binding or
                        prior.get('record_sha256')!=sha256(canonical_json(prior['record']))):
                    raise ValueError('STT binding or record hash differs')
            except (OSError,ValueError,KeyError) as exc:
                raise ArtifactError('Abweichender STT-Stufenreport; wird nicht überschrieben') from exc
            return prior['record']
        if self.stop_event.is_set():raise InterruptedError('STT abgebrochen')
        started=time.perf_counter()
        heard=self.stt(self.cli,path,wav,self.run_root,record['decoder']['threads'])
        result={**record,'transcript':heard,'model_sha256':model['sha256'],'stt_model':name,
                'stt_status':'pass','stt_seconds':time.perf_counter()-started}
        result.update(qa1.compare(record['accepted_forms'],heard))
        result=qa2.rescore(result)
        immutable_json(target,{'binding':binding,'record':result,'record_sha256':sha256(canonical_json(result))})
        return result

    def __call__(self, attempt: dict, wav: Path) -> dict:
        row = self.dialogues[attempt['dialog_id']]
        expected = attempt['qa_expected_text']
        forms = [expected, *attempt['qa_allowed_variants']]
        record = {'render_status': 'ok', 'content_type': attempt['content_type'],
                  'technical_status': 'not_run', 'duration_status': 'not_run', 'stt_status': 'not_run',
                  'qa_expected_text': expected, 'accepted_forms': forms, 'transcript': '',
                  'reference_duration_ms': row['original_audio'].get('duration_ms', 0),
                  'model_sha256': self.models['base']['sha256'], 'decoder': attempt['qa']['decoder_parameters'],
                  'qa_policy': attempt['qa'], 'wav_sha256': sha256_file(wav)}
        if attempt.get('render_execution') is not None:
            record['render_execution'] = attempt['render_execution']
        try:
            pcm_info(wav)
            metrics = qa1.wav_info(wav)
        except (ValueError, OSError, wave.Error, EOFError) as exc:
            record.update(render_status='error',technical_status='fail', qa_status='fail',
                          reason_codes=['technical_invalid'],error=str(exc))
            return record
        record.update(audio_metrics=metrics, output_duration_ms=metrics['duration_ms'],
                      technical_status='warn' if metrics['clipping'] else 'pass')
        if record['content_type'] == 'speech' and metrics['silence']:
            record['technical_status'] = 'fail'
        initial=qa2.rescore(record)
        if record['content_type'] == 'speech' and initial['qa_status'] != 'fail':
            deferred=self.qa_deferrals.get(attempt['fingerprint'])
            if deferred is not None:
                actual_hash=sha256_file(wav)
                actual_size=wav.stat().st_size
                if (deferred['dialog_id']!=attempt['dialog_id'] or
                        deferred['role_id']!=row.get('role_id') or
                        deferred['qa_expected_text']!=expected or
                        deferred['wav_path']!=attempt['artifacts']['wav'] or
                        deferred['wav_sha256']!=actual_hash or deferred['wav_size_bytes']!=actual_size):
                    raise ArtifactError('QA-Deferral Attempt-/Solltext-/WAV-Beleg weicht ab')
                self.used_qa_deferrals.add(attempt['fingerprint'])
                reasons=[r for r in initial.get('reason_codes',[]) if r!='stt_unavailable']
                reasons.append('stt_deferred_manual_review')
                return {**initial,'qa_status':'warn','stt_status':'not_run',
                        'reason_codes':list(dict.fromkeys(reasons)),
                        'qa_completion_status':'deferred_manual_review','manual_review_required':True,
                        'qa_deferral_reason':deferred['reason'],
                        'stt_stages':[],'stt_cascade':CASCADE,'medium_checked':False}
            base=self.recognize(attempt,wav,record,'base')
            stages=[base]
            if set(base['reason_codes']) & set(LEXICAL_REASONS):
                stages.append(self.recognize(attempt,wav,record,'medium'))
            chosen=stages[-1]
            return {**chosen,'stt_stages':stages,'stt_cascade':CASCADE,
                    'medium_checked':len(stages)==2}
        # Reuse the existing conservative policy: lexical differences warn;
        # technical failure/extreme duration fail. No new acceptance thresholds.
        return {**initial,'stt_stages':[],'medium_checked':False}


class ManifestExecutor:
    def __init__(self, root: Path, manifest: dict, sessions: SessionPool, auditor, *, ane_worker=None, asset_root: Path | None = None):
        self.root, self.manifest, self.sessions, self.auditor = root, manifest, sessions, auditor
        self.asset_root = asset_root or root
        self.ffmpeg = self.asset(manifest['runtime']['artifacts']['ffmpeg'])
        self.cache = AudioPreparationCache(root/'cache/audio_preparation_v1', self.ffmpeg)
        self.models = [resolve_manifest_path(self.asset_root, a['path']) for a in manifest['models']['tts']['files']]
        self.ane_worker = ane_worker
        self.instance_id = getattr(sessions.instance_context, 'instance_id', None) if hasattr(sessions,'instance_context') else None
        if self.ane_worker is not None and not self.instance_id and hasattr(self.sessions,'set_ane_worker'):
            self.sessions.set_ane_worker(self.ane_worker)
        if self.instance_id:
            self.cache = AudioPreparationCache(root/'cache/audio_preparation_v1'/self.instance_id, self.ffmpeg)
        selected = manifest.get('render_execution', backend_profile('gpu'))
        if selected['profile_id'] == 'ane' and selected.get('ane_worker') and self.ane_worker is None and not self.instance_id:
            bindings = selected.get('ane_worker')
            if not bindings:
                raise ValueError('ANE-Ausführung benötigt extern installierte, hashgebundene Modellpfade (--ane-worker-config)')
            identity = selected.get('model_identity', {}) if selected.get('profile_id') == 'ane' else selected.get('model_identity', {}).get('parallel_ane', {})
            expected = identity.get('component_receipts_sha256')
            if not identity.get('weights_present') or expected != sha256(canonical_json(bindings)):
                raise ValueError('ANE-Modellkomponenten sind nicht an die Profilidentität gebunden')
            from .ane_worker_process import AneWorkerProcess
            self.ane_worker = AneWorkerProcess(bindings,
                logs_dir=resolve_manifest_path(root,manifest['output']['root']+'/session-tmp'),
                cwd=root, verify_hashes=self.manifest['source_evidence']['production'].get('integrity_policy',{}).get('mode')=='full')
            if hasattr(self.sessions,'set_ane_worker'):
                self.sessions.set_ane_worker(self.ane_worker)

    def asset(self, receipt: dict) -> Path:
        asset_root = self.root if receipt['path'].startswith('portable-references/') else self.asset_root
        try: path = resolve_manifest_path(asset_root, receipt['path'])
        except ValueError as exc: raise ArtifactError(str(exc)) from exc
        policy=self.manifest['source_evidence']['production'].get('integrity_policy',{})
        if (policy.get('mode')=='full' or path.stat().st_size <= DEFAULT_HASH_LIMIT) and sha256_file(path) != receipt['sha256']:
            raise ArtifactError(f'Audio/Runtime-Datei verändert: {path}')
        return path

    def convert(self, source: Path, target: Path, legacy=False):
        if legacy:
            def producer(out):
                subprocess.run([str(self.ffmpeg), '-nostdin', '-loglevel', 'error', '-i', str(source),
                                '-ac', '1', '-c:a', 'pcm_s16le', str(out)], check=True, shell=False)
        else:
            producer = lambda out: normalize(self.ffmpeg, source, out)
        self.cache.prepare('legacy-mono-source-rate' if legacy else 'normalize-48000', [source], target,
                           producer, rate=None if legacy else 48000)

    def prewarm(self, attempt: dict, descriptor: dict, original: Path) -> Path:
        paths = {key: resolve_manifest_path(self.root, value) for key, value in descriptor['artifacts'].items()}
        output, report, log = paths['wav'], paths['report'], paths['log']
        if output.exists() or report.exists() or log.exists():
            if not (output.is_file() and report.is_file() and log.is_file()):
                raise ArtifactError('Zeilen-Prewarm unvollständig; keine Ersatzreferenz')
            prior = json.loads(report.read_text())
            if (prior.get('generation') != descriptor or prior.get('status') != 'ok'
                    or prior.get('wav_sha256') != sha256_file(output) or prior.get('log_sha256') != sha256_file(log)):
                raise ArtifactError('Zeilen-Prewarm fehlgeschlagen/verändert; keine Ersatzreferenz')
            info = pcm_info(output)
            duration = round(info['frames']*1000/info['sample_rate_hz'])
            if not 2000 <= duration <= 5000:
                raise RenderOutputError('Zeilen-Prewarm liegt außerhalb 2–5 Sekunden')
            return output
        if descriptor['target_duration_ms'] != [3000,4000] or descriptor['reference_mode'] != 'line_original':
            raise ValueError('Unzulässiger zeilenweiser Prewarm')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.prewarm-', dir=output.parent) as folder:
            folder=Path(folder); reference=folder/'reference.wav'; generated=folder/'output.wav'; tmp_log=folder/'log'
            self.convert(original, reference, legacy=True)
            selected=attempt.get('render_execution') or self.manifest.get('render_execution') or backend_profile('gpu')
            if selected['profile_id'] == 'ane':
                with tmp_log.open('w',encoding='utf-8') as stream:
                    timing=self._run_ane(descriptor['text'], reference, None, None, descriptor['seed'],
                                         generated, stream, selected, mode='prewarm',
                                         fingerprint=attempt['fingerprint'])
                info=self.validate_render_output(generated)
                atomic_install(generated,output); atomic_install(tmp_log,log)
                immutable_json(report,{'status':'ok','generation':descriptor,
                    'wav_sha256':info['sha256'],'log_sha256':sha256_file(log),
                    'execution':timing,'render_execution':attempt.get('render_execution'),
                    'duration_ms':info['duration_ms']})
                return output
            cli=self.asset(attempt['runtime']['artifacts']['isolated_cli'])
            params=selected['effective_parameters']
            request=RenderRequest(descriptor['text'],str(reference),str(generated),descriptor['seed'],
                                  steps=params.get('steps',200),timesteps=params.get('timesteps',10),
                                  cfg=params.get('cfg',2.0),temperature=params.get('temperature',1.0))
            backend=selected['profile_id']
            cmd=request.argv(str(cli),tuple(map(str,self.models)))
            with tmp_log.open('w',encoding='utf-8') as stream:
                timing=self.sessions.run(cmd,stream,backend=backend)
            info=self.validate_render_output(generated); good=2000 <= info['duration_ms'] <= 5000
            atomic_install(generated,output); atomic_install(tmp_log,log)
            immutable_json(report,{'status':'ok' if good else 'error','generation':descriptor,
                                    'wav_sha256':info['sha256'],'log_sha256':sha256_file(log),
                                    'execution':timing,'render_execution':attempt.get('render_execution'),
                                    'duration_ms':info['duration_ms']})
            if not good:raise RenderOutputError('Zeilen-Prewarm außerhalb 2–5 Sekunden; WAV/Log erhalten')
            return output

    @staticmethod
    def validate_render_output(path):
        try: return validate_wav(path)
        except (ValueError,EOFError,wave.Error,FileNotFoundError) as exc:
            raise RenderOutputError(str(exc)) from exc

    def _ensure_ane_worker(self,selected):
        def resource_guard():
            if self.sessions.resource_stop is not None and self.sessions.resource_stop.is_set():
                from .adaptive_ane import ResourceGuardError
                raise ResourceGuardError('resource_guard_critical: ANE request interrupted')
        cold_start = self.ane_worker is None
        if self.ane_worker is None:
            bindings = selected.get('ane_worker')
            identity = selected.get('model_identity', {})
            if (not bindings or not identity.get('weights_present') or
                    identity.get('component_receipts_sha256') != sha256(canonical_json(bindings))):
                raise ValueError('ANE-Worker ist nicht mit hashgebundenen Modellpfaden konfiguriert')
            from .ane_worker_process import AneWorkerProcess
            if self.instance_id:
                bindings = dict(bindings)
                cache_dir = resolve_manifest_path(self.root,self.manifest['output']['root']+'/instance-cache/'+self.instance_id)
                cache_dir.mkdir(parents=True,exist_ok=True)
                bindings['cache_dir'] = str(cache_dir)
            try:
                self.ane_worker = AneWorkerProcess(bindings,
                    logs_dir=resolve_manifest_path(self.root,self.manifest['output']['root']+'/session-tmp'+('/'+self.instance_id if self.instance_id else '')),
                    cwd=self.root, verify_hashes=self.manifest['source_evidence']['production'].get('integrity_policy',{}).get('mode')=='full',
                    resource_guard=resource_guard if self.instance_id else None)
            except (RuntimeError, TimeoutError) as exc:
                from .pipeline import isolatable_failure
                if not isolatable_failure(exc): raise
                raise ArtifactError('ANE-Worker konnte nicht startbereit werden; Lauf bleibt fortsetzbar: '+str(exc)) from exc
            if self.instance_id: self.sessions.attach_ane_instance(self.instance_id,self.ane_worker)
            else: self.sessions.set_ane_worker(self.ane_worker)
        return cold_start

    def preload(self):
        """Load the bound ANE models and await READY without synthesizing speech."""
        selected=self.manifest['render_execution']
        if selected['profile_id']!='ane':raise ValueError('ANE preload requires the ANE profile')
        return {'cold_start':self._ensure_ane_worker(selected),'backend':'coreml_cpu_and_ne'}

    def _run_ane(self, text, reference, prompt, prompt_text, seed, output, stream, selected, *, mode,
                 fingerprint=''):
        cold_start=self._ensure_ane_worker(selected)
        request_id = f"{time.time_ns()}"
        report = output.with_suffix('.ane.json')
        request = {'request_id':request_id,'attempt_fingerprint':fingerprint,
                   'text':text,'reference_path':str(reference) if reference else None,
                   'reference_sha256':sha256_file(reference) if reference else None,
                   'prompt_path':str(prompt) if prompt else None,
                   'prompt_sha256':sha256_file(prompt) if prompt else None,
                   'prompt_text':prompt_text or '', 'output_path':str(output),
                   'log_path':str(report),'seed':seed,'mode':mode,
                   'parameters':{},
                   'model_identity':selected.get('model_identity'),
                   'runtime_identity':{'contract':selected.get('runtime_contract'),
                                       'assets':selected.get('assets',[])}}
        started=time.perf_counter()
        try:
            result=self.ane_worker.run(request)
        except BaseException:
            if report.exists():
                report_text=report.read_text(encoding='utf-8')
                try:
                    evidence=json.loads(report_text)
                    partial=Path(evidence.get('partial_path') or '')
                    if partial.is_file() and not partial.is_symlink():
                        failure_root=resolve_manifest_path(self.root,self.manifest['output']['root'])/'failures'/fingerprint
                        failure_root.mkdir(parents=True,exist_ok=True)
                        folder=Path(tempfile.mkdtemp(prefix='ane-partial-',dir=failure_root))
                        shutil.copy2(partial,folder/'partial.wav')
                        immutable_json(folder/'partial.json',{'attempt_fingerprint':fingerprint,
                            'worker_report':evidence,'partial_wav_sha256':sha256_file(folder/'partial.wav')})
                except (OSError,ValueError,TypeError):
                    pass
                stream.write(report_text); report.unlink()
            else:
                stream.write(json.dumps({'status':'error','backend':'coreml_cpu_and_ne',
                                         'request_id':request_id})+'\n')
            self.release_ane_worker()
            raise
        stream.write(report.read_text(encoding='utf-8'))
        report.unlink()
        return {**result,'duration_seconds':time.perf_counter()-started,
                'backend':'coreml_cpu_and_ne', **({'cold_start':cold_start} if self.instance_id else {})}

    def release_ane_worker(self):
        worker, self.ane_worker = self.ane_worker, None
        if worker is not None:
            detach=getattr(self.sessions,'detach_ane_worker',None)
            if detach: detach(worker)
            close=getattr(worker,'close',None)
            if close: close()

    def pending_path(self, attempt: dict) -> Path:
        return resolve_manifest_path(self.root,self.manifest['output']['root']+'/pending/'+attempt['fingerprint'])

    def read_pending(self, attempt: dict):
        pending = self.pending_path(attempt)
        if not pending.exists():
            return None
        for name in ('render.json','output.wav','render.log'):
            artifact=pending/name
            if artifact.is_symlink() or not artifact.is_file():
                raise ArtifactError('Ausstehender QA-Versuch enthält keine regulären vollständigen Dateien')
        try:
            record = json.loads((pending/'render.json').read_text(encoding='utf-8'))
            if (record['fingerprint'] != attempt['fingerprint'] or record['wav_sha256'] != sha256_file(pending/'output.wav')
                    or record['log_sha256'] != sha256_file(pending/'render.log')):
                raise ValueError('hash mismatch')
        except (OSError, ValueError, KeyError) as exc:
            raise ArtifactError('Ausstehender QA-Versuch beschädigt; keine Datei wird überschrieben') from exc
        return record

    def stage(self, attempt: dict):
        parent=self.pending_path(attempt).parent
        parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.synthesis-',dir=parent) as folder:
            folder=Path(folder)
            began=time.monotonic()
            result=self.synthesize(attempt,folder/'output.wav',folder/'render.log')
            elapsed=time.monotonic()-began
            with wave.open(str(folder/'output.wav'),'rb') as audio:
                duration=audio.getnframes()*1000/audio.getframerate()
            return {**result,'output_duration_ms':duration,'synthesis_duration_seconds':elapsed}

    def audit_pending(self, attempt: dict, output: Path, log: Path):
        # Only reads the published synthesis. It never invokes a TTS session.
        record=self.read_pending(attempt)
        if record is None:raise ArtifactError('Keine dauerhaft gespeicherte WAV zur QA')
        pending=self.pending_path(attempt)
        shutil.copyfile(pending/'output.wav',output);shutil.copyfile(pending/'render.log',log)
        try:
            result={**self.auditor(attempt,output),'execution':record['execution'],'preparation':record['preparation']}
            if record.get('render_execution') is not None: result['render_execution']=record['render_execution']
            return result
        except Exception as exc:
            self.preserve_failure(attempt,output,log,exc)
            raise

    def preserve_failure(self, attempt, output, log, exc):
        failure=resolve_manifest_path(self.root,self.manifest['output']['root'])/'failures'/attempt['fingerprint']
        failure.mkdir(parents=True,exist_ok=True)
        failure=Path(tempfile.mkdtemp(prefix='failure-',dir=failure))
        for artifact in (output,log,Path(str(output)+'.partial.wav')):
            if artifact.exists():shutil.copy2(artifact,failure/artifact.name)
        failure_record={'fingerprint':attempt['fingerprint'],'error':str(exc)}
        if attempt.get('render_execution') is not None:
            failure_record['render_execution']=attempt['render_execution']
        immutable_json(failure/'error.json',failure_record)

    def __call__(self, attempt: dict, output: Path, log: Path) -> dict:
        # Synchronous fixture/one-attempt API; the production scheduler uses
        # stage() on the render thread and audit_pending() on the QA worker.
        synthesis=self.synthesize(attempt,output,log)
        audit=self.audit_pending(attempt,output,log)
        return {**synthesis,**audit}

    def synthesize(self, attempt: dict, output: Path, log: Path) -> dict:
        try:
            record = self.read_pending(attempt)
            if record is not None:
                pending = self.pending_path(attempt)
                shutil.copyfile(pending/'output.wav',output);shutil.copyfile(pending/'render.log',log)
                return {'execution':record['execution'],
                        'preparation':record['preparation'],'synthesis_resumed_before_qa':True}
            parts = {p['kind']:p for p in attempt['input_parts']}
            paths = {}
            # Anker und globale Prewarms sind bereits materialisiert.
            for kind,part in parts.items():
                if kind!='german_prewarm' or not part.get('generation_fingerprint'):
                    paths[kind]=self.asset(part)
            original = paths.get('line_original')
            if attempt['prewarm'] and attempt['prewarm'].get('reference_mode')=='line_original':
                paths['german_prewarm']=self.prewarm(attempt,attempt['prewarm'],original)
            with tempfile.TemporaryDirectory(prefix='.prepare-',dir=output.parent) as folder:
                folder=Path(folder)
                if original is not None:
                    paths['line_original']=folder/'line.wav'
                    self.convert(original,paths['line_original'],legacy=attempt['reference_mode']=='line_original')
                prompt_audio=[paths[p['input_kind']] for p in attempt['prompt_parts']]
                prompt=None
                if len(prompt_audio)>1:
                    prompt=folder/'prompt.wav'
                    self.cache.prepare('ffmpeg-concat',prompt_audio,prompt,
                                       lambda out:concatenate(self.ffmpeg,prompt_audio,out))
                elif prompt_audio:prompt=prompt_audio[0]
                anchor=paths.get('main_anchor') or paths.get('line_original')
                selected=attempt.get('render_execution') or self.manifest.get('render_execution') or backend_profile('gpu')
                if selected['profile_id'] == 'ane':
                    with log.open('w',encoding='utf-8') as stream:
                        timing=self._run_ane(attempt['target_text'],anchor,prompt,
                            ' '.join(p['text'].strip() for p in attempt['prompt_parts'])+' ' if prompt else None,
                            attempt['seed'],output,stream,selected,
                            mode='combined' if attempt['runtime_mode']=='combined' else 'isolated',
                            fingerprint=attempt['fingerprint'])
                    if not output.is_file(): raise RenderOutputError('ANE-Renderer erzeugte keine WAV')
                    events,self.cache.events=self.cache.events,[]
                    pending=self.pending_path(attempt); pending.parent.mkdir(parents=True,exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix='.pending-',dir=pending.parent) as temporary:
                        staging=Path(temporary)/'render';staging.mkdir()
                        shutil.copyfile(output,staging/'output.wav');shutil.copyfile(log,staging/'render.log')
                        render_record={'fingerprint':attempt['fingerprint'],'wav_sha256':sha256_file(output),
                            'log_sha256':sha256_file(log),'execution':timing,'preparation':events,
                            'render_execution':attempt.get('render_execution')}
                        immutable_json(staging/'render.json',render_record)
                        if pending.exists():raise ArtifactError('Ausstehender Versuch existiert bereits')
                        os.rename(staging,pending)
                    return {'execution':timing,'preparation':events}
                cli=self.asset(attempt['runtime']['artifacts']['combined_cli' if attempt['runtime_mode']=='combined' else 'isolated_cli'])
                params=selected['effective_parameters']
                request=RenderRequest(attempt['target_text'],str(anchor),str(output),attempt['seed'],
                    str(prompt) if prompt else None,
                    ' '.join(p['text'].strip() for p in attempt['prompt_parts'])+' ' if prompt else None,
                    params.get('steps',200),params.get('timesteps',10),params.get('cfg',2.0),params.get('temperature',1.0))
                cmd=request.argv(str(cli),tuple(map(str,self.models)))
                print(f"Render {attempt['dialog_id']} · {attempt['method']} · Seed {attempt['seed']}",flush=True)
                with log.open('w',encoding='utf-8') as stream:
                    timing=self.sessions.run(cmd,stream,backend=selected['profile_id'])
                if not output.is_file(): raise RenderOutputError('VoxCPM2-Renderer erzeugte keine WAV')
                events,self.cache.events=self.cache.events,[]
                pending=self.pending_path(attempt)
                pending.parent.mkdir(parents=True,exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='.pending-',dir=pending.parent) as temporary:
                    staging=Path(temporary)/'render';staging.mkdir()
                    shutil.copyfile(output,staging/'output.wav');shutil.copyfile(log,staging/'render.log')
                    render_record={'fingerprint':attempt['fingerprint'],
                        'wav_sha256':sha256_file(output),'log_sha256':sha256_file(log),
                        'execution':timing,'preparation':events}
                    if attempt.get('render_execution') is not None:
                        render_record['render_execution']=attempt['render_execution']
                    immutable_json(staging/'render.json',render_record)
                    if pending.exists():raise ArtifactError('Ausstehender Versuch existiert bereits')
                    os.rename(staging,pending)
                return {'execution':timing,'preparation':events}
        except BaseException as exc:
            # Preserve failed synthesis/STT evidence before AttemptStore removes its temp directory.
            self.preserve_failure(attempt,output,log,exc)
            raise


def verify_production(manifest: dict, root: Path):
    if (manifest['qa'].get('status')!='configured' or manifest['qa']['policy_id']!=POLICY
            or manifest['qa']['thresholds']!=THRESHOLDS or manifest['qa']['decoder_parameters']!=DECODER):
        raise ValueError('Keine unterstützte, vollständig konfigurierte QA im Produktionsmanifest')
    if (tuple(r['path'] for r in manifest['models']['stt']['files'])!=STT_PATHS or
            manifest['source_evidence']['production'].get('stt_cascade')!=CASCADE):
        raise ValueError('Base→Medium-Modellbindung oder asynchrone QA-Konfiguration abweichend')
    policy=manifest['source_evidence']['production'].get('integrity_policy', {'mode':'full','max_hash_bytes':None})
    if policy.get('mode') not in {'fast','full'} or (policy.get('mode')=='fast' and policy.get('max_hash_bytes')!=DEFAULT_HASH_LIMIT):
        raise ValueError('Ungültige Integritätsrichtlinie im Produktionsmanifest')
    full_integrity=policy['mode']=='full'
    issues=validate(manifest,asset_root=root,check_assets=True,
                    max_hash_bytes=None if full_integrity else DEFAULT_HASH_LIMIT,
                    skip_content_hashes=not full_integrity)
    if issues:raise ValueError('; '.join(i.message for i in issues[:8]))
    evidence=manifest['source_evidence']['production']
    selected = manifest.get('render_execution', backend_profile('gpu'))
    if evidence.get('backend_profile') != selected:
        raise ValueError('Backendauswahl und abgeleiteter Profilbeleg unterscheiden sich')
    for asset in [evidence['stt_cli'],*evidence['code_files']]:
        target=resolve_manifest_path(root,asset['path'])
        if not target.is_file():
            raise ValueError(f"QA-/Renderer-/Runtimecode fehlt: {asset['path']}")
        if full_integrity and sha256_file(target)!=asset['sha256']:
            raise ValueError('QA-/Renderer-/Runtimecode seit Manifestvorbereitung verändert')
    if full_integrity:
        if profile_assets(root, selected) != selected.get('assets'):
            raise ValueError('Ausgewählte Backendprofilassets fehlen oder wurden seit der Vorbereitung verändert')
    else:
        for asset in selected.get('assets',[]):
            target=resolve_manifest_path(root,asset['path'])
            if not target.is_file():
                raise ValueError(f"Backendprofilasset fehlt: {asset['path']}")
    if selected.get('operating_mode') in {'parallel_gpu_ane', 'adaptive_gpu_ane'}:
        identity = (selected.get('model_identity', {}) if selected['profile_id'] == 'ane'
                    else selected.get('model_identity', {}).get('parallel_ane', {}))
        if not selected.get('ane_worker') or not identity.get('weights_present'):
            raise ValueError('Parallelbetrieb benötigt gebundene ANE-Modellkomponenten')
        if identity.get('component_receipts_sha256') != sha256(canonical_json(selected['ane_worker'])):
            raise ValueError('ANE-Komponentenbelege stimmen nicht mit der Profilidentität überein')
        expected_revisions={key:identity.get(key) for key in
            ('model_id','version','fallback_model_id','fallback_version','tokenizer_version')}
        if selected['ane_worker'].get('model_revisions') != expected_revisions:
            raise ValueError('ANE Runtime-/Modellrevisionen stimmen nicht mit den gebundenen Pfaden überein')
        from .ane_worker import resolve_bindings
        resolve_bindings(selected['ane_worker'], verify_hashes=full_integrity)


    elif selected['profile_id'] == 'ane' and selected.get('ane_worker'):
        identity = selected.get('model_identity', {})
        if (not identity.get('weights_present') or
                identity.get('component_receipts_sha256') != sha256(canonical_json(selected['ane_worker']))):
            raise ValueError('ANE-Komponentenbelege stimmen nicht mit der Profilidentität überein')
        expected_revisions={key:identity.get(key) for key in
            ('model_id','version','fallback_model_id','fallback_version','tokenizer_version')}
        if selected['ane_worker'].get('model_revisions') != expected_revisions:
            raise ValueError('ANE Runtime-/Modellrevisionen stimmen nicht mit den gebundenen Pfaden überein')
        from .ane_worker import resolve_bindings
        resolve_bindings(selected['ane_worker'], verify_hashes=full_integrity)


def prepare_qa_compatible_resume(current: dict, saved: dict, root: Path,
                                 run_root: Path, *, run_label: str,
                                 progress: dict) -> dict:
    """Authorize only the diagnostic UTF-8 fix against an immutable interrupted run."""
    root=root.resolve(strict=True)
    if run_root.is_symlink():
        raise ArtifactError('Kompatibler Resume-Runroot darf kein Symlink sein')
    run_root=run_root.resolve(strict=True)
    expected_root=resolve_manifest_path(root,saved.get('output',{}).get('root',''))
    if expected_root.resolve(strict=True)!=run_root:
        raise ArtifactError('Resume-Runroot stimmt nicht mit dem gespeicherten Manifest überein')
    if not run_label or not run_root.name.endswith('-'+run_label):
        raise ArtifactError('QA-kompatibler Resume benötigt das gespeicherte Runlabel')
    for name in ('manifest.json','run-plan.json','progress.json'):
        path=run_root/name
        if path.is_symlink() or not path.is_file():
            raise ArtifactError(f'Gespeichertes Resume-Artefakt fehlt oder ist unsicher: {name}')
    if (run_root/'.run.lock').exists():
        raise ArtifactError('Lauf ist gesperrt; Sperrdatei wird nicht entfernt')
    if progress.get('status')!='interrupted' or progress.get('completed_ids',0)<=0:
        raise ArtifactError('QA-kompatibler Resume verlangt einen unterbrochenen Lauf mit abgeschlossenen Ergebnissen')
    saved_digest=sha256(canonical_json(saved))
    if progress.get('manifest_sha256')!=saved_digest:
        raise ArtifactError('Fortschritt und gespeichertes Runmanifest widersprechen sich')
    if not isinstance(progress.get('run_plan_sha256'),str) or len(progress['run_plan_sha256'])!=64:
        raise ArtifactError('Gespeicherter Fortschritt enthält keinen gültigen Runplanhash')

    saved_selection=saved.get('render_execution',{})
    current_selection=current.get('render_execution',{})
    if (saved_selection.get('routing_policy')!='fixed-character-v1' or
            current_selection!=saved_selection):
        raise ArtifactError('QA-Kompatibilität verlangt dieselbe gespeicherte figurenfeste Backend-Auswahl')
    old_evidence=saved.get('source_evidence',{}).get('production',{})
    new_evidence=current.get('source_evidence',{}).get('production',{})
    if old_evidence.get('source_manifest_sha256')!=new_evidence.get('source_manifest_sha256'):
        raise ArtifactError('Quellmanifestidentität hat sich geändert; alte Versuche bleiben getrennt')

    old_codes=old_evidence.get('code_files',[])
    new_codes=new_evidence.get('code_files',[])
    old_by={row.get('path'):row.get('sha256') for row in old_codes if isinstance(row,dict)}
    new_by={row.get('path'):row.get('sha256') for row in new_codes if isinstance(row,dict)}
    if list(old_by)!=list(new_by) or set(old_by)!=set(new_by):
        raise ArtifactError('Codebelegliste wurde außerhalb der geprüften Änderung verändert')
    changed={path for path in old_by if old_by[path]!=new_by[path]}
    if changed!=QA_RESUME_CHANGED_CODE:
        raise ArtifactError('Codeänderungen sind nicht auf den geprüften UTF-8-Diagnosefix und Resume-Guard begrenzt')
    for path in changed:
        target=resolve_manifest_path(root,path)
        if target.stat().st_size>DEFAULT_HASH_LIMIT or sha256_file(target)!=new_by[path]:
            raise ArtifactError(f'Aktueller kleiner Codebeleg stimmt nicht: {path}')

    def runtime_version(manifest):
        evidence=manifest['source_evidence']['production']
        cli=manifest.get('runtime',{}).get('artifacts',{}).get('isolated_cli')
        payload={'profile':manifest['render_execution'],'tts':cli,
                 'stt':evidence['stt_cli'],'code':evidence['code_files']}
        return 'sha256:'+sha256(canonical_json(payload))

    def qa_policy_version(manifest):
        code=manifest['source_evidence']['production']['code_files']
        qa_code=[row for row in code if row['path'].startswith('audio_qa_')]
        return qa2.VERSION+':sha256:'+sha256(canonical_json(qa_code))

    if saved['runtime']['runtime_version']!=runtime_version(saved) or current['runtime']['runtime_version']!=runtime_version(current):
        raise ArtifactError('Renderer-/TTS-/STT-Identität ist intern inkonsistent')
    if saved['qa']['policy_version']!=qa_policy_version(saved) or current['qa']['policy_version']!=qa_policy_version(current):
        raise ArtifactError('QA-Policyhash ist intern inkonsistent')

    normalized=copy.deepcopy(current)
    normalized_codes=normalized['source_evidence']['production']['code_files']
    for row in normalized_codes:
        if row['path'] in changed:
            row['sha256']=old_by[row['path']]
    normalized['manifest_revision']=saved['manifest_revision']
    normalized['output']['root']=saved['output']['root']
    normalized['runtime']['runtime_version']=saved['runtime']['runtime_version']
    normalized['qa']['policy_version']=saved['qa']['policy_version']
    if canonical_json(normalized)!=canonical_json(saved):
        raise ArtifactError('Manifeständerung enthält neben den erlaubten Codeidentitäten weitere Abweichungen')

    return {'compatibility_id':QA_RESUME_COMPATIBILITY_ID,
            'run_root':saved['output']['root'],
            'saved_manifest_sha256':saved_digest,
            'source_manifest_sha256':old_evidence['source_manifest_sha256'],
            'run_plan_sha256':progress['run_plan_sha256'],
            'previous_code_sha256':{path:old_by[path] for path in sorted(changed)},
            'active_code_sha256':{path:new_by[path] for path in sorted(changed)},
            'compatibility_scope':'only whisper diagnostic pipe handling and guarded resume CLI changed; transcript JSON remains strict UTF-8 and existing TTS/QA receipts are unchanged'}

def bind_run_attempts(manifest: dict, root: Path, plan: dict | None = None) -> list[dict]:
    """Persist all logical attempt choices before any executor/model is started."""
    plan = plan or build_run_plan(manifest)
    selected = manifest.get('render_execution', backend_profile('gpu'))
    return [bind_attempt(root, manifest, attempt, selected=selected)
            for row in plan['dialogues'] for attempt in row['attempts']]


def select_run_plan(plan: dict, dialog_ids: list[str] | None) -> dict:
    """Restrict a real manifest plan to an explicit, duplicate-free ID sample."""
    if dialog_ids is None:
        return plan
    if not dialog_ids or len(dialog_ids) != len(set(dialog_ids)):
        raise ValueError('Dialog-Auswahl muss eindeutige IDs enthalten')
    rows = {row['dialog_id']: row for row in plan['dialogues']}
    unknown = set(dialog_ids) - set(rows)
    if unknown:
        raise ValueError('Dialog-Auswahl enthält nicht enthaltene IDs: ' + ', '.join(sorted(unknown)))
    selected = {**plan, 'dialogues': [rows[dialog_id] for dialog_id in dialog_ids]}
    selected['sha256'] = sha256(canonical_json({key:value for key,value in selected.items() if key != 'sha256'}))
    return selected


def install_selected(source: Path, target: Path):
    if target.exists():
        if target.is_symlink() or sha256_file(source)!=sha256_file(target):
            raise ArtifactError('Ausgewählte Ausgabe ist abweichend: '+str(target))
        return
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as stream:temporary=Path(stream.name)
    try:
        shutil.copyfile(source,temporary);atomic_install(temporary,target)
    finally:temporary.unlink(missing_ok=True)


def write_review(root: Path, run_root: Path, rows: list[dict]):
    records=[]
    for row in rows:
        item={k:row.get(k) for k in ('native_speech_id','text','processing','qa_status','reason_codes',
                                   'role_id','role_name','short','selected_method','escalation_level','selected_wav_sha256')}
        for key in ('original','new_audio','historical'):
            item[key]=quote(os.path.relpath(root/row[key],run_root)) if row.get(key) else None
        records.append(item)
    encoded=json.dumps(records,ensure_ascii=False).replace('<','\\u003c')
    page='''<!doctype html><meta charset="utf-8"><title>MI1 – vollständiger Manifestlauf</title>
<style>body{font:16px system-ui;max-width:1100px;margin:30px auto;padding:0 20px;background:#f5f4f0;color:#202725}article{background:white;padding:20px;margin:15px 0;border-radius:10px}audio{width:100%;max-width:340px}label{display:inline-block;margin:10px}input,select{font:inherit;padding:8px}</style>
<h1>MI1 – Original, neue Fassung, historische Fassung</h1><p>Die vorläufige Textprüfung ersetzt die Hörbewertung nicht. Warnungen bleiben sichtbar.</p>
<input id="search" placeholder="ID oder Text suchen"><select id="filter"><option value="">Alle QA-Status</option><option>warn</option><option>fail</option><option>pass</option><option value="copy_original">Originalübernahmen</option></select>
<select id="role"><option value="">Alle Rollen</option></select><select id="method"><option value="">Alle Methoden</option></select>
<select id="length"><option value="">Alle Längen</option><option value="short">Kurz</option><option value="long">Lang</option></select>
<select id="escalation"><option value="">Alle Eskalationsstufen</option><option value="0">Standard</option><option value="1">1</option><option value="2">2</option><option value="3">3</option></select>
<button id="jsonExport">Bewertungen als JSON</button><button id="csvExport">Bewertungen als CSV</button><p id="notice">Bewertungen werden in diesem Browser gespeichert. JSON/CSV zum dauerhaften Aufbewahren exportieren.</p><span id="count"></span><main id="list"></main>
<script>
const rows=DATA, elements=Object.fromEntries(['search','filter','role','method','length','escalation','jsonExport','csvExport','notice','count','list'].map(id=>[id,document.getElementById(id)]));
const storageKey='mi1-manifest-review:'+location.pathname;let reviews={};try{reviews=JSON.parse(localStorage.getItem(storageKey)||'{}')}catch(e){elements.notice.textContent='Browserspeicher nicht verfügbar; Bewertungen vor dem Schließen exportieren.'}
function key(r){return r.native_speech_id+':'+(r.selected_wav_sha256||'no-new-wav')}
function save(){try{localStorage.setItem(storageKey,JSON.stringify(reviews))}catch(e){elements.notice.textContent='Speichern fehlgeschlagen; Bewertungen vor dem Schließen exportieren.'}}
for(let [id,field] of [['role','role_id'],['method','selected_method']]){let values=[...new Set(rows.map(r=>r[field]).filter(Boolean))].sort();for(let value of values){let option=document.createElement('option');option.value=value;option.textContent=id==='role'?(rows.find(r=>r.role_id===value).role_name||value):value;elements[id].append(option)}}
function show(){let e=elements,shown=rows.filter(r=>(!e.filter.value||r.qa_status===e.filter.value||r.processing===e.filter.value)&&(!e.role.value||r.role_id===e.role.value)&&(!e.method.value||r.selected_method===e.method.value)&&(!e.length.value||(e.length.value==='short'?r.short===true:r.short===false))&&(!e.escalation.value||String(r.escalation_level)===e.escalation.value)&&((r.native_speech_id+' '+r.text).toLowerCase().includes(e.search.value.toLowerCase())));e.count.textContent=' '+shown.length+' Ergebnisse (maximal 100 gleichzeitig; Suche eingrenzen)';e.list.replaceChildren();for(let r of shown.slice(0,100)){let a=document.createElement('article'),h=document.createElement('h3');h.textContent=r.native_speech_id+' · '+(r.role_name||r.role_id||'')+' · '+(r.qa_status||r.processing);a.append(h);let p=document.createElement('p');p.textContent=r.text+' '+(r.reason_codes||[]).join(', ');a.append(p);for(let [field,title] of [['original','A: Original'],['new_audio','B: Neuer Lauf'],['historical','C: Historisch']])if(r[field]){let l=document.createElement('label');l.append(document.createTextNode(title));let v=document.createElement('audio');v.controls=true;v.preload='none';v.src=r[field];l.append(document.createElement('br'),v);a.append(l)}let note=reviews[key(r)]||{};for(let [field,title] of [['voice','Stimme'],['performance','Spielweise'],['completeness','Vollständigkeit'],['comment','Kommentar']]){let label=document.createElement('label'),input=document.createElement('input');label.append(document.createTextNode(title+' '));if(field!=='comment'){input.type='number';input.min='1';input.max='5'}input.value=note[field]||'';input.onchange=()=>{reviews[key(r)]={...reviews[key(r)],speech_id:r.native_speech_id,wav_sha256:r.selected_wav_sha256,[field]:input.value};save()};label.append(input);a.append(label)}e.list.append(a)}}
function download(name,data,type){let url=URL.createObjectURL(new Blob([data],{type})),link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
elements.jsonExport.onclick=()=>download('manifest-reviews.json',JSON.stringify(reviews,null,2),'application/json');
elements.csvExport.onclick=()=>{let fields=['speech_id','wav_sha256','voice','performance','completeness','comment'],cell=v=>'"'+String(v??'').replaceAll('"','""')+'"';download('manifest-reviews.csv',[fields,...Object.values(reviews).map(r=>fields.map(f=>r[f]||''))].map(r=>r.map(cell).join(',')).join('\\r\\n'),'text/csv')};
for(let id of ['search','filter','role','method','length','escalation'])elements[id].oninput=show;show();
</script>'''.replace('DATA',encoded)
    (run_root/'review.html').write_text(page,encoding='utf-8')


def _parallel_profiles(manifest: dict) -> dict[str, dict]:
    selected = manifest['render_execution']
    from .backend import profile
    profiles = {}
    for name in selected.get('dispatch_profiles', []):
        item = profile(name, operating_mode=selected['operating_mode'], facts={})
        item['assets'] = selected.get('assets', [])
        item['operating_mode'] = selected['operating_mode']
        item.update({key: selected[key] for key in ('ane_worker','routing_policy','adaptive_policy') if key in selected})
        if name == selected.get('profile_id'):
            item['model_identity'] = selected['model_identity']
        if name == 'gpu' and 'model_identity' not in item:
            tts = manifest['models']['tts']
            item['model_identity'] = {'model_id':tts['model_id'],'version':tts['version'],
                'assets_sha256':sha256(canonical_json(tts['files'])),'weights_present':True}
        if name == 'ane':
            if 'ane_worker' not in item:
                raise ValueError('Paralleler ANE-Worker ist nicht konfiguriert')
            if 'parallel_ane' in selected.get('model_identity', {}):
                item['model_identity'] = selected['model_identity']['parallel_ane']
            if not item.get('model_identity', {}).get('weights_present'):
                raise ValueError('Paralleler ANE-Worker benötigt installierte Modellkomponenten')
        profiles[name] = item
    if set(profiles) != {'gpu', 'ane'}:
        raise ValueError('Paralleler Betrieb benötigt genau GPU und ANE')
    return profiles


def run_all(manifest: dict, root: Path, *, dialog_ids: list[str] | None = None, guard=require_m1, verifier=verify_production,
            executor_factory=ManifestExecutor, auditor_factory=ManifestAuditor,
            serialize_ane_qa: bool = False, timing=None,
            resume_compatibility: dict | None = None, store_factory=AttemptStore,
            event_sink=None, progress_sink=None, first_pass=False,
            defer_qa_until_tts_complete=False) -> dict:
    root=root.resolve(strict=True)
    selected = manifest.get('render_execution', backend_profile('gpu'))
    parallel = selected.get('operating_mode') in {'parallel_gpu_ane', 'adaptive_gpu_ane'}
    adaptive = selected.get('operating_mode') in {'adaptive_ane', 'adaptive_gpu_ane'}
    if defer_qa_until_tts_complete and (not first_pass or parallel):
        raise ValueError('Deferred QA is supported only for single-backend launcher first passes')
    if adaptive:
        from .adaptive_ane import effective_policy, validate_policy
        validate_policy(selected.get('adaptive_policy'))
        if selected['adaptive_policy']['maximum_instances'] > 4:
            from .hardware import host_facts
            facts=host_facts()
            if selected['adaptive_policy']['version'] in {'adaptive-ane-v3','adaptive-ane-v4'}:
                if effective_policy(facts)['maximum_instances'] < selected['adaptive_policy']['maximum_instances']:
                    raise ValueError('Resource ANE policy requires more than 8 GiB physical RAM')
            elif (facts.get('system_ram_bytes') or 0) < 64*1024**3:
                raise ValueError('Saved five-instance v2 ANE policy requires at least 64 GiB physical RAM')
    if selected.get('operating_mode') == 'adaptive_gpu_ane' and selected.get('routing_policy') != 'fixed-character-v1':
        raise ValueError('Adaptive GPU+ANE benötigt festes Figurenrouting')
    if serialize_ane_qa and (selected.get('profile_id') != 'ane' or parallel or adaptive):
        raise ValueError('Serialisierte TTS/QA-Ausführung gilt nur für isoliertes ANE-Single')
    if selected['profile_id'] not in {'gpu','cpu','ane'} or (not parallel and not adaptive and selected['operating_mode'] != 'single') or (adaptive and not parallel and selected['profile_id']!='ane'):
        raise ValueError('Das ausgewählte Backend/Betriebsprofil ist nicht ausführbar')
    if selected['profile_id'] == 'ane' and not selected.get('ane_worker'):
        raise ValueError('ANE-Ausführung benötigt hashgebundene lokale Modellpfade (--ane-worker-config)')
    guard(root)
    mode=manifest['source_evidence']['production'].get('integrity_policy',{}).get('mode','full')
    if timing:
        with timing.phase('production_preflight',counts={'asset_hash_mode':mode},announce=True):
            verifier(manifest,root)  # All gates before any lazy model process.
        with timing.phase('run_plan_generation',counts={'manifest_dialogues':len(manifest['scope']['included_dialog_ids'])},announce=True):
            resolved=resolve_manifest(manifest);plan=build_run_plan(resolved)
            plan = select_run_plan(plan, dialog_ids)
    else:
        print("Prüfe vollständige Inhalts-Hashes …" if mode=='full' else
              "Prüfe Pfade, Größen, Schema und Konfiguration; Inhalts-Hashes werden gemäß gespeicherten Belegen übersprungen …",flush=True)
        verifier(manifest,root)  # All gates before any lazy model process.
        resolved=resolve_manifest(manifest);plan=build_run_plan(resolved)
        plan = select_run_plan(plan, dialog_ids)
    if (resume_compatibility is not None and
            resume_compatibility.get('run_plan_sha256') != plan.get('sha256')):
        raise ArtifactError('Gespeicherter Resume-Fortschritt verweist auf einen anderen Runplan')
    attempt_count=sum(len(row['attempts']) for row in plan['dialogues'])
    if timing: timing.event('run_plan_ready',counts={'dialogues':len(plan['dialogues']),'attempts':attempt_count})
    routing_policy=selected.get('routing_policy')
    portable_recipe = manifest.get('source_evidence',{}).get('portable_render_recipe')
    character_routing = routing_policy == 'fixed-character-v1' and not portable_recipe
    backend_profiles = _parallel_profiles(manifest) if parallel else {selected['profile_id']:selected}
    run_root=resolve_manifest_path(root,manifest['output']['root']);run_root.mkdir(parents=True,exist_ok=True)
    if timing: timing.bind_run_root(manifest['output']['root'])
    if timing:
        with timing.phase('run_artifact_initialization',counts={'dialogues':len(plan['dialogues']),'attempts':attempt_count},announce=True):
            immutable_json(run_root/'manifest.json',manifest);immutable_json(run_root/'run-plan.json',plan)
    else:
        immutable_json(run_root/'manifest.json',manifest);immutable_json(run_root/'run-plan.json',plan)
    bindings_by_fingerprint={}
    aggregate_bindings=run_root/'backend-bindings.json'
    if aggregate_bindings.is_file():
        try:
            prior=json.loads(aggregate_bindings.read_text(encoding='utf-8')).get('bindings',[])
            bindings_by_fingerprint={item['attempt_fingerprint']:item for item in prior}
        except (OSError,ValueError,KeyError,TypeError) as exc:
            raise ArtifactError('Backendbindungen im Runbericht beschädigt') from exc
    bindings=list(bindings_by_fingerprint.values())
    manifest_binding_sha256=sha256(canonical_json(manifest))
    sessions=SessionPool(logs_dir=resolve_manifest_path(root,manifest['output']['root']+'/session-tmp'))
    store=store_factory(root);results=[];error=None;last_progress={};restart_required=False
    tts_released=False
    source_rows={r['dialog_id']:r for r in manifest['dialogues']}
    roles={r['role_id']:r for r in manifest['roles']}
    role_binding_path=run_root/'character-bindings.json'
    role_bindings={}
    role_bindings_written=set()
    if character_routing and role_binding_path.exists():
        try:
            saved=json.loads(role_binding_path.read_text(encoding='utf-8'))
            if saved.get('policy')!='fixed-character-v1' or saved.get('manifest_sha256')!=resolved['manifest_sha256']:
                raise ArtifactError('Figurenbindungen gehören zu einer anderen Routingpolicy oder Manifestrevision')
            role_bindings=saved.get('roles',{})
            if not isinstance(role_bindings,dict): raise ValueError('roles is not an object')
            role_bindings_written=set(role_bindings)
        except (OSError,UnicodeError,json.JSONDecodeError,ValueError) as exc:
            if isinstance(exc,ArtifactError): raise
            raise ArtifactError('Figurenbindungen beschädigt') from exc
    if character_routing:
        for role_id,worker in role_bindings.items():
            if worker not in backend_profiles or role_id not in roles:
                raise ArtifactError('Figurenbindung verweist auf unbekannte Figur oder Backend')
        rendered_roles=list(dict.fromkeys(d.get('role_id') for d in plan['dialogues']
                                          if d['processing']=='render' and d.get('role_id')))
        if not parallel:
            for role_id in rendered_roles:
                existing=role_bindings.get(role_id)
                if existing is not None and existing != selected['profile_id']:
                    raise ArtifactError('Figur ist bereits an ein anderes Backend gebunden')
                role_bindings[role_id]=selected['profile_id']
            atomic_json(role_binding_path,{'policy':'fixed-character-v1',
                'manifest_sha256':resolved['manifest_sha256'],'roles':role_bindings})
            role_bindings_written=set(role_bindings)
    manual_path=run_root/'manual-review.json'
    manual_identity={'manifest_sha256':resolved['manifest_sha256'],'run_plan_sha256':plan['sha256']}
    manual_review={}
    if first_pass and manual_path.exists():
        try:
            saved=json.loads(manual_path.read_text(encoding='utf-8'))
            if manual_path.is_symlink() or saved.get('identity')!=manual_identity:
                raise ValueError('foreign failure ledger')
            planned={d['dialog_id']:d for d in plan['dialogues']}
            for item in saved['dialogues']:
                d=planned[item['dialog_id']]
                if (d['processing']!='render' or item['dialog_id'] in manual_review or
                        item['attempt_fingerprint'] not in {a['fingerprint'] for a in d['attempts']} or
                        item['stage'] not in {'render','qa'} or
                        not isinstance(item['error_type'],str) or not isinstance(item['error'],str)):
                    raise ValueError('invalid failure record')
                manual_review[item['dialog_id']]=item
        except (OSError,ValueError,KeyError,TypeError) as exc:
            raise ArtifactError('Manuelle Renderfehlerliste beschädigt oder abweichend') from exc
    lock=run_root/'.run.lock'
    crashed_resume=False
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:
        # Recover only a demonstrably dead process, never an active/unknown lock.
        try:
            if not first_pass or lock.is_symlink(): raise ValueError('unsafe lock')
            pid=int(lock.read_text(encoding='ascii'))
            if pid<=0: raise ValueError('invalid pid')
            try: os.kill(pid,0)
            except ProcessLookupError: pass
            else: raise ValueError('process is alive')
            lock.unlink()
            fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
            crashed_resume=True
        except (OSError,ValueError) as exc:
            raise ArtifactError('Lauf gesperrt: '+str(lock)+'; der gespeicherte Prozess ist aktiv oder nicht sicher als beendet erkennbar') from exc
    os.write(fd,str(os.getpid()).encode());os.close(fd)
    summary={'manifest_sha256':resolved['manifest_sha256'],'run_plan_sha256':plan['sha256'],
             'qa_is_provisional':True,'game_patch_built':False,'expected_ids':len(plan['dialogues']),
             'expected_render_ids':sum(d['processing']=='render' for d in plan['dialogues']),
             'stt_cascade':CASCADE}
    try:
        if resume_compatibility is not None:
            write_resume_compatibility(run_root,resume_compatibility)
        from .render_event_log import RenderEventLog
        durable_events=RenderEventLog(run_root/'render-events.jsonl',{
            'manifest_sha256':resolved['manifest_sha256'],
            'run_plan_sha256':plan['sha256'],
            'run_root':manifest['output']['root'],
            'routing_policy':routing_policy,
        })
        class EventProjection:
            bindings = durable_events.bindings
            def append(self, event, **details):
                durable_events.append(event, **details)
                if event_sink: event_sink(event, **details)
        render_events = EventProjection()
        if resume_compatibility is not None:
            render_events.append('qa_resume_compatibility',
                compatibility_id=resume_compatibility['compatibility_id'],
                previous_code_sha256=resume_compatibility['previous_code_sha256'],
                active_code_sha256=resume_compatibility['active_code_sha256'],
                compatibility_scope=resume_compatibility['compatibility_scope'])
        for fingerprint,binding in render_events.bindings.items():
            prior=bindings_by_fingerprint.get(fingerprint)
            if prior is not None and prior!=binding:
                raise ArtifactError('Renderlog und bestehende Backendbindungen widersprechen sich')
            if binding.get('profile_id') not in backend_profiles:
                raise ArtifactError('Renderlog verweist auf ein im aktuellen Lauf unbekanntes Backend')
            bindings_by_fingerprint[fingerprint]=binding
        bindings[:]=bindings_by_fingerprint.values()
        auditor=auditor_factory(root,manifest,run_root)
        if parallel and not adaptive:
            executors={name:executor_factory(root,{**manifest,'render_execution':profile},sessions,auditor)
                       for name,profile in backend_profiles.items()}
            executor=executors
        elif adaptive:
            executor = {}
        else:
            executor=executor_factory(root,manifest,sessions,auditor)
        def release_tts():
            nonlocal tts_released
            owners = executor.values() if isinstance(executor,dict) else [executor]
            for owner in owners:
                release=getattr(owner,'release_ane_worker',None)
                if release: release()
            sessions.close()
            tts_released=True
            render_events.append('tts_models_released', qa_strategy='after_all_tts')
        def completed(dialogue,result):
            row=source_rows[dialogue['dialog_id']];sid=row['output_stem']
            if not str(sid).isdigit():raise ValueError('Ungültige Spiel-ID')
            # QA-completed artifacts now own the retained variants; delete only
            # matching staging copies, never unknown or partially committed data.
            owned_executors = executor if isinstance(executor,dict) else ({'single':executor} if isinstance(executor,ManifestExecutor) else {})
            for attempt_result in result['attempts']:
                worker_name=attempt_result.get('render_execution',{}).get('profile_id','single')
                owner=owned_executors.get(worker_name) or owned_executors.get('single')
                if owner is None and adaptive and owned_executors:
                    owner=next(iter(owned_executors.values()))
                if isinstance(owner,ManifestExecutor):
                    if attempt_result.get('qa_completion_status')=='deferred_manual_review':
                        continue
                    attempt=next(a for a in dialogue['attempts'] if a['fingerprint']==attempt_result['fingerprint'])
                    pending_record=owner.read_pending(attempt)
                    if pending_record is not None:
                        if (pending_record['wav_sha256']!=attempt_result['wav_sha256'] or
                                pending_record['log_sha256']!=attempt_result['log_sha256']):
                            raise ArtifactError('Staging/abgeschlossener QA-Versuch abweichend')
                        shutil.rmtree(owner.pending_path(attempt))
            result['attempts']=[{k:v for k,v in a.items() if k not in {'resumed','synthesis_resumed_before_qa'}} for a in result['attempts']]
            result.update(native_speech_id=sid,original=row['original_audio']['path'],
                          text=next((v['tts_text'] for v in row['text_variants'] if v['variant_id']==row['selected_variant_id']),''))
            result.update(role_id=row.get('role_id'),role_name=roles.get(row.get('role_id'),{}).get('name'),
                          short=dialogue.get('classification',{}).get('short'))
            if dialogue['processing']=='copy_original':
                source=resolve_manifest_path(root,row['original_audio']['path'])
                if sha256_file(source)!=row['original_audio']['sha256']:raise ValueError('Originalübernahme verändert')
                chosen=run_root/'selected'/(str(sid)+source.suffix)
                result['qa_status']='not_required_original'
            elif result['selected']:
                selected=result['selected']['fingerprint']
                attempt=next(a for a in dialogue['attempts'] if a['fingerprint']==selected)
                source=resolve_manifest_path(root,attempt['artifacts']['wav']);chosen=run_root/'selected'/(str(sid)+'.wav')
                selected_result=next(a for a in result['attempts'] if a['fingerprint']==selected)
                method=attempt['method']
                escalation=0 if method=='standard' else manifest['project_policy']['render_strategy']['escalation_order'].index(method)+1
                result.update(qa_status=selected_result['qa_status'],reason_codes=selected_result.get('reason_codes',[]),
                              selected_method=method,escalation_level=escalation,selected_wav_sha256=selected_result['wav_sha256'])
            else:
                source=chosen=None;result['qa_status']='fail'
                result['reason_codes']=list(dict.fromkeys([*result.get('reason_codes',[]),
                    *(code for a in result['attempts'] for code in a.get('reason_codes',[]))]))
            if chosen is not None:
                install_selected(source,chosen);result['new_audio']=chosen.relative_to(root).as_posix()
            historical=root/'output/german_wav'/(str(sid)+'.wav')
            if historical.is_file():result['historical']=historical.relative_to(root).as_posix()
            immutable_json(run_root/'dialogues'/(str(sid)+'.json'),result)
            results.append(result)
            counts=dict(Counter(r['qa_status'] for r in results))
            render_events.append('dialogue_completed',dialog_id=dialogue['dialog_id'],
                role_id=row.get('role_id'),qa_status=result['qa_status'],
                selected_attempt_fingerprint=result.get('selected',{}).get('fingerprint')
                    if isinstance(result.get('selected'),dict) else None,
                selected_wav_sha256=result.get('selected_wav_sha256'),
                reason_codes=result.get('reason_codes',[]),counts=counts)
            print(f'QA [{len(results)}/{len(plan["dialogues"])}] {sid}: {result["qa_status"]}',flush=True)
        def failed_dialogue(dialogue,attempt,stage,exc,attempt_results):
            did=dialogue['dialog_id']
            record={'dialog_id':did,'native_speech_id':source_rows[did]['output_stem'],
                'attempt_fingerprint':attempt['fingerprint'],'stage':stage,
                'error_type':type(exc).__name__,'error':str(exc) or type(exc).__name__,
                'restart_required':False}
            manual_review[did]=record
            atomic_json(manual_path,{'identity':manual_identity,'dialogues':list(manual_review.values())})
            complete_failure(dialogue,record,attempt_results)
        def complete_failure(dialogue,record,attempt_results=()):
            completed(dialogue,{'dialog_id':dialogue['dialog_id'],'processing':'render','selected':None,
                'qa_status':'fail','attempts':list(attempt_results),'render_status':'skipped',
                'manual_review_required':True,'manual_review':record,
                'reason_codes':['render_exception' if record['stage']=='render' else 'qa_exception']})
        def failure_status():
            return {'manual_review':list(manual_review.values()),'skipped_count':len(manual_review),
                    'restart_required':False}
        def progress(stats):
            nonlocal last_progress
            last_progress={**stats,**failure_status()}
            atomic_json(run_root/'progress.json',{**summary,**last_progress,'status':'running',
                       'completed_ids':len(results),'counts':dict(Counter(r['qa_status'] for r in results))})
            if progress_sink: progress_sink({**stats,**failure_status(), 'completed_ids':len(results),
                'expected_ids':len(plan['dialogues']), 'counts':dict(Counter(r['qa_status'] for r in results))})
        def persist_role_binding(role_id, worker):
            if not character_routing: return
            prior=role_bindings.get(role_id)
            if prior is not None and prior != worker:
                raise ArtifactError('Figur ist bereits dauerhaft an ein anderes Backend gebunden')
            if prior is None:
                role_bindings[role_id]=worker
            if role_id not in role_bindings_written:
                atomic_json(role_binding_path,{'policy':'fixed-character-v1',
                    'manifest_sha256':resolved['manifest_sha256'],'roles':role_bindings})
                role_bindings_written.add(role_id)

        legacy_binding_cache={}
        def legacy_binding(raw):
            fingerprint=raw['fingerprint']
            if fingerprint in legacy_binding_cache:
                return legacy_binding_cache[fingerprint]
            binding_path=resolve_manifest_path(root,manifest['output']['root']+'/bindings/'+fingerprint+'.json')
            if not binding_path.exists():
                legacy_binding_cache[fingerprint]=None
                return None
            try:
                if binding_path.is_symlink(): raise ValueError('Symlink')
                record=json.loads(binding_path.read_text(encoding='utf-8'))
                worker=record['profile_id']
                if worker not in backend_profiles: raise ValueError('unknown backend')
                expected_attempt={**raw,'render_execution':backend_profiles[worker]} if parallel else raw
                expected=bind_attempt(root,manifest,expected_attempt,selected=backend_profiles[worker],
                    manifest_sha256=manifest_binding_sha256,persist=False)
                if record!=expected: raise ValueError('binding differs')
            except (OSError,UnicodeError,json.JSONDecodeError,KeyError,TypeError,ValueError) as exc:
                raise ArtifactError('Legacy-Backendzuordnung beschädigt oder abweichend') from exc
            legacy_binding_cache[fingerprint]=record
            return record

        def preferred_worker(raw):
            if portable_recipe:
                worker=raw['render_execution']['profile_id']
                if worker not in backend_profiles: raise ArtifactError('Renderrezept benötigt ein nicht verfügbares Backend')
                return worker
            if character_routing and raw.get('role_id') in role_bindings:
                return role_bindings[raw['role_id']]
            fingerprint=raw['fingerprint']
            recorded=(render_events.bindings.get(fingerprint) or
                      bindings_by_fingerprint.get(fingerprint) or legacy_binding(raw))
            if recorded is not None and recorded.get('profile_id') not in backend_profiles:
                raise ArtifactError('Backendzuordnung verweist auf unbekannten Worker')
            return recorded.get('profile_id') if recorded else None
        binding_started=False
        binding_elapsed=0.0
        def prepare_attempt(raw, worker_name=None):
            nonlocal binding_started, binding_elapsed
            bind_started=time.monotonic()
            if timing and not binding_started:
                timing.event('attempt_bindings',state='start',counts={'attempts':attempt_count})
                binding_started=True
            if character_routing:
                worker_name=worker_name or selected['profile_id']
                persist_role_binding(raw['role_id'],worker_name)
                raw={**raw,'role_id':raw['role_id']}
            worker_name=worker_name or (preferred_worker(raw) if portable_recipe else selected['profile_id'])
            if portable_recipe and worker_name != preferred_worker(raw):
                raise ArtifactError('Konkretes Renderrezept-Backend darf nicht umgeroutet werden')
            profile = raw['render_execution'] if portable_recipe else backend_profiles[worker_name]
            attempt = {**raw, 'render_execution':profile} if parallel else raw
            expected=bind_attempt(root,manifest,attempt,selected=profile,
                                  manifest_sha256=manifest_binding_sha256,persist=False)
            prior=(render_events.bindings.get(raw['fingerprint']) or
                   bindings_by_fingerprint.get(raw['fingerprint']) or legacy_binding(raw))
            if prior is not None and prior!=expected:
                raise ArtifactError('Backendzuordnung weicht von der gespeicherten Manifest-/Modell-/Parameteridentität ab')
            if prior is not None and prior.get('profile_id')!=worker_name:
                raise ArtifactError('Versuch ist bereits an ein anderes Backend gebunden')
            bound=prior or expected
            if raw['fingerprint'] not in render_events.bindings:
                render_events.append('attempt_bound',attempt_fingerprint=raw['fingerprint'],
                    dialog_id=raw['dialog_id'],role_id=raw.get('role_id'),seed=raw.get('seed'),
                    backend=worker_name,renderer=profile.get('runtime_contract'),
                    model_identity=profile.get('model_identity'),
                    effective_parameters=profile.get('effective_parameters'),binding=bound)
            bindings_by_fingerprint[raw['fingerprint']]=bound
            bindings[:] = bindings_by_fingerprint.values()
            binding_elapsed += time.monotonic()-bind_started
            return attempt
        if first_pass:
            if crashed_resume:
                for did, active in durable_events.unfinished.items():
                    if did in manual_review: continue
                    dialogue=next((d for d in plan['dialogues'] if d['dialog_id']==did),None)
                    if dialogue is None: raise ArtifactError('Unterbrochener Dialog fehlt im Plan')
                    attempt=next((a for a in dialogue['attempts'] if a['fingerprint']==active['attempt_fingerprint']),None)
                    if attempt is None: raise ArtifactError('Unterbrochener Versuch fehlt im Plan')
                    # A process crash supplies no valid QA. Keep any pending WAV as evidence.
                    record={'dialog_id':did,'native_speech_id':source_rows[did]['output_stem'],
                        'attempt_fingerprint':attempt['fingerprint'],'stage':active['stage'],
                        'error_type':'ProcessInterrupted','error':'Vorheriger Renderprozess wurde während dieses Dialogs beendet',
                        'restart_required':False}
                    manual_review[did]=record
                if manual_review:
                    atomic_json(manual_path,{'identity':manual_identity,'dialogues':list(manual_review.values())})
            for d in plan['dialogues']:
                if d['dialog_id'] in manual_review: complete_failure(d,manual_review[d['dialog_id']])
        runnable=[d for d in plan['dialogues'] if d['dialog_id'] not in manual_review]
        if adaptive:
            from .adaptive_ane import Controller, run_adaptive
            def adaptive_factory():
                name=sessions.instance_context.instance_id
                worker_manifest={**manifest,'render_execution':backend_profiles['ane']}
                owner=executor_factory(root,worker_manifest,sessions,auditor)
                executor[name]=owner
                return owner
            gpu_executor=None
            if parallel:
                gpu_executor=executor_factory(root,{**manifest,'render_execution':backend_profiles['gpu']},sessions,auditor)
                executor['gpu']=gpu_executor
            run_adaptive(runnable,store,adaptive_factory,sessions,completed,progress,
                getattr(auditor,'cancel',lambda:None),prepare_attempt,event_sink=render_events.append,
                first_pass=first_pass,paused=getattr(executor_factory,'is_paused',lambda:False),
                controller=Controller(policy=selected['adaptive_policy']),
                previous_instances=dict(durable_events.role_instances),
                gpu_executor=gpu_executor, character_bindings=role_bindings,
                guybrush_role_id='mi1-ute-1.02:voice-code:GUY',
                failed_dialogue=failed_dialogue if first_pass else None,
                defer_qa_until_tts_complete=defer_qa_until_tts_complete,release_tts=release_tts,
                benchmark_policy=getattr(executor_factory,'benchmark_policy',lambda: {}),
                preferred_worker=preferred_worker if portable_recipe else None)
        else:
            run_pipeline(runnable,store,executor,completed,progress,getattr(auditor,'cancel',lambda:None),
                     prepare_attempt=prepare_attempt,
                     preferred_worker=preferred_worker if parallel else None,
                     character_routing=character_routing,
                     character_bindings=role_bindings,
                     guybrush_role_id='mi1-ute-1.02:voice-code:GUY',
                     serialize_tts_qa=serialize_ane_qa,
                     timing=timing, single_worker=selected['profile_id'],
                     event_sink=render_events.append, first_pass=first_pass,
                     failed_dialogue=failed_dialogue if first_pass else None,
                     defer_qa_until_tts_complete=defer_qa_until_tts_complete,release_tts=release_tts,
                     benchmark_policy=getattr(executor_factory,'benchmark_policy',lambda: {}))
        if (isinstance(auditor,ManifestAuditor) and
                set(auditor.qa_deferrals)!=auditor.used_qa_deferrals):
            missing=sorted(set(auditor.qa_deferrals)-auditor.used_qa_deferrals)
            raise ArtifactError(f'QA-Deferral nicht angewendet, Attempt wurde nicht geprüft: {missing}')
    except BaseException as exc:
        error=str(exc) or type(exc).__name__
        restart_required=restart_needed(exc)
        raise
    finally:
        had_prior_error = error is not None
        close_error = None
        try:
            if not tts_released: sessions.close()
        except BaseException as exc:
            close_error = exc
            if error is None:
                error = str(exc) or type(exc).__name__
                restart_required=restart_needed(exc)
        order={r['dialog_id']:i for i,r in enumerate(plan['dialogues'])}
        results.sort(key=lambda r:order[r['dialog_id']])
        counts=dict(Counter(r['qa_status'] for r in results))
        processed=len(results)==len(plan['dialogues']) and not error
        complete=processed and not manual_review
        completed={r['dialog_id'] for r in results}
        report={**summary,'status':'complete' if complete else ('complete_with_errors' if processed else 'interrupted'),'counts':counts,
                **({'manual_review':list(manual_review.values()),'skipped_count':len(manual_review),
                    'restart_required':restart_required,'all_ids_processed':processed} if first_pass else {}),
                **({'first_pass':True} if first_pass else {}),'all_ids_accounted':complete,'all_rendered_speech_passed':complete and not counts.get('fail') and not counts.get('warn'),
                'error':error,'dialogues':results,
                'processing_counts':dict(Counter(r['processing'] for r in results)),
                'render_status_counts':dict(Counter(a['render_status'] for r in results for a in r['attempts'])),
                'attempt_qa_counts':dict(Counter(a['qa_status'] for r in results for a in r['attempts'])),
                'method_counts':dict(Counter(a['method'] for r in results for a in r['attempts'])),
                'selected_escalation_counts':dict(Counter(str(r.get('escalation_level')) for r in results if r.get('selected_method'))),
                'warn_ids':[r['native_speech_id'] for r in results if r['qa_status']=='warn'],
                'fail_ids':[r['native_speech_id'] for r in results if r['qa_status']=='fail'],
                'not_run_ids':[r['output_stem'] for r in manifest['dialogues'] if r['dialog_id'] not in completed]}
        try:
            atomic_json(run_root/'backend-bindings.json', {'bindings': bindings})
            atomic_json(run_root/'render-report.json',report)
            atomic_json(run_root/'qa-summary.json',{k:v for k,v in report.items() if k!='dialogues'})
            atomic_json(run_root/'progress.json',{**summary,**last_progress,'status':report['status'],
                        'tts_status':'complete' if complete else 'interrupted',
                        'qa_status':'complete' if complete else 'interrupted',
                        'completed_ids':len(results),'counts':counts,'error':error,
                        **({k:report[k] for k in ('manual_review','skipped_count','restart_required','all_ids_processed')} if first_pass else {})})
            write_review(root,run_root,results)
            if timing:
                timing.event('attempt_bindings',state='end',
                    counts={'persisted':len(bindings_by_fingerprint),'planned':attempt_count},
                    total_binding_elapsed_seconds=round(binding_elapsed,6))
        finally:
            lock.unlink(missing_ok=True)
        if close_error is not None and not had_prior_error:
            raise close_error
    return report
