"""Explicit, bounded launcher continuation of the recorded warning chain.

The controller stores orchestration receipts only. ReviewService owns candidate
jobs, selection, audio evidence and Base/Medium QA. Reading status never starts
preparation, STT or synthesis. Each invocation advances at most one synthesis
chain per affected dialogue; another explicit invocation can try a fresh seed
from a remaining, exactly bound warning candidate without repeating a preset.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading
import time

from .contracts import ContractError, digest, resolve_relative

_MODEL_HASHES={}
_OPTIONAL_PREWARM_ERROR='Alternativanker-Prewarm-Audioformat oder -Länge ist ungültig'


def checked_asset(root,asset,*,model=False):
    from manifest_renderer.audio import sha256_file
    path=resolve_relative(root,asset['path'],must_exist=True)
    stat=path.stat();key=(str(path),stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
    actual=_MODEL_HASHES.get(key) if model else None
    if actual is None:actual=sha256_file(path)
    if actual!=asset['sha256']:raise ContractError('Gebundener Reparatur-Assethash abweichend: '+asset['path'])
    if model:
        if len(_MODEL_HASHES)>32:_MODEL_HASHES.clear()
        _MODEL_HASHES[key]=actual
    return path


def lexical(output):
    from patch_review.findings import word_findings
    stt=(output or {}).get('stt') or {}
    if not output or not output.get('verified') or stt.get('status')!='completed' or not isinstance(stt.get('transcript'),str):
        return None
    return word_findings(output.get('qa_expected_text',''),stt['transcript'],output.get('qa_allowed_variants',[]))[1]


def accepted(row):
    current=row.get('current') or {}
    return bool(row.get('good') or current.get('id') in row.get('accepted_candidate_ids',[])
                or current.get('id')=='original')


def clean_for_automatic_repair(service, output):
    """Require the service's strict deep-clean shape plus a completed lexical pass."""
    checker=getattr(service,'clean_automatic_repair_output',None)
    clean=checker(output) if callable(checker) else bool(
        output and output.get('verified') is True and output.get('warning') is False
        and output.get('review_status')=='pass' and output.get('qa_status')=='pass'
        and (output.get('stt') or {}).get('status')=='completed')
    return bool(clean and lexical(output)==[])


def adopt_clean_output(service, did, output, stage):
    """Select only the exact deeply checked, fully clean output through service policy."""
    if not clean_for_automatic_repair(service,output):return False
    with service.lock:
        synchronize_revision(service)
        result=service.mutate('release',dict(revision=service.state['revision'],dialog_id=did,
            candidate_id=output['id'],automatic_repair=True,repair_stage=stage))
    row=result.get('row') or service.row(did)
    current=row.get('current') or {}
    if current.get('binding')!=output.get('binding') or not current.get('automatically_repaired'):
        raise ContractError('Automatische Übernahme wurde nicht für die exakt geprüfte Ausgabe belegt')
    return True


def source_identity(row):
    current=row.get('current') or {}
    return digest({'binding':current.get('binding'),'tts_text':row.get('tts_text'),
                   'qa_expected_text':row.get('qa_expected_text'),'preset':current.get('preset')})


def write_current_medium(service, row):
    """Portable phase-0 form of the existing immutable Medium overlay writer."""
    from patch_review.evidence import Evidence
    from patch_review.stt_overlay import (write_candidate_medium_overlay, _immutable_json,
                                         _MODEL_HASH_CACHE, POLICY_ID)
    from manifest_renderer.audio import sha256_file
    from audio_qa_v1 import audit
    from audio_qa_v2 import rescore
    current=service.validate_output(row['dialog_id'],row['current'])
    stt=current.get('stt') or {}
    if any(s.get('model')=='medium' and s.get('status')=='completed' for s in stt.get('stages',[])):
        return
    fp=current['id'];attempt,record,root,mh,_=service.attempts[fp]
    candidate=next((c for c in service.saved(row['dialog_id'])['candidates'] if service.candidate_id(c)==fp),None)
    if candidate:
        c=service.hydrate_candidate(candidate)
        manifest=json.loads((service.root/c['manifest_path']).read_text())
        return write_candidate_medium_overlay(service.evidence.root,service.root,service.root/c['run_root'],manifest,attempt,record)
    root=Path(root);manifest=service.evidence.manifest;run=service.evidence.run
    if mh!=service.evidence.manifest_hash or digest(service.evidence.plan.get('dialogues',[]))==digest([]):
        raise ContractError('Medium-STT benötigt den gebundenen vollständigen Launcher-Run')
    files=manifest['models']['stt']['files']
    if len(files)<2:raise ContractError('Gebundenes Medium-Modell fehlt')
    model=resolve_relative(root,files[1]['path'],must_exist=True)
    stat=model.stat();key=(str(model),stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
    if _MODEL_HASH_CACHE.get(key)!=files[1]['sha256']:
        if sha256_file(model)!=files[1]['sha256']:raise ContractError('Medium-Modellhash abweichend')
        _MODEL_HASH_CACHE.clear();_MODEL_HASH_CACHE[key]=files[1]['sha256']
    cli_asset=manifest['source_evidence']['production']['stt_cli']
    cli=resolve_relative(root,cli_asset['path'],must_exist=True)
    if sha256_file(cli)!=cli_asset['sha256']:raise ContractError('Whisper-CLI-Hash abweichend')
    wav=resolve_relative(root,attempt['artifacts']['wav'],must_exist=True)
    if sha256_file(wav)!=record['wav_sha256']:raise ContractError('Medium-STT-Audiobindung abweichend')
    decoder=record.get('decoder');policy=record.get('qa_policy')
    if not isinstance(decoder,dict) or not isinstance(policy,dict):raise ContractError('QA-Decoder-/Policybeleg fehlt')
    source={k:v for k,v in record.items() if k!='resumed'}
    forms=[attempt['qa_expected_text'],*attempt.get('qa_allowed_variants',[])]
    binding=dict(format='patch-review-stt-overlay-binding/1',policy_id=POLICY_ID,
        source_run=str(run),manifest_sha256=mh,run_plan_sha256=service.evidence.plan['sha256'],
        review_request_sha256=None,attempt_fingerprint=fp,dialog_id=row['dialog_id'],
        source_record_sha256=digest(source),wav_path=attempt['artifacts']['wav'],
        wav_sha256=record['wav_sha256'],wav_size_bytes=wav.stat().st_size,
        qa_expected_text=attempt['qa_expected_text'],accepted_forms=forms,
        model_sha256=files[1]['sha256'],decoder=decoder,qa_policy=policy)
    out=Evidence.overlay_path(run,fp,root)
    resolve_relative(root,out.relative_to(root).as_posix())
    out.parent.mkdir(parents=True,exist_ok=True)
    if out.exists():
        prior=json.loads(out.read_text())
        if prior.get('binding')!=binding or prior.get('record_sha256')!=digest(prior.get('record')):
            raise ContractError('Vorhandener Medium-STT-Beleg weicht ab')
        return prior
    started=time.perf_counter();heard=audit.run_stt(cli,model,wav,out.parent,int(decoder.get('threads',4)))
    medium=rescore.rescore({**source,**audit.compare(forms,heard),
        'stt_status':'pass','transcript':heard,'stt_model':'medium','model_sha256':files[1]['sha256'],
        'stt_seconds':round(time.perf_counter()-started,6)})
    receipt=dict(format='patch-review-stt-overlay/1',binding=binding,
        source_qa_state={k:source.get(k) for k in ('qa_status','stt_status','qa_completion_status','qa_deferral_reason','manual_review_required')},
        record=medium,record_sha256=digest(medium))
    _immutable_json(out,receipt)
    return receipt


def alternative_components(service, role_id):
    """Use the recorded same-role eligibility and bounded Wizard selection."""
    from voice_anchor_wizard.anchor_selection import pick_candidates, automatic_composite
    selected=service.roles[role_id].get('main_anchor') or {}
    excluded={selected.get('anchor_dialog_id'),*selected.get('anchor_component_dialog_ids',[])}
    pool=[]
    for d in service.dialogues.values():
        v=next((v for v in d.get('text_variants',[]) if v.get('variant_id')==d.get('selected_variant_id')),None)
        original=d.get('original_text') or {};audio=d.get('original_audio') or {}
        sid=d['dialog_id'].rsplit(':',1)[-1]
        if (d.get('role_id')!=role_id or d['dialog_id'] in excluded or d.get('processing')!='render'
                or d.get('content_type')!='speech' or not sid.isdigit()
                or original.get('verification') not in ('exact','inferred_reviewed')
                or not original.get('text') or not v or not v.get('tts_text')
                or type(audio.get('duration_ms')) is not int):continue
        pool.append(dict(speech_id=sid,dialog_id=d['dialog_id'],duration_ms=audio['duration_ms'],
                         audio=copy.deepcopy(audio),english_text=original['text']))
    singles=[c for c in pool if 4000<=c['duration_ms']<=15000]
    if singles:return [min(singles,key=lambda c:(abs(c['duration_ms']-7000),int(c['speech_id'])))],0
    curated,_=pick_candidates(pool,limit=18);choice=automatic_composite(curated)
    if not choice or not 2<=len(choice['component_speech_ids'])<=8:
        raise ContractError('Kein zulässiger Alternativanker im kuratierten Same-role-Pool (höchstens 18 Quellen)')
    by={c['speech_id']:c for c in curated}
    return [by[sid] for sid in choice['component_speech_ids']],choice['join_silence_ms']


def prepare_alternative(service, row):
    """Prepare local original-demo alternates with P19's reference/prewarm core."""
    from manifest_renderer.audio import sha256_file, validate_wav
    from manifest_renderer.audio_cache import AudioPreparationCache
    from manifest_renderer import preparation_core as core
    from .local_preparation import materialize_role_audio
    role=service.roles[row['role_id']];root=service.evidence.root
    # Own voices have a dedicated adapter and never use game-original voices.
    choice=service.manifest.get('source_evidence',{}).get('launcher_voice_choices',{}).get('roles',{}).get(row['role_id'],{})
    if choice.get('source')=='custom':
        alternative=choice.get('alternative')
        if alternative is None:return None
        from .render_preparation import materialize_custom_role
        from .contracts import load_or_create_installation
        recipe=copy.deepcopy(next(r for r in service.manifest['source_evidence']['launcher_preparation']['roles'] if r['role_id']==row['role_id']))
        if not row['current']['preset'].get('prewarm'):recipe.pop('prewarm',None)
        ffmpeg=checked_asset(root,service.manifest['runtime']['artifacts']['ffmpeg'])
        cli=checked_asset(root,service.manifest['runtime']['artifacts']['combined_cli'])
        models=[checked_asset(root,a,model=True) for a in service.manifest['models']['tts']['files']]
        config=json.loads((root/'.launcher/project.json').read_text())
        assets=materialize_custom_role(root,recipe,alternative,installation=load_or_create_installation(root),
            project_id=config['project_state']['project_id'],ffmpeg=ffmpeg,cli=cli,models=models,
            cache=AudioPreparationCache(root/'cache/audio_preparation_v1',ffmpeg),
            runner=getattr(service,'run_repair_prewarm',None))
        if assets.get('prewarm'):
            assets['prewarm']['selection']=assets['selection']
        ref=dict(role_id=row['role_id'],anchor=assets['anchor'],prewarm=assets.get('prewarm'));ref['fingerprint']=digest(ref)
        return service.validate_anchor_ref(row['dialog_id'],ref,deep=True)
    components,join=alternative_components(service,row['role_id'])
    ffmpeg=checked_asset(root,service.manifest['runtime']['artifacts']['ffmpeg'])
    cli=checked_asset(root,service.manifest['runtime']['artifacts']['combined_cli'])
    model_assets=service.manifest['models']['tts']['files']
    models=[checked_asset(root,a,model=True) for a in model_assets]
    for c in components:
        path=resolve_relative(root,c['audio']['path'],must_exist=True)
        if sha256_file(path)!=c['audio']['sha256']:raise ContractError('Alternativanker-Quellhash abweichend')
    prewarm=role.get('prewarm') or {};needed=bool(row['current']['preset'].get('prewarm'))
    if needed and (not prewarm.get('text') or prewarm.get('source') not in ('dialog','neutral_standard','custom_text')):
        raise ContractError('Exakter Rollenprewarm für Alternativanker fehlt')
    identity=dict(role_id=row['role_id'],components=components,join_silence_ms=join,
        prewarm=prewarm if needed else None,binaries={'ffmpeg':sha256_file(ffmpeg),cli.name:sha256_file(cli)},
        models={p.name:a['sha256'] for p,a in zip(models,model_assets)})
    fp=digest(identity);folder=service.root/'launcher-repair/anchors'/fp
    verified=dict(component_audio_sha256=[c['audio']['sha256'] for c in components],
        component_prompt_text_sha256=[hashlib.sha256(c['english_text'].encode()).hexdigest() for c in components],
        binaries=identity['binaries'],models=identity['models'],cases=[dict(speech_id='prewarm',audio_path=None,
            audio_sha256=None,prompt_text_sha256=hashlib.sha256(b'').hexdigest(),
            target_text_sha256=hashlib.sha256(prewarm.get('text','').encode()).hexdigest())])
    plan=dict(anchor={'fingerprint':fp},components=components,join_silence_ms=join,main_anchor={})
    cache=AudioPreparationCache(root/'cache/audio_preparation_v1',ffmpeg)
    reference=folder/'reference.wav';reference_report=folder/'reference.json'
    if needed:
        materialize_role_audio(root=root,role_plan=plan,verified_inputs=verified,ffmpeg=ffmpeg,cli=cli,models=models,
            cache=cache,reference=reference,reference_report=reference_report,
            target_case=dict(speech_id='prewarm',german_text=prewarm['text']),output=folder/'prewarm.wav',
            report=folder/'prewarm.json',runner=getattr(service,'run_repair_prewarm',None),seed=next((r.get('prewarm',{}).get('seed',20260926)
                for r in service.manifest.get('source_evidence',{}).get('launcher_preparation',{}).get('roles',[])
                if r['role_id']==row['role_id']),20260926))
    else:core.materialize_role_reference(root,plan,verified,ffmpeg,reference,reference_report,cache)
    def asset(path):
        info=validate_wav(path)
        return dict(path=path.relative_to(root).as_posix(),sha256=info['sha256'],media_type='audio/wav',
                    duration_ms=info['duration_ms'],sample_rate_hz=48000,channels=1)
    prompt=' '.join(c['english_text'] for c in components)
    anchor=dict(kind='single' if len(components)==1 else 'composite',anchor_dialog_id=components[0]['dialog_id'],
        anchor_component_dialog_ids=[c['dialog_id'] for c in components[1:]],source_audio=[c['audio'] for c in components],
        prompt_text=prompt,prompt_text_sha256=hashlib.sha256(prompt.encode()).hexdigest(),audio=asset(reference),
        selection_evidence='local_wizard_direct_selection')
    paired=None
    if needed:
        paired=copy.deepcopy(prewarm);paired.update(audio=asset(folder/'prewarm.wav'),technical_check='pass',
            generated_with_anchor_dialog_id=anchor['anchor_dialog_id'],generated_with_anchor_audio_sha256=anchor['audio']['sha256'])
        paired['selection']=dict(status='selected',evidence_ref=(folder/'prewarm.json').relative_to(root).as_posix())
    ref=dict(role_id=row['role_id'],anchor=anchor,prewarm=paired);ref['fingerprint']=digest(ref)
    return service.validate_anchor_ref(row['dialog_id'],ref,deep=True)


def synchronize_revision(service):
    owner=getattr(service,'_repair_owner',None)
    if owner is not None:service.state['revision']=owner.state['revision']


def repair_job(service,did):
    """Recover the dialogue's own receipt even after another worker finished."""
    pending=getattr(service,'_repair_pending_request',None)
    job=service.state['job']
    recorded=service.state.get('repair_jobs',{}).get(did)
    return recorded if recorded and (pending is None or recorded.get('request_id')==pending) else job


def repair_session(owner,did):
    """Private job/thread, shared rows, and a single canonical persistence writer.

    ReviewService is deliberately single-job. A repair session borrows its policy
    methods while keeping the execution receipt local. It never copies back a
    state snapshot: row mutations already use the canonical rows and lock.
    """
    session=copy.copy(owner);session._repair_owner=owner;session.thread=None
    session.state=dict(owner.state)
    session.state['job']=copy.deepcopy(owner.state.get('repair_jobs',{}).get(did) or
        (owner.state['job'] if did in owner.state['job'].get('dialog_ids',[]) else
         dict(status='idle',errors={})))
    if session.state['job'].get('status')=='running':session.state['job']['status']='failed'
    for name in ('_json_cache','_snapshot_identity_cache','_overlay_signatures'):
        if hasattr(owner,name):setattr(session,name,copy.copy(getattr(owner,name)))
    def persist():
        with owner.lock:
            owner.state['revision']=max(owner.state['revision']+1,session.state['revision'])
            session.state['revision']=owner.state['revision']
            jobs=owner.state.setdefault('repair_jobs',{})
            jobs[did]=copy.deepcopy(session.state['job'])
            session.state['repair_jobs']=jobs
            active={key:value for key,value in jobs.items() if value.get('status')=='running'}
            aggregate=copy.deepcopy(session.state['job'])
            aggregate.update(repair_parallel=True,workers=copy.deepcopy(active),
                active_dialog_ids=list(active),dialog_ids=list(active) or [did])
            if active:aggregate['status']='running'
            owner.state['job']=aggregate
            owner.persist()
    session.persist=persist
    def projection_writer(name):
        def write(*args,**kwargs):
            # Both workers must reuse the latest canonical projection, never
            # their older shallow-copy cache from before a peer's acceptance.
            with owner.lock:
                result=getattr(owner,name)(*args,**kwargs)
                for cache in ('_refined_manifest','_selection_projection'):
                    if hasattr(owner,cache):setattr(session,cache,getattr(owner,cache))
                return result
        return write
    for name in ('write_refined_manifest','write_selection'):
        if hasattr(owner,name):setattr(session,name,projection_writer(name))
    return session


class LauncherRepair:
    def __init__(self, launcher, *, alternate=prepare_alternative, medium=write_current_medium):
        self.launcher=launcher;self.alternate=alternate;self.medium=medium
        self.thread=None;self.lock=threading.RLock();self._progress=None;self.local=threading.local()
        self.worker_threads=();self.preparation_lock=threading.Lock()
        self.render_gate=threading.Condition();self.active_backends=set()
        self.warning_by_dialogue={}
        self.value=dict(status='idle',phase=None,total=0,completed=0,remaining=0,warnings=0,errors={},
                        active_dialog_ids=[],result=None,repeat_available=False)

    def busy(self):return bool(self.thread and self.thread.is_alive())
    @property
    def progress(self):return getattr(self.local,'progress',self._progress)
    @progress.setter
    def progress(self,value):self._progress=value
    def status(self):
        with self.lock:
            selector=getattr(self.launcher,'selected_activation',None)
            if callable(selector): selected=selector()
            else:
                active=self.launcher.root/'.launcher/active-review.json'
                selected=json.loads(active.read_text()) if active.is_file() else None
            selected_review=(selected or {}).get('review')
            if not self.busy() and selected_review!=getattr(self,'_status_review',None):
                self._status_review=selected_review
                self._progress=None
                self.value=dict(status='idle',phase='ready',completed=0,total=0,warnings=0)
            if self.progress is None and self.value['status']=='idle' and selected_review:
                path=resolve_relative(self.launcher.root,selected_review+'/launcher-repair/progress.json')
                if path.is_file():
                    saved=json.loads(path.read_text());self.value.update(saved.get('summary',{}))
                    if self.value['status']=='running':self.value.update(status='interrupted',active_dialog_ids=[])

            value=copy.deepcopy(self.value)
            with self.render_gate:value['active_backends']=sorted(self.active_backends)
            value['busy']=self.busy();value['pass_count']=(self.progress or {}).get('passes',value.get('pass_count',0))
            if self.busy() and self.launcher.review:
                job=self.launcher.review.state['job'];value['job']=copy.deepcopy(job)
            return value

    def save(self, service):
        from patch_review.service import atomic_json
        with self.lock:
            local=getattr(self.local,'progress',None)
            if local is not None:self._progress['dialogues'].update(copy.deepcopy(local['dialogues']))
            self._progress['status']=self.value['status'];self._progress['summary']=copy.deepcopy(self.value)
            atomic_json(service.root/'launcher-repair/progress.json',self._progress)

    @staticmethod
    def parallel_allowed(service):
        allowed=getattr(service,'repair_parallel_allowed',None)
        return bool(callable(allowed) and allowed())

    @contextmanager
    def render_slot(self,service,backend):
        if getattr(service,'_repair_owner',None) is None:
            yield;return
        with self.render_gate:
            while (backend in self.active_backends or
                   (self.active_backends and not self.parallel_allowed(service))):
                self.render_gate.wait(.2)
            self.active_backends.add(backend)
        try:yield
        finally:
            with self.render_gate:
                self.active_backends.remove(backend);self.render_gate.notify_all()

    def refresh_counts(self,service,targets,*,changed=None):
        # Targets are already known warnings at start. Rehydrate only the
        # completed dialogue; a full scan here holds up both render workers.
        for did in targets:self.warning_by_dialogue.setdefault(did,True)
        for did in targets if changed is None else [changed]:
            state=self.progress['dialogues'].get(did,{})
            row=service.row(did);out=row.get('current') or {}
            if state.get('candidate_id'):
                out=next((c for c in row.get('candidates',[]) if c['id']==state['candidate_id']),out)
            self.warning_by_dialogue[did]=bool(out.get('warning',True) and not accepted(row))
        decisions=[]
        for did in targets:
            state=self.progress['dialogues'].get(did,{})
            if state.get('phase'):
                decisions.append(dict(dialog_id=did,phase=state.get('phase'),reason=state.get('reason'),
                                      skipped_anchor=state.get('skipped_anchor'),candidate_id=state.get('candidate_id')))
        self.value.update(warnings=sum(self.warning_by_dialogue[did] for did in targets),decisions=decisions[-100:])

    def load(self, service):
        path=service.root/'launcher-repair/progress.json'
        identity=service.evidence.manifest_hash
        if path.exists():
            if path.is_symlink():raise ContractError('Reparaturfortschritt darf kein Symlink sein')
            progress=json.loads(path.read_text())
            if progress.get('format')!='launcher-repair/1' or progress.get('manifest_sha256')!=identity:
                raise ContractError('Reparaturfortschritt gehört zu einer anderen Review')
            self.progress=progress
        else:self.progress=dict(format='launcher-repair/1',manifest_sha256=identity,passes=0,dialogues={})

    def start(self, *, confirmed=False):
        if confirmed is not True:raise ContractError('Automatischen Reparaturversuch ausdrücklich bestätigen')
        with self.launcher.lock,self.lock:
            if self.launcher.busy():raise RuntimeError('Launcher-/Review-/Reparaturauftrag läuft bereits')
            service=self.launcher.repair_review()
            if not service.evidence.valid:raise ContractError('Reparatur benötigt eine vollständige gebundene Review')
            builder=getattr(service,'_patch_builder',None)
            if builder is not None and builder.status().get('job',{}).get('status')=='running':
                raise RuntimeError('Die gepatchte Fassung wird gerade erstellt; kein paralleler Reparaturauftrag')
            self.load(service)
            # No worker survives an explicit new invocation. Keep its exact
            # request receipt available for resume, without a stale busy lease.
            with service.lock:
                for job in service.state.get('repair_jobs',{}).values():
                    if job.get('status')=='running':job.update(status='failed',active_dialog_id=None)
            rows=[service.row(did) for did in service.dialogues]
            targets=[r['dialog_id'] for r in rows if r.get('processing')=='render' and r.get('warning') and not accepted(r)]
            self.warning_by_dialogue=dict.fromkeys(targets,True)
            self.progress['passes']+=1
            self.value.update(status='running',phase='planning',total=len(targets),completed=0,remaining=len(targets),
                warnings=len(targets),errors={},active_dialog_ids=[],result=None,repeat_available=False,
                pass_count=self.progress['passes'])
            self.save(service)
            self.thread=threading.Thread(target=self.run,args=(service,targets),daemon=True);self.thread.start()
            return self.status()

    def run(self, service, targets):
        try:
            if len(targets)>1 and self.parallel_allowed(service):
                pending=iter(targets);queue_lock=threading.Lock()
                def worker():
                    while True:
                        with queue_lock:
                            did=next(pending,None)
                        if did is None:return
                        with service.lock:view=repair_session(service,did)
                        self.run_dialogue(view,did,targets,service)
                self.worker_threads=tuple(threading.Thread(target=worker,daemon=True) for _ in range(2))
                for worker_thread in self.worker_threads:worker_thread.start()
                for worker_thread in self.worker_threads:worker_thread.join()
                self.worker_threads=()
            else:
                for did in targets:self.run_dialogue(service,did,targets,service)
            states=list(self.progress['dialogues'].values())
            repeat=any(s.get('phase') in ('anchor','seed','backend','paused_manual') or
                       (s.get('phase')=='manual' and s.get('lexical_findings')) for s in states)
            self.value.update(status='completed_with_errors' if self.value['errors'] else 'completed',phase='review',
                active_dialog_ids=[],repeat_available=repeat,
                active_dialog=None,
                blocked=sum(s.get('phase')=='blocked' for s in states),
                stages=sum(len(s.get('steps',[])) for s in states),
                skipped_anchors={did:s['skipped_anchor'] for did,s in self.progress['dialogues'].items() if s.get('skipped_anchor')},
                result=('Keine offenen Reparaturfälle.' if not targets else
                        'Vollständig unauffällige Ausgaben wurden automatisch übernommen; offene Fehler und Warnungen bleiben zur Review.'))
        except Exception as exc:self.value.update(status='failed',errors={'repair':str(exc)},active_dialog_ids=[])
        finally:self.save(service)

    def run_dialogue(self,service,did,targets,owner):
        if getattr(service,'_repair_owner',None) is not None:
            with self.lock:
                saved=self._progress['dialogues'].get(did)
                self.local.progress={**self._progress,'dialogues':{did:copy.deepcopy(saved)} if saved else {}}
        with self.lock:
            self.value['active_dialog_ids'].append(did);self.value['phase']='checking'
        try:
            with service.lock:row=service.row(did)
            with self.lock:
                self.value['active_dialog']=dict(dialog_id=did,text=row.get('tts_text'),role_id=row.get('role_id'))
            previous=self.progress['dialogues'].get(did)
            if previous and previous['phase']=='manual' and previous.get('candidate_id') and previous.get('lexical_findings'):
                previous['phase']='seed';previous['retry']=previous.get('retry',0)+1
            for _ in range(4):
                self.advance(service,did)
                if self.progress['dialogues'].get(did,{}).get('phase') not in ('medium','anchor','seed','backend'):break
            else:raise ContractError('Endliche Reparaturkette hat ihre maximale Stufenzahl überschritten')
        except Exception as exc:
            with self.lock:self.value['errors'][did]=str(exc)
        with self.lock:
            self.value['active_dialog_ids'].remove(did)
            self.value['completed']+=1;self.value['remaining']=len(targets)-self.value['completed']
        self.save(owner)
        if hasattr(self.local,'progress'):del self.local.progress
        with owner.lock,self.lock:self.refresh_counts(owner,targets,changed=did);self.save(owner)

    def advance(self, service, did):
        row=service.row(did)
        if accepted(row) or not row.get('warning'):return
        source=service.validate_output(did,row['current']) if row.get('current') else None
        if not source:raise ContractError('Keine tief geprüfte aktuelle Fassung; manuelle Review erforderlich')
        s=self.progress['dialogues'].get(did)
        if s is None:
            saved=service.saved(did)
            settings=service.action_settings(saved)
            baseline=saved.get('action_baseline')
            if saved.get('marked') or (isinstance(baseline,dict) and settings!=baseline):
                # Do not silently replace a user's staged action. Bind the pause to
                # the saved baseline so cancelling those edits makes this chain resumable.
                paused_row={**row,'tts_text':source.get('tts_text'),
                            'qa_expected_text':source.get('qa_expected_text')}
                s=dict(source_identity=source_identity(paused_row),source_binding=source['binding'],phase='paused_manual',
                       resume_phase='medium',steps=[],
                       settings_sha256=digest(baseline if isinstance(baseline,dict) else settings),
                       reason='Offene manuelle Reviewänderungen; Reparatur übersprungen, bis die Änderungen verworfen oder geprüft sind.')
                self.progress['dialogues'][did]=s
                return
        elif s.get('phase')!='paused_manual':
            expected=(s.get('pending') or {}).get('settings_sha256',s.get('settings_sha256'))
            if not expected:
                s.update(phase='manual',reason='Gespeicherte Reparatur-Einstellungsbindung fehlt; Fortsetzung nur nach manueller Review.')
                return
            if expected and expected!=digest(service.action_settings(service.saved(did))):
                s.update(phase='paused_manual',resume_phase=s.get('phase'),
                        reason='Revieweinstellungen geändert; Reparatur pausiert, bis die manuellen Änderungen verworfen oder geprüft sind.')
                return
        elif s.get('phase')=='paused_manual':
            expected=s.get('settings_sha256')
            live=digest(service.action_settings(service.saved(did)))
            if not expected or expected!=live:return
            s['phase']=s.pop('resume_phase','medium');s.pop('reason',None)
        if source.get('tts_text')!=row['tts_text'] or source.get('qa_expected_text')!=row['qa_expected_text']:
            raise ContractError('Aktuelle Audio-/Textfassung stimmt nicht mit den Revieweingaben überein')
        key=source_identity(row)
        if s and s['source_identity']!=key:
            raise ContractError('Audio-/Textquelle nach Reparaturbeginn geändert; manuelle Review erforderlich')
        if s is None:
            settings=service.action_settings(saved)
            s=dict(source_identity=key,source_binding=source['binding'],phase='medium',steps=[],
                   settings_sha256=digest(settings))
            self.progress['dialogues'][did]=s
        if s['phase']=='blocked' and str(s.get('reason','')).startswith(_OPTIONAL_PREWARM_ERROR):
            s.update(phase='seed',anchor_ref=None,skipped_anchor=str(s.pop('reason')))
        elif (s['phase']=='error' and (s.get('pending') or {}).get('phase')=='backend'
              and "No module named 'manifest_renderer'" in str(s.get('reason',''))):
            # Explicit new repair run: retry only the failed backend launch,
            # preserving the last verified candidate and all earlier steps.
            s.update(phase='backend',retry=s.get('retry',0)+1,
                     settings_sha256=s['pending']['settings_sha256'])
            s.pop('pending',None);s.pop('reason',None)
        if s['phase'] in ('clean','manual','blocked','error'):return
        expected_settings=(s.get('pending') or {}).get('settings_sha256',s.get('settings_sha256'))
        if expected_settings and expected_settings!=digest(service.action_settings(service.saved(did))):
            # Keep the chain's recorded phase and hashes intact. If the user
            # discards their staged edits, a later explicit repair can resume it.
            s.update(phase='paused_manual',resume_phase=s.get('phase'),
                    reason='Revieweinstellungen geändert; Reparatur pausiert, bis die manuellen Änderungen verworfen oder geprüft sind.')
            return
        if s.get('pending'):
            pending=s['pending'];service._repair_pending_request=pending['request']['request_id'];job=repair_job(service,did)
            if job.get('request_id')!=pending['request']['request_id']:
                s.update(phase='error',reason='Unterbrochener Auftrag hat keinen eindeutig gebundenen Job; kein blinder Neustart')
                raise ContractError(s['reason'])
            pending['job_id']=job['id']
            while service.thread and service.thread.is_alive():service.thread.join(.2)
            return self.finish(service,did,s,pending['phase'])
        if s['phase']=='medium':
            if lexical(source) is None:s.update(phase='blocked',reason='STT unvollständig; keine Synthese');return
            if not lexical(source):s.update(phase='manual',reason='Nur Dauer-/Technikwarnungen; keine Wortabweichung');return
            self.value['phase']='medium_stt';self.medium(service,row)
            row=service.row(did);source=service.validate_output(did,row['current'])
            if lexical(source) is None:s.update(phase='blocked',reason='Medium-STT unvollständig');return
            if not lexical(source):
                if adopt_clean_output(service,did,source,'medium'):
                    s['settings_sha256']=digest(service.action_settings(service.saved(did)))
                    s.update(phase='clean',reason='Medium-STT bestätigt vollständig unauffällige Ausgabe; automatisch übernommen.')
                else:s.update(phase='manual',reason='Medium-STT unauffällig, aber QA-/Warnungsbelege erlauben keine automatische Übernahme.')
                return
            self.value['phase']='anchor_preparation'
            try:
                # Same-role alternates share immutable cache filenames.
                with self.preparation_lock:ref=self.alternate(service,row)
            except Exception as exc:
                if not str(exc).startswith(_OPTIONAL_PREWARM_ERROR):
                    s.update(phase='blocked',reason=str(exc));raise
                ref=None;s['skipped_anchor']=str(exc)+'; Alternativanker ausgelassen, frischer Seed mit bisheriger Stimme.'
            s['anchor_ref']=ref;s['phase']='anchor' if ref else 'seed'
            if ref is None and not s.get('skipped_anchor'):
                s['skipped_anchor']='Keine zweite eigene Stimmprobe; nur frischer Seed und ggf. ANE'
            self.save(service)
        phase=s['phase'];source_id=s.get('candidate_id')
        if source_id:
            candidate=next((c for c in service.saved(did)['candidates'] if service.candidate_id(c)==source_id),None)
            if not candidate:raise ContractError('Gespeicherter Reparaturkandidat fehlt')
            source=service.candidate_output(candidate,deep=True)
            if lexical(source) is None:raise ContractError('Reparaturkandidat hat keinen abgeschlossenen STT-Beleg')
            if not lexical(source):
                stage=(s.get('steps') or [{}])[-1].get('phase',phase)
                if adopt_clean_output(service,did,source,stage):
                    s['settings_sha256']=digest(service.action_settings(service.saved(did)))
                    s.update(phase='clean',reason='QA und STT vollständig unauffällig; Kandidat automatisch übernommen.')
                else:s.update(phase='manual',reason='Kandidat ohne Wortabweichung, aber QA-/Warnungsbelege erlauben keine automatische Übernahme.')
                return
        preset=source.get('preset') or {};backend=preset.get('backend')
        if backend not in ('gpu','ane') or type(preset.get('seed')) is not int:
            s.update(phase='blocked',reason='Kein belegter Apple-Silicon-Backend/Seed');return
        if phase=='backend' and backend=='ane':s.update(phase='manual',reason='ANE bereits verwendet; keine weitere Synthese');return
        backend='ane' if phase=='backend' else backend
        choices=dict(seed='keep',anchor='on' if s.get('anchor_ref') else 'keep',
                     prewarm='on' if preset.get('prewarm') else 'off')
        with self.render_slot(service,backend):
            return self.render(service,did,s,phase,key,source_id,backend,choices)

    def render(self,service,did,s,phase,key,source_id,backend,choices):
        with service.lock:
            synchronize_revision(service)
            service.mutate('update',dict(revision=service.state['revision'],dialog_id=did,
                anchor_ref=s.get('anchor_ref'),backend=backend,next_preset=choices))
            request=dict(revision=service.state['revision'],dialog_ids=[did],backend=backend,intent='audition',
                         request_id=digest(dict(source=key,phase=phase,from_candidate=source_id,retry=s.get('retry',0))))
            if phase=='seed':request['fresh_seed']=True
            if source_id:request['from_candidate']=source_id
            service._repair_pending_request=request['request_id']
            s['pending']=dict(phase=phase,request=request,
                settings_sha256=digest(service.action_settings(service.saved(did))));self.save(service)
            try:result=service.start(request)
            except Exception as exc:
                s.update(phase='error',reason=str(exc));self.save(service);raise
            s['pending']['job_id']=result['job']['id'];self.save(service)
        self.value['phase']=phase
        while service.thread and service.thread.is_alive():service.thread.join(.2)
        self.finish(service,did,s,phase)

    def finish(self,service,did,s,phase):
        job=repair_job(service,did)
        row=service.row(did);candidate=next((c for c in row['candidates'] if c.get('job_id')==s['pending']['job_id']),None)
        if candidate is None:
            s.update(phase='error',reason=job.get('errors') or 'Kein belegter Kandidat gespeichert')
            raise ContractError(str(s['reason']))
        s['steps'].append(dict(phase=phase,job_id=s['pending']['job_id'],candidate_id=candidate['id'],binding=candidate['binding']))
        raw=next(c for c in service.saved(did)['candidates'] if service.candidate_id(c)==candidate['id'])
        candidate=service.candidate_output(raw,deep=True)
        s['candidate_id']=candidate['id'];s.pop('pending',None)
        s['settings_sha256']=digest(service.action_settings(service.saved(did)))
        if job.get('errors') or lexical(candidate) is None:
            s.update(phase='error',reason=job.get('errors') or 'STT-Kandidat unvollständig')
            raise ContractError(str(s['reason']))
        s['lexical_findings']=lexical(candidate)
        if not lexical(candidate):
            if adopt_clean_output(service,did,candidate,phase):
                s['settings_sha256']=digest(service.action_settings(service.saved(did)))
                s.update(phase='clean',reason='QA und STT vollständig unauffällig; Kandidat automatisch übernommen.')
            else:s.update(phase='manual',reason='Kandidat ohne Wortabweichung, aber QA-/Warnungsbelege erlauben keine automatische Übernahme.')
        else:s['phase']={'anchor':'seed','seed':'backend','backend':'manual'}[phase]
