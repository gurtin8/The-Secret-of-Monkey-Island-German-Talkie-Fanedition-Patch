"""Safe, local import of user supplied voice anchor audio."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import wave
from pathlib import Path
from typing import Callable, Sequence

SUPPORTED_LANGUAGES = frozenset({"en", "de"})
SUPPORTED_SUFFIXES = frozenset({".wav", ".flac", ".ogg"})
MIN_DURATION_SECONDS = 4.0
MAX_DURATION_SECONDS = 15.0


class AnchorImportError(ValueError):
    """Input audio or metadata could not be imported safely."""


Runner = Callable[..., subprocess.CompletedProcess]


def _run(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), shell=False, check=True, capture_output=True, **kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if not value:
        raise AnchorImportError("Rollenname muss mindestens ein ASCII-Zeichen enthalten.")
    return value[:80].rstrip("-")


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        frames, rate = audio.getnframes(), audio.getframerate()
        if rate <= 0:
            raise AnchorImportError("Ungültige WAV-Samplerate.")
        return frames / rate


def _probe_duration(source: Path, runner: Runner) -> float:
    if source.suffix.lower() == ".wav":
        try:
            return _wav_duration(source)
        except (wave.Error, EOFError, OSError) as error:
            raise AnchorImportError(f"WAV-Datei kann nicht gelesen werden: {error}") from error
    command = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(source)]
    try:
        result = runner(command, text=True)
        duration = float(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as error:
        raise AnchorImportError(f"Audiodauer konnte nicht ermittelt werden: {error}") from error
    if not (duration > 0):
        raise AnchorImportError("Audiodatei hat keine gültige Dauer.")
    return duration


def import_custom_anchor(
    data_root: Path | str,
    role: str,
    source_path: Path | str,
    name: str,
    language: str,
    transcript: str,
    *,
    runner: Runner = _run,
    import_id_factory: Callable[[], str] | None = None,
    ffmpeg: Path | str = "ffmpeg",
    ffprobe: Path | str = "ffprobe",
    minimum_duration_seconds: float = MIN_DURATION_SECONDS,
) -> dict:
    """Normalize and atomically store an anchor; return its metadata record.

    ``runner`` receives subprocess-style arguments, making ffprobe/ffmpeg
    invocations replaceable in tests. The source is only read; all output is
    created below ``data_root/custom-anchors``.
    """
    source = Path(source_path).expanduser()
    if not source.is_file():
        raise AnchorImportError("Quelldatei muss eine vorhandene lokale Datei sein.")
    if source.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise AnchorImportError("Unterstützte Formate sind WAV, FLAC und OGG.")
    if not name.strip():
        raise AnchorImportError("Ankername darf nicht leer sein.")
    if language not in SUPPORTED_LANGUAGES:
        raise AnchorImportError("Sprache muss en oder de sein.")
    if not transcript.strip():
        raise AnchorImportError("Transkript darf nicht leer sein.")

    if source.suffix.lower() == ".wav":
        duration = _probe_duration(source, runner)
    else:
        command = [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(source)]
        try:
            result = runner(command, text=True)
            duration = float(result.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as error:
            raise AnchorImportError(f"Audiodauer konnte nicht ermittelt werden: {error}") from error
    if not 0 < minimum_duration_seconds <= MAX_DURATION_SECONDS:
        raise AnchorImportError("Ungültige minimale Audiodauer.")
    if not minimum_duration_seconds <= duration <= MAX_DURATION_SECONDS:
        raise AnchorImportError(f"Audiodauer muss zwischen {minimum_duration_seconds:g} und 15 Sekunden liegen.")

    root = Path(data_root).expanduser() / "custom-anchors" / _slug(role)
    root.mkdir(parents=True, exist_ok=True)
    import_id = (import_id_factory or (lambda: uuid.uuid4().hex))()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", import_id):
        raise AnchorImportError("Ungültige Import-ID.")
    destination = root / import_id
    if destination.exists():
        raise AnchorImportError("Import-ID existiert bereits.")
    stage = Path(tempfile.mkdtemp(prefix=f".{import_id}-", dir=root))
    try:
        wav_path = stage / "anchor.wav"
        command = [str(ffmpeg), "-nostdin", "-v", "error", "-i", str(source), "-map_metadata", "-1",
                   "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", "-f", "wav", str(wav_path)]
        try:
            runner(command)
        except (OSError, subprocess.SubprocessError) as error:
            raise AnchorImportError(f"Normalisierung mit ffmpeg fehlgeschlagen: {error}") from error
        if not wav_path.is_file() or wav_path.stat().st_size == 0:
            raise AnchorImportError("ffmpeg hat keine WAV-Ausgabe erzeugt.")
        normalized_duration = _wav_duration(wav_path)
        if not minimum_duration_seconds <= normalized_duration <= MAX_DURATION_SECONDS:
            raise AnchorImportError(f"Normalisierte Audiodauer liegt außerhalb von {minimum_duration_seconds:g}–15 Sekunden.")
        metadata = {
            "format": "mi1-custom-voice-anchor/1",
            "import_id": import_id,
            "role": role,
            "name": name.strip(),
            "language": language,
            "transcript": transcript,
            "original_filename": source.name,
            "sha256": _sha256(wav_path),
            "duration_seconds": normalized_duration,
            "audio_path": "anchor.wav",
            "audio_format": "wav",
            "sample_rate_hz": 48000,
            "channels": 1,
            "sample_format": "pcm_s16le",
        }
        metadata_path = stage / "metadata.json"
        with metadata_path.open("x", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, destination)
        return {**metadata, "directory": str(destination), "audio_path": str(destination / "anchor.wav"),
                "metadata_path": str(destination / "metadata.json")}
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
