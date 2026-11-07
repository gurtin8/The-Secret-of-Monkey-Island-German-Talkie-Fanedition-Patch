"""Explicit launcher adapter for the existing production scheduler and review.

No models or files are touched by importing this module. Historical attempt
fingerprints remain intact; only signed launcher synthesis may cross versions.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import time
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import threading
import uuid
from collections import OrderedDict
from functools import lru_cache

from manifest_renderer.recipes import FORMAT as RECIPE_FORMAT, recipe_mode, validate_recipe

from .contracts import (ContractError, authorize_audio, canonical_json, digest,
    generation_receipt, load_or_create_installation, qa_projection,
    resolve_relative, synthesis_projection, validate_state)

MODES = {'fast': ('gpu', 'parallel_gpu_ane', 'mid'),
         'medium': ('ane', 'single', 'mid'), 'slow': ('gpu', 'single', 'highest'),
         'adaptive_ane': ('ane', 'adaptive_ane', 'mid')}

LEGACY_LIST = 'legacy'
INCOMPLETE_STATES = {'running','pausing','paused','failed','completed_with_errors'}


def _timestamp(path=None):
    if path is None:
        return datetime.now(timezone.utc).isoformat(timespec='seconds')
    try:
        stat=Path(path).stat()
        return datetime.fromtimestamp(getattr(stat,'st_birthtime',stat.st_mtime),timezone.utc).isoformat(timespec='seconds')
    except OSError:
        return None


def _category(mode=None, manifest=None):
    execution=(manifest or {}).get('render_execution',{})
    operating=execution.get('operating_mode')
    if operating in {'parallel_gpu_ane','adaptive_gpu_ane'} or mode=='fast': return 'ANE+GPU'
    backend=execution.get('profile_id') or execution.get('backend')
    if backend=='cpu': return 'CPU'
    if backend=='ane' or mode in {'medium','adaptive_ane'}: return 'ANE'
    return 'GPU'


@lru_cache(maxsize=1)
def _production_synthesis_code_digest():
    import ast
    from manifest_renderer import production
    tree=ast.parse(Path(production.__file__).read_text())
    executor=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ManifestExecutor')
    methods=[ast.dump(n,include_attributes=False) for n in executor.body
        if isinstance(n,ast.FunctionDef) and n.name in ('convert','prewarm','_run_ane','synthesize')]
    return digest(methods)


def manifest_list_id(manifest):
    parts=Path(manifest['output']['root']).parts
    return parts[2] if len(parts)>3 and parts[:2]==('runs','render-lists') else LEGACY_LIST


def validate_launcher_host(root, *, backend='gpu', operating_mode='single', facts=None):
    from manifest_renderer.hardware import host_facts
    facts = facts if facts is not None else host_facts(disk_path=root)
    if facts.get('os_family') != 'macos' or facts.get('architecture') != 'arm64':
        raise ContractError('Launcher-Synthese benötigt macOS auf Apple Silicon (arm64); Intel ist noch nicht implementiert')
    if backend == 'ane' or operating_mode in {'parallel_gpu_ane', 'adaptive_gpu_ane'}:
        version = tuple(int(p) for p in (facts.get('os_version') or '').split('.')[:2] if p.isdigit())
        if version < (15, 0): raise ContractError('ANE benötigt macOS 15 oder neuer')
    return True


def mode_selection(mode, *, ultra_fast=False):
    if not isinstance(ultra_fast, bool): raise ContractError('Ultra Fast muss ein Wahrheitswert sein')
    if ultra_fast and mode not in {'medium', 'fast'}:
        raise ContractError('Ultra Fast ist nur für Mittel und Schnell verfügbar')
    if mode not in MODES: raise ContractError('Unbekannter Rendermodus')
    backend, operating_mode, quality = MODES[mode]
    if ultra_fast: operating_mode = 'adaptive_gpu_ane' if mode == 'fast' else 'adaptive_ane'
    return dict(backend=backend, operating_mode=operating_mode,
                quality=quality, routing_policy='fixed-character-v1')


def require_mode_available(mode, *, ultra_fast=False):
    return mode_selection(mode, ultra_fast=ultra_fast)


def defer_first_pass_qa(*, facts=None):
    """Use physical capacity, never current free RAM, for the launcher strategy."""
    from manifest_renderer.hardware import GIB, host_facts
    facts = facts if facts is not None else host_facts()
    ram = facts.get('system_ram_bytes')
    return isinstance(ram, int) and not isinstance(ram, bool) and 0 < ram <= 8 * GIB


def mode_status(*, facts=None):
    """Backend contract for the independently maintained launcher interface."""
    from manifest_renderer.hardware import host_facts
    facts = facts if facts is not None else host_facts()
    result={}
    for name in MODES:
        selection=mode_selection(name)
        try:
            validate_launcher_host(None,backend=selection['backend'],operating_mode=selection['operating_mode'],facts=facts)
            available=True;reason=None
        except ContractError as exc:
            available=False;reason=str(exc)
        result[name]={**selection,'available':available,'reason':reason}
    return result


def component_identity(manifest, selected, *, qa=False):
    """Actual synthesis/decoder bytes, excluding manifest, QA and dispatch."""
    if qa:
        production = manifest['source_evidence']['production']
        assets = [{'artifact_id':'stt_cli', 'sha256':production['stt_cli']['sha256']}]
        assets.extend({'artifact_id':a['path'], 'sha256':a['sha256']}
                      for a in production.get('code_files', []) if a['path'].startswith('audio_qa_'))
        return dict(profile_id='stt', runtime_contract='whisper.cpp',
                    effective_parameters=manifest['qa']['decoder_parameters'], assets=assets)
    assets = [{'artifact_id':k, 'sha256':v['sha256']}
              for k,v in manifest['runtime']['artifacts'].items()
              if selected['profile_id'] != 'ane' or k == 'ffmpeg']
    assets.extend({'artifact_id':'tts:'+str(i), 'sha256':v['sha256']}
                  for i,v in enumerate(manifest['models']['tts']['files']))
    if selected['profile_id']=='ane':
        assets.extend({'artifact_id':a['path'], 'sha256':a['sha256']} for a in selected.get('assets',[]))
    assets.extend({'artifact_id':a['path'], 'sha256':a['sha256']}
        for a in manifest['source_evidence']['production'].get('code_files',[])
        if a['path'] in ('manifest_renderer/voxcpm2.py','manifest_renderer/audio.py',
            'manifest_renderer/audio_cache.py','manifest_renderer/preparation_core.py',
            'manifest_renderer/session.py','manifest_renderer/ane_worker.py','manifest_renderer/ane_worker_process.py'))
    # Bind actual command/conditioning code separately from the auditor in the
    # same legacy module. QA implementation changes therefore remain QA-only.
    assets.append({'artifact_id':'production-synthesis-code','sha256':_production_synthesis_code_digest()})
    # The ANE config includes all local model/tokenizer component receipts.
    if selected['profile_id']=='ane' and selected.get('ane_worker'):
        assets.append({'artifact_id':'ane_worker', 'sha256':digest(selected['ane_worker'])})
    return dict(profile_id=selected['profile_id'], runtime_contract=selected['runtime_contract'],
                effective_parameters=selected['effective_parameters'], assets=assets)


class VersionAudio:
    """Signed receipts beside existing artifacts, not a second run database."""
    _source_run_cache=OrderedDict();_source_run_cache_lock=threading.RLock()
    _source_run_cache_bytes=0;_source_run_cache_budget=32*1024*1024
    def __init__(self, root, manifest, project, installation):
        self.root=Path(root); self.manifest=manifest; self.project=project; self.installation=installation
        self.rows={d['dialog_id']:d for d in manifest['dialogues']}
        self.run_id=str(uuid.uuid5(uuid.UUID(project['project_id']), manifest['output']['root']))
        self.directory=self.root/'.launcher/audio-receipts'
        self.list_id=manifest_list_id(manifest)
        if self.list_id!=LEGACY_LIST:
            self.directory=self.directory/'render-lists'/self.list_id
        self.lock=threading.RLock();self._completed_index=None
        self._source_owner_cache=None

    def owns(self, entry):
        return entry.get('render_list_id',LEGACY_LIST)==self.list_id

    def synthesis(self, attempt):
        selected=attempt['render_execution']
        return synthesis_projection(self.rows[attempt['dialog_id']], attempt,
                                    backend=component_identity(self.manifest,selected))

    def qa(self, attempt, audio_hash):
        return qa_projection(audio_sha256=audio_hash, expected_text=attempt['qa_expected_text'],
            allowed_variants=attempt['qa_allowed_variants'], content_type=attempt['content_type'],
            original_duration_ms=self.rows[attempt['dialog_id']]['original_audio'].get('duration_ms',0),
            model=attempt['models']['stt'], decoder=component_identity(self.manifest,{},qa=True),
            rules={**attempt['qa'], 'cascade':self.manifest['source_evidence']['production'].get('stt_cascade')})

    def sign(self, body):
        return hmac.new(self.installation.receipt_key, canonical_json(body),hashlib.sha256).hexdigest()

    def find(self, attempt, *, audio_hash=None, log_hash=None, qa_key=None, attempt_fingerprint=None, phase=None):
        identity=self.synthesis(attempt)
        directory=self.directory/identity['key']
        failures=[]
        for path in sorted(directory.glob('*.json')) if directory.exists() else []:
            try:
                if path.is_symlink(): raise ContractError('Symlinkreceipt')
                entry=json.loads(path.read_text())
                if not self.owns(entry): continue
                if attempt_fingerprint is not None and entry.get('record',{}).get('fingerprint')!=attempt_fingerprint:continue
                if phase is not None and entry.get('phase')!=phase:continue
                body={k:v for k,v in entry.items() if k!='signature'}
                if not hmac.compare_digest(entry.get('signature',''),self.sign(body)):
                    raise ContractError('Receipt-Signatur abweichend')
                audio=authorize_audio(entry['generation'],self.installation,identity,
                    project_id=self.project['project_id'],work_root=self.root)
                if audio_hash is not None and entry['generation']['audio']['sha256']!=audio_hash: continue
                if log_hash is not None and entry['log']['sha256']!=log_hash: continue
                if qa_key is not None and (entry.get('qa') or {}).get('key')!=qa_key: continue
                log=resolve_relative(self.root,entry['log']['path'],must_exist=True)
                from manifest_renderer.audio import sha256_file
                if sha256_file(log)!=entry['log']['sha256']: raise ContractError('Loghash abweichend')
                return entry,audio,log
            except (OSError,ValueError,KeyError,TypeError) as exc:
                failures.append(str(exc))
        # Invalid historical receipts are never activated. They are retained;
        # a new host renders its own output under the current version root.
        return None

    def publish(self, attempt, wav, log, record, *, phase, audio_path=None, log_path=None):
        from manifest_renderer.audio import sha256_file
        from manifest_renderer.production import immutable_json
        identity=self.synthesis(attempt); audio_hash=sha256_file(wav)
        body={'render_list_id':self.list_id,'generation':generation_receipt(self.installation,identity,self.project['version'],
            project_id=self.project['project_id'],run_id=self.run_id,
            audio_path=audio_path or wav.relative_to(self.root).as_posix(),audio_sha256=audio_hash),
            'log':{'path':log_path or log.relative_to(self.root).as_posix(),'sha256':sha256_file(log)},
            'record':record, 'phase':phase, 'qa':self.qa(attempt,audio_hash) if phase=='qa' else None}
        entry={**body,'signature':self.sign(body)}
        target=self.directory/identity['key']/(digest(body)+'.json')
        with self.lock: immutable_json(target,entry)
        return target

    @staticmethod
    def resume_recipe(identity):
        """Comparison only: never a synthesis identity or an authorization key."""
        recipe=copy.deepcopy(identity['inputs'])
        recipe.pop('backend')
        for runtime in [recipe['runtime'], *(([recipe['prewarm']['runtime']])
                if recipe.get('prewarm',{} ) and 'runtime' in recipe['prewarm'] else [])]:
            for key in ('runtime_id','runtime_version','backend'): runtime.pop(key)
        return digest(recipe)

    def completed_source(self, path, attempt, *, deep=True):
        """Authenticate the complete original attempt before a mode-switch reuse."""
        from manifest_renderer.audio import sha256_file, validate_wav
        path=resolve_relative(self.root,Path(path).relative_to(self.root).as_posix(),must_exist=True)
        entry=json.loads(path.read_text())
        if not self.owns(entry): raise ContractError('Audiobeleg gehört zu einer anderen Ergebnisliste')
        body={k:v for k,v in entry.items() if k!='signature'}
        if not hmac.compare_digest(entry.get('signature',''),self.sign(body)):
            raise ContractError('Wiederverwendungsbeleg nicht lokal signiert')
        generation=entry['generation'];identity=generation['synthesis'];old=entry['record']
        if generation['version']!=self.project['version'] or entry['phase']!='qa':
            raise ContractError('Wiederverwendung benötigt dieselbe gewählte Manifestfassung und abgeschlossene QA')
        if (old.get('qa_status') not in {'pass','warn','fail'} or old.get('launcher_reuse') or
                old.get('qa_completion_status')=='deferred_manual_review' or
                any(s.get('stt_status')=='deferred' for s in old.get('stt_stages',[]))):
            raise ContractError('Unvollständige QA wird nicht modusübergreifend wiederverwendet')
        if self.resume_recipe(identity)!=self.resume_recipe(self.synthesis(attempt)):
            raise ContractError('Dialog-/Text-/Seed-/Anker-/Prewarm-Rezept abweichend')
        audio=authorize_audio(generation,self.installation,identity,
            project_id=self.project['project_id'],work_root=self.root,verify_audio=deep)
        import wave
        if deep:
            try: validate_wav(audio)
            except (OSError,ValueError,EOFError,wave.Error) as exc:
                raise ContractError('Englische Sprachausgabe ist keine gültige vollständige WAV') from exc
        log=resolve_relative(self.root,entry['log']['path'],must_exist=True)
        if (not log.is_file() or (deep and sha256_file(log)!=entry['log']['sha256']) or
                old['wav_sha256']!=generation['audio']['sha256'] or old['log_sha256']!=entry['log']['sha256'] or
                entry.get('qa')!=self.qa(attempt,generation['audio']['sha256'])):
            raise ContractError('Audio-/Log-/QA-Bindung abweichend')
        source_run=next((p for p in audio.parents if p!=self.root and
                        (p/'run-plan.json').is_file() and (p/'manifest.json').is_file()),None)
        if source_run is None: raise ContractError('Originalrunplan fehlt')
        manifest_path=resolve_relative(self.root,(source_run/'manifest.json').relative_to(self.root).as_posix(),must_exist=True)
        plan_path=resolve_relative(self.root,(source_run/'run-plan.json').relative_to(self.root).as_posix(),must_exist=True)
        source_manifest,plan,source_attempts,plan_valid=self._source_run_bundle(source_run,manifest_path,plan_path)
        if not plan_valid:
            raise ContractError('Originalmanifest-/Runplanbindung abweichend')
        source_attempt=source_attempts.get(old['fingerprint'])
        if source_attempt is None: raise ContractError('Originalversuch fehlt im Runplan')
        if digest({k:v for k,v in source_attempt.items() if k not in {'fingerprint','artifacts'}})!=old['fingerprint']:
            raise ContractError('Originalversuchsfingerprint abweichend')
        if resolve_relative(self.root,source_attempt['artifacts']['wav'],must_exist=True)!=audio:
            raise ContractError('Englische Sprachausgabe gehört nicht zum Originalversuch')
        report=resolve_relative(self.root,source_attempt['artifacts']['report'],must_exist=True)
        if json.loads(report.read_text())!=old: raise ContractError('Original-QA-Report abweichend')
        bound_attempt={**source_attempt,'render_execution':old['render_execution']}
        source_owner_identity=(str(source_run),*(
            (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
            for st in ((source_run/'manifest.json').stat(),(source_run/'run-plan.json').stat())))
        if self._source_owner_cache and self._source_owner_cache[0]==source_owner_identity:
            source_owner=self._source_owner_cache[1]
        else:
            source_owner=VersionAudio(self.root,source_manifest,self.project,self.installation)
            self._source_owner_cache=(source_owner_identity,source_owner)
        if source_owner.synthesis(bound_attempt)!=identity:
            raise ContractError('Originalsynthesebeleg gehört nicht zum Runplan')
        stages=old.get('stt_stages',[])
        if old.get('stt_status') in {'pass','warn'} and not stages:
            raise ContractError('Abgeschlossene STT-Belege fehlen')
        for stage in stages:
            receipt=resolve_relative(self.root,(source_run/'stt'/old['fingerprint']/(stage['stt_model']+'.json')).relative_to(self.root).as_posix(),must_exist=True)
            proof=json.loads(receipt.read_text());binding=proof['binding']
            if (proof['record']!=stage or proof['record_sha256']!=digest(stage) or
                    binding.get('attempt_fingerprint')!=old['fingerprint'] or
                    binding.get('wav_sha256')!=old['wav_sha256'] or
                    binding.get('qa_expected_text')!=attempt['qa_expected_text'] or
                    binding.get('model_sha256')!=stage.get('model_sha256') or
                    binding.get('decoder')!=stage.get('decoder') or binding.get('qa_policy')!=attempt['qa'] or
                    binding.get('accepted_forms')!=[attempt['qa_expected_text'],*attempt['qa_allowed_variants']]):
                raise ContractError('Original-STT-Beleg abweichend')
        return entry,audio,log,source_manifest,source_attempt,source_run,plan

    @classmethod
    def _source_run_bundle(cls, source_run, manifest_path, plan_path):
        """Parse/index an immutable source run once, not once per reused dialogue."""
        stat_m=manifest_path.stat();stat_p=plan_path.stat()
        signature=tuple((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns) for s in (stat_m,stat_p))
        key=str(source_run)
        with cls._source_run_cache_lock:
            cls._source_run_cache_bytes=sum(value[1] for value in cls._source_run_cache.values())
            cached=cls._source_run_cache.get(key)
            if cached and cached[0]==signature:
                cls._source_run_cache.move_to_end(key)
                return cached[2:]
            if cached:
                cls._source_run_cache_bytes-=cached[1]
                del cls._source_run_cache[key]
            # Keep the cold parse under this lock: concurrent reuse of the same
            # run must not allocate multiple complete manifest/plan trees.
            from manifest_renderer.json_memory import MetadataPool, read_compact, recursive_size
            pool=MetadataPool(max_bytes=8*1024*1024)
            manifest=read_compact(manifest_path,pool=pool)
            plan=read_compact(plan_path,pool=pool)
            plan_valid=(plan.get('manifest_sha256')==digest(manifest) and
                    plan.get('sha256')==digest({k:v for k,v in plan.items() if k!='sha256'}))
            index={a['fingerprint']:a for row in plan['dialogues'] for a in row['attempts']}
            result=(manifest,plan,index,plan_valid)
            if (signature==tuple((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
                    for s in (manifest_path.stat(),plan_path.stat()))):
                size=recursive_size(result)
                # A single source run can exceed the normal budget. Retain
                # precisely that one bundle so thousands of reused dialogues
                # do not each reparse and hash the same large run.
                if size>cls._source_run_cache_budget:
                    cls._source_run_cache.clear()
                    cls._source_run_cache_bytes=0
                else:
                    while cls._source_run_cache and cls._source_run_cache_bytes+size>cls._source_run_cache_budget:
                        _,evicted=cls._source_run_cache.popitem(last=False)
                        cls._source_run_cache_bytes-=evicted[1]
                cls._source_run_cache[key]=(signature,size,*result)
                cls._source_run_cache_bytes+=size
            return result

    def completed_candidates(self, attempt):
        """Index signed recipe metadata once, then hash only matching candidates."""
        if self._completed_index is None:
            index={}
            for path in sorted(self.directory.glob('*/*.json')):
                try:
                    safe=resolve_relative(self.root,path.relative_to(self.root).as_posix(),must_exist=True)
                    entry=json.loads(safe.read_text());body={k:v for k,v in entry.items() if k!='signature'}
                    if (self.owns(entry) and entry['phase']=='qa' and entry['generation']['version']==self.project['version'] and
                            hmac.compare_digest(entry.get('signature',''),self.sign(body))):
                        key=self.resume_recipe(entry['generation']['synthesis'])
                        index.setdefault(key,[]).append(path)
                except (OSError,ValueError,KeyError,TypeError): continue
            self._completed_index=index
        return self._completed_index.get(self.resume_recipe(self.synthesis(attempt)),[])

    def reuse_reference(self, path, source, attempt):
        entry,_,_,source_manifest,_,source_run,plan=source
        reference=dict(receipt_path=path.relative_to(self.root).as_posix(),receipt_sha256=digest(entry),
            source_run=source_run.relative_to(self.root).as_posix(),
            source_manifest_sha256=plan['manifest_sha256'],source_plan_sha256=plan['sha256'],
            attempt_fingerprint=attempt['fingerprint'])
        return {**reference,'signature':self.sign(reference)}

    def reused_source(self, attempt, record, *, deep=True):
        from manifest_renderer.audio import sha256_file
        reference=record['launcher_reuse'];body={k:v for k,v in reference.items() if k!='signature'}
        if (not hmac.compare_digest(reference.get('signature',''),self.sign(body)) or
                reference['attempt_fingerprint']!=attempt['fingerprint']):
            raise ContractError('Wiederverwendungsreferenz nicht lokal gebunden')
        path=resolve_relative(self.root,reference['receipt_path'],must_exist=True)
        source=self.completed_source(path,attempt,deep=deep)
        if self.reuse_reference(path,source,attempt)!=reference:
            raise ContractError('Originalbeleg-/Runplanreferenz abweichend')
        expected={**source[0]['record'],'fingerprint':attempt['fingerprint'],'launcher_reuse':reference}
        if {k:v for k,v in record.items() if k!='resumed'}!=expected:
            raise ContractError('Wiederverwendeter QA-Report wurde verändert')
        for key,hash_key in (('wav','wav_sha256'),('log','log_sha256')):
            artifact=resolve_relative(self.root,attempt['artifacts'][key],must_exist=True)
            if not artifact.is_file() or (deep and sha256_file(artifact)!=record[hash_key]):
                raise ContractError('Wiederverwendete Ausgabe wurde verändert')
        return source

    def binding(self, attempt, candidate):
        """Review references the original synthesis and QA, including its backend."""
        if candidate.get('launcher_reuse'):
            entry=self.reused_source(attempt,candidate)[0]
            receipt=resolve_relative(self.root,candidate['launcher_reuse']['receipt_path'],must_exist=True)
        else:
            found=self.find(attempt,audio_hash=candidate['wav_sha256'],qa_key=self.qa(attempt,candidate['wav_sha256'])['key'])
            if found is None: raise ContractError('Lokaler Audiobeleg fehlt')
            entry=found[0]
            receipt=self.directory/entry['generation']['synthesis']['key']/(digest({k:v for k,v in entry.items() if k!='signature'})+'.json')
        return dict(dialog_id=attempt['dialog_id'],synthesis_key=entry['generation']['synthesis']['key'],
            qa_key=entry['qa']['key'],audio_sha256=candidate['wav_sha256'],receipt_path=receipt.relative_to(self.root).as_posix())

    def store(self, root):
        from manifest_renderer.runner import AttemptStore, ArtifactError
        owner=self
        class LocalStore(AttemptStore):
            def cached(self, attempt):
                existing=super().cached(attempt)
                if existing is None:
                    wav,log,report=self._paths(attempt)
                    if any(p.exists() for p in (wav,log,report)):
                        raise ArtifactError('Unvollständige Artefakte werden nicht durch Wiederverwendung überschrieben')
                    for path in owner.completed_candidates(attempt):
                        try:
                            source=owner.completed_source(path,attempt)
                        except (OSError,ValueError,KeyError,TypeError,StopIteration): continue
                        entry,audio,source_log,*_=source
                        reference=owner.reuse_reference(path,source,attempt)
                        record={**entry['record'],'fingerprint':attempt['fingerprint'],'launcher_reuse':reference}
                        from manifest_renderer.audio import atomic_install
                        from manifest_renderer.runner import _exclusive_json
                        import tempfile
                        wav.parent.mkdir(parents=True,exist_ok=True)
                        with tempfile.TemporaryDirectory(prefix='.mode-reuse-',dir=wav.parent) as folder:
                            a=Path(folder)/'output.wav';l=Path(folder)/'render.log'
                            shutil.copyfile(audio,a);shutil.copyfile(source_log,l)
                            atomic_install(a,wav);atomic_install(l,log);_exclusive_json(report,record)
                        return record
                    return None
                if existing.get('launcher_reuse'):
                    try: owner.reused_source(attempt,existing)
                    except (OSError,ValueError,KeyError,TypeError,StopIteration) as exc:
                        raise ArtifactError('Modusübergreifende Wiederverwendung ist beschädigt') from exc
                    return existing
                bound=owner.find(attempt,audio_hash=existing['wav_sha256'],log_hash=existing['log_sha256'])
                if bound is None or bound[0]['generation']['audio']['sha256']!=existing['wav_sha256']:
                    raise ArtifactError('Legacy/fremdes Audio ohne gültigen lokalen Launcherbeleg wird nicht aktiviert')
                wav,log,_=self._paths(attempt)
                # A crash between report publication and the receipt is
                # recoverable only through the prior signed synthesis.
                owner.publish(attempt,wav,log,existing,phase='qa')
                return existing
            def run(self, attempt, executor):
                result=super().run(attempt,executor)
                wav,log,_=self._paths(attempt)
                if not result.get('launcher_reuse'):
                    owner.publish(attempt,wav,log,{k:v for k,v in result.items() if k!='resumed'},phase='qa')
                return result
        return LocalStore(root)

    def executor(self, factory):
        owner=self
        def create(root, manifest, sessions, auditor):
            delegate=factory(root,manifest,sessions,auditor)
            class BoundExecutor:
                def stage(self, attempt):
                    from manifest_renderer.production import immutable_json
                    from manifest_renderer.audio import sha256_file
                    from manifest_renderer.runner import ArtifactError
                    pending=delegate.pending_path(attempt)
                    record=delegate.read_pending(attempt) if pending.exists() else None
                    found=owner.find(attempt,**({'audio_hash':record['wav_sha256'],'log_hash':record['log_sha256']} if record else {}))
                    if pending.exists() and found is None:
                        raise ArtifactError('Unbelegte/fremde Pending-Synthese wird nicht als lokal erzeugt aktiviert')
                    if found is not None and not pending.exists():
                        entry,audio,log=found
                        pending.parent.mkdir(parents=True,exist_ok=True)
                        import tempfile,os
                        with tempfile.TemporaryDirectory(prefix='.reuse-',dir=pending.parent) as folder:
                            stage=Path(folder)/'render';stage.mkdir()
                            shutil.copyfile(audio,stage/'output.wav');shutil.copyfile(log,stage/'render.log')
                            old=entry['record']
                            record=dict(fingerprint=attempt['fingerprint'],wav_sha256=sha256_file(audio),
                                log_sha256=sha256_file(log),execution=old['execution'],
                                preparation=old.get('preparation',[]),render_execution=attempt['render_execution'])
                            immutable_json(stage/'render.json',record);os.rename(stage,pending)
                    result=delegate.stage(attempt)
                    record=delegate.read_pending(attempt)
                    owner.publish(attempt,pending/'output.wav',pending/'render.log',record,phase='synthesis')
                    return result
                def audit_pending(self, attempt, wav, log):
                    return delegate.audit_pending(attempt,wav,log)
                def __getattr__(self, name): return getattr(delegate, name)
            return BoundExecutor()
        return create


def attach_review_renderer(service, home, project, installation):
    """Keep the existing explicit candidate jobs, pool and exact review rules."""
    from patch_review.render import ProductionAdapter,release_tts_before_qa
    from .local_preparation import prepared_german_ute_directory
    identity=(str(service.evidence.source_path),service.evidence.manifest_hash,str(service.root.resolve()))
    existing=getattr(service,'executor',None)
    if getattr(existing,'_launcher_review_identity',None)==identity:
        return service
    close=getattr(existing,'close',None)
    if close:close()
    preparation_cache={}
    def resolve_prepared_game_root():
        receipt_path=home.parent/'MI1 Generator/generated/game-preparation/preparation.json'
        try:
            st=receipt_path.stat();token=[(str(receipt_path),st.st_size,st.st_mtime_ns,st.st_ctime_ns)]
            value=json.loads(receipt_path.read_text(encoding='utf-8'))
            outputs=value['preparation_receipt']['outputs']
            for key,entry in outputs.items():
                if isinstance(key,str) and key.lower() in {'german:monkey.000','german:monkey.001','german:monster.sou','german:monkey.sof'} and isinstance(entry,dict):
                    path=resolve_relative(home.parent,entry.get('path',''),must_exist=False)
                    try:
                        st=path.stat();token.append((str(path),st.st_size,st.st_mtime_ns,st.st_ctime_ns))
                    except OSError:token.append((str(path),None))
            token=tuple(sorted(token))
            if preparation_cache.get('token')==token:
                service.prepared_game_root_diagnostic=preparation_cache['diagnostic']
                service.prepared_game_basis=preparation_cache['basis']
                return preparation_cache['result']
        except (OSError,ValueError,KeyError,TypeError,ContractError):token=None
        diagnostic={};basis_info={}
        result=prepared_german_ute_directory(home.parent,project,installation,diagnostic,basis_info)
        service.prepared_game_root_diagnostic=diagnostic
        service.prepared_game_basis=basis_info or None
        preparation_cache.update(token=token,diagnostic=diagnostic,basis=basis_info or None,result=result)
        return result
    # Review audio does not need the game archive. Resolve and fully hash the
    # signed UTE basis only when the export controller asks for it.
    service.prepared_game_root=None
    service.prepared_game_root_resolver=resolve_prepared_game_root
    def host_guard(backend, _mode, *, facts):
        if backend=='cpu': raise ContractError('Intel/CPU ist im Launcher noch nicht implementiert')
        if backend=='ane': require_mode_available('medium')
        return validate_launcher_host(home,backend=backend,facts=facts)
    ane_config=None;ane_binding_error=None
    if (Path(home)/'.launcher/ane-worker.json').is_file():
        from .local_ane import resolve_local_ane
        try:ane_config=resolve_local_ane(home,read_only=True)
        except Exception as exc:ane_binding_error=str(exc)
    delegate=ProductionAdapter(home,ane_config=ane_config,host_validator=host_guard)
    from manifest_renderer.hardware import host_facts
    service.release_tts_before_qa=release_tts_before_qa(host_facts())
    service.audition_queue_enabled=__import__('patch_review.render',fromlist=['audition_queue_enabled']).audition_queue_enabled(host_facts())
    service.release_review_tts=delegate.release_warm_tts
    service.repair_parallel_allowed=delegate.repair_parallel_allowed
    def run_repair_prewarm(command,stream):
        manifest=copy.deepcopy(service.manifest)
        manifest['render_execution']=delegate.backend_profile('gpu',service.manifest)
        manifest['runtime']=delegate.runtime_for_attempt('gpu',service.manifest)
        try:return delegate.run_prewarm(command,stream,manifest,service.root)
        finally:
            if service.release_tts_before_qa:delegate.release_warm_tts()
    service.run_repair_prewarm=run_repair_prewarm
    class SignedAdapter:
        def backend_profile(self,backend,manifest):
            if backend=='ane':
                require_mode_available('medium')
                if ane_config is None:raise ContractError('Gebundene lokale ANE-Modelle sind nicht verfügbar'+(': '+ane_binding_error if ane_binding_error else ''))
            if backend=='cpu': raise ContractError('Intel/CPU ist im Launcher noch nicht implementiert')
            return delegate.backend_profile(backend,manifest)
        def runtime_for_attempt(self,*args): return delegate.runtime_for_attempt(*args)
        def _signed_execution(self,manifest,state_root,execution,*,audit_only=False):
            audio=VersionAudio(home,manifest,project,installation)
            class SignedExecution:
                completed=False
                def __init__(self):self.progress_callback=None;self.staged_fingerprint=None;self.staged_result=None
                def set_progress_callback(self,callback):
                    self.progress_callback=callback
                    return execution.set_progress_callback(callback)
                def stage(self,attempt):
                    if self.staged_fingerprint==attempt['fingerprint']:
                        return self.staged_result
                    if not hasattr(execution.executor,'stage'):
                        if self.resume_staged(attempt):return self.staged_result
                        raise ContractError('Nur exakt signierte TTS-Pendings dürfen ohne Synthese zur QA fortgesetzt werden')
                    synthesis=execution.executor.stage(attempt)
                    pending=execution.executor.pending_path(attempt)
                    record=execution.executor.read_pending(attempt)
                    if record is None:raise ContractError('Synthese-WAV wurde nicht dauerhaft zur QA gespeichert')
                    audio.publish(attempt,pending/'output.wav',pending/'render.log',record,phase='synthesis',
                        audio_path=(pending/'output.wav').relative_to(home).as_posix(),
                        log_path=(pending/'render.log').relative_to(home).as_posix())
                    self.staged_fingerprint=attempt['fingerprint'];self.staged_result=synthesis
                    return synthesis
                def resume_staged(self,attempt):
                    found=audio.find(attempt,attempt_fingerprint=attempt['fingerprint'],phase='synthesis')
                    if not found:return False
                    entry,wav_path,log_path=found
                    if entry.get('phase')!='synthesis' or entry.get('generation',{}).get('version')!=project['version']:
                        return False
                    pending=execution.executor.pending_path(attempt)
                    if wav_path!=pending/'output.wav' or log_path!=pending/'render.log':return False
                    record=execution.executor.read_pending(attempt)
                    if record is None or record!=entry.get('record'):return False
                    self.staged_fingerprint=attempt['fingerprint'];self.staged_result={}
                    return True
                def audit_pending(self,attempt,wav,log):
                    progress=self.progress_callback or (lambda *_args:None)
                    progress('qa','WAV gesichert; QA und Whisper-Base beginnen.')
                    if getattr(service,'release_tts_before_qa',True) and not getattr(service,'_audition_batch_tts_released',False):delegate.release_warm_tts()
                    auditor=getattr(execution.executor,'auditor',None)
                    if auditor is not None and hasattr(auditor,'progress_callback'):
                        auditor.progress_callback=progress
                    result=execution.executor.audit_pending(attempt,wav,log)
                    audio.publish(attempt,wav,log,result,phase='qa',
                        audio_path=(Path(state_root)/attempt['artifacts']['wav']).relative_to(home).as_posix(),
                        log_path=(Path(state_root)/attempt['artifacts']['log']).relative_to(home).as_posix())
                    self.completed=True;self.staged_fingerprint=None;self.staged_result=None
                    return result
                def __call__(self,attempt,wav,log):
                    try:
                        synthesis=self.stage(attempt)
                        return {**synthesis,**self.audit_pending(attempt,wav,log)}
                    except BaseException:
                        if not getattr(service,'_audition_batch_tts_released',False):delegate.release_warm_tts()
                        raise
                def close(self,preserve_staged=False):
                    preserve=self.completed or preserve_staged
                    if not preserve and not getattr(service,'_audition_batch_tts_released',False):delegate.release_warm_tts()
                    if hasattr(execution,'executor'):
                        execution.close(preserve_ane_worker=preserve)
                    else:execution.close()
            return SignedExecution()
        def for_manifest(self,manifest,state_root):
            execution=delegate.for_manifest(manifest,state_root)
            return self._signed_execution(manifest,state_root,execution)
        def audit_for_manifest(self,manifest,state_root):
            # QA requires the exact manifest and pending paths, but no TTS
            # model/session. Build only the auditor and its pending-file API.
            from manifest_renderer.production import ManifestAuditor,ManifestExecutor
            root=Path(state_root);run=root/manifest['output']['root']
            auditor=ManifestAuditor(root,manifest,run,asset_root=home)
            pending=ManifestExecutor.__new__(ManifestExecutor)
            pending.root=root;pending.manifest=manifest;pending.asset_root=Path(home);pending.auditor=auditor
            class AuditOnlyExecution:
                executor=pending
                def set_progress_callback(self,callback):
                    auditor.progress_callback=callback;return True
                def close(self,**_kwargs):pass
            return self._signed_execution(manifest,state_root,AuditOnlyExecution(),audit_only=True)
        def close(self):delegate.close()
        _launcher_review_identity=identity
        _production_adapter=delegate
    service.executor=SignedAdapter()
    return service


class RenderStatus:
    """Projection of production events; counts dialogues, never attempts."""
    def __init__(self):
        self.lock=threading.RLock();self.reset([])
    def reset(self, rows):
        self.rows={r['dialog_id']:r for r in rows};self.completed=set();self.workers={};self.qa_workers={}
        self.value=dict(status='idle',phase=None,total=len(rows),completed=0,remaining=len(rows),
                        warnings=0,qa_backlog=0,workers={},qa_workers={},error=None,
                        preparation_messages=[],manual_review=[],skipped_count=0,restart_required=False)
    def preparation(self, details):
        """Preparation counters describe roles, never completed dialogue jobs."""
        labels={'preparing':'Prewarm vorbereiten','reusing':'Vorhandenen Prewarm wiederverwenden',
                'loading_model':'Sprachmodell laden / Prewarm erzeugen','saving':'Prewarm speichern'}
        details={**details,'preparation_detail':labels.get(details.get('preparation_detail'),details.get('preparation_detail'))}
        with self.lock:
            for key in ('phase','role_id','role_name','preparation_completed','preparation_total',
                        'preparation_attempt','preparation_detail','preparation_duration_ms'):
                if key in details: self.value[key]=details[key]
            self.value['preparation_log_path']=str(details['log_path']) if details.get('log_path') else None
            text=' · '.join(str(v) for v in (details.get('role_name') or details.get('role_id'),
                details.get('preparation_detail'),
                str(details['preparation_duration_ms'])+' ms' if details.get('preparation_duration_ms') is not None else None) if v)
            messages=self.value['preparation_messages']
            if text and (not messages or messages[-1]!=text):
                messages.append(text)
                del messages[:-80]
    def event(self, event, **details):
        with self.lock:
            backend=details.get('backend')
            instance=details.get('instance_id') or backend
            if event=='adaptive_ane_status': self.value['adaptive_ane']=copy.deepcopy(details)
            if event=='resource_guard':
                self.value['resource_guard_reason']=details.get('reason')
                self.workers.clear();self.qa_workers.clear()
            if event in ('render_started','qa_started'):
                did=details.get('dialog_id');row=self.rows.get(did,{})
                worker=dict(dialog_id=did,text=details.get('text') or next((v['tts_text'] for v in row.get('text_variants',[]) if v['variant_id']==row.get('selected_variant_id')),''),role_id=details.get('role_id') or row.get('role_id'))
                if details.get('instance_id'): worker.update(backend=backend,instance_id=details['instance_id'])
                (self.workers if event.startswith('render') else self.qa_workers)[instance]=worker
                self.value['phase']='render' if event.startswith('render') else 'qa'
            elif event in ('render_finished','render_error','qa_finished','qa_error'):
                (self.workers if event.startswith('render') else self.qa_workers).pop(instance,None)
            elif event=='dialogue_completed':
                self.completed.add(details['dialog_id'])
                self.value['warnings']=details.get('counts',{}).get('warn',0)+details.get('counts',{}).get('fail',0)
            self.value.update(completed=len(self.completed),remaining=len(self.rows)-len(self.completed),
                              workers=copy.deepcopy(self.workers),qa_workers=copy.deepcopy(self.qa_workers))
    def progress(self, stats):
        with self.lock:
            self.value['qa_backlog']=stats.get('pending_qa_attempts',0)
            for key in ('manual_review','skipped_count','restart_required','tts_status','qa_status'):
                if key in stats: self.value[key]=copy.deepcopy(stats[key])
            if stats.get('qa_status') == 'auditing': self.value['phase']='qa'
            if 'adaptive_ane' in stats: self.value['adaptive_ane']=copy.deepcopy(stats['adaptive_ane'])
    def snapshot(self):
        with self.lock: return copy.deepcopy(self.value)


class RenderPauseGate:
    """Cooperative boundaries; active steps finish and publish before pausing."""
    def __init__(self, status):
        self.condition=threading.Condition();self.status=status
        self.requested=False;self.stopped=False;self.switching=False;self.active=0;self.local=threading.local()
    def _status(self, state):
        with self.status.lock: self.status.value['status']=state
    def pause(self):
        with self.condition:
            if self.requested: return
            self.requested=True;self._status('pausing')
    def resume(self):
        with self.condition:
            self.requested=False;self._status('running');self.condition.notify_all()
    def stop(self):
        with self.condition:
            self.stopped=True;self.condition.notify_all()
    def _wait(self):
        while self.requested and not self.stopped:
            self._status('pausing' if self.active else 'paused')
            self.condition.wait()
        if self.stopped: raise InterruptedError('Renderauftrag beim Beenden abgebrochen; vorhandene Audios und Reports bleiben erhalten')
    def checkpoint(self):
        with self.condition: self._wait()
    def step(self, *, isolate_dialogue=False):
        from contextlib import contextmanager
        @contextmanager
        def permit():
            if getattr(self.local,'inside',False):
                yield
                return
            with self.condition:
                self._wait();self.active+=1
            self.local.inside=True
            try:
                yield
            except BaseException as exc:
                from manifest_renderer.pipeline import isolatable_failure
                if not isolate_dialogue or not isolatable_failure(exc): self.stop()
                raise
            finally:
                self.local.inside=False
                with self.condition:
                    self.active-=1
                    if self.requested and not self.active and not self.stopped: self._status('paused')
                    self.condition.notify_all()
        return permit()


class LauncherRender:
    def __init__(self, root, *, prepare=None, producer=None, installation_factory=load_or_create_installation):
        self.root=Path(root)/'MI1 Generator';self.prepare=prepare;self.producer=producer
        self.installation_factory=installation_factory;self.status=RenderStatus();self.thread=None;self.review=None
        self.lock=threading.RLock();self.pause_gate=RenderPauseGate(self.status)
        from .repair import LauncherRepair
        self.repair=LauncherRepair(self)
        from .benchmark import LauncherBenchmark
        self.benchmark=LauncherBenchmark(self)
        self._benchmark_source=None;self._benchmark_prepared=None;self._production_owners=[];self._benchmark_borrowed_pools=[]
        self._render_selection=None;self._benchmark_policy={};self._auto_benchmark_checked=False
        self._switch_thread=None;self._switch_cancel=threading.Event();self._list_id=LEGACY_LIST
        self._selected_list_id=LEGACY_LIST
        self._review_activation_lock=threading.RLock()
        self._review_activation_run_lock=threading.Lock()
        self._review_activation=dict(status='idle',phase=None,detail='Review noch nicht geöffnet.',
            completed=0,total=0,error=None,elapsed_seconds=None)
        self._review_activation_started=None
        self._activated_review_identity=None
        self._activation_receipt_cache=OrderedDict()
        self._activation_receipt_cache_bytes=0
        self._activation_receipt_cache_budget=16*1024*1024
        self._launcher_warning_summary_cache=None
        self._launcher_warning_lock=threading.Lock()
        self.restore_status()
    def _close_review(self):
        service,self.review=self.review,None
        executor=getattr(service,'executor',None)
        close=getattr(executor,'close',None)
        if close:close()
    def _lists_path(self): return self.root/'.launcher/render-lists.json'
    def _read_lists(self):
        try:
            value=json.loads(self._lists_path().read_text())
            if isinstance(value,dict) and isinstance(value.get('lists'),list):
                value.setdefault('selected_list_id',value.get('active_list_id',LEGACY_LIST))
                return value
        except (OSError,ValueError,TypeError): pass
        return {'active_list_id':LEGACY_LIST,'selected_list_id':LEGACY_LIST,'lists':[]}
    def _catalog_item(self, identifier=None):
        identifier=identifier or self._selected_list_id
        return next((r for r in self._read_lists()['lists'] if isinstance(r,dict) and r.get('id')==identifier),None)
    def _activation_path(self, identifier=None):
        identifier=identifier or self._selected_list_id
        item=self._catalog_item(identifier)
        relative=item.get('activation') if item else None
        if relative:
            global_path=self.root/'.launcher/active-review.json'
            if identifier==self._read_lists().get('published_list_id') and global_path.exists():
                return global_path
            return resolve_relative(self.root,relative)
        # Before migration the only signed activation is the legacy pointer.
        if identifier==LEGACY_LIST: return self.root/'.launcher/active-review.json'
        return self.root/'.launcher/fassungen'/identifier/'active-review.json'
    def selected_activation(self):
        try:
            return json.loads(self._activation_path().read_text())
        except (OSError,ValueError,TypeError): return None
    def _historical_reviews(self, rows):
        """Recover pre-catalog reviews from their signed audio bindings and exact run."""
        from .host import atomic_json
        from .resume import _authenticated_review
        known={r.get('run') for r in rows.values()}
        retired=set()
        for path in (self.root/'.launcher/retired-fassungen').glob('*.json'):
            try: retired.add(json.loads(path.read_text()).get('run'))
            except (OSError,ValueError,TypeError): pass
        candidates=[]
        for path in (self.root/'reviews').glob('*/launcher-version.json'):
            try:
                state=validate_state(json.loads(path.read_text()))
                if state.get('kind')=='review' and state.get('status')=='completed' and state.get('bindings'):
                    candidates.append((path,state))
            except (OSError,ValueError,KeyError,TypeError): pass
        if not candidates: return
        installation=None
        runs=list((self.root/'runs').glob('*/manifest.json'))+list((self.root/'runs/render-lists').glob('*/*/manifest.json'))
        index={}
        for path in runs:
            try:
                relative=path.parent.relative_to(self.root).as_posix()
                if relative in known or relative in retired: continue
                manifest=json.loads(path.read_text())
                index[relative]=(path,manifest)
            except (OSError,ValueError,TypeError): pass
        for version_path,state in candidates:
            for relative,(manifest_path,manifest) in index.items():
                if str(uuid.uuid5(uuid.UUID(state['project_id']),relative))!=state['run_id']: continue
                try:
                    source_hash=manifest['source_evidence']['production']['source_manifest_sha256']
                    source=self.root/'input/local-render'/(source_hash+'.json')
                    if not source.is_file(): continue
                    if installation is None:
                        from .contracts import load_existing_installation
                        installation=(load_existing_installation(self.root) if self.installation_factory is load_or_create_installation
                            else self.installation_factory(self.root))
                    # The old completion metadata alone cannot authorize audio.
                    for binding in state['bindings']:
                        receipt=json.loads(resolve_relative(self.root,binding['receipt_path'],must_exist=True).read_text())
                        body={k:v for k,v in receipt.items() if k!='signature'}
                        signature=hmac.new(installation.receipt_key,canonical_json(body),hashlib.sha256).hexdigest()
                        if (not hmac.compare_digest(receipt.get('signature',''),signature) or
                                receipt.get('phase')!='qa' or (receipt.get('qa') or {}).get('key')!=binding['qa_key'] or
                                receipt['generation']['synthesis']['key']!=binding['synthesis_key'] or
                                receipt['generation']['audio']['sha256']!=binding['audio_sha256']):
                            raise ContractError('Historische Reviewbindung ungültig')
                    active=dict(source=source.relative_to(self.root).as_posix(),run=relative,
                        review=version_path.parent.relative_to(self.root).as_posix(),
                        version=state['version'],state_sha256=digest(state))
                    active['signature']=hmac.new(installation.receipt_key,canonical_json(active),hashlib.sha256).hexdigest()
                    if _authenticated_review(self.root.parent,self.root,project_id=state['project_id'],
                            active=active,installation=installation) is None: continue
                    identifier=manifest_list_id(manifest)
                    if identifier==LEGACY_LIST: identifier='legacy-review-'+version_path.parent.name
                    target=self.root/'.launcher/fassungen'/identifier/'active-review.json'
                    atomic_json(target,active)
                    count=len(manifest['dialogues'])
                    rows[identifier]={**rows.get(identifier,{}),'id':identifier,'label':'Fassung',
                        'version':state['version'],'status':'completed','completed':count,'total':count,
                        'run':relative,'mode':'medium' if _category(manifest=manifest)=='ANE' else 'fast' if _category(manifest=manifest)=='ANE+GPU' else 'slow',
                        'category':_category(manifest=manifest),'started_at':_timestamp(manifest_path),
                        'completed_at':_timestamp(version_path),'activation':target.relative_to(self.root).as_posix()}
                    known.add(relative)
                except (OSError,ValueError,KeyError,TypeError): continue
    def _migrate_catalog(self):
        """Retain old signed activations and original dates without loading models."""
        from .host import atomic_json
        data=self._read_lists();changed=False
        rows={r['id']:r for r in data['lists'] if isinstance(r,dict) and r.get('id')}
        try: saved=json.loads((self.root/'.launcher/render-status.json').read_text())
        except (OSError,ValueError,TypeError): saved={}
        pointer=self.root/'.launcher/active-review.json'
        try:
            active=json.loads(pointer.read_text())
            run=resolve_relative(self.root,active['run'],must_exist=True)
            manifest=json.loads((run/'manifest.json').read_text())
            identifier=manifest_list_id(manifest)
            prior=saved.get('status',{})
            if (identifier==LEGACY_LIST and prior.get('status') in INCOMPLETE_STATES and
                    prior.get('run') and prior['run']!=active['run']):
                identifier='legacy-review-'+Path(active['review']).name
            data['published_list_id']=identifier
            item=rows.get(identifier,{})
            # Preserve the original signature byte-for-byte in a per-Fassung pointer.
            target=self.root/'.launcher/fassungen'/identifier/'active-review.json'
            if not target.exists(): atomic_json(target,active)
            review=resolve_relative(self.root,active['review'],must_exist=True)
            count=len(manifest.get('dialogues',[]))
            item={**item,'id':identifier,'version':active['version'],'run':active['run'],
                'status':'completed','completed':count,'total':count,
                'mode':item.get('mode', 'medium' if _category(manifest=manifest)=='ANE' else 'fast' if _category(manifest=manifest)=='ANE+GPU' else 'slow'),
                'started_at':item.get('started_at') or _timestamp(run/'manifest.json'),
                'completed_at':item.get('completed_at') or _timestamp(review/'launcher-version.json'),
                'activation':target.relative_to(self.root).as_posix(),
                'category':item.get('category') or _category(manifest=manifest)}
            rows[identifier]=item;changed=True
        except (OSError,ValueError,KeyError,TypeError): pass
        job_id=saved.get('list_id') or data.get('running_list_id') or data.get('active_list_id',LEGACY_LIST)
        status=saved.get('status',{})
        if status.get('status') in INCOMPLETE_STATES | {'completed'} and (saved.get('version') or {}).get('manifest_sha256'):
            item=rows.get(job_id,{})
            # A genuine incomplete job stays separate from any completed review.
            if item.get('run') and status.get('run') and item['run']!=status['run'] and item.get('status')=='completed':
                job_id='legacy-job-'+digest([saved.get('version'),status.get('run')])[:16]
                item=rows.get(job_id,{})
            run_path=self.root/status['run']/'manifest.json' if status.get('run') else self.root/'.launcher/render-status.json'
            rows[job_id]={**item,'id':job_id,'version':saved.get('version'),
                'status':status['status'],'completed':status.get('completed',0),'total':status.get('total',0),
                'run':status.get('run') or item.get('run'),
                'started_at':item.get('started_at') or status.get('started_at') or _timestamp(run_path),
                'completed_at':item.get('completed_at') or status.get('completed_at'),
                'mode':item.get('mode','slow'),'category':item.get('category') or _category(item.get('mode'))}
            data['running_list_id']=job_id;changed=True
        if not data.get('historical_reviews_migrated'):
            self._historical_reviews(rows)
            data['historical_reviews_migrated']=True
            changed=changed or bool(rows)
        for item in rows.values():
            item.setdefault('label','Fassung')
            item.setdefault('category',_category(item.get('mode')))
            item.setdefault('started_at',None);item.setdefault('completed_at',None)
            if item['status'] in {'running','pausing','paused'}:
                item['status']='failed';item['restart_required']=True;changed=True
        if changed:
            if data.get('selected_list_id') not in rows:
                selected=data.get('running_list_id') or data.get('published_list_id') or next(iter(rows),LEGACY_LIST)
                data['selected_list_id']=selected;data['active_list_id']=selected
            data['lists']=list(rows.values());atomic_json(self._lists_path(),data)
    def _list_rows(self):
        data=self._read_lists(); rows=copy.deepcopy(data['lists'])
        current=self.status.snapshot()
        for item in rows:
            if item.get('id')==self._list_id:
                for key in ('status','completed','total','run','restart_required','error'):
                    if key in current: item[key]=current[key]
        return data,rows
    def select_list(self, list_id):
        """Selection is independent of the worker and never starts or resets it."""
        with self.lock:
            item=self._catalog_item(list_id)
            if item is None: raise ContractError('Fassung ist nicht mehr vorhanden')
            self._selected_list_id=list_id
            self._save_list(item,active=True)
            if self.review is not None:
                self.review.launcher_read_only=self.busy()
        return self.snapshot()
    def snapshot(self):
        value=self.status.snapshot(); data,rows=self._list_rows()
        try: value['warnings']=self.launcher_warning_count()
        except (OSError,ValueError,KeyError,TypeError,ContractError): pass
        selected=next((r for r in rows if r.get('id')==self._selected_list_id),None)
        running=self._list_id if (self.thread and self.thread.is_alive()) or (self._switch_thread and self._switch_thread.is_alive()) else None
        ready=bool(selected and selected.get('status')=='completed' and self.selected_activation())
        read_only=bool(self.busy())
        if self.review is not None: self.review.launcher_read_only=read_only
        value.update(render_lists=rows,list_id=self._selected_list_id,selected_list_id=self._selected_list_id,
            running_list_id=running,selected_fassung=selected,selected_review_ready=ready,
            read_only=read_only,mode=(selected or {}).get('mode',getattr(self,'_mode',None)),
            ultra_fast=(selected or {}).get('ultra_fast',getattr(self,'_ultra_fast',False)))
        value['review_activation']=self.activation_status()
        return value
    def launcher_warning_count(self):
        if not self._launcher_warning_lock.acquire(blocking=False):
            return self.status.snapshot().get('warnings',0)
        try:
            return self._compute_launcher_warning_count()
        finally:
            self._launcher_warning_lock.release()
    def _compute_launcher_warning_count(self):
        """Return effective warnings from already-bound source evidence.

        This does not activate/load the review UI. It inspects only selected
        warn records, authenticates their existing evidence through the shared
        Evidence validator, and caches the result against source/state files.
        WAV bytes and model files are never read.
        """
        active_path=self.root/'.launcher/active-review.json'
        if not active_path.is_file():return self.status.snapshot().get('warnings',0)
        current=self.status.snapshot()
        if ((self.thread and self.thread.is_alive()) or
                current.get('status') in {'running','pausing','paused'}):
            return current.get('warnings',0)
        active=json.loads(active_path.read_text())
        review_root=resolve_relative(self.root,active['review'],must_exist=True)
        version_path=review_root/'launcher-version.json';state_path=review_root/'review-state.json'
        run=resolve_relative(self.root,active['run'],must_exist=True)
        source_path=resolve_relative(self.root,active['source'],must_exist=True)
        overlay_dir=(self.root/'reviews'/'patch-review'/'stt-escalations'/run.name/
                     'patch-review-medium-overlay-v1')
        paths=(active_path,self.root/'.launcher/project.json',version_path,source_path,
               run/'manifest.json',run/'run-plan.json',run/'render-report.json',
               run/'backend-bindings.json',state_path,overlay_dir)
        identity=tuple(self._activation_file_identity(path) for path in paths)
        cached=self._launcher_warning_summary_cache
        from .contracts import load_existing_installation
        installation=load_existing_installation(self.root)
        body={k:v for k,v in active.items() if k!='signature'}
        signature=hmac.new(installation.receipt_key,canonical_json(body),hashlib.sha256).hexdigest()
        if (cached and cached[0]==identity and
                (not current.get('run') or current['run']==active.get('run')) and
                hmac.compare_digest(active.get('signature',''),signature)):
            return cached[1]
        config,_,_,_=self.selection()
        if active.get('version')!=config['project_state']['version']:
            return self.status.snapshot().get('warnings',0)
        from patch_review.evidence import Evidence
        state=validate_state(json.loads(version_path.read_text()))
        if (not hmac.compare_digest(active.get('signature',''),signature)
                or active.get('state_sha256')!=digest(state)
                or state.get('version')!=config['project_state']['version']):
            return self.status.snapshot().get('warnings',0)
        if current.get('run') and current['run']!=active.get('run'):
            return current.get('warnings',0)
        evidence=Evidence(source_path,self.root,run)
        if not evidence.valid:raise ContractError('Reviewquellbindung ungültig')
        saved=json.loads(state_path.read_text()) if state_path.is_file() else {}
        rows=saved.get('rows',{}) if isinstance(saved,dict) else {}
        plans={did:{a['fingerprint']:a for a in row.get('attempts',[])}
               for did,row in evidence.plans.items()}
        warnings=0
        for did,report in evidence.reports.items():
            selected=(report.get('selected') or {}).get('fingerprint')
            record=next((r for r in report.get('attempts',[]) if r.get('fingerprint')==selected),None)
            # A failed or unselected dialogue remains a warning; only a fully
            # bound selected QA warning can be reclassified as equivalent.
            if report.get('qa_status')=='fail' or record is None:
                warnings+=int(report.get('qa_status') in ('warn','fail'))
                continue
            if report.get('qa_status')!='warn':continue
            attempt=plans.get(did,{}).get(selected)
            if attempt is None:
                warnings+=1;continue
            output=evidence.output(attempt,record)
            if output.get('warning'):
                from patch_review.service import ReviewService
                approved_state=rows.get(did,{}) if isinstance(rows,dict) else {}
                if not output.get('verified') or not ReviewService.is_approved(approved_state,output.get('binding')):warnings+=1
        if identity==tuple(self._activation_file_identity(path) for path in paths):
            self._launcher_warning_summary_cache=(identity,warnings)
        return warnings
    def activation_status(self):
        with self._review_activation_lock:
            value=copy.deepcopy(self._review_activation)
            if value['status']=='running' and self._review_activation_started is not None:
                value['elapsed_seconds']=round(time.monotonic()-self._review_activation_started,1)
            return value
    def _activation_progress(self,phase,detail,completed=0,total=0,*,status='running',error=None,started=None):
        with self._review_activation_lock:
            self._review_activation_started=started if status=='running' else None
            self._review_activation=dict(status=status,phase=phase,detail=detail,completed=completed,total=total,
                error=error,elapsed_seconds=round(time.monotonic()-started,1) if started is not None else None)
    @staticmethod
    def _activation_file_identity(path):
        path=Path(path)
        try:
            st=path.stat()
            return (str(path.resolve()),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
        except OSError:return (str(path),None)
    def _review_service_identity(self,active_path,version_path,source_path,run_path,state_path):
        return tuple(self._activation_file_identity(path) for path in (
            active_path,version_path,source_path,run_path/'manifest.json',run_path/'run-plan.json',
            run_path/'render-report.json',run_path/'backend-bindings.json',state_path))
    def _save_list(self, item, *, active=True):
        from .host import atomic_json
        with self.lock:
            data=self._read_lists();rows={r['id']:r for r in data['lists'] if isinstance(r,dict) and r.get('id')}
            rows[item['id']]={**rows.get(item['id'],{}),**item}
            if active:
                data['selected_list_id']=item['id'];data['active_list_id']=item['id']
            data['lists']=list(rows.values())
            atomic_json(self._lists_path(),data)
    def _new_list(self, mode, ultra_fast, version, *, replaces=None):
        identifier=str(uuid.uuid4()); item=dict(id=identifier,label='Fassung',
            mode=mode,ultra_fast=ultra_fast,category=_category(mode),version=version,
            status='idle',completed=0,total=0,run=None,started_at=None,completed_at=None)
        if replaces: item['replaces_list_id']=replaces
        self._save_list(item);return identifier
    def _remove_exclusive_storage(self, old, remaining):
        """Delete only UUID-owned trees after ruling out retained source references."""
        identifier=old['id'];retained=[];removed=[]
        try:
            if str(uuid.UUID(identifier))!=identifier: return [],['Legacy-Daten bleiben als gemeinsame Quellen erhalten']
        except (ValueError,AttributeError): return [],['Legacy-Daten bleiben als gemeinsame Quellen erhalten']
        try:
            pointer=resolve_relative(self.root,old['activation'],must_exist=True)
            activation=json.loads(pointer.read_text())
            review=Path(activation['review'])
            if len(review.parts)!=2 or review.parts[0]!='reviews': return [],['Reviewpfad ist nicht exklusiv']
            if str(uuid.UUID(review.name))!=review.name: return [],['Reviewpfad ist nicht exklusiv']
            prefix='runs/render-lists/'+identifier
            if not activation['run'].startswith(prefix+'/'): return [],['Runpfad ist nicht exklusiv']
            needles=(prefix,activation['review'],'.launcher/audio-receipts/render-lists/'+identifier)
            candidates=[self.root/prefix,self.root/activation['review'],
                self.root/'.launcher/audio-receipts/render-lists'/identifier,
                self.root/'.launcher/fassungen'/identifier]
            own_roots=tuple(path.resolve() for path in candidates)
            def owned(path):
                resolved=path.resolve()
                return any(resolved==root or root in resolved.parents for root in own_roots)
            # Check retained manifests and review decisions, including accepted
            # prewarm/audio references. Receipts never cross independent list IDs.
            documents=list((self.root/'input').rglob('*.json'))
            documents+=list((self.root/'runs').glob('*/manifest.json'))
            documents+=list((self.root/'runs/render-lists').glob('*/*/manifest.json'))
            documents+=list((self.root/'reviews').rglob('*.json'))
            documents+=list((self.root/'.launcher/fassungen').glob('*/active-review.json'))
            for document in documents:
                if owned(document): continue
                if document.is_symlink(): return [],['Symbolischer Quellverweis bleibt erhalten']
                text=document.read_text()
                if any(needle in text for needle in needles):
                    return [],['Andere gespeicherte Quellen verweisen noch auf diese Fassung']
            for row in remaining:
                if (row.get('run') or '').startswith(prefix+'/'):
                    return [],['Eine andere Fassung verwendet den Runpfad']
            for path in candidates:
                # resolve_relative rejects symlinks in every parent component.
                safe=resolve_relative(self.root,path.relative_to(self.root).as_posix())
                if not safe.exists(): continue
                if any(child.is_symlink() for child in safe.rglob('*')):
                    retained.append(path.relative_to(self.root).as_posix());continue
                try:
                    shutil.rmtree(safe);removed.append(path.relative_to(self.root).as_posix())
                except OSError:
                    retained.append(path.relative_to(self.root).as_posix())
            return removed,retained
        except (OSError,ValueError,KeyError,TypeError):
            return [],['Nicht eindeutig exklusive Daten bleiben erhalten']
    def _retire_replaced(self):
        """A failed successor never reaches this completion-only retirement."""
        from .host import atomic_json
        item=self._catalog_item(self._list_id) or {}
        replaced=item.get('replaces_list_id')
        if not replaced or item.get('status')!='completed': return
        with self.lock:
            data=self._read_lists()
            old=next((r for r in data['lists'] if r.get('id')==replaced),None)
            if old is None: return
            archived={**old,'retired_at':_timestamp(),'replaced_by':self._list_id}
            try: archived['retired_activation']=json.loads(resolve_relative(self.root,old['activation'],must_exist=True).read_text())
            except (OSError,ValueError,KeyError,TypeError): pass
            archive_path=self.root/'.launcher/retired-fassungen'/(replaced+'.json')
            atomic_json(archive_path,archived)
            data['lists']=[r for r in data['lists'] if r.get('id')!=replaced]
            if self._selected_list_id==replaced:
                self._selected_list_id=self._list_id
                data['selected_list_id']=self._list_id;data['active_list_id']=self._list_id
                self._close_review()
            if data.get('published_list_id')==replaced:
                activation=json.loads(resolve_relative(self.root,item['activation'],must_exist=True).read_text())
                atomic_json(self.root/'.launcher/active-review.json',activation)
                data['published_list_id']=self._list_id
            atomic_json(self._lists_path(),data)
            removed,retained=self._remove_exclusive_storage(old,data['lists'])
            atomic_json(archive_path,{**archived,'removed_paths':removed,'retained_paths':retained})
    def restore_status(self):
        self._migrate_catalog()
        data=self._read_lists(); active=data.get('running_list_id') or data.get('active_list_id',LEGACY_LIST)
        self._selected_list_id=data.get('selected_list_id',data.get('active_list_id',LEGACY_LIST))
        item=next((r for r in data.get('lists',[]) if isinstance(r,dict) and r.get('id')==active),None)
        self._list_id=active if item else LEGACY_LIST
        if item:
            self._mode=item.get('mode');self._ultra_fast=item.get('ultra_fast',False)
        path=self.root/'.launcher/render-status.json'
        try:
            saved=json.loads(path.read_text(encoding='utf-8'))
            config,_,manifest,_=self.selection()
            if saved['version']!=config['project_state']['version'] or saved['status'].get('status')=='idle':
                raise ValueError('Renderstatus passt nicht zur vorhandenen Fassung')
            self.status.reset(manifest['dialogues'])
            self.status.value.update(saved['status'])
            self.status.value.update(workers={},qa_workers={})
            if self.status.value['status'] in {'running','pausing','paused'}:
                self.status.value.update(status='failed',restart_required=True,
                    error='Vorheriger Renderlauf wurde beendet; gesicherter Fortschritt bleibt erhalten')
            return
        except (OSError,ValueError,KeyError,TypeError):
            pass
        # A completed, signed review survives a missing/stale launcher status.
        # Interrupted repair progress is independent of first-pass completion.
        try:
            from .resume import completed_review_status
            config,_,manifest,_=self.selection()
            completed=completed_review_status(self.root,config)
            if completed is not None:
                self.status.reset(manifest['dialogues'])
                self.status.value.update(completed)
        except (OSError,ValueError,KeyError,TypeError):
            pass
    def persist_status(self, config=None):
        from manifest_renderer.production import atomic_json
        if config is None: config=getattr(self,'_render_config',None) or self.selection()[0]
        atomic_json(self.root/'.launcher/render-status.json',
            {'version':config['project_state']['version'],'list_id':self._list_id,'status':self.status.snapshot()})
        value=self.status.snapshot();item=self._catalog_item(self._list_id) or {}
        started=item.get('started_at') or value.get('started_at')
        completed=item.get('completed_at')
        if value.get('status')=='completed': completed=completed or _timestamp()
        self._save_list({**item,'id':self._list_id,'mode':getattr(self,'_mode',item.get('mode')),
            'ultra_fast':getattr(self,'_ultra_fast',item.get('ultra_fast',False)),
            'category':item.get('category') or _category(getattr(self,'_mode',None)),
            'version':config['project_state']['version'],'status':value.get('status','idle'),
            'completed':value.get('completed',0),'total':value.get('total',0),
            'run':value.get('run') or item.get('run'),'started_at':started,'completed_at':completed,
            'restart_required':value.get('restart_required',False),'error':value.get('error')},active=False)
        with self.lock:
            from .host import atomic_json
            data=self._read_lists();data['running_list_id']=self._list_id
            atomic_json(self._lists_path(),data)
    def production_progress(self, stats):
        self.status.progress(stats)
        self.persist_status()
    def busy(self):
        return bool((self.thread and self.thread.is_alive()) or (self._switch_thread and self._switch_thread.is_alive()) or self.repair.busy() or
            (self.review and self.review.state['job']['status']=='running') or self.benchmark.busy())
    def queue_benchmark(self,count=12):return self.benchmark.queue(count)
    def benchmark_status(self):return {**self.benchmark.status(),'busy':self.benchmark.busy()}
    def cancel_benchmark(self):return self.benchmark.cancel()
    def repair_status(self):return {**self.repair.status(),**self.repair_availability()}
    def start_repair(self, *, confirmed=False):return self.repair.start(confirmed=confirmed)
    def current_render_incomplete(self, config=None):
        item=self._catalog_item()
        if item is not None:
            if config and item.get('version')!=config['project_state']['version']: return False
            return item.get('status') in INCOMPLETE_STATES
        try:
            config=config or self.selection()[0]
            saved=json.loads((self.root/'.launcher/render-status.json').read_text(encoding='utf-8'))
            return (saved['version']==config['project_state']['version'] and
                saved['status']['status'] in INCOMPLETE_STATES)
        except (OSError,ValueError,KeyError,TypeError): return False
    def repair_availability(self):
        try:
            if self.busy():return dict(ready=False,ready_reason='Der laufende Auftrag muss erst vollständig enden')
            if self.current_render_incomplete():return dict(ready=False,ready_reason='Aktuelle Audiofassung ist unvollständig; übersprungene Dialoge manuell prüfen')
            active=self.selected_activation()
            config=json.loads((self.root/'.launcher/project.json').read_text())
            item=self._catalog_item()
            if not active or active['version']!=(item or {}).get('version',config['project_state']['version']):
                return dict(ready=False,ready_reason='Gewählte Fassung noch nicht vollständig gerendert')
            report=json.loads(resolve_relative(self.root,active['run']+'/render-report.json',must_exist=True).read_text())
            ready=report.get('all_ids_accounted') is True and report.get('status')=='complete'
            return dict(ready=ready,ready_reason='' if ready else 'Erste vollständige Renderpassage fehlt')
        except (OSError,ValueError,KeyError,TypeError):
            return dict(ready=False,ready_reason='Erst die aktuelle Fassung vollständig rendern')
    def repair_review(self):
        readiness=self.repair_availability()
        if not readiness['ready']:raise ContractError(readiness['ready_reason'])
        service=self.activate_review() # Verify current version, signatures and exact audio bindings.
        expected={row['dialog_id'] for row in service.evidence.plan['dialogues']}
        reported=[row['dialog_id'] for row in service.evidence.report['dialogues']]
        scope=set(service.manifest['scope']['included_dialog_ids'])
        if (service.evidence.report.get('all_ids_accounted') is not True or expected!=scope
                or set(reported)!=expected or len(reported)!=len(expected)):
            raise ContractError('Reparatur benötigt die vollständige erste Renderpassage aller Dialoge')
        return service
    def review_availability(self):
        busy=bool((self.thread and self.thread.is_alive()) or
                  (self.repair.busy() and threading.current_thread() not in
                   {self.repair.thread,*getattr(self.repair,'worker_threads',())}) or self.benchmark.busy())
        return dict(busy=busy,busy_source='launcher',reason='Launcher-Render-/Reparaturauftrag läuft bereits')
    def pause(self):
        with self.lock:
            if self.benchmark.busy():raise ContractError('Benchmark übernimmt die sichere Pause bis zum Abschluss')
            if not (self.thread and self.thread.is_alive()) or self.status.snapshot()['status'] not in ('running','pausing','paused'):
                raise RuntimeError('Kein laufender Launcher-Renderauftrag zum Anhalten')
            self.pause_gate.pause()
        return self.status.snapshot()
    def resume(self):
        with self.lock:
            if self.benchmark.busy():raise ContractError('Benchmark übernimmt die sichere Fortsetzung bis zum Abschluss')
            if not (self.thread and self.thread.is_alive()) or self.status.snapshot()['status'] not in ('pausing','paused'):
                raise RuntimeError('Kein angehaltener Launcher-Renderauftrag zum Fortsetzen')
            self.pause_gate.resume()
        return self.status.snapshot()
    def stop(self):
        self._switch_cancel.set()
        self.benchmark.cancel()
        self.pause_gate.stop()
    def preparation_progress(self, details):
        # Saving/model callbacks are inside a live synthesis. Wait only once
        # outputs have been saved, or before starting the next preparation unit.
        if (details.get('preparation_detail') not in ('loading_model','saving')
                and not getattr(self.pause_gate.local,'inside',False)):
            self.pause_gate.checkpoint()
        self.status.preparation(details)
    def paused_store(self, factory):
        gate=self.pause_gate
        def create(*args,**kwargs):
            delegate=factory(*args,**kwargs)
            class Store:
                def cached(self,*args,**kwargs):
                    with gate.step(): return delegate.cached(*args,**kwargs)
                def run(self,*args,**kwargs):
                    # Includes QA output/report and signed receipt publication.
                    with gate.step(isolate_dialogue=True): return delegate.run(*args,**kwargs)
                def __getattr__(self,name): return getattr(delegate,name)
            return Store()
        return create
    def production_event(self, event, **details):
        if event == 'dialogue_completed': self.pause_gate.checkpoint()
        self.status.event(event,**details)
    def paused_executor(self, factory):
        gate=self.pause_gate
        def create(*args,**kwargs):
            with gate.step():
                delegate=factory(*args,**kwargs)
                sessions=args[2] if len(args)>2 else kwargs.get('sessions')
                if sessions is not None:
                    with self.lock:self._production_owners.append((delegate,sessions))
            auditor=args[3] if len(args)>3 else kwargs.get('auditor')
            if auditor is not None and not getattr(auditor,'_launcher_pause_cancel',False):
                cancel=getattr(auditor,'cancel',lambda:None)
                def cancel_all():
                    gate.stop();cancel()
                auditor.cancel=cancel_all;auditor._launcher_pause_cancel=True
            class Executor:
                def preload(self,*args,**kwargs):
                    with gate.step():return delegate.preload(*args,**kwargs)
                def stage(self,*args,**kwargs):
                    with gate.step(isolate_dialogue=True): return delegate.stage(*args,**kwargs)
                def audit_pending(self,*args,**kwargs):
                    with gate.step(isolate_dialogue=True): return delegate.audit_pending(*args,**kwargs)
                def __getattr__(self,name): return getattr(delegate,name)
            return Executor()
        create.is_paused=lambda:gate.requested or gate.stopped
        def benchmark_policy():
            policy=dict(self._benchmark_policy)
            if self._render_config and self._render_config.get('render_recipe'):
                policy.pop('preferred_backend',None)
            return policy
        create.benchmark_policy=benchmark_policy
        return create
    def benchmark_prepared(self, manifest):
        with self.lock: self._benchmark_prepared=manifest
        if self.prepare is None and self._render_selection is not None and not self._auto_benchmark_checked:
            self.ensure_benchmark(manifest)

    def ensure_benchmark(self, prepared):
        """Use the first materialized role; production TTS waits at the pause gate."""
        from .benchmark import capture_source,cache_key,cached_report,select_method
        from manifest_renderer import production as p
        from .local_ane import resolve_local_ane
        self._auto_benchmark_checked=True
        source=self.root/'.launcher/benchmark-sources'/(digest(prepared)+'-input.json')
        p.immutable_json(source,prepared)
        gpu=p.prepare_production(source,self.root,backend='gpu',full_integrity=True)
        ane=p.prepare_production(source,self.root,backend='ane',ane_worker_config=resolve_local_ane(self.root),full_integrity=True)
        key=cache_key(gpu,ane)
        cached=cached_report(key)
        if cached is not None:
            selection=select_method(cached,self._render_selection)
            if selection['selection_applied']:self._benchmark_policy.update(selection)
            selection['selection_reason']='Gespeicherte Messung dieser Maschine und Synthesekomponenten: '+selection['selection_reason']
            self.benchmark.update(status='cached',phase='report',variants=cached['variants'],probes=cached.get('probes',[]),
                speedups=cached.get('speedups',{}),ratio_reason=cached.get('ratio_reason'),error=None,solution=None,**selection)
            return
        self._benchmark_source=capture_source(self.root,gpu,source)
        self.benchmark.queue()
        # The callback still owns the current preparation step. Let borrowing
        # request its pause before leaving it, so no production request slips in.
        while self.benchmark.busy() and not self.pause_gate.requested:
            threading.Event().wait(.01)

    def apply_benchmark(self, report):
        from .benchmark import select_method
        from .benchmark import save_cached_report
        from manifest_renderer.production import atomic_json
        save_error=None
        try:save_cached_report(report)
        except OSError as exc:save_error='Maschinenmessung konnte nicht gespeichert werden: '+str(exc)
        ongoing=bool(self.thread and self.thread.is_alive())
        config=getattr(self,'_render_config',None) if ongoing else None
        if config is None:
            try: config=self.selection()[0]
            except (OSError,ValueError,KeyError,ContractError): pass
        if config is not None:
            try:
                atomic_json(self.root/'.launcher/benchmark-preference.json',
                    dict(version=config['project_state']['version'], report=report))
            except OSError as exc:save_error='Projektmessung konnte nicht gespeichert werden: '+str(exc)
        if not ongoing or self._render_selection is None:
            return dict(selected_method=None, selected_workers=None, selection_applied=False,
                selection_reason=save_error or 'Messung gespeichert; die nächste ausdrücklich gestartete Renderwahl bestimmt die zulässige Methode')
        selection=select_method(report,self._render_selection)
        if save_error:selection['selection_reason']+='; '+save_error
        if selection['selection_applied']:
            self._benchmark_policy.clear();self._benchmark_policy.update(selection)
        return selection

    def selection(self):
        config=json.loads((self.root/'.launcher/project.json').read_text())
        source=resolve_relative(self.root,config['manifest'],must_exist=True)
        raw=source.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=config['project_state']['version']['manifest_sha256']:
            raise ContractError('Gewählte Manifestdatei wurde nach dem Speichern verändert')
        manifest=json.loads(raw)
        correction=None
        if config.get('correction'):
            path=resolve_relative(self.root,config['correction'],must_exist=True)
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=path.stem:
                raise ContractError('Gewähltes Rezept wurde nach dem Speichern verändert')
            correction=json.loads(raw)
        return config,source,manifest,correction
    def approved_review_projection(self, config, manifest, correction, installation):
        """Overlay accepted seeds from the signed active review for the same source.

        Review edits are stored beside their run, while the normal launcher
        continues to use its immutable selected manifest. This projection
        admits only the concrete seed inputs written after an output was
        accepted by the active review; it does not change the selected source
        or the resume identity.
        """
        active_path=getattr(self,'_render_input_activation_path',None) or self.root/'.launcher/active-review.json'
        if active_path.is_symlink() or not active_path.is_file():return manifest
        try:
            active_identity=self._activation_file_identity(active_path)
            active=json.loads(active_path.read_text(encoding='utf-8'))
            project_state=config['project_state'];version=project_state['version']
            if active.get('version')!=version:return manifest
            body={key:value for key,value in active.items() if key!='signature'}
            signature=hmac.new(installation.receipt_key,canonical_json(body),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(active.get('signature',''),signature):return manifest
            review_root=resolve_relative(self.root,active['review'],must_exist=True)
            version_path=review_root/'launcher-version.json'
            if version_path.is_symlink() or not version_path.is_file():return manifest
            version_identity=self._activation_file_identity(version_path)
            review_state=validate_state(json.loads(version_path.read_text(encoding='utf-8')))
            if (review_state.get('kind')!='review' or review_state.get('project_id')!=project_state['project_id']
                    or review_state.get('version')!=version or active.get('state_sha256')!=digest(review_state)):
                return manifest
            source_path=resolve_relative(self.root,active['source'],must_exist=True)
            source_identity=self._activation_file_identity(source_path)
            source_manifest=json.loads(source_path.read_text(encoding='utf-8'))
            correction_sha=digest(correction) if correction is not None else None
            expected_selection=dict(manifest_sha256=version['manifest_sha256'],correction_sha256=correction_sha)
            source_selection=source_manifest.get('source_evidence',{}).get('launcher_selection',{})
            if any(source_selection.get(key)!=value for key,value in expected_selection.items()):return manifest
            projection_path=review_root/'review-manifest.json'
            if projection_path.is_symlink() or not projection_path.is_file():return manifest
            projection_identity=self._activation_file_identity(projection_path)
            projection=json.loads(projection_path.read_text(encoding='utf-8'))
            if (projection.get('manifest_id')!=manifest.get('manifest_id')
                    or projection.get('game',{}).get('game_id')!=manifest.get('game',{}).get('game_id')):
                return manifest
            projected_selection=projection.get('source_evidence',{}).get('launcher_selection',{})
            if any(projected_selection.get(key)!=value for key,value in expected_selection.items()):return manifest
            # Recheck the signed/versioned inputs after reading their projections
            # so concurrent review changes cannot create a mixed snapshot.
            if any(path.is_symlink() for path in (active_path,version_path,source_path,projection_path)):return manifest
            if (self._activation_file_identity(active_path)!=active_identity
                    or self._activation_file_identity(version_path)!=version_identity
                    or self._activation_file_identity(source_path)!=source_identity
                    or self._activation_file_identity(projection_path)!=projection_identity):
                return manifest
        except (OSError,ValueError,KeyError,TypeError,ContractError):
            return manifest

        result=copy.deepcopy(manifest)
        evidence=result.setdefault('source_evidence',{})
        approved=projection.get('source_evidence',{}).get('launcher_render_inputs',{})
        if not isinstance(approved,dict):return manifest
        target=evidence.setdefault('launcher_render_inputs',{})
        known={row['dialog_id'] for row in manifest.get('dialogues',[]) if row.get('processing')=='render'}
        for did,value in approved.items():
            if did not in known or not isinstance(value,dict):continue
            seeds=value.get('standard_seeds')
            if not isinstance(seeds,list) or len(seeds)!=1:continue
            if type(seeds[0]) is not int or seeds[0]<0:
                raise ContractError('Persistierter Reviewseed ist ungültig: '+did)
            target[did]={'standard_seeds':[seeds[0]]}
        reviewed_recipe=projection.get('source_evidence',{}).get('portable_render_recipe')
        if isinstance(reviewed_recipe,dict):
            evidence['portable_render_recipe']=copy.deepcopy(reviewed_recipe)
        return result
    def start(self, mode=None, *, ultra_fast=False, list_policy='mixed', list_id=None, replace_list_id=None):
        if list_policy not in {'mixed','new','replace'}: raise ContractError('Unbekannte Fassungs-Auswahl')
        with self.lock:
            target_id=list_id or self._selected_list_id
            item=self._catalog_item(target_id)
            if list_id and item is None: raise ContractError('Fassung ist nicht mehr vorhanden')
            # A completed selection is immutable until the user chooses an explicit action.
            completed=(item or {}).get('status')=='completed'
            if item is None and target_id==self._list_id:
                completed=self.status.snapshot().get('status')=='completed'
            if completed and list_policy=='mixed':
                return {**self.snapshot(),'requires_action':'completed_fassung',
                    'reason':'Fassung bereits fertig; ausdrücklich neue Fassung oder Ersetzen wählen'}
            active=bool(self.thread and self.thread.is_alive())
            if (active and item is not None and target_id==self._list_id and list_policy=='mixed' and
                    self.status.snapshot().get('status') in {'pausing','paused'}):
                requested_mode=mode or item.get('mode','slow')
                requested_ultra=ultra_fast if mode is not None else item.get('ultra_fast',False)
                if requested_mode!=getattr(self,'_mode',None) or requested_ultra!=getattr(self,'_ultra_fast',False):
                    return self.switch_mode(requested_mode,ultra_fast=requested_ultra,list_policy='mixed')
                self.resume()
                return self.snapshot()
            internal=threading.current_thread() is self._switch_thread
            if self.busy() and not (internal and self.thread and not self.thread.is_alive()):
                raise RuntimeError('Launcher-/Review-Renderauftrag läuft bereits')
            if mode is None and item is not None and list_policy=='mixed':
                mode=item.get('mode','slow');ultra_fast=item.get('ultra_fast',False)
            if mode is not None: require_mode_available(mode,ultra_fast=ultra_fast)
            saved_selection=self.selection() if (self.root/'.launcher/project.json').is_file() else None
            if saved_selection is None: raise ContractError('Manifest zuerst wählen und speichern')
            config,source,manifest,correction=saved_selection
            if mode is None:
                mode=recipe_mode(validate_recipe(correction,manifest)) if correction and correction.get('format')==RECIPE_FORMAT else 'slow'
                if mode=='slow': ultra_fast=False
            selected=require_mode_available(mode,ultra_fast=ultra_fast)
            if self.prepare is None:
                if not (self.root/'.launcher/runtime.json').is_file():
                    raise ContractError('Runtimes und Modelle sind noch nicht vollständig eingerichtet. '
                                        'Zuerst „Runtimes und Modelle automatisch einrichten“ erfolgreich abschließen; '
                                        'es wurde kein Renderauftrag gestartet.')
                if not (self.root/'generated/game-preparation/preparation.json').is_file():
                    raise ContractError('Die lokale Vorbereitung ist noch nicht abgeschlossen. '
                                        'Zuerst die gewählte PC-SE- oder UTE-Entwicklungsbasis mit deutschem Patch '
                                        'in Schritt 2 erfolgreich abschließen; es wurde kein Renderauftrag gestartet.')
            input_activation_path=self._activation_path(target_id) if item and item.get('status')=='completed' else None
            replacement=None
            if list_policy=='replace':
                replacement=replace_list_id or target_id
                old=self._catalog_item(replacement)
                if old is None or old.get('status')!='completed':
                    raise ContractError('Ersetzen benötigt eine vorhandene fertige Fassung')
            if list_policy in {'new','replace'} or item is None:
                target_id=self._new_list(mode,ultra_fast,config['project_state']['version'],replaces=replacement)
                item=self._catalog_item(target_id)
            else:
                if item.get('version')!=config['project_state']['version']:
                    raise ContractError('Fassung gehört zu einer anderen Manifestfassung; ausdrücklich neue Fassung wählen')
            self._list_id=target_id;self._selected_list_id=target_id
            self._save_list(item,active=True)
            self._mode=mode;self._ultra_fast=ultra_fast
            self._render_input_activation_path=input_activation_path
            self._render_selection=selected;self._benchmark_policy={};self._auto_benchmark_checked=False
            if self.prepare is None:
                from .voice_choices import VoiceChoices
                config={**config,'voice_choices':VoiceChoices(self.root,manifest).capture()}
            self._render_config=config
            self._benchmark_source=None;self._benchmark_prepared=None;self._production_owners=[]
            # Retained completed reviews stay available for listening during this job.
            self.pause_gate=RenderPauseGate(self.status);self.status.reset(manifest['dialogues'])
            self.status.value.update(status='running',phase='preparation',
                started_at=item.get('started_at') or _timestamp())
            self.persist_status(config)
            if self.review is not None: self.review.launcher_read_only=True
            self.thread=threading.Thread(target=self.execute,args=(config,source,manifest,correction,selected),daemon=True)
            self.thread.start()
        return self.snapshot()
    def switch_mode(self, mode, *, ultra_fast=False, list_policy='mixed'):
        require_mode_available(mode,ultra_fast=ultra_fast)
        if list_policy not in {'mixed','new'}: raise ContractError('Unbekannte Ergebnislisten-Auswahl')
        with self.lock:
            if self.repair.busy() or self.benchmark.busy() or (self.review and self.review.state['job']['status']=='running'):
                raise RuntimeError('Launcher-/Review-Renderauftrag läuft bereits')
            if self._switch_thread and self._switch_thread.is_alive(): raise RuntimeError('Moduswechsel läuft bereits')
            active=bool(self.thread and self.thread.is_alive())
            if not active:
                # Completed, failed and idle jobs can change list policy immediately.
                return self.start(mode,ultra_fast=ultra_fast,list_policy=list_policy)
            if self.status.snapshot()['status'] not in {'running','pausing','paused'}:
                raise RuntimeError('Kein wechselbarer Renderauftrag aktiv')
            self._switch_cancel.clear();self.pause_gate.switching=True
            self._switch_thread=threading.Thread(target=self._switch_worker,args=(mode,ultra_fast,list_policy),daemon=True)
            self._switch_thread.start()
        return self.snapshot()
    def _switch_worker(self, mode, ultra_fast, list_policy):
        try:
            gate=self.pause_gate
            with gate.condition:
                gate.requested=True;gate._status('pausing')
                while gate.active and not self._switch_cancel.is_set(): gate.condition.wait(.1)
                if self._switch_cancel.is_set(): return
                gate.stopped=True;gate.condition.notify_all()
            if self.thread: self.thread.join()
            if self._switch_cancel.is_set(): return
            for owner,sessions in self._production_owners:
                try: sessions.close()
                except Exception: pass
                try: owner.close()
                except Exception: pass
            self._production_owners=[]
            if self._switch_cancel.is_set(): return
            if list_policy=='mixed': self._drain_pending_qa()
            if self._switch_cancel.is_set(): return
            self.start(mode,ultra_fast=ultra_fast,list_policy=list_policy,list_id=self._list_id)
        except BaseException as exc:
            self.status.value.update(status='failed',error=str(exc),restart_required=True)
            self.persist_status()
    def _drain_pending_qa(self):
        """Finish already synthesized old-list attempts before mixed-mode reuse."""
        manifest=getattr(self,'_active_manifest',None)
        if manifest is None:return
        from manifest_renderer import production as p
        from manifest_renderer.plan import build_run_plan
        config=self._render_config;installation=self._active_installation
        plan=build_run_plan(manifest);owner=VersionAudio(self.root,manifest,config['project_state'],installation)
        run=resolve_relative(self.root,manifest['output']['root'],must_exist=True)
        auditor=p.ManifestAuditor(self.root,manifest,run)
        executor=p.ManifestExecutor.__new__(p.ManifestExecutor)
        executor.root=self.root;executor.manifest=manifest;executor.auditor=auditor
        store=owner.store(self.root)
        for row in plan['dialogues']:
            for attempt in row.get('attempts',[]):
                pending=resolve_relative(self.root,manifest['output']['root']+'/pending/'+attempt['fingerprint'])
                if not pending.is_dir():continue
                found=owner.find(attempt)
                if found is None or found[0].get('phase')!='synthesis':continue
                # The store authenticates the pending WAV, performs QA only, and
                # atomically publishes the original run's report and signed receipt.
                store.run(attempt,executor.audit_pending)
    def execute(self, config, source, manifest, correction, selected):
        try:
            from manifest_renderer import production as p
            self.pause_gate.checkpoint()
            installation=self.installation_factory(self.root)
            from .reconstruction_runtime import reconstruct_local_manifest
            manifest=reconstruct_local_manifest(self.root,manifest,config,installation)
            if correction and correction.get('format')==RECIPE_FORMAT:
                correction=validate_recipe(correction,manifest)
                # Concrete dialogue choices take precedence over the declared pipeline.
                backends={row['backend'] for row in correction['dialogues'].values() if row['processing']=='render'}
                baseline={selected['backend']} if selected['operating_mode'] not in {'parallel_gpu_ane','adaptive_gpu_ane'} else {'gpu','ane'}
                if not backends <= baseline:
                    selected={**selected,**mode_selection('fast',ultra_fast=self._ultra_fast)}
                config={**config,'render_recipe':config.get('render_recipe') or {'pipeline':correction['pipeline']}}
                self._render_config=config
                self._render_selection=selected
            allowed={'gpu','ane'} if selected['operating_mode'] in {'parallel_gpu_ane', 'adaptive_gpu_ane'} else {selected['backend']}
            if correction and any(row.get('backend') not in allowed|{None} for row in correction.get('rows',{}).values()):
                raise ContractError('Korrektur-Backend passt nicht zum gewählten Modus; CPU/Intel wird nicht automatisch eingesetzt')
            validate_launcher_host(self.root,backend=selected['backend'],operating_mode=selected['operating_mode'])
            ane_config=None
            if selected['backend']=='ane' or selected['operating_mode'] in {'parallel_gpu_ane', 'adaptive_gpu_ane'}:
                from .local_ane import resolve_local_ane
                self.status.preparation(dict(phase='preparation_sources',preparation_detail='Lokale ANE-Installation und Modellbindung prüfen',log_path=None))
                ane_config=resolve_local_ane(self.root)
            if self.prepare is None:
                from .render_preparation import prepare_render_manifest
                approved_review=self.approved_review_projection(config,manifest,correction,installation)
                prepared=prepare_render_manifest(self.root,manifest,correction,config,installation,
                    approved_review_projection=approved_review,
                    progress=self.preparation_progress, ready=self.benchmark_prepared, preparation_step=self.pause_gate.step)
            else: prepared=self.prepare(self.root,manifest,correction,config,installation)
            if self.prepare is None and not self._auto_benchmark_checked:
                self.ensure_benchmark(prepared)
            self.pause_gate.checkpoint()
            local_source=self.root/'input/local-render'/(digest(prepared)+'.json')
            p.immutable_json(local_source,prepared)
            m=p.prepare_production(local_source,self.root,backend=selected['backend'],
                operating_mode=selected['operating_mode'],routing_policy=selected['routing_policy'],ane_worker_config=ane_config,full_integrity=True)
            # Production runtime_version historically contains STT/QA/code.
            # Launcher binds only effective synthesis bytes; historical paths retain their identity.
            m['runtime']['runtime_version']='launcher-synthesis:'+digest(component_identity(m,m['render_execution']))
            for requirement in m.get('requirements',[]):
                if requirement['component_id']==m['runtime']['runtime_id']:
                    requirement['version']=m['runtime']['runtime_version']
            m['source_evidence']['production']['launcher_quality']=selected['quality']
            m['source_evidence']['production']['launcher_execution_strategy']='first_standard_pass'
            if self._list_id==LEGACY_LIST: m['output']['root']='runs/'+digest(m)
            else: m['output']['root']='runs/render-lists/'+self._list_id+'/'+digest(m)
            self.status.value['run']=m['output']['root']
            from manifest_renderer.manifest import resolve_manifest
            resolve_manifest(m)
            from .benchmark import capture_source
            self._benchmark_source=capture_source(self.root,m,local_source)
            audio=VersionAudio(self.root,m,config['project_state'],installation)
            self._active_manifest=m;self._active_installation=installation
            if self._list_id!=LEGACY_LIST:
                self._save_list(dict(id=self._list_id,label=self._mode.replace('_',' ').title()+(' · Ultra Fast' if self._ultra_fast else ''),
                    mode=self._mode,ultra_fast=self._ultra_fast,category=_category(self._mode,m),version=config['project_state']['version'],status='running',total=len(m['dialogues']),run=m['output']['root']),active=False)
            report=(self.producer or p.run_all)(m,self.root,guard=lambda r:validate_launcher_host(r,backend=selected['backend'],operating_mode=selected['operating_mode']),
                store_factory=self.paused_store(audio.store),executor_factory=self.paused_executor(audio.executor(p.ManifestExecutor)),
                event_sink=self.production_event,progress_sink=self.production_progress,first_pass=True,
                defer_qa_until_tts_complete=defer_first_pass_qa() and selected['operating_mode'] not in {'parallel_gpu_ane','adaptive_gpu_ane'})
            self.pause_gate.checkpoint()
            if report.get('manual_review'):
                self.status.value.update(status='completed_with_errors',phase='manual_review',qa_backlog=0,
                    manual_review=copy.deepcopy(report['manual_review']),skipped_count=report['skipped_count'],
                    restart_required=report.get('restart_required',False),
                    error='Einzelne Dialoge wurden für die manuelle Review übersprungen; die Audiofassung ist unvollständig')
                self.persist_status(config)
                return
            if not report['all_ids_accounted']: raise ContractError('Renderfassung ist unvollständig')
            run=resolve_relative(self.root,m['output']['root'],must_exist=True)
            bindings=[]
            plan=json.loads((run/'run-plan.json').read_text())
            for row in plan['dialogues']:
                result=next(r for r in report['dialogues'] if r['dialog_id']==row['dialog_id'])
                if row['processing']=='render':
                    chosen=(result.get('selected') or {}).get('fingerprint')
                    candidate=next((a for a in result['attempts'] if a['fingerprint']==chosen),None) if chosen else next((a for a in reversed(result['attempts']) if a.get('wav_sha256')),None)
                    if candidate is None: raise ContractError('Audio fehlt für '+row['dialog_id'])
                    attempt=next(a for a in row['attempts'] if a['fingerprint']==candidate['fingerprint'])
                    # Parallel worker metadata is attached by production bindings.
                    binding=next(b for b in json.loads((run/'backend-bindings.json').read_text())['bindings'] if b['attempt_fingerprint']==attempt['fingerprint'])
                    attempt={**attempt,'render_execution':{**m['render_execution'], 'profile_id':binding['profile_id'], 'effective_parameters':binding['effective_parameters']}}
                    bindings.append(audio.binding(attempt,candidate))
            review_id=str(uuid.uuid5(uuid.UUID(config['project_state']['project_id']),m['output']['root']))
            state=validate_state(dict(schema_version=1,kind='review',project_id=config['project_state']['project_id'],
                review_id=review_id,run_id=audio.run_id,version=config['project_state']['version'],status='completed',bindings=bindings))
            review_root=self.root/'reviews'/review_id
            p.atomic_json(review_root/'launcher-version.json',state)
            activation=dict(source=local_source.relative_to(self.root).as_posix(),run=m['output']['root'],
                review=review_root.relative_to(self.root).as_posix(),version=state['version'],state_sha256=digest(state))
            activation['signature']=audio.sign(activation)
            from patch_review.service import ReviewService
            service=ReviewService(local_source,self.root,run,review_root)
            if not service.evidence.valid: raise ContractError('Reviewquellbindung ungültig')
            attach_review_renderer(service,self.root,config['project_state'],installation)
            service.availability_callback=self.review_availability
            service.launcher_read_only=False
            pointer=self.root/'.launcher/fassungen'/self._list_id/'active-review.json'
            p.atomic_json(pointer,activation)
            self._save_list(dict(id=self._list_id,activation=pointer.relative_to(self.root).as_posix()),active=False)
            with self.lock:
                if self._selected_list_id==self._list_id:
                    self._close_review();self.review=service
                    p.atomic_json(self.root/'.launcher/active-review.json',activation)
                    data=self._read_lists();data['published_list_id']=self._list_id
                    p.atomic_json(self._lists_path(),data)
                    active_path=self._activation_path()
                    self._activated_review_identity=self._review_service_identity(active_path,review_root/'launcher-version.json',
                        local_source,run,review_root/'review-state.json')
                    if self.selection()[0]['project_state']['version']!=state['version']: self._close_review()
                else:
                    close=getattr(getattr(service,'executor',None),'close',None)
                    if close: close()
            self.status.value.update(status='completed',phase='review',qa_backlog=0)
            self.persist_status(config)
            self._retire_replaced()
        except BaseException as exc:
            self.pause_gate.stop()
            if isinstance(exc,InterruptedError) and self.pause_gate.switching:
                self.status.value.update(status='paused',error=None,restart_required=False,workers={},qa_workers={})
                self.persist_status(config);return
            from manifest_renderer.pipeline import restart_needed
            self.status.value.update(status='failed',error=str(exc),workers={},qa_workers={},
                restart_required=restart_needed(exc))
            self.persist_status(config)

    def activate_review(self):
        """Only load the explicitly selected complete version. Never start jobs."""
        from patch_review.service import ReviewService
        from .contracts import resolve_relative
        if not self._review_activation_run_lock.acquire(blocking=False):
            raise RuntimeError('Reviewaktivierung läuft bereits')
        started=time.monotonic()
        self._activation_progress('validate_active','Aktive Review, Version und Quellbelege prüfen.',started=started)
        try:
            if self.review and self.review.state['job']['status']=='running':
                raise RuntimeError('Review-Renderauftrag läuft bereits')
            selected_id=self._selected_list_id
            active_path=self._activation_path(selected_id)
            if not active_path.is_file():
                raise ContractError('Noch keine vollständige Review vorhanden. '
                                    'Zuerst die Einrichtung und das Rendern der gewählten Fassung abschließen; '
                                    'das Öffnen der Review startet keine Audiojobs.')
            config,_,_,_=self.selection()
            if self.current_render_incomplete():
                raise ContractError('Aktuelle Audiofassung ist unvollständig; übersprungene Dialoge zuerst manuell prüfen')
            active=json.loads(active_path.read_text())
            item=self._catalog_item()
            expected_version=item.get('version') if item else config['project_state']['version']
            if active['version']!=expected_version: raise ContractError('Review gehört zu einer anderen gewählten Fassung; erst ausdrücklich rendern')
            project={**config['project_state'],'version':active['version']}
            review_root=resolve_relative(self.root,active['review'],must_exist=True)
            version_path=review_root/'launcher-version.json'
            state=validate_state(json.loads(version_path.read_text()))
            installation=self.installation_factory(self.root)
            body={k:v for k,v in active.items() if k!='signature'}
            signature=hmac.new(installation.receipt_key,canonical_json(body),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(active.get('signature',''),signature) or active['state_sha256']!=digest(state):
                raise ContractError('Lokale Versions-/Reviewaktivierung ist ungültig')
            source_path=resolve_relative(self.root,active['source'],must_exist=True)
            run_path=resolve_relative(self.root,active['run'],must_exist=True)
            state_path=review_root/'review-state.json'
            identity=self._review_service_identity(active_path,version_path,source_path,run_path,state_path)
            # Signed receipt checks remain on every explicit activation. Parsing
            # can be reused only while the exact file identity is unchanged.
            total=len(state['bindings'])
            self._activation_progress('verify_receipts','Signierte QA-, Synthese- und Audiobelege prüfen.',0,total,started=started)
            for index,binding in enumerate(state['bindings'],1):
                receipt_path=resolve_relative(self.root,binding['receipt_path'],must_exist=True)
                receipt_identity=self._activation_file_identity(receipt_path)
                cached=self._activation_receipt_cache.get(receipt_identity)
                if cached is None:
                    entry=json.loads(receipt_path.read_text())
                    body={k:v for k,v in entry.items() if k!='signature'}
                    canonical=canonical_json(body)
                    proof=(canonical,entry.get('signature',''),(entry.get('qa') or {}).get('key'),
                           entry['generation']['synthesis']['key'],entry['generation']['audio']['sha256'],
                           entry['generation']['audio']['path'])
                else:
                    proof=cached[1]
                    self._activation_receipt_cache.move_to_end(receipt_identity)
                canonical,receipt_signature,qa_key,synthesis_key,audio_hash,audio_path=proof
                # Verify the HMAC even on a hot hit. The cache only saves JSON
                # parsing and retains no mutable receipt object tree.
                signature=hmac.new(installation.receipt_key,canonical,hashlib.sha256).hexdigest()
                if not hmac.compare_digest(receipt_signature,signature) or qa_key!=binding['qa_key']:
                    raise ContractError('Review-QA-/Receiptsignatur abweichend')
                if synthesis_key!=binding['synthesis_key'] or audio_hash!=binding['audio_sha256']:
                    raise ContractError('Review-Synthese-/Audiobindung abweichend')
                audio=resolve_relative(self.root,audio_path,must_exist=True)
                if not audio.is_file(): raise ContractError('Reviewaudio fehlt')
                if receipt_identity!=self._activation_file_identity(receipt_path):
                    raise ContractError('Reviewreceipt wurde während der Aktivierung geändert')
                if cached is None:
                    from manifest_renderer.json_memory import recursive_size
                    size=recursive_size((receipt_identity,proof))+128
                    if size<=self._activation_receipt_cache_budget:
                        while (self._activation_receipt_cache and
                               self._activation_receipt_cache_bytes+size>self._activation_receipt_cache_budget):
                            _,evicted=self._activation_receipt_cache.popitem(last=False)
                            self._activation_receipt_cache_bytes-=evicted[0]
                        self._activation_receipt_cache[receipt_identity]=(size,proof)
                        self._activation_receipt_cache_bytes+=size
                if index%128==0 or index==total:
                    self._activation_progress('verify_receipts',f'{index} von {total} Reviewbelegen geprüft.',index,total,started=started)
                # Full WAV hashing is deferred to the Review evidence/audio endpoint.
            identity_after_receipts=self._review_service_identity(active_path,version_path,source_path,run_path,state_path)
            if identity_after_receipts!=identity:
                raise ContractError('Reviewquelle oder gespeicherter Reviewstand wurde während der Aktivierung geändert; Review erneut öffnen')
            if self._selected_list_id!=selected_id:
                raise ContractError('Fassung wurde während der Reviewaktivierung geändert; Review erneut öffnen')
            cached_identity=getattr(self,'_activated_review_identity',None)
            own_save=(self.review is not None and cached_identity is not None
                      and cached_identity[:-1]==identity_after_receipts[:-1]
                      and getattr(self.review,'_persisted_state_identity',None)==identity_after_receipts[-1])
            if self.review is not None and (cached_identity==identity_after_receipts or own_save):
                service=self.review
                self._activated_review_identity=identity_after_receipts
            else:
                def report_load_progress(phase,detail,completed=0,load_total=0):
                    self._activation_progress(phase,detail,completed,load_total,started=started)
                service=ReviewService(source_path,self.root,run_path,review_root,
                    load_progress_callback=report_load_progress)
                if not service.evidence.valid: raise ContractError('Reviewquelle ungültig')
                previous=getattr(self.review,'executor',None)
                if previous is not None and hasattr(previous,'close'):previous.close()
                self._activated_review_identity=self._review_service_identity(active_path,version_path,source_path,run_path,state_path)
            attach_review_renderer(service,self.root,project,installation)
            service.launcher_read_only=self.busy()
            service.availability_callback=self.review_availability
            with self.lock:
                if self._selected_list_id!=selected_id:
                    close=getattr(getattr(service,'executor',None),'close',None)
                    if close and service is not self.review: close()
                    raise ContractError('Fassung wurde während der Reviewaktivierung geändert; Review erneut öffnen')
                self.review=service
            actual_total=len(service.dialogues)
            self._activation_progress('ready','Review geladen.',actual_total,actual_total,status='completed',started=started)
            return service
        except BaseException as exc:
            self._activation_progress('failed',str(exc),error=str(exc),status='failed',started=started)
            raise
        finally:
            self._review_activation_run_lock.release()
