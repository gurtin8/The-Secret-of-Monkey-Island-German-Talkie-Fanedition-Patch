"""Shared reference/prewarm core. No wizard, chip guard or implicit jobs."""
from __future__ import annotations
from array import array
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import wave
from .audio_cache import AudioPreparationCache
from .session import SessionPool

NEUTRAL_PREWARM_TEXTS = ("Ich bin bereit.", "Jetzt kann es losgehen.", "Alles klar, wir können beginnen.", "Wir sind jetzt bereit und können gemeinsam beginnen.")

def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def invoke_voxcpm2(command: list[str], stream, sessions: SessionPool | None = None) -> dict:
    if sessions is not None:
        return sessions.run(command, stream)
    import time
    started = time.perf_counter()
    subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, shell=False)
    return {"cold": True, "request_seconds": time.perf_counter() - started}


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as handle:
        channels, width, rate, frames = (handle.getnchannels(), handle.getsampwidth(),
                                          handle.getframerate(), handle.getnframes())
        if (channels, width, rate) != (1, 2, 48000) or frames <= 0:
            raise ValueError("Ausgabe muss nichtleere 48-kHz-Mono-PCM16-WAV sein")
        samples = array("h", handle.readframes(frames))
    return {"duration_ms": round(frames * 1000 / rate),
            "clipping_fraction": round(sum(abs(x) >= 32760 for x in samples) / len(samples), 7),
            "active_fraction": round(sum(abs(x) > 100 for x in samples) / len(samples), 7)}


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Pfad verlässt Projektwurzel")
    return path


def reference_inputs(plan: dict, verified_inputs: dict) -> dict:
    """Inputs that actually determine the materialized anchor reference WAV."""
    return {"selection_fingerprint": plan["anchor"]["fingerprint"],
            "source_sha256": verified_inputs["component_audio_sha256"],
            "prompt_text_sha256": verified_inputs["component_prompt_text_sha256"],
            "ffmpeg_sha256": verified_inputs["binaries"]["ffmpeg"]}


def reference_report_value(plan: dict, verified_inputs: dict, reference: Path, info: dict) -> dict:
    inputs = reference_inputs(plan, verified_inputs)
    fingerprint = digest_bytes(canonical(inputs))
    return {"selection_fingerprint": plan["anchor"]["fingerprint"],
            "fingerprint": fingerprint,
            "verified_inputs": {**inputs, "reference_input_fingerprint": fingerprint},
            "output_sha256": digest_file(reference), **info}


def materialize_role_reference(root: Path, plan: dict, verified_inputs: dict, ffmpeg: Path,
                               reference: Path, reference_report: Path, cache,
                               *, convert_fn=None, composite_fn=None) -> dict:
    """Shared reference preparation used by the wizard and the launcher adapter."""
    convert_fn = convert_fn or convert
    composite_fn = composite_fn or composite
    if reference.exists():
        stored = json.loads(reference_report.read_text(encoding="utf-8")) if reference_report.exists() else {}
        expected_inputs = reference_inputs(plan, verified_inputs)
        if (stored.get("selection_fingerprint") != plan["anchor"]["fingerprint"]
                or any(stored.get("verified_inputs", {}).get(key) != value
                       for key, value in expected_inputs.items())
                or stored.get("output_sha256") != digest_file(reference)):
            raise ValueError("Vorhandene Referenz ist unvollständig oder abweichend; nichts wird überschrieben")
        return stored
    if reference_report.exists():
        raise ValueError("Referenzreport ohne WAV vorhanden; nichts wird überschrieben")
    reference.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="components-", dir=reference.parent) as temporary:
        temporary = Path(temporary)
        parts = []
        for index, item in enumerate(plan["components"]):
            source = inside(root, item["audio"]["path"])
            part = temporary / f"{index}.wav"
            cache.prepare("normalize-48000", [source], part,
                          lambda output, source=source: convert_fn(ffmpeg, source, output))
            parts.append(part)
        cache.prepare("composite", parts, reference,
                      lambda output: composite_fn(parts, output, plan["join_silence_ms"]),
                      join_ms=plan["join_silence_ms"])
    info = wav_info(reference)
    if not plan["main_anchor"].get("small_role_fallback") and not 4000 <= info["duration_ms"] <= 15000:
        reference.unlink(missing_ok=True)
        raise ValueError("Materialisierter Stimmanker liegt außerhalb 4–15 s")
    value = reference_report_value(plan, verified_inputs, reference, info)
    atomic_json(reference_report, value)
    return value


def convert(ffmpeg: Path, source: Path, target: Path) -> None:
    subprocess.run([str(ffmpeg), "-nostdin", "-loglevel", "error", "-i", str(source),
                    "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(target)], check=True)


def composite(parts: list[Path], target: Path, join_ms: int) -> None:
    params = None
    frames: list[bytes] = []
    for part in parts:
        with wave.open(str(part), "rb") as handle:
            current = (handle.getnchannels(), handle.getsampwidth(), handle.getframerate())
            if current != (1, 2, 48000):
                raise ValueError("Kompositquelle ist nicht 48-kHz-Mono-PCM16")
            params = handle.getparams()
            frames.append(handle.readframes(handle.getnframes()))
    silence = b"\0\0" * round(48000 * join_ms / 1000)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(48000)
        for index, block in enumerate(frames):
            if index: handle.writeframes(silence)
            handle.writeframes(block)


def build_render_identity(selection_fingerprint: str, kind: str, seed: int, target_case: dict,
                          reference_sha256: str, verified_inputs: dict) -> dict:
    target_input = next(case for case in verified_inputs["cases"] if case["speech_id"] == target_case["speech_id"])
    # Each artifact depends on its own target and reference. The verification
    # bundle also contains the other probe, which must not invalidate this one.
    identity = {"selection_fingerprint": selection_fingerprint,
                "kind": kind, "seed": seed, "target": target_input,
                "reference_sha256": reference_sha256,
                "reference_component_sha256": verified_inputs["component_audio_sha256"],
                "reference_prompt_text_sha256": verified_inputs["component_prompt_text_sha256"],
                "binaries": verified_inputs["binaries"], "models": verified_inputs["models"]}
    identity["fingerprint"] = digest_bytes(canonical(identity))
    return identity


def render_one(root: Path, cli: Path, ffmpeg: Path, models: list[Path], reference: Path,
               target_case: dict, output: Path, report: Path, selection_fingerprint: str,
               kind: str, seed: int, verified_inputs: dict, progress=None, sessions=None, preparation_events=()) -> None:
    if progress:
        progress("preparing", kind, target_case["speech_id"])
    identity = build_render_identity(selection_fingerprint, kind, seed, target_case,
                                     digest_file(reference), verified_inputs)
    target_input = identity["target"]
    fingerprint = identity.pop("fingerprint")
    if output.exists() or report.exists():
        if output.is_file() and report.is_file():
            prior = json.loads(report.read_text(encoding="utf-8"))
            prior_inputs = {key: value for key, value in prior.get("verified_inputs", {}).items()
                            if key != "verified_input_fingerprint"}
            if (prior.get("status") == "ok" and (prior.get("fingerprint") == fingerprint or prior_inputs == identity)
                    and prior.get("selection_fingerprint") == selection_fingerprint
                    and prior.get("output_sha256") == digest_file(output)):
                if progress:
                    progress("reusing", kind, target_case["speech_id"])
                print(f"{kind}: unverändert vorhanden ({output})")
                return
        raise ValueError(f"Vorhandene {kind}-Ausgabe ist unvollständig oder abweichend; nichts wird überschrieben: {output.parent}")
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {"status": "started", "kind": kind, "fingerprint": fingerprint,
              "selection_fingerprint": selection_fingerprint, "verified_inputs": identity,
              "target_speech_id": target_case["speech_id"], "target_audio_sha256": target_input["audio_sha256"],
              "target_text": target_case["german_text"],
              "audition_only": True, "preparation": list(preparation_events),
              "prompt_text_sha256": target_input["prompt_text_sha256"],
              "target_text_sha256": target_input["target_text_sha256"], "started_at": utc_now()}
    atomic_json(report, record)
    try:
        with tempfile.TemporaryDirectory(prefix=f"{kind}-", dir=output.parent) as temporary:
            temporary = Path(temporary)
            generated = temporary / "generated.wav"
            command = [str(cli), "-t", target_case["german_text"], "-r", str(reference),
                       "--seed", str(seed), "-o", str(generated), *map(str, models)]
            log = output.parent / "synthesis.log"
            with log.open("w", encoding="utf-8") as stream:
                if progress:
                    progress("loading_model", kind, target_case["speech_id"], log)
                record["execution"] = invoke_voxcpm2(command, stream, sessions)
            if progress:
                progress("saving", kind, target_case["speech_id"])
            record.update(wav_info(generated))
            record["output_sha256"] = digest_file(generated)
            os.replace(generated, output)
        record["status"] = "ok"
    except Exception as exc:
        record.update(status="error", error=str(exc))
        raise
    finally:
        record["finished_at"] = utc_now()
        atomic_json(report, record)

