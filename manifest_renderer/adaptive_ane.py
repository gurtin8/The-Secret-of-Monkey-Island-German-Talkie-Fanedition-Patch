"""Conservative ANE instance admission; no inferred ANE utilization counters."""
from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import ctypes
import os
import platform
import re
import subprocess
import threading
import time

from .hardware import GIB, ram_snapshot

POLICY = {'version': 'adaptive-ane-v2', 'maximum_instances': 4,
          'stable_seconds': 30, 'window_seconds': 60, 'cooldown_seconds': 120,
          'minimum_gain': 1.10, 'sample_interval_seconds': 2, 'stale_seconds': 6,
          'maximum_qa_backlog': 2}


def effective_policy(facts):
    """Bind the admission ceiling to physical RAM before run identity is written."""
    policy = dict(POLICY)
    ram = facts.get('system_ram_bytes')
    if isinstance(ram, int) and ram > 8 * GIB:
        policy.update(version='adaptive-ane-v4', maximum_instances=5, maximum_qa_backlog=10,
                      throughput_basis='tts', admission_basis='resources',
                      initial_probe_budget_bytes=8*GIB, role_dispatch='shared-ane',
                      stable_seconds=0, admission_interval_seconds=0,
                      reserve_fraction=.15, startup_plan='sequential-model-ready')
    return policy


def validate_policy(policy):
    """Accept only shipped policies, including existing four-instance resumes."""
    if policy == POLICY or policy == effective_policy({'system_ram_bytes':64*GIB}):
        return
    if policy == {**POLICY,'version':'adaptive-ane-v3','maximum_instances':5,'maximum_qa_backlog':10,
                  'throughput_basis':'tts','admission_basis':'resources',
                  'initial_probe_budget_bytes':8*GIB,'role_dispatch':'shared-ane'}:
        return
    if policy == {**POLICY,'maximum_instances':5,'maximum_qa_backlog':10,'throughput_basis':'tts'}:
        return
    if policy == {**POLICY, 'version': 'adaptive-ane-v1'}:
        return
    raise ValueError('Adaptive ANE policy differs from the supported resume contract')


class ResourceGuardError(RuntimeError):
    pass


def telemetry():
    """Unknown OS signals remain unknown and prevent expansion."""
    sample = ram_snapshot()
    sample.update(memory_pressure=None, thermal_state=None, swap_used_bytes=None,
                  swapouts=None, cpu_load=None)
    if platform.system() != 'Darwin':
        return sample
    def output(command):
        try:
            return subprocess.run(command, capture_output=True, text=True,
                                  timeout=1, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return ''
    pressure = output(['sysctl', '-n', 'kern.memorystatus_vm_pressure_level']).strip()
    sample['memory_pressure'] = {'1': 'normal', '2': 'warning', '4': 'critical'}.get(pressure)
    swap = output(['sysctl', '-n', 'vm.swapusage'])
    match = re.search(r'used\s*=\s*([\d.]+)([KMG])', swap)
    if match:
        sample['swap_used_bytes'] = int(float(match[1]) * {'K': 1024, 'M': 1024**2, 'G': GIB}[match[2]])
    vm = output(['vm_stat'])
    match = re.search(r'^Swapouts:\s*(\d+)\.', vm, re.M)
    if match: sample['swapouts'] = int(match[1])
    try:
        sample['cpu_load'] = os.getloadavg()[0] / (os.cpu_count() or 1)
        # Foundation ProcessInfo.thermalState is an OS enum, not a guessed temperature.
        ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
        objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        objc.objc_getClass.argtypes = [ctypes.c_char_p]; objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]; objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]; objc.objc_msgSend.restype = ctypes.c_void_p
        info = objc.objc_msgSend(objc.objc_getClass(b'NSProcessInfo'), objc.sel_registerName(b'processInfo'))
        value = objc.objc_msgSend(info, objc.sel_registerName(b'thermalState'))
        sample['thermal_state'] = {0: 'nominal', 1: 'fair', 2: 'serious', 3: 'critical'}.get(value or 0)
    except (OSError, AttributeError, ValueError):
        pass
    return sample


class Controller:
    def __init__(self, *, policy=None):
        self.policy = dict(POLICY if policy is None else policy)
        if not 1 <= self.policy['maximum_instances'] <= 5:
            raise ValueError('Adaptive ANE maximum must be between one and five')
        self.stable_since = None; self.last_change = None; self.cooldown_until = 0
        self.peak_bytes = 0; self.minimum_available = None; self.baseline_available = None
        self.previous = None; self.swap_growth_samples = 0
        self.observations = []; self.baseline = None; self.probing = False
        self.tts_observations = []; self.baseline_quality = None
        self.last_sample = {}; self.reason = 'warming_first_instance'
        self.warm_cutoff = 0
        self.previous_count = 1

    def completed(self, now, attempt, result, latency=None):
        # Compare like methods/text-length bins, including audited audio per wall time.
        bucket = (attempt.get('method'), len(attempt.get('target_text', '').split()) // 10)
        seconds = result.get('output_duration_ms', 0) / 1000
        self.observations.append((now, bucket, seconds, result.get('qa_status'), latency))
        self.observations = [row for row in self.observations if row[0] >= now - 180]

    def model_loaded(self, now):
        self.warm_cutoff=now
        if self.policy.get('admission_basis') != 'resources':
            self.last_change=now

    def synthesized(self, now, attempt, result, latency):
        # Staged audio is unreviewed. It never supplies a QA pass or failure.
        if (not isinstance(result,dict) or result.get('synthesis_resumed_before_qa') or
                result.get('execution',{}).get('cold_start') or
                result.get('output_duration_ms',0) <= 0 or latency is None):
            return
        bucket = (attempt.get('method'), len(attempt.get('target_text','').split()) // 10)
        self.tts_observations.append((now,bucket,result['output_duration_ms']/1000,None,latency))
        self.tts_observations = [row for row in self.tts_observations if row[0] >= now-180]

    def window(self, now, *, basis='qa'):
        observations = self.tts_observations if basis=='tts' else self.observations
        rows = [row for row in observations if max(self.warm_cutoff,now-self.policy['window_seconds']) <= row[0] <= now]
        if len(rows) < 3 or (basis=='qa' and any(row[3] not in {'pass', 'warn', 'fail'} for row in rows)): return None
        distribution = Counter(row[1] for row in rows)
        total = len(rows)
        return {('rendered_per_second' if basis=='tts' else 'checked_per_second'): total/self.policy['window_seconds'],
                'audio_per_second': sum(row[2] for row in rows)/self.policy['window_seconds'],
                'latency_seconds':sum(row[4] for row in rows)/total if all(row[4] is not None for row in rows) else None,
                **({'qa_failure_rate':sum(row[3]=='fail' for row in rows)/total} if basis=='qa' else {}),
                'mix': {key: value/total for key, value in distribution.items()}}

    def additional_worker_budget(self):
        # A bootstrap estimate is used only until system loss gives a measured peak.
        return self.peak_bytes or self.policy.get('initial_probe_budget_bytes',0)

    def reserve_for(self,total):
        if not total:return None
        if 'reserve_fraction' in self.policy:return int(self.policy['reserve_fraction']*total)
        return max(2*GIB,int(.2*total))

    def decide(self, sample, now, count, *, qa_backlog=0, paused=False):
        self.last_sample = dict(sample)
        available = sample.get('available_ram_estimate_bytes'); total = sample.get('system_ram_bytes')
        if available is not None:
            if count < self.previous_count:
                self.baseline_available=available;self.minimum_available=available
            if self.baseline_available is None: self.baseline_available = available
            self.minimum_available = min(available, self.minimum_available if self.minimum_available is not None else available)
            # Whole-system loss includes Core ML services, loading, QA and other apps.
            self.peak_bytes = max(self.peak_bytes, self.baseline_available-self.minimum_available)
        self.previous_count=count
        if self.previous is not None:
            growing = (sample.get('swapouts') is not None and self.previous.get('swapouts') is not None and
                       sample['swapouts'] > self.previous['swapouts']) or (
                       sample.get('swap_used_bytes') is not None and self.previous.get('swap_used_bytes') is not None and
                       sample['swap_used_bytes'] > self.previous['swap_used_bytes'])
            self.swap_growth_samples = self.swap_growth_samples+1 if growing else 0
        self.previous = dict(sample)
        reserve = self.reserve_for(total)
        self.reserve_bytes = reserve
        critical = sample.get('memory_pressure') == 'critical' or sample.get('thermal_state') == 'critical'
        if critical:
            self.reason = 'resource_guard_critical'; return 'stop'
        if paused:
            self.stable_since = None; self.reason = 'user_paused'; return 'hold'
        known = all(sample.get(key) is not None for key in ('memory_pressure', 'thermal_state',
                    'swap_used_bytes', 'swapouts', 'cpu_load', 'available_ram_estimate_bytes', 'system_ram_bytes'))
        stale = now-sample.get('measured_at', 0) > self.policy['stale_seconds']
        reason = ('stale_telemetry' if stale else 'unknown_telemetry' if not known else
                  'ram_reserve' if available < reserve else
                  'memory_pressure' if sample['memory_pressure'] != 'normal' else
                  'thermal_pressure' if sample['thermal_state'] not in {'nominal','fair'} else
                  'swap_growth' if self.swap_growth_samples >= 2 else
                  'qa_backlog' if qa_backlog >= self.policy['maximum_qa_backlog'] else
                  'cpu_saturation' if sample['cpu_load'] >= .9 else None)
        if reason:
            self.reason = reason
            # Queue depth and CPU load throttle admissions, not healthy models.
            if self.policy.get('admission_basis') == 'resources' and reason in {'qa_backlog','cpu_saturation'}:
                return 'hold'
            self.stable_since = None
            if count > 1:
                self.cooldown_until = now+self.policy['cooldown_seconds']; self.probing = False
                return 'retire'
            return 'hold'
        if self.stable_since is None: self.stable_since = now
        if self.last_change is None: self.last_change = now
        self.reason = ('resource_cooldown' if now < self.cooldown_until else 'resource_stability_pending') if self.policy.get('admission_basis') == 'resources' else 'stable_window_pending'
        if now < self.cooldown_until or now-self.stable_since < self.policy['stable_seconds']: return 'hold'
        if now-self.last_change < self.policy.get('admission_interval_seconds',self.policy['window_seconds']):
            self.reason = 'instance_interval_pending' if self.policy.get('admission_basis') == 'resources' else 'stable_window_pending'; return 'hold'
        if self.policy.get('admission_basis') == 'resources':
            if count >= self.policy['maximum_instances']:
                self.reason = 'instance_limit'; return 'hold'
            budget=self.additional_worker_budget()
            if budget <= 0 or available < reserve+int(1.5*budget):
                self.reason = 'additional_worker_ram_unproven'; return 'hold'
            self.last_change=now; self.probing=False
            self.baseline_available=available; self.minimum_available=available
            self.reason='controlled_resource_probe'; return 'expand'
        tts_basis = self.policy.get('throughput_basis') == 'tts'
        quality = self.window(now)
        current = self.window(now,basis='tts') if tts_basis else quality
        rate = 'rendered_per_second' if tts_basis else 'checked_per_second'
        if self.probing:
            comparable = current and self.baseline and current['mix'].keys() == self.baseline['mix'].keys() and all(
                abs(current['mix'][key]-self.baseline['mix'][key]) <= .15 for key in current['mix'])
            benefit = comparable and current[rate] >= self.baseline[rate]*self.policy['minimum_gain'] and (
                self.baseline['audio_per_second'] > 0 and current['audio_per_second'] >= self.baseline['audio_per_second']*self.policy['minimum_gain']) and (
                current['latency_seconds'] is not None and self.baseline['latency_seconds'] is not None and
                current['latency_seconds'] <= self.baseline['latency_seconds']*1.1) and (
                quality is not None and self.baseline_quality is not None and
                quality['qa_failure_rate'] <= self.baseline_quality['qa_failure_rate'])
            if not benefit:
                self.reason = 'no_comparable_throughput_gain'; self.probing = False
                self.cooldown_until = now+self.policy['cooldown_seconds']; self.last_change = now
                return 'retire'
            self.probing = False
        if count >= self.policy['maximum_instances']:
            self.reason = 'instance_limit'; return 'hold'
        if not current or not quality:
            self.reason = 'throughput_baseline_pending'; return 'hold'
        # Never use zero or model filesize as the first unknown additional-worker cost.
        if self.peak_bytes <= 0 or available < reserve+int(1.5*self.peak_bytes):
            self.reason = 'additional_worker_ram_unproven'; return 'hold'
        self.baseline = current; self.baseline_quality = quality; self.probing = True; self.last_change = now
        # The next observation measures the marginal load of this one probe.
        self.baseline_available=available;self.minimum_available=available
        self.reason = 'controlled_throughput_probe'; return 'expand'

    def status(self, instances):
        window=self.window(time.time())
        tts=self.window(time.time(),basis='tts')
        return {'policy': self.policy, 'instances': list(instances), 'active_instances': len(instances),
                'maximum_instances': self.policy['maximum_instances'], 'reason': self.reason,
                'telemetry': self.last_sample, 'reserve_bytes': getattr(self, 'reserve_bytes', None),
                'worker_peak_estimate_bytes': self.peak_bytes, 'probing': self.probing,
                'additional_worker_budget_bytes':self.additional_worker_budget(),
                'admission_basis':self.policy.get('admission_basis','throughput'),
                'next_expansion_in_seconds':max(0, int(max(self.cooldown_until,
                    (self.stable_since if self.stable_since is not None else time.time())+self.policy['stable_seconds'],
                    (self.last_change if self.last_change is not None else time.time())+self.policy.get('admission_interval_seconds',self.policy['window_seconds']))-time.time()+.999)),
                'throughput_basis':self.policy.get('throughput_basis','qa'),
                'tts_throughput':{key:value for key,value in tts.items() if key!='mix'} if tts else None,
                'throughput':{key:value for key,value in window.items() if key!='mix'} if window else None}


def run_adaptive(dialogues, store, factory, sessions, completed, progress, cancel,
                 prepare_attempt, *, event_sink=None, first_pass=False, paused=lambda: False,
                 sampler=telemetry, controller=None, previous_instances=None, failed_dialogue=None,
                 gpu_executor=None, character_bindings=None, guybrush_role_id=None,
                 defer_qa_until_tts_complete=False, release_tts=None, benchmark_policy=lambda: {}, preferred_worker=None):
    """Bounded role scheduler. Retirement drains QA; critical pressure aborts safely."""
    from .pipeline import _failed
    if defer_qa_until_tts_complete and (not first_pass or gpu_executor is not None or release_tts is None):
        raise ValueError('Deferred adaptive QA requires an ANE-only first pass and TTS cleanup')
    if not first_pass: failed_dialogue = None
    controller = controller or Controller()
    instance_ceiling = controller.policy['maximum_instances']
    def apply_benchmark_budget():
        ceiling = benchmark_policy().get('maximum_ane_instances', instance_ceiling)
        controller.policy['maximum_instances'] = min(instance_ceiling, max(1, ceiling))
    apply_benchmark_budget()
    states = [{'dialogue': d, 'index': 0, 'results': [], 'done': False} for d in dialogues if d['processing']=='render']
    for d in dialogues:
        if d['processing'] == 'copy_original':
            completed(d, {'dialog_id': d['dialog_id'], 'processing': 'copy_original',
                          'selected': {'source_audio': d['source_audio']}, 'attempts': []})
    character_bindings = character_bindings if character_bindings is not None else {}
    if gpu_executor is not None:
        states.sort(key=lambda s: s['dialogue'].get('role_id') != guybrush_role_id)
    instances = {}; retiring = set(); renders = {}; audits = {}; owners = {}; previous_owners = dict(previous_instances or {})
    deferred = deque()
    qa = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mi1-qa')
    stopping = threading.Event(); emergency = threading.Event(); lock = threading.RLock()
    sessions.resource_stop = emergency
    staged = audited = 0; serial = 0; last_status = None

    def emit(event, **details):
        if event_sink: event_sink(event, **details)

    def call(prefix, name, attempt, fn, *args):
        details={'backend':'gpu' if name=='gpu' else 'ane','instance_id':name,'dialog_id':attempt.get('dialog_id'),
                 'attempt_fingerprint':attempt.get('fingerprint'),'role_id':attempt.get('role_id'),
                 'text':attempt.get('target_text'),'seed':attempt.get('seed')}
        began=time.monotonic();emit(prefix+'_started',**details)
        try: result=fn(*args)
        except BaseException as exc:
            emit(prefix+'_error',**details,error_type=type(exc).__name__,error=str(exc))
            raise
        emit(prefix+'_finished',**details,duration_seconds=time.monotonic()-began,
             **({'qa_status':result.get('qa_status')} if prefix=='qa' and isinstance(result,dict) else {}))
        return result

    def add():
        nonlocal serial
        serial += 1; name = f'ane-{serial}'
        sessions.instance_context.instance_id = name
        try: executor = factory()
        finally: sessions.instance_context.instance_id = None
        instances[name] = (executor, ThreadPoolExecutor(max_workers=1, thread_name_prefix=name))
        emit('ane_instance_started', backend='ane', instance_id=name)
        if controller.policy.get('startup_plan')=='sequential-model-ready':
            preload=getattr(executor,'preload',None)
            if preload:preload()
        return name

    def ane_names():
        return [name for name in instances if name != 'gpu']

    def monitor():
        while not stopping.is_set():
            with sessions.benchmark_monitor_lock:
                if not sessions.borrowed_for_benchmark.is_set():
                    if sessions.benchmark_resource_reset.is_set():
                        controller.previous = None
                        controller.baseline_available = controller.minimum_available = None
                        controller.stable_since = None
                        controller.last_change = time.time()
                        controller.swap_growth_samples = 0
                        sessions.adaptive_decision = 'hold'
                        sessions.benchmark_resource_reset.clear()
                    try: sample = sampler()
                    except Exception: sample = {'measured_at':time.time()}
                    with lock:
                        apply_benchmark_budget()
                        decision = controller.decide(sample, time.time(), len(ane_names())-len(retiring),
                                                 qa_backlog=len(audits), paused=paused())
                        if decision == 'stop':
                            emergency.set()
                            cancel()
                            sessions.cancel_ane_workers(timeout=.2)
                            if gpu_executor is not None: sessions.cancel_model_sessions(timeout=.2)
                        if decision != 'hold': sessions.adaptive_decision = decision
            stopping.wait(controller.policy['sample_interval_seconds'])

    thread = threading.Thread(target=monitor, name='mi1-ane-resource-guard', daemon=True)
    try:
        initial=sampler()
        if controller.decide(initial,time.time(),1,paused=paused())=='stop':
            raise ResourceGuardError('resource_guard_critical: stopped before first worker')
        if gpu_executor is not None:
            instances['gpu'] = (gpu_executor, ThreadPoolExecutor(max_workers=1, thread_name_prefix='mi1-gpu'))
        if controller.policy.get('startup_plan')=='sequential-model-ready':
            if controller.reason=='ram_reserve':
                raise ResourceGuardError('ram_reserve: insufficient headroom before ANE model loading')
            thread.start()
            before=initial
            while True:
                while paused() and not emergency.is_set():stopping.wait(.1)
                if emergency.is_set():raise ResourceGuardError('resource_guard_critical: stopped during preload')
                name=add()
                after=sampler()
                with lock:
                    apply_benchmark_budget()
                    if before.get('available_ram_estimate_bytes') is not None and after.get('available_ram_estimate_bytes') is not None:
                        delta=before['available_ram_estimate_bytes']-after['available_ram_estimate_bytes']
                        controller.peak_bytes=max(controller.peak_bytes,delta)
                    decision=controller.decide(after,time.time(),len(ane_names()),paused=paused())
                    status=controller.status(ane_names())
                emit('adaptive_ane_status',**status)
                if decision=='stop':raise ResourceGuardError('resource_guard_critical: stopped after model preload')
                if decision=='retire' and len(ane_names())>1:
                    executor,pool=instances.pop(name);pool.shutdown(wait=True)
                    sessions.close_ane_instance(name)
                    emit('ane_instance_retired',backend='ane',instance_id=name)
                if decision!='expand' or defer_qa_until_tts_complete:break
                before=after
            sessions.adaptive_decision='hold'
        else:
            add(); thread.start()
        while any(not s['done'] for s in states) or renders or audits:
            if emergency.is_set(): raise ResourceGuardError('resource_guard_critical: ANE job admissions stopped; unfinished attempts retained only as failures')
            for future in list(renders):
                if not future.done(): continue
                name, state, attempt = renders.pop(future)
                try:
                    result=future.result()
                except Exception as exc:
                    _failed(failed_dialogue,state,attempt,'render',exc)
                    state['done']=True
                    continue
                staged += 1
                state['measure']=name != 'gpu' and not (isinstance(result,dict) and result.get('synthesis_resumed_before_qa'))
                if name != 'gpu' and isinstance(result,dict) and result.get('execution',{}).get('cold_start'):
                    state['measure']=False
                    with lock:
                        controller.model_loaded(time.time())
                if state['measure']:
                    with lock:
                        controller.synthesized(time.time(),attempt,result,
                            result.get('synthesis_duration_seconds',time.monotonic()-state['started']) if isinstance(result,dict) else None)
                if defer_qa_until_tts_complete:
                    # Mark synthesis dispatched, without marking dialogue QA complete.
                    state['done'] = True
                    deferred.append((name, state, attempt, instances[name][0]))
                else:
                    audits[qa.submit(call,'qa',name,attempt,store.run, attempt, instances[name][0].audit_pending)] = (name, state, attempt)
            for future in list(audits):
                if not future.done(): continue
                name, state, attempt = audits.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    _failed(failed_dialogue,state,attempt,'qa',exc)
                    state['done']=True
                    continue
                audited += 1
                if state.get('measure') and not result.get('resumed'):
                    with lock: controller.completed(time.time(), attempt, result,time.monotonic()-state['started'])
                state['results'].append(result)
                if first_pass or result['qa_status'] in {'pass','warn'}:
                    state['done'] = True
                    completed(state['dialogue'], {'dialog_id':state['dialogue']['dialog_id'],'processing':'render',
                        'selected':{'fingerprint':result['fingerprint'],'qa_status':result['qa_status']},'attempts':state['results']})
                else:
                    state['index'] += 1
                    if state['index'] >= len(state['dialogue']['attempts']):
                        state['done'] = True
                        completed(state['dialogue'], {'dialog_id':state['dialogue']['dialog_id'],'processing':'render',
                                  'selected':None,'qa_status':'fail','attempts':state['results']})
            with lock:
                apply_benchmark_budget()
                decision = getattr(sessions, 'adaptive_decision', 'hold'); sessions.adaptive_decision = 'hold'
                sample=controller.last_sample
                expansion_safe=(sample.get('memory_pressure')=='normal' and sample.get('thermal_state') in {'nominal','fair'} and
                    (sample.get('cpu_load') if sample.get('cpu_load') is not None else 1)<.9 and time.time()-sample.get('measured_at',0)<=controller.policy['stale_seconds'] and
                    sample.get('swapouts') is not None and sample.get('swap_used_bytes') is not None and
                    (sample.get('available_ram_estimate_bytes') or 0)>=(controller.reserve_for(sample.get('system_ram_bytes')) or 0)+int(1.5*controller.additional_worker_budget()) and
                    controller.swap_growth_samples<2 and len(audits)<controller.policy['maximum_qa_backlog'])
                if (decision == 'expand' and expansion_safe and not paused() and not retiring and
                        len(ane_names()) < controller.policy['maximum_instances'] and not defer_qa_until_tts_complete): add()
                if decision == 'retire' and len(ane_names())-len(retiring)>1:
                    retiring.add(next(name for name in reversed(ane_names()) if name not in retiring))
                excess = len(ane_names())-len(retiring)-controller.policy['maximum_instances']
                if excess > 0 and not paused():
                    retiring.update([name for name in reversed(ane_names()) if name not in retiring][:excess])
                busy = {v[0] for v in renders.values()} | {v[0] for v in audits.values()}
                for name in list(retiring):
                    if name in busy: continue
                    executor, pool = instances.pop(name); pool.shutdown(wait=True)
                    sessions.close_ane_instance(name)
                    for role, owner in list(owners.items()):
                        if owner == name: previous_owners[role] = owners.pop(role)
                    retiring.remove(name); emit('ane_instance_retired',backend='ane',instance_id=name)
                status = controller.status(ane_names())
                status['retiring_instances']=list(retiring)
                status['admission_blocked']=paused() or controller.reason=='ram_reserve'
            if status != last_status:
                emit('adaptive_ane_status', **status); last_status = status
            if not paused() and controller.reason!='ram_reserve' and len(audits)<controller.policy['maximum_qa_backlog']:
                for name, (executor, pool) in list(instances.items()):
                    if name in retiring or any(v[0]==name for v in renders.values()): continue
                    if preferred_worker is not None:
                        backend = 'gpu' if name == 'gpu' else 'ane'
                        state = next((s for s in states if not s['done'] and
                            preferred_worker(s['dialogue']['attempts'][s['index']]) == backend and
                            not any(v[1] is s for v in audits.values()) and
                            not any(v[1] is s for v in renders.values())), None)
                    else:
                        roles = {s['dialogue'].get('role_id') for s in states if not s['done']}
                        for role, owner in list(owners.items()):
                            if role not in roles: owners.pop(role)
                        role = next((role for role, owner in owners.items() if owner==name), None)
                        if role is None:
                            backend = 'gpu' if name == 'gpu' else 'ane'
                            candidates = [s['dialogue'].get('role_id') for s in states if not s['done'] and s['dialogue'].get('role_id') not in owners]
                            role = next((r for r in candidates if character_bindings.get(r) == backend), None)
                            if role is None:
                                preferred = benchmark_policy().get('preferred_backend')
                                role = next((r for r in candidates if r not in character_bindings and
                                    (gpu_executor is None or r != guybrush_role_id or backend == 'gpu') and
                                    (gpu_executor is None or not preferred or backend == preferred or
                                     (r == guybrush_role_id and backend == 'gpu'))), None)
                            if role is not None:
                                owners[role]=name
                                if role in previous_owners:
                                    previous=previous_owners.pop(role)
                                    if previous!=name and name!='gpu':emit('ane_role_transferred',backend='ane',role_id=role,from_instance=previous,instance_id=name)
                        state = next((s for s in states if not s['done'] and s['dialogue'].get('role_id')==role and
                                      not any(v[1] is s for v in audits.values()) and not any(v[1] is s for v in renders.values())), None)
                        if state is None and name!='gpu' and controller.policy.get('role_dispatch')=='shared-ane':
                            # A role keeps its backend; distinct unstarted dialogues may use
                            # another ANE process. Scheduler state prevents duplicate attempts.
                            state=next((s for s in states if not s['done'] and
                                (character_bindings.get(s['dialogue'].get('role_id'))=='ane' or
                                 owners.get(s['dialogue'].get('role_id'),'').startswith('ane-')) and
                                not any(v[1] is s for v in audits.values()) and
                                not any(v[1] is s for v in renders.values())),None)
                    if state is None: continue
                    attempt=prepare_attempt(state['dialogue']['attempts'][state['index']], 'gpu' if name=='gpu' else 'ane')
                    state['started']=time.monotonic()
                    existing=store.cached(attempt)
                    if existing is not None:
                        state['measure']=False
                        future=qa.submit(lambda value=existing:value); audits[future]=(name,state,attempt)
                    else:
                        def stage(executor=executor, attempt=attempt, name=name):
                            return call('render',name,attempt,executor.stage,attempt)
                        renders[pool.submit(stage)]=(name,state,attempt)
            progress({'rendered_attempts':staged,'audited_attempts':audited,'pending_qa_attempts':len(audits)+len(deferred),
                      **({'tts_status':'rendering','qa_status':'waiting_for_tts'} if defer_qa_until_tts_complete else {}),
                      'adaptive_ane':status,'workers':{name:{'backend':'gpu' if name=='gpu' else 'ane','active':int(any(v[0]==name for v in renders.values()))} for name in instances}})
            pending = list(renders)+list(audits)
            if pending: wait(pending, timeout=.25, return_when=FIRST_COMPLETED)
            else: stopping.wait(.1)
    except BaseException as exc:
        emergency.set(); cancel(); sessions.cancel_ane_workers(timeout=.2)
        if gpu_executor is not None: sessions.cancel_model_sessions(timeout=.2)
        if controller.reason == 'resource_guard_critical':
            emit('resource_guard',reason=controller.reason,backend='ane')
            raise ResourceGuardError('resource_guard_critical: pool aborted; unfinished attempts were not released') from exc
        raise
    finally:
        stopping.set()
        if thread.ident is not None: thread.join(timeout=4)
        for executor, pool in instances.values(): pool.shutdown(wait=True, cancel_futures=True)
        qa.shutdown(wait=True, cancel_futures=True)
    if defer_qa_until_tts_complete:
        # Resource monitoring and TTS pools must stop before freeing model processes.
        try:
            release_tts()
            while deferred:
                name, state, attempt, executor = deferred[0]
                progress({'rendered_attempts':staged,'audited_attempts':audited,
                          'pending_qa_attempts':len(deferred),'ready_tts_ids':0,
                          'tts_status':'complete','qa_status':'auditing',
                          'workers':{}})
                try:
                    result=call('qa',name,attempt,store.run,attempt,executor.audit_pending)
                except Exception as exc:
                    _failed(failed_dialogue,state,attempt,'qa',exc)
                else:
                    audited += 1
                    state['results'].append(result)
                    completed(state['dialogue'], {'dialog_id':state['dialogue']['dialog_id'],
                        'processing':'render','selected':{'fingerprint':result['fingerprint'],
                        'qa_status':result['qa_status']},'attempts':state['results']})
                deferred.popleft()
            progress({'rendered_attempts':staged,'audited_attempts':audited,
                      'pending_qa_attempts':0,'ready_tts_ids':0,'tts_status':'complete',
                      'qa_status':'complete','workers':{}})
        except BaseException:
            cancel()
            raise
