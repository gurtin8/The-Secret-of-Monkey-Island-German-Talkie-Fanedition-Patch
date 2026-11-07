"""Expose only the live, explicitly started German patch process to its UI."""
from __future__ import annotations

import subprocess


def run_patch_process(*, command, cwd, environment, log, interaction, publish, popen=None):
    popen = popen or subprocess.Popen
    with popen(command, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT) as process:
        descriptor = {**interaction, "process_pid": process.pid}
        publish(descriptor, process)
        try:
            result = process.wait()
        finally:
            publish(None, None)
        if result:
            raise subprocess.CalledProcessError(result, command)
    return {"patch_process_pid": descriptor["process_pid"]}
