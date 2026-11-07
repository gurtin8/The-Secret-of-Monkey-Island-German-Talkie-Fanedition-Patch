"""Content-addressed preparation cache; no synthesized/QA artifact is cached here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import wave

from .audio import atomic_install, sha256_file

PREPARATION_VERSION = "mi1-audio-preparation/1"


def pcm_info(path: Path, rate: int | None = 48000) -> dict:
    with wave.open(str(path), "rb") as wav:
        channels, width, actual_rate, frames = (wav.getnchannels(), wav.getsampwidth(),
                                                wav.getframerate(), wav.getnframes())
        if (channels, width) != (1, 2) or actual_rate <= 0 or frames <= 0 or (rate and actual_rate != rate):
            raise ValueError(f"Ungültige vorbereitete Mono-PCM16-WAV: {path}")
        if len(wav.readframes(frames)) != frames * 2:
            raise ValueError(f"Unvollständige WAV: {path}")
    return {"channels": channels, "sample_width": width, "sample_rate_hz": actual_rate,
            "frames": frames, "sha256": sha256_file(path)}


class AudioPreparationCache:
    """Immutable entries installed as complete directories; corruption fails closed.

    FFmpeg bytes bind conversion identity, including builds with the same version
    string. Producers remain injectable for model-free tests and legacy conversion.
    """
    def __init__(self, root: Path, ffmpeg: Path):
        self.root, self.ffmpeg = Path(root), Path(ffmpeg)
        self.events: list[dict] = []

    def prepare(self, operation: str, sources: list[Path], target: Path, producer,
                *, rate: int | None = 48000, join_ms: int = 0) -> dict:
        started = time.perf_counter()
        identity = {"version": PREPARATION_VERSION, "operation": operation,
                    "sources": [sha256_file(path) for path in sources],
                    "format": {"channels": 1, "sample_width": 2, "rate": rate or "source"},
                    "join_ms": join_ms, "ffmpeg_sha256": sha256_file(self.ffmpeg)}
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        import hashlib
        key = hashlib.sha256(encoded).hexdigest()
        entry = self.root / key
        self.root.mkdir(parents=True, exist_ok=True)
        if entry.is_symlink():
            raise ValueError(f"Symlink im Vorbereitungscache: {entry}")
        hit = entry.exists()
        if not hit:
            with tempfile.TemporaryDirectory(prefix=".prepare-", dir=self.root) as folder:
                staging = Path(folder) / "entry"
                staging.mkdir()
                output = staging / "audio.wav"
                producer(output)
                info = pcm_info(output, rate)
                # Reject input changes during conversion before publishing the entry.
                if identity["sources"] != [sha256_file(path) for path in sources] or identity["ffmpeg_sha256"] != sha256_file(self.ffmpeg):
                    raise ValueError("Vorbereitungseingabe wurde während der Konvertierung verändert")
                (staging / "entry.json").write_text(json.dumps({"identity": identity, "audio": info}), encoding="utf-8")
                try:
                    os.rename(staging, entry)
                except OSError:
                    if not entry.is_dir():
                        raise
                    # A concurrent writer must have published this same complete entry.
        try:
            if (entry / "entry.json").is_symlink() or (entry / "audio.wav").is_symlink():
                raise ValueError("Symlink im Cacheeintrag")
            record = json.loads((entry / "entry.json").read_text(encoding="utf-8"))
            if record["identity"] != identity or pcm_info(entry / "audio.wav", rate) != record["audio"]:
                raise ValueError("Cacheidentität oder Audio abweichend")
        except (OSError, ValueError, KeyError, wave.Error, EOFError) as exc:
            raise ValueError(f"Vorbereitungscache beschädigt/unvollständig; bleibt erhalten: {entry}") from exc
        # Copy, never hard-link: callers cannot mutate the cache via their target.
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".cached-", suffix=".wav", dir=target.parent, delete=False) as tmp:
            temporary = Path(tmp.name)
        try:
            shutil.copyfile(entry / "audio.wav", temporary)
            atomic_install(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        event = {"operation": operation, "key": key, "hit": hit,
                 "preparation_seconds": time.perf_counter() - started}
        self.events.append(event)
        return event
