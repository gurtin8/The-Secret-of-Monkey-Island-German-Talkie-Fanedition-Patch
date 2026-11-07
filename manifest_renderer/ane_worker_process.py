"""Run and control the optional ANE runtime in a persistent subprocess."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from .ane_worker import AneWorker, serve, _path_snapshot, _PATH_DIGEST_CACHE

PROTOCOL = "MI1_ANE_REQUEST"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="JSON with hash-bound local model paths")
    parser.add_argument("--skip-content-hashes", action="store_true")
    args = parser.parse_args(argv)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            with open(args.config, encoding="utf-8") as stream:
                bindings=json.load(stream)
                for path,snapshot in bindings.pop("_verified_paths",{}).items():
                    if _path_snapshot(Path(path))!=snapshot:
                        raise ValueError("ANE component changed after parent verification: "+path)
                worker = AneWorker.from_bindings(bindings, verify_hashes=not args.skip_content_hashes)
    except Exception as exc:
        sys.stderr.write(f"ANE worker startup failed: {type(exc).__name__}: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        return 2
    cancel_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: cancel_event.set())
    sys.stdout.write(json.dumps({"protocol": PROTOCOL, "status": "ready"}) + "\n")
    sys.stdout.flush()
    serve(worker, sys.stdin, sys.stdout, cancel_event=cancel_event)
    return 0


class AneWorkerProcess:
    """Persistent JSON-lines client with a startup handshake and cancellation."""
    def __init__(self, bindings: dict, *, logs_dir: str | Path, cwd: str | Path,
                 command: list[str] | None = None, startup_timeout: float = 1800, verify_hashes: bool = True,
                 resource_guard=None):
        self.resource_guard = resource_guard
        bound_command=bindings.get("worker_command")
        if bound_command is not None and (not isinstance(bound_command,list) or
                len(bound_command)!=5 or bound_command[1:]!=["-m","manifest_renderer.ane_worker_process","--config","{config}"] or
                not isinstance(bound_command[0],str) or not Path(bound_command[0]).is_absolute() or not Path(bound_command[0]).is_file()):
            raise ValueError("Invalid bound ANE worker command")
        Path(logs_dir).mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="mi1-ane-process-", dir=logs_dir)
        work = Path(self.directory.name)
        config_path = work / "config.json"
        # Reuse only parent-verified hashes whose file identities still match.
        # The private launch file carries stat evidence checked again by main().
        verified_paths={}
        receipts=[*bindings.get("model_paths",{}).values(),bindings.get("tokenizer")]
        parent_verified=bool(bindings.get("model_paths")) and bool(bindings.get("tokenizer"))
        for receipt in receipts:
            for item in receipt if isinstance(receipt,list) else [receipt]:
                if not item:
                    parent_verified=False;continue
                path=Path(bindings["model_root"])/item["path"]
                snapshot=_path_snapshot(path)
                cached=_PATH_DIGEST_CACHE.get(str(path.absolute()))
                if not cached or cached!=(snapshot,item["sha256"]): parent_verified=False
                verified_paths[str(path.absolute())]=snapshot
        launch_bindings=dict(bindings)
        if parent_verified: launch_bindings["_verified_paths"]=verified_paths
        config_path.write_text(json.dumps(launch_bindings, ensure_ascii=False), encoding="utf-8")
        self.stderr = (work / "stderr.log").open("w", encoding="utf-8")
        argv = command or (bound_command[:4]+[str(config_path)] if bound_command else None) or [sys.executable, "-m", "manifest_renderer.ane_worker_process",
                           "--config", str(config_path)]
        if command is None and (not verify_hashes or parent_verified): argv.append("--skip-content-hashes")
        environment=dict(os.environ)
        package_root = str(Path(__file__).resolve().parents[1])
        search_path = [package_root, str(Path(cwd).resolve())]
        if environment.get("PYTHONPATH"):
            search_path.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(search_path)
        try:
            self.process = subprocess.Popen(argv, cwd=str(cwd), env=environment, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=self.stderr, bufsize=0)
        except BaseException:
            self.stderr.close(); self.directory.cleanup()
            raise
        self._lock = threading.Lock()
        self.startup_stdout_noise = []
        self._stdout_buffer = bytearray()
        try:
            deadline = time.monotonic() + startup_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("ANE worker startup handshake timed out")
                ready_line = self._readline(remaining)
                if not ready_line:
                    detail = self._startup_error({"status": "eof"})
                    if self.startup_stdout_noise:
                        detail += "\nUnexpected worker stdout before handshake: " + repr(self.startup_stdout_noise)
                    raise RuntimeError(detail)
                try:
                    message = json.loads(ready_line)
                except json.JSONDecodeError:
                    # Native CoreML startup diagnostics can bypass Python's
                    # redirect_stdout and appear on the protocol pipe. Keep
                    # them as diagnostics and wait for the exact JSON handshake.
                    self.startup_stdout_noise.append(ready_line.rstrip("\r\n"))
                    continue
                if message.get("protocol") == PROTOCOL and message.get("status") == "ready":
                    for noise in self.startup_stdout_noise:
                        print(f"ANE worker startup stdout diagnostic: {noise}", file=sys.stderr, flush=True)
                    break
                raise RuntimeError(self._startup_error(message))
        except BaseException:
            self.close()
            raise

    def _readline(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            if self.resource_guard is not None:
                try: self.resource_guard()
                except BaseException:
                    self.cancel(timeout=.2)
                    raise
            newline = self._stdout_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._stdout_buffer[:newline + 1])
                del self._stdout_buffer[:newline + 1]
                return line.decode("utf-8")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ANE worker startup handshake timed out")
            ready, _, _ = select.select([self.process.stdout], [], [], min(remaining,.5) if self.resource_guard else remaining)
            if not ready:
                if self.resource_guard: continue
                raise TimeoutError("ANE worker startup handshake timed out")
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                if self._stdout_buffer:
                    line = bytes(self._stdout_buffer)
                    self._stdout_buffer.clear()
                    return line.decode("utf-8")
                return ""
            self._stdout_buffer.extend(chunk)

    def _startup_error(self, message):
        self.process.poll()
        self.stderr.flush()
        log = Path(self.directory.name) / "stderr.log"
        detail = log.read_text(encoding="utf-8") if log.exists() else ""
        return detail.strip() or f"ANE worker startup failed: {message}"

    def run(self, request: dict) -> dict:
        with self._lock:
            if self.process.poll() is not None:
                raise RuntimeError(self._startup_error({"status": "exited"}))
            self.process.stdin.write((json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            self.process.stdin.flush()
            line = self._readline(3600)
            if not line:
                raise RuntimeError(self._startup_error({"status": "eof"}))
            response = json.loads(line)
            if response.get("protocol") != PROTOCOL or response.get("request_id") != request.get("request_id"):
                self.close()
                raise RuntimeError("ANE worker protocol response did not match request")
            if response.get("status") != "ok":
                error = response.get("error", "ANE worker request failed")
                self.close()
                error_type=response.get('error_type') or str(error).split(':',1)[0]
                if error_type=='MemoryError': raise MemoryError(error)
                if error_type in {'AneCancelled','InterruptedError'}: raise InterruptedError(error)
                if error_type in {'ValueError','ArtifactError','FileNotFoundError','PermissionError'}:
                    from .runner import ArtifactError
                    raise ArtifactError(error)
                raise RuntimeError(error)
            return response["result"]

    def cancel(self, timeout: float = 30):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait()

    def close(self):
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            try:
                process.stdin.close(); process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
        if process is not None and process.stdout:
            process.stdout.close()
        if getattr(self, "stderr", None): self.stderr.close()
        if getattr(self, "directory", None):
            self.directory.cleanup(); self.directory = None


if __name__ == "__main__":
    raise SystemExit(main())
