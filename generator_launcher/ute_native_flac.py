"""Native UTE audio conversion with unchanged Wine resource/archive tools.

All audio decoding, effects, PCM preparation and FLAC encoding run on the host.
Original extraction, game resources and speech archive assembly remain in Wine.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import os
from pathlib import Path
import re
import struct
import subprocess
import time
import wave
from types import SimpleNamespace

from .contracts import ContractError
from .original_audio import scan_sof

REVISION = "ute-native-audio/3"
# User approval, 2026-10-14: all bank-verified xWMA payloads may replace
# exactly 512 missing frames with trailing PCM silence, preserving duration.
# These are the original UTE 1.02 bytes from the already pinned ZIP.
ORIGINAL_HASHES = {
    "install.bat": "ae3423d3484519fcd9eca292c0ea6b97947ea242cedea16ba6361b225e8829ae",
    "install_flac.bat": "1866c47238dae239f55084c78d98390f3837f8024124302dd46835e48ece1b76",
    "tools/voice.bat": "d5f131dfddf1e4656e991cfa410e30ce692841effaec33dc6207dde808bfba66",
    "tools/sbl.bat": "18c810b5ca2778fed70a24b8487af1ffa7a1ab9db6fbeead2560bd920eecfdd2",
    "tools/cdaudio.bat": "eb406836a2e1c458e609d207b5fa4bc332adb7b287e49e7e2c8358ae23cc3176",
    "tools/flac.bat": "f32ba124b836522c9e11fe22b0fbad55e9da3de41e3ceab489ffec23b8d96cf4",
    "tools/monster.tbl": "5e21217130ba8bcd302dbf360c427e8ba681a03596406e3e9c059429ebc40a96",
}


def sha256_file(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def source_plan(builder: Path) -> tuple[dict, dict[str, str]]:
    """Only interpret exact original UTE bytes; keep every original file intact."""
    originals, hashes = {}, {}
    for name, expected in ORIGINAL_HASHES.items():
        path = builder / name
        if not path.is_file() or path.is_symlink() or sha256_file(path) != expected:
            raise ContractError(f"UTE-Originalskript/-Tabelle fehlt oder Hash weicht ab: {name}")
        originals[name] = path.read_text(encoding="ascii")
        hashes[name] = expected
    helpers = ("unxwb.exe", "extract_classic.exe", "bspatch.exe", "scummpacker.exe", "build_monster.exe")
    filenames = {path.name.lower(): path for path in (builder / "tools").iterdir()}
    for name in helpers:
        path = filenames.get(name)
        if path is None or not path.is_file() or path.is_symlink():
            raise ContractError(f"UTE-Originalhelfer fehlt: {name}")
        hashes[f"tools/{name}"] = sha256_file(path)
    for name in ("patch10.000", "patch10.001", "_cdt_silence"):
        path = builder / "tools" / name
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"UTE-Ressourcenquelle fehlt: {name}")
        hashes[f"tools/{name}"] = sha256_file(path)
    plan = {"revision": REVISION, "scripts": {name: originals[f"tools/{name}.bat"].splitlines()
            for name in ("voice", "sbl", "cdaudio")}}
    return plan, hashes


def table_samples(table: Path) -> dict[int, str]:
    samples = {}
    for row in table.read_text(encoding="ascii").splitlines():
        if not re.fullmatch(r"[0-9a-fA-F]{8}[A-Za-z0-9_ -]+", row):
            raise ContractError("UTE monster.tbl enthält einen unbekannten Eintrag")
        speech_id, name = int(row[:8], 16), row[8:]
        if speech_id < 8 or speech_id in samples:
            raise ContractError("UTE monster.tbl enthält ungültige/doppelte Sprach-IDs")
        samples[speech_id] = name
    if not samples:
        raise ContractError("UTE monster.tbl ist leer")
    return samples


def speech_wavs(tools: Path, samples: dict[int, str]) -> list[Path]:
    files = sorted(path for path in tools.iterdir() if path.suffix.lower() == ".wav")
    names = {path.stem.lower() for path in files}
    missing = sorted({name for name in samples.values() if name.lower() not in names})
    if missing:
        raise ContractError(f"UTE-Sprach-WAVs fehlen ({len(missing)}): {', '.join(missing[:8])}; kein Archiv/Musikstart")
    if not files:
        raise ContractError("UTE hat keine Sprach-WAVs erzeugt")
    if len(names) != len(files) or any(path.is_symlink() or not path.is_file() for path in files):
        raise ContractError("UTE-Sprach-WAVs sind keine eindeutigen regulären Dateien")
    return files


def wav_info(source: Path) -> dict:
    """Read PCM RIFF data bounds without loading samples or tolerating truncation."""
    size = source.stat().st_size
    with source.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ContractError(f"Keine unterstützte PCM-WAV: {source.name}")
        end = struct.unpack("<I", header[4:8])[0] + 8
        if end > size:
            raise ContractError(f"Abgeschnittene WAV: {source.name}")
        fmt = data = None
        while stream.tell() + 8 <= end:
            tag, length = struct.unpack("<4sI", stream.read(8))
            start = stream.tell()
            if start + length > end:
                raise ContractError(f"Abgeschnittener WAV-Chunk: {source.name}")
            if tag == b"fmt ":
                if fmt is not None or length < 16 or length > 4096:
                    raise ContractError(f"Ungültiger WAV-Formatblock: {source.name}")
                content = stream.read(length)
                codec, channels, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", content[:16])
                if codec == 0xFFFE:
                    # WAVE_FORMAT_EXTENSIBLE, PCM subformat and full valid bits.
                    if (length < 40 or content[24:40] != bytes.fromhex("0100000000001000800000aa00389b71")
                            or struct.unpack("<H", content[18:20])[0] != bits):
                        raise ContractError(f"Nicht unterstütztes WAV-Extensible-PCM: {source.name}")
                    codec = 1
                if (codec != 1 or bits not in (8, 16, 24) or not 1 <= channels <= 8 or rate < 1
                        or align != channels * (bits // 8) or byte_rate != rate * align):
                    raise ContractError(f"Nicht unterstütztes WAV-PCM (nur 8/16/24 Bit): {source.name}")
                fmt = {"channels": channels, "rate": rate, "bits": bits, "align": align}
            elif tag == b"data":
                if data is not None:
                    raise ContractError(f"Mehrere WAV-Datenblöcke: {source.name}")
                data = (start, length)
            stream.seek(start + length + (length % 2))
        if fmt is None or data is None or not data[1] or data[1] % fmt["align"]:
            raise ContractError(f"WAV-PCM-Daten fehlen oder sind unvollständig: {source.name}")
        return {**fmt, "data_offset": data[0], "data_bytes": data[1], "frames": data[1] // fmt["align"]}


def _pcm_hash(source, info):
    result = hashlib.sha256()
    left = info["data_bytes"]
    with source.open("rb") as stream:
        stream.seek(info["data_offset"])
        while left:
            block = stream.read(min(left, 1024 * 1024))
            if not block:
                raise ContractError(f"WAV änderte sich beim Lesen: {source.name}")
            result.update(block)
            left -= len(block)
    return result.hexdigest()


def flac_info(source):
    with source.open("rb") as stream:
        header = stream.read(8)
        if len(header) != 8 or header[:4] != b"fLaC" or header[4] & 0x7f or int.from_bytes(header[5:8], "big") != 34:
            raise ContractError(f"FLAC-STREAMINFO fehlt: {source.name}")
        info = stream.read(34)
        if len(info) != 34:
            raise ContractError(f"FLAC-STREAMINFO abgeschnitten: {source.name}")
        packed = int.from_bytes(info[10:18], "big")
        return {"rate": packed >> 44, "channels": ((packed >> 41) & 7) + 1,
                "bits": ((packed >> 36) & 31) + 1, "frames": packed & ((1 << 36) - 1)}


def convert_one(source: Path, ffmpeg: Path, log) -> dict:
    info = wav_info(source)
    expected_pcm = _pcm_hash(source, info)
    target = source.with_suffix(".flac")
    part = target.with_name(target.name + ".part")
    if target.exists() or part.exists():
        raise ContractError(f"Native FLAC-Zieldatei existiert bereits: {target.name}; Spielvorbereitung neu beginnen")
    encode = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
              "-filter_threads", "1", "-threads", "1", "-i", str(source),
              "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "flac", "-compression_level", "8",
              "-threads", "1", "-f", "flac", "-n", str(part)]
    subprocess.run(encode, check=True, stdout=log, stderr=log)
    actual = flac_info(part)
    if (any(actual[key] != info[key] for key in ("rate", "channels", "frames"))
            or actual["bits"] != max(16, info["bits"])):
        raise ContractError(f"FLAC verändert WAV-Format/Framezahl: {source.name}")
    raw_format = {8: "u8", 16: "s16le", 24: "s24le"}[info["bits"]]
    codec = {8: "pcm_u8", 16: "pcm_s16le", 24: "pcm_s24le"}[info["bits"]]
    decode = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
              "-filter_threads", "1", "-threads", "1", "-i", str(part),
              "-map", "0:a:0", "-c:a", codec, "-threads", "1", "-f", raw_format, "pipe:1"]
    result, decoded_bytes = hashlib.sha256(), 0
    with subprocess.Popen(decode, stdout=subprocess.PIPE, stderr=log) as process:
        try:
            for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
                result.update(block)
                decoded_bytes += len(block)
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, decode)
        except BaseException:
            process.kill()
            process.wait()
            raise
    if result.hexdigest() != expected_pcm or decoded_bytes != info["data_bytes"]:
        raise ContractError(f"FLAC-PCM-Prüfung fehlgeschlagen: {source.name}")
    os.replace(part, target)
    return {"wav": source.name, "flac": target.name, "sha256": sha256_file(target),
            "pcm_sha256": expected_pcm, **{key: info[key] for key in ("rate", "channels", "bits", "frames")}}


def convert_wavs(files: list[Path], ffmpeg: Path, log, *, jobs=None, progress=None) -> dict:
    """Bound active tasks to CPU count; finish all active work before returning."""
    jobs = jobs if jobs is not None else (os.cpu_count() or 1)
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ContractError("Native FLAC-Parallelität muss eine positive ganze Zahl sein")
    started = time.monotonic()
    results, pending, remaining = [], {}, iter(files)
    def report(status):
        if progress:
            progress({"phase": "native_flac", "status": status, "file_count": len(results),
                      "expected_file_count": len(files), "jobs": jobs, "encoder_threads": 1})
    report("running")
    try:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            def refill():
                while len(pending) < jobs:
                    source = next(remaining, None)
                    if source is None:
                        break
                    pending[pool.submit(convert_one, source, ffmpeg, log)] = source
            refill()
            while pending:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    pending.pop(future)
                    results.append(future.result())
                report("running")
                refill()
    except Exception:
        report("failed")
        raise
    report("completed")
    return {"revision": REVISION, "jobs": jobs, "encoder_threads": 1, "file_count": len(results),
            "elapsed_seconds": round(time.monotonic() - started, 3), "files": sorted(results, key=lambda row: row["wav"])}


def validate_archive(archive: Path, samples: dict[int, str], conversion: dict):
    """Check every original speech ID and each exact verified FLAC payload."""
    rows = scan_sof(archive, lambda speech_id, start, size, audio, flac_size, tags:
                    SimpleNamespace(speech_id=speech_id, audio=audio, flac_size=flac_size))
    if {row.speech_id for row in rows} != set(samples):
        raise ContractError("UTE-SOF-Archiv enthält nicht sämtliche Original-Sprach-IDs")
    flacs = {Path(row["flac"]).stem.lower(): row["sha256"] for row in conversion["files"]}
    with archive.open("rb") as stream:
        for row in rows:
            value, left = hashlib.sha256(), row.flac_size
            stream.seek(row.audio)
            while left:
                block = stream.read(min(left, 1024 * 1024))
                if not block:
                    raise ContractError("UTE-SOF-Audiopayload abgeschnitten")
                value.update(block)
                left -= len(block)
            if value.hexdigest() != flacs.get(samples[row.speech_id].lower()):
                raise ContractError(f"UTE-SOF enthält abweichendes FLAC für Sprach-ID {row.speech_id}")
    return len(rows)


def _read_xwma_metadata(bank):
    from .ute_xwma import read_xwb_xwma_metadata
    return read_xwb_xwma_metadata(bank, include_pcm=True)


def _pad_approved_xwm(path, info, provenance):
    if not (provenance.get("compressed_bytes_unchanged") is True
            and provenance.get("sample_rate") == info["rate"]
            and provenance.get("channels") == info["channels"]
            and info["bits"] == 16
            and provenance.get("frames", 0) - info["frames"] == 512):
        return None
    padded = path.with_name(path.name + ".padding")
    original_pcm_sha = _pcm_hash(path, info)
    created = False
    try:
        with padded.open("xb") as output, path.open("rb") as source:
            created = True
            with wave.open(output, "wb") as audio:
                audio.setnchannels(info["channels"]); audio.setsampwidth(2); audio.setframerate(info["rate"])
                source.seek(info["data_offset"])
                remaining = info["data_bytes"]
                while remaining:
                    block = source.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise ContractError("WAV änderte sich beim Ergänzen freigegebener Stille")
                    audio.writeframesraw(block)
                    remaining -= len(block)
                audio.writeframesraw(bytes(512 * info["align"]))
        corrected = wav_info(padded)
        if (corrected["frames"] != provenance["frames"]
                or _pcm_hash(padded, {**corrected, "data_bytes": info["data_bytes"]}) != original_pcm_sha):
            raise ContractError("Freigegebene WAV-Stille verändert dekodierte Samples oder Dauer")
        os.replace(padded, path)
    finally:
        if created:
            padded.unlink(missing_ok=True)
    return {"policy": "user-approved-xwma-tail-silence/1", "approved_on": "2026-10-14",
            "decoded_frames": info["frames"], "appended_silent_frames": 512,
            "bank_frames": provenance["frames"], "decoded_pcm_sha256": original_pcm_sha,
            "decoded_samples_unchanged": True}


def decode_xwm(source: Path, target: Path, ffmpeg: Path, log, metadata=None) -> dict:
    """Decode a bank-verified corrected XWMA header at its source rate/channels."""
    from .ute_xwma import prepare_xwma
    part = target.with_name(target.name + ".part")
    if target.exists() or part.exists() or source.is_symlink():
        raise ContractError(f"XWM-Ziel existiert bereits/Quelle ist ein Link: {target.name}")
    corrected = source.parent / ".native-xwma" / source.name
    corrected.parent.mkdir(exist_ok=True)
    provenance = prepare_xwma(source, corrected, metadata or [])
    command = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
               "-filter_threads", "1", "-threads", "1", "-i", str(corrected),
               "-map", "0:a:0", "-c:a", "pcm_s16le", "-threads", "1", "-f", "wav", "-n", str(part)]
    subprocess.run(command, check=True, stdout=log, stderr=log)
    info = wav_info(part)
    padding = _pad_approved_xwm(part, info, provenance)
    if padding:
        info = wav_info(part)
        if log is not None:
            log.write((f"Native Audiovorbereitung: {source.name} · 512 Frames freigegebene Stille "
                       "ergänzt · dekodierte Samples unverändert\n").encode("utf-8"))
            log.flush()
    if (info["bits"] != 16 or info["rate"] != provenance["sample_rate"]
            or info["channels"] != provenance["channels"] or info["frames"] != provenance["frames"]):
        raise ContractError(
            f"XWM-Decoder verändert Bank-Samplerate/Kanäle/Framezahl: {source.name}. "
            f"Experimentelles FFmpeg: Soll {provenance['sample_rate']} Hz / "
            f"{provenance['channels']} Kanäle / 16 Bit / {provenance['frames']} Frames; "
            f"Ist {info['rate']} Hz / {info['channels']} Kanäle / {info['bits']} Bit / {info['frames']} Frames. "
            "Die native Audiokonvertierung wurde gestoppt; der deutsche Patch wurde noch nicht gestartet.")
    os.replace(part, target)
    return {"xwm": source.name, "wav": target.name, "xwma_provenance": provenance,
            "pcm_sha256": _pcm_hash(target, info),
            **({"tail_silence": padding} if padding else {}),
            **{key: info[key] for key in ("rate", "channels", "bits", "frames")}}


def _decode_samples(tools, ffmpeg, log, jobs, progress=None, phase="original_builder", metadata=None):
    files = sorted(path for path in tools.rglob("*") if path.suffix.lower() == ".xwm"
                   and ".native-xwma" not in path.parts)
    if len({path.stem.lower() for path in files}) != len(files):
        raise ContractError("UTE-XWM-Extraktion enthält mehrdeutige Namen")
    if not files:
        if not any(path.suffix.lower() == ".wav" for path in tools.rglob("*")):
            raise ContractError("UTE-Audioextraktion hat weder PCM-WAV noch XWM erzeugt")
        return []
    remaining, pending, results = iter(files), set(), []
    def report(status):
        if progress:
            progress({"phase": phase, "status": status, "jobs": jobs,
                      "file_count": len(results), "expected_file_count": len(files),
                      "audio_operation": "native_xwm_decode", "cwd": str(tools)})
    report("running")
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        def refill():
            while len(pending) < jobs:
                path = next(remaining, None)
                if path is None:
                    break
                pending.add(pool.submit(decode_xwm, path, tools / (path.stem + ".wav"), ffmpeg, log, metadata))
        refill()
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                results.append(future.result())
            report("running")
            refill()
    report("running")  # Resource/effect work remains part of the builder stage.
    return results


def _tokens(line):
    return [token.strip('"') for token in re.findall(r'"[^"]*"|\S+', line)]


def _local_path(tools, token):
    return tools / token.replace("\\", "/")


def _flatten_wavs(tools, *, decoded=(), metadata=(), overrides=None):
    """Match the original recursive move, including directly extracted PCM."""
    from .ute_xwma import match_bank_pcm
    files = sorted(path for path in tools.rglob("*") if path.suffix.lower() == ".wav")
    roots, moves = {}, []
    for source in files:
        if source.is_symlink() or not source.is_file():
            raise ContractError("UTE-PCM-Extraktion enthält keine reguläre WAV-Datei")
        if source.parent == tools:
            key = source.name.casefold()
            if key in roots:
                raise ContractError(f"UTE-WAV-Namen kollidieren beim Zusammenführen: {source.name}")
            roots[key] = source
    claimed = set(roots)
    moved_keys = set()
    for source in files:
        if source.parent == tools:
            continue
        key = source.name.casefold()
        if key in moved_keys:
            raise ContractError(f"UTE-WAV-Namen kollidieren beim Zusammenführen: {source.name}")
        target = roots.get(key, tools / source.name)
        override = None
        if key in claimed or target.exists() or target.is_symlink():
            records = [row for row in decoded if row['wav'].casefold() == key]
            if (key not in roots or len(records) != 1 or target.is_symlink()
                    or not records[0].get('xwma_provenance', {}).get('compressed_bytes_unchanged')):
                raise ContractError(f"UTE-WAV-Namen kollidieren beim Zusammenführen: {source.name}")
            info = wav_info(target)
            record = records[0]
            if (any(info[k] != record[k] for k in ('rate', 'channels', 'bits', 'frames'))
                    or _pcm_hash(target, info) != record.get('pcm_sha256')):
                raise ContractError(f"UTE-WAV-Namen kollidieren mit veränderter Dekodierung: {source.name}")
            pcm = match_bank_pcm(source, wav_info(source), metadata)
            override = {'wav': target.name, 'policy': 'original-recipe-direct-pcm-over-decoded-xwma/1',
                        'selected_pcm': pcm, 'replaced_xwma': record['xwma_provenance'],
                        'replaced_pcm_sha256': record['pcm_sha256']}
        claimed.add(key)
        moved_keys.add(key)
        moves.append((source, target, override))
    # Verify all collisions before moving anything; unknown duplicates fail.
    for source, target, override in moves:
        os.replace(source, target)
        if override is not None and overrides is not None:
            overrides.append(override)
    return len(files)


def run_original_script(name, plan, *, tools, wine, ffmpeg, sox, log, environment, jobs, progress=None):
    """Execute pinned original operations with checked native audio boundaries."""
    import shutil
    from .ute_native_sbl import wav_to_sbl
    reports = {"decoded": [], "effects": [], "sbl": [], "flattened_wav_counts": [], "pcm_overrides": []}
    bank_metadata = []
    flatten_metadata = []
    sox_environment = dict(os.environ)
    sox_environment.pop("SOX_OPTS", None)
    parameter = "22050" if name == "sbl" else "flac"
    helpers = {path.name.lower(): path for path in tools.iterdir() if path.is_file()}
    for original_line in plan["scripts"][name]:
        line = original_line.strip().replace("%1", parameter)
        lower = line.lower()
        if not line or lower.startswith(("@echo", "echo", "if ", "goto ", ":")) or line == ")":
            continue
        if lower.startswith("for "):
            if "(*.xwm)" in lower:
                reports["decoded"].extend(_decode_samples(tools, ffmpeg, log, jobs, progress,
                    "builder_finish" if name == "cdaudio" else "original_builder", bank_metadata))
                flatten_metadata = bank_metadata
                bank_metadata = []
            elif "(*.wav)" in lower:
                before = len(reports["pcm_overrides"])
                reports["flattened_wav_counts"].append(_flatten_wavs(tools, decoded=reports["decoded"],
                    metadata=flatten_metadata, overrides=reports["pcm_overrides"]))
                if log is not None:
                    for row in reports["pcm_overrides"][before:]:
                        log.write((f"Native Audiovorbereitung: {row['wav']} · bankgeprüftes Original-PCM "
                                   "gemäß UTE-Rezept statt xWMA-Dekodierung verwendet\n").encode('utf-8'))
                    log.flush()
            else:
                raise ContractError(f"Unbekannte UTE-Schleife: {line}")
            continue
        if lower.startswith("xwmaencode "):
            continue  # Executed by the bounded native decoder at the loop head.
        tokens = _tokens(re.sub(r">nul:?", "", line, flags=re.I))
        command, args = tokens[0].lower(), tokens[1:]
        if command == "unxwb.exe":
            argv = [str(wine), str(helpers["unxwb.exe"]), *args]
            subprocess.run(argv, cwd=tools, env=environment, check=True, stdout=log, stderr=log)
            bank_metadata.extend(_read_xwma_metadata(_local_path(tools, args[-1]).resolve()))
        elif command == "sox":
            audio_indices = [index for index, token in enumerate(args) if token.lower().endswith((".wav", ".flac"))]
            if len(audio_indices) < 2:
                raise ContractError(f"UTE-SoX-Aufruf ohne eindeutige Ein-/Ausgabe: {line}")
            output_index = audio_indices[-1]
            output = _local_path(tools, args[output_index])
            if output.suffix.lower() == ".flac":
                output = output.with_suffix(".wav")
                args[output_index] = str(output)
                args[output_index:output_index] = ["-b", "16", "-t", "wav"]
            args = [str(_local_path(tools, token)) if "\\" in token else token for token in args]
            subprocess.run([str(sox), *args], cwd=tools, env=sox_environment,
                           check=True, stdout=log, stderr=log)
            wav_info(output)
            reports["effects"].append(original_line.strip())
        elif command == "wav2sbl":
            reports["sbl"].append(wav_to_sbl(_local_path(tools, args[0]), _local_path(tools, args[1])))
        elif command == "scummpacker":
            subprocess.run([str(wine), str(helpers["scummpacker.exe"]), *args], cwd=tools,
                           env=environment, check=True, stdout=log, stderr=log)
        elif command == "copy":
            source, destination = (_local_path(tools, token) for token in args[:2])
            if destination.is_dir():
                destination = destination / source.name
            shutil.copyfile(source, destination)
        elif command == "md":
            _local_path(tools, args[0]).mkdir(parents=True, exist_ok=True)
        elif command == "rd":
            directory = _local_path(tools, args[-1])
            if directory.exists():
                shutil.rmtree(directory)
        elif command == "del":
            for path in tools.glob(args[0]):
                if path.is_file():
                    path.unlink()
        elif command == "ren":
            source = _local_path(tools, args[0])
            source.rename(source.with_name(args[1]))
        else:
            raise ContractError(f"Nicht unterstützter Original-UTE-Befehl: {line}")
    return reports


def execute_native_builder(*, builder, plan, wine, ffmpeg, sox, log, environment, jobs=None, progress=None):
    """All audio native; retain original resource/archive executable boundaries."""
    import shutil
    jobs = jobs if jobs is not None else (os.cpu_count() or 1)
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ContractError("Native Audio-Parallelität muss positiv sein")
    tools, game = builder / "tools", builder / "MI1_Ultimate_Talkie_Edition"
    if not (builder.parent / "Monkey1.pak").is_file():
        raise ContractError("Monkey1.pak nicht gefunden; Original-UTE-Ressourcenextraktion kann nicht starten")
    game.mkdir(exist_ok=True)
    shutil.copyfile(builder / "readme.txt", game / "readme.txt")
    def report(phase, status="running"):
        if progress:
            progress({"phase": phase, "status": status, "jobs": jobs, "file_count": None,
                      "expected_file_count": None, "cwd": str(tools)})
    def helper(name, *args):
        matches = [path for path in tools.iterdir() if path.name.lower() == name.lower()]
        if len(matches) != 1:
            raise ContractError(f"UTE-Helfer fehlt/ist mehrdeutig: {name}")
        subprocess.run([str(wine), str(matches[0]), *args], cwd=tools, env=environment,
                       check=True, stdout=log, stderr=log)
    voice = run_original_script("voice", plan, tools=tools, wine=wine, ffmpeg=ffmpeg, sox=sox,
                                log=log, environment=environment, jobs=jobs, progress=progress)
    resources = None
    if not (game / "monkey.000").exists():
        helper("extract_classic.exe")
        helper("bspatch.exe", "monkey1.000", "monkey.000", "patch10.000")
        helper("bspatch.exe", "monkey1.001", "monkey.001", "patch10.001")
        resources = run_original_script("sbl", plan, tools=tools, wine=wine, ffmpeg=ffmpeg, sox=sox,
                                        log=log, environment=environment, jobs=jobs, progress=progress)
        for extension in ("000", "001"):
            shutil.copyfile(tools / f"Resource.{extension}", game / f"monkey.{extension}")
        for name in ("Resource.000", "Resource.001", "monkey.000", "monkey.001", "monkey1.000", "monkey1.001"):
            (tools / name).unlink(missing_ok=True)
    if any(not (game / name).is_file() or not (game / name).stat().st_size for name in ("monkey.000", "monkey.001")):
        raise ContractError("UTE-Ressourcen sind nach Originalhelfern unvollständig")
    samples = table_samples(tools / "monster.tbl")
    files = speech_wavs(tools, samples)
    speech = convert_wavs(files, ffmpeg, log, jobs=jobs, progress=progress)
    report("speech_archive")
    helper("build_monster.exe", "flac")
    count = validate_archive(tools / "monkey.sof", samples, speech)
    shutil.copyfile(tools / "monkey.sof", game / "monkey.sof")
    # WAVs survive decoder/encoder/archive failure. Only a checked archive
    # permits speech cleanup before the original music recipe starts.
    for path in files:
        path.unlink()
    (tools / "monkey.sof").unlink()
    for row in speech["files"]:
        (tools / row["flac"]).unlink()
    report("speech_archive", "completed")
    report("builder_finish")
    music = run_original_script("cdaudio", plan, tools=tools, wine=wine, ffmpeg=ffmpeg, sox=sox,
                                log=log, environment=environment, jobs=jobs, progress=progress)
    music_wavs = sorted(path for directory in (game / "cd_music_flac", game / "se_music_flac")
                        for path in directory.glob("*.wav"))
    for directory, expected in ((game / "cd_music_flac", set(range(1, 25)) - {10, 20}),
                                (game / "se_music_flac", set(range(1, 30)))):
        names = {path.stem for path in directory.glob("*.wav")}
        expected_names = {f"track{number}" for number in expected}
        if directory.name == "se_music_flac":
            expected_names.add("track8_no_sfx")
        if names != expected_names:
            raise ContractError("Original-UTE-Musikrecipe hat unvollständige WAV-Tracks erzeugt")
    music_flac = convert_wavs(music_wavs, ffmpeg, log, jobs=jobs)
    for path in music_wavs:
        path.unlink()
    report("builder_finish", "completed")
    return {"native_audio": {"revision": REVISION, "jobs": jobs, "encoder_threads": 1,
                            "voice": voice, "resources": resources, "speech": speech,
                            "speech_ids": count, "music": music, "music_flac": music_flac}}
