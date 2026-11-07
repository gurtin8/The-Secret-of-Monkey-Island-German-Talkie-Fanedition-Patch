"""Read-only exact run/attempt/WAV/STT evidence binding."""
import json
import sys
from pathlib import Path
from urllib.parse import urlencode
from manifest_renderer.manifest import canonical_json,sha256
from manifest_renderer.audio import resolve_manifest_path,sha256_file
from manifest_renderer.json_memory import MetadataPool,read_compact,recursive_size
from .findings import word_findings,stt_state,REASONS,integer_numbers_equivalent
from audio_qa_v1.date_equivalence import has_named_date_conversion


def _review_warning(valid,qa_status,stt_status,errors,findings,qa_reasons,converted_date):
    # A passing final QA result may coexist with word-alignment notes. Keep
    # those categories/findings visible, but do not turn them into a review
    # warning when the STT result and all provenance bindings are valid.
    lexical_notes_only=bool(valid and qa_status=='pass' and stt_status=='completed' and not errors and not qa_reasons)
    return bool(errors or (findings and not lexical_notes_only) or (qa_status!='pass' and not converted_date))


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def digest(value):return sha256(canonical_json(value))


class Evidence:
    def __init__(self,manifest_path,asset_root,run_root):
        self.source_path=Path(manifest_path).resolve(strict=True)
        self.root=Path(asset_root).resolve(strict=True);self.run=Path(run_root).resolve(strict=True)
        self.metadata_pool=MetadataPool()
        self.source=read_compact(manifest_path,pool=self.metadata_pool)
        self.source_sha256=digest(self.source)
        run_manifest=self.run/'manifest.json'
        self.manifest=read_compact(run_manifest,pool=self.metadata_pool) if run_manifest.exists() and run_manifest.resolve()!=self.source_path else self.source
        self.manifest_hash=self.source_sha256 if self.manifest is self.source else digest(self.manifest)
        self.plan=read_compact(self.run/'run-plan.json',pool=self.metadata_pool);self.report=read_compact(self.run/'render-report.json',pool=self.metadata_pool)
        # Keep the canonical hash of the parsed source manifest for state
        # binding checks in ReviewService; hashing this large object again
        # during row-service construction adds no new evidence.
        source_bound=(self.source_sha256==self.manifest_hash or self.manifest.get('source_evidence',{}).get('production',{}).get('source_manifest_sha256')==self.source_sha256)
        self.valid=(source_bound and self.plan.get('manifest_sha256')==self.manifest_hash and self.report.get('manifest_sha256')==self.manifest_hash and self.report.get('run_plan_sha256')==self.plan.get('sha256') and digest({k:v for k,v in self.plan.items() if k!='sha256'})==self.plan.get('sha256'))
        self.plans={r['dialog_id']:r for r in self.plan['dialogues']};self.reports={r['dialog_id']:r for r in self.report['dialogues']}
        self.paths={};self.hash_cache={};self._external_run_plan_cache={}
        self._external_run_plan_cache_sizes={};self._external_run_plan_cache_max_bytes=16*1024*1024
        self.bindings={b['attempt_fingerprint']:b for b in read_compact(self.run/'backend-bindings.json',pool=self.metadata_pool).get('bindings',[])} if (self.run/'backend-bindings.json').exists() else {}
        self._launcher_owner_cache=None;self._source_evidence_cache={}
        self._source_evidence_cache_sizes={};self._source_evidence_cache_max_bytes=32*1024*1024

    @staticmethod
    def _retain_graph(cache,sizes,key,value,graph,max_bytes,limit,*,keep_active_oversize=False,accounted_bytes=None):
        """Conservatively bound references to complete immutable evidence graphs."""
        size=recursive_size((key,graph)) if accounted_bytes is None else accounted_bytes
        cache.pop(key,None);sizes.pop(key,None)
        if size>max_bytes:
            if keep_active_oversize:
                # One active large source avoids reparsing and authenticating
                # the same manifest for every reused row. Never retain a second
                # full graph beside it; this explicit exception is not a hard
                # per-object RAM bound.
                cache.clear();sizes.clear();cache[key]=value;sizes[key]=size
            return
        while cache and (len(cache)>=limit or sum(sizes.values())+size>max_bytes):
            oldest=next(iter(cache));cache.pop(oldest);sizes.pop(oldest)
        cache[key]=value;sizes[key]=size

    @staticmethod
    def _stat_identity(path):
        path=Path(path)
        try:
            stat=path.lstat()
            return (stat.st_dev,stat.st_ino,stat.st_mode,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        except OSError:return None

    def _launcher_owner(self,root):
        """Reuse one parsed project/installation owner for this immutable run."""
        from generator_launcher.contracts import installation_config_path,load_existing_installation
        from generator_launcher.render import VersionAudio
        root=Path(root).resolve(strict=True);project_path=root/'.launcher/project.json'
        identity_path=installation_config_path()
        if identity_path.resolve().is_relative_to(root):raise ValueError('Installation identity must be outside asset root')
        for component in (identity_path,*identity_path.parents):
            if component.is_symlink():raise ValueError('Symlink local configuration forbidden')
        if project_path.is_symlink() or not project_path.is_file():raise ValueError('Launcher project configuration is invalid')
        signature=(self._stat_identity(project_path),tuple(self._stat_identity(p) for p in (identity_path,*identity_path.parents)))
        cached=self._launcher_owner_cache
        if cached and cached[0]==signature:return cached[1]
        config=read(project_path)
        installation=load_existing_installation(root)
        owner=VersionAudio(root,self.manifest,config['project_state'],installation)
        if signature!=(self._stat_identity(project_path),tuple(self._stat_identity(p) for p in (identity_path,*identity_path.parents))):
            raise ValueError('Launcher identity changed while loading reuse evidence')
        self._launcher_owner_cache=(signature,owner)
        return owner

    def _source_evidence(self,source_run,source_manifest,plan,source_attempt,entry,root):
        """Share the authenticated source-run binding index across reused rows."""
        import copy
        from manifest_renderer.backend import bind_attempt
        run=Path(source_run);binding_path=run/'backend-bindings.json'
        binding_stat=self._stat_identity(binding_path)
        key=(str(run),id(source_manifest),id(plan),binding_stat)
        cached=self._source_evidence_cache.get(key)
        if cached is not None:source_evidence=cached
        else:
            source_evidence=copy.copy(self)
            source_evidence.run=run;source_evidence.plan=plan;source_evidence.manifest=source_manifest
            # `_source_run_bundle` only returns this plan after checking its
            # manifest_sha256 against the canonical source manifest hash.
            source_evidence.manifest_hash=plan['manifest_sha256']
            source_evidence.bindings=({b['attempt_fingerprint']:b for b in read_compact(binding_path,pool=self.metadata_pool)['bindings']}
                if binding_path.is_file() else {})
            if binding_stat!=self._stat_identity(binding_path):
                raise ValueError('Source backend bindings changed while loading reuse evidence')
        added_bytes=0
        if not binding_path.is_file() and source_attempt['fingerprint'] not in source_evidence.bindings:
            fingerprint=source_attempt['fingerprint']
            binding=bind_attempt(root,source_manifest,source_attempt,
                selected=entry['record']['render_execution'],
                manifest_sha256=source_evidence.manifest_hash,persist=False)
            before=sys.getsizeof(source_evidence.bindings)
            source_evidence.bindings[fingerprint]=binding
            # Count just the new receipt on a hot graph. Walking the complete
            # source manifest/plan per dialogue makes startup quadratic.
            added_bytes=recursive_size((fingerprint,binding))+sys.getsizeof(source_evidence.bindings)-before
        # This cache also owns source-plan/manifest references, so the launcher
        # source-run cache's byte limit alone would not bound their lifetime.
        if cached is None or added_bytes:
            self._retain_graph(self._source_evidence_cache,self._source_evidence_cache_sizes,key,source_evidence,
                (source_manifest,plan,source_evidence.bindings),self._source_evidence_cache_max_bytes,16,
                keep_active_oversize=True,
                accounted_bytes=(self._source_evidence_cache_sizes[key]+added_bytes) if cached is not None else None)
        return source_evidence,source_evidence.manifest_hash

    def file_valid(self,path,wanted):
        try:
            p=Path(path)
            if p.is_symlink() or not p.is_file():return False
            stat=p.stat();key=(str(p),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            if key not in self.hash_cache:self.hash_cache[key]=sha256_file(p)
            return self.hash_cache[key]==wanted
        except OSError:return False

    def url(self,key,path,wanted):
        self.paths[(key,wanted)]=Path(path)
        return '/api/patch-review/audio?'+urlencode({'key':key,'sha256':wanted})

    def audio(self,key,wanted):
        p=self.paths.get((key,wanted))
        if p is None or not self.file_valid(p,wanted):raise FileNotFoundError('Audio-Beleg stimmt nicht überein')
        return p

    @staticmethod
    def overlay_path(receipt_run,attempt_fingerprint,root):
        return (Path(root)/'reviews'/'patch-review'/'stt-escalations'/Path(receipt_run).name/
                'patch-review-medium-overlay-v1'/(attempt_fingerprint+'.json'))

    def _medium_overlay(self,attempt,record,receipt_run,root,manifest_hash,*,deep=False):
        """Read a separate Patchreview Medium receipt without editing its source run."""
        # Overlay receipts live under the fixed USB asset root even when a
        # candidate output is being resolved with Patchreview's state root.
        path=self.overlay_path(receipt_run,attempt['fingerprint'],self.root)
        if not path.exists() and not path.is_symlink():return None,None
        try:
            if path.is_symlink() or not path.is_file():raise ValueError('Overlayreceipt ist keine reguläre Datei')
            receipt=read(path);binding=receipt.get('binding');medium=receipt.get('record')
            run=Path(receipt_run).resolve(strict=True)
            plan_path=run/'run-plan.json'
            candidate_job=False
            if not plan_path.exists():
                if run.is_relative_to(Path(root).resolve()/'jobs'):run_plan_hash=None
                else:raise ValueError('Runplan fehlt außerhalb eines Patchreview-Kandidatenjobs')
                candidate_job=True
            elif run==self.run:plan=self.plan;run_plan_hash=plan.get('sha256')
            else:
                stat=plan_path.stat();cache_key=str(plan_path);signature=(stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
                cached=self._external_run_plan_cache.get(cache_key)
                if cached and cached[0]==signature:plan=cached[1]
                else:
                    plan=read_compact(plan_path,pool=self.metadata_pool)
                    self._retain_graph(self._external_run_plan_cache,self._external_run_plan_cache_sizes,
                        cache_key,(signature,plan),plan,self._external_run_plan_cache_max_bytes,32,
                        keep_active_oversize=True)
                run_plan_hash=plan.get('sha256')
            files=attempt.get('models',{}).get('stt',{}).get('files',[])
            if len(files)<2:raise ValueError('Medium-Modell fehlt im Attempt')
            request_path=run/'review-requests'/(attempt['fingerprint']+'.json')
            request_sha256=None
            if request_path.exists():
                if request_path.is_symlink() or not request_path.is_file():raise ValueError('Review-Auftragsbeleg ist ungültig')
                request_receipt=read(request_path)
                request_sha256=digest(request_receipt)
                if (request_receipt.get('format')!='patch-review-request/1' or
                        request_receipt.get('fingerprint')!=attempt['fingerprint'] or
                        request_receipt.get('manifest_sha256')!=manifest_hash or
                        request_receipt.get('render_seed')!=attempt.get('seed')):
                    raise ValueError('Review-Auftragsbeleg passt nicht zum Attempt')
            elif run.is_relative_to(Path(root).resolve()/'jobs'):
                raise ValueError('Kandidaten-Review-Auftragsbeleg fehlt')
            wanted={
                'format':'patch-review-stt-overlay-binding/1',
                'policy_id':'patch-review-medium-overlay-v1',
                'source_run':str(run),
                'manifest_sha256':manifest_hash,
                'run_plan_sha256':run_plan_hash,
                'review_request_sha256':request_sha256,
                'attempt_fingerprint':attempt['fingerprint'],
                'dialog_id':attempt['dialog_id'],
                # Candidate AttemptStore results carry a transport-only
                # `resumed` flag absent from the immutable report. Keep the
                # source-run aggregate-report hash convention unchanged.
                'source_record_sha256':digest({k:v for k,v in record.items() if k!='resumed'} if candidate_job else record),
                'wav_path':attempt['artifacts']['wav'],
                'wav_sha256':record.get('wav_sha256'),
                'wav_size_bytes':resolve_manifest_path(root,attempt['artifacts']['wav']).stat().st_size,
                'qa_expected_text':attempt['qa_expected_text'],
                'accepted_forms':[attempt['qa_expected_text'],*attempt.get('qa_allowed_variants',[])],
                'model_sha256':files[1].get('sha256'),
                'decoder':record.get('decoder'),
                'qa_policy':record.get('qa_policy'),
            }
            if receipt.get('format')!='patch-review-stt-overlay/1' or binding!=wanted:
                raise ValueError('Overlayreceipt-Bindung passt nicht zum Run/Attempt/Text/WAV')
            if receipt.get('record_sha256')!=digest(medium):raise ValueError('Overlay-STT-Recordhash stimmt nicht')
            source_qa_state={key:record.get(key) for key in
                             ('qa_status','stt_status','qa_completion_status','qa_deferral_reason','manual_review_required')}
            if receipt.get('source_qa_state')!=source_qa_state:raise ValueError('Gespeicherter Quell-STT-/QA-Zustand weicht ab')
            if (not isinstance(medium,dict) or medium.get('stt_model')!='medium' or
                    medium.get('stt_status') not in ('pass','warn') or not isinstance(medium.get('transcript'),str) or
                    medium.get('wav_sha256')!=record.get('wav_sha256') or
                    medium.get('model_sha256')!=files[1].get('sha256') or
                    medium.get('qa_expected_text')!=attempt['qa_expected_text'] or
                    medium.get('accepted_forms')!=wanted['accepted_forms'] or
                    medium.get('decoder')!=record.get('decoder') or medium.get('qa_policy')!=record.get('qa_policy') or
                    medium.get('render_status')!=record.get('render_status') or
                    medium.get('content_type')!=record.get('content_type') or
                    medium.get('audio_metrics')!=record.get('audio_metrics') or
                    medium.get('technical_status')!=record.get('technical_status') or
                    medium.get('duration_status')!=record.get('duration_status') or
                    medium.get('output_duration_ms')!=record.get('output_duration_ms') or
                    medium.get('reference_duration_ms')!=record.get('reference_duration_ms')):
                raise ValueError('Overlaymedium weicht bei STT-, Technik- oder Dauerdaten ab')
            wav=resolve_manifest_path(root,attempt['artifacts']['wav'])
            if deep and not self.file_valid(wav,record.get('wav_sha256')):
                raise ValueError('Overlay-WAV-Hash stimmt auf dem Datenträger nicht')
            stage={k:v for k,v in medium.items() if k not in ('stt_stages','stt_cascade','medium_checked')}
            stage['_overlay']=True
            deferred_stage=None
            if record.get('qa_completion_status')=='deferred_manual_review':
                deferred_stage={'stt_model':'base','stt_status':'deferred','qa_status':record.get('qa_status'),
                                'qa_expected_text':record.get('qa_expected_text'),'wav_sha256':record.get('wav_sha256'),
                                'model_sha256':files[0].get('sha256'),'error':record.get('qa_deferral_reason')}
            return (medium,stage,deferred_stage),None
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return None,'Medium-STT-Overlay ungültig: '+str(exc)

    def output(self,attempt,record,*,root=None,manifest_hash=None,receipt_required=True,deep=False,run_root=None,source_valid=None):
        root=Path(root or self.root);valid=self.valid if source_valid is None else source_valid;errors=[]
        receipt_run=Path(run_root or self.run)
        fp=attempt['fingerprint'];mh=manifest_hash or self.manifest_hash
        if record.get('launcher_reuse'):
            try:
                owner=self._launcher_owner(root)
                source=owner.reused_source(attempt,record,deep=deep)
                entry,_,_,source_manifest,source_attempt,source_run,_=source
                valid &= (digest({k:v for k,v in attempt.items() if k not in ('fingerprint','artifacts')})==fp
                          and attempt.get('manifest_sha256')==mh
                          and read(resolve_manifest_path(root,attempt['artifacts']['report']))==
                          {k:v for k,v in record.items() if k!='resumed'})
                # A killed source process may never publish its aggregate report.
                # The authenticated original plan, QA report and synthesis receipt
                # still provide the exact per-attempt evidence required below.
                source_evidence,source_manifest_hash=self._source_evidence(source_run,source_manifest,
                    source[6],source_attempt,entry,root)
                original=source_evidence.output(source_attempt,entry['record'],root=root,
                    manifest_hash=source_manifest_hash,receipt_required=True,deep=deep,
                    run_root=source_run,source_valid=bool(valid))
                original['id']=fp
                original['binding']={**original['binding'],'manifest_sha256':mh,'fingerprint':fp}
                original['launcher_reuse']=record['launcher_reuse']
                return original
            except (OSError,ValueError,KeyError,TypeError,StopIteration):
                # Invalid local references use the normal fail-closed evidence path.
                valid=False
        valid &= (digest({k:v for k,v in attempt.items() if k not in ('fingerprint','artifacts')})==fp and attempt.get('manifest_sha256')==mh and record.get('fingerprint')==fp and record.get('dialog_id')==attempt['dialog_id'] and record.get('qa_expected_text')==attempt['qa_expected_text'])
        execution=record.get('execution',{}).get('backend');actual_backend={'metal':'gpu','coreml_cpu_and_ne':'ane'}.get(execution,execution)
        binding_receipt=(read(receipt_run/'bindings'/(fp+'.json')) if (receipt_run/'bindings'/(fp+'.json')).is_file() else self.bindings.get(fp)) if receipt_required else None
        if receipt_required:
            valid &= bool(binding_receipt and binding_receipt.get('binding_sha256')==digest({k:v for k,v in binding_receipt.items() if k!='binding_sha256'}) and binding_receipt.get('attempt_fingerprint')==fp and binding_receipt.get('manifest_sha256')==mh and binding_receipt.get('dialog_id')==attempt['dialog_id'] and binding_receipt.get('seed')==attempt['seed'] and binding_receipt.get('profile_id')==actual_backend and binding_receipt.get('method')==attempt['method'] and binding_receipt.get('models')==attempt.get('models') and binding_receipt.get('runtime')==attempt.get('runtime') and binding_receipt.get('effective_parameters')==attempt.get('render_execution',{}).get('effective_parameters'))
        valid &= record.get('render_execution')==attempt.get('render_execution')
        valid &= actual_backend in ('gpu','ane','cpu') and actual_backend==attempt.get('render_execution',{}).get('profile_id')
        stages=record.get('stt_stages',[])
        if record.get('stt_status') in ('pass','warn'):
            valid &= bool(stages)
            if stages:valid &= record.get('transcript')==stages[-1].get('transcript') and record.get('stt_model')==stages[-1].get('stt_model')
        for stage in stages:
            models=attempt.get('models',{}).get('stt',{}).get('files',[])
            model_index={'base':0,'medium':1}.get(stage.get('stt_model'))
            valid &= model_index is not None and len(models)>model_index and stage.get('model_sha256')==models[model_index]['sha256']
            valid &= stage.get('qa_expected_text')==attempt['qa_expected_text'] and stage.get('wav_sha256')==record.get('wav_sha256')
        wav=None
        try:
            wav=resolve_manifest_path(root,attempt['artifacts']['wav']);log=resolve_manifest_path(root,attempt['artifacts']['log']);rp=resolve_manifest_path(root,attempt['artifacts']['report'])
            valid &= wav.is_file() and not wav.is_symlink() and log.is_file() and rp.is_file()
            if deep:
                valid &= self.file_valid(wav,record.get('wav_sha256')) and self.file_valid(log,record.get('log_sha256'))
                disk=read(rp);valid &= disk=={k:v for k,v in record.items() if k!='resumed'}
            if receipt_required:
                for stage in record.get('stt_stages',[]):
                    receipt=read(receipt_run/'stt'/fp/(stage['stt_model']+'.json'));binding=receipt['binding']
                    valid &= (receipt.get('record_sha256')==digest(receipt['record']) and receipt['record']==stage and binding.get('attempt_fingerprint')==fp and binding.get('wav_sha256')==record.get('wav_sha256') and binding.get('qa_expected_text')==attempt['qa_expected_text'] and binding.get('model_sha256')==stage.get('model_sha256') and binding.get('decoder')==stage.get('decoder') and binding.get('qa_policy')==attempt.get('qa') and binding.get('accepted_forms')==[attempt['qa_expected_text'],*attempt.get('qa_allowed_variants',[])])
        except (OSError,ValueError,KeyError):valid=False
        # Validate the immutable run record and its original Base receipts first.
        # Only then apply an independently stored Medium result for display/rescore.
        source_record=record
        overlay,overlay_error=self._medium_overlay(attempt,source_record,receipt_run,root,mh,deep=deep)
        if overlay_error:
            valid=False;errors.append(overlay_error)
        elif overlay:
            medium_record,medium_stage,deferred_stage=overlay
            source_stages=list(source_record.get('stt_stages',[]))
            if any(s.get('stt_model')=='medium' for s in source_stages):
                valid=False;errors.append('Medium-STT-Overlay existiert trotz bereits belegter Medium-Stufe.')
            else:
                record={**source_record,**medium_record,
                        'stt_stages':source_stages+([deferred_stage] if deferred_stage else [])+[medium_stage],
                        'stt_cascade':source_record.get('stt_cascade'),
                        'medium_checked':True}
        if not valid:errors.append('Die Manifest-/Versuchs-/Audio-/STT-Belegbindung ist ungültig; dieser Befund kann nicht freigegeben werden.')
        stt=stt_state(record,valid,copy=attempt.get('content_type')=='nonverbal' and valid)
        categories,findings=word_findings(attempt['qa_expected_text'],stt['transcript'],attempt.get('qa_allowed_variants',[])) if stt['status']=='completed' else ([],[])
        if stt['status'] not in ('completed','not_required'):findings.append({'deferred':'STT wurde zurückgestellt.','failed':'STT ist fehlgeschlagen.','absent':'Ein erforderliches STT-Ergebnis fehlt.','invalid':'STT kann wegen ungültiger Belegbindung nicht ausgewertet werden.'}[stt['status']]+(' '+str(stt['cause']) if stt['cause'] else ''))
        qa_reasons=list(record.get('reason_codes',[]))
        lexical_only={'missing_content_words','number_mismatch','number_unverified','short_text_mismatch','low_expected_coverage','many_extra_words'}
        expected_forms=[attempt['qa_expected_text'],*attempt.get('qa_allowed_variants',[])]
        converted_date=bool(valid and record.get('content_type')=='speech' and stt['status']=='completed'
            and record.get('qa_status')=='warn' and qa_reasons and set(qa_reasons)<=lexical_only
            and record.get('technical_status')=='pass' and record.get('duration_status')=='pass'
            and any(has_named_date_conversion(form,stt['transcript']) for form in expected_forms))
        converted_number=bool(valid and record.get('content_type')=='speech' and stt['status']=='completed'
            and record.get('qa_status')=='warn' and qa_reasons and set(qa_reasons)<=lexical_only
            and record.get('technical_status')=='pass' and record.get('duration_status')=='pass'
            and any(integer_numbers_equivalent(form,stt['transcript']) for form in expected_forms))
        lexical_equivalent = converted_date or converted_number
        qa_findings=[]
        for reason in qa_reasons:
            if reason in REASONS and REASONS[reason] not in qa_findings:qa_findings.append(REASONS[reason])
            if reason in REASONS and not (lexical_equivalent and reason in lexical_only) and REASONS[reason] not in findings:findings.append(REASONS[reason])
        if record.get('qa_status') in ('warn','fail') and not qa_findings:qa_findings.append('Die automatische Audio-/Textprüfung meldet einen auffälligen Befund: '+', '.join(qa_reasons)+'.')
        if record.get('qa_status') in ('warn','fail') and not findings and not lexical_equivalent:findings.append('Die automatische Audio-/Textprüfung meldet einen auffälligen Befund: '+', '.join(qa_reasons)+'.')
        binding=dict(manifest_sha256=mh,dialog_id=attempt['dialog_id'],fingerprint=fp,wav_sha256=record.get('wav_sha256'),text_sha256=digest([attempt['target_text'],attempt['qa_expected_text']]))
        audio_url=self.url('attempt:'+fp,wav,record['wav_sha256']) if wav and wav.is_file() and not wav.is_symlink() else None
        review_warning=_review_warning(valid,record.get('qa_status'),stt['status'],errors,findings,qa_reasons,lexical_equivalent)
        review_status='fail' if record.get('qa_status')=='fail' else 'warn' if review_warning else 'pass' if lexical_equivalent or record.get('qa_status')=='pass' else record.get('qa_status')
        return dict(id=fp,binding=binding,audio_url=audio_url,tts_text=attempt['target_text'],qa_expected_text=attempt['qa_expected_text'],qa_allowed_variants=attempt.get('qa_allowed_variants',[]),preset=dict(seed=attempt['seed'],anchor=bool(attempt.get('fixed_anchor')),prewarm=bool(attempt.get('prewarm')),backend=actual_backend,method=attempt['method']) if valid else None,qa_status=record.get('qa_status'),qa_reason_codes=qa_reasons,qa_evidence_findings=qa_findings,review_status=review_status,review_reason='named_date_equivalence' if converted_date else 'integer_number_equivalence' if converted_number else None,stt=stt,categories=categories,findings=errors+findings,warning=review_warning,verified=bool(valid))
