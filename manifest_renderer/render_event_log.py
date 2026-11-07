"""Append-only run event log with strict resume validation."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .manifest import canonical_json, sha256
from .runner import ArtifactError


class RenderEventLog:
    EVENTS = {
        "run_header", "run_resume", "partial_tail_recovered", "attempt_bound",
        "render_started", "render_finished", "render_error",
        "qa_started", "qa_finished", "qa_error", "dialogue_completed",
        "qa_resume_compatibility",
        "ane_instance_started", "ane_instance_retired", "ane_role_transferred",
        "adaptive_ane_status", "resource_guard", "tts_models_released",
    }

    def __init__(self, path: Path, identity: dict):
        self.path = path
        self.identity = dict(identity)
        self._lock = threading.RLock()
        self._session_id = uuid.uuid4().hex
        self._started = time.monotonic()
        self._sequence = -1
        self.bindings: dict[str, dict] = {}
        self.role_instances = {}
        self.unfinished = {}
        if path.is_symlink():
            raise ArtifactError("Renderereignislog ist ein Symlink")
        partial = self._load_existing()
        if self._sequence == -1:
            self.append("run_header", log_version="1", identity=self.identity)
        else:
            self.append("run_resume", prior_event_count=self._sequence + 1)
        if partial is not None:
            self.append("partial_tail_recovered", **partial)

    @staticmethod
    def _decode_object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def _load_existing(self):
        if not self.path.exists():
            return None
        if not self.path.is_file():
            raise ArtifactError("Renderereignislog ist keine reguläre Datei")
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise ArtifactError("Renderereignislog nicht lesbar") from exc
        boundary = raw.rfind(b"\n") + 1
        partial_bytes = raw[boundary:] if boundary < len(raw) else b""
        complete = raw[:boundary]
        if partial_bytes:
            digest = hashlib.sha256(partial_bytes).hexdigest()
            sidecar = self.path.with_name(self.path.name + f".partial-{digest}.bin")
            try:
                if sidecar.exists():
                    if sidecar.is_symlink() or sidecar.read_bytes() != partial_bytes:
                        raise ArtifactError("Sicherung des unvollständigen Renderlog-Endes weicht ab")
                else:
                    fd = os.open(sidecar, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    try:
                        self._write_all(fd, partial_bytes)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                fd = os.open(self.path, os.O_WRONLY)
                try:
                    os.ftruncate(fd, boundary)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise ArtifactError("Unvollständiges Renderlog-Ende konnte nicht gesichert/wiederhergestellt werden") from exc
            recovery = {"partial_bytes": len(partial_bytes), "partial_sha256": digest,
                        "partial_backup": sidecar.name}
        else:
            recovery = None
        rows = []
        for index, line in enumerate(complete.splitlines()):
            if not line:
                raise ArtifactError(f"Renderereignislog enthält leere vollständige Zeile {index + 1}")
            try:
                row = json.loads(line, object_pairs_hook=self._decode_object_pairs)
            except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise ArtifactError(f"Renderereignislog-Zeile {index + 1} ist beschädigt") from exc
            rows.append(row)
        for expected_sequence, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("sequence") != expected_sequence:
                raise ArtifactError("Renderereignislog-Sequenz ist beschädigt")
            if row.get("event") not in self.EVENTS or row.get("identity") != self.identity:
                raise ArtifactError("Renderereignislog enthält unbekannte Ereignisse oder abweichende Runidentität")
            if (not isinstance(row.get("session_id"), str) or not row["session_id"] or
                    not isinstance(row.get("utc"), str) or
                    not isinstance(row.get("elapsed_seconds"), (int, float)) or
                    isinstance(row.get("elapsed_seconds"), bool) or row["elapsed_seconds"] < 0):
                raise ArtifactError(f"Renderereignislog-Zeile {expected_sequence + 1} hat ungültige Zeit-/Sessionfelder")
            try:
                datetime.fromisoformat(row["utc"])
            except ValueError as exc:
                raise ArtifactError(f"Renderereignislog-Zeile {expected_sequence + 1} hat ungültige UTC-Zeit") from exc
            if expected_sequence == 0 and row.get("event") != "run_header":
                raise ArtifactError("Renderereignislog beginnt nicht mit einem Runheader")
            event=row["event"]
            if event in {'render_started','ane_role_transferred'} and row.get('backend')=='ane' and row.get('instance_id') and row.get('role_id'):
                self.role_instances[row['role_id']]=row['instance_id']
            if event=="run_header" and row.get("log_version")!="1":
                raise ArtifactError("Renderereignislogversion unbekannt")
            if event in {"render_started","render_finished","render_error","qa_started","qa_finished","qa_error"}:
                if (not isinstance(row.get("attempt_fingerprint"),str) or
                        not isinstance(row.get("dialog_id"),str) or
                        not isinstance(row.get("backend"),str)):
                    raise ArtifactError(f"Renderereignislog-Zeile {expected_sequence + 1} fehlen Versuchsdaten")
                if event.endswith("_error") and not isinstance(row.get("error_type"),str):
                    raise ArtifactError(f"Renderereignislog-Zeile {expected_sequence + 1} enthält keinen Fehlertyp")
                if event.endswith("_finished") and not isinstance(row.get("duration_seconds"),(int,float)):
                    raise ArtifactError(f"Renderereignislog-Zeile {expected_sequence + 1} enthält keine Laufzeit")
            if event=="dialogue_completed" and (not isinstance(row.get("dialog_id"),str) or
                    row.get("qa_status") not in {"pass","warn","fail","not_required_original"}):
                raise ArtifactError(f"Renderereignislog-Zeile {expected_sequence + 1} hat ungültigen Cueausgang")
            if row.get("event") == "attempt_bound":
                binding = row.get("binding")
                fingerprint = row.get("attempt_fingerprint")
                if (not isinstance(binding, dict) or not isinstance(fingerprint, str) or
                        binding.get("attempt_fingerprint") != fingerprint or
                        binding.get("manifest_sha256") != self.identity["manifest_sha256"] or
                        row.get("backend") != binding.get("profile_id") or
                        row.get("dialog_id") != binding.get("dialog_id") or
                        row.get("seed") != binding.get("seed") or
                        row.get("role_id") != binding.get("role_id")):
                    raise ArtifactError(f"Renderereignislog-Binding in Zeile {expected_sequence + 1} ist beschädigt")
                record = {key: value for key, value in binding.items() if key != "binding_sha256"}
                if binding.get("binding_sha256") != sha256(canonical_json(record)):
                    raise ArtifactError(f"Renderereignislog-Bindinghash in Zeile {expected_sequence + 1} ist ungültig")
                prior = self.bindings.get(fingerprint)
                if prior is not None and prior != binding:
                    raise ArtifactError("Renderereignislog enthält widersprüchliche Attempt-Zuordnungen")
                self.bindings[fingerprint] = binding
            self._track_attempt(event,row)
        self._sequence = len(rows) - 1
        return recovery

    def _track_attempt(self,event,row):
        did=row.get('dialog_id')
        if event in {'render_started','render_finished','qa_started'}:
            self.unfinished[did]={'attempt_fingerprint':row['attempt_fingerprint'],
                'stage':'render' if event=='render_started' else 'qa'}
        elif event in {'qa_finished','render_error','qa_error','dialogue_completed'}:
            self.unfinished.pop(did,None)

    @staticmethod
    def _write_all(fd, data: bytes):
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short render event write")
            view = view[written:]

    def append(self, event: str, **details):
        if event not in self.EVENTS:
            raise ValueError(f"unknown render event: {event}")
        with self._lock:
            row = {
                "sequence": self._sequence + 1,
                "event": event,
                "session_id": self._session_id,
                "utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.monotonic() - self._started, 6),
                "identity": self.identity,
                **details,
            }
            payload = (json.dumps(row, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")) + "\n").encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                try:
                    self._write_all(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise ArtifactError("Renderereignis konnte nicht dauerhaft angehängt werden") from exc
            self._sequence += 1
            self._track_attempt(event,row)
            if event in {'render_started','ane_role_transferred'} and details.get('backend')=='ane' and details.get('instance_id') and details.get('role_id'):
                self.role_instances[details['role_id']]=details['instance_id']
            if event == "attempt_bound":
                self.bindings[details["attempt_fingerprint"]] = details["binding"]
            return row
