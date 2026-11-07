"""Render while one independent QA worker drains durable synthesis jobs."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time
import subprocess
import wave

from .runner import ArtifactError


def isolatable_failure(exc):
    """Dialogue faults only; control, resource and artifact errors remain fatal."""
    from .ane_worker import AneCancelled
    from .adaptive_ane import ResourceGuardError
    return (isinstance(exc, (RuntimeError, UnicodeError, subprocess.SubprocessError,
                             EOFError, wave.Error, BrokenPipeError, ChildProcessError, TimeoutError))
        and not isinstance(exc, (InterruptedError, MemoryError, ArtifactError, AneCancelled, ResourceGuardError)))


def restart_needed(exc):
    """A broken runtime may need a fresh launcher; file/contract repair is separate."""
    return isinstance(exc,(RuntimeError,MemoryError)) and not isinstance(exc,ArtifactError)


def _failed(failed_dialogue, state, attempt, stage, exc):
    if failed_dialogue is None or not isolatable_failure(exc):
        raise exc
    failed_dialogue(state["dialogue"], attempt, stage, exc, state["results"])



def _timed_call(timing, phase, worker, attempt, event_sink, fn, *args):
    event_prefix = "render" if phase.startswith("worker_") else "qa"
    details = {"backend": worker, "dialog_id": attempt.get("dialog_id"),
               "attempt_fingerprint": attempt.get("fingerprint"), "seed": attempt.get("seed"),
               "role_id": attempt.get("role_id"), "text": attempt.get("target_text")}
    started = time.monotonic()
    if event_sink:
        event_sink(event_prefix + "_started", **details)
    try:
        if timing is None:
            result = fn(*args)
        else:
            with timing.phase(phase, counts={"attempts": 1}, **details):
                result = fn(*args)
    except BaseException as exc:
        if event_sink:
            event_sink(event_prefix + "_error", **details,
                       duration_seconds=round(time.monotonic()-started,6),
                       error_type=type(exc).__name__, error=str(exc))
        raise
    outcome = {"duration_seconds":round(time.monotonic()-started,6), **details}
    if event_prefix == "qa" and isinstance(result,dict):
        outcome.update({key:result.get(key) for key in
                        ("qa_status","technical_status","reason_codes","wav_sha256","log_sha256","resumed")
                        if key in result})
    if event_sink:
        event_sink(event_prefix + "_finished", **outcome)
    return result


def _heartbeat(timing, started, *, workers, renders, qa_pending):
    now = time.monotonic()
    if now - started < 5:
        return started
    names = ", ".join(workers) if workers else "Worker"
    message = f"Noch keine neue Cue-Abnahme; {names} arbeitet oder lädt. TTS aktiv: {renders}, QA aktiv: {qa_pending}."
    print(message, flush=True)
    if timing:
        timing.event("worker_heartbeat", state="waiting",
                     counts={"active_render_workers": renders, "pending_qa": qa_pending},
                     workers=list(workers))
    return now


def _run_parallel(dialogues, store, executors, completed, progress, cancel, prepare_attempt, preferred_worker, timing, event_sink, first_pass=False, failed_dialogue=None):
    states = [{'dialogue': d, 'index': 0, 'results': []} for d in dialogues]
    ready = deque(s for s in states if s['dialogue']['processing'] == 'render')
    qa_pending = {}
    renders = {}
    audited = staged = 0
    worker_names = tuple(executors)
    available = set(worker_names)
    worker_pools = {name: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f'mi1-tts-{name}')
                    for name in worker_names}
    qa_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mi1-qa')
    rendered_ids = set()
    worker_rendered = {name: 0 for name in worker_names}
    failed = False
    last_heartbeat = time.monotonic()

    def update():
        progress({'rendered_ids': len(rendered_ids), 'rendered_attempts': staged,
                  'audited_attempts': audited, 'pending_qa_attempts': len(qa_pending),
                  'ready_tts_ids': len(ready), 'tts_status': 'rendering' if ready or renders else 'waiting_for_retry_results',
                  'workers': {name: {'active': int(any(details[0] == name for details in renders.values())),
                                     'rendered': worker_rendered[name]}
                              for name in worker_names}})

    def accepted(state, result):
        nonlocal audited
        dialogue = state['dialogue']
        state['results'].append(result); audited += 1
        if first_pass or result['qa_status'] in {'pass', 'warn'}:
            completed(dialogue, {'dialog_id': dialogue['dialog_id'], 'processing': 'render',
                      'selected': {'fingerprint': result['fingerprint'], 'qa_status': result['qa_status']},
                      'attempts': state['results']})
        else:
            state['index'] += 1
            if state['index'] < len(dialogue['attempts']):
                ready.append(state)
            else:
                completed(dialogue, {'dialog_id': dialogue['dialog_id'], 'processing': 'render',
                          'selected': None, 'qa_status': 'fail', 'attempts': state['results']})

    try:
        for state in states:
            d = state['dialogue']
            if d['processing'] == 'copy_original':
                completed(d, {'dialog_id': d['dialog_id'], 'processing': 'copy_original',
                              'selected': {'source_audio': d['source_audio']}, 'attempts': []})
        update()
        while ready or renders or qa_pending:
            for future in list(renders):
                if future.done():
                    name, state, attempt = renders.pop(future)
                    available.add(name)
                    try:
                        future.result()
                    except Exception as exc:
                        _failed(failed_dialogue, state, attempt, 'render', exc)
                        update()
                        continue
                    staged += 1; rendered_ids.add(state['dialogue']['dialog_id']); worker_rendered[name] += 1
                    qa = qa_pool.submit(_timed_call, timing, 'qa', name, attempt, event_sink,
                                        store.run, attempt, executors[name].audit_pending)
                    qa_pending[qa] = (state, attempt)
                    update()
            for future in list(qa_pending):
                if future.done():
                    state, attempt = qa_pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        _failed(failed_dialogue, state, attempt, 'qa', exc)
                    else:
                        accepted(state, result)
                    update()
            while ready and available:
                choice = None
                for state in ready:
                    raw = state['dialogue']['attempts'][state['index']]
                    preferred = preferred_worker(raw) if preferred_worker else None
                    if preferred is not None:
                        if preferred in available:
                            choice = (state, preferred)
                            break
                        continue
                    choice = (state, next(name for name in worker_names if name in available))
                    break
                if choice is None:
                    break
                state, name = choice
                raw = state['dialogue']['attempts'][state['index']]
                attempt = prepare_attempt(raw, name)
                if attempt['render_execution']['profile_id'] != name:
                    raise RuntimeError('Backend assignment changed during dispatch')
                ready.remove(state)
                existing = store.cached(attempt)
                if existing is not None:
                    rendered_ids.add(state['dialogue']['dialog_id']); staged += 1; worker_rendered[name] += 1
                    accepted(state, existing)
                    continue
                available.remove(name)
                future = worker_pools[name].submit(_timed_call, timing,
                    'worker_model_load_and_render_request', name, attempt,
                    event_sink, executors[name].stage, attempt)
                renders[future] = (name, state, attempt)
            update()
            if renders:
                wait(renders, timeout=.25, return_when=FIRST_COMPLETED)
                last_heartbeat = _heartbeat(timing,last_heartbeat,
                    workers=[details[0] for details in renders.values()],renders=len(renders),qa_pending=len(qa_pending))
            elif qa_pending:
                wait(qa_pending, timeout=.25, return_when=FIRST_COMPLETED)
                last_heartbeat = _heartbeat(timing,last_heartbeat,
                    workers=[],renders=0,qa_pending=len(qa_pending))
    except BaseException:
        failed = True
        cancel()
        for future in renders:
            future.cancel()
        for future in qa_pending:
            future.cancel()
        raise
    finally:
        for pool in worker_pools.values():
            pool.shutdown(wait=True, cancel_futures=True)
        qa_pool.shutdown(wait=True, cancel_futures=True)


def _run_parallel_characters(dialogues, store, executors, completed, progress, cancel,
                             prepare_attempt, character_bindings, guybrush_role_id, timing, event_sink, first_pass=False, failed_dialogue=None,
                             benchmark_policy=lambda: {}):
    """Run one whole manifest role at a time on each worker; keep its retries pinned."""
    states = [{'dialogue': d, 'index': 0, 'results': [], 'role': d.get('role_id') or d['dialog_id'],
               'finished': False} for d in dialogues]
    render_states = [s for s in states if s['dialogue']['processing'] == 'render']
    role_order = list(dict.fromkeys(s['role'] for s in render_states))
    if guybrush_role_id in role_order:
        role_order.remove(guybrush_role_id); role_order.insert(0, guybrush_role_id)
    by_role = {role: [s for s in render_states if s['role'] == role] for role in role_order}
    copies = [s for s in states if s['dialogue']['processing'] == 'copy_original']
    for state in copies:
        d = state['dialogue']
        completed(d, {'dialog_id': d['dialog_id'], 'processing': 'copy_original',
                      'selected': {'source_audio': d['source_audio']}, 'attempts': []})
    worker_names = tuple(executors)
    if len(worker_names) != 2 or set(worker_names) != {'gpu', 'ane'}:
        raise ValueError('Figurenrouting verlangt genau GPU und ANE')
    available = set(worker_names)
    active = {}
    active_roles = set()
    role_status = {role: {'next': 0, 'finished': 0} for role in role_order}
    qa_pending, renders = {}, {}
    pools = {name: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f'mi1-tts-{name}')
             for name in worker_names}
    qa_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mi1-qa')
    staged = audited = 0
    rendered_ids = set()
    worker_rendered = {name: 0 for name in worker_names}
    last_heartbeat = time.monotonic()

    def mark_finished(state):
        role_status[state['role']]['finished'] += 1
        if role_status[state['role']]['finished'] == len(by_role[state['role']]):
            worker = active.pop(state['role'], None)
            if worker is not None:
                active_roles.discard(state['role'])
                available.add(worker)

    def update():
        progress({'rendered_ids': len(rendered_ids), 'rendered_attempts': staged,
                  'audited_attempts': audited, 'pending_qa_attempts': len(qa_pending),
                  'ready_tts_ids': sum(1 for role in active_roles
                      for state in by_role[role] if not state['finished'] and
                      state['index'] < len(state['dialogue']['attempts'])),
                  'tts_status': 'rendering' if active or renders else 'waiting_for_retry_results',
                  'workers': {name: {'active': int(any(details[0] == name for details in renders.values())),
                                     'role_id': next((role for role, owner in active.items() if owner == name), None),
                                     'rendered': worker_rendered[name]} for name in worker_names}})

    def accepted(state, result):
        nonlocal audited
        audited += 1
        state['results'].append(result)
        if first_pass or result['qa_status'] in {'pass', 'warn'}:
            completed(state['dialogue'], {'dialog_id': state['dialogue']['dialog_id'], 'processing': 'render',
                      'selected': {'fingerprint': result['fingerprint'], 'qa_status': result['qa_status']},
                      'attempts': state['results']})
            state['finished'] = True
            mark_finished(state)
        else:
            state['index'] += 1
            if state['index'] >= len(state['dialogue']['attempts']):
                completed(state['dialogue'], {'dialog_id': state['dialogue']['dialog_id'], 'processing': 'render',
                          'selected': None, 'qa_status': 'fail', 'attempts': state['results']})
                state['finished'] = True
                mark_finished(state)

    def allocate(worker):
        # Resume assignments take precedence. The initial unbound assignment reserves GPU for Guybrush.
        for role in role_order:
            if role in active_roles or role_status[role]['finished'] >= len(by_role[role]):
                continue
            bound = character_bindings.get(role)
            if bound == worker:
                active[role] = worker; active_roles.add(role); available.discard(worker)
                return
        for role in role_order:
            if (role in active_roles or role in character_bindings or
                    role_status[role]['finished'] >= len(by_role[role])):
                continue
            if role == guybrush_role_id and worker != 'gpu':
                continue
            preferred = benchmark_policy().get('preferred_backend')
            if preferred and worker != preferred and role != guybrush_role_id:
                continue
            if role != guybrush_role_id and guybrush_role_id in role_order and guybrush_role_id not in character_bindings and 'gpu' in available:
                continue
            # Stable role order; after Guybrush, the first free worker takes the next whole role.
            character_bindings[role] = worker
            active[role] = worker; active_roles.add(role); available.discard(worker)
            return

    try:
        update()
        while any(role_status[role]['finished'] < len(by_role[role]) for role in role_order) or renders or qa_pending:
            for future in list(renders):
                if future.done():
                    worker, state, attempt = renders.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        _failed(failed_dialogue, state, attempt, 'render', exc)
                        state['finished'] = True
                        mark_finished(state)
                        update()
                        continue
                    staged += 1; rendered_ids.add(state['dialogue']['dialog_id']); worker_rendered[worker] += 1
                    qa = qa_pool.submit(_timed_call, timing, 'qa', worker, attempt, event_sink,
                                        store.run, attempt, executors[worker].audit_pending)
                    qa_pending[qa] = (state, worker, attempt)
                    update()
            for future in list(qa_pending):
                if future.done():
                    state, worker, attempt = qa_pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        _failed(failed_dialogue, state, attempt, 'qa', exc)
                        state['finished'] = True
                        mark_finished(state)
                    else:
                        accepted(state, result)
                    update()
            for worker in worker_names:
                if worker in available:
                    allocate(worker)
            launched = False
            for worker in worker_names:
                role = next((role for role, owner in active.items() if owner == worker), None)
                if role is None or any(details[0] == worker for details in renders.values()):
                    continue
                state = next((s for s in by_role[role] if not s['finished'] and
                              s['index'] < len(s['dialogue']['attempts']) and
                              not any(pstate is s for pstate, _, _ in qa_pending.values()) and
                              not any(details[1] is s for details in renders.values())), None)
                if state is None:
                    continue
                raw = state['dialogue']['attempts'][state['index']]
                attempt = prepare_attempt(raw, worker)
                existing = store.cached(attempt)
                if existing is not None:
                    rendered_ids.add(state['dialogue']['dialog_id']); staged += 1; worker_rendered[worker] += 1
                    accepted(state, existing)
                else:
                    future = pools[worker].submit(_timed_call, timing,
                        'worker_model_load_and_render_request', worker, attempt,
                        event_sink, executors[worker].stage, attempt)
                    renders[future] = (worker, state, attempt)
                launched = True
                update()
            if renders:
                wait(renders, timeout=.25, return_when=FIRST_COMPLETED)
                last_heartbeat = _heartbeat(timing,last_heartbeat,
                    workers=[details[0] for details in renders.values()],renders=len(renders),qa_pending=len(qa_pending))
            elif qa_pending:
                wait(qa_pending, timeout=.25, return_when=FIRST_COMPLETED)
                last_heartbeat = _heartbeat(timing,last_heartbeat,
                    workers=[],renders=0,qa_pending=len(qa_pending))
            elif not launched and not active and not available:
                raise RuntimeError('Figurenqueue blieb ohne freien Worker stehen')
            elif (not launched and not active and available and all(role in character_bindings for role in role_order)
                  and any(role_status[role]['finished'] < len(by_role[role]) for role in role_order)):
                raise RuntimeError('Figurenbindungen lassen keinen fortsetzbaren Worker zu')
            elif not launched and active and not renders and not qa_pending:
                # Active figure can be waiting for QA result already delivered; schedule the next cue.
                continue
    except BaseException:
        cancel()
        for future in renders: future.cancel()
        for future in qa_pending: future.cancel()
        raise
    finally:
        for pool in pools.values(): pool.shutdown(wait=True, cancel_futures=True)
        qa_pool.shutdown(wait=True, cancel_futures=True)


def _run_staged_first_pass(dialogues, store, executor, completed, progress, cancel,
                           prepare_attempt, single_worker, timing, event_sink,
                           failed_dialogue, release_tts, character_routing, guybrush_role_id):
    """Keep only durable attempt metadata until all synthesis has finished."""
    states = [{'dialogue': d, 'results': []} for d in dialogues]
    if character_routing and guybrush_role_id:
        states.sort(key=lambda s: s['dialogue'].get('role_id') != guybrush_role_id)
    ready = deque(s for s in states if s['dialogue']['processing'] == 'render')
    pending = deque()
    rendered = audited = 0

    def update(qa=False):
        progress({'rendered_ids': rendered, 'rendered_attempts': rendered,
                  'audited_attempts': audited, 'pending_qa_attempts': len(pending),
                  'ready_tts_ids': len(ready), 'tts_status': 'complete' if qa else 'rendering',
                  'qa_status': ('auditing' if pending else 'complete') if qa else 'waiting_for_tts'})

    def accepted(state, result):
        nonlocal audited
        audited += 1
        completed(state['dialogue'], {'dialog_id': state['dialogue']['dialog_id'],
            'processing': 'render', 'selected': {'fingerprint': result['fingerprint'],
            'qa_status': result['qa_status']}, 'attempts': [result]})

    try:
        for state in states:
            d = state['dialogue']
            if d['processing'] == 'copy_original':
                completed(d, {'dialog_id': d['dialog_id'], 'processing': 'copy_original',
                             'selected': {'source_audio': d['source_audio']}, 'attempts': []})
        update()
        while ready:
            state = ready.popleft()
            attempt = state['dialogue']['attempts'][0]
            if prepare_attempt: attempt = prepare_attempt(attempt, single_worker)
            existing = store.cached(attempt)
            if existing is not None:
                rendered += 1
                accepted(state, existing)
            else:
                try:
                    _timed_call(timing, 'worker_model_load_and_render_request', single_worker,
                                attempt, event_sink, executor.stage, attempt)
                except Exception as exc:
                    _failed(failed_dialogue, state, attempt, 'render', exc)
                else:
                    rendered += 1
                    pending.append((state, attempt))
            update()
        release_tts()
        update(qa=True)
        while pending:
            state, attempt = pending[0]
            try:
                result = _timed_call(timing, 'qa', single_worker, attempt, event_sink,
                                     store.run, attempt, executor.audit_pending)
            except Exception as exc:
                _failed(failed_dialogue, state, attempt, 'qa', exc)
            else:
                accepted(state, result)
            pending.popleft()
            update(qa=True)
    except BaseException:
        cancel()
        raise


def run_pipeline(dialogues, store, executor, completed, progress, cancel=lambda: None,
                 prepare_attempt=None, preferred_worker=None, serialize_tts_qa=False,
                 character_routing=False, character_bindings=None, guybrush_role_id=None,
                 timing=None, single_worker='single', event_sink=None, first_pass=False, failed_dialogue=None,
                 defer_qa_until_tts_complete=False, release_tts=None, benchmark_policy=lambda: {}):
    if defer_qa_until_tts_complete:
        if not first_pass or isinstance(executor, dict) or release_tts is None:
            raise ValueError('Deferred QA requires a single-worker first pass and TTS cleanup')
        return _run_staged_first_pass(dialogues, store, executor, completed, progress, cancel,
            prepare_attempt, single_worker, timing, event_sink, failed_dialogue, release_tts,
            character_routing, guybrush_role_id)
    if not first_pass:
        failed_dialogue = None
    if isinstance(executor, dict):
        if character_routing:
            return _run_parallel_characters(dialogues, store, executor, completed, progress, cancel,
                                            prepare_attempt, character_bindings or {}, guybrush_role_id,timing,event_sink,first_pass,failed_dialogue,
                                            benchmark_policy)
        return _run_parallel(dialogues, store, executor, completed, progress, cancel, prepare_attempt, preferred_worker,timing,event_sink,first_pass,failed_dialogue)

    states=[{'dialogue':d,'index':0,'results':[]} for d in dialogues]
    if character_routing and guybrush_role_id:
        states.sort(key=lambda state: (state['dialogue'].get('role_id') != guybrush_role_id,))
    ready=deque(s for s in states if s['dialogue']['processing']=='render')
    pending={};rendered_ids=set();audited=0;staged=0
    worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix='mi1-qa')
    failed=False
    last_heartbeat=time.monotonic()

    def update():
        progress({'rendered_ids':len(rendered_ids),'rendered_attempts':staged,
                  'audited_attempts':audited,'pending_qa_attempts':len(pending),
                  'ready_tts_ids':len(ready),'tts_status':'rendering' if ready else 'waiting_for_retry_results'})

    def accepted(state,result):
        nonlocal audited
        dialogue=state['dialogue'];state['results'].append(result);audited+=1
        if first_pass or result['qa_status'] in {'pass','warn'}:
            completed(dialogue,{'dialog_id':dialogue['dialog_id'],'processing':'render',
                      'selected':{'fingerprint':result['fingerprint'],'qa_status':result['qa_status']},
                      'attempts':state['results']})
        else:
            state['index']+=1
            if state['index']<len(dialogue['attempts']):ready.append(state)
            else:
                completed(dialogue,{'dialog_id':dialogue['dialog_id'],'processing':'render',
                          'selected':None,'qa_status':'fail','attempts':state['results']})

    try:
        for state in states:
            d=state['dialogue']
            if d['processing']=='copy_original':
                completed(d,{'dialog_id':d['dialog_id'],'processing':'copy_original',
                             'selected':{'source_audio':d['source_audio']},'attempts':[]})
        update()
        while ready or pending:
            for future in list(pending):
                if future.done():
                    state, attempt = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        _failed(failed_dialogue, state, attempt, 'qa', exc)
                    else:
                        accepted(state, result)
                    update()
            if ready:
                state=ready.popleft();d=state['dialogue'];attempt=d['attempts'][state['index']]
                if prepare_attempt: attempt=prepare_attempt(attempt,single_worker)
                existing=store.cached(attempt)
                if existing is not None:
                    rendered_ids.add(d['dialog_id']);accepted(state,existing)
                else:
                    try:
                        _timed_call(timing,'worker_model_load_and_render_request',single_worker,attempt,event_sink,
                                    executor.stage,attempt)
                    except Exception as exc:
                        _failed(failed_dialogue, state, attempt, 'render', exc)
                        update()
                        continue
                    staged+=1;rendered_ids.add(d['dialog_id'])
                    if serialize_tts_qa:
                        executor.release_ane_worker()
                        try:
                            result = _timed_call(timing,'qa',single_worker,attempt,event_sink,
                                                 store.run,attempt,executor.audit_pending)
                        except Exception as exc:
                            _failed(failed_dialogue, state, attempt, 'qa', exc)
                        else:
                            accepted(state,result)
                    else:
                        future=worker.submit(_timed_call,timing,'qa',single_worker,attempt,event_sink,
                                             store.run,attempt,executor.audit_pending)
                        pending[future]=(state, attempt)
                update()
            elif pending:
                wait(pending,timeout=.25,return_when=FIRST_COMPLETED)
                last_heartbeat=_heartbeat(timing,last_heartbeat,workers=[single_worker],renders=0,qa_pending=len(pending))
    except BaseException:
        failed=True
        cancel()
        for future in pending:future.cancel()
        raise
    finally:
        worker.shutdown(wait=True,cancel_futures=True)
