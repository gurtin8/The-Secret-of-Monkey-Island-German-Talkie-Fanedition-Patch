"""One background setup job using the existing pinned acquisition recipes.

Preview only reads recipes and local file metadata. Bytes are observed from the
actual destination/.part files; unknown Git/archive sizes remain unknown.
"""
from __future__ import annotations
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess
import threading
from .setup_recipes import RECIPES, transfer, checkout, verify
from .local_preparation import preparation_downloads, _fetch_original, _sha


def catalogue(work_root, *, gog_installer=False):
    root = Path(work_root)/'MI1 Generator'
    runtime = json.loads((RECIPES/'runtime.json').read_text())
    paths = {'cmake':'foreign/runtime/cmake.tar.gz', 'vox-source':'foreign/runtime/vox-source',
             'whisper-source':'foreign/runtime/whisper-source', 'ffmpeg-source':'foreign/runtime/ffmpeg-source',
             'scummtr-source':'foreign/runtime/scummtr-source',
             'vox-baselm':'models/voxcpm2/VoxCPM2-BaseLM-F16.gguf',
             'vox-acoustic':'models/voxcpm2/VoxCPM2-Acoustic-F16.gguf',
             'whisper-base':'models/asr/ggml-base.bin','whisper-medium':'models/asr/ggml-medium.bin',
             'ane-wheel':'work/ane_runtime_contract/voxcpmane2-0.1.3b1-py3-none-any.whl'}
    rows = []
    for artifact in runtime['artifacts']:
        aid = artifact['id']
        if aid in ('python','search'): continue  # Python bootstrapped; Search unused
        relative = paths.get(aid)
        if aid.startswith('ane-preview/'): relative = 'models/ane-runtime/preview/'+aid[len('ane-preview/'):] 
        elif aid.startswith('ane-split/'): relative = 'models/ane-runtime/split/'+aid[len('ane-split/'):]
        elif aid.startswith('ane-tokenizer/'): relative = 'models/ane-runtime/tokenizer/'+aid[len('ane-tokenizer/'):] 
        if relative is None: raise ValueError('Unmapped runtime catalogue asset: '+aid)
        rows.append(dict(id=aid,name=aid,path=root/relative,artifact=artifact,kind=artifact['delivery']))
    for aid, artifact in preparation_downloads().items():
        rows.append(dict(id='preparation/'+aid,name=aid,path=root/'foreign/preparation'/artifact['name'],artifact=artifact,kind='original'))
    if gog_installer:
        from .gog_tool import gog_tool_downloads
        for aid, artifact in gog_tool_downloads().items():
            rows.append(dict(id='gog-extractor/'+aid,name=artifact.get('label',aid),
                path=root/'foreign/preparation/gog-extractor'/artifact['name'],artifact=artifact,kind='gog_tool'))
    return rows


class SetupDashboard:
    def __init__(self, root, *, runner=None, operation_lock=None, render_busy=lambda: False, gog_installer=False):
        self.root=Path(root); self.gog_installer=gog_installer
        self.rows=catalogue(root, gog_installer=gog_installer)
        self.operation_lock=operation_lock or threading.RLock(); self.render_busy=render_busy
        self.lock=threading.RLock(); self.thread=None
        self.phase='preview'; self.current=None; self.error=None; self.download_ready=False
        self.installed=False; self.ane_installed=False; self.states={}; self.initial={}; self.logs=deque(maxlen=400)
        self.log_sequence=0; self.command_sequence=0; self.runner=runner or self.run
        self.verified_cache={}

    def update_input(self, gog_installer):
        if type(gog_installer) is not bool: raise ValueError('Ungültige Spielquelle')
        with self.operation_lock:
            if self.busy() or self.render_busy(): raise RuntimeError('Ein Setup- oder Renderauftrag läuft bereits')
            with self.lock:
                self.gog_installer=gog_installer
                self.rows=catalogue(self.root, gog_installer=gog_installer)
                self.phase='preview'; self.current=None; self.error=None
                self.download_ready=False; self.installed=False; self.states={}; self.initial={}

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def log(self, message):
        with self.lock:
            for line in str(message).splitlines():
                self.log_sequence+=1; self.logs.append(dict(sequence=self.log_sequence,text=line[:2000]))

    def set_phase(self, phase, current=None):
        with self.lock: self.phase=phase; self.current=current
        self.log(phase+(' · '+current if current else ''))

    def run(self, command, **kwargs):
        """Drain both pipes continuously; preserve capture_output runner contract."""
        with self.lock:
            self.command_sequence+=1; command_id=self.command_sequence
        command_text=' '.join(map(str,command))
        self.log(f'[RUN #{command_id}] $ '+command_text)
        options=dict(kwargs); check=options.pop('check',False); capture=options.pop('capture_output',False)
        text=options.pop('text',False); options.pop('stdout',None); options.pop('stderr',None)
        hide_response=capture and any(str(arg).startswith('https://ghcr.io/token?') for arg in command)
        try:
            process=subprocess.Popen(command, stdout=subprocess.PIPE,stderr=subprocess.PIPE,**options)
        except OSError as exc:
            self.log(f'[FEHLER #{command_id}] '+str(exc)); raise
        captured=[bytearray(),bytearray()]
        def drain(stream,index):
            while True:
                data=stream.readline(4096)
                if not data: break
                if capture: captured[index].extend(data)
                if not hide_response: self.log(data.decode('utf-8','replace').rstrip())
            stream.close()
        readers=[threading.Thread(target=drain,args=(stream,index),daemon=True) for index,stream in enumerate((process.stdout,process.stderr))]
        for reader in readers: reader.start()
        returncode=process.wait()
        for reader in readers: reader.join()
        self.log(f'[{"OK" if returncode==0 else "FEHLER"} #{command_id}] Exit {returncode} · $ '+command_text)
        outputs=[bytes(value).decode('utf-8','replace') if text else bytes(value) for value in captured]
        result=subprocess.CompletedProcess(command,returncode,outputs[0] if capture else None,outputs[1] if capture else None)
        if check and returncode: raise subprocess.CalledProcessError(returncode,command,output=result.stdout,stderr=result.stderr)
        return result

    def observed(self,row):
        path=row['path']; part=path.with_name(path.name+'.part')
        actual=path if path.exists() else part
        if actual.is_symlink(): return 0
        if actual.is_file(): return actual.stat().st_size
        if row['kind']=='git' and actual.is_dir():
            return sum(p.stat().st_size for p in actual.rglob('*') if p.is_file() and not p.is_symlink())
        return 0

    def verified_file(self,row):
        path=row['path']
        if row['kind']=='git' or not path.is_file() or path.is_symlink(): return False
        stat=path.stat(); signature=(stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        key=str(path)
        if key in self.verified_cache and self.verified_cache[key][0]==signature: return self.verified_cache[key][1]
        try:
            if row['kind'] in ('original','gog_tool'):
                a=row['artifact']; valid=(a.get('bytes') is None or stat.st_size==a['bytes']) and _sha(path)==a['sha256']
            else: verify(path,row['artifact']); valid=True
        except (OSError,ValueError): valid=False
        self.verified_cache[key]=(signature,valid)
        return valid

    def snapshot(self):
        with self.lock:
            packages=[]
            for row in self.rows:
                aid=row['id']; observed=self.observed(row); size=row['artifact'].get('bytes')
                state=self.states.get(aid)
                if state is None:
                    state='existing_unverified' if row['path'].exists() else ('partial' if observed else 'pending')
                reused=min(observed,self.initial.get(aid,0)) if state in ('reused','complete','downloading') else 0
                packages.append(dict(id=aid,name=row['name'],bytes=size,actual_bytes=observed,
                    reused_bytes=reused,downloaded_bytes=max(0,observed-self.initial.get(aid,observed)),
                    remaining_bytes=max(0,size-observed) if size is not None else None,state=state,
                    terms=row['artifact'].get('terms',{}),url=row['artifact'].get('url')))
            cache=self.root/'MI1 Generator/foreign/runtime/ane-pip'
            cache_bytes=sum(p.stat().st_size for p in cache.rglob('*') if p.is_file() and not p.is_symlink()) if cache.is_dir() and not cache.is_symlink() else 0
            cache_reused=min(cache_bytes,self.initial.get('ane-dependencies',cache_bytes))
            packages.append(dict(id='ane-dependencies',name='ANE / PyPI-Abhängigkeiten',bytes=None,actual_bytes=cache_bytes,
                reused_bytes=cache_reused,downloaded_bytes=max(0,cache_bytes-cache_reused),remaining_bytes=None,
                state='complete' if self.ane_installed else ('installing' if self.phase=='ane_install' else 'pending'),terms={},url='https://pypi.org/project/voxcpmane2/0.1.3b1/'))
            done=sum(p['state'] in ('verified','reused','complete') for p in packages)
            return dict(phase=self.phase,current=self.current,busy=self.busy(),error=self.error,gog_installer=self.gog_installer,
                download_ready=self.download_ready,installed=self.installed,packages=packages,
                package_count=len(packages),completed=done,remaining=len(packages)-done,
                known_total_bytes=sum(p['bytes'] or 0 for p in packages),unknown_size_count=sum(p['bytes'] is None for p in packages),
                actual_bytes=sum(p['actual_bytes'] for p in packages),reused_bytes=sum(p['reused_bytes'] for p in packages),
                downloaded_bytes=sum(p['downloaded_bytes'] for p in packages),
                known_remaining_bytes=sum(p['remaining_bytes'] or 0 for p in packages),
                log=list(self.logs),log_sequence=self.log_sequence,
                requirements=['Apple Silicon (M1/M2 und neuer), macOS 14+', 'Command Line Tools für lokale Builds: OS-Dialog selbst bestätigen',
                    'Schnell und Mittel benötigen macOS 15 oder neuer; Langsam nutzt GPU',
                    'Lokale ANE-Bindung wird geprüft; Modelle werden erst beim Renderstart geladen',
                    'Rosetta: Wine/x86_64 OS-Abfrage später selbst bestätigen'],
                ane_status='ANE-Abhängigkeiten installiert' if self.ane_installed else 'ANE-Abhängigkeiten: zusätzliche PyPI-Downloads mit unbekannter Gesamtgröße, eigene Installationsphase',
                tts_started=False,qa_started=False,game_build_started=False)

    def start(self, *, terms_confirmed):
        if terms_confirmed is not True: raise ValueError('Originalbedingungen müssen vor dem gemeinsamen Paketdownload bestätigt werden')
        with self.operation_lock:
            if self.busy() or self.render_busy(): raise RuntimeError('Ein Setup- oder Renderauftrag läuft bereits')
            with self.lock:
                self.error=None; self.download_ready=False; self.installed=False; self.ane_installed=False
                self.states={}; self.initial={row['id']:self.observed(row) for row in self.rows}
                cache=self.root/'MI1 Generator/foreign/runtime/ane-pip'
                self.initial['ane-dependencies']=sum(p.stat().st_size for p in cache.rglob('*') if p.is_file() and not p.is_symlink()) if cache.is_dir() and not cache.is_symlink() else 0
                self.phase='download'; self.thread=threading.Thread(target=self.execute,daemon=True)
                self.thread.start()
            return dict(accepted=True,busy=True)

    def download_package(self, row):
        aid=row['id']
        with self.lock: self.states[aid]='downloading'
        self.log('Download / Prüfung: '+aid)
        try:
            existed=row['path'].is_dir() if row['kind']=='git' else self.verified_file(row)
            if row['kind']=='git': checkout(row['artifact'],row['path'],runner=self.runner)
            elif row['kind']=='original': _fetch_original(row['artifact'],row['path'],self.runner)
            elif row['kind']=='gog_tool':
                from .gog_tool import fetch_gog_tool_artifact
                fetch_gog_tool_artifact(row['artifact'],row['path'],runner=self.runner)
            else: transfer(row['artifact'],row['path'],runner=self.runner)
            with self.lock: self.states[aid]='reused' if existed else 'complete'
            self.log('Geprüft: '+aid)
        except Exception:
            with self.lock: self.states[aid]='failed'
            raise

    def download_packages(self):
        self.set_phase('download', 'bis zu 3 Pakete gleichzeitig')
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending={pool.submit(self.download_package,row) for row in self.rows}
            try:
                for future in as_completed(pending): future.result()
            except Exception:
                for future in pending: future.cancel()
                raise

    def execute(self):
        try:
            self.download_packages()
            self.download_ready=True; self.set_phase('download_ready')
            from .runtime_setup import install_runtime
            from .local_preparation import acquire_preparation_sources
            from manifest_renderer.production import atomic_json
            install_runtime(self.root/'MI1 Generator',runner=self.runner,phase=self.set_phase)
            self.set_phase('extract','Originalpakete')
            result=acquire_preparation_sources(self.root,terms_confirmed=True,runner=self.runner)
            sources_path=self.root/'MI1 Generator/.launcher/preparation-sources.json'
            atomic_json(sources_path, result)
            if self.gog_installer:
                from .gog_tool import acquire_gog_extractor
                self.set_phase("extract", "GOG-Extraktionswerkzeug")
                acquire_gog_extractor(self.root,runner=self.runner)
            self.set_phase('ane_install','PyPI-Abhängigkeiten (Gesamtgröße unbekannt)')
            from tools.setup_ane_runtime import install_venv
            python_home=self.root/'MI1 Generator/foreign/bootstrap/python'
            python_paths=list(python_home.rglob('bin/python3.12'))
            if len(python_paths)!=1: raise ValueError('Gebootstrapptes Original-Python fehlt oder ist mehrdeutig')
            install_venv(self.root/'MI1 Generator',python=python_paths[0],
                wheel=self.root/'MI1 Generator/work/ane_runtime_contract/voxcpmane2-0.1.3b1-py3-none-any.whl',
                cache=self.root/'MI1 Generator/foreign/runtime/ane-pip',constraints=RECIPES/'ane-constraints.txt',runner=self.runner)
            from .local_ane import resolve_local_ane
            resolve_local_ane(self.root/'MI1 Generator',runner=self.runner)
            self.ane_installed=True; self.installed=True
            self.set_phase('awaiting_user','Rosetta bei Bedarf bestätigen; Renderbelege separat prüfen')
        except Exception as exc:
            with self.lock: self.error=str(exc)
            with self.lock:
                if self.current in self.states: self.states[self.current]='failed'
            self.set_phase('failed'); self.log(str(exc))
