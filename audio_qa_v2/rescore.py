#!/usr/bin/env python3
"""Rescore completed Audio QA v1 records without opening WAVs or running ASR."""
from __future__ import annotations

import argparse
import collections
import csv
import difflib
import io
import json
import re
import sys
import unicodedata
from pathlib import Path

VERSION = "2.2-named-date-integer-v1"
try:
    from audio_qa_v1.date_equivalence import canonical_named_dates
    from voice_anchor_wizard.text_rules import canonicalize_integer_numbers
    from voice_anchor_wizard.text_rules import integer_to_german, integer_number_words
except ModuleNotFoundError:  # Support direct invocation from this script's directory.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from audio_qa_v1.date_equivalence import canonical_named_dates
    from voice_anchor_wizard.text_rules import canonicalize_integer_numbers
    from voice_anchor_wizard.text_rules import integer_to_german, integer_number_words
NUMBER_WORDS = {integer_to_german(value).casefold().replace("ß", "ss") for value in range(1000)}
STOP = set("ich du er sie es wir ihr der die das den dem des ein eine einen einem einer und oder aber zu zum zur im in am an auf aus bei mit von vor nach fur fuer ist sind war waren bin bist hat haben hatte kann konnte will soll wird wurde werden nicht noch schon auch nur so ja nein da hier dort mir mich dir dich ihm ihnen uns euch was wer wie wo wenn dann denn doch mal bitte sehr mein meine dein deine sein seine ihr ihre unser eure dass als um bis ob oh ach hey".split())
FIELDS = ["speech_id", "speaker", "render_status", "qa_status", "content_type",
          "technical_status", "duration_status", "stt_status", "reason_codes",
          "qa_expected_text", "transcript", "wer", "cer", "missing_words", "extra_words",
          "output_duration_ms", "reference_duration_ms", "wav_sha256"]


def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ß", "ss")
    return re.findall(r"[^\W_]+", text, re.UNICODE)


def resembles(a: str, b: str) -> bool:
    return a == b or (len(a) >= 5 and len(b) >= 5 and
                      difflib.SequenceMatcher(None, a, b).ratio() >= .80)


def lexical_reasons(record: dict) -> list[str]:
    forms = record.get("accepted_forms") or [record.get("qa_expected_text", "")]
    transcript = record.get("transcript", "")
    hyp = words(canonicalize_integer_numbers(canonical_named_dates(transcript), allow_sentence_final_period=True))
    protected_number_words = NUMBER_WORDS | integer_number_words(transcript, allow_sentence_final_period=True)
    if any("".join(words(canonicalize_integer_numbers(canonical_named_dates(form), allow_sentence_final_period=True))) ==
           "".join(hyp)
           for form in forms):
        return []
    # ASR word boundaries are unreliable (e.g. "auf einmal" vs "aufeinmal").
    # An exact normalized character stream is stronger evidence than token heuristics.
    if any("".join(words(canonicalize_integer_numbers(form, allow_sentence_final_period=True))) == "".join(hyp) for form in forms):
        return []
    # Score every accepted form; a spelled-out number can validate a numeric form.
    alternatives = []
    for form in forms:
        ref = words(canonicalize_integer_numbers(canonical_named_dates(form), allow_sentence_final_period=True))
        form_number_words = protected_number_words | integer_number_words(form, allow_sentence_final_period=True)
        unmatched = list(hyp)
        missing = []
        for token in ref:
            match = next((i for i, other in enumerate(unmatched)
                          if (token == other if token.isdigit() or other.isdigit() or token in form_number_words or other in form_number_words else resembles(token, other))), None)
            if match is None:
                missing.append(token)
            else:
                unmatched.pop(match)
        reasons = []
        expected_number_words = [token for token in ref if token in form_number_words]
        missed_number_words = [token for token in missing if token in form_number_words]
        if expected_number_words and missed_number_words:
            observed_numbers = [token for token in hyp if token.isdigit() or token in form_number_words]
            reasons.append("number_mismatch" if observed_numbers else "number_unverified")
        if len(ref) <= 2:
            if missing and any(not t.isdigit() for t in missing):
                reasons.append("short_text_mismatch")
        else:
            content_missing = [t for t in missing if not t.isdigit() and t not in STOP and len(t) >= 4]
            if content_missing:
                reasons.append("missing_content_words")
            coverage = (len(ref) - len(missing)) / len(ref) if ref else 1
            if len(ref) >= 5 and coverage < .40 and len(missing) >= 3:
                reasons.append("low_expected_coverage")
            if len(unmatched) >= max(5, len(ref) + 1):
                reasons.append("many_extra_words")
        alternatives.append(reasons)
    reasons = min(alternatives, key=lambda x: len(x)) if alternatives else []
    if len(hyp) >= 8:
        pairs = collections.Counter(zip(hyp, hyp[1:]))
        if any(n >= 4 for n in pairs.values()) or (len(hyp) >= 12 and len(set(hyp)) <= 3):
            reasons = list(dict.fromkeys([*reasons, "asr_loop"] ))
    return reasons


def rescore(record: dict) -> dict:
    result = dict(record)
    result["audit_version"] = VERSION
    result["source_audit_version"] = record.get("audit_version")
    result["source_fingerprint"] = record.get("fingerprint")
    reasons = []
    content = record.get("content_type", "unknown")
    if record.get("render_status") != "ok":
        result.update(qa_status="not_run", technical_status="not_run",
                      duration_status="not_run", stt_status="not_run", reason_codes=["render_not_available"])
        return result
    technical = record.get("technical_status", "not_run")
    metrics = record.get("audio_metrics") or {}
    if technical == "fail":
        reasons.append("technical_silence" if metrics.get("silence") and content == "speech"
                       else "technical_invalid")
    elif technical == "warn":
        reasons.append("technical_clipping")
    ms = int(record.get("output_duration_ms") or 0)
    ref_ms = int(record.get("reference_duration_ms") or 0)
    expected = words(record.get("qa_expected_text", ""))
    duration = "not_run"
    if content == "speech" and ms:
        duration = "pass"
        if ms < 150 or (len(expected) <= 2 and ms > 15000) or (len(expected) >= 25 and ms < 500):
            duration = "fail"
            reasons.append("extreme_duration")
        else:
            if expected and (ms < max(180, 90 * len(expected)) or ms > max(6000, 2200 * len(expected))):
                reasons.append("text_duration_outlier")
            if ref_ms and (ms / ref_ms < .4 or ms / ref_ms > 2.5):
                reasons.append("reference_duration_outlier")
            if reasons and any(r.endswith("duration_outlier") for r in reasons):
                duration = "warn"
    result["duration_status"] = duration
    result["stt_status"] = "not_run"
    if content == "speech" and technical != "fail":
        if record.get("stt_status") != "not_run":
            lexical = lexical_reasons(record)
            reasons.extend(lexical)
            result["stt_status"] = "warn" if lexical else "pass"
        else:
            reasons.append("stt_unavailable")
    elif content in {"unknown", "mixed"}:
        reasons.append("content_needs_classification")
    # Technical failure remains authoritative; lexical findings are review signals.
    result["qa_status"] = ("fail" if technical == "fail" or duration == "fail" else
                           "warn" if reasons else "pass")
    result["reason_codes"] = list(dict.fromkeys(reasons))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="v1 base_m1 directory")
    parser.add_argument("--output", type=Path, required=True, help="new v2 base_m1 directory")
    args = parser.parse_args(argv)
    source, output = args.source.resolve(), args.output.resolve()
    if source == output or source in output.parents and output.name in {"records", "asr_cache"}:
        parser.error("output must be a separate report directory")
    with (source / "audit.csv").open(encoding="utf-8-sig", newline="") as f:
        ids = [row["speech_id"] for row in csv.DictReader(f)]
    records = [rescore(json.loads((source / "records" / f"{sid}.json").read_text(encoding="utf-8")))
               for sid in ids]
    output.mkdir(parents=True, exist_ok=True)
    (output / "records").mkdir(exist_ok=True)
    for item in records:
        (output / "records" / f"{item['speech_id']}.json").write_text(
            json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "audit.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in records:
            flat = {**item, "reason_codes": ";".join(item["reason_codes"])}
            writer.writerow(flat)
    summary = {"audit_version": VERSION, "source": str(source), "total": len(records),
               "status": dict(collections.Counter(r["qa_status"] for r in records)),
               "content_type": dict(collections.Counter(r["content_type"] for r in records)),
               "reason_codes": dict(collections.Counter(code for r in records for code in r["reason_codes"]))}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
