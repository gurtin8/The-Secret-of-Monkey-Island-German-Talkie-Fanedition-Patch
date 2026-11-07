"""Model-free host facts and deliberately provisional renderer guidance."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time

GIB = 1024 ** 3


def ram_snapshot(*, system=None, runner=subprocess.run, facts=None):
    """One model-free coldstart sample; free+inactive is an estimate only."""
    facts = facts or {}
    system = system or platform.system()
    result = dict(measured_at=time.time(), free_ram_bytes=None,
                  available_ram_estimate_bytes=None, system_ram_bytes=facts.get('system_ram_bytes'),
                  os_family=facts.get('os_family') or ('macos' if system == 'Darwin' else None),
                  measurement_method='macos-vm_stat-free-plus-inactive/1')
    if system != 'Darwin':
        result['measurement_method'] = None
        return result
    if result['system_ram_bytes'] is None:
        try:
            total = int(runner(['sysctl', '-n', 'hw.memsize'], capture_output=True,
                               text=True, timeout=2, check=True).stdout.strip())
            result['system_ram_bytes'] = total if total > 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    try:
        output = runner(['vm_stat'], capture_output=True, text=True, timeout=2, check=True).stdout
        page = re.search(r'page size of (\d+) bytes', output)
        free = re.search(r'^Pages free:\s*(\d+)\.\s*$', output, re.M)
        inactive = re.search(r'^Pages inactive:\s*(\d+)\.\s*$', output, re.M)
        if page and free and inactive:
            size = int(page[1])
            if 0 < size <= 1024 * 1024 and size & (size - 1) == 0:
                available = (int(free[1]) + int(inactive[1])) * size
                total = result['system_ram_bytes']
                if total is None or available <= total:
                    result['free_ram_bytes'] = int(free[1]) * size
                    result['available_ram_estimate_bytes'] = available
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def host_facts(*, system=None, machine=None, runner=subprocess.run, disk_path=None):
    system = system or platform.system()
    machine = machine or platform.machine()
    os_family = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(system)
    chip = None
    if os_family == "macos":
        try:
            chip = runner(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                          text=True, timeout=2, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        os_version = platform.mac_ver()[0]
        try:
            ram = int(runner(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                             timeout=2, check=True).stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            ram = None
    else:
        chip = None
        os_version = platform.release() if os_family else None
        ram = None
        if os_family == "linux":
            try:
                for line in Path("/proc/meminfo").read_text().splitlines():
                    if line.startswith("MemTotal:"):
                        ram = int(line.split()[1]) * 1024
                        break
            except (OSError, ValueError):
                pass
    memory_available = None
    if disk_path:
        try:
            memory_available = shutil.disk_usage(disk_path).free
        except OSError:
            pass
    return {"os_family": os_family, "os_version": os_version or None,
            "architecture": {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64"}.get(machine),
            "chip": chip or None, "system_ram_bytes": ram,
            "available_disk_bytes": memory_available}


def recommend(facts):
    """Return an advisory only; selection is always the caller's explicit choice."""
    ram = facts.get("system_ram_bytes")
    chip = facts.get("chip") or ""
    known_apple = facts.get("os_family") == "macos" and facts.get("architecture") == "arm64" and chip.startswith("Apple ")
    if ram is None or not known_apple:
        candidate, reason = "cpu", "Hardwareangaben unvollständig oder kein belegter Apple-Silicon-Host."
    elif ram < 16 * GIB:
        candidate, reason = "cpu", "Unter 16 GiB RAM: konservativ CPU-Einzelbetrieb erwägen."
    elif ram < 32 * GIB:
        candidate, reason = "gpu", "Ab 16 GiB ist Parallelbetrieb wählbar; zunächst GPU-Einzelbetrieb empfehlen."
    else:
        candidate, reason = "ane-gpu", "Ab 32 GiB kann Parallelbetrieb geprüft werden; ANE-Assets und freier Speicher bleiben Pflicht."
    free = facts.get("available_disk_bytes")
    if free is not None and free < 20 * GIB:
        reason += " Verfügbarer Speicher liegt unter dem vorläufigen 20-GiB-Puffer."
    unknown = [label for key, label in (("chip", "Chipmodell"), ("system_ram_bytes", "RAM"),
                ("available_disk_bytes", "freier Speicher"), ("os_version", "Betriebssystemversion")) if facts.get(key) is None]
    return {"candidate": candidate, "provisional": True, "reason": reason,
            "unknown": unknown, "parallel_selectable": bool(ram is not None and ram >= 16 * GIB),
            "supported_apple_silicon": known_apple}


def report(root=None):
    facts = host_facts(disk_path=root)
    return {"facts": facts, "recommendation": recommend(facts)}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disk-path", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(report(args.disk_path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
