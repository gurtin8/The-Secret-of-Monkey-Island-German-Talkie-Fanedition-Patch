"""Low-overhead, append-only timing events for long manifest runs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import uuid


class RunTiming:
    """Write structured run events as they happen; partial logs survive errors."""

    def __init__(self, asset_root: Path, *, label: str | None = None):
        self.run_id = uuid.uuid4().hex
        self.label = label
        self.started = time.monotonic()
        self.run_root: str | None = None
        log_dir = Path(asset_root) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.path = log_dir / f"manifest-timing-{stamp}-{self.run_id[:8]}.jsonl"
        self._lock = threading.Lock()
        self.event("run_start", counts={"label": label})

    def event(self, name: str, *, counts: dict | None = None, **details):
        now = time.monotonic()
        row = {"run_id": self.run_id, "event": name,
               "utc": datetime.now(timezone.utc).isoformat(),
               "elapsed_seconds": round(now - self.started, 6),
               "counts": counts or {}}
        if self.label is not None:
            row["label"] = self.label
        if self.run_root is not None:
            row["run_root"] = self.run_root
        row.update(details)
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
                stream.flush()

    def bind_run_root(self, run_root: str):
        self.run_root = run_root
        self.event("run_bound", run_root=run_root)

    @contextmanager
    def phase(self, name: str, *, counts: dict | None = None, announce: bool = False, **details):
        start = time.monotonic()
        self.event(name, state="start", counts=counts, **details)
        if announce:
            print(f"Start: {name}" + (f" ({counts})" if counts else ""), flush=True)
        try:
            yield
        except BaseException as exc:
            elapsed = round(time.monotonic() - start, 6)
            self.event(name, state="error", counts=counts,
                       phase_elapsed_seconds=elapsed,
                       error_type=type(exc).__name__, error=str(exc))
            if announce:
                print(f"Fehler: {name} nach {elapsed:.2f}s", flush=True)
            raise
        else:
            elapsed = round(time.monotonic() - start, 6)
            self.event(name, state="end", counts=counts, phase_elapsed_seconds=elapsed)
            if announce:
                print(f"Fertig: {name} nach {elapsed:.2f}s", flush=True)

    def finish(self, *, status: str, counts: dict | None = None):
        self.event("run_end", state=status, counts=counts,
                   total_elapsed_seconds=round(time.monotonic() - self.started, 6))
