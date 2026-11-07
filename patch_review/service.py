"""Atomic review revisions and explicit one-attempt jobs; no model calls on edits."""
import copy,hashlib,json,os,re,secrets,shutil,tempfile,threading,uuid
from collections import OrderedDict
from pathlib import Path
from manifest_renderer.manifest import canonical_json
from manifest_renderer.plan import _attempt
from manifest_renderer.backend import profile,bind_attempt
from manifest_renderer.runner import AttemptStore
from voice_anchor_wizard.text_rules import prepare_text
from .evidence import Evidence,read,digest
from .findings import CATEGORIES

CPU_WARNING='CPU ist experimentell und als Renderbackend ungetestet. Nur ausdrücklich auswählen.'
RULES=['seed','anchor','prewarm','backend']


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.review-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


class ReviewService:
    def __init__(self,manifest_path,asset_root,run_root,state_root,mode='patch',executor=None,text_callback=None,sync_callback=None,prepare_callback=None,selection_callback=None,processing_callback=None,availability_callback=None,game_root=None,ffmpeg=None,load_progress_callback=None):
        if mode not in ('patch','wizard'):raise ValueError('Unbekannter Reviewmodus')
        self.load_progress_callback=load_progress_callback
        self._load_progress('source_loading','Reviewquelle und Belege einlesen.')
        self.evidence=Evidence(manifest_path,asset_root,run_root);self.manifest=self.evidence.manifest
        self.mode=mode;self.executor=executor;self.text_callback=text_callback;self.sync_callback=sync_callback;self.prepare_callback=prepare_callback
        self.selection_callback=selection_callback
        self.processing_callback=processing_callback
        self.availability_callback=availability_callback
        self.game_root=game_root;self.ffmpeg=ffmpeg;self._patch_builder=None
        self.root=Path(state_root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        if self.root==self.evidence.run or self.root.is_relative_to(self.evidence.run):raise ValueError('Reviewzustand darf vorhandenen Run nicht verändern')
        self.path=self.root/'review-state.json';self.lock=threading.RLock();self.thread=None
        self.dialogues={r['dialog_id']:r for r in self.manifest['dialogues']};self.roles={r['role_id']:r for r in self.manifest.get('roles',[])}
        self.state=read(self.path) if self.path.exists() else dict(format='patch-review-state/1',manifest_id=self.manifest['manifest_id'],source_manifest_sha256=self.evidence.source_sha256,revision=0,method_rules=self.manifest.get('project_policy',{}).get('review_method_rules',RULES),rows={},job=self.idle())
        self.state.setdefault('audition_queue',[]);self.state.setdefault('audition_queue_prepared',{})
        for value in self.state.get('rows',{}).values():
            value.setdefault('note_binding',None);value.setdefault('note_history',[])
        if self.state.get('manifest_id')!=self.manifest['manifest_id'] or self.state.get('source_manifest_sha256')!=self.evidence.source_sha256:raise ValueError('Reviewzustand gehört zu einem anderen Quellmanifest')
        job_before=copy.deepcopy(self.state['job'])
        self.state['job'].setdefault('intent',None);self.state['job'].setdefault('reused',[]);self.state['job'].setdefault('active_dialog_id',None)
        self.state['job'].setdefault('progress',dict(phase=None,available=False,detail='Kein Renderauftrag aktiv.'))
        if self.state['job']['status']=='running':self.state['job']['status']='failed';self.state['job']['active_dialog_id']=None;self.state['job']['errors']['interrupted']='Server wurde während des Auftrags beendet; kein automatischer Neustart.';self.persist()
        self.base={};self.attempts={};self._selection_projection=None
        self._json_cache=OrderedDict();self._json_cache_limit=256;self._json_cache_bytes=0;self._json_cache_max_bytes=64*1024*1024;self._json_cache_max_file_bytes=16*1024*1024
        # Candidate manifests can be larger than the general JSON cache limit.
        # Keep only their bound identity plus the selected dialogue/role needed
        # by startup projections, keyed by the full stat identity.
        self._snapshot_identity_cache=OrderedDict();self._snapshot_identity_limit=4096
        self._snapshot_identity_cache_bytes=0;self._snapshot_identity_cache_max_bytes=32*1024*1024;self._code_receipt_cache=None
        # Medium STT overlays can be written while the review server stays
        # open. Capture each signature immediately before its row snapshot:
        # capturing all signatures after all rows can miss receipts installed
        # during startup and leave those rows stale for the server lifetime.
        self._overlay_signatures={}
        row_total=len(self.dialogues)
        self._load_progress('load_rows','Reviewzeilen und QA-/STT-Belege laden.',0,row_total)
        for index,(did,d) in enumerate(self.dialogues.items(),1):
            self._overlay_signatures[did]=self._overlay_signature(did)
            self.base[did]=self.base_row(d)
            if index%64==0 or index==row_total:
                self._load_progress('load_rows',f'{index} von {row_total} Reviewzeilen geladen.',index,row_total)
        self._load_progress('prepare_state','Reviewzustand und Auswahl vorbereiten.')
        migrated_state=self.state['job']!=job_before
        queue_before=copy.deepcopy(self.state['audition_queue'])
        if not isinstance(self.state['audition_queue'],list):self.state['audition_queue']=[]
        self.state['audition_queue']=list(dict.fromkeys(did for did in self.state['audition_queue'] if isinstance(did,str) and did in self.dialogues and self.dialogues[did].get('content_type')=='speech'))
        if not isinstance(self.state['audition_queue_prepared'],dict):self.state['audition_queue_prepared']={}
        migrated_state=migrated_state or queue_before!=self.state['audition_queue']
        migration_fields=('next_preset','anchor_ref','action_baseline','generation_mode',
                          'processing_binding','accepted_probe','seed_defaults_version')
        for did,value in self.state.get('rows',{}).items():
            # Migration only writes these small settings fields. Review rows
            # can also contain large candidate/release histories; copying and
            # comparing the entire row here made a no-op restart proportional
            # to all historical evidence JSON, once per dialogue.
            missing=object()
            before={key:copy.deepcopy(value[key]) if key in value else missing for key in migration_fields}
            self.upgrade_settings(did,value)
            after={key:value[key] if key in value else missing for key in migration_fields}
            migrated_state=migrated_state or after!=before
        if migrated_state:self.persist()
        if self.path.exists():
            self._load_progress('prepare_manifest','Ausgewählte Texte und Rollen vorbereiten.')
            self.write_refined_manifest()
            self._load_progress('prepare_selection','Ausgewählte Audios und Freigaben prüfen.')
            self.write_selection()

    def _load_progress(self,phase,detail,completed=0,total=0):
        if self.load_progress_callback is not None:
            self.load_progress_callback(phase,detail,completed,total)

    @staticmethod
    def idle():return dict(status='idle',intent=None,dialog_ids=[],active_dialog_id=None,completed=[],reused=[],errors={},progress=dict(phase=None,available=False,model_loaded=None,detail='Kein Renderauftrag aktiv.'))
    def persist(self):
        atomic_json(self.path,self.state)
        st=self.path.stat()
        self._persisted_state_identity=(str(self.path.resolve()),st.st_dev,st.st_ino,
                                       st.st_size,st.st_mtime_ns,st.st_ctime_ns)
    def check(self,p):
        if type(p.get('revision')) is not int or p['revision']!=self.state['revision']:raise RuntimeError('Reviewstand wurde geändert; neu laden')
    def audition_queue_status(self):
        with self.lock:return dict(revision=self.state['revision'],audition_queue_enabled=bool(getattr(self,'audition_queue_enabled',False)),audition_queue=list(self.state['audition_queue']),recipe=self.recipe_info())
    def mutate_audition_queue(self,p):
        with self.lock:
            self.check(p)
            if not getattr(self,'audition_queue_enabled',False):raise ValueError('Die Vorschau-Warteliste ist nur auf bekannten Hosts mit höchstens 8 GiB RAM verfügbar')
            if self.state['job']['status']=='running' or (self._patch_builder is not None and self._patch_builder.status(validate_basis=False).get('job',{}).get('status')=='running'):
                raise RuntimeError('Die Warteliste ist während eines laufenden Auftrags gesperrt')
            if set(p)-{'revision','operation','dialog_id'}:raise ValueError('Unbekannte Wartelistenfelder')
            operation=p.get('operation');did=p.get('dialog_id')
            if operation not in ('add','remove','clear'):raise ValueError('Unbekannte Wartelistenaktion')
            queue=list(self.state['audition_queue'])
            if operation=='clear':queue=[]
            else:
                if did not in self.dialogues or self.dialogues[did].get('content_type')!='speech':raise ValueError('Nur bekannte Sprachdialoge können vorgemerkt werden')
                if operation=='add' and did not in queue:queue.append(did)
                if operation=='remove':queue=[value for value in queue if value!=did]
            if queue!=self.state['audition_queue']:
                self.state['audition_queue']=queue;self.state['revision']+=1;self.persist()
            return self.audition_queue_status()
    def default_saved(self,did):
        current=self.base[did].get('current') or {};preset=current.get('preset') or {}
        seed='keep' if current.get('verified') and type(preset.get('seed')) is int else 'fresh'
        s=dict(tts_text=self.base[did]['tts_text'],qa_expected_text=self.base[did]['qa_expected_text'],text_revision=1,note='',note_binding=None,note_history=[],marked=False,method='seed',next_preset=dict(seed=seed,anchor='keep',prewarm='keep'),anchor_ref=None,generation_mode='synthesize',backend=None,good_binding=None,selection=None,candidates=[],revisions=[],releases=[],processing_binding=None,accepted_probe=None,seed_defaults_version=2)
        s['action_baseline']=self.action_settings(s);return s
    def saved(self,did):return self.state['rows'].get(did,self.default_saved(did))
    @staticmethod
    def action_settings(s):return copy.deepcopy({k:s[k] for k in ('tts_text','qa_expected_text','next_preset','anchor_ref','generation_mode','backend')})
    def next_action(self,s):return ('copy_original' if s['generation_mode']=='copy_original' else 'render') if s['marked'] or self.action_settings(s)!=s['action_baseline'] else 'none'
    def legacy_preset(self,did,method,value=None):
        settings=dict(seed='fresh',anchor='keep',prewarm='keep')
        if method in ('anchor','prewarm'):
            current=self.base[did].get('current') or {}
            if value and value.get('selection'):
                selected=value['selection']
                if selected.get('fingerprint')=='original':
                    original=self.original_output(did,deep=True)
                    if original['verified'] and original['binding']==selected and any(r.get('binding')==selected and r.get('processing')=='copy_original' for r in value.get('releases',[])):current=original
                else:
                    for candidate in value.get('candidates',[]):
                        if self.candidate_id(candidate)!=selected.get('fingerprint'):continue
                        try:
                            out=self.candidate_output(candidate,deep=True)
                            if out['verified'] and out['binding']==selected:current=out
                        except (KeyError,ValueError,OSError):pass
            preset=current.get('preset') or {}
            if current.get('id')=='original':
                role=self.roles.get(self.dialogues[did].get('role_id'),{})
                preset=dict(anchor=bool(role.get('main_anchor')),prewarm=bool(role.get('prewarm')))
            settings[method]='off' if preset.get(method) else 'on'
        return settings
    def upgrade_settings(self,did,value):
        if 'next_preset' not in value:value['next_preset']=self.legacy_preset(did,value.get('method','seed'),value)
        value.setdefault('anchor_ref',None)
        if isinstance(value.get('action_baseline'),dict):value['action_baseline'].setdefault('anchor_ref',None)
        value.setdefault('generation_mode','synthesize')
        value.setdefault('processing_binding',value.get('selection') if value['generation_mode']=='copy_original' else None)
        if 'action_baseline' not in value:
            accepted=False
            current=self.base[did].get('current') or {}
            if value.get('selection'):
                for candidate in value.get('candidates',[]):
                    try:
                        out=self.candidate_output(candidate,deep=True)
                        if out['verified'] and out['binding']==value['selection']:current=out
                    except (KeyError,ValueError,OSError):pass
            preset=current.get('preset') or {}
            for candidate in value.get('candidates',[]):
                try:
                    out=self.candidate_output(candidate,deep=True)
                    actual=out.get('preset') or {};choices=value['next_preset']
                    matching=actual and all(actual[k]==(preset.get(k) if choices[k]=='keep' else choices[k]=='on') for k in ('anchor','prewarm')) and actual.get('backend')==(value['backend'] or preset.get('backend')) and (actual.get('seed')==preset.get('seed') if choices['seed']=='keep' else actual.get('seed')!=preset.get('seed'))
                    if out['verified'] and matching and out['tts_text']==value['tts_text'] and out['qa_expected_text']==value['qa_expected_text']:accepted=True
                except (KeyError,ValueError,OSError):pass
            if (value.get('selection') or {}).get('fingerprint')=='original':
                original=self.original_output(did,deep=True);accepted=original['verified'] and original['binding']==value['selection']
            value['action_baseline']=self.action_settings(value) if accepted else self.action_settings(self.default_saved(did))
        value.setdefault('accepted_probe',None)
        # Older releases could leave the just-accepted output on "fresh".
        # Migrate only when that was also the saved baseline; fresh against a
        # keep baseline is an explicit pending user choice and must survive.
        selected=value.get('selection') or {}
        accepted_selected=bool(value.get('good_binding') or (selected and any(r.get('binding')==selected for r in value.get('releases',[]))))
        if accepted_selected and value['next_preset']['seed']=='fresh' and value['action_baseline']['next_preset']['seed']=='fresh':
            value['next_preset']['seed']='keep';value['action_baseline']['next_preset']['seed']='keep'
        if value.get('seed_defaults_version')!=2:
            current=self.base[did].get('current') or {};preset=current.get('preset') or {}
            unmarked=not value['marked']
            staged=bool(value.get('candidates')) or did in self.state.get('job',{}).get('dialog_ids',[])
            # Older states defaulted to fresh even when the user had not chosen
            # a new seed. Migrate that seed dimension independently so pending
            # text/anchor/backend edits survive and remain queued.
            if current.get('verified') and type(preset.get('seed')) is int and value['next_preset']['seed']=='fresh' and value['action_baseline']['next_preset']['seed']=='fresh' and unmarked and not staged:
                value['next_preset']['seed']='keep';value['action_baseline']['next_preset']['seed']='keep'
            value['seed_defaults_version']=2
        return value
    @staticmethod
    def validate_settings(value):
        settings=value.get('next_preset')
        if not isinstance(settings,dict) or set(settings)!={'seed','anchor','prewarm'} or settings['seed'] not in ('fresh','keep') or any(settings[k] not in ('keep','on','off') for k in ('anchor','prewarm')):raise ValueError('Ungültige unabhängige Presetwahl')
        if value.get('generation_mode') not in ('synthesize','copy_original'):raise ValueError('Unbekannte Ausgabewahl')
        if 'action_baseline' in value:
            baseline=value['action_baseline']
            if not isinstance(baseline,dict) or set(baseline)!={'tts_text','qa_expected_text','next_preset','anchor_ref','generation_mode','backend'} or not isinstance(baseline['tts_text'],str) or not isinstance(baseline['qa_expected_text'],str) or baseline['backend'] not in (None,'gpu','ane','cpu'):raise ValueError('Ungültige Aktionsbasis')
            ReviewService.validate_settings(baseline)
    def original_output(self,did,deep=False):
        from manifest_renderer.audio import resolve_manifest_path
        d=self.dialogues[did];orig=d.get('original_audio',{});text=d.get('original_text',{}).get('text','')
        try:path=resolve_manifest_path(self.evidence.root,orig.get('path',''))
        except ValueError:path=None
        valid=bool(self.evidence.valid and path and path.is_file() and not path.is_symlink() and re.fullmatch(r'[0-9a-f]{64}',orig.get('sha256') or ''))
        if deep:valid=valid and self.evidence.file_valid(path,orig['sha256'])
        binding=dict(manifest_sha256=self.evidence.manifest_hash,dialog_id=did,fingerprint='original',wav_sha256=orig.get('sha256'),text_sha256=digest([text,'']))
        return dict(id='original',binding=binding,audio_url=self.evidence.url('original:'+did,path,orig.get('sha256')) if valid else None,tts_text=text,qa_expected_text='',preset=None,qa_status=None,stt=dict(status='not_required',transcript=None,model=None,stages=[],cause=None),categories=[],findings=['Englisches Original übernommen; deutsche QA und STT sind nicht erforderlich.'] if valid else ['Die englische Sprachausgabe fehlt oder ihr Pfad-/Hashbeleg ist ungültig.'],warning=not valid,verified=valid)
    def original_dialogue(self,did):
        d=copy.deepcopy(self.dialogues[did]);d['processing']='copy_original';text=d.get('original_text',{}).get('text','')
        # Keep historical variants referenced by role anchor receipts. The new
        # selected English text is descriptive metadata, never a German QA run.
        if text:
            old=next((v for v in d.get('text_variants',[]) if v['variant_id']==d.get('selected_variant_id')),None)
            v=copy.deepcopy(old) if old else dict(variant_revision=1,provenance=dict(kind='manual',source_ref='patch_review.original',source_version='1'),creation_version='patch-review:1',edit_version='1',edit_status='user_edited',review_status='unreviewed',normalization_steps=[])
            v.update(variant_id=did+':original:'+digest(text)[:16],display_text=text,tts_text=text,qa_expected_text=text,qa_allowed_variants=[])
            v['sha256']=digest([v[k] for k in ('display_text','tts_text','qa_expected_text','qa_allowed_variants')]);d.setdefault('text_variants',[]).append(v);d['selected_variant_id']=v['variant_id']
        return d
    def base_row(self,d):
        did=d['dialog_id'];report=self.evidence.reports.get(did,{})
        variant=next((v for v in d.get('text_variants',[]) if v['variant_id']==d.get('selected_variant_id')),{});history=[]
        plans={a['fingerprint']:a for a in self.evidence.plans.get(did,{}).get('attempts',[])}
        for record in report.get('attempts',[]):
            a=plans.get(record.get('fingerprint'))
            if a is not None:
                self.attempts[a['fingerprint']]=(a,record,self.evidence.root,self.evidence.manifest_hash,True)
                history.append(self.evidence.output(a,record))
        current=next((o for o in history if o['id']==(report.get('selected') or {}).get('fingerprint')),None)
        orig=d.get('original_audio',{});path=self.evidence.root/orig.get('path','')
        from manifest_renderer.audio import resolve_manifest_path
        try:path=resolve_manifest_path(self.evidence.root,orig.get('path',''))
        except ValueError:path=None
        original_url=self.evidence.url('original:'+did,path,orig.get('sha256')) if path and path.is_file() else None
        if d['processing']=='copy_original':current=self.original_output(did)
        return dict(dialog_id=did,speech_id=str(d.get('output_stem',did.rsplit(':',1)[-1])),role_id=d.get('role_id'),role_name=self.roles.get(d.get('role_id'),{}).get('name') or self.roles.get(d.get('role_id'),{}).get('display_name') or report.get('role_name',''),processing=d['processing'],original_text=d.get('original_text',{}).get('text',''),display_text=variant.get('display_text',''),tts_text=variant.get('tts_text',''),qa_expected_text=variant.get('qa_expected_text',''),text_revision=1,note='',marked=False,method='seed',backend=None,good=False,warning=current['warning'] if current else True,categories=current['categories'] if current else [],findings=current['findings'] if current else ['Keine belegte generierte Audioausgabe vorhanden.'],original_audio_url=original_url,current=current,history=history,candidates=[],recommendation=None)


    @staticmethod
    def candidate_id(c):return c.get('attempt',{}).get('fingerprint') or c.get('id')
    def hydrate_candidate(self,c):
        if c.get('reference_version')!=1:return c
        from manifest_renderer.audio import resolve_manifest_path
        attempt=self.read_cached(resolve_manifest_path(self.root,c['run_root']+'/attempts/'+c['id']+'.json'))
        record=self.read_cached(resolve_manifest_path(self.root,attempt['artifacts']['report']))
        observed=dict(manifest_sha256=c['manifest_sha256'],dialog_id=attempt['dialog_id'],fingerprint=attempt['fingerprint'],wav_sha256=record['wav_sha256'],text_sha256=digest([attempt['target_text'],attempt['qa_expected_text']]))
        if attempt['fingerprint']!=c['id'] or attempt['manifest_sha256']!=c['manifest_sha256'] or c.get('binding')!=observed or c.get('tts_text')!=attempt['target_text'] or c.get('qa_expected_text')!=attempt['qa_expected_text']:raise ValueError('Importierte Kandidatenreferenz stimmt nicht mit lokalen Belegen überein')
        return dict(attempt=attempt,record=record,manifest_sha256=c['manifest_sha256'],manifest_path=c['manifest_path'],run_root=c['run_root'],**({'review_request_sha256':c['review_request_sha256']} if c.get('review_request_sha256') else {}))
    def candidate_reference(self,c):
        if c.get('reference_version')==1:return copy.deepcopy(c)
        a,r=c['attempt'],c['record']
        binding=dict(manifest_sha256=c['manifest_sha256'],dialog_id=a['dialog_id'],fingerprint=a['fingerprint'],wav_sha256=r['wav_sha256'],text_sha256=digest([a['target_text'],a['qa_expected_text']]))
        return dict(reference_version=1,id=a['fingerprint'],binding=binding,manifest_sha256=c['manifest_sha256'],manifest_path=c['manifest_path'],run_root=c['run_root'],tts_text=a['target_text'],qa_expected_text=a['qa_expected_text'],**({'review_request_sha256':c['review_request_sha256']} if c.get('review_request_sha256') else {}))
    def candidate_output(self,c,deep=False):
        c=self.hydrate_candidate(c)
        a,r=c['attempt'],c['record'];mh=c['manifest_sha256']
        from manifest_renderer.audio import resolve_manifest_path
        snapshot_hash,snapshot_id,game_id=self.snapshot_identity(resolve_manifest_path(self.root,c['manifest_path']),a['dialog_id'])
        source_valid=self.evidence.valid and snapshot_hash==mh and snapshot_id==self.manifest['manifest_id'] and game_id==self.manifest.get('game',{}).get('game_id')
        run=resolve_manifest_path(self.root,c['run_root'])
        self.attempts[a['fingerprint']]=(a,r,self.root,mh,True)
        output=self.evidence.output(a,r,root=self.root,manifest_hash=mh,receipt_required=True,deep=deep,run_root=run,source_valid=source_valid)
        # Candidate transport carries its owning job so the review UI can bind
        # audition playback to this render even after a reload.
        relative=Path(c['run_root'])
        if len(relative.parts)>=2 and relative.parts[0]=='jobs':output['job_id']=relative.parts[1]
        return output
    def read_cached(self,path):
        """Read immutable candidate evidence with bounded stat-bound parsing cache."""
        path=Path(path);stat=path.stat();key=(str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,getattr(stat,'st_ino',None))
        cached=self._json_cache.get(key)
        if cached is not None:
            self._json_cache.move_to_end(key);return copy.deepcopy(cached[0])
        value=read(path)
        after=path.stat()
        after_key=(str(path),after.st_size,after.st_mtime_ns,after.st_ctime_ns,getattr(after,'st_ino',None))
        if key==after_key and stat.st_size<=self._json_cache_max_file_bytes:
            self._json_cache[key]=(value,stat.st_size);self._json_cache_bytes+=stat.st_size;self._json_cache.move_to_end(key)
        while len(self._json_cache)>self._json_cache_limit or self._json_cache_bytes>self._json_cache_max_bytes:
            _,(_,size)=self._json_cache.popitem(last=False);self._json_cache_bytes-=size
        return copy.deepcopy(value)
    def _snapshot_info(self,path,dialog_id=None):
        """Bind a full snapshot while retaining only a requested row projection."""
        path=Path(path);stat=path.stat();key=(str(path),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        cached=self._snapshot_identity_cache.get(key)
        if cached is not None and (dialog_id is None or dialog_id in cached['dialogues']):
            self._snapshot_identity_cache.move_to_end(key);return cached
        snapshot=read(path)
        value=dict(identity=(digest(snapshot),snapshot['manifest_id'],snapshot.get('game',{}).get('game_id')),dialogues={},bytes=0)
        if dialog_id is not None:
            dialogue=next((d for d in snapshot.get('dialogues',[]) if d.get('dialog_id')==dialog_id),None)
            role_id=dialogue.get('role_id') if dialogue else None
            role=next((r for r in snapshot.get('roles',[]) if r.get('role_id')==role_id),None)
            value['dialogues'][dialog_id]=(dialogue,role)
            value['bytes']=len(json.dumps([dialogue,role],ensure_ascii=False,separators=(',',':')).encode('utf-8'))
        after=path.stat()
        after_key=(str(path),after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
        if key==after_key:
            if cached is not None:
                value['dialogues']={**cached['dialogues'],**value['dialogues']};value['bytes']+=cached['bytes']
                self._snapshot_identity_cache_bytes-=cached['bytes']
            self._snapshot_identity_cache[key]=value;self._snapshot_identity_cache_bytes+=value['bytes'];self._snapshot_identity_cache.move_to_end(key)
            while (len(self._snapshot_identity_cache)>self._snapshot_identity_limit or
                   self._snapshot_identity_cache_bytes>self._snapshot_identity_cache_max_bytes):
                _,evicted=self._snapshot_identity_cache.popitem(last=False)
                self._snapshot_identity_cache_bytes-=evicted['bytes']
        return value
    def snapshot_identity(self,path,dialog_id=None):
        return self._snapshot_info(path,dialog_id)['identity']
    def snapshot_projection(self,path,dialog_id):
        """Return the selected dialogue and its role from a verified snapshot."""
        info=self._snapshot_info(path,dialog_id)
        dialogue,role=info['dialogues'].get(dialog_id,(None,None))
        if dialogue is None:raise KeyError(dialog_id)
        return copy.deepcopy(dialogue),copy.deepcopy(role)
    def _overlay_signature(self,did):
        """Return stat identities for only this dialogue's overlay paths."""
        from patch_review.evidence import Evidence
        report=self.evidence.reports.get(did,{})
        signature=[]
        for record in report.get('attempts',[]):
            fp=record.get('fingerprint')
            if not isinstance(fp,str):continue
            path=Evidence.overlay_path(self.evidence.run,fp,self.evidence.root)
            try:
                st=path.lstat()
                sig=(st.st_dev,st.st_ino,st.st_mode,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
            except FileNotFoundError:
                sig=None
            except OSError as exc:
                sig=('error',type(exc).__name__,getattr(exc,'errno',None))
            signature.append((fp,sig))
        return tuple(signature)

    def _refresh_overlay_base(self,did):
        latest=self._overlay_signature(did)
        if latest==getattr(self,'_overlay_signatures',{}).get(did):return
        self.base[did]=self.base_row(self.dialogues[did])
        self._overlay_signatures[did]=latest

    def row(self,did):
        if did not in self.base:raise ValueError('Unbekannter Dialog')
        self._refresh_overlay_base(did)
        row=copy.deepcopy(self.base_row(self.dialogues[did]) if self.base[did]['processing']=='copy_original' else self.base[did]);row['original_text']=self.base[did]['original_text'];s=self.state['rows'].get(did)
        row['previous_selection']=None
        row['automatically_repaired']=False
        if s:
            row.update({k:s[k] for k in ('tts_text','qa_expected_text','text_revision','note','marked','method','backend')})
            row['candidates']=[]
            for c in s['candidates']:
                try:row['candidates'].append(self.candidate_output(c))
                except (KeyError,ValueError,OSError):pass
            selected=next((o for o in row['candidates'] if o['verified'] and o['binding']==s.get('selection')),None)
            if selected:
                selected_record=next(c for c in s['candidates'] if self.candidate_id(c)==selected['id'])
                selected=self.candidate_output(selected_record,deep=True)
                if selected['verified']:row['current']=selected
            if (s.get('selection') or {}).get('fingerprint')=='original' and any(r.get('binding')==s['selection'] and r.get('processing')=='copy_original' for r in s['releases']):
                original=self.original_output(did,deep=True)
                if original['verified'] and original['binding']==s['selection']:row['current']=original
            elif s.get('selection') and not any(o.get('binding')==s['selection'] for o in row['candidates']):
                historical=next((o for o in row['history'] if o.get('binding')==s['selection']),None)
                if historical:
                    try:row['current']=self.validate_output(did,historical)
                    except (KeyError,ValueError,OSError):pass
            current=row['current']
            base_current=self.base[did].get('current')
            if (base_current and (not current or base_current.get('binding')!=current.get('binding'))
                    and not any(o.get('binding')==base_current.get('binding') for o in row['history'])):
                row['history'].append(copy.deepcopy(base_current))
            if current and s.get('good_binding')==current['binding']:
                try:current=self.validate_output(did,current);row['current']=current
                except (ValueError,KeyError,OSError):row['current']['verified']=False;row['current']['warning']=True;row['current']['findings']=[*row['current']['findings'],'Die frühere Gutbestätigung gehört nicht mehr zu einem gültigen Audio-/Textbeleg.']
            row['note_binding']=s.get('note_binding');row['note_history']=s.get('note_history',[])
            row['previous_selection']=self.previous_selection(did,s)
            if current:
                row['good']=bool(current['verified'] and self.is_approved(s,current['binding']))
                if row['good']:self.expose_approval(s,current)
                row['warning']=current['warning'];row['categories']=current['categories'];row['findings']=current['findings']
                current['good_confirmed']=row['good']
        imported=self.state.get('render_recipe',{}).get('dialogues',{}).get(did,{})
        row['recipe_seed']=imported.get('seed')
        current=row['current'];preset=current.get('preset') if current and current['verified'] else None
        settings=s or self.saved(did)
        accepted_binding=settings.get('selection') if settings.get('selection')==current.get('binding') else settings.get('good_binding')
        row['accepted_seed']=preset.get('seed') if preset and type(preset.get('seed')) is int and current.get('binding')==accepted_binding else None
        row['next_preset']=copy.deepcopy(settings['next_preset']);row['generation_mode']=settings['generation_mode']
        row['anchor_ref']=copy.deepcopy(settings.get('anchor_ref'))
        row['anchor_ref_fingerprint']=(settings.get('anchor_ref') or {}).get('fingerprint')
        row['next_action']=self.next_action(settings);row['marked']=row['next_action']!='none'
        original=self.original_output(did);row['original_binding']=original['binding'];row['original_release_enabled']=original['verified']
        if settings['generation_mode']=='copy_original':
            checked=self.original_output(did,deep=True)
            row['original_choice_valid']=bool(checked['verified'] and checked['binding']==settings.get('processing_binding'))
            if not row['original_choice_valid']:
                row.update(next_action='none',marked=False,warning=True,good=False)
                row['findings']=[*row['findings'],'Die gespeicherte Originaltonvorgabe passt nicht mehr zur englischen Sprachausgabe, englischem Text oder Quellmanifest. Originalton erneut auswählen und speichern.']
        row['processing']=('copy_original' if current['id']=='original' else 'render') if current else self.base[did]['processing']
        if s:
            released=[o for o in row['candidates'] if any(r.get('binding')==o['binding'] for r in s['releases'])]
            row['accepted_candidate_ids']=[o['id'] for o in released]
            if any(r.get('binding')==original['binding'] and r.get('processing')=='copy_original' for r in s['releases']) and original['verified']:released.append(original)
            known={o['id'] for o in row['history']}
            for out in released:
                if out['id'] not in known and (not current or out['id']!=current['id']):row['history'].append(copy.deepcopy(out));known.add(out['id'])
            for out in [*row['candidates'],*row['history']]:self.expose_approval(s,out)
            self.expose_automatic_repair(s,current,s['releases'])
            for out in [*row['candidates'],*row['history']]:self.expose_automatic_repair(s,out,s['releases'])
            row['automatically_repaired']=bool(current and current.get('automatically_repaired'))
        if preset:
            row['recommendation']=dict(seed='fresh',anchor=not preset['anchor'],prewarm=not preset['prewarm'],backend='ane' if preset['backend']=='gpu' else 'gpu')
        row['accepted_probe']=None
        if settings.get('accepted_probe'):
            probe=copy.deepcopy(settings['accepted_probe']);backend=settings['backend'] or probe.get('effective_backend')
            probe['seed']=row['accepted_seed']
            probe['reusable']=self.reusable_probe(did,backend,row=row) is not None
            row['accepted_probe']=probe
        if s and s.get('selection') and (not row.get('current') or not row['current'].get('verified') or row['current'].get('binding')!=s['selection']):
            row['warning']=True;row['good']=False
            row['findings']=[*row.get('findings',[]),'Die freigegebene Ausgabe fehlt oder ihre Audio-/Textbindung ist ungültig.']
        return row

    def render_request(self,did,backend,row,current_override=None,from_candidate=None):
        recipe=self.portable_recipe_for(did,row,backend)
        if recipe:
            entry=recipe['dialogues'][did];selected=profile(backend)
            if hasattr(self.executor,'backend_profile'):selected=self.executor.backend_profile(backend,self.manifest)
            return dict(dialog_id=did,tts_text=row['tts_text'],qa_expected_text=row['qa_expected_text'],
                seed_policy=row['next_preset']['seed'],seed_value=entry['seed'] if row['next_preset']['seed']=='keep' else None,
                anchor=entry['anchor'],prewarm=entry['prewarm'],backend=backend,backend_profile_sha256=digest(selected),
                method=entry.get('method','standard'),portable_recipe=True,recipe_inputs=recipe)
        current=current_override or row.get('current') or {};preset=current.get('preset') or {}
        if not preset and current.get('id')=='original':
            role=self.roles.get(self.dialogues[did].get('role_id'),{})
            preset=dict(anchor=bool(role.get('main_anchor')),prewarm=bool(role.get('prewarm')),seed=None,method='standard')
        choices=row['next_preset'];anchor=preset.get('anchor') if choices['anchor']=='keep' else choices['anchor']=='on';prewarm=preset.get('prewarm') if choices['prewarm']=='keep' else choices['prewarm']=='on'
        # Keep the UI's anchor and prewarm switches independent. An unused
        # alternate reference is neither validated nor put into this attempt.
        anchor_ref=self.validate_anchor_ref(did,row.get('anchor_ref'),deep=True) if anchor else None
        if anchor_ref and anchor and prewarm and anchor_ref.get('prewarm') is None:
            raise ValueError('Alternativanker braucht einen mit genau diesem Anker gebundenen Prewarm')
        selected=profile(backend)
        if hasattr(self.executor,'backend_profile'):selected=self.executor.backend_profile(backend,self.manifest)
        method='without_emotion' if preset.get('method')=='main_anchor_only' and prewarm else preset.get('method')
        return dict(source_manifest_sha256=self.evidence.manifest_hash,dialog_id=did,tts_text=row['tts_text'],qa_expected_text=row['qa_expected_text'],source_binding=current.get('binding'),from_candidate=from_candidate,seed_policy=choices['seed'],seed_value=preset.get('seed') if choices['seed']=='keep' else None,anchor=anchor,anchor_ref=copy.deepcopy(anchor_ref),anchor_ref_fingerprint=anchor_ref.get('fingerprint') if anchor and anchor_ref else None,prewarm=prewarm,prewarm_ref_fingerprint=anchor_ref.get('fingerprint') if anchor_ref and anchor_ref.get('prewarm') and prewarm else None,backend=backend,backend_profile_sha256=digest(selected),method=method)

    def request_receipt(self,c):
        from manifest_renderer.audio import resolve_manifest_path
        c=self.hydrate_candidate(c);a=c['attempt'];receipt=read(resolve_manifest_path(self.root,c['run_root']+'/review-requests/'+a['fingerprint']+'.json'))
        if not isinstance(receipt,dict) or not isinstance(receipt.get('request'),dict):raise ValueError('Vorhör-Auftragsbeleg ist ungültig')
        request=receipt['request']
        if receipt.get('format')!='patch-review-request/1' or digest(receipt)!=c.get('review_request_sha256') or receipt.get('fingerprint')!=a['fingerprint'] or receipt.get('manifest_sha256')!=a['manifest_sha256'] or receipt.get('source_manifest_sha256')!=self.evidence.manifest_hash or receipt.get('render_seed')!=a['seed']:raise ValueError('Vorhör-Auftragsbeleg ist ungültig')
        if request.get('dialog_id')!=a['dialog_id'] or request.get('tts_text')!=a['target_text'] or request.get('qa_expected_text')!=a['qa_expected_text'] or request.get('anchor')!=bool(a['fixed_anchor']) or request.get('prewarm')!=bool(a['prewarm']) or request.get('backend')!=a['render_execution']['profile_id'] or request.get('backend_profile_sha256')!=digest(a['render_execution']) or request.get('method')!=a['method'] or request.get('seed_policy') not in ('fresh','keep') or (request['seed_policy']=='keep' and request.get('seed_value')!=a['seed']):raise ValueError('Vorhör-Settings passen nicht zum tatsächlichen Attempt')
        # New receipts bind continuation inputs and concrete alternate-anchor
        # material. Older receipts remain readable through the checks above.
        if 'source_binding' in request:
            did=a['dialog_id'];source_binding=request['source_binding'];saved_candidates=self.saved(did)['candidates']
            target_index=next((i for i,x in enumerate(saved_candidates) if self.candidate_id(x)==a['fingerprint']),len(saved_candidates))
            earlier=saved_candidates[:target_index];from_candidate=request.get('from_candidate')
            if from_candidate is not None:
                source=next((x for x in earlier if self.candidate_id(x)==from_candidate),None)
                if source is None:raise ValueError('Quellkandidat fehlt, gehört nicht zu diesem Dialog oder ist nicht früher als sein Folgeversuch')
                sources=[self.candidate_output(source,deep=True)]
            else:
                sources=[]
                for source in earlier:
                    if self.candidate_id(source)==source_binding.get('fingerprint'):
                        sources.append(self.candidate_output(source,deep=True))
                for source in self.base[did].get('history',[]):
                    if source.get('binding')==source_binding:sources.append(self.validate_output(did,source))
                original=self.original_output(did,deep=True)
                if original.get('binding')==source_binding:sources.append(original)
            if not any(out.get('verified') and out.get('binding')==source_binding for out in sources):
                raise ValueError('Quellbindung des Renderauftrags verweist nicht auf eine tief geprüfte, frühere Ausgabe')
        if request.get('anchor_ref_fingerprint') is not None:
            anchor_ref=request.get('anchor_ref')
            anchor_ref=self.validate_anchor_ref(a['dialog_id'],anchor_ref,deep=True)
            if (not anchor_ref or anchor_ref['fingerprint']!=request.get('anchor_ref_fingerprint') or
                    (request.get('prewarm') and request.get('prewarm_ref_fingerprint')!=anchor_ref['fingerprint'])):
                raise ValueError('Alternativankerbindung des Renderauftrags fehlt oder ist verändert')
            snapshot=read(resolve_manifest_path(self.root,c['manifest_path']))
            rendered=next(x for x in snapshot['dialogues'] if x['dialog_id']==a['dialog_id'])
            overrides=rendered.get('render_overrides') or {}
            if digest(overrides.get('anchor_reference'))!=digest(anchor_ref['anchor']):
                raise ValueError('Rendermanifest enthält nicht den angeforderten Alternativanker')
            if request.get('prewarm') and digest(overrides.get('prewarm_reference'))!=digest(anchor_ref.get('prewarm')):
                raise ValueError('Rendermanifest enthält nicht den passend gebundenen Alternativanker-Prewarm')
        return receipt

    def reusable_probe(self,did,backend,row=None):
        s=self.saved(did);accepted=s.get('accepted_probe')
        if not accepted or s['generation_mode']!='synthesize':return None
        c=next((c for c in s['candidates'] if self.candidate_id(c)==accepted.get('candidate_id')),None)
        if not c:return None
        try:
            out=self.candidate_output(c,deep=True);receipt=self.request_receipt(c)
            if not out['verified'] or out['binding']!=accepted.get('binding') or out['binding']!=s.get('selection') or accepted.get('request_sha256')!=c.get('review_request_sha256') or not any(r.get('binding')==out['binding'] for r in s['releases']):return None
            if row is None:row=self.row(did)
            if not row.get('current') or row['current']['binding']!=out['binding']:return None
            request=self.render_request(did,backend,row)
            # With rendering disabled no new backend configuration is requested;
            # reuse the proven configuration of this already selected output.
            if self.executor is None and request['backend']==receipt['request']['backend']:request['backend_profile_sha256']=receipt['request']['backend_profile_sha256']
            expected=copy.deepcopy(receipt['request'])
            # Adoption changes the source output and freezes a formerly fresh
            # seed to keep. Those are provenance/policy changes; the concrete
            # render settings and seed must still match exactly.
            for key in ('source_binding','from_candidate'):
                request.pop(key,None);expected.pop(key,None)
            request['seed_policy']='keep';expected['seed_policy']='keep'
            expected['seed_value']=receipt['render_seed']
            return accepted if request==expected else None
        except (KeyError,ValueError,OSError,TypeError):return None
    def patch_builder(self):
        """Create the game-build controller lazily; this never starts a build."""
        with self.lock:
            if self._patch_builder is None:
                from .build import PatchBuildController
                self._patch_builder=PatchBuildController(self,game_root=self.game_root,ffmpeg=self.ffmpeg,defer_basis=True)
            return self._patch_builder

    def bootstrap(self):
        with self.lock:
            if self.sync_callback:self.sync_callback(self)
            patch_build=self.patch_builder().status(validate_basis=False)
            availability=self.render_availability()
            queue_enabled=bool(getattr(self,'audition_queue_enabled',False))
            return dict(read_only=self.launcher_readonly(),api_version=1,mode=self.mode,revision=self.state['revision'],manifest_id=self.manifest['manifest_id'],manifest_sha256=self.evidence.manifest_hash,render_enabled=self.executor is not None,render_disabled_reason=availability['reason'],render_availability=availability,method_rules=self.state['method_rules'],backends=['gpu','ane','cpu'],cpu_warning=CPU_WARNING,categories=[dict(id=i,label=l) for i,l in CATEGORIES],rows=self.rows(sync=False),job=copy.deepcopy(self.state['job']),patch_build=patch_build,audition_queue_enabled=queue_enabled,audition_queue=list(self.state['audition_queue']),recipe=self.recipe_info())
    def rows(self,dialog_ids=None,sync=True):
        with self.lock:
            ids=list(self.base) if dialog_ids is None else list(dict.fromkeys(dialog_ids))
            if any(did not in self.base for did in ids):raise ValueError('Unbekannter Dialog')
            if sync and self.sync_callback:
                if dialog_ids is None or not getattr(self.sync_callback,'supports_scoped_rows',False):self.sync_callback(self)
                else:self.sync_callback(self,ids)
            return [self.row(did) for did in ids]
    def rows_payload(self,dialog_ids=None):
        with self.lock:return dict(revision=self.state['revision'],rows=self.rows(dialog_ids))
    def launcher_readonly(self):
        if not getattr(self,'launcher_read_only',False): return False
        if self.availability_callback:
            value=self.availability_callback() or {}
            self.launcher_read_only=bool(value.get('busy'))
        return bool(getattr(self,'launcher_read_only',False))

    def render_availability(self):
        if self._patch_builder is not None and self._patch_builder.status(validate_basis=False).get("job",{}).get("status")=="running":
            return dict(available=False,reason="Die gepatchte Fassung wird gerade erstellt; kein Renderauftrag möglich.",busy=True,busy_source="patch_build")
        if self.executor is None:
            if self.mode=='wizard' and str(self.manifest.get('manifest_id','')).startswith('wizard:'):
                reason='Draft/Demo hat keinen vorhandenen Produktionsrun; Rendering ist gesperrt.'
            elif not self.evidence.valid:
                reason='Die Manifest-/Runbelege sind ungültig; produktives Rendering ist gesperrt.'
            else:
                reason='Der Produktionsrenderer wurde für dieses Review ausdrücklich deaktiviert.'
            return dict(available=False,reason=reason,busy=False,busy_source=None)
        if self.state['job']['status']=='running':return dict(available=False,reason='Ein Patchreview-Renderauftrag läuft bereits.',busy=True,busy_source='patch_review')
        if self.availability_callback:
            value=self.availability_callback()
            if value and value.get('busy'):
                source=value.get('busy_source') or 'wizard'
                return dict(available=False,reason=value.get('reason') or 'Der Wizardrenderer ist belegt; es wird nichts vorgemerkt oder automatisch gestartet.',busy=True,busy_source=source)
        return dict(available=True,reason=None,busy=False,busy_source=None)
    def verified_current(self,did):
        out=self.row(did)['current']
        if not out:raise ValueError('Keine Audioausgabe')
        return self.validate_output(did,out)

    def previous_selection(self,did,s=None):
        """Resolve the selection immediately before the current release event."""
        s=s or self.saved(did);selected=s.get('selection')
        if not selected:return None
        releases=s.get('releases',[])
        event_index=next((i for i in range(len(releases)-1,-1,-1)
                          if not releases[i].get('approval_event') and releases[i].get('binding')==selected),None)
        event=releases[event_index] if event_index is not None else None
        prior=event.get('previous_selection') if event else None
        if prior==selected:prior=None
        if prior is None and event:
            # Legacy release records did not persist the prior binding. Their
            # ordered accepted-release history is enough to infer the nearest
            # distinct prior selection, but never infer from generated output.
            for earlier in reversed(releases[:event_index]):
                if earlier.get('approval_event'):continue
                earlier_prior=earlier.get('previous_selection')
                if earlier_prior and earlier_prior!=selected:
                    prior=earlier_prior;break
                if earlier.get('binding') and earlier.get('binding')!=selected:
                    prior=earlier['binding'];break
        if not prior:return None
        try:
            if prior.get('fingerprint')=='original':out=self.original_output(did)
            else:
                candidate=next((c for c in s.get('candidates',[]) if self.candidate_id(c)==prior.get('fingerprint')),None)
                if candidate:out=self.candidate_output(candidate)
                else:
                    base=self.base[did]
                    historical=base.get('current') if (base.get('current') or {}).get('binding')==prior else next((o for o in base.get('history',[]) if o.get('binding')==prior),None)
                    if historical is None:return None
                    out=historical
            if not out['verified'] or out['binding']!=prior:return None
            return dict(candidate_id=out['id'],binding=out['binding'],seed=(out.get('preset') or {}).get('seed'),warning=bool(out.get('warning')))
        except (KeyError,ValueError,OSError):return None

    @staticmethod
    def is_approved(s,binding):
        """Return the latest human approval decision for this exact binding."""
        decision=None
        for record in s.get('releases',[]):
            if record.get('binding')!=binding:continue
            if record.get('approval_event'):
                decision=record.get('approved') is True
            elif record.get('ignore_warnings') is True:
                decision=True
        if decision is not None:return decision
        return s.get('good_binding')==binding

    @classmethod
    def preserve_legacy_approval(cls,s):
        binding=s.get('good_binding')
        if not binding or not cls.is_approved(s,binding):return
        if any(r.get('approval_event') and r.get('binding')==binding for r in s.get('releases',[])):return
        s['releases'].append(dict(binding=binding,approval_event=True,approved=True))

    @classmethod
    def expose_approval(cls,s,out):
        if out and out.get('verified') and cls.is_approved(s,out.get('binding')):
            out['raw_warning']=bool(out.get('warning'))
            out['approved']=True;out['warning']=False;out['good_confirmed']=True
        return out

    @staticmethod
    def clean_automatic_repair_output(out):
        equivalent_qa_warning=(out and out.get('qa_status')=='warn'
                               and out.get('review_reason') in ('integer_number_equivalence','named_date_equivalence'))
        return bool(out and out.get('verified') is True and out.get('warning') is False
                    and out.get('raw_warning',out.get('warning')) is False
                    and out.get('review_status')=='pass'
                    and (out.get('qa_status')=='pass' or equivalent_qa_warning)
                    and (out.get('stt') or {}).get('status')=='completed')

    @classmethod
    def expose_automatic_repair(cls,s,out,releases):
        if (cls.clean_automatic_repair_output(out) and any(
                r.get('automatic_repair') is True and r.get('binding')==out.get('binding')
                for r in releases or [])):
            out['automatically_repaired']=True
        return out

    def previous_output(self,did,s,expected_binding):
        target=self.previous_selection(did,s)
        if target is None:raise ValueError('Keine gültige vorherige Auswahl vorhanden')
        current=self.verified_current(did)
        if expected_binding!=current['binding']:raise RuntimeError('Aktuelle Audio-/Textbindung hat sich geändert')
        prior=target['binding']
        if prior.get('fingerprint')=='original':out=self.original_output(did,deep=True)
        else:
            candidate=next((c for c in s.get('candidates',[]) if self.candidate_id(c)==target['candidate_id']),None)
            if candidate:out=self.candidate_output(candidate,deep=True)
            else:
                base=self.base[did]
                historical=base.get('current') if (base.get('current') or {}).get('binding')==prior else next((o for o in base.get('history',[]) if o.get('binding')==prior),None)
                if historical is None:raise ValueError('Vorherige Audioausgabe ist nicht mehr verfügbar')
                out=self.validate_output(did,historical)
        if not out['verified'] or out['binding']!=prior:raise ValueError('Vorherige Audioausgabe ist ungültig')
        if out['warning'] and not self.is_approved(s,prior):
            raise ValueError('Warnende vorherige Version wurde nicht ausdrücklich bestätigt')
        return out

    def validate_anchor_ref(self,did,value,*,deep=True):
        """Validate a concrete, unselected same-role anchor and its paired prewarm."""
        from manifest_renderer.audio import resolve_manifest_path
        if value is None:return None
        if not isinstance(value,dict) or set(value)!={'fingerprint','role_id','anchor','prewarm'}:
            raise ValueError('Alternativanker braucht fingerprint, role_id, anchor und prewarm')
        role_id=self.dialogues[did].get('role_id')
        if value['role_id']!=role_id:raise ValueError('Alternativanker gehört nicht zur Dialogfigur')
        anchor=value['anchor']
        if not isinstance(anchor,dict):raise ValueError('Alternativankerbeleg fehlt')
        extras=anchor.get('anchor_component_dialog_ids')
        if (anchor.get('kind') not in ('single','composite') or not isinstance(extras,list)
                or len(extras)>7 or anchor.get('anchor_dialog_id') in extras
                or len(set(extras))!=len(extras)):
            raise ValueError('Alternativanker-Komponenten sind ungültig')
        components=[anchor.get('anchor_dialog_id'),*extras]
        custom=bool(isinstance(components[0],str) and components[0].startswith('custom-anchor:'))
        if (not components[0] or (anchor['kind']=='single' and len(components)!=1)
                or (anchor['kind']=='composite' and len(components)<2)):
            raise ValueError('Alternativanker-Komponenten sind ungültig')
        selected=self.roles.get(role_id,{}).get('main_anchor') or {}
        selected_components=[]
        if selected.get('anchor_dialog_id'):
            selected_components=[selected['anchor_dialog_id'],*selected.get('anchor_component_dialog_ids',[])]
        selected_audio=(selected.get('audio') or selected.get('materialized_audio') or {}).get('sha256')
        candidate_audio=(anchor.get('audio') or anchor.get('materialized_audio') or {}).get('sha256')
        if (set(components)==set(selected_components) or
                (selected_audio and candidate_audio==selected_audio) or
                (not custom and selected.get('prompt_text') and anchor.get('prompt_text')==selected.get('prompt_text'))):
            raise ValueError('Alternativanker ist identisch mit dem aktuell ausgewählten Rollenanker')
        source_audio=anchor.get('source_audio')
        if not isinstance(source_audio,list) or len(source_audio)!=len(components):
            raise ValueError('Alternativanker-Originalquellen fehlen')
        prompt=[];duration=0
        for index,component_id in enumerate(components):
            if custom:
                # Only the explicitly captured second recording of this own
                # voice is eligible; a custom-looking ID is not sufficient.
                from generator_launcher.voice_choices import verify_custom
                choices=self.manifest.get('source_evidence',{}).get('launcher_voice_choices',{}).get('roles',{})
                primary=choices.get(role_id) or {};choice=primary.get('alternative') or {}
                if (primary.get('source')!='custom' or anchor['kind']!='single' or extras or
                        component_id!='custom-anchor:'+str(choice.get('semantic_sha256',''))):
                    raise ValueError('Eigener Alternativanker ist nicht als zweite Aufnahme dieser Figur gebunden')
                audio=verify_custom(self.evidence.root,role_id,choice)
                if source_audio[index]!=audio or candidate_audio!=audio['sha256']:
                    raise ValueError('Eigener Alternativanker stimmt nicht mit der lokalen zweiten Aufnahme überein')
                prompt.append(choice['transcript']);duration+=audio['duration_ms'];continue
            source=self.dialogues.get(component_id)
            if not source or source.get('role_id')!=role_id or source.get('processing')!='render' or source.get('content_type')!='speech':
                raise ValueError('Alternativanker enthält eine fremde oder nicht aktive Figurzeile')
            original=source.get('original_text') or {};audio=source.get('original_audio') or {}
            variant=next((v for v in source.get('text_variants',[]) if v.get('variant_id')==source.get('selected_variant_id')),None)
            if original.get('verification') not in ('exact','inferred_reviewed') or not original.get('text') or not variant or not variant.get('tts_text'):
                raise ValueError('Alternativanker-Dialog erfüllt die Voice-Anchor-Kandidateneignung nicht')
            cited=source_audio[index]
            if cited.get('path')!=audio.get('path') or cited.get('sha256')!=audio.get('sha256'):
                raise ValueError('Englische Sprachausgabe für den Alternativanker stimmt nicht mit dem Quellmanifest überein')
            path=resolve_manifest_path(self.evidence.root,audio.get('path',''))
            if path.is_symlink() or not path.is_file() or (deep and not self.evidence.file_valid(path,audio.get('sha256'))):
                raise ValueError('Englische Sprachausgabe für den Alternativanker fehlt oder ihre Prüfsumme ist ungültig')
            prompt.append(original['text']);duration+=audio.get('duration_ms') or 0
        prompt_text=' '.join(prompt)
        if anchor.get('prompt_text')!=prompt_text or anchor.get('prompt_text_sha256')!=hashlib.sha256(prompt_text.encode('utf-8')).hexdigest():
            raise ValueError('Alternativanker-Sprechertext oder Textfingerprint stimmt nicht')
        audio=anchor.get('audio') or {}
        if not audio.get('path') or not re.fullmatch(r'[0-9a-f]{64}',audio.get('sha256') or ''):
            raise ValueError('Materialisierter Alternativanker braucht Pfad und SHA-256')
        audio_path=resolve_manifest_path(self.evidence.root,audio['path'])
        if audio_path.is_symlink() or not audio_path.is_file() or (deep and not self.evidence.file_valid(audio_path,audio['sha256'])):
            raise ValueError('Materialisierter Alternativanker fehlt oder sein Hash ist ungültig')
        minimum=3000 if custom else 4000
        if type(audio.get('duration_ms')) is not int or not minimum<=audio['duration_ms']<=15000:
            raise ValueError('Alternativanker liegt außerhalb seiner sicheren Dauergrenze')
        paired=value['prewarm']
        if paired is not None:
            if not isinstance(paired,dict) or paired.get('generated_with_anchor_audio_sha256')!=audio['sha256']:
                raise ValueError('Alternativanker-Prewarm wurde nicht mit genau diesem Anker erzeugt')
            if (paired.get('generated_with_anchor_dialog_id')!=anchor.get('anchor_dialog_id') or
                    paired.get('technical_check')!='pass' or
                    paired.get('source') not in ('dialog','neutral_standard','custom_text')):
                raise ValueError('Alternativanker-Prewarm stimmt nicht mit dem Anker oder seiner Quelle überein')
            if paired.get('source')=='dialog':
                prewarm_source=self.dialogues.get(paired.get('prewarm_dialog_id'))
                if not prewarm_source or prewarm_source.get('role_id')!=role_id or prewarm_source.get('content_type')!='speech':
                    raise ValueError('Alternativanker-Prewarm-Dialog gehört nicht zur gleichen Figur')
            elif paired.get('prewarm_dialog_id') is not None:
                raise ValueError('Neutraler Alternativanker-Prewarm darf keine Dialog-ID binden')
            prewarm_audio=paired.get('audio') or {};prewarm_text=paired.get('text')
            if (not isinstance(prewarm_text,str) or not prewarm_text.strip()
                    or paired.get('text_sha256')!=hashlib.sha256(prewarm_text.encode('utf-8')).hexdigest()
                    or not prewarm_audio.get('path') or not re.fullmatch(r'[0-9a-f]{64}',prewarm_audio.get('sha256') or '')):
                raise ValueError('Alternativanker-Prewarmbeleg ist ungültig')
            prewarm_path=resolve_manifest_path(self.evidence.root,prewarm_audio['path'])
            if prewarm_path.is_symlink() or not prewarm_path.is_file() or (deep and not self.evidence.file_valid(prewarm_path,prewarm_audio['sha256'])):
                raise ValueError('Alternativanker-Prewarm fehlt oder sein Hash ist ungültig')
            if (type(prewarm_audio.get('duration_ms')) is not int or not 3000<=prewarm_audio['duration_ms']<=4000 or
                    prewarm_audio.get('sample_rate_hz')!=self.manifest.get('runtime',{}).get('sample_rate_hz') or
                    prewarm_audio.get('channels')!=self.manifest.get('runtime',{}).get('channels')):
                raise ValueError('Alternativanker-Prewarm-Audioformat oder -Länge ist ungültig: '
                    f"{prewarm_audio.get('duration_ms')} ms, {prewarm_audio.get('sample_rate_hz')} Hz, "
                    f"{prewarm_audio.get('channels')} Kanal/Kanäle; erwartet 3000–4000 ms, "
                    f"{self.manifest.get('runtime',{}).get('sample_rate_hz')} Hz, "
                    f"{self.manifest.get('runtime',{}).get('channels')} Kanal/Kanäle")
        fingerprint=digest({'role_id':role_id,'anchor':anchor,'prewarm':paired})
        if value['fingerprint']!=fingerprint:raise ValueError('Alternativankerfingerprint stimmt nicht')
        return value
    def validate_output(self,did,out):
        if out['id']=='original':
            current=self.original_output(did,deep=True)
            if not current['verified'] or current['binding']!=out['binding']:raise ValueError('Englische Sprachausgabe verändert oder Originalbindung ungültig')
            out=current
        else:
            candidate=next((c for c in self.state['rows'].get(did,{}).get('candidates',[]) if self.candidate_id(c)==out['id']),None)
            if candidate:out=self.candidate_output(candidate,deep=True)
            else:
                a,r,root,mh,receipts=self.attempts[out['id']]
                out=self.evidence.output(a,r,root=root,manifest_hash=mh,receipt_required=receipts,deep=True)
        if not out['verified']:raise ValueError('Audio-/Textbeleg ist ungültig')
        return out
    def accepted_seed(self,did,s=None):
        s=s or self.saved(did);selected=s.get('selection') or s.get('good_binding') or {}
        if not selected or selected.get('fingerprint')=='original':return None
        attempt=self.attempts.get(selected['fingerprint'],(None,))[0]
        if attempt is not None and type(attempt.get('seed')) is int:return attempt['seed']
        candidate=next((c for c in s.get('candidates',[]) if self.candidate_id(c)==selected.get('fingerprint')),None)
        if candidate:
            try:
                attempt=self.hydrate_candidate(candidate)['attempt']
                if type(attempt.get('seed')) is int:return attempt['seed']
            except (KeyError,ValueError,OSError):pass
        return None
    @staticmethod
    def persist_accepted_seed(manifest,did,seed):
        if type(seed) is not int:return
        manifest.setdefault('source_evidence',{}).setdefault('launcher_render_inputs',{})[did]={'standard_seeds':[seed]}
    def mutate(self,operation,p):
        with self.lock:
            self.check(p);did=p.get('dialog_id')
            if did not in self.base:raise ValueError('Unbekannter Dialog')
            # Only notes and the legacy preset toggle need a pre-save output
            # snapshot. Other edits return the checked row once, after persist.
            row=self.row(did) if operation=='update' and ('note' in p or 'method' in p) else None
            s=copy.deepcopy(self.saved(did));before=copy.deepcopy(s)
            if operation=='update':
                if set(p)-{'revision','dialog_id','tts_text','note','method','backend','next_preset','generation_mode','anchor_ref'}:raise ValueError('Unbekannte Korrekturfelder')
                if 'tts_text' in p:
                    t=p['tts_text']
                    if not isinstance(t,str) or not t.strip() or len(t)>4000:raise ValueError('Sprachausgabetext muss 1–4000 Zeichen enthalten')
                    prepared=self.prepare_callback(did,t.strip()) if self.prepare_callback else prepare_text(t.strip());s['revisions'].append(dict(tts_text=s['tts_text'],qa_expected_text=s['qa_expected_text'],text_revision=s['text_revision']))
                    s.update(tts_text=prepared['tts_text'],qa_expected_text=prepared['qa_expected_text'],text_revision=s['text_revision']+1)

                if 'note' in p:
                    if not isinstance(p['note'],str) or len(p['note'])>10000:raise ValueError('Ungültige Notiz')
                    if s['note']:s['note_history'].append(dict(note=s['note'],binding=s.get('note_binding')))
                    s['note']=p['note'];s['note_binding']=dict(audio=row['current']['binding'] if row['current'] else None,text_revision=s['text_revision'],text_sha256=digest([s['tts_text'],s['qa_expected_text']]))
                if 'method' in p:
                    if p['method'] not in RULES:raise ValueError('Unbekannte Methode')
                    s['method']=p['method']
                    s['next_preset']=self.legacy_preset(did,p['method'])
                    preset=(row.get('current') or {}).get('preset') or {}
                    if p['method'] in ('anchor','prewarm'):s['next_preset'][p['method']]='off' if preset.get(p['method']) else 'on'
                if 'next_preset' in p:s['next_preset']=copy.deepcopy(p['next_preset'])
                if 'generation_mode' in p:s['generation_mode']=p['generation_mode']
                if 'generation_mode' in p:s['processing_binding']=self.original_output(did)['binding'] if s['generation_mode']=='copy_original' else None
                if 'anchor_ref' in p:
                    s['anchor_ref']=self.validate_anchor_ref(did,p['anchor_ref'],deep=True)
                self.validate_settings(s)
                if 'backend' in p:
                    if p['backend'] not in (None,'gpu','ane','cpu'):raise ValueError('Unbekanntes Backend')
                    s['backend']=p['backend']
            elif operation=='mark':
                if type(p.get('marked')) is not bool:raise ValueError('marked muss bool sein')
                s['marked']=p['marked']
            elif operation=='good':
                if type(p.get('good')) is not bool:raise ValueError('good muss bool sein')
                out=self.verified_current(did)
                if p.get('binding')!=out['binding']:raise RuntimeError('Audioausgabe hat sich geändert')
                s['good_binding']=out['binding'] if p['good'] else None
                if not p['good'] and before.get('good_binding')!=out['binding']:
                    s['good_binding']=before.get('good_binding')
                s['releases'].append(dict(binding=out['binding'],approval_event=True,approved=p['good']))
                if p['good'] and out['id']!='original':
                    attempt=self.attempts.get(out['id'],(None,))[0]
                    if attempt:
                        s['next_preset']['seed']='keep'
                        s['action_baseline']=self.action_settings(s)
            elif operation=='release':
                automatic_repair=p.get('automatic_repair',False)
                if type(automatic_repair) is not bool:raise ValueError('automatic_repair muss bool sein')
                repair_stage=p.get('repair_stage')
                if repair_stage is not None and (not automatic_repair or not isinstance(repair_stage,str) or not repair_stage.strip() or len(repair_stage)>100):
                    raise ValueError('repair_stage ist nur als kurze Angabe für automatische Reparaturen zulässig')
                if automatic_repair and p.get('ignore_warnings') is True:raise ValueError('Automatische Reparatur verwendet keine menschliche Warnungsfreigabe')
                c=next((c for c in s['candidates'] if self.candidate_id(c)==p.get('candidate_id')),None)
                if c is not None:
                    out=self.candidate_output(c,deep=True)
                else:
                    base=self.base[did]
                    historical=base.get('current') if (base.get('current') or {}).get('id')==p.get('candidate_id') else next((o for o in base.get('history',[]) if o.get('id')==p.get('candidate_id')),None)
                    if historical is None:raise ValueError('Unbekannter Kandidat')
                    out=self.validate_output(did,historical)
                if not out['verified']:raise ValueError('Kandidatenbeleg ungültig')
                if automatic_repair and not self.clean_automatic_repair_output(out):
                    raise ValueError('Automatische Übernahme nur für tief geprüfte, vollständig unauffällige Audioausgaben mit QA und STT möglich')
                if out['warning'] and p.get('ignore_warnings') is not True and not self.is_approved(s,out['binding']):raise ValueError('Warnungen müssen ausdrücklich bestätigt werden')
                self.preserve_legacy_approval(s)
                try:previous_binding=self.verified_current(did)['binding']
                except (ValueError,KeyError,OSError):previous_binding=None
                release_record=dict(binding=out['binding'],previous_selection=previous_binding,ignore_warnings=p.get('ignore_warnings') is True)
                if automatic_repair:
                    release_record.update(automatic_repair=True,via='automatic_repair')
                    if repair_stage is not None:release_record['repair_stage']=repair_stage.strip()
                s['selection']=out['binding'];s['releases'].append(release_record);s['marked']=False
                receipt=self.request_receipt(c) if c and c.get('review_request_sha256') else None
                attempt=self.attempts.get(out['id'],(None,))[0]
                if attempt:
                    s['tts_text']=attempt['target_text'];s['qa_expected_text']=attempt['qa_expected_text']
                    if s['text_revision']<attempt.get('variant_revision',s['text_revision']):s['text_revision']=attempt['variant_revision']
                    s['next_preset']={'seed':'keep','anchor':'on' if attempt['fixed_anchor'] else 'off','prewarm':'on' if attempt['prewarm'] else 'off'}
                    s['backend']=attempt['render_execution']['profile_id']
                    s['anchor_ref']=self.validate_anchor_ref(did,receipt['request'].get('anchor_ref'),deep=True) if receipt and receipt['request'].get('anchor_ref') else None
                    s['action_baseline']=self.action_settings(s)
                if receipt:
                    s['accepted_probe']=dict(candidate_id=out['id'],binding=out['binding'],effective_backend=receipt['request']['backend'],request_sha256=c['review_request_sha256'])
                else:s['accepted_probe']=None
                if out['warning']:s['good_binding']=out['binding']
                if self.state.get('render_recipe'):
                    from manifest_renderer.recipes import export_recipe
                    from manifest_renderer.audio import resolve_manifest_path
                    if c:
                        concrete=self.hydrate_candidate(c)
                        snapshot=self.read_cached(resolve_manifest_path(self.root,concrete['manifest_path']))
                    else:snapshot=self.manifest
                    accepted=export_recipe(snapshot,{did:attempt})
                    self.state['render_recipe']['dialogues'][did]=accepted['dialogues'][did]
            elif operation=='release-original':
                if s['generation_mode']!='copy_original':raise ValueError('Zuerst Englisches Original übernehmen auswählen')
                if self.state['job']['status']=='running':raise RuntimeError('Ein Reviewauftrag läuft bereits')
                out=self.original_output(did,deep=True)
                if not out['verified']:raise ValueError('Englische Sprachausgabe verändert oder Originalbeleg ungültig')
                if p.get('binding')!=out['binding']:raise RuntimeError('Bindung der englischen Sprachausgabe zum Text hat sich geändert')
                self.preserve_legacy_approval(s)
                try:previous_binding=self.verified_current(did)['binding']
                except (ValueError,KeyError,OSError):previous_binding=None
                s['selection']=out['binding'];s['releases'].append(dict(binding=out['binding'],previous_selection=previous_binding,ignore_warnings=False,processing='copy_original'));s['marked']=False;s['good_binding']=None;s['accepted_probe']=None
                if s['next_preset']['seed']=='keep':s['next_preset']['seed']='fresh'
                s['action_baseline']=self.action_settings(s)
            elif operation=='restore-previous':
                if set(p)-{'revision','dialog_id','expected_current_binding'}:raise ValueError('Unbekannte Wiederherstellungsfelder')
                if self.state['job']['status']=='running':raise RuntimeError('Ein Reviewauftrag läuft bereits')
                out=self.previous_output(did,s,p.get('expected_current_binding'))
                self.preserve_legacy_approval(s)
                previous_binding=self.verified_current(did)['binding']
                if out['id']=='original':
                    s['generation_mode']='copy_original';s['processing_binding']=out['binding'];s['good_binding']=None;s['accepted_probe']=None
                    if s['next_preset']['seed']=='keep':s['next_preset']['seed']='fresh'
                    s['tts_text']=out['tts_text'];s['qa_expected_text']=out['qa_expected_text']
                else:
                    s['generation_mode']='synthesize';s['processing_binding']=None
                    attempt=self.attempts.get(out['id'],(None,))[0]
                    if attempt is None:
                        candidate=next((c for c in s['candidates'] if self.candidate_id(c)==out['id']),None)
                        if candidate:attempt=self.hydrate_candidate(candidate)['attempt']
                    if attempt is None:raise ValueError('Vorherige Syntheseeinstellungen sind nicht verfügbar')
                    candidate=next((c for c in s['candidates'] if self.candidate_id(c)==out['id']),None)
                    receipt=self.request_receipt(candidate) if candidate and candidate.get('review_request_sha256') else None
                    s['tts_text']=attempt['target_text'];s['qa_expected_text']=attempt['qa_expected_text']
                    if s['text_revision']<attempt.get('variant_revision',s['text_revision']):s['text_revision']=attempt.get('variant_revision',s['text_revision'])
                    s['next_preset']={'seed':'keep','anchor':'on' if attempt['fixed_anchor'] else 'off','prewarm':'on' if attempt['prewarm'] else 'off'}
                    s['backend']=attempt['render_execution']['profile_id']
                    s['anchor_ref']=self.validate_anchor_ref(did,receipt['request'].get('anchor_ref'),deep=True) if receipt and receipt['request'].get('anchor_ref') else None
                    s['good_binding']=out['binding'] if out['warning'] else None;s['accepted_probe']=None
                s['selection']=out['binding'];s['releases'].append(dict(binding=out['binding'],previous_selection=previous_binding,ignore_warnings=bool(out['warning']),processing='copy_original' if out['id']=='original' else 'render'));s['marked']=False
                s['action_baseline']=self.action_settings(s)
                if out['id']!='original' and self.state.get('render_recipe'):
                    from manifest_renderer.recipes import export_recipe
                    from manifest_renderer.audio import resolve_manifest_path
                    concrete=self.hydrate_candidate(next(c for c in s['candidates'] if self.candidate_id(c)==out['id'])) if any(self.candidate_id(c)==out['id'] for c in s['candidates']) else None
                    snapshot=self.read_cached(resolve_manifest_path(self.root,concrete['manifest_path'])) if concrete else self.manifest
                    self.state['render_recipe']['dialogues'][did]=export_recipe(snapshot,{did:attempt})['dialogues'][did]
            else:raise ValueError('Unbekannte Aktion')
            if operation=='update' and 'generation_mode' in p and self.processing_callback:
                original=self.original_output(did,deep=True)
                if not original['verified']:raise ValueError('Originalbeleg für die Manifestverarbeitung ist ungültig')
                self.processing_callback(did,'copy_original' if s['generation_mode']=='copy_original' else 'render',original['binding'],original['binding'])
            if operation=='update' and 'tts_text' in p and self.text_callback:self.text_callback(did,s['tts_text'])
            if operation in ('release','release-original','restore-previous'):
                original_binding=self.original_output(did)['binding']
                if self.processing_callback:self.processing_callback(did,'copy_original' if s['generation_mode']=='copy_original' else 'render',original_binding,original_binding)
                elif self.selection_callback:self.selection_callback(did,'copy_original' if out['id']=='original' else 'render',out['binding'],original_binding)
            self.state['rows'][did]=s
            recipe=self.state.get('render_recipe')
            if operation=='update' and recipe and did in recipe['dialogues'] and recipe['dialogues'][did]['processing']=='render':
                if p.get('backend') in ('gpu','ane') and p['backend']!=recipe['dialogues'][did]['backend']:
                    recipe['dialogues'][did]['backend']=p['backend']
                    recipe['dialogues'][did].pop('effective_parameters',None)
            self.state['revision']+=1;self.persist()
            manifest_changed=(operation in ('release','release-original','restore-previous','good') or before['tts_text']!=s['tts_text'] or before['generation_mode']!=s['generation_mode'] or self.next_action(before)!=self.next_action(s))
            if manifest_changed:self.write_refined_manifest([did])
            if operation in ('release','release-original','restore-previous'):self.write_selection([did])
            return dict(revision=self.state['revision'],row=self.row(did),recipe=self.recipe_info())

    def write_refined_manifest(self,dialog_ids=None):
        # A settings save changes one dialogue, not the evidence of every
        # previously saved selection. Reuse those already persisted effective
        # entries; full rebuilds (startup/import/process) still revalidate all.
        previous=getattr(self,'_refined_manifest',None)
        changed=set(dialog_ids) if dialog_ids is not None and previous is not None else None
        previous_dialogues={d['dialog_id']:d for d in previous['dialogues']} if changed is not None else {}
        m=copy.deepcopy(self.manifest);m['manifest_revision']+=self.state['revision'];m.setdefault('project_policy',{})['review_method_rules']=self.state['method_rules']
        if changed is not None:
            known_roles={r['role_id'] for r in m['roles']}
            m['roles'].extend(copy.deepcopy(r) for r in previous['roles'] if r['role_id'] not in known_roles)
        for index,d in enumerate(m['dialogues']):
            if changed is not None and d['dialog_id'] not in changed and d['dialog_id'] in previous_dialogues:
                m['dialogues'][index]=copy.deepcopy(previous_dialogues[d['dialog_id']])
                continue
            s=self.state['rows'].get(d['dialog_id'])
            if not s:continue
            if s['generation_mode']=='copy_original':
                original=self.original_output(d['dialog_id'],deep=True)
                if original['verified'] and original['binding']==s['processing_binding']:
                    d.update(self.original_dialogue(d['dialog_id']))
                    continue
            selected=next((c for c in s['candidates'] if self.candidate_id(c)==(s.get('selection') or {}).get('fingerprint')),None)
            if selected:
                try:
                    from manifest_renderer.audio import resolve_manifest_path
                    if not self.candidate_output(selected,deep=True)['verified']:raise ValueError('Nicht verfügbare importierte Auswahl')
                    snapshot_path=resolve_manifest_path(self.root,selected['manifest_path'])
                    chosen,candidate_role=self.snapshot_projection(snapshot_path,d['dialog_id'])
                    d.update(copy.deepcopy(chosen))
                    if candidate_role and not any(old_role['role_id']==candidate_role['role_id'] for old_role in m['roles']):m['roles'].append(candidate_role)
                except (OSError,ValueError,KeyError,StopIteration):pass
            old=next((v for v in d.get('text_variants',[]) if v['variant_id']==d.get('selected_variant_id')),None)
            if s['generation_mode']=='synthesize' and self.next_action(s)=='render':d['processing']='render'
            if old is None and s['tts_text'].strip():
                old=dict(variant_id=d['dialog_id']+':de:review-base',variant_revision=1,display_text=self.base[d['dialog_id']]['display_text'] or s['tts_text'],tts_text='',qa_expected_text='',qa_allowed_variants=[],provenance=dict(kind='manual',source_ref='patch_review',source_version='1'),creation_version='patch-review:1',edit_version='1',edit_status='user_edited',review_status='unreviewed',normalization_steps=[])
            if old and old['tts_text']!=s['tts_text']:
                v=copy.deepcopy(old);v['variant_id']=old['variant_id']+':review:'+str(s['text_revision']);v['variant_revision']=old['variant_revision']+s['text_revision'];v.update(tts_text=s['tts_text'],qa_expected_text=s['qa_expected_text'],qa_allowed_variants=[],edit_status='user_edited',edit_version=str(s['text_revision']));v['sha256']=digest([v[k] for k in ('display_text','tts_text','qa_expected_text','qa_allowed_variants')]);d['text_variants'].append(v);d['selected_variant_id']=v['variant_id']
        for did,s in self.state['rows'].items():self.persist_accepted_seed(m,did,self.accepted_seed(did,s))
        if self.state.get('render_recipe'):
            accepted_dialogues={}
            for did,entry in self.state['render_recipe']['dialogues'].items():
                s=self.state['rows'].get(did,{})
                if s.get('selection') and s['selection'].get('fingerprint')!='original' and self.accepted_seed(did,s) is not None:
                    accepted_dialogues[did]=copy.deepcopy(entry)
            if accepted_dialogues:
                recipe=copy.deepcopy(self.state['render_recipe']);recipe['dialogues']=accepted_dialogues
                m.setdefault('source_evidence',{})['portable_render_recipe']=recipe
        active_roles={d.get('role_id') for d in m['dialogues'] if d['processing']=='render' and d.get('content_type')=='speech'}
        m['roles']=[r for r in m['roles'] if r['role_id'] in active_roles]
        if not active_roles and 'scope' in m:
            m['manifest_profile']='local_render';m.setdefault('source_evidence',{}).setdefault('source_dialog_ids',list(m['scope']['included_dialog_ids']))
        atomic_json(self.root/'review-manifest.json',m)
        self._refined_manifest=dict(dialogues=m['dialogues'],roles=m['roles'])
    def write_selection(self,dialog_ids=None):
        previous=getattr(self,'_selection_projection',None)
        changed=set(dialog_ids) if dialog_ids is not None and previous is not None else None
        if changed is None:
            selections={};patch_manifest=copy.deepcopy(self.manifest)
        else:
            selections=copy.deepcopy(previous['selections'])
            patch_manifest=copy.deepcopy(previous['manifest'])
            patch_manifest['roles']=copy.deepcopy(self.manifest.get('roles',[]))
            known_roles={r['role_id'] for r in patch_manifest['roles']}
            patch_manifest['roles'].extend(copy.deepcopy(r) for r in previous['manifest'].get('roles',[]) if r['role_id'] not in known_roles)
            for did in changed:
                selections.pop(did,None)
                # Rebuild the changed dialogue from its source before checking
                # its current selection. This also restores source seed inputs
                # if the new binding is missing or invalid.
                source=next((d for d in self.manifest['dialogues'] if d['dialog_id']==did),None)
                if source is not None:
                    for index,d in enumerate(patch_manifest['dialogues']):
                        if d['dialog_id']==did:
                            patch_manifest['dialogues'][index]=copy.deepcopy(source);break
                seed_inputs=patch_manifest.get('source_evidence',{}).get('launcher_render_inputs',{})
                if isinstance(seed_inputs,dict):seed_inputs.pop(did,None)
        ids=changed if changed is not None else set(self.state['rows'])
        for did in ids:
            s=self.state['rows'].get(did)
            if not s:continue
            selected=s.get('selection')
            if not selected:continue
            if selected.get('fingerprint')=='original':
                try:
                    out=self.original_output(did,deep=True)
                    if not out['verified'] or out['binding']!=selected or not any(r.get('binding')==selected and r.get('processing')=='copy_original' for r in s['releases']):continue
                    source=self.evidence.audio('original:'+did,selected['wav_sha256'])
                    relative='selected/'+selected['wav_sha256']+source.suffix.lower();target=self.root/relative
                    self.materialize(source,target,selected['wav_sha256'])
                    chosen=self.original_dialogue(did)
                    for index,d in enumerate(patch_manifest['dialogues']):
                        if d['dialog_id']==did:patch_manifest['dialogues'][index]=chosen
                    selections[did]=dict(wav=relative,binding=selected,processing='copy_original',tts_text=out['tts_text'],qa_expected_text='',stt_status='not_required',ignore_warnings=False)
                except (KeyError,ValueError,OSError):pass
                continue
            c=next((c for c in s['candidates'] if self.candidate_id(c)==selected['fingerprint']),None)
            try:
                if c is not None:
                    c=self.hydrate_candidate(c)
                    out=self.candidate_output(c,deep=True)
                    snapshot_path=self.root/c['manifest_path']
                    chosen,candidate_role=self.snapshot_projection(snapshot_path,did)
                    manifest_snapshot=c['manifest_path']
                    attempt=c['attempt']
                else:
                    historical=next((o for o in self.base[did].get('history',[]) if o.get('id')==selected['fingerprint']),None)
                    if historical is None:continue
                    out=self.validate_output(did,historical);attempt=self.attempts[out['id']][0]
                    snapshot=self.read_cached(self.evidence.run/'manifest.json');manifest_snapshot=None
                    chosen=next(d for d in snapshot['dialogues'] if d['dialog_id']==did);candidate_role=None
                if not out['verified'] or out['binding']!=selected:continue
                source=self.evidence.audio('attempt:'+out['id'],selected['wav_sha256'])
                relative='selected/'+selected['wav_sha256']+'.wav';target=self.root/relative
                self.materialize(source,target,selected['wav_sha256'])
                for index,d in enumerate(patch_manifest['dialogues']):
                    if d['dialog_id']==did:patch_manifest['dialogues'][index]=copy.deepcopy(chosen)
                if manifest_snapshot and candidate_role and not any(old['role_id']==candidate_role['role_id'] for old in patch_manifest['roles']):patch_manifest['roles'].append(candidate_role)
                selection=dict(wav=relative,binding=selected,tts_text=attempt['target_text'],qa_expected_text=attempt['qa_expected_text'],text_revision=attempt['variant_revision'],ignore_warnings=bool(out['warning'] and self.is_approved(s,selected)))
                if manifest_snapshot:selection['manifest_snapshot']=manifest_snapshot
                selections[did]=selection
                self.persist_accepted_seed(patch_manifest,did,attempt['seed'])
            except (KeyError,ValueError,OSError):continue
        if selections or (self.root/'patch-selection.json').exists():
            active_roles={d.get('role_id') for d in patch_manifest['dialogues'] if d['processing']=='render' and d.get('content_type')=='speech'}
            patch_manifest['roles']=[r for r in patch_manifest['roles'] if r['role_id'] in active_roles]
            if not active_roles and 'scope' in patch_manifest:
                patch_manifest['manifest_profile']='local_render';patch_manifest.setdefault('source_evidence',{}).setdefault('source_dialog_ids',list(patch_manifest['scope']['included_dialog_ids']))
            atomic_json(self.root/'patch-manifest.json',patch_manifest)
            atomic_json(self.root/'patch-selection.json',dict(format='patch-review-selection/1',manifest_id=self.manifest['manifest_id'],source_manifest_sha256=self.state['source_manifest_sha256'],effective_manifest='patch-manifest.json',correction_manifest='review-manifest.json',dialogues=selections))
        self._selection_projection=dict(manifest=copy.deepcopy(patch_manifest),selections=copy.deepcopy(selections))
    def materialize(self,source,target,expected):
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            fd,name=tempfile.mkstemp(prefix='.selected-',dir=target.parent);os.close(fd)
            try:shutil.copyfile(source,name);os.replace(name,target)
            finally:Path(name).unlink(missing_ok=True)
        if not self.evidence.file_valid(target,expected):raise ValueError('Freigegebene lokale Audiodatei verändert')
    def export(self):
        if self.mode!='patch':raise ValueError('Korrekturexport ist nur im Patchreview verfügbar')
        with self.lock:
            rows=copy.deepcopy(self.state['rows'])
            for row in rows.values():row['candidates']=[self.candidate_reference(c) for c in row['candidates']]
            extension=dict(format='patch-review-extension/1',manifest_id=self.state['manifest_id'],source_manifest_sha256=self.state['source_manifest_sha256'],game_id=self.manifest.get('game',{}).get('game_id'),method_rules=self.state['method_rules'],rows=rows)
            if self.state.get('render_recipe'):extension['render_recipe']=copy.deepcopy(self.state['render_recipe'])
            return extension

    def export_recipe(self):
        """Share the executed recipes of selected outputs, never draft settings."""
        from manifest_renderer.recipes import export_recipe,validate_recipe
        with self.lock:
            entries={}
            for did in self.dialogues:
                out=self.verified_current(did)
                if out['id']=='original':
                    entries[did]=dict(processing='copy_original')
                    continue
                candidate=next((c for c in self.saved(did)['candidates'] if self.candidate_id(c)==out['id']),None)
                if candidate:
                    candidate=self.hydrate_candidate(candidate)
                    entry=copy.deepcopy(candidate['attempt'])
                    from manifest_renderer.audio import resolve_manifest_path
                    entry['_recipe_manifest']=self.read_cached(resolve_manifest_path(self.root,candidate['manifest_path']))
                else:entry=copy.deepcopy(self.attempts[out['id']][0])
                entries[did]=entry
            recipe=export_recipe(self.manifest,entries)
            if self.state.get('render_recipe'):
                recipe['pipeline']=self.state['render_recipe']['pipeline']
                recipe=validate_recipe(recipe,self.manifest)
            return recipe

    def recipe_info(self):
        recipe=self.state.get('render_recipe')
        if not recipe:return None
        from manifest_renderer.recipes import pipeline_label,recipe_warnings
        return dict(pipeline=recipe['pipeline'],label=pipeline_label(recipe),warnings=recipe_warnings(recipe))

    def save_recipe(self,p):
        with self.lock:
            self.check(p)
            recipe=self.export_recipe()
            path=self.root/('mi1-rezept-'+recipe['pipeline']+'.json')
            atomic_json(path,recipe)
            return dict(path=str(path),recipe=recipe)

    def save_extension(self,p):
        with self.lock:
            self.check(p)
            extension=self.export()
            path=self.root/'patch-review-extension.json'
            atomic_json(path,extension)
            return dict(path=str(path),extension=extension)

    def import_recipe(self,p):
        from manifest_renderer.recipes import validate_recipe
        with self.lock:
            self.check(p)
            if self.mode!='patch':raise ValueError('Rezeptladen ist nur im Patchreview verfügbar')
            if self.render_availability()['busy']:raise RuntimeError('Rezeptladen ist während eines laufenden Auftrags gesperrt')
            recipe=validate_recipe(p.get('extension'),self.manifest)
            # Compare recipes, not sender hashes. Import neither generates audio
            # nor activates the sender's selection/good/release decisions.
            try:existing=self.export_recipe()
            except (ValueError,KeyError,OSError):existing=None
            checked={};changed=0
            for did,entry in recipe['dialogues'].items():
                s=copy.deepcopy(self.saved(did))
                if entry['processing']=='copy_original':
                    s['generation_mode']='copy_original'
                    s['processing_binding']=self.original_output(did)['binding']
                else:
                    s.update(tts_text=entry['tts_text'],qa_expected_text=prepare_text(entry['tts_text'])['qa_expected_text'],
                        text_revision=s['text_revision']+1,generation_mode='synthesize',backend=entry['backend'],
                        next_preset=dict(seed='keep',anchor='on' if entry['anchor'] else 'off',prewarm='on' if entry['prewarm'] else 'off'),anchor_ref=None)
                role_id=self.dialogues[did].get('role_id')
                same=bool(existing and existing['dialogues'].get(did)==entry and
                    existing.get('roles',{}).get(role_id)==recipe.get('roles',{}).get(role_id))
                s['marked']=not same
                if same:s['action_baseline']=self.action_settings(s)
                else:changed+=1
                s['accepted_probe']=None
                checked[did]=s
            self.state['rows'].update(checked);self.state['render_recipe']=recipe
            self.state['revision']+=1;self.persist();self.write_refined_manifest()
            return dict(revision=self.state['revision'],rows=[self.row(did) for did in checked],
                recipe=self.recipe_info(),changed_dialogues=changed)

    def portable_recipe_for(self,did,row,backend=None):
        """The imported recipe remains the default; row edits are explicit overrides."""
        stored=self.state.get('render_recipe')
        if not stored or did not in stored['dialogues'] or stored['dialogues'][did]['processing']!='render':return None
        recipe=copy.deepcopy({key:value for key,value in stored.items() if key!='dialogues'})
        entry=copy.deepcopy(stored['dialogues'][did]);recipe['dialogues']={did:entry}
        entry['tts_text']=row['tts_text']
        requested=row['backend'] or backend or entry['backend']
        if requested!=entry['backend']:entry.pop('effective_parameters',None)
        entry['backend']=requested
        for field in ('anchor','prewarm'):
            choice=row['next_preset'][field]
            if choice!='keep':entry[field]=choice=='on'
        return recipe

    def make_recipe_attempt(self,did,backend,job_id,row,fresh_seed=False):
        from manifest_renderer.recipes import apply_recipe,materialize_recipe_references
        from manifest_renderer.plan import build_run_plan
        recipe=self.portable_recipe_for(did,row,backend)
        if fresh_seed or type(recipe['dialogues'][did].get('seed')) is not int:
            seed=secrets.randbelow(2**31)
            while seed==recipe['dialogues'][did]['seed']:seed=secrets.randbelow(2**31)
            recipe['dialogues'][did]['seed']=seed
        m=copy.deepcopy(self.manifest)
        m['manifest_revision']+=self.state['revision']+1;m['output']['root']='jobs/'+job_id
        m=apply_recipe(m,recipe)
        m=materialize_recipe_references(m,recipe,asset_root=self.evidence.root,state_root=self.root,executor_factory=self.executor)
        selected=profile(backend)
        if hasattr(self.executor,'backend_profile'):selected=self.executor.backend_profile(backend,m)
        m['render_execution']=selected
        m['runtime']['backend']={'gpu':'metal','ane':'ane','cpu':'cpu'}[backend]
        for hardware in m.get('hardware',{}).get('profiles',[]):
            if 'render' in hardware.get('stages',[]):
                hardware['execution_path']=m['runtime']['backend']
                for section in ('minimum','recommended'):
                    if section in hardware:hardware[section]['accelerator']['api']=m['runtime']['backend']
        if hasattr(self.executor,'runtime_for_attempt'):m['runtime']=self.executor.runtime_for_attempt(backend,m)
        m.setdefault('source_evidence',{}).setdefault('production',{})['review_code_files']=self.review_code_receipt()
        m['source_evidence']['production']['review_qa_semantics']='nfkc-casefold-sharp-s-named-date-v2'
        attempt=next(d for d in build_run_plan(m)['dialogues'] if d['dialog_id']==did)['attempts'][0]
        atomic_json(self.root/'jobs'/job_id/(digest(did)+'.manifest.json'),m)
        return attempt,m

    @staticmethod
    def recipe_request(attempt,row,fresh_seed=False):
        return dict(dialog_id=attempt['dialog_id'],tts_text=attempt['target_text'],qa_expected_text=attempt['qa_expected_text'],
            seed_policy='fresh' if fresh_seed or row['next_preset']['seed']=='fresh' else 'keep',seed_value=None if fresh_seed or row['next_preset']['seed']=='fresh' else attempt['seed'],
            anchor=bool(attempt['fixed_anchor']),prewarm=bool(attempt['prewarm']),
            backend=attempt['render_execution']['profile_id'],backend_profile_sha256=digest(attempt['render_execution']),
            method=attempt['method'],portable_recipe=True)
    def save_todo_diagnostic(self,report):
        if self.mode!='patch':raise ValueError('Todo-Diagnoseexport ist nur im Patchreview verfügbar')
        if not isinstance(report,dict) or report.get('format')!='patch-review-todo-diagnostic/1':raise ValueError('Ungültiger Todo-Diagnosebericht')
        if report.get('manifest_id')!=self.manifest['manifest_id'] or report.get('manifest_sha256')!=self.evidence.manifest_hash:raise ValueError('Todo-Diagnosebericht gehört zu einem anderen Manifest')
        if type(report.get('revision')) is not int or report['revision']!=self.state['revision']:raise RuntimeError('Reviewstand wurde geändert; Diagnose bitte erneut exportieren')
        rows=report.get('rows')
        if not isinstance(rows,list) or len(rows)>len(self.dialogues):raise ValueError('Ungültige Todo-Zeilen')
        allowed={'dialog_id','reasons','processing','next_action','row_warning','row_good','current_verified','current_warning','qa_status','review_status','stt_status','findings','qa_reason_codes','open_candidates','original_text','display_text','tts_text','qa_expected_text'}
        safe_rows=[]
        for row in rows:
            if not isinstance(row,dict) or set(row)-allowed or str(row.get('dialog_id')) not in self.dialogues:raise ValueError('Ungültiger Todo-Zeileneintrag')
            if not isinstance(row.get('reasons'),list) or any(reason not in {'running_job','planned_render','open_candidates','warning','current_unverified','not_reviewed_or_good'} for reason in row['reasons']):raise ValueError('Ungültige Todo-Klassifikation')
            if not isinstance(row.get('open_candidates',[]),list) or any(not isinstance(c,dict) or set(c)-{'id','verified','warning'} for c in row.get('open_candidates',[])):raise ValueError('Ungültige offene Kandidaten')
            safe_rows.append(row)
        counts=report.get('reason_counts',{})
        allowed_reasons={'running_job','planned_render','open_candidates','warning','current_unverified','not_reviewed_or_good'}
        if not isinstance(counts,dict) or set(counts)-allowed_reasons or any(type(value) is not int or value<0 or value>len(self.dialogues) for value in counts.values()):raise ValueError('Ungültige Todo-Grundzahlen')
        safe=dict(format='patch-review-todo-diagnostic/1',manifest_id=self.manifest['manifest_id'],manifest_sha256=self.evidence.manifest_hash,revision=report['revision'],total_rows=len(self.dialogues),todo_rows=len(safe_rows),reviewed_rows=len(self.dialogues)-len(safe_rows),reason_counts_overlap=True,reason_counts=counts,rows=safe_rows)
        path=self.root/'todo-diagnostic.json';atomic_json(path,safe)
        return dict(path=str(path),todo_rows=len(safe_rows),revision=self.state['revision'])
    def import_extension(self,p):
        from manifest_renderer.recipes import FORMAT
        if isinstance(p.get('extension'),dict) and p['extension'].get('format')==FORMAT:return self.import_recipe(p)
        with self.lock:
            self.check(p)
            if self.mode!='patch':raise ValueError('Korrekturladen ist nur im Patchreview verfügbar')
            e=p.get('extension',{})
            if e.get('format')!='patch-review-extension/1' or e.get('manifest_id')!=self.state['manifest_id'] or e.get('game_id')!=self.manifest.get('game',{}).get('game_id'):raise ValueError('Manifesterweiterung gehört zu einem anderen Manifest')
            if e.get('method_rules')!=RULES or not isinstance(e.get('rows'),dict):raise ValueError('Ungültige Erweiterung')
            from manifest_renderer.recipes import validate_recipe
            imported_recipe=validate_recipe(e['render_recipe'],self.manifest) if e.get('render_recipe') else None
            checked={}
            for did,s in e['rows'].items():
                if did not in self.base:raise ValueError('Unbekannter importierter Dialog')
                if not isinstance(s,dict):raise ValueError('Ungültiger importierter Korrekturstand')
                s=self.upgrade_settings(did,copy.deepcopy(s));self.validate_settings(s)
                fields=set(self.saved(did))
                if not isinstance(s,dict) or set(s)!=fields or not isinstance(s['tts_text'],str) or (not s['tts_text'].strip() and self.base[did]['processing']!='copy_original') or len(s['tts_text'])>4000 or s['qa_expected_text']!=prepare_text(s['tts_text'])['qa_expected_text'] or s['method'] not in RULES or s['backend'] not in (None,'gpu','ane','cpu') or type(s['marked']) is not bool or type(s['text_revision']) is not int or not isinstance(s['note'],str) or len(s['note'])>10000 or s['text_revision']<1 or any(not isinstance(s[key],list) for key in ('candidates','revisions','releases','note_history')):raise ValueError('Ungültiger importierter Korrekturstand')
                value=copy.deepcopy(s)
                # Candidate payloads only attach if their bytes already exist in our isolated store.
                candidates=[]
                for c in value['candidates']:
                    try:
                        if self.candidate_output(c,deep=True)['verified']:candidates.append(c)
                    except (KeyError,ValueError,OSError):pass
                # Keep unavailable historical receipts in subsequent exports.
                # row() never selects them without matching local bytes.
                checked[did]=value
            self.state['rows'].update(checked);self.state['method_rules']=e['method_rules']
            if imported_recipe:self.state['render_recipe']=imported_recipe
            elif self.state.get('render_recipe'):
                for did in checked:self.state['render_recipe']['dialogues'].pop(did,None)
                if not self.state['render_recipe']['dialogues']:self.state.pop('render_recipe',None)
            self.state['revision']+=1;self.persist();self.write_refined_manifest();self.write_selection()
            return dict(revision=self.state['revision'],rows=[self.row(d) for d in checked],recipe=self.recipe_info())

    def review_code_receipt(self):
        from manifest_renderer.production import CODE_FILES
        from manifest_renderer.audio import sha256_file
        root=Path(__file__).resolve().parents[1]
        paths=[root/name for name in CODE_FILES]
        identity=[]
        for path in paths:
            stat=path.stat()
            identity.append((str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,getattr(stat,'st_ino',None)))
        identity=tuple(identity)
        cached=self._code_receipt_cache
        if cached is None or cached[0]!=identity:
            value=[dict(path=name,sha256=sha256_file(root/name)) for name in CODE_FILES]
            self._code_receipt_cache=(identity,value)
        return copy.deepcopy(self._code_receipt_cache[1])
    def make_attempt(self,did,backend,job_id,row=None,current=None,fresh_seed=False):
        row=row or self.row(did)
        if self.portable_recipe_for(did,row,backend):return self.make_recipe_attempt(did,backend,job_id,row,fresh_seed=fresh_seed)
        current=current or self.validate_output(did,row.get('current'))
        preset=current.get('preset')
        original=current['id']=='original'
        if preset is None and not original:raise ValueError('Für Neurender fehlt ein belegtes gerendertes Preset')
        m=copy.deepcopy(self.manifest);m['manifest_revision']+=self.state['revision']+1;m['output']['root']='jobs/'+job_id
        selected=profile(backend)
        if hasattr(self.executor,'backend_profile'):selected=self.executor.backend_profile(backend,m)
        m['render_execution']=selected
        if hasattr(self.executor,'runtime_for_attempt'):m['runtime']=self.executor.runtime_for_attempt(backend,m)
        # Record the locally executing review implementation while preserving
        # the source run's original production code receipts.
        m.setdefault('source_evidence',{}).setdefault('production',{})['review_code_files']=self.review_code_receipt()
        m['source_evidence']['production']['review_qa_semantics']='nfkc-casefold-sharp-s-named-date-v2'
        m['runtime']['backend']={'gpu':'metal','cpu':'cpu','ane':'ane'}[backend]
        d=copy.deepcopy(self.dialogues[did])
        role=copy.deepcopy(self.roles.get(d.get('role_id'),{}))
        if not role:
            if not original:raise ValueError('Keine konfigurierte Syntheserolle')
            role=dict(role_id=d.get('role_id') or did+':review-role',name='Originalreferenz',speaker_evidence='Explizit gestartete Neusynthese einer Originalübernahme aus deren belegter Originalreferenz.',without_anchor=True,without_prewarm=True)
            d['role_id']=role['role_id'];m['roles'].append(role)
        d['processing']='render'
        old=next((v for v in d['text_variants'] if v['variant_id']==d['selected_variant_id']),None)
        if old is None:
            if not row['tts_text'].strip():raise ValueError('Für die Originalübernahme zuerst einen deutschen Sprachausgabetext speichern')
            old=dict(variant_id=did+':de:review',variant_revision=1,display_text=row['display_text'] or row['tts_text'],tts_text='',qa_expected_text='',qa_allowed_variants=[],provenance=dict(kind='manual',source_ref='patch_review',source_version='1'),creation_version='patch-review:1',edit_version='1',edit_status='user_edited',review_status='unreviewed',normalization_steps=[])
        v=copy.deepcopy(old)
        if original:d['content_type']='speech'
        v.update(tts_text=row['tts_text'],qa_expected_text=row['qa_expected_text'],variant_id=v['variant_id']+':review:'+str(row['text_revision']),variant_revision=v['variant_revision']+row['text_revision'])
        if v['tts_text']!=old['tts_text']:v['qa_allowed_variants']=[]
        v['sha256']=digest([v[k] for k in ('display_text','tts_text','qa_expected_text','qa_allowed_variants')])
        if original:
            # First synthesis starts from the configured role; no prior preset is claimed.
            preset=dict(anchor=bool(role.get('main_anchor')),prewarm=bool(role.get('prewarm')),seed=None,method='standard')
        anchor=preset['anchor'];prewarm=preset['prewarm']
        settings=row['next_preset'];self.validate_settings(row)
        for key in ('anchor','prewarm'):
            if settings[key]!='keep':
                if key=='anchor':anchor=settings[key]=='on'
                else:prewarm=settings[key]=='on'
        anchor_ref=self.validate_anchor_ref(did,row.get('anchor_ref'),deep=True) if anchor else None
        if anchor_ref and anchor:
            if prewarm and anchor_ref.get('prewarm') is None:raise ValueError('Alternativanker hat keinen passend gebundenen Prewarm')
        # A per-dialogue alternate reference is a complete, same-role input
        # binding. It may supply the fixed anchor/prewarm even when the role's
        # selected defaults are absent. With anchor off, anchor_ref is None and
        # the normal line-original/per-line-prewarm route remains available.
        if anchor and not (anchor_ref or role.get('main_anchor')):raise ValueError('Kein Stimmanker für Umschaltung verfügbar')
        if prewarm and not ((anchor_ref and anchor_ref.get('prewarm')) or role.get('prewarm')):raise ValueError('Kein Prewarm für Umschaltung verfügbar')
        role['without_anchor']=not bool(role.get('main_anchor'));role['without_prewarm']=not bool(role.get('prewarm'))
        d['render_overrides']=dict(anchor='use' if anchor else 'skip',prewarm='use' if prewarm else 'skip')
        if anchor_ref and anchor:
            d['render_overrides']['anchor_reference']=copy.deepcopy(anchor_ref['anchor'])
            if prewarm:
                d['render_overrides']['prewarm_reference']=copy.deepcopy(anchor_ref['prewarm'])
        for index,x in enumerate(m['dialogues']):
            if x['dialog_id']==did:m['dialogues'][index]=d;d['text_variants'].append(v);d['selected_variant_id']=v['variant_id']
        for index,x in enumerate(m['roles']):
            if x['role_id']==role['role_id']:m['roles'][index]=role
        mh=digest(m)
        if not fresh_seed and type(preset.get('seed')) is int:
            seed=preset['seed']
        elif settings['seed']=='keep' and not fresh_seed:
            raise ValueError('Kein belegter Synthese-Seed zum Beibehalten verfügbar')
        else:
            seed=secrets.randbelow(2**31)
            while seed==preset['seed']:seed=secrets.randbelow(2**31)
        short=(d['content_type']=='speech' and (d['original_audio'].get('duration_ms',0)<m['project_policy']['short_if_original_duration_below_ms'] or len(v['tts_text'].split())<m['project_policy']['short_if_tts_words_below']))
        render_method='without_emotion' if preset['method']=='main_anchor_only' and prewarm else preset['method']
        attempt=_attempt(d,v,role,render_method,seed,m,dict(short=short,manifest_sha256=mh))
        atomic_json(self.root/'jobs'/job_id/(digest(did)+'.manifest.json'),m)
        return attempt,m
    def start(self,p):
        with self.lock:
            if self._patch_builder is not None and self._patch_builder.status(validate_basis=False).get("job",{}).get("status")=="running":
                raise RuntimeError("Die gepatchte Fassung wird gerade erstellt; keine parallelen Render-/Exportaufträge.")
            request_id=p.get('request_id')
            if request_id is not None:
                if not isinstance(request_id,str) or not re.fullmatch(r'[a-f0-9]{64}',request_id):
                    raise ValueError('request_id muss ein SHA-256-Token sein')
                if self.state['job'].get('request_id')==request_id:
                    return dict(revision=self.state['revision'],job=copy.deepcopy(self.state['job']))
            self.check(p)
            if self.state['job']['status']=='running':raise RuntimeError('Ein Patchreview-Renderauftrag läuft bereits; keine Warteschlange.')
            availability=self.render_availability()
            if availability['busy']:raise RuntimeError(availability['reason'] or 'Renderer belegt; keine Warteschlange.')
            ids=p.get('dialog_ids');batch=p.get('backend')
            intent=p.get('intent','process')
            fresh_seed=p.get('fresh_seed',False)
            if type(fresh_seed) is not bool:raise ValueError('fresh_seed muss bool sein')
            if fresh_seed and intent!='audition':raise ValueError('fresh_seed ist nur für ein einzelnes Vorhören verfügbar')
            if intent not in ('audition','process'):raise ValueError('Unbekannter Startzweck')
            if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)) or batch not in (None,'gpu','ane','cpu'):raise ValueError('Ausgewählte IDs und gültiges Backend erforderlich')
            queue_job=p.get('audition_queue') is True
            if p.get('audition_queue') not in (None,False,True):raise ValueError('Ungültiger Wartelistenstart')
            if queue_job:
                if fresh_seed:raise ValueError('Die Vorschau-Warteliste verwendet den festgehaltenen Seed')
                if intent!='audition' or not getattr(self,'audition_queue_enabled',False):raise ValueError('Vorschau-Warteliste ist auf diesem Host nicht verfügbar')
                if ids!=self.state['audition_queue']:raise RuntimeError('Warteliste wurde geändert; neu laden')
                if not ids:raise ValueError('Die Vorschau-Warteliste ist leer')
                if any(self.dialogues.get(did,{}).get('content_type')!='speech' for did in ids):raise ValueError('Warteliste enthält einen ungültigen Sprachdialog')
            elif intent=='audition' and len(ids)!=1:raise ValueError('Vorhören benötigt genau einen Dialog')
            if fresh_seed and (len(ids)!=1 or queue_job):raise ValueError('fresh_seed benötigt ein einzelnes sofortiges Vorhören')
            from_candidate=p.get('from_candidate')
            if from_candidate is not None and (not isinstance(from_candidate,str) or len(ids)!=1):raise ValueError('from_candidate benötigt eine einzelne Dialog-ID und einen Kandidatenfingerprint')
            if any(did not in self.dialogues for did in ids):raise ValueError('Unbekannter Dialog')
            prepared_rows=None if queue_job else {did:self.row(did) for did in ids}
            if len(ids)>1 and not queue_job and any(not prepared_rows[did]['marked'] and not self.reusable_probe(did,prepared_rows[did]['backend'] or batch,row=prepared_rows[did]) for did in ids):raise ValueError('Ein Stapel darf nur vorgemerkte Dialoge oder passende freigegebene Proben enthalten')
            jid=uuid.uuid4().hex;requests=[]
            for did in ids:
                row=self.row(did) if queue_job else prepared_rows[did];backend=row['backend'] or batch
                if queue_job:
                    if row['generation_mode']!='synthesize':raise ValueError('Die Vorschau-Warteliste unterstützt nur neu zu synthetisierende Sprachdialoge')
                    if backend not in ('gpu','ane','cpu'):raise ValueError('Backend für Synthese erforderlich')
                    if backend=='cpu' and p.get('experimental_cpu') is not True:raise ValueError(CPU_WARNING)
                    requests.append((did,dict(_audition_queue_request=True,backend=backend),None,self.action_settings(row),None));continue
                saved=copy.deepcopy(self.saved(did))
                if not fresh_seed and saved['next_preset']['seed']=='fresh':
                    saved['next_preset']['seed']='keep';row['next_preset']['seed']='keep'
                    if saved['action_baseline']['next_preset']['seed']=='fresh':saved['action_baseline']['next_preset']['seed']='keep'
                    self.state['rows'][did]=saved
                baseline=self.action_settings(saved)
                if row['generation_mode']=='copy_original':
                    if intent=='audition':raise ValueError('Originalton verwendet den Originalplayer und benötigt keine Syntheseprobe')
                    original=self.original_output(did,deep=True)
                    if not original['verified'] or original['binding']!=saved['processing_binding']:raise ValueError('Gespeicherte Originaltonbindung fehlt oder stimmt nicht mit der Quelle überein')
                    requests.append((did,None,original['binding'],baseline,None));continue
                one_shot_seed=fresh_seed
                reused=self.reusable_probe(did,backend,row=row)
                if reused and not one_shot_seed:
                    requests.append((did,'reuse',reused,baseline,None));continue
                if self.executor is None:raise ValueError('Rendering ist für diesen Einstieg deaktiviert')
                if backend not in ('gpu','ane','cpu'):raise ValueError('Backend für Synthese erforderlich')
                if backend=='cpu' and p.get('experimental_cpu') is not True:raise ValueError(CPU_WARNING)
                if self.portable_recipe_for(did,row,backend) and from_candidate is None:
                    requests.append((did,dict(_recipe_request=True,backend=backend,row=copy.deepcopy(row),fresh_seed=one_shot_seed),None,baseline,None));continue
                current=None
                if from_candidate is not None:
                    if did!=ids[0] or row['generation_mode']!='synthesize':raise ValueError('Kandidatfortsetzung benötigt eine einzelne Synthesezeile')
                    source=next((c for c in saved['candidates'] if self.candidate_id(c)==from_candidate),None)
                    if source is None:raise ValueError('from_candidate verweist nicht auf einen Kandidaten dieses Dialogs')
                    current=self.candidate_output(source,deep=True)
                    if not current['verified'] or current['tts_text']!=row['tts_text'] or current['qa_expected_text']!=row['qa_expected_text']:
                        raise ValueError('from_candidate muss ein tief geprüfter Kandidat mit exakt passendem Dialogtext sein')
                else:
                    current=self.validate_output(did,row.get('current'))
                request=self.render_request(did,backend,row,current_override=current,from_candidate=from_candidate)
                a,m=self.make_attempt(did,backend,jid,row=row,current=current,fresh_seed=one_shot_seed)
                if one_shot_seed:request.update(seed_policy='fresh',seed_value=None)
                requests.append((did,a,m,baseline,request))
            self.state['job']=dict(id=jid,request_id=request_id,status='running',intent=intent,dialog_ids=ids,active_dialog_id=None,completed=[],reused=[],errors={},audition_queue=queue_job,progress=dict(phase='preparing',available=True,model_loaded=False,detail='Renderauftrag wird vorbereitet.'));self.state['revision']+=1;self.persist()
            self.thread=threading.Thread(target=self.run_job,args=(requests,),daemon=True);self.thread.start()
            return dict(revision=self.state['revision'],job=copy.deepcopy(self.state['job']))
    def _load_audition_prepared(self,did,action_sha,backend):
        """Load only a prior queue attempt whose saved row settings still match."""
        entry=self.state.get('audition_queue_prepared',{}).get(did)
        if not isinstance(entry,dict) or entry.get('action_sha256')!=action_sha:return None
        from manifest_renderer.audio import resolve_manifest_path
        try:
            jid=entry['job_id'];fingerprint=entry['fingerprint']
            manifest_path=resolve_manifest_path(self.root,f'jobs/{jid}/{digest(did)}.manifest.json')
            attempt_path=resolve_manifest_path(self.root,f'jobs/{jid}/attempts/{fingerprint}.json')
            m=json.loads(manifest_path.read_text(encoding='utf-8'));a=json.loads(attempt_path.read_text(encoding='utf-8'))
            if (a.get('fingerprint')!=fingerprint or a.get('dialog_id')!=did or
                    digest({k:v for k,v in a.items() if k not in {'fingerprint','artifacts'}})!=fingerprint or
                    a.get('render_execution',{}).get('profile_id')!=m.get('render_execution',{}).get('profile_id') or
                    a.get('render_execution',{}).get('profile_id')!=backend):return None
            row=self.saved(did)
            if a.get('target_text')!=row['tts_text'] or a.get('qa_expected_text')!=row['qa_expected_text']:return None
            return a,m,jid
        except (OSError,ValueError,KeyError,TypeError):return None
    def run_job(self,requests):
        if self.state.get('job',{}).get('audition_queue'):
            return self._run_audition_queue(requests)
        for did,a,m,baseline,request in requests:
            selection_changed=False
            try:
                if isinstance(a,dict) and a.get('_recipe_request'):
                    row=a['row'];backend=a['backend'];fresh_seed=bool(a.get('fresh_seed'))
                    with self.lock:
                        self.state['job']['active_dialog_id']=did
                        self.state['job']['progress']=dict(phase='preparing',available=True,model_loaded=False,detail='Stimmanker und Vorwärmen aus dem Rezept vorbereiten.')
                        self.state['revision']+=1;self.persist()
                    a,m=self.make_recipe_attempt(did,backend,self.state['job']['id'],row,fresh_seed=fresh_seed)
                    request=self.render_request(did,backend,row)
                    request.update(self.recipe_request(a,row,fresh_seed=fresh_seed))
                if a=='reuse':
                    selection_changed=self.state['job'].get('intent')=='process'
                    with self.lock:
                        self.state['job']['progress']=dict(phase='reusing',available=True,model_loaded=False,detail='Verwende die exakt passende, freigegebene lokale Probe.')
                        self.state['job']['active_dialog_id']=did;self.state['revision']+=1;self.persist()
                        if self.reusable_probe(did,m['effective_backend'])!=m:raise ValueError('Freigegebene Probe ist nicht mehr wiederverwendbar')
                        self.state['job']['reused'].append(did)
                elif a is None:
                    selection_changed=True
                    with self.lock:
                        original=self.original_output(did,deep=True)
                        if not original['verified'] or original['binding']!=m:raise ValueError('Originalton wurde nach Auftragsstart verändert')
                        if self.selection_callback and self.saved(did)['generation_mode']=='copy_original':self.selection_callback(did,'copy_original',m,m)
                        s=self.state['rows'].setdefault(did,self.saved(did));s['selection']=m;s['good_binding']=None;s['accepted_probe']=None
                        s['releases'].append(dict(binding=m,ignore_warnings=False,processing='copy_original',via='start',job_id=self.state['job']['id']))
                        if self.action_settings(s)==baseline and s['next_preset']['seed']=='keep':
                            s['next_preset']['seed']='fresh';baseline['next_preset']['seed']='fresh'
                else:
                    atomic_json(self.root/ m['output']['root']/'attempts'/(a['fingerprint']+'.json'),a)
                    receipt=dict(format='patch-review-request/1',fingerprint=a['fingerprint'],manifest_sha256=a['manifest_sha256'],source_manifest_sha256=self.evidence.manifest_hash,render_seed=a['seed'],intent=self.state['job']['intent'],request=request)
                    atomic_json(self.root/m['output']['root']/'review-requests'/(a['fingerprint']+'.json'),receipt)
                    bind_attempt(self.root,m,a,persist=True)
                    with self.lock:
                        self.state['job']['active_dialog_id']=did
                        self.state['job']['progress']=dict(phase='preparing',available=True,model_loaded=False,detail='Audioeingaben und Renderauftrag werden vorbereitet.')
                        self.state['revision']+=1;self.persist()
                    executor=self.executor.for_manifest(m,self.root) if hasattr(self.executor,'for_manifest') else self.executor
                    progress_capability=None
                    if hasattr(executor,'set_progress_callback'):
                        has_progress=executor.set_progress_callback(lambda phase,detail=None,did=did:self._render_progress(did,phase,detail))
                        progress_capability=has_progress if isinstance(has_progress,bool) else None
                        if has_progress is False:
                            with self.lock:
                                self.state['job']['progress']=dict(phase=None,available=False,model_loaded=None,detail='Der aktive ANE-Worker liefert in diesem Ablauf keine Live-Phasen; der Renderauftrag läuft weiterhin.')
                                self.state['revision']+=1;self.persist()
                    else:
                        with self.lock:
                            self.state['job']['progress']=dict(phase=None,available=False,model_loaded=None,detail='Dieser Executor stellt keine echten Live-Phasen bereit; der Renderauftrag läuft weiterhin.')
                            self.state['revision']+=1;self.persist()
                    try:record=AttemptStore(self.root).run(a,executor)
                    finally:
                        if hasattr(executor,'close'):executor.close()
                    overlay_error=None
                    if a.get('content_type')=='speech':
                        from .findings import word_findings
                        from .stt_overlay import write_candidate_medium_overlay
                        _,lexical_findings=word_findings(a['qa_expected_text'],record.get('transcript') or '',a.get('qa_allowed_variants',[])) if record.get('stt_status') in ('pass','warn') else ([],[])
                        if lexical_findings or record.get('qa_completion_status')=='deferred_manual_review':
                            try:write_candidate_medium_overlay(self.evidence.root,self.root,self.root/m['output']['root'],m,a,record)
                            except Exception as exc:overlay_error=f'Kandidat-WAV ist gespeichert; Medium-STT-Eskalation fehlgeschlagen: {exc}'
                    with self.lock:
                        self.state['job']['progress']=dict(phase='saving',available=True,model_loaded=None,detail='Render- und QA-Belege werden im Reviewzustand gespeichert.')
                        self.state['revision']+=1;self.persist()
                    with self.lock:
                        self.state['rows'].setdefault(did,self.saved(did))['candidates'].append(dict(attempt=a,record=record,manifest_sha256=digest(m),run_root=m['output']['root'],manifest_path=m['output']['root']+'/'+digest(did)+'.manifest.json',review_request_sha256=digest(receipt)))
                        if overlay_error:self.state['job']['errors'][did]=overlay_error
                with self.lock:
                    s=self.state['rows'][did];s['action_baseline']=baseline;s['marked']=False
                    self.persist()
                    if selection_changed:
                        self.write_refined_manifest([did]);self.write_selection([did])
            except Exception as exc:
                with self.lock:self.state['job']['errors'][did]=str(exc)
            if self.state['job'].get('intent')=='audition' and did in self.state['audition_queue'] and did not in self.state['job']['errors']:
                with self.lock:
                    self.state['audition_queue']=[value for value in self.state['audition_queue'] if value!=did]
                    self.state['audition_queue_prepared'].pop(did,None)
            with self.lock:
                if self.state['job'].get('active_dialog_id')==did:self.state['job']['active_dialog_id']=None
                self.state['job']['progress']=dict(phase=None,available=False,model_loaded=None,detail='Dialogauftrag abgeschlossen.')
                self.state['job']['completed'].append(did);self.state['revision']+=1;self.persist()
        with self.lock:self.state['job']['active_dialog_id']=None;self.state['job']['status']='failed' if self.state['job']['errors'] else 'completed';self.state['revision']+=1;self.persist()
    def _run_audition_queue(self,requests):
        staged=[]
        for did,a,m,baseline,request in requests:
            executor=None
            try:
                if isinstance(a,dict) and a.get('_audition_queue_request'):
                    backend=a['backend'];action_sha=digest(baseline)
                    prepared=self._load_audition_prepared(did,action_sha,backend)
                    if prepared:a,m,origin_job=prepared
                    else:
                        origin_job=self.state['job']['id'];row=self.row(did)
                        current=None if self.portable_recipe_for(did,row,backend) else self.validate_output(did,row.get('current'))
                        request=self.render_request(did,backend,row,current_override=current)
                        a,m=self.make_attempt(did,backend,origin_job,row=row,current=current)
                        if self.portable_recipe_for(did,row,backend):request.update(self.recipe_request(a,row))
                else:raise ValueError('Ungültiger Wartelistenauftrag')
                if a=='reuse' or a is None:raise ValueError('Wartelistenaufträge müssen echte Synthesen enthalten')
                root=m['output']['root'];job_dir=self.root/'jobs'/origin_job
                attempt_path=self.root/root/'attempts'/(a['fingerprint']+'.json')
                atomic_json(attempt_path,a)
                receipt=dict(format='patch-review-request/1',fingerprint=a['fingerprint'],manifest_sha256=a['manifest_sha256'],source_manifest_sha256=self.evidence.manifest_hash,render_seed=a['seed'],intent='audition',request=request)
                atomic_json(job_dir/'review-requests'/(a['fingerprint']+'.json'),receipt)
                bind_attempt(self.root,m,a,persist=True)
                with self.lock:
                    self.state['job']['active_dialog_id']=did
                    self.state['job']['progress']=dict(phase='preparing',available=True,model_loaded=False,detail=f'Vorschau-TTS wird vorbereitet · Dialog {did}.')
                    self.state['audition_queue_prepared'][did]=dict(job_id=origin_job,fingerprint=a['fingerprint'],action_sha256=digest(baseline))
                    self.state['revision']+=1;self.persist()
                # Probe the exact signed pending receipt with an audit-only
                # handle first. A resumed queue entry must not construct/load
                # the TTS executor just to discover that synthesis is done.
                audit_factory=getattr(self.executor,'audit_for_manifest',None)
                if audit_factory:
                    probe=audit_factory(m,self.root)
                    try:
                        already_staged=bool(getattr(probe,'resume_staged',None) and probe.resume_staged(a))
                    finally:
                        if hasattr(probe,'close'):probe.close()
                    executor=None
                else:
                    # Compatibility for model-free/test executors without an
                    # audit-only factory. Production SignedAdapter always has
                    # the lightweight path above.
                    executor=self.executor.for_manifest(m,self.root) if hasattr(self.executor,'for_manifest') else self.executor
                    already_staged=bool(getattr(executor,'resume_staged',None) and executor.resume_staged(a))
                if not already_staged:
                    if executor is None:
                        executor=self.executor.for_manifest(m,self.root) if hasattr(self.executor,'for_manifest') else self.executor
                    if hasattr(executor,'set_progress_callback'):
                        executor.set_progress_callback(lambda phase,detail=None,did=did:self._render_progress(did,phase,detail))
                    if not hasattr(executor,'stage'):raise ValueError('Executor unterstützt keine getrennte Vorschau-Synthese')
                    executor.stage(a)
                with self.lock:
                    self.state['job']['progress']=dict(phase='saving',available=True,model_loaded=None,detail=f'TTS gesichert · Dialog {did} · QA folgt nach dem TTS-Paket.')
                    self.state['revision']+=1;self.persist()
                staged.append((did,a['fingerprint'],origin_job,digest(baseline),digest(receipt)))
                close_stage=getattr(executor,'close',None)
                if close_stage:
                    try:close_stage(preserve_staged=True)
                    except TypeError:close_stage()
                executor=None
            except Exception as exc:
                if executor is not None and hasattr(executor,'close'):
                    try:executor.close()
                    except Exception:pass
                with self.lock:self.state['job']['errors'][did]=str(exc)
                self._finish_queue_dialog(did)
        release_error=None
        try:
            release=getattr(self,'release_review_tts',None)
            if release:release()
        except Exception as exc:release_error=exc
        self._audition_batch_tts_released=True
        try:
            for did,fingerprint,origin_job,action_sha,request_sha in staged:
                executor=None
                try:
                    from manifest_renderer.audio import resolve_manifest_path
                    manifest_path=resolve_manifest_path(self.root,f'jobs/{origin_job}/{digest(did)}.manifest.json')
                    attempt_path=resolve_manifest_path(self.root,f'jobs/{origin_job}/attempts/{fingerprint}.json')
                    request_path=resolve_manifest_path(self.root,f'jobs/{origin_job}/review-requests/{fingerprint}.json')
                    m=json.loads(manifest_path.read_text(encoding='utf-8'));a=json.loads(attempt_path.read_text(encoding='utf-8'));receipt=json.loads(request_path.read_text(encoding='utf-8'))
                    if a.get('fingerprint')!=fingerprint or a.get('dialog_id')!=did or digest(receipt)!=request_sha:
                        raise ValueError('Gespeicherter Wartelistenversuch wurde verändert')
                    if action_sha!=self.state.get('audition_queue_prepared',{}).get(did,{}).get('action_sha256'):
                        raise ValueError('Dialogeinstellungen haben sich seit der TTS-Vorbereitung geändert')
                    if action_sha!=digest(self.action_settings(self.saved(did))):
                        raise ValueError('Dialogeinstellungen wurden während des Wartelistenlaufs geändert; gespeicherte TTS wird nicht mit der neuen Fassung vermischt')
                    with self.lock:
                        self.state['job']['active_dialog_id']=did
                        self.state['job']['progress']=dict(phase='qa',available=True,model_loaded=None,detail=f'TTS-Paket abgeschlossen · TTS freigegeben · QA/STT · Dialog {did}.')
                        self.state['revision']+=1;self.persist()
                    if release_error:raise RuntimeError(f'TTS-Modell konnte vor der QA nicht freigegeben werden: {release_error}')
                    audit_factory=getattr(self.executor,'audit_for_manifest',None)
                    executor=audit_factory(m,self.root) if audit_factory else (self.executor.for_manifest(m,self.root) if hasattr(self.executor,'for_manifest') else self.executor)
                    if hasattr(executor,'set_progress_callback'):
                        executor.set_progress_callback(lambda phase,detail=None,did=did:self._render_progress(did,phase,detail))
                    record=AttemptStore(self.root).run(a,executor)
                    overlay_error=None
                    if a.get('content_type')=='speech':
                        from .findings import word_findings
                        from .stt_overlay import write_candidate_medium_overlay
                        _,findings=word_findings(a['qa_expected_text'],record.get('transcript') or '',a.get('qa_allowed_variants',[])) if record.get('stt_status') in ('pass','warn') else ([],[])
                        if findings or record.get('qa_completion_status')=='deferred_manual_review':
                            try:write_candidate_medium_overlay(self.evidence.root,self.root,self.root/m['output']['root'],m,a,record)
                            except Exception as exc:overlay_error=f'Kandidat-WAV ist gespeichert; Medium-STT-Eskalation fehlgeschlagen: {exc}'
                    with self.lock:
                        self.state['rows'].setdefault(did,self.saved(did))['candidates'].append(dict(attempt=a,record=record,manifest_sha256=digest(m),run_root=m['output']['root'],manifest_path=m['output']['root']+'/'+digest(did)+'.manifest.json',review_request_sha256=digest(receipt)))
                        if overlay_error:self.state['job']['errors'][did]=overlay_error
                        self._remove_successful_queue_item(did)
                        self.state['rows'][did]['action_baseline']=baseline
                        self.persist()
                except Exception as exc:
                    with self.lock:self.state['job']['errors'][did]=str(exc)
                finally:
                    if hasattr(executor,'close'):
                        try:executor.close()
                        except Exception as exc:
                            with self.lock:self.state['job']['errors'].setdefault(did,str(exc))
                    self._finish_queue_dialog(did)
        finally:
            self._audition_batch_tts_released=False
        with self.lock:
            self.state['job']['active_dialog_id']=None
            self.state['job']['status']='failed' if self.state['job']['errors'] else 'completed'
            self.state['job']['progress']=dict(phase=None,available=False,model_loaded=None,detail='Vorschau-Warteliste abgeschlossen. Erfolgreiche Einträge wurden entfernt; Fehler bleiben vorgemerkt.')
            self.state['revision']+=1;self.persist()
    def _remove_successful_queue_item(self,did):
        self.state['audition_queue']=[value for value in self.state['audition_queue'] if value!=did]
        self.state['audition_queue_prepared'].pop(did,None)
        self.state['job']['completed'].append(did);self.state['revision']+=1;self.persist()
    def _finish_queue_dialog(self,did):
        with self.lock:
            if self.state['job'].get('active_dialog_id')==did:self.state['job']['active_dialog_id']=None
            self.state['job']['progress']=dict(phase=None,available=False,model_loaded=None,detail=f'Vorschauauftrag abgeschlossen · Dialog {did}.')
            if did not in self.state['job']['completed']:self.state['job']['completed'].append(did)
            self.state['revision']+=1;self.persist()
    def _render_progress(self,did,phase,detail=None):
        allowed={'preparing','memory_check','loading_model','model_ready','rendering','saving','reusing','qa','qa_medium'}
        if phase not in allowed:return
        with self.lock:
            job=self.state['job']
            if job.get('status')!='running' or job.get('active_dialog_id')!=did:return
            advisory_changed=False
            if phase=='memory_check' and isinstance(detail,dict):
                advisory=copy.deepcopy(detail);advisory['dialog_id']=did
                advisory_changed=advisory!=job.get('memory_advisory')
                job['memory_advisory']=advisory
                detail=advisory.get('detail')
            loaded=True if phase in {'model_ready','rendering'} else False if phase in {'preparing','memory_check','loading_model','reusing'} else None
            value=dict(phase=phase,available=True,model_loaded=loaded,detail=detail or 'Fortschritt vom aktiven Produktionsrenderer.')
            if value!=job.get('progress') or advisory_changed:
                job['progress']=value;self.state['revision']+=1;self.persist()
    def status(self):
        with self.lock:return dict(read_only=self.launcher_readonly(),revision=self.state['revision'],job=copy.deepcopy(self.state['job']),patch_build=self.patch_builder().status(validate_basis=False),render_availability=self.render_availability(),audition_queue_enabled=bool(getattr(self,'audition_queue_enabled',False)),audition_queue=list(self.state['audition_queue']))
