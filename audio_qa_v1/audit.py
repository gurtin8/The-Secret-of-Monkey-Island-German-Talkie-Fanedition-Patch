#!/usr/bin/env python3
"""Read-only, resumable audit of MI1 German dubbing WAVs.

Only reports/audio_qa_v1 is written. No production CSV or audio is changed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave
from pathlib import Path

try:
    from audio_qa_v1.date_equivalence import canonical_named_dates, has_named_date_conversion
    from voice_anchor_wizard.text_rules import canonicalize_integer_numbers
except ModuleNotFoundError:  # Support the documented direct-script invocation.
    from date_equivalence import canonical_named_dates, has_named_date_conversion
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from voice_anchor_wizard.text_rules import canonicalize_integer_numbers

VERSION = "1.1-integer-number-v1"
DECODER = {"language": "de", "beam_size": 1, "best_of": 1,
           "temperature": 0, "temperature_increment": 0,
           "temperature_fallback": False, "vad": False, "prompt": None}
CSV_FIELDS = ["speech_id", "speaker", "render_status", "qa_status", "content_type",
              "technical_status", "duration_status", "stt_status", "display_text", "tts_text",
              "qa_expected_text", "transcript", "wer", "cer", "missing_words", "extra_words",
              "output_duration_ms", "reference_duration_ms", "wav_sha256", "model_sha256", "notes"]
MARKER = re.compile(r"(?:--\s*[^-]{1,50}\s*--|\[[^\]]{1,60}\]|\([^)]{1,60}\))")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".qa_", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ß", "ss")
    value = "".join(c if c.isalnum() or c.isspace() else " " for c in value)
    return " ".join(value.split())


def distance(a: list[str], b: list[str]) -> tuple[int, int, int]:
    """Return edit distance, missing reference units and extra hypothesis units."""
    rows = [[(i, i, 0) for i in range(len(a) + 1)]]
    for j, item in enumerate(b, 1):
        prior = rows[-1]
        row = [(j, 0, j)]
        for i, ref in enumerate(a, 1):
            choices = [(prior[i][0] + 1, prior[i][1], prior[i][2] + 1),
                       (row[i - 1][0] + 1, row[i - 1][1] + 1, row[i - 1][2]),
                       tuple(x + (0 if item == ref else 1) if k == 0 else x
                             for k, x in enumerate(prior[i - 1]))]
            row.append(min(choices, key=lambda x: (x[0], x[1] + x[2])))
        rows.append(row)
    return rows[-1][-1]


def compare(expected_forms: list[str], transcript: str) -> dict:
    hyp = normalize(transcript)
    comparable_hyp = normalize(canonicalize_integer_numbers(canonical_named_dates(transcript), allow_sentence_final_period=True))
    candidates = []
    for expected in expected_forms:
        ref = normalize(expected)
        comparable_ref = normalize(canonicalize_integer_numbers(canonical_named_dates(expected), allow_sentence_final_period=True))
        words = distance(comparable_ref.split(), comparable_hyp.split())
        chars = distance(list(comparable_ref.replace(" ", "")), list(comparable_hyp.replace(" ", "")))
        candidates.append((words[0] / max(1, len(ref.split())),
                           chars[0] / max(1, len(comparable_ref.replace(" ", ""))), words, ref,
                           has_named_date_conversion(expected, transcript)))
    wer, cer, edits, best, date_equivalent = min(candidates, key=lambda c: (c[0], c[1]))
    return {"normalized_expected": best, "normalized_transcript": hyp,
            "wer": round(wer, 4), "cer": round(cer, 4),
            "missing_words": edits[1], "extra_words": edits[2],
            "date_equivalent": date_equivalent}


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as f:
        channels, rate, width, compression = f.getnchannels(), f.getframerate(), f.getsampwidth(), f.getcomptype()
        frames = f.getnframes()
        if (channels, rate, width, compression) != (1, 48000, 2, "NONE"):
            raise ValueError("expected 48 kHz mono PCM16 WAV")
        if frames < 1:
            raise ValueError("empty WAV")
        peak = total_sq = clipped = total = 0
        while chunk := f.readframes(48000):
            for (sample,) in struct.iter_unpack("<h", chunk):
                v = abs(sample)
                peak = max(peak, v)
                total_sq += sample * sample
                clipped += v >= 32760
                total += 1
    rms = math.sqrt(total_sq / total) / 32768
    return {"duration_ms": round(frames * 1000 / rate), "rms": round(rms, 6),
            "peak": peak, "silence": peak < 100 or rms < 0.001,
            "clipping": clipped / total > 0.001}


def duration_check(ms: int, original_ms: int, text: str, silence: bool) -> tuple[str, list[str]]:
    words = len(normalize(text).split())
    notes = []
    if ms < 150 or (words and silence and ms < 400):
        return "fail", ["nearly empty speech output"]
    if words <= 2 and ms > 15000:
        return "fail", ["one/two-word output exceeds 15 seconds"]
    if words >= 25 and ms < 500:
        return "fail", ["long text shorter than 0.5 seconds"]
    if words and (ms < max(180, 90 * words) or ms > max(6000, 2200 * words)):
        notes.append("text duration outlier")
    if original_ms > 0 and (ms / original_ms < .4 or ms / original_ms > 2.5):
        notes.append("original/output duration ratio outlier")
    return ("warn" if notes else "pass"), notes


def classify(text: str, override: dict) -> str:
    content = override.get("content_type")
    if content is not None:
        if content not in {"speech", "nonverbal", "mixed", "unknown"}:
            raise ValueError(f"invalid content_type: {content}")
        return content
    if MARKER.search(text):
        return "unknown"
    return "speech" if text.strip() else "unknown"


def run_stt(cli: Path, model: Path, wav: Path, tmp_root: Path, threads: int, *, cancel_event=None) -> str:
    # The machine-readable transcript is the strictly decoded JSON file. Keep
    # diagnostic stdout/stderr as bytes: Whisper may be interrupted between
    # UTF-8 bytes, and decoding a partial pipe must never fail or mask QA state.
    with tempfile.TemporaryDirectory(prefix=".asr_", dir=tmp_root) as td:
        base = Path(td) / "transcript"
        cmd = [str(cli), "-m", str(model), "-f", str(wav), "-l", "de",
               "-bs", "1", "-bo", "1", "-tp", "0", "-tpi", "0", "-nf",
               "-t", str(threads), "-oj", "-of", str(base), "-np", "-nt"]
        if cancel_event is None:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  text=False, timeout=300)
            status,stderr=proc.returncode,proc.stderr
        else:
            if cancel_event.is_set():raise InterruptedError('STT abgebrochen')
            # A separate process group lets the producer cancel STT on Ctrl-C
            # or infrastructure failure without touching the TTS session.
            proc=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                                  text=False,start_new_session=True)
            deadline=time.monotonic()+300
            try:
                while True:
                    if cancel_event.is_set():raise InterruptedError('STT abgebrochen')
                    if time.monotonic()>deadline:raise RuntimeError('Whisper timeout after 300 seconds')
                    try:
                        _,stderr=proc.communicate(timeout=.2)
                        break
                    except subprocess.TimeoutExpired:pass
                status=proc.returncode
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:proc.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill();proc.communicate()
                # communicate closes pipes, including on termination.
        if cancel_event is not None and cancel_event.is_set():raise InterruptedError('STT abgebrochen')
        if status:
            diagnostic=stderr[-500:].decode('utf-8',errors='replace') if isinstance(stderr,bytes) else stderr[-500:]
            raise RuntimeError(f"whisper exit {status}: {diagnostic}")
        # Transcript JSON is content, not a diagnostic. Keep UTF-8 strict so
        # malformed output cannot be normalized into a passing transcript.
        payload = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))
        return " ".join(seg.get("text", "").strip() for seg in payload.get("transcription", [])).strip()


def asr_fingerprint(wav_hash: str, model_hash: str, runtime_commit: str, decoder: dict) -> str:
    return hashlib.sha256(json.dumps({"wav": wav_hash, "model": model_hash,
        "runtime": runtime_commit, "decoder": decoder}, sort_keys=True).encode()).hexdigest()


def prefetch_asr(root: Path, ids: list[str], production: dict, manifest: dict,
                 overrides: dict, args, out: Path, model_hash: str, runtime_commit: str) -> None:
    """Fill missing ASR caches while loading Whisper once per group of WAVs."""
    if args.batch_size < 2 or os.name != "posix":
        return
    decoder = {**DECODER, "threads": args.threads}
    pending = []
    for sid in ids:
        row = production[sid]
        if row.get("status") != "ok":
            continue
        override = overrides.get(sid, {})
        display = override.get("display_text", manifest.get(sid, {}).get("german_text", row.get("german_text", "")))
        if classify(display, override) != "speech":
            continue
        wav = root / row.get("output_wav", f"output/german_wav/{sid}.wav")
        if not wav.is_file():
            continue
        wav_hash = digest(wav)
        if row.get("output_sha256") and row["output_sha256"] != wav_hash:
            continue
        try:
            info = wav_info(wav)
        except (OSError, EOFError, wave.Error, ValueError):
            continue
        if info["silence"]:
            continue
        key = asr_fingerprint(wav_hash, model_hash, runtime_commit, decoder)
        cache_path = out / "asr_cache" / f"{sid}.json"
        if cache_path.is_file() and not args.force:
            try:
                if json.loads(cache_path.read_text(encoding="utf-8")).get("fingerprint") == key:
                    continue
            except (OSError, ValueError):
                pass
        pending.append((sid, wav, wav_hash, key, cache_path))
    if not pending:
        return
    print(f"ASR batches: {len(pending)} uncached speech WAVs", file=sys.stderr)
    for offset in range(0, len(pending), args.batch_size):
        group = pending[offset:offset + args.batch_size]
        with tempfile.TemporaryDirectory(prefix=".asr_batch_", dir=out) as td:
            batch_dir = Path(td)
            links = []
            for sid, wav, *_ in group:
                link = batch_dir / f"{sid}.wav"
                link.symlink_to(wav.resolve())
                links.append(link)
            cmd = [str(args.cli), "-m", str(args.model), "-l", "de", "-bs", "1",
                   "-bo", "1", "-tp", "0", "-tpi", "0", "-nf", "-t", str(args.threads),
                   "-oj", "-np", "-nt", *map(str, links)]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=max(300, 20 * len(group)))
            missing = []
            for (sid, _wav, wav_hash, key, cache_path), link in zip(group, links):
                json_path = Path(str(link) + ".json")
                if not json_path.is_file():
                    missing.append(sid)
                    continue
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                transcript = " ".join(seg.get("text", "").strip() for seg in
                                      payload.get("transcription", [])).strip()
                cache = {"fingerprint": key, "transcript": transcript,
                         "wav_sha256": wav_hash, "model_sha256": model_hash,
                         "whisper_commit": runtime_commit, "decoder": decoder}
                atomic_text(cache_path, json.dumps(cache, ensure_ascii=False, indent=2) + "\n")
            print(f"ASR batch {offset // args.batch_size + 1}: {len(group) - len(missing)}/{len(group)} cached",
                  file=sys.stderr)
            if proc.returncode or missing:
                raise RuntimeError(f"Whisper batch exit {proc.returncode}; missing IDs {missing[:8]}; "
                                   f"stderr tail: {proc.stderr[-500:]}")


def load_csv(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r["speech_id"]: r for r in csv.DictReader(f)}


def load_overrides(path: Path | None) -> dict[str, dict]:
    if not path:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("rows"), dict):
        raise ValueError("override file requires version: 1 and rows object")
    for key, value in data["rows"].items():
        if not key.isdecimal() or not isinstance(value, dict):
            raise ValueError(f"invalid override row: {key}")
        if any(k not in {"content_type", "display_text", "tts_text", "qa_expected_text", "accepted_forms"} for k in value):
            raise ValueError(f"unknown override key for {key}")
        if "accepted_forms" in value and (not isinstance(value["accepted_forms"], list) or
                                          not all(isinstance(x, str) and x.strip() for x in value["accepted_forms"])):
            raise ValueError(f"invalid accepted_forms for {key}")
    return data["rows"]


def load_ids_file(path: Path) -> list[str]:
    """Read a versioned UTF-8 selection; reject malformed or duplicate IDs."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("IDs file must be a version 1 JSON object")
    ids = payload.get("ids")
    if (not isinstance(ids, list) or not ids or
            any(not isinstance(sid, str) or not re.fullmatch(r"[0-9]+", sid) for sid in ids) or
            len(ids) != len(set(ids))):
        raise ValueError("IDs file must contain unique decimal string IDs")
    return ids


def git_revision(path: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def audit_one(root: Path, row: dict, source: dict, override: dict, args, out: Path,
              model_hash: str, runtime_commit: str, override_hash: str) -> dict:
    sid = row["speech_id"]
    display = override.get("display_text", source.get("german_text", row.get("german_text", "")))
    tts = override.get("tts_text", row.get("german_text", display))
    expected = override.get("qa_expected_text", tts)
    forms = [expected] + override.get("accepted_forms", [])
    content = classify(display, override)
    wav = root / row.get("output_wav", f"output/german_wav/{sid}.wav")
    result = {"audit_version": VERSION, "speech_id": sid, "speaker": row.get("speaker", ""),
              "render_status": row.get("status", ""), "qa_status": "not_run", "content_type": content,
              "technical_status": "not_run", "duration_status": "not_run", "stt_status": "not_run",
              "display_text": display, "tts_text": tts, "qa_expected_text": expected,
              "accepted_forms": forms, "transcript": "", "wer": "", "cer": "",
              "missing_words": "", "extra_words": "", "output_duration_ms": "",
              "reference_duration_ms": row.get("reference_duration_ms") or source.get("duration_ms", ""),
              "wav_sha256": "", "model_sha256": model_hash, "whisper_commit": runtime_commit,
              "decoder": {**DECODER, "threads": args.threads}, "notes": []}
    if row.get("status") != "ok":
        result["notes"].append("render not available")
        atomic_text(out / "records" / f"{sid}.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    if not wav.is_file():
        result.update(qa_status="fail", technical_status="fail")
        result["notes"].append("WAV missing")
        atomic_text(out / "records" / f"{sid}.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    result["wav_sha256"] = digest(wav)
    key = hashlib.sha256(json.dumps({"version": VERSION, "wav": result["wav_sha256"],
                  "model": model_hash, "runtime": runtime_commit, "override": override_hash,
                  "content": content, "forms": forms, "decoder": result["decoder"]},
                  sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result["fingerprint"] = key
    record_path = out / "records" / f"{sid}.json"
    if not args.force and record_path.is_file():
        try:
            previous = json.loads(record_path.read_text(encoding="utf-8"))
            if previous.get("fingerprint") == key:
                return previous
        except (ValueError, OSError):
            pass
    if row.get("output_sha256") and row["output_sha256"] != result["wav_sha256"]:
        result.update(qa_status="fail", technical_status="fail")
        result["notes"].append("WAV hash differs from production report")
        atomic_text(record_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    try:
        info = wav_info(wav)
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        result.update(qa_status="fail", technical_status="fail")
        result["notes"].append(str(exc))
        atomic_text(record_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    result.update(output_duration_ms=info["duration_ms"], audio_metrics=info,
                  technical_status="warn" if info["clipping"] else "pass")
    if info["silence"] and content == "speech":
        result["technical_status"] = "fail"
        result["notes"].append("silent speech WAV")
    if info["clipping"]:
        result["notes"].append("clipping detected")
    if content == "speech":
        ref_ms = int(result["reference_duration_ms"] or 0)
        result["duration_status"], duration_notes = duration_check(info["duration_ms"], ref_ms,
                                                                      expected, info["silence"])
        result["notes"].extend(duration_notes)
        if result["technical_status"] != "fail":
            cache_path = out / "asr_cache" / f"{sid}.json"
            asr_key = asr_fingerprint(result["wav_sha256"], model_hash,
                                      runtime_commit, result["decoder"])
            cache = {}
            if cache_path.is_file() and (not args.force or args.batch_size > 1):
                try:
                    cache = json.loads(cache_path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    pass
            if cache.get("fingerprint") != asr_key:
                transcript = run_stt(args.cli, args.model, wav, out, args.threads)
                cache = {"fingerprint": asr_key, "transcript": transcript, "wav_sha256": result["wav_sha256"],
                         "model_sha256": model_hash, "whisper_commit": runtime_commit,
                         "decoder": result["decoder"]}
                atomic_text(cache_path, json.dumps(cache, ensure_ascii=False, indent=2) + "\n")
            result["transcript"] = cache["transcript"]
            cmp = compare(forms, result["transcript"])
            result.update(cmp)
            if cmp["wer"] == 0:
                result["stt_status"] = "pass"
            elif not cmp["normalized_transcript"] and info["duration_ms"] < 400:
                result["stt_status"] = "fail"
            else:
                result["stt_status"] = "warn"
                result["notes"].append("STT differs from accepted forms")
    elif content in {"mixed", "unknown"}:
        result["notes"].append("content type requires classification")
    levels = {result[k] for k in ("technical_status", "duration_status", "stt_status")}
    result["qa_status"] = ("fail" if "fail" in levels else
                           "warn" if "warn" in levels or content in {"mixed", "unknown"} else "pass")
    atomic_text(record_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def write_csv(path: Path, records: list[dict]) -> None:
    # In-memory CSV is small (<5 MiB); atomic replacement keeps interrupted runs readable.
    import io
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in records:
        flat = r.copy()
        flat["notes"] = "; ".join(r.get("notes", []))
        writer.writerow(flat)
    atomic_text(path, buf.getvalue())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--id", action="append", help="speech ID; repeatable")
    selection.add_argument("--all", action="store_true", help="audit every manifest row")
    selection.add_argument("--ids-file", type=Path, help="versioned UTF-8 JSON selection")
    p.add_argument("--plan", action="store_true", help="inspect availability, no model and no writes")
    p.add_argument("--model", type=Path, help="multilingual ggml-tiny/base/small.bin")
    p.add_argument("--cli", type=Path, help="whisper-cli path")
    p.add_argument("--overrides", type=Path, help="versioned JSON with accepted forms/content types")
    p.add_argument("--output-dir", type=Path, help="report directory (default reports/audio_qa_v1)")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--batch-size", type=int, default=128,
                   help="Whisper WAVs per model load on macOS; 1 uses per-file mode")
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    root = args.root.resolve()
    manifest = load_csv(root / "input/dubbing_reference.csv")
    production = load_csv(root / "reports/dubbing_voxcpm2.csv")
    try:
        ids = (list(production) if args.all else load_ids_file(args.ids_file) if args.ids_file
               else list(dict.fromkeys(args.id)))
    except (OSError, ValueError, UnicodeError) as exc:
        p.error(f"invalid --ids-file: {exc}")
    if any(sid not in production for sid in ids):
        p.error("unknown speech ID")
    overrides = load_overrides(args.overrides)
    if args.plan:
        counts = {"selected": len(ids), "render_ok": 0, "wav_present": 0, "missing_wav": 0,
                  "unrendered": 0, "marker_unknown": 0}
        for sid in ids:
            row = production[sid]
            if row.get("status") != "ok":
                counts["unrendered"] += 1
                continue
            counts["render_ok"] += 1
            counts["wav_present" if (root / row["output_wav"]).is_file() else "missing_wav"] += 1
            text = manifest.get(sid, {}).get("german_text", "")
            counts["marker_unknown"] += classify(text, overrides.get(sid, {})) == "unknown"
        print(json.dumps(counts, ensure_ascii=False, indent=2))
        return 0
    if not args.model or not args.cli:
        p.error("--model and --cli required for audit")
    if args.batch_size < 1:
        p.error("--batch-size must be positive")
    if not args.model.is_file() or not args.cli.is_file():
        p.error("model or whisper-cli not found")
    if ".en." in args.model.name or args.model.name.endswith(".en.bin"):
        p.error("English-only Whisper model cannot audit German speech")
    args.model = args.model.resolve()
    args.cli = args.cli.resolve()
    model_hash = digest(args.model)
    override_hash = digest(args.overrides) if args.overrides else "none"
    runtime_commit = git_revision(args.cli.parent.parent.parent)
    out = (args.output_dir or root / "reports/audio_qa_v1").resolve()
    out.mkdir(parents=True, exist_ok=True)
    prefetch_asr(root, ids, production, manifest, overrides, args, out, model_hash, runtime_commit)
    records = []
    def merged_records() -> list[dict]:
        by_id = {}
        for path in (out / "records").glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                by_id[item["speech_id"]] = item
            except (OSError, ValueError, KeyError):
                continue
        by_id.update({r["speech_id"]: r for r in records})
        return [by_id[sid] for sid in production if sid in by_id]
    for n, sid in enumerate(ids, 1):
        row = production[sid]
        try:
            record = audit_one(root, row, manifest.get(sid, {}), overrides.get(sid, {}),
                               args, out, model_hash, runtime_commit, override_hash)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
            print(f"{sid}: {exc}", file=sys.stderr)
            record = {"speech_id": sid, "render_status": row.get("status", ""),
                      "qa_status": "not_run", "technical_status": "not_run",
                      "duration_status": "not_run", "stt_status": "not_run", "notes": [str(exc)]}
        records.append(record)
        if n % 50 == 0:
            write_csv(out / "audit.csv", merged_records())
            print(f"{n}/{len(ids)}", file=sys.stderr)
    write_csv(out / "audit.csv", merged_records())
    print(json.dumps({"processed": len(records), "status": {s: sum(r.get("qa_status") == s for r in records)
                          for s in ("pass", "warn", "fail", "not_run")},
                      "csv": str(out / "audit.csv")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
