"""Immutable Medium STT overlay writer shared by Patchreview escalation stages."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

from audio_qa_v1 import audit
from audio_qa_v2 import rescore
from manifest_renderer.audio import resolve_manifest_path, sha256_file
from .evidence import Evidence, digest, read

POLICY_ID = 'patch-review-medium-overlay-v1'
_MODEL_HASH_CACHE={}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.stt-overlay-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _immutable_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.stt-receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        try: os.link(tmp, path)
        except FileExistsError: raise ValueError(f'Immutable STT overlay already exists: {path}')
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _stage_complete(record: dict) -> bool:
    return any(s.get('stt_model') == 'medium' and s.get('stt_status') in ('pass', 'warn')
               for s in record.get('stt_stages', []))


def write_candidate_medium_overlay(asset_root: Path, state_root: Path, run_root: Path,
                                   manifest: dict, attempt: dict, record: dict) -> dict:
    """Write/resume one completed candidate's Medium QA overlay.

    Call after its Base QA report exists and strict Patchreview word findings
    require escalation. Returns the immutable overlay receipt; it does not
    select/release the candidate or mutate its render report.
    """
    root=Path(asset_root).resolve(strict=True);state=Path(state_root).resolve(strict=True)
    run=Path(run_root).resolve(strict=True);jobs_root=state/'jobs'
    fp=attempt['fingerprint'];mh=digest(manifest)
    if (digest({k:v for k,v in attempt.items() if k not in ('fingerprint','artifacts')})!=fp or
            attempt.get('manifest_sha256')!=mh or record.get('fingerprint')!=fp or
            record.get('dialog_id')!=attempt.get('dialog_id') or
            record.get('qa_expected_text')!=attempt.get('qa_expected_text')):
        raise ValueError('Candidate attempt, manifest, text and QA report are not exactly bound')
    if record.get('content_type')!='speech' or _stage_complete(record):
        return {'status':'skipped','reason':'not_speech_or_medium_already_complete','record':record}
    complete=(record.get('stt_status') in ('pass','warn') and isinstance(record.get('transcript'),str))
    deferred=(record.get('qa_completion_status')=='deferred_manual_review' and record.get('stt_status')=='not_run')
    if not (complete or deferred):raise ValueError('Candidate has no escalatable completed/deferred STT input')
    files=manifest.get('models',{}).get('stt',{}).get('files',[])
    if len(files)<2:raise ValueError('Candidate manifest has no bound Medium model')
    model=resolve_manifest_path(root,files[1]['path'])
    if model.is_symlink() or not model.is_file():
        raise ValueError('Bound Medium model is missing or has a different SHA-256')
    stat=model.stat();key=(str(model),stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
    if _MODEL_HASH_CACHE.get(key)!=files[1]['sha256']:
        if sha256_file(model)!=files[1]['sha256']:raise ValueError('Bound Medium model is missing or has a different SHA-256')
        _MODEL_HASH_CACHE.clear();_MODEL_HASH_CACHE[key]=files[1]['sha256']
    cli_receipt=manifest.get('source_evidence',{}).get('production',{}).get('stt_cli',{})
    cli=resolve_manifest_path(root,cli_receipt.get('path',''))
    if cli.is_symlink() or not cli.is_file():raise ValueError('Bound Whisper CLI is unavailable')
    wav=resolve_manifest_path(state,attempt['artifacts']['wav'])
    wav_hash=record.get('wav_sha256')
    if wav.is_symlink() or not wav.is_file() or sha256_file(wav)!=wav_hash:
        raise ValueError('Candidate WAV is missing or differs from its QA hash')
    expected=attempt['qa_expected_text'];forms=[expected,*attempt.get('qa_allowed_variants',[])]
    decoder=record.get('decoder');qa_policy=record.get('qa_policy')
    if not isinstance(decoder,dict) or not isinstance(qa_policy,dict):
        raise ValueError('Candidate QA lacks its original decoder/policy binding')
    run_plan_path=run/'run-plan.json'
    if run_plan_path.exists() or not run.is_relative_to(jobs_root):
        raise ValueError('Candidate Medium overlays require a Patchreview jobs/<id> run without run-plan.json')
    run_plan_hash=None
    request_path=run/'review-requests'/(fp+'.json');request_sha256=None
    if request_path.exists():
        if request_path.is_symlink() or not request_path.is_file():raise ValueError('Candidate review request is invalid')
        request=read(request_path);request_sha256=digest(request)
        if (request.get('format')!='patch-review-request/1' or request.get('fingerprint')!=fp or
                request.get('manifest_sha256')!=mh or request.get('render_seed')!=attempt.get('seed')):
            raise ValueError('Candidate review request does not match its attempt')
    elif run.is_relative_to(jobs_root):
        raise ValueError('Candidate review request is missing')
    # AttemptStore returns a transient `resumed` flag, but that flag is not
    # part of the immutable report persisted to disk. Bind and rescore the
    # report projection so exported/re-hydrated candidates have the same hash.
    source_record={key:value for key,value in record.items() if key!='resumed'}
    binding={
        'format':'patch-review-stt-overlay-binding/1','policy_id':POLICY_ID,
        'source_run':str(run),'manifest_sha256':mh,'run_plan_sha256':run_plan_hash,
        'review_request_sha256':request_sha256,
        'attempt_fingerprint':fp,'dialog_id':attempt['dialog_id'],
        'source_record_sha256':digest(source_record),'wav_path':attempt['artifacts']['wav'],
        'wav_sha256':wav_hash,'wav_size_bytes':wav.stat().st_size,
        'qa_expected_text':expected,'accepted_forms':forms,'model_sha256':files[1]['sha256'],
        'decoder':decoder,'qa_policy':qa_policy,
    }
    out=Evidence.overlay_path(run,fp,root);lock_path=out.parent/'.writer.lock'
    allowed=root/'reviews'/'patch-review'/'stt-escalations'
    cursor=out
    while cursor!=root and cursor!=cursor.parent:
        if cursor.is_symlink():raise ValueError(f'Candidate overlay path contains a symlink: {cursor}')
        cursor=cursor.parent
    if cursor!=root or root.is_symlink():
        raise ValueError('Candidate overlay path is outside the fixed asset root')
    if not out.resolve(strict=False).is_relative_to(allowed.resolve(strict=False)):
        raise ValueError('Candidate overlay path escapes the fixed STT escalation directory')
    out.parent.mkdir(parents=True,exist_ok=True)
    if not out.resolve(strict=False).is_relative_to(allowed.resolve(strict=True)):
        raise ValueError('Resolved candidate overlay path escapes the fixed STT escalation directory')
    if lock_path.is_symlink():raise ValueError('Candidate overlay lock is a symlink')
    with lock_path.open('a',encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        try:
            if out.exists():
                if out.is_symlink():raise ValueError('Candidate overlay receipt is a symlink')
                previous=read(out)
                if previous.get('binding')!=binding or previous.get('record_sha256')!=digest(previous.get('record')):
                    raise ValueError('Existing candidate overlay is stale or bound differently')
                return previous
            started=time.perf_counter()
            heard=audit.run_stt(cli,model,wav,out.parent,int(decoder.get('threads',4)))
            compared=audit.compare(forms,heard)
            stage={
                'render_status':record.get('render_status','ok'),'content_type':record.get('content_type','speech'),
                'technical_status':record.get('technical_status'),'duration_status':record.get('duration_status'),
                'stt_status':'pass','qa_expected_text':expected,'accepted_forms':forms,'transcript':heard,
                'wav_sha256':wav_hash,'model_sha256':files[1]['sha256'],'stt_model':'medium',
                'decoder':decoder,'qa_policy':qa_policy,'stt_seconds':round(time.perf_counter()-started,6),
            }
            medium=rescore.rescore({**source_record,**stage,**compared})
            source_qa_state={key:source_record.get(key) for key in
                             ('qa_status','stt_status','qa_completion_status','qa_deferral_reason','manual_review_required')}
            if deferred:
                medium['qa_completion_status']='completed_with_patchreview_medium_overlay'
                medium['qa_deferral_reason']=None;medium['manual_review_required']=False
            receipt={'format':'patch-review-stt-overlay/1','binding':binding,
                     'source_qa_state':source_qa_state,'record':medium,'record_sha256':digest(medium)}
            _immutable_json(out,receipt)
            return receipt
        finally:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
