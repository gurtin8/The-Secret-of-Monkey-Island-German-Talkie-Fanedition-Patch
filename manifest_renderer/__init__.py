"""Deterministic renderer core for executable dubbing manifests.

Public manifest helpers are loaded lazily so audio-only tools do not require the
full manifest validator and schema bundle at import time.
"""

__all__ = ["ManifestError", "load_manifest", "resolve_manifest", "build_run_plan"]


def __getattr__(name):
    if name in {"ManifestError", "load_manifest", "resolve_manifest"}:
        from . import manifest
        return getattr(manifest, name)
    if name == "build_run_plan":
        from .plan import build_run_plan
        return build_run_plan
    raise AttributeError(name)
