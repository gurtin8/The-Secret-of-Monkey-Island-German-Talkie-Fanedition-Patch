"""Opt-in sequential sessions for the patched VoxCPM2 CLI.

Each request carries a fresh complete argv. Model loading and feature caches are
scoped to one immutable binary/model pair; generation state never crosses requests.
"""
from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
import subprocess
import time
import tempfile
import threading
import re

PREFIX = "MI1_REQUEST\t"


@dataclass(frozen=True)
class RenderRequest:
    """Complete immutable request passed to a backend-bound CLI worker."""
    text: str
    reference: str
    output: str
    seed: int
    prompt_wav: str | None = None
    prompt_text: str | None = None
    steps: int = 200
    timesteps: int = 10
    cfg: float = 2.0
    temperature: float = 1.0

    def argv(self, cli: str, models: tuple[str, str]) -> list[str]:
        if (self.prompt_wav is None) != (self.prompt_text is None):
            raise ValueError("Prompt-WAV und Prompt-Text müssen gemeinsam gesetzt sein")
        args = [cli, "-t", self.text, "-r", self.reference]
        if self.prompt_wav is not None:
            args += ["--prompt-wav", self.prompt_wav, "--prompt-text", self.prompt_text]
        args += ["--seed", str(self.seed), "-o", self.output,
                 "--steps", str(self.steps), "--timesteps", str(self.timesteps),
                 "--cfg", str(self.cfg), "--temperature", str(self.temperature), *models]
        return args


def request_arguments(command: list[str], models: tuple[str, str]) -> list[str]:
    if tuple(command[-2:]) != models:
        raise ValueError("Request muss dieselben beiden Modelle verwenden")
    args = command[1:-2]
    allowed = {"-t", "-o", "-r", "--prompt-wav", "--prompt-text", "--seed",
               "--steps", "--timesteps", "--cfg", "--temperature"}
    found = set()
    for index in range(0, len(args), 2):
        if index + 1 >= len(args) or args[index] not in allowed or args[index] in found:
            raise ValueError("Unzulässige/unvollständige Requestargumente")
        found.add(args[index])
    if not {"-t", "-o", "--seed"}.issubset(found):
        raise ValueError("Request braucht Text, Ausgabe und Seed")
    for flag, value in (("--steps", "200"), ("--timesteps", "10"), ("--cfg", "2.0"), ("--temperature", "1.0")):
        if flag not in found:
            args = [*args, flag, value]
    return args


class VoxCPM2Session:
    def __init__(self, cli: str, models: tuple[str, str], *, backend: str = "gpu", feature_cache_mb: int = 64, logs_dir=None):
        if backend not in {"gpu", "cpu"}:
            raise ValueError(f"Unbekanntes VoxCPM2-Backend: {backend}")
        self.cli, self.models = cli, models
        self.backend = backend
        self.feature_cache_mb = feature_cache_mb
        self.logs_dir = logs_dir
        self.process = None
        self.count = 0
        self.identity = None
        self.logs = None
        self.log_writer = None
        self.log_reader = None

    def run(self, command: list[str], stream, *, progress_callback=None, resource_guard=None) -> dict:
        if resource_guard: resource_guard()
        args = request_arguments(command, self.models)
        line = "\t".join(arg.encode("utf-8").hex() for arg in args) + "\n"
        if len(line) > 1024 * 1024:
            raise ValueError("Request überschreitet Protokollgrenze")
        identity = tuple((str(path), Path(path).stat().st_size, Path(path).stat().st_mtime_ns,
                          Path(path).stat().st_ino) for path in (self.cli, *self.models))
        if self.identity is not None and identity != self.identity:
            self.close()
            raise ValueError("CLI/Modelldatei während einer geladenen Session verändert")
        self.identity = identity
        started = time.perf_counter()
        cold = self.process is None
        memory_advisory = None
        if cold:
            if self.backend == 'gpu':
                from .hardware import ram_snapshot, GIB
                memory_advisory = ram_snapshot()
                try:
                    model_bytes = sum(Path(model).stat().st_size for model in self.models)
                except OSError:
                    model_bytes = None
                required = model_bytes + GIB if model_bytes is not None else None
                available = memory_advisory['available_ram_estimate_bytes']
                pressure = available < required if available is not None and required is not None else None
                memory_advisory.update(model_file_bytes=model_bytes, estimated_required_bytes=required,
                    reserve_bytes=GIB, estimate_method='gguf-file-sizes-plus-1gib-reserve/1', pressure=pressure,
                    metal_pressure=None,
                    detail='Speicher vor dem Modellladen geprüft; dynamischer Spitzenbedarf ist unbekannt.')
                if progress_callback:progress_callback('memory_check', memory_advisory)
            if progress_callback:progress_callback('loading_model', 'VoxCPM2 lädt CLI/Modelle.')
            self.count = 0
            self.logs = tempfile.TemporaryDirectory(prefix="mi1-session-",dir=self.logs_dir)
            log_path = Path(self.logs.name) / "stderr.log"
            self.log_writer = log_path.open("w", encoding="utf-8")
            self.log_reader = log_path.open("r", encoding="utf-8")
            try:
                backend_args = ["--cpu"] if self.backend == "cpu" else []
                self.process = subprocess.Popen([self.cli, "--requests", "--feature-cache-mb", str(self.feature_cache_mb), *backend_args, *self.models],
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=self.log_writer, text=True, encoding="utf-8", bufsize=1)
            except Exception:
                self.close()
                raise
        if resource_guard:
            try: resource_guard()
            except BaseException:
                self.close()
                raise
        if progress_callback and not cold:
            progress_callback('model_ready', 'VoxCPM2-Modell ist in dieser Session bereits geladen.')
        self.count += 1
        monitor_stop=threading.Event();monitor=None
        if progress_callback and self.log_writer is not None:
            log_path=Path(self.log_writer.name)
            try:offset=log_path.stat().st_size if not cold else 0
            except OSError:offset=0
            def monitor_progress():
                position=offset;tail='';reported_ready=not cold;reported_rendering=False;reported_memory=False
                while not monitor_stop.wait(.15):
                    try:
                        with log_path.open('rb') as source:
                            source.seek(position);raw=source.read(64*1024)
                    except OSError:continue
                    if not raw:continue
                    position+=len(raw)
                    observed=tail+raw.decode('utf-8',errors='replace')
                    if memory_advisory is not None and not reported_memory:
                        match=re.search(r'MI1_MEMORY metal_recommended_bytes=(\d+) metal_available_bytes=(\d+)\r?\n', observed)
                        if match and int(match[1]) > 0:
                            reported_memory=True
                            advisory=dict(memory_advisory,
                                metal_recommended_working_set_bytes=int(match[1]),
                                metal_available_working_set_bytes=int(match[2]),
                                metal_pressure=required > int(match[2]) if required is not None else None)
                            progress_callback('memory_check', advisory)
                    if not reported_ready and 'VoxCPM2Runtime: initialized (' in observed:
                        reported_ready=True
                        progress_callback('model_ready','VoxCPM2 meldet initialisierte Runtime und geladene Modelle.')
                    if not reported_rendering and any(marker in observed for marker in ('  Reference:', 'Generating (combined)', 'Generating...')):
                        reported_rendering=True
                        progress_callback('rendering','VoxCPM2 meldet den Beginn der Generierung.')
                        return
                    tail=observed[-8192:]
            monitor=threading.Thread(target=monitor_progress,daemon=True);monitor.start()
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
            for output in self.process.stdout:
                if output.startswith(PREFIX):
                    monitor_stop.set()
                    if monitor is not None:monitor.join(timeout=.3)
                    self.drain_logs(stream)
                    if output.strip().split("\t") != ["MI1_REQUEST", str(self.count), "0"]:
                        raise RuntimeError("VoxCPM2-Request fehlgeschlagen oder Protokoll abweichend")
                    if resource_guard: resource_guard()
                    return {"session_request": self.count, "cold": cold, "backend": self.backend,
                            "request_seconds": time.perf_counter() - started}
                raise RuntimeError("Unerwartete Daten auf VoxCPM2-Protokollkanal")
            self.drain_logs(stream)
            raise RuntimeError("VoxCPM2-Session ohne Requestabschluss beendet")
        except BaseException:
            monitor_stop.set()
            if monitor is not None:monitor.join(timeout=.3)
            self.close()
            raise

    def drain_logs(self, stream):
        # Separate stderr and stdout: embedded newlines in text cannot spoof a
        # protocol reply. The CLI flushes its log before acknowledging a request.
        if self.log_reader is not None:
            stream.write(self.log_reader.read())
            stream.flush()

    def close(self):
        if self.process is not None:
            process, self.process = self.process, None
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
        if self.log_reader is not None:
            self.log_reader.close()
            self.log_reader = None
        if self.log_writer is not None:
            self.log_writer.close()
            self.log_writer = None
        if self.logs is not None:
            self.logs.cleanup()
            self.logs = None


class SessionPool:
    """Bounded by the two CLI paths and two C++ backends; closed after each job."""
    def __init__(self, *, logs_dir=None):
        self.sessions = {}
        self.logs_dir = logs_dir
        self.ane_worker = None
        self.ane_instances = {}
        self.instance_context = threading.local()
        self.resource_stop = None
        self.borrowed_for_benchmark = threading.Event()
        self.benchmark_resource_reset = threading.Event()
        self.benchmark_monitor_lock = threading.RLock()
        self.progress_callback = None
        self._lock = threading.Lock()
        if logs_dir is not None:Path(logs_dir).mkdir(parents=True,exist_ok=True)

    def set_ane_worker(self, worker):
        instance_id = getattr(self.instance_context, 'instance_id', None)
        if instance_id:
            with self._lock:
                if instance_id in self.ane_instances:
                    raise ValueError('ANE instance already attached')
                self.ane_instances[instance_id] = worker
            return
        if self.ane_worker is not None:
            raise ValueError("ANE worker process already attached to this job")
        self.ane_worker = worker

    def detach_ane_worker(self, worker):
        with self._lock:
            for name, owned in list(self.ane_instances.items()):
                if owned is worker: self.ane_instances.pop(name)
        if self.ane_worker is worker:
            self.ane_worker = None

    def attach_ane_instance(self, instance_id, worker):
        with self._lock:
            if instance_id in self.ane_instances: raise ValueError('ANE instance already attached')
            self.ane_instances[instance_id] = worker

    def close_ane_instance(self, instance_id):
        with self._lock: worker = self.ane_instances.pop(instance_id, None)
        if worker is not None: worker.close()

    def cancel_ane_workers(self, timeout=1):
        with self._lock: workers = list(self.ane_instances.values())
        for worker in workers: worker.cancel(timeout=timeout)

    def cancel_model_sessions(self, timeout=1):
        with self._lock: sessions = list(self.sessions.values())
        for session in sessions:
            process = session.process
            if process is None or process.poll() is not None: continue
            try:
                process.terminate()
                try: process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=timeout)
            except ProcessLookupError:
                pass

    def run(self, command, stream, *, backend="gpu"):
        def resource_guard():
            if self.resource_stop is not None and self.resource_stop.is_set():
                from .adaptive_ane import ResourceGuardError
                raise ResourceGuardError('resource_guard_critical: GPU request interrupted')
        resource_guard()
        key = (command[0], tuple(command[-2:]))
        key = (*key, backend)
        with self._lock:
            if key not in self.sessions:
                if len(self.sessions) >= 4:
                    raise ValueError("Maximal vier Backend/CLI/Modell-Sessions pro Hörtestjob")
                self.sessions[key] = VoxCPM2Session(key[0], key[1], backend=backend, logs_dir=self.logs_dir)
            session = self.sessions[key]
        options = {}
        if self.resource_stop is not None: options['resource_guard'] = resource_guard
        if self.progress_callback is not None: options['progress_callback'] = self.progress_callback
        return session.run(command, stream, **options)

    def close(self):
        first_error = None
        for name in list(self.ane_instances):
            try: self.close_ane_instance(name)
            except BaseException as exc: first_error = first_error or exc
        for session in self.sessions.values():
            try: session.close()
            except BaseException as exc:
                first_error = first_error or exc
        if self.ane_worker is not None:
            worker, self.ane_worker = self.ane_worker, None
            try: worker.close()
            except BaseException as exc:
                first_error = first_error or exc
        self.sessions.clear()
        self.ane_instances.clear()
        if first_error is not None:
            raise first_error
