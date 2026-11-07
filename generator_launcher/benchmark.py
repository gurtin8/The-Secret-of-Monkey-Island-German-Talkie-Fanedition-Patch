"""Explicit, temporary synthesis benchmark; importing never starts model work."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
import platform
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from .contracts import ContractError, digest, resolve_relative


class BenchmarkCancelled(RuntimeError):
    pass


def machine_identity(*, runner=subprocess.run):
    """A moved USB project must not reuse another Mac's optimization."""
    result=runner(['/usr/sbin/ioreg','-rd1','-c','IOPlatformExpertDevice'],
        check=True,capture_output=True,text=True,timeout=3)
    match=re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"',result.stdout)
    if not match: raise ContractError('Hardware-ID für den maschinengebundenen Benchmark fehlt')
    return dict(hardware_uuid_sha256=digest(match[1]), os_version=platform.mac_ver()[0],
        architecture=platform.machine())


def cache_key(gpu, ane, *, machine=None):
    from .render import component_identity
    from manifest_renderer.audio import sha256_file
    profile=copy.deepcopy(ane['render_execution'])
    worker=profile.get('ane_worker')
    if worker:
        # Installation locations/cache directories do not change synthesis bytes.
        worker.pop('model_root',None);worker.pop('cache_dir',None)
        command=worker.get('worker_command')
        if command:worker['worker_command']=[{'sha256':sha256_file(Path(command[0]))},*command[1:]]
    return digest(dict(format='launcher-benchmark-optimization/1',machine=machine or machine_identity(),
        gpu=component_identity(gpu,gpu['render_execution']),ane=component_identity(ane,profile),
        benchmark_code_sha256=digest(Path(__file__).read_text())))


def cache_path():
    from .contracts import installation_config_path
    return installation_config_path().parent/'benchmark-optimizations.json'


def cached_report(key, *, path=None):
    try:
        saved=json.loads((path or cache_path()).read_text())
        if not isinstance(saved,dict) or not isinstance(saved.get('entries'),dict):return None
        report=saved['entries'][key]
        if not isinstance(report,dict):return None
        if report.get('cache_key') != key or report.get('status') not in {'completed','partial'} or report.get('error'):
            return None
        if not all(v['complete'] and not v['errors'] for v in report['variants']+report.get('probes',[])):
            return None
        if not select_method(report,dict(backend='gpu',operating_mode='adaptive_gpu_ane',quality='mid'))['selection_applied']:
            return None
        return report
    except (OSError,ValueError,KeyError,TypeError,AttributeError): return None


def save_cached_report(report, *, path=None):
    from manifest_renderer.production import atomic_json
    if not report.get('cache_key'):return
    target=path or cache_path()
    try: saved=json.loads(target.read_text())
    except (OSError,ValueError):saved={}
    entries=saved.get('entries',{}) if isinstance(saved,dict) else {}
    if not isinstance(entries,dict):entries={}
    entries[report['cache_key']]=report
    atomic_json(target,dict(format='launcher-benchmark-optimizations/1',entries=entries))


def capture_source(root, manifest, source):
    """Bind the already materialized render inputs, never the initial export."""
    from manifest_renderer.production import immutable_json, atomic_json
    from manifest_renderer.audio import sha256_file
    body = dict(format='launcher-benchmark-source/1', manifest=copy.deepcopy(manifest),
                source=Path(source).relative_to(root).as_posix(), source_sha256=sha256_file(source))
    relative = '.launcher/benchmark-sources/' + digest(body) + '.json'
    immutable_json(Path(root)/relative, body)
    reference = dict(path=relative, sha256=digest(body))
    atomic_json(Path(root)/'.launcher/benchmark-source.json', reference)
    return reference


def load_source(root, reference):
    from manifest_renderer.audio import sha256_file
    from manifest_renderer.manifest import resolve_manifest
    path = resolve_relative(root, reference['path'], must_exist=True)
    body = json.loads(path.read_text(encoding='utf-8'))
    if body.get('format') != 'launcher-benchmark-source/1' or digest(body) != reference['sha256']:
        raise ContractError('Vorbereitete Benchmarkquelle wurde verändert')
    resolve_manifest(body['manifest'])
    source = resolve_relative(root, body['source'], must_exist=True)
    if sha256_file(source) != body['source_sha256']:
        raise ContractError('Materialisierte Renderquelle wurde verändert')
    if digest(json.loads(source.read_text())) != body['manifest']['source_evidence']['production']['source_manifest_sha256']:
        raise ContractError('Benchmarkquelle und vorbereitetes Manifest widersprechen sich')
    return body, source


def summarize_variant(name, records, *, workers, expected_ids):
    good = [r for r in records if r.get('status') == 'completed' and not r.get('cached')]
    makespan = max((r['ended'] for r in good), default=0)-min((r['started'] for r in good), default=0)
    # Count actual simultaneous requests, not pairwise intersecting intervals.
    events = sorted([(r['started'], 1) for r in good]+[(r['ended'], -1) for r in good])
    active = peak = 0
    for _, change in events:
        active += change; peak = max(peak, active)
    complete = len(good) == len(expected_ids) and {r['dialog_id'] for r in good} == set(expected_ids)
    audio_seconds = sum(r['audio_seconds'] for r in good)
    return dict(name=name, workers=workers, actual_concurrency=peak, completed=len(good),
                errors=len(records)-len(good), complete=complete, records=records,
                makespan_seconds=makespan, audio_seconds=audio_seconds,
                dialogs_per_second=len(good)/makespan if makespan > 0 else None,
                audio_seconds_per_second=audio_seconds/makespan if makespan > 0 else None,
                qa_status='unreviewed', validation='PCM16 mono 48 kHz; no content QA')


def speedups(variants, ids):
    by = {v['name']:v for v in variants}
    if set(by) != {'gpu_single', 'ane_single', 'ane_multi'}:
        return {}, 'Nicht alle drei Varianten abgeschlossen'
    if any(not v['complete'] or v['errors'] or v['makespan_seconds'] <= 0 for v in variants):
        return {}, 'Teilbericht: fehlende, wiederverwendete oder fehlerhafte Versuche'
    if by['ane_multi']['actual_concurrency'] < 2:
        return {}, 'Keine ausreichende parallele Kapazität oder kein tatsächlicher Überlapp'
    expected = set(ids)
    if any({r['dialog_id'] for r in v['records']} != expected for v in variants):
        return {}, 'Dialogauswahl zwischen Varianten abweichend'
    return dict(ane_single_vs_gpu=by['gpu_single']['makespan_seconds']/by['ane_single']['makespan_seconds'],
                ane_multi_vs_gpu=by['gpu_single']['makespan_seconds']/by['ane_multi']['makespan_seconds'],
                ane_multi_vs_ane_single=by['ane_single']['makespan_seconds']/by['ane_multi']['makespan_seconds']), None


def select_method(report, selected):
    """Choose a measured lane within the user's backend/quality/instance limits."""
    empty = dict(selected_method=None, selected_workers=None, selection_applied=False)
    if report.get('status') in {'failed', 'cancelled'} or report.get('error'):
        return {**empty, 'selection_reason': 'Benchmark fehlgeschlagen oder abgebrochen; bisherige Einstellung bleibt erhalten'}
    if selected['quality'] != report.get('quality'):
        return {**empty, 'selection_reason': 'Höchste Qualität wurde nicht gemessen; GPU und Qualitätswahl bleiben erhalten'}
    mode = selected['operating_mode']
    allowed = {'gpu', 'ane'} if mode in {'parallel_gpu_ane', 'adaptive_gpu_ane'} else {selected['backend']}
    multiple = mode in {'adaptive_ane', 'adaptive_gpu_ane'}
    expected = set(report['dialog_ids'])
    candidates = []
    for variant in report['variants'] + report.get('probes', []):
        backend = 'gpu' if variant['name'] == 'gpu_single' else 'ane'
        records = variant['records']
        if (backend not in allowed or (variant['workers'] > 1 and not multiple) or
                not variant['complete'] or variant['errors'] or variant['makespan_seconds'] <= 0 or
                len(records) != len(expected) or {r['dialog_id'] for r in records} != expected or
                any(r.get('status') != 'completed' or r.get('cached') for r in records) or
                (variant['workers'] > 1 and variant['actual_concurrency'] < 2)):
            continue
        candidates.append((variant['makespan_seconds'], variant['workers'], backend, variant))
    if not candidates:
        return {**empty, 'selection_reason': 'Keine vollständig vergleichbare Messung für die gewählte Einstellung'}
    _, workers, backend, winner = min(candidates, key=lambda item: item[:2])
    method = 'gpu_single' if backend == 'gpu' else 'ane_single' if workers == 1 else 'ane_multi_'+str(workers)
    reason = 'Schnellste fehlerfreie Messung innerhalb der gewählten Qualität, Backends und Ultra-Fast-Grenze'
    if len(allowed) > 1:
        reason += '; bevorzugt neue ungebundene Rollen, bestehende Figurenbindungen und Guybrush-GPU bleiben erhalten. Der gemischte Produktionspool wurde nicht gemessen'
    return dict(selected_method=method, selected_workers=workers, selection_applied=True,
                selection_reason=reason, preferred_backend=backend, maximum_ane_instances=workers if backend == 'ane' else 1)


class LauncherBenchmark:
    def __init__(self, renderer):
        self.renderer = renderer; self.root = renderer.root
        self.lock = threading.RLock(); self.thread = None; self.cancelled = threading.Event()
        self.value = dict(status='idle', phase=None, total=0, completed=0, variants=[], report=None, error=None)
        self.pools = []; self.resume_original = False

    def status(self):
        renderer=self.renderer
        ongoing=renderer.thread and renderer.thread.is_alive()
        source_ready=bool(getattr(renderer,'_benchmark_source',None) or getattr(renderer,'_benchmark_prepared',None)) if ongoing else (self.root/'.launcher/benchmark-source.json').is_file()
        with self.lock: return {**copy.deepcopy(self.value),'source_ready':source_ready}

    def update(self, **values):
        with self.lock: self.value.update(values)

    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def check(self):
        if self.cancelled.is_set(): raise BenchmarkCancelled('Benchmark abgebrochen')

    def cancel(self):
        self.cancelled.set()
        for pool in list(self.pools):
            if pool.resource_stop is not None: pool.resource_stop.set()
            pool.cancel_ane_workers(timeout=.2); pool.cancel_model_sessions(timeout=.2)
        return self.status()

    def queue(self, count=12):
        if type(count) is not int or not 2 <= count <= 24:
            raise ContractError('Benchmark benötigt 2 bis 24 Dialoge')
        renderer = self.renderer
        with renderer.lock:
            if self.busy(): raise ContractError('Benchmark läuft bereits oder wartet')
            if renderer.repair.busy() or (renderer.review and renderer.review.state['job']['status']=='running'):
                raise ContractError('Reparatur-/Reviewauftrag zuerst beenden')
            ongoing = renderer.thread if renderer.thread and renderer.thread.is_alive() else None
            reference = getattr(renderer, '_benchmark_source', None) if ongoing else None
            if ongoing and reference is None and not getattr(renderer,'_benchmark_prepared',None) and renderer.pause_gate.requested and not renderer.pause_gate.active:
                raise ContractError('Noch keine zwei vollständig vorbereiteten Dialoge verfügbar; die bestehende Pause bleibt erhalten')
            if not ongoing:
                try: reference = json.loads((self.root/'.launcher/benchmark-source.json').read_text())
                except (OSError, ValueError): raise ContractError('Zuerst eine lokale Renderfassung vorbereiten')
            self.cancelled.clear(); self.pools = []; self.resume_original = False
            self.update(status='queued', phase='waiting_for_preparation' if ongoing and not reference else 'waiting_for_pause' if ongoing else 'preparation',
                        total=count*3, completed=0, variants=[], probes=[],speedups={},ratio_reason=None,
                        stop_reason=None,solution=None,report=None,error=None,
                        selected_method=None,selected_workers=None,selection_applied=False,selection_reason=None)
            self.thread = threading.Thread(target=self.execute, args=(ongoing, reference, count),
                                           name='launcher-benchmark', daemon=True)
            self.thread.start()
        return self.status()

    def borrow(self, ongoing, reference):
        """Drain active work and borrow either a complete or safely frozen subset."""
        renderer = self.renderer
        if ongoing:
            while reference is None and not getattr(renderer,'_benchmark_prepared',None) and ongoing.is_alive():
                self.check(); reference = getattr(renderer, '_benchmark_source', None)
                if renderer.pause_gate.requested:
                    break
                self.cancelled.wait(.1)
            self.check()
            if ongoing.is_alive():
                self.update(phase='waiting_for_pause')
                gate = renderer.pause_gate
                with gate.condition:
                    self.resume_original = not gate.requested
                    gate.pause()
                while ongoing.is_alive():
                    self.check()
                    with gate.condition:
                        if not gate.active:
                            gate._status('paused')
                            break
                        gate.condition.wait(.1)
                self.check()
                # Gated constructors and requests cannot start after this point.
                with renderer.lock:
                    owners = list(renderer._production_owners)
                    pools = list({id(s):s for _,s in owners}.values())
                    for pool in pools: pool.borrowed_for_benchmark.set()
                    renderer._benchmark_borrowed_pools = pools
                    # Wait for any monitor sample that had already started.
                    for pool in pools:
                        with pool.benchmark_monitor_lock: pass
                    for owner,_ in owners:
                        release = getattr(owner, 'release_ane_worker', None)
                        if release: release()
                    for pool in pools: pool.close()
            if reference is None:
                reference=getattr(renderer,'_benchmark_source',None)
            if reference is None:
                prepared=getattr(renderer,'_benchmark_prepared',None)
                if prepared is None:
                    raise ContractError('Noch keine zwei vollständig vorbereiteten Dialoge verfügbar; die bestehende Pause bleibt erhalten')
                from manifest_renderer import production as p
                source=self.root/'.launcher/benchmark-sources'/(digest(prepared)+'-input.json')
                p.immutable_json(source,prepared)
                manifest=p.prepare_production(source,self.root,backend='gpu',full_integrity=True)
                reference=capture_source(self.root,manifest,source)
        return reference

    def restore_original(self, *, successful=False):
        renderer = self.renderer
        for pool in getattr(renderer, '_benchmark_borrowed_pools', []):
            pool.benchmark_resource_reset.set(); pool.borrowed_for_benchmark.clear()
        renderer._benchmark_borrowed_pools = []
        if ((self.resume_original or successful) and renderer.thread and renderer.thread.is_alive() and not renderer.pause_gate.stopped
                and renderer.status.snapshot()['status'] in {'paused','pausing'}):
            renderer.pause_gate.resume()
        self.resume_original = False

    def execute(self, ongoing, reference, count):
        report = None; destination = None; successful = False
        try:
            reference = self.borrow(ongoing, reference)
            self.check()
            operation_lock = getattr(self.renderer, 'operation_lock', self.renderer.lock)
            with operation_lock:
                self.check(); self.update(status='running', phase='preparation')
                source, prepared_path = load_source(self.root, reference)
                from manifest_renderer import production as p
                from .render import validate_launcher_host
                validate_launcher_host(self.root, backend='ane')
                from .local_ane import resolve_local_ane
                ane = resolve_local_ane(self.root)
                gpu = p.prepare_production(prepared_path, self.root, backend='gpu', full_integrity=True)
                ane_manifest = p.prepare_production(prepared_path, self.root, backend='ane', ane_worker_config=ane, full_integrity=True)
                # load_source authenticates the immutable input. Old code
                # receipts describe the previous app, so validate fresh runtime
                # bindings rather than requiring the workdir to keep old code.
                p.verify_production(gpu, self.root)
                p.verify_production(ane_manifest, self.root)
                optimization_key=cache_key(gpu,ane_manifest) if self.renderer.prepare is None else None
                # Pick the same deterministic standard attempt across all variants.
                plan = p.build_run_plan(gpu)
                templates=[]
                for row in plan['dialogues']:
                    if row['processing']!='render':continue
                    candidate=row['attempts'][0]
                    generated=(candidate.get('prewarm') or {}).get('artifacts',{})
                    # Reuse only prewarms that already exist; never materialize
                    # production-scoped references from a benchmark job.
                    if generated and not all(resolve_relative(self.root,path).is_file() for path in generated.values()):continue
                    templates.append(candidate)
                    if len(templates)>=count:break
                if len(templates) < 2: raise ContractError('Benchmark benötigt mindestens zwei renderbare Dialoge')
                ids = [a['dialog_id'] for a in templates]
                self.update(total=len(ids)*3)
                benchmark_id = str(uuid.uuid4()); relative = '.launcher/benchmarks/'+benchmark_id
                destination = resolve_relative(self.root, relative)
                destination.mkdir(parents=True, exist_ok=False)
                p.immutable_json(destination/'source.json', source)
                report = dict(format='launcher-synthesis-benchmark/1', benchmark_id=benchmark_id,
                              source=reference, dialog_ids=ids, quality='mid', synthesis_only=True,
                              qa_status='unreviewed', variants=[], speedups={}, ratio_reason=None)
                if optimization_key:report['cache_key']=optimization_key
                report['probes']=[]; best=None; stop_reason=None
                for name, manifest in [('gpu_single',gpu),('ane_single',ane_manifest)]:
                    self.check()
                    variant=self.run_variant(name,manifest,templates,relative,1)
                    report['variants'].append(variant);self.update(variants=report['variants'])
                    p.atomic_json(destination/'report.json',report)
                    if not variant['complete'] or variant['errors']:
                        stop_reason='Einzelvariante unvollständig';break
                if stop_reason is None:
                    previous=report['variants'][1]
                    for requested in range(2,6):
                        self.check();self.update(total=len(ids)*(2+len(report['probes'])+1))
                        probe=self.run_variant('ane_multi_'+str(requested),ane_manifest,templates,relative,requested)
                        probe['requested_workers']=requested;report['probes'].append(probe)
                        if not probe['complete'] or probe['errors']:
                            stop_reason='ANE-Probe fehlgeschlagen';break
                        if probe['workers']<requested or probe['actual_concurrency']<2:
                            stop_reason='Ressourcen erlauben keine weitere parallele Instanz';break
                        probe['speedup_vs_previous']=previous['makespan_seconds']/probe['makespan_seconds']
                        if probe['makespan_seconds']>=previous['makespan_seconds']:
                            stop_reason='Weitere ANE-Instanz verbessert den Durchsatz nicht';break
                        best=probe;previous=probe
                        self.update(probes=report['probes'])
                        p.atomic_json(destination/'report.json',report)
                    if best is None and report['probes']:
                        # Keep measured data visible; a slower two-worker probe
                        # is a valid measurement, not a claimed improvement.
                        best=report['probes'][0] if report['probes'][0]['complete'] else None
                    if best is not None:
                        report['variants'].append({**best,'name':'ane_multi'})
                report['stop_reason']=stop_reason
                report['speedups'],report['ratio_reason']=speedups(report['variants'],ids)
                probe_error=next((probe for probe in report['probes'] if not probe['complete'] or probe['errors']),None)
                if probe_error:
                    report['speedups']={};report['ratio_reason']='Teilbericht: ANE-Probe fehlgeschlagen'
                    report['error']='ANE-Probe fehlgeschlagen'
                    report['solution']='Weniger ANE-Instanzen verwenden, andere Programme schließen und RAM/Swap prüfen'
                report['status']='completed' if report['speedups'] else 'partial'
                successful = (not report.get('error') and len(report['variants']) >= 2 and
                    all(v['complete'] and not v['errors'] for v in report['variants']+report['probes']))
                if successful:
                    self.check()
                    selection = self.renderer.apply_benchmark(report)
                    report.update(selection); self.update(**selection)
                self.update(variants=report['variants'],probes=report['probes'],stop_reason=stop_reason,
                            error=report.get('error'),solution=report.get('solution'))
                self.update(phase='report')
                p.atomic_json(destination/'report.json', report)
                self.update(status=report['status'], phase='report', report=relative+'/report.json',
                            speedups=report['speedups'],ratio_reason=report['ratio_reason'])
        except BaseException as exc:
            successful = False
            status = 'cancelled' if isinstance(exc, BenchmarkCancelled) or self.cancelled.is_set() else 'failed'
            if report is not None and destination is not None:
                from manifest_renderer.production import atomic_json
                report.update(status=status,error=str(exc),variants=self.status()['variants'],speedups={},ratio_reason='Benchmark nicht vollständig abgeschlossen')
                if status=='failed':report['solution']='Weniger ANE-Instanzen verwenden, andere Programme schließen und RAM/Swap prüfen'
                atomic_json(destination/'report.json', report)
            self.update(status=status,error=str(exc),solution=report.get('solution') if report else None,report=(destination/'report.json').relative_to(self.root).as_posix() if destination else None)
        finally:
            try:
                # Cancel only benchmark-owned processes before returning models
                # to the paused production job, including exceptional cleanup.
                for pool in list(self.pools):
                    pool.cancel_ane_workers(timeout=.2);pool.cancel_model_sessions(timeout=.2)
                    pool.close();self.pools.remove(pool)
                self.restore_original(successful=successful and not self.cancelled.is_set())
            except BaseException as exc: self.update(status='failed', error='Renderfortsetzung fehlgeschlagen: '+str(exc))

    def guard_sample(self, controller, count):
        from manifest_renderer.adaptive_ane import telemetry, ResourceGuardError
        self.check(); sample = telemetry()
        with controller.benchmark_lock:
            decision = controller.decide(sample,time.time(),count)
        if decision == 'stop' or controller.reason in {'ram_reserve','memory_pressure','thermal_pressure','unknown_telemetry','stale_telemetry','swap_growth'}:
            raise ResourceGuardError('Benchmark-Ressourcenschutz: '+controller.reason)
        return sample, decision

    def run_variant(self, name, manifest, templates, relative, maximum):
        from manifest_renderer import production as p
        from manifest_renderer.adaptive_ane import Controller, effective_policy
        from manifest_renderer.hardware import host_facts, GIB
        from manifest_renderer.session import SessionPool
        from manifest_renderer.backend import bind_attempt
        from manifest_renderer.audio_cache import pcm_info
        facts = host_facts(); ram = facts.get('system_ram_bytes')
        maximum = min(maximum, len(templates)) if isinstance(ram,int) and ram > 8*GIB else 1
        controller = Controller(policy={**effective_policy(facts), 'maximum_instances':maximum,'reserve_fraction':.15})
        controller.benchmark_lock=threading.RLock()
        resources = threading.Event(); stopping = threading.Event(); guard_error = []
        owners = []; records = []; pools = []; warmups=[]; counter_lock = threading.Lock()
        m = copy.deepcopy(manifest); m['output']['root']=relative+'/'+name
        m['source_evidence']['production']['launcher_quality']='mid'
        # Model feature caches stay inside this benchmark, never production caches.
        if m['render_execution'].get('ane_worker'):
            m['render_execution']['ane_worker']=copy.deepcopy(m['render_execution']['ane_worker'])
        p.immutable_json(self.root/m['output']['root']/'manifest.json',m)

        def attempt(template, worker, warm=False):
            a=copy.deepcopy(template)
            a['runtime']=copy.deepcopy(m['runtime']); a['models']=copy.deepcopy(m['models'])
            a['render_execution']=copy.deepcopy(m['render_execution'])
            a['fingerprint']=digest(dict(original=template['fingerprint'],benchmark=relative,variant=name,
                                         worker=worker,warmup=warm,nonce=str(uuid.uuid4())))
            if a.get('prewarm'):
                for path in a['prewarm'].get('artifacts',{}).values():
                    if not resolve_relative(self.root,path,must_exist=True).is_file():
                        raise ContractError('Benchmark benötigt bereits materialisierte Prewarms')
            bind_attempt(self.root,m,a)
            return a

        def add_worker():
            sample,_=self.guard_sample(controller,max(1,len(owners)))
            if owners:
                reserve=int(.15*(sample.get('system_ram_bytes') or 0))
                budget=controller.peak_bytes or 8*GIB
                if (sample.get('cpu_load',1)>=.9 or
                        sample.get('available_ram_estimate_bytes',0) < reserve+int(1.5*budget)):
                    return False
            number=len(owners); pool=SessionPool(logs_dir=self.root/m['output']['root']/f'sessions-{number}')
            pool.resource_stop=resources; pool.instance_context.instance_id='benchmark-'+str(number)
            executor=p.ManifestExecutor(self.root,m,pool,None)
            # Preparation conversion caches are isolated too.
            from manifest_renderer.audio_cache import AudioPreparationCache
            executor.cache=AudioPreparationCache(self.root/m['output']['root']/f'audio-cache-{number}',executor.ffmpeg)
            pools.append(pool); self.pools.append(pool); owners.append(executor)
            self.update(phase='warmup', variant=name, workers=len(owners))
            load_started=time.monotonic()
            if m['render_execution']['profile_id']=='ane':executor.preload()
            loaded=time.monotonic()
            self.guard_sample(controller,len(owners))
            result=executor.stage(attempt(templates[number % len(templates)],number,warm=True))
            if result.get('synthesis_resumed_before_qa'): raise ContractError('Warmup darf keine Synthese wiederverwenden')
            warmups.append(dict(worker=number,load_started=load_started,loaded=loaded,
                                warmup_ended=time.monotonic(),excluded_from_timed_total=True))
            controller.model_loaded(time.time())
            self.guard_sample(controller,len(owners))
            return True

        def monitor():
            while not stopping.wait(2):
                try: self.guard_sample(controller,len(owners))
                except BaseException as exc:
                    guard_error.append(exc); resources.set()
                    for pool in list(pools):
                        pool.cancel_ane_workers(timeout=.2);pool.cancel_model_sessions(timeout=.2)
                    return

        def run_job(item):
            number, template = item
            executor=owners[number % len(owners)]; a=attempt(template,number % len(owners))
            self.check()
            self.guard_sample(controller,len(owners))
            if executor.pending_path(a).exists(): raise ContractError('Benchmarkversuch existiert bereits')
            began=time.monotonic()
            record=dict(dialog_id=a['dialog_id'],target_text=a['target_text'],seed=a['seed'],method=a['method'],
                        fingerprint=a['fingerprint'],started=began,worker=number % len(owners),cached=False)
            try:
                result=executor.stage(a); record.update(ended=time.monotonic())
                record['cached']=bool(result.get('synthesis_resumed_before_qa'))
                if record['cached']: raise ContractError('Wiederverwendete Synthese ist kein Messwert')
                record.update(status='synthesized',wav=executor.pending_path(a).relative_to(self.root).as_posix()+'/output.wav',
                              duration_seconds=record['ended']-began,execution=result.get('execution'),qa_status='unreviewed')
            except BaseException as exc:
                record.update(status='failed',ended=time.monotonic(),error=str(exc))
            with counter_lock:
                records.append(record)
            return record

        monitor_thread=threading.Thread(target=monitor,name='benchmark-resource-guard',daemon=True)
        try:
            # A live guard covers loading and warmup as well as measured work.
            self.guard_sample(controller,1); monitor_thread.start(); add_worker()
            if maximum>1:
                self.update(phase='resource_admission',variant=name,workers=len(owners))
                for _ in range(maximum-1):
                    self.check()
                    if guard_error: raise guard_error[0]
                    if not add_worker(): break
            self.update(phase='generating',variant=name,workers=len(owners))
            # One executor stays on one serial worker; separate executors overlap.
            def lane(number):
                for index in range(number,len(templates),len(owners)):
                    self.check(); run_job((index,templates[index]))
            with ThreadPoolExecutor(max_workers=len(owners),thread_name_prefix='benchmark-tts') as workers:
                futures=[workers.submit(lane,n) for n in range(len(owners))]
                for future in futures: future.result()
            if guard_error: raise guard_error[0]
            self.check()
            self.update(phase='validation',variant=name)
            for record in records:
                if record['status']!='synthesized':continue
                try:
                    info=pcm_info(resolve_relative(self.root,record['wav'],must_exist=True))
                    record.update(status='completed',audio_seconds=info['frames']/info['sample_rate_hz'],wav_sha256=info['sha256'])
                    with self.lock:self.value['completed']+=1
                except Exception as exc:record.update(status='failed',error=str(exc))
            return {**summarize_variant(name,records,workers=len(owners),expected_ids=[a['dialog_id'] for a in templates]),
                    'warmups':warmups,'maximum_workers':maximum,'ram_reserve_fraction':.15}
        except BaseException:
            summary={**summarize_variant(name,records,workers=len(owners),expected_ids=[a['dialog_id'] for a in templates]),
                     'warmups':warmups,'maximum_workers':maximum,'ram_reserve_fraction':.15}
            self.update(variants=[*self.status()['variants'],summary])
            p.atomic_json(self.root/m['output']['root']/'partial.json',summary)
            raise
        finally:
            stopping.set()
            if monitor_thread.ident: monitor_thread.join(timeout=3)
            cleanup_error=None
            for owner in owners:
                try:owner.release_ane_worker()
                except BaseException as exc:cleanup_error=cleanup_error or exc
            for pool in pools:
                try:
                    pool.close()
                    if pool in self.pools:self.pools.remove(pool)
                except BaseException as exc:cleanup_error=cleanup_error or exc
            if cleanup_error:raise cleanup_error
