#!/usr/bin/env python3
"""Resume one bounded Plan-07 target-hardware run and record timing/QA."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(card_path: Path, *, execute=False):
    card_path = card_path.resolve(strict=True)
    card = json.loads(card_path.read_text(encoding="utf-8"))
    command = card["execution_command"]
    if not execute:
        return {"selection": card["selection"], "command": command, "executed": False,
                "sample_dialogues": len(card["sample_dialog_ids"]),
                "covered_combinations": len(card["required_combinations"])}
    if card.get("timings", {}).get("end_utc") and card.get("qa_summary"):
        raise ValueError("Dieser Run ist bereits abgeschlossen; zum Fortsetzen nur unvollständige Runs erneut starten")
    start = time.monotonic()
    started_at = utc_now()
    card["timings"]["start_utc"] = card["timings"].get("start_utc") or started_at
    attempts = card["timings"].setdefault("resume_attempts", [])
    attempts.append({"start_utc": started_at, "end_utc": None, "elapsed_seconds": None})
    card["qa_summary"] = None
    card_path.write_text(json.dumps(card, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    asset_root = Path(card["asset_root"])
    result = subprocess.run(command, cwd=asset_root, text=True, capture_output=True)
    log = card_path.parent / "runner.log"
    with log.open("a", encoding="utf-8") as stream:
        if stream.tell(): stream.write(f"\n--- resume attempt {len(attempts)} started {started_at} ---\n")
        stream.write(result.stdout + result.stderr)
    elapsed = time.monotonic() - start
    attempts[-1].update(end_utc=utc_now(), elapsed_seconds=round(elapsed, 3))
    card["timings"]["end_utc"] = attempts[-1]["end_utc"]
    card["timings"]["elapsed_seconds"] = round(sum(item["elapsed_seconds"] or 0 for item in attempts), 3)
    if result.returncode not in (0, 2):
        card["return_code"] = result.returncode
        card_path.write_text(json.dumps(card, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return {"selection": card["selection"], "executed": True, "returncode": result.returncode,
                "elapsed_seconds": round(elapsed, 3), "log": str(log), "resumable": True}
    output = None
    for line in reversed(result.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("output"), str):
            output = value["output"]
            break
    if output is None:
        raise ValueError("Renderer beendete sich ohne Run-Ausgabepfad; Laufkarte für Diagnose erhalten")
    qa_path = (asset_root / output / "qa-summary.json")
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    card["qa_summary"] = {"path": str(qa_path), "status": qa.get("status"), "counts": qa.get("counts")}
    card["return_code"] = result.returncode
    card_path.write_text(json.dumps(card, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {"selection": card["selection"], "executed": True, "returncode": result.returncode,
            "elapsed_seconds": round(elapsed, 3), "qa_summary": card["qa_summary"], "log": str(log)}


def run_series(series_dir: Path, *, execute=False):
    order=("ane-gpu-free","ane-gpu","ane","gpu")
    cards=sorted((path for path in series_dir.glob("*/run-card.json") if path.parent.name in order),
                 key=lambda p: json.loads(p.read_text()).get("series_order",99))
    if [json.loads(p.read_text()).get("selection") for p in cards] != list(order):
        raise ValueError("Serie benötigt freie ANE+GPU-, figurenfeste ANE+GPU-, ANE- und GPU-Karte in dieser Reihenfolge")
    ids=[json.loads(p.read_text()).get("sample_dialog_ids") for p in cards]
    signatures=[json.loads(p.read_text()).get("required_combinations") for p in cards]
    if not all(item==ids[0] for item in ids) or not all(item==signatures[0] for item in signatures):
        raise ValueError("Serienkarten haben keine identische Dialog-/Kombinationsauswahl")
    report_path=series_dir/"series-report.json"
    state={"series_id":"plan07-m1-four-mode-routing-v1","status":"running","sample_dialog_ids":ids[0],
           "required_combinations":signatures[0],"runs":[]}
    for path in cards:
        saved=json.loads(path.read_text(encoding="utf-8"))
        saved_qa=saved.get("qa_summary") or {}
        try:
            if execute and saved.get("timings",{}).get("end_utc") and saved_qa.get("status") in {"complete","pass"} and not saved_qa.get("counts",{}).get("fail",0) and saved.get("return_code")==0:
                result={"selection":saved["selection"],"executed":False,"returncode":0,
                        "qa_summary":saved_qa,"resumed":"already complete"}
            else:
                result=run(path,execute=execute)
        except KeyboardInterrupt:
            result={"selection":saved["selection"],"executed":execute,"outcome":"aborted",
                    "error":"series interrupted by user"}
            state["runs"].append(result);state["status"]="interrupted";state["returncode"]=130
            state["stop_reason"]="explicit user interruption; remaining modes were not started"
            report_path.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n")
            return {"executed":execute,"runs":state["runs"],"report":str(report_path),"returncode":130}
        except Exception as exc:
            result={"selection":saved["selection"],"executed":execute,"returncode":1,
                    "outcome":"error","error":f"{type(exc).__name__}: {exc}"}
        if execute:
            qa=result.get("qa_summary") or {};counts=qa.get("counts",{})
            passed=(result.get("returncode")==0 and qa.get("status") in {"complete","pass"}
                    and counts.get("fail",0)==0 and result.get("outcome")!="error")
            result["outcome"]="passed" if passed else (result.get("outcome") or "failed")
            result["error"] = result.get("error") or (None if passed else
                f"run/QA failed: returncode={result.get('returncode')}, qa_status={qa.get('status')}, counts={counts}")
        state["runs"].append(result)
        report_path.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n")
    if not execute:
        state["status"]="dry_run";state["returncode"]=0
    else:
        failures=[r["selection"] for r in state["runs"] if r.get("outcome")!="passed"]
        state["status"]="failed" if failures else "complete"
        state["returncode"]=1 if failures else 0
        if failures: state["failed_modes"]=failures
    report_path.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n")
    return {"executed":execute,"runs":state["runs"],"report":str(report_path),"returncode":state["returncode"],
            "next_action":"M1-Lauf mit --execute starten" if not execute else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_card", type=Path)
    parser.add_argument("--execute", action="store_true", help="explicitly load target models and run this sample")
    parser.add_argument("--series", action="store_true", help="run all four Plan-07 M1 modes sequentially and summarize failures after all have ended")
    args = parser.parse_args(argv)
    try:
        result=run_series(args.run_card,execute=args.execute) if args.series else run(args.run_card, execute=args.execute)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.series: return result.get("returncode", 0)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
