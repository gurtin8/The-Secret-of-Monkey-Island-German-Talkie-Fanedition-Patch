"""Wizard review source projection and text refinement without external writes."""
from pathlib import Path
from .service import ReviewService,atomic_json,RULES
from .evidence import digest


def for_wizard(wizard,manifest_path=None,run_root=None):
    root=wizard.data_root/'patch-review'
    build_config=dict(game_root=getattr(wizard,'review_game_root',None),ffmpeg=getattr(wizard,'review_ffmpeg',None))
    def refine(did,text):
        sid=did.rsplit(':',1)[-1]
        wizard.save_review_tts(sid,text)
    def prepare(did,text):
        from voice_anchor_wizard.text_rules import prepare_text
        return prepare_text(text,wizard._effective_preparation_rules(wizard.state['text_prewizard'],did.rsplit(':',1)[-1]))
    def select(did,processing,binding,original_binding):
        wizard.save_review_processing(did.rsplit(':',1)[-1],processing,binding,original_binding)
    def availability():
        callback=getattr(wizard,'review_busy_callback',None)
        return callback() if callback else None
    draft_projection=not (manifest_path and run_root)
    def sync(service,dialog_ids=None):
        source=service.base.items() if dialog_ids is None else ((did,service.base[did]) for did in dialog_ids)
        for did,base in source:
            sid=base['speech_id']
            if sid not in wizard.dialogue_by_speech:continue
            live=wizard.effective_text(sid)
            base['display_text']=live['display_text']
            if draft_projection:base['original_text']=(wizard.dialogue_by_speech[sid].get('english_text_candidate') or {}).get('text','')
            if did not in service.state['rows']:
                base['tts_text']=live['tts_text'];base['qa_expected_text']=live['qa_expected_text']
            base['role_id']=wizard.effective_role_id(wizard.dialogue_by_speech[sid])
            base['role_name']=wizard.role_by_id.get(base['role_id'],{}).get('display_name',base.get('role_name',''))
    sync.supports_scoped_rows=True
    if manifest_path and run_root:
        executor=getattr(wizard,'review_executor',None)
        render_enabled=getattr(wizard,'review_render_enabled',False)
        service=ReviewService(manifest_path,wizard.dubbing_root,run_root,root,mode='wizard',executor=executor,text_callback=refine,sync_callback=sync,prepare_callback=prepare,selection_callback=select,processing_callback=select,availability_callback=availability,**build_config)
        if executor is None and render_enabled and service.evidence.valid:
            from .render import ProductionAdapter
            service.executor=ProductionAdapter(wizard.dubbing_root,ane_config=getattr(wizard,'review_ane_config',None))
        return service
    if getattr(wizard,'review_render_enabled',False):raise ValueError('Wizardreview-Rendering benötigt ein echtes Runmanifest und zugehörigen Runroot')
    # Demo/draft Wizard has no completed production run: expose actual original
    # sources and missing generated/STT state; never invent synthesis evidence.
    rows=[]
    for d in wizard.dialogues:
        sid=str(d['native_speech_id']);text=wizard.effective_text(sid);audio=d.get('original_audio',{})
        rows.append(dict(dialog_id=d.get('dialog_id','wizard:'+sid),role_id=wizard.effective_role_id(d),output_stem=sid,processing=wizard.effective_processing(d),original_audio=dict(path=audio.get('path',''),sha256=audio.get('sha256_from_extraction',audio.get('sha256'))),original_text=dict(text=(d.get('english_text_candidate') or d.get('original_text') or {}).get('text','')),selected_variant_id='draft:'+sid,text_variants=[dict(variant_id='draft:'+sid,variant_revision=1,display_text=text['display_text'],tts_text=text['tts_text'],qa_expected_text=text['qa_expected_text'],qa_allowed_variants=[])]))
    m=dict(manifest_id='wizard:'+wizard.source_sha256,manifest_revision=wizard.state['revision'],dialogues=rows,roles=[dict(role_id=r['role_id'],name=r.get('display_name',r.get('name',''))) for r in wizard.roles],project_policy=dict(review_method_rules=RULES))
    # Fresh draft projections include English source text. Preserve any older
    # immutable projection and its saved corrections rather than rewriting it.
    run=root/('draft-source-v2' if (root/'draft-source-v2').exists() or not (root/'state'/'review-state.json').exists() else 'draft-source');run.mkdir(parents=True,exist_ok=True)
    # Stable identity is the draft source; live text is synchronized on API reads.
    source_path=run/'source.json'
    if not source_path.exists():
        atomic_json(source_path,m);mh=digest(m);plan=dict(manifest_sha256=mh,dialogues=[]);plan['sha256']=digest(plan)
        atomic_json(run/'run-plan.json',plan);atomic_json(run/'render-report.json',dict(manifest_sha256=mh,run_plan_sha256=plan['sha256'],dialogues=[]))
    service=ReviewService(source_path,wizard.dubbing_root,run,root/'state',mode='wizard',text_callback=refine,sync_callback=sync,prepare_callback=prepare,selection_callback=select,processing_callback=select,**build_config)
    return service
