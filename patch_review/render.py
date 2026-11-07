"""Opt-in production adapter; all generated/cache/STT files use review state root."""
import copy
import atexit
import threading
from pathlib import Path
from manifest_renderer.backend import profile
from manifest_renderer.manifest import canonical_json,sha256
from manifest_renderer.production import ManifestExecutor,ManifestAuditor,validate_host_selection
from manifest_renderer.hardware import host_facts
from manifest_renderer.session import SessionPool


class ProductionAdapter:
    def __init__(self,asset_root,ane_config=None,host_validator=None):
        self.asset_root=Path(asset_root).resolve(strict=True);self.ane_config=ane_config
        self.host_validator=host_validator or validate_host_selection
        self._pool_lock=threading.RLock();self._pool_idle=threading.Condition(self._pool_lock);self._gpu_run_lock=threading.RLock()
        self._gpu_identity=None;self._gpu_sessions=None;self._gpu_logs=None
        self._ane_identity=None;self._ane_worker=None;self._ane_asset_cache=None
        self._active_gpu=0;self._active_ane=0;self._retire_gpu=False;self._retire_ane=False
        self._runtime_receipt_cache=None
        atexit.register(self.close)
    def backend_profile(self,backend,manifest):
        selected=profile(backend)
        if backend=='ane':
            if self.ane_config is None:raise ValueError('ANE benötigt --ane-worker-config mit hashgebundenen Modellpfaden')
            selected['ane_worker']=copy.deepcopy(self.ane_config)
            selected['model_identity']=dict(weights_present=True,component_receipts_sha256=sha256(canonical_json(self.ane_config)))
            selected['assets']=self._profile_assets(selected)
        else:selected['model_identity']=copy.deepcopy(manifest.get('render_execution',{}).get('model_identity'))
        return selected
    def runtime_for_attempt(self,backend,manifest):
        """Bind an explicitly started attempt to the installed review speed CLI."""
        runtime=copy.deepcopy(manifest['runtime'])
        if backend not in ('gpu','cpu'):return runtime
        artifacts=runtime.get('artifacts',{});names=('isolated_cli','combined_cli')
        # Rebinding is limited to the review's established local speed binary.
        # Other runtimes retain their source receipts and normal validation.
        if any(not isinstance(artifacts.get(name),dict) or artifacts[name].get('path')!='bin/voxcpm2_cli_speed' for name in names):return runtime
        from manifest_renderer.audio import resolve_manifest_path,sha256_file
        path=resolve_manifest_path(self.asset_root,'bin/voxcpm2_cli_speed')
        def identity():
            if resolve_manifest_path(self.asset_root,'bin/voxcpm2_cli_speed')!=path:
                raise ValueError('Review-Speed-CLI-Pfad wurde verändert')
            stat=path.stat()
            if not path.is_file() or not 0<stat.st_size<=8*1024*1024:
                raise ValueError('Review-Speed-CLI muss eine reguläre Datei von höchstens 8 MiB sein')
            return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        with self._pool_lock:
            before=identity();cached=self._runtime_receipt_cache
            if cached is None or cached[0]!=before:
                active_hash=sha256_file(path)
                if identity()!=before:raise ValueError('Review-Speed-CLI wurde beim Lesen verändert')
                self._runtime_receipt_cache=(before,active_hash)
            else:active_hash=cached[1]
        changed=any(artifacts[name].get('sha256')!=active_hash for name in names)
        for name in names:artifacts[name]['sha256']=active_hash
        if changed:
            runtime['runtime_version']='sha256:'+sha256(canonical_json(dict(
                format='patch-review-runtime/1',source_runtime_version=manifest['runtime'].get('runtime_version'),
                backend=backend,artifacts=artifacts)))
        return runtime
    def _profile_assets(self,selected):
        from manifest_renderer.backend import ANE_RUNTIME_ASSETS,profile_assets
        from manifest_renderer.audio import resolve_manifest_path
        identity=[]
        for name in ANE_RUNTIME_ASSETS:
            path=resolve_manifest_path(self.asset_root,name);st=path.stat()
            identity.append((str(path),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns))
        identity=tuple(identity)
        cached=self._ane_asset_cache
        if cached is not None and cached[0]==identity:return copy.deepcopy(cached[1])
        assets=profile_assets(self.asset_root,selected)
        verify=[]
        for name in ANE_RUNTIME_ASSETS:
            path=resolve_manifest_path(self.asset_root,name);st=path.stat()
            verify.append((str(path),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns))
        if tuple(verify)!=identity:raise ValueError('ANE-Runtimeassets wurden beim Prüfen geändert')
        self._ane_asset_cache=(identity,copy.deepcopy(assets))
        return assets
    def repair_parallel_allowed(self):
        """Whether repair may overlap the already-warm GPU and ANE backends."""
        from manifest_renderer.adaptive_ane import telemetry
        try:sample=telemetry()
        except Exception:return False
        if not isinstance(sample,dict):return False
        total=sample.get('system_ram_bytes');available=sample.get('available_ram_estimate_bytes')
        if (not isinstance(total,int) or isinstance(total,bool) or total<16*1024**3 or
                not isinstance(available,int) or isinstance(available,bool) or available<0 or
                sample.get('memory_pressure')!='normal' or sample.get('thermal_state') not in {'nominal','fair'}):
            return False
        # Before the second backend exists, reserve a conservative 8 GiB for
        # loading it. Once both pools are resident, only enforce the 15% reserve.
        with self._pool_lock:
            pool=self._gpu_sessions
            sessions=getattr(pool,'sessions',{}) if pool is not None else {}
            gpu_warm=any(getattr(session,'process',None) is not None and
                         getattr(session.process,'poll',lambda:0)() is None for session in sessions.values())
            both=gpu_warm and self._ane_worker is not None
        reserve=int(.15*total)+(0 if both else 8*1024**3)
        return available>=reserve
    def _retire_gpu_locked(self):
        if self._gpu_sessions is None:return
        if self._active_gpu:
            self._retire_gpu=True
            return
        self._gpu_sessions.close();self._gpu_sessions=None;self._gpu_identity=None;self._retire_gpu=False
    def _retire_ane_locked(self):
        if self._ane_worker is None:return
        if self._active_ane:
            self._retire_ane=True
            return
        self._close_ane_locked();self._retire_ane=False
    def _track_execution(self,execution,backend):
        adapter=self
        class BorrowedExecution:
            def __init__(self):self._closed=False;self._progress_callback=None
            def _active(self,operation,*args,**kwargs):
                return adapter._active_backend_run(backend,lambda:operation(*args,**kwargs),progress_callback=self._progress_callback)
            def __call__(self,*args,**kwargs):return self._active(execution,*args,**kwargs)
            def set_progress_callback(self,callback):
                self._progress_callback=callback
                if backend=='gpu':return getattr(execution,'supports_live_progress',True)
                return execution.set_progress_callback(callback)
            def close(self,*args,**kwargs):
                if backend=='ane':kwargs.setdefault('preserve_ane_worker',True)
                return execution.close(*args,**kwargs)
            def __getattr__(self,name):
                value=getattr(execution,name)
                if name=='stage' and callable(value):return lambda *args,**kwargs:self._active(value,*args,**kwargs)
                return value
        return BorrowedExecution()
    def _active_backend_run(self,backend,operation,*,acquired=False,progress_callback=None):
        if not acquired:
            with self._pool_lock:
                if backend=='gpu':self._active_gpu+=1
                elif backend=='ane':self._active_ane+=1
        try:
            if backend=='gpu':
                with self._gpu_run_lock:
                    sessions=self._gpu_sessions
                    previous=getattr(sessions,'progress_callback',None)
                    if sessions is not None:sessions.progress_callback=progress_callback
                    try:return operation()
                    finally:
                        if sessions is not None:sessions.progress_callback=previous
            return operation()
        finally:
            with self._pool_lock:
                try:
                    if backend=='gpu':
                        self._active_gpu=max(0,self._active_gpu-1)
                        if self._retire_gpu and not self._active_gpu:self._retire_gpu_locked()
                    elif backend=='ane':
                        self._active_ane=max(0,self._active_ane-1)
                        if self._retire_ane and not self._active_ane:self._retire_ane_locked()
                finally:self._pool_idle.notify_all()
    def for_manifest(self,manifest,state_root):
        # Runs only after explicit start, never while constructing UI or saving edits.
        backend=manifest['render_execution']['profile_id']
        # The run directory is created by Execution below. Measure its filesystem
        # through the nearest existing ancestor before creating any run files.
        output_root=Path(state_root).resolve()/manifest['output']['root']
        disk_path=output_root
        while not disk_path.exists():
            disk_path=disk_path.parent
        self.host_validator(backend,'single',facts=host_facts(disk_path=disk_path))
        if backend=='ane':
            from manifest_renderer.ane_worker import resolve_bindings
            selected=manifest.get('render_execution',{})
            bindings=selected.get('ane_worker')
            expected=selected.get('model_identity',{}).get('component_receipts_sha256')
            if not isinstance(bindings,dict) or expected!=sha256(canonical_json(bindings)):
                raise ValueError('ANE-Warmmodell benötigt exakt gebundene Modellbelege')
            # Revalidate the signed local component bindings before borrowing an
            # already-loaded worker; resolve_bindings is stat-bound and rehashes
            # changed files before accepting the prior receipt.
            resolve_bindings(bindings,verify_hashes=True)
            if selected.get('assets')!=self._profile_assets(selected):
                raise ValueError('ANE-Runtimeassets stimmen nicht mehr mit ihren Belegen überein')
            identity=sha256(canonical_json(dict(profile=selected, runtime=manifest.get('runtime',{}).get('runtime_id'),
                code=manifest.get('source_evidence',{}).get('production',{}).get('code_files',[]))))
            parallel=self.repair_parallel_allowed()
            with self._pool_lock:
                if not parallel:
                    self._retire_gpu_locked()
                    while self._active_gpu:self._pool_idle.wait()
                    self._retire_gpu_locked()
                if identity!=self._ane_identity:
                    if self._active_ane:raise RuntimeError('ANE-Warmmodell ist in einer aktiven Ausführung gebunden')
                    self._retire_ane_locked()
                worker=self._ane_worker
            execution=Execution(self.asset_root,manifest,state_root,ane_worker=worker)
            with self._pool_lock:
                created_worker=getattr(getattr(execution,'executor',None),'ane_worker',None)
                if self._ane_worker is None:
                    self._ane_worker=created_worker
                    self._ane_identity=identity if created_worker is not None else None
                elif self._ane_worker is not created_worker:
                    execution.close()
                    raise RuntimeError('ANE-Warmmodell wurde gleichzeitig ersetzt')
            execution.shared_ane_worker=True
            return self._track_execution(execution,'ane')
        if backend=='gpu':
            identity,paths=self._gpu_binding(manifest)
            parallel=self.repair_parallel_allowed()
            with self._pool_lock:
                if not parallel:
                    self._retire_ane_locked()
                    while self._active_ane:self._pool_idle.wait()
                    self._retire_ane_locked()
                if identity!=self._gpu_identity:
                    if self._active_gpu:raise RuntimeError('GPU-Warmmodell ist in einer aktiven Ausführung gebunden')
                    self._retire_gpu_locked()
                    self._gpu_logs=Path(state_root).resolve()/'session-tmp-gpu'
                    self._gpu_sessions=SessionPool(logs_dir=self._gpu_logs)
                    self._gpu_identity=identity
                sessions=self._gpu_sessions
            return self._track_execution(Execution(self.asset_root,manifest,state_root,sessions=sessions,owns_sessions=False),'gpu')
        # The review service runs a single backend at a time. Retire an owned
        # warm GPU pool before starting CPU/ANE to avoid holding both runtimes.
        with self._pool_lock:
            self._retire_gpu_locked();self._retire_ane_locked()
        return Execution(self.asset_root,manifest,state_root)
    def _gpu_binding(self,manifest):
        from manifest_renderer.audio import resolve_manifest_path
        runtime=manifest.get('runtime',{});artifacts=runtime.get('artifacts',{})
        paths=[artifacts[name]['path'] for name in ('isolated_cli','combined_cli') if isinstance(artifacts.get(name),dict) and artifacts[name].get('path')]
        paths.extend(item['path'] for item in manifest.get('models',{}).get('tts',{}).get('files',[]) if item.get('path'))
        files=[];absolute=[]
        for relative in dict.fromkeys(paths):
            path=resolve_manifest_path(self.asset_root,relative);stat=path.stat()
            files.append((str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,getattr(stat,'st_ino',None)))
            absolute.append(str(path))
        identity=sha256(canonical_json(dict(backend='gpu',render_execution=manifest.get('render_execution'),runtime=runtime,models=manifest.get('models'),files=files)))
        return identity,absolute
    def run_prewarm(self,command,stream,manifest,state_root):
        """Run a GPU prewarm through the same model-bound pool used by dialogs."""
        identity,paths=self._gpu_binding(manifest)
        expected=tuple(paths[-2:])
        if (len(expected)!=2 or tuple(command[-2:])!=expected or len(paths)<3 or
                command[0] not in paths[:-2]):
            raise ValueError('GPU-Prewarm stimmt nicht mit den gebundenen Modellen überein')
        if manifest.get('render_execution',{}).get('profile_id')!='gpu':
            raise ValueError('GPU-Prewarm benötigt ein GPU-gebundenes Manifest')
        output_root=Path(state_root).resolve()/manifest['output']['root']
        disk_path=output_root
        while not disk_path.exists():disk_path=disk_path.parent
        self.host_validator('gpu','single',facts=host_facts(disk_path=disk_path))
        if not self.repair_parallel_allowed():
            with self._pool_lock:
                self._retire_ane_locked()
                while self._active_ane:self._pool_idle.wait()
                self._retire_ane_locked()
        with self._pool_lock:
            if identity!=self._gpu_identity:
                if self._active_gpu:raise RuntimeError('GPU-Warmmodell ist in einer aktiven Ausführung gebunden')
                self._retire_gpu_locked()
                self._gpu_logs=Path(state_root).resolve()/'session-tmp-gpu'
                self._gpu_sessions=SessionPool(logs_dir=self._gpu_logs);self._gpu_identity=identity
            sessions=self._gpu_sessions
            self._active_gpu+=1
        return self._active_backend_run('gpu',lambda:sessions.run(command,stream,backend='gpu'),acquired=True)
    def close(self):
        error=None
        with self._pool_lock:
            try:self._retire_gpu_locked()
            except BaseException as exc:error=exc
            try:self._retire_ane_locked()
            except BaseException as exc:error=error or exc
        atexit.unregister(self.close)
        if error is not None:raise error
    def _close_ane_locked(self):
        worker,self._ane_worker=self._ane_worker,None
        self._ane_identity=None
        if worker is not None:
            close=getattr(worker,'close',None)
            if close:close()
    def release_warm_tts(self):
        """Release only synthesis pools; QA uses its separately bound CLI."""
        error=None
        with self._pool_lock:
            try:self._retire_gpu_locked()
            except BaseException as exc:error=exc
            try:self._retire_ane_locked()
            except BaseException as exc:error=error or exc
        if error is not None:raise error


class Execution:
    def __init__(self,assets,manifest,state_root,sessions=None,owns_sessions=None,ane_worker=None):
        root=Path(state_root);run=root/manifest['output']['root'];run.mkdir(parents=True,exist_ok=True)
        self.owns_sessions=(sessions is None) if owns_sessions is None else owns_sessions
        self.sessions=sessions or SessionPool(logs_dir=run/'session-tmp')
        if self.owns_sessions:(run/'session-tmp').mkdir(exist_ok=True)
        self.auditor=ManifestAuditor(root,manifest,run,asset_root=assets)
        self.executor=ManifestExecutor(root,manifest,self.sessions,self.auditor,ane_worker=ane_worker,asset_root=assets)
        self.shared_ane_worker=ane_worker is not None
        self.supports_live_progress=manifest['render_execution']['profile_id']!='ane'
    def set_progress_callback(self,callback):
        if self.supports_live_progress:self.sessions.progress_callback=callback
        return self.supports_live_progress
    def __call__(self,attempt,wav,log):return self.executor(attempt,wav,log)
    def close(self,preserve_ane_worker=False):
        worker=self.executor.ane_worker
        if worker is not None and preserve_ane_worker:
            detach=getattr(self.sessions,'detach_ane_worker',None)
            if detach:detach(worker)
            self.executor.ane_worker=None
        else:self.executor.release_ane_worker()
        if self.owns_sessions:self.sessions.close()


def release_tts_before_qa(facts):
    """Keep TTS resident only when enough RAM is known for concurrent QA."""
    ram=facts.get('system_ram_bytes') if isinstance(facts,dict) else None
    return not isinstance(ram,int) or isinstance(ram,bool) or ram<16*1024**3


def audition_queue_enabled(facts):
    """Expose batch auditions only on hosts with known physical RAM <= 8 GiB."""
    ram=facts.get('system_ram_bytes') if isinstance(facts,dict) else None
    return isinstance(ram,int) and not isinstance(ram,bool) and 0<ram<=8*1024**3
