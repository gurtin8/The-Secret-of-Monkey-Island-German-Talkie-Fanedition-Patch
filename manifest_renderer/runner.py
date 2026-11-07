"""Resumable attempt storage and QA-driven render state machine."""
from __future__ import annotations

import json
import os
import tempfile
import wave
from pathlib import Path
from typing import Callable

from .audio import atomic_install, resolve_manifest_path, sha256_file, validate_wav


class RenderOutputError(RuntimeError, ValueError):
    """This attempt produced missing/invalid audio; unrelated artifacts are intact."""


class ArtifactError(RuntimeError):
    """An existing artifact is incomplete, foreign, or has changed bytes."""


def _exclusive_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".report-", suffix=".json", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        atomic_install(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AttemptStore:
    """Own fingerprint-addressed WAV/report artifacts beneath one package root."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)

    def _paths(self, attempt: dict) -> tuple[Path, Path, Path]:
        artifacts = attempt["artifacts"]
        return tuple(resolve_manifest_path(self.root, artifacts[key])
                     for key in ("wav", "log", "report"))

    def cached(self, attempt: dict) -> dict | None:
        wav, log, report = self._paths(attempt)
        if not wav.exists() and not report.exists():
            return None
        if not wav.is_file() or not report.is_file() or wav.is_symlink() or report.is_symlink():
            raise ArtifactError("WAV und Report müssen gemeinsam als reguläre Dateien vorliegen")
        try:
            record = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactError(f"Versuchsreport ist unlesbar: {report}") from exc
        if record.get("fingerprint") != attempt["fingerprint"]:
            raise ArtifactError("Versuchsreport gehört zu einem anderen Fingerprint")
        actual = sha256_file(wav)
        if record.get("wav_sha256") != actual:
            raise ArtifactError("WAV-Hash stimmt nicht mit dem Versuchsreport überein")
        if not log.is_file() or log.is_symlink() or record.get("log_sha256") != sha256_file(log):
            raise ArtifactError("Renderlog fehlt oder stimmt nicht mit dem Versuchsreport überein")
        if record.get("qa_status") not in {"pass", "warn", "fail"}:
            raise ArtifactError("Versuchsreport enthält keinen abgeschlossenen QA-Status")
        if not (record.get("qa_status") == "fail" and record.get("technical_status") == "fail"):
            validate_wav(wav)
        return record

    def run(self, attempt: dict,
            executor: Callable[[dict, Path, Path], dict]) -> dict:
        """Run one attempt through an injected CLI/auditor or reuse exact output.

        The executor writes the requested temporary WAV and log and returns an
        audit mapping containing at least qa_status.
        """
        existing = self.cached(attempt)
        if existing is not None:
            return {**existing, "resumed": True}
        wav, log, report = self._paths(attempt)
        if log.exists():
            raise ArtifactError(f"Fremdes oder unvollständiges Log wird nicht überschrieben: {log}")
        wav.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".attempt-", dir=wav.parent) as folder:
            temporary_wav = Path(folder) / "output.wav"
            temporary_log = Path(folder) / "render.log"
            audit = executor(attempt, temporary_wav, temporary_log)
            if audit.get("qa_status") not in {"pass", "warn", "fail"}:
                raise ValueError("Auditor muss qa_status pass, warn oder fail liefern")
            try:
                info = validate_wav(temporary_wav)
            except (ValueError, OSError, EOFError, wave.Error) as exc:
                if isinstance(exc,OSError) and not isinstance(exc,FileNotFoundError): raise
                if audit.get("qa_status") != "fail" or audit.get("technical_status") != "fail" or not temporary_wav.is_file():
                    raise RenderOutputError(str(exc)) from exc
                # Preserve and resume a technically failed variant; its exact bytes
                # and completed failure audit are still required.
                info = {"sha256": sha256_file(temporary_wav)}
            if not temporary_log.is_file():
                raise ValueError("Renderer muss für jeden Versuch ein getrenntes Log schreiben")
            atomic_install(temporary_wav, wav)
            log_hash = sha256_file(temporary_log)
            atomic_install(temporary_log, log)
            record = {"fingerprint": attempt["fingerprint"],
                      "dialog_id": attempt["dialog_id"], "method": attempt["method"],
                      "seed": attempt["seed"], "render_status": "ok",
                      "wav_sha256": info["sha256"], "log_sha256": log_hash, **audit}
            if attempt.get("render_execution") is not None:
                record["render_execution"] = attempt["render_execution"]
            try:
                _exclusive_json(report, record)
            except FileExistsError as exc:
                raise ArtifactError(f"Versuchsreport existiert bereits: {report}") from exc
            return {**record, "resumed": False}


def run_dialogue(dialogue: dict, store: AttemptStore,
                 executor: Callable[[dict, Path, Path], dict]) -> dict:
    """Execute two standard seeds, then one pass per escalation after two fails."""
    if dialogue["processing"] == "copy_original":
        return {"dialog_id": dialogue["dialog_id"], "processing": "copy_original",
                "selected": {"source_audio": dialogue["source_audio"]}, "attempts": []}
    attempts = dialogue["attempts"]
    if len(attempts) < 2 or any(item["method"] != "standard" for item in attempts[:2]):
        raise ValueError("Plan muss mit genau zwei Standardversuchen beginnen")
    results = []
    for index, attempt in enumerate(attempts):
        if index >= 2 and any(item["qa_status"] != "fail" for item in results[:2]):
            break
        result = store.run(attempt, executor)
        results.append(result)
        if result["qa_status"] in {"pass", "warn"}:
            return {"dialog_id": dialogue["dialog_id"], "processing": "render",
                    "selected": {"fingerprint": attempt["fingerprint"],
                                 "qa_status": result["qa_status"]}, "attempts": results}
    return {"dialog_id": dialogue["dialog_id"], "processing": "render",
            "selected": None, "qa_status": "fail", "attempts": results}
