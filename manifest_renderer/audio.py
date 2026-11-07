"""Safe audio file helpers shared by manifest render paths."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Sequence

RATE = 48_000


def resolve_manifest_path(root: Path, relative: str | Path) -> Path:
    """Resolve a manifest-relative path and reject traversal and symlink escapes."""
    root = Path(root).resolve(strict=True)
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(part in ("..", "") for part in rel.parts):
        raise ValueError(f"Manifestpfad muss relativ und ohne '..' sein: {relative}")
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Symlink im Manifestpfad ist nicht zulässig: {relative}")
    result = (root / rel).resolve(strict=False)
    if not result.is_relative_to(root):
        raise ValueError(f"Manifestpfad verlässt die Manifestwurzel: {relative}")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_wav(path: Path) -> dict:
    with wave.open(str(path), "rb") as wav:
        info = {"channels": wav.getnchannels(), "sample_width": wav.getsampwidth(),
                "sample_rate_hz": wav.getframerate(), "frames": wav.getnframes()}
    if (info["channels"], info["sample_width"], info["sample_rate_hz"]) != (1, 2, RATE) or info["frames"] < 1:
        raise ValueError(f"WAV muss nichtleer, Mono, PCM16 und 48 kHz sein: {path}")
    info["duration_ms"] = round(info["frames"] * 1000 / RATE)
    info["sha256"] = sha256_file(path)
    return info


def atomic_install(source: Path, target: Path) -> None:
    """Atomically move a completed artifact without replacing any existing target."""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Hard-link creation is atomic and fails if target exists (including a symlink).
    try:
        os.link(source, target)
    except FileExistsError as exc:
        raise FileExistsError(f"Zieldatei existiert bereits und wird nicht überschrieben: {target}") from exc
    source.unlink()


def ffmpeg_normalize_argv(ffmpeg: Path | str, source: Path, target: Path) -> list[str]:
    return [str(ffmpeg), "-nostdin", "-loglevel", "error", "-i", str(source),
            "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(target)]


def ffmpeg_concat_argv(ffmpeg: Path | str, sources: Sequence[Path], target: Path) -> list[str]:
    if not sources:
        raise ValueError("Mindestens eine Audioquelle für die Verkettung erforderlich")
    argv = [str(ffmpeg), "-nostdin", "-loglevel", "error"]
    for source in sources:
        argv.extend(["-i", str(source)])
    pads = "".join(f"[{i}:a]" for i in range(len(sources)))
    argv.extend(["-filter_complex", f"{pads}concat=n={len(sources)}:v=0:a=1[out]",
                 "-map", "[out]", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(target)])
    return argv


def normalize(ffmpeg: Path | str, source: Path, target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".normalize-", suffix=".wav", dir=target.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
    temporary.unlink()
    try:
        subprocess.run(ffmpeg_normalize_argv(ffmpeg, source, temporary), check=True, shell=False)
        info = validate_wav(temporary)
        atomic_install(temporary, target)
        return info
    finally:
        temporary.unlink(missing_ok=True)


def concatenate(ffmpeg: Path | str, sources: Sequence[Path], target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".concat-", suffix=".wav", dir=target.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
    temporary.unlink()
    try:
        subprocess.run(ffmpeg_concat_argv(ffmpeg, sources, temporary), check=True, shell=False)
        info = validate_wav(temporary)
        atomic_install(temporary, target)
        return info
    finally:
        temporary.unlink(missing_ok=True)
