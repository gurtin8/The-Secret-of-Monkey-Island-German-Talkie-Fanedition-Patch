"""Optional, persistent VoxCPMANE2 adapter and line-oriented worker protocol.

Importing this module does not import CoreML or VoxCPMANE2. The runtime is
loaded only when an ANE worker is constructed on a supported host.
"""
from __future__ import annotations

import json
import hashlib
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import sys
import array
import contextlib
import threading
import tempfile
import time
import wave

from .audio import sha256_file, validate_wav

PROTOCOL = "MI1_ANE_REQUEST"
RATE = 48_000


class AneCancelled(RuntimeError):
    """Raised at a streaming chunk boundary after closing the runtime iterator."""


def _sha(path: str | Path) -> str:
    return sha256_file(Path(path))


def _safe_file(path: str, expected: str | None = None) -> Path:
    value = Path(path)
    if value.is_symlink() or not value.is_file():
        raise ValueError(f"ANE input is missing or unsafe: {value}")
    if expected and _sha(value) != expected:
        raise ValueError(f"ANE input hash mismatch: {value}")
    return value


_PATH_DIGEST_CACHE = {}


def _path_snapshot(path: Path):
    """Stat identity for local, hash-verified content; include added/removed files."""
    if path.is_symlink() or not path.exists():
        raise ValueError(f"ANE model binding is missing or unsafe: {path}")
    items=[path] if path.is_file() else sorted(path.rglob("*"))
    result=[]
    for item in items:
        if item.is_symlink(): raise ValueError(f"Symlink in ANE model binding: {item}")
        if item.is_file():
            st=item.stat()
            result.append([item.relative_to(path).as_posix() if item!=path else '.',
                           st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns])
    return result


def _path_digest(path: Path) -> str:
    """Hash files and compiled-model trees deterministically; reject symlinks."""
    if path.is_symlink() or not path.exists():
        raise ValueError(f"ANE model binding is missing or unsafe: {path}")
    snapshot=_path_snapshot(path)
    cached=_PATH_DIGEST_CACHE.get(str(path.absolute()))
    if cached and cached[0]==snapshot: return cached[1]
    if path.is_file():
        value=sha256_file(path)
        _PATH_DIGEST_CACHE[str(path.absolute())]=(snapshot,value)
        return value
    if not path.is_dir():
        raise ValueError(f"ANE model binding is not a file or directory: {path}")
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"Symlink in ANE model binding: {item}")
        if item.is_file():
            relative = item.relative_to(path).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big")); digest.update(relative)
            digest.update(bytes.fromhex(sha256_file(item)))
    value=digest.hexdigest()
    _PATH_DIGEST_CACHE[str(path.absolute())]=(snapshot,value)
    return value


def resolve_bindings(bindings: dict, *, verify_hashes: bool = True) -> tuple[dict, str, str, bool]:
    """Validate manifest-bound local model/tokenizer receipts."""
    root_input = Path(bindings["model_root"])
    if root_input.is_symlink():
        raise ValueError("ANE model_root cannot be a symlink")
    root = root_input.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("ANE model_root must be a local directory")
    paths = {}
    for key, receipt in bindings["model_paths"].items():
        values = receipt if isinstance(receipt, list) else [receipt]
        resolved = []
        for item in values:
            rel = Path(item["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError("ANE component paths must be relative to model_root")
            candidate = root
            for part in rel.parts:
                candidate = candidate / part
                if candidate.is_symlink():
                    raise ValueError(f"Symlink in ANE component path: {rel}")
            target = candidate.resolve(strict=True)
            if not target.is_relative_to(root):
                raise ValueError(f"ANE component escapes model_root: {rel}")
            if verify_hashes and _path_digest(target) != item["sha256"]:
                raise ValueError(f"ANE model component hash mismatch: {rel}")
            resolved.append(str(target))
        paths[key] = resolved if isinstance(receipt, list) else resolved[0]
    tokenizer = bindings["tokenizer"]
    token_rel = Path(tokenizer["path"])
    if token_rel.is_absolute() or ".." in token_rel.parts:
        raise ValueError("ANE tokenizer path must be relative to model_root")
    token_candidate = root
    for part in token_rel.parts:
        token_candidate = token_candidate / part
        if token_candidate.is_symlink():
            raise ValueError("Symlink in ANE tokenizer path")
    token_path = token_candidate.resolve(strict=True)
    if not token_path.is_relative_to(root):
        raise ValueError("ANE tokenizer escapes model_root")
    if verify_hashes and _path_digest(token_path) != tokenizer["sha256"]:
        raise ValueError("ANE tokenizer hash mismatch")
    cache = Path(bindings["cache_dir"]).resolve(strict=True)
    if not cache.is_dir():
        raise ValueError("ANE cache_dir must be an existing local directory")
    return paths, str(token_path), str(cache), bool(bindings.get("split_base_lm", False))


class AneWorker:
    """Single-threaded generator owner. A loader can be injected for tests."""
    def __init__(self, model_paths: dict[str, str], *, tokenizer_path: str,
                 cache_dir: str, split_base_lm: bool = False,
                 decoder_context_mode: str = "prompt_context",
                 loader=None, generator=None):
        if decoder_context_mode not in {"prompt_context", "fresh_reset_no_context"}:
            raise ValueError("Unsupported ANE decoder context mode")
        self.model_paths = {key: ([str(Path(item).resolve(strict=True)) for item in value]
                                  if isinstance(value, (list, tuple)) else str(Path(value).resolve(strict=True)))
                            for key, value in model_paths.items()}
        self.tokenizer_path = str(Path(tokenizer_path).resolve(strict=True))
        self.cache_dir = str(Path(cache_dir).resolve())
        self.split_base_lm = split_base_lm
        self.decoder_context_mode = decoder_context_mode
        self.model_identity = None
        self._reference_features: dict[str, object] = {}
        if generator is not None:
            self.generator = generator
        else:
            self.generator = (loader or self._load_runtime)()
        if decoder_context_mode == "fresh_reset_no_context":
            self._install_fresh_decoder_hook()

    def _install_fresh_decoder_hook(self):
        """Opt-in experiment: reset VAE state without priming from prompt latents."""
        from types import MethodType
        generator = self.generator
        if not callable(getattr(generator, "_warm_vae_decoder", None)):
            raise RuntimeError("ANE generator lacks the pinned VAE warmup hook")
        decoder = getattr(generator, "vae_decoder", None)
        if not callable(getattr(decoder, "reset", None)):
            raise RuntimeError("ANE generator lacks a resettable VAE decoder")
        self._original_decoder_warm_hook = generator._warm_vae_decoder

        def reset_without_context(instance, _decode_context):
            instance.vae_decoder.reset()

        generator._warm_vae_decoder = MethodType(reset_without_context, generator)

    @classmethod
    def from_bindings(cls, bindings: dict, *, loader=None, generator=None, verify_hashes=True):
        paths, tokenizer, cache, split = resolve_bindings(bindings, verify_hashes=verify_hashes)
        worker=cls(paths, tokenizer_path=tokenizer, cache_dir=cache,
                   split_base_lm=split,
                   decoder_context_mode=bindings.get("decoder_context_mode", "prompt_context"),
                   loader=loader, generator=generator)
        worker.model_identity={"components":bindings["model_paths"],
                               "tokenizer":bindings["tokenizer"],
                               "split_base_lm":split,
                               "decoder_context_mode":worker.decoder_context_mode}
        return worker

    def _load_runtime(self):
        if os.uname().sysname != "Darwin" or os.uname().machine != "arm64":
            raise RuntimeError("VoxCPMANE2 requires Apple Silicon macOS")
        # The upstream server may consult the Hub for voice-cache fallbacks.
        # Offline mode makes missing locally-bound files fail before rendering.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            installed = version("voxcpmane2")
        except PackageNotFoundError as exc:
            raise RuntimeError("Install the pinned voxcpmane2==0.1.3b1 runtime") from exc
        if installed != "0.1.3b1":
            raise RuntimeError(f"Unsupported voxcpmane2 version {installed}; expected 0.1.3b1")
        try:
            from voxcpmane.server import load_model
        except ImportError as exc:
            raise RuntimeError("Install the pinned voxcpmane2==0.1.3b1 runtime") from exc
        required = {"model_dir", "base_lm_path", "residual_lm_path", "locdit_path",
                    "vae_encoder_path", "feat_encoder_path", "vae_decoder_path",
                    "fsq_path", "projections_path", "embedding_path"}
        if not required.issubset(self.model_paths):
            raise ValueError("ANE model binding is incomplete")
        for value in (*self.model_paths.values(), self.tokenizer_path):
            for path in value if isinstance(value, list) else [value]:
                if not Path(path).exists():
                    raise FileNotFoundError(f"Bound ANE runtime path missing: {path}")
        from voxcpmane import server
        from voxcpmane.generator import VoxCPM2Generator
        try:
            from tokenizers import Tokenizer
            import coremltools as ct
        except ImportError as exc:
            raise RuntimeError("VoxCPMANE2 CoreML/tokenizer dependency is unavailable") from exc
        original_preload = VoxCPM2Generator.preload_tokenizer
        original_init = VoxCPM2Generator.__init__
        tokenizer_path = self.tokenizer_path

        def preload_local(generator, _hf_model_id="openbmb/VoxCPM2"):
            if not hasattr(generator, "_tokenizer"):
                generator._tokenizer = Tokenizer.from_file(tokenizer_path)
                generator._unk_token_id = generator._tokenizer.token_to_id("<unk>")
                generator._multichar_chinese_tokens = {
                    token for token in generator._tokenizer.get_vocab().keys()
                    if len(token) >= 2 and all("\u4e00" <= char <= "\u9fff" for char in token)
                }

        def init_with_ane_units(generator, *args, **kwargs):
            kwargs.update(lm_compute_units=ct.ComputeUnit.CPU_AND_NE,
                          feat_compute_units=ct.ComputeUnit.CPU_AND_NE,
                          locdit_compute_units=ct.ComputeUnit.CPU_AND_NE,
                          proj_compute_units=ct.ComputeUnit.CPU_AND_NE)
            return original_init(generator, *args, **kwargs)

        VoxCPM2Generator.preload_tokenizer = preload_local
        VoxCPM2Generator.__init__ = init_with_ane_units
        options = dict(self.model_paths)
        options["included_voice_cache_dir"] = self.cache_dir
        options["split_base_lm"] = self.split_base_lm
        # The pinned Beta server annotates base_lm_path as list[str] and calls
        # list(base_lm_path); always wrap a single path to avoid iterating its
        # characters (which previously turned the 89-char path into 89 entries).
        if isinstance(options["base_lm_path"], str):
            options["base_lm_path"] = [options["base_lm_path"]]
        try:
            load_model(**options)
        finally:
            VoxCPM2Generator.preload_tokenizer = original_preload
            VoxCPM2Generator.__init__ = original_init
        return server.generator

    def run(self, request: dict, cancel_event: threading.Event | None = None) -> dict:
        started = time.perf_counter()
        required = {"request_id", "attempt_fingerprint", "text", "reference_path",
                    "reference_sha256", "output_path", "log_path", "seed", "mode"}
        if not required.issubset(request):
            raise ValueError("ANE request is missing required fields")
        if request["mode"] not in {"isolated", "combined", "prewarm"}:
            raise ValueError(f"Unsupported ANE mode: {request['mode']}")
        reference = request.get("reference_path")
        if reference:
            _safe_file(reference, request.get("reference_sha256"))
        elif request.get("reference_sha256"):
            raise ValueError("ANE reference hash supplied without reference")
        prompt = request.get("prompt_path")
        prompt_text = request.get("prompt_text") or ""
        if bool(prompt) != bool(prompt_text):
            raise ValueError("ANE prompt audio and text must be supplied together")
        if prompt:
            _safe_file(prompt, request.get("prompt_sha256"))
        if request["mode"] in {"isolated", "prewarm"} and (prompt or prompt_text):
            raise ValueError("ANE isolated mode does not accept a combined prompt")
        if request["mode"] == "prewarm" and not reference:
            raise ValueError("ANE prewarm requires its line original reference")
        seed = request["seed"]
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ValueError("ANE seed must be a nonnegative integer")
        params = request.get("parameters", {})
        allowed = {"inference_timesteps": 10, "cfg_value": 2.0, "min_len": 2}
        if any(params.get(k, v) != v for k, v in allowed.items()):
            raise ValueError("Unsupported ANE generation parameters")
        max_len = params.get("max_len")
        if max_len is None:
            # Token counting belongs beside the loaded generator. The renderer
            # owns an AneWorkerProcess and intentionally has no in-process
            # ``generator`` attribute.
            encoder = getattr(self.generator, "_encode_text", None)
            if encoder is None:
                raise ValueError("ANE generator cannot resolve a token-bounded max_len")
            max_len = min(6 * len(encoder(request["text"])) + 10, 2048)
        if not isinstance(max_len, int) or max_len < 2 or max_len > 2048:
            raise ValueError("ANE max_len must be resolved to an integer in [2, 2048]")
        output, log = Path(request["output_path"]), Path(request["log_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() or output.is_symlink() or log.exists() or log.is_symlink():
            raise FileExistsError("ANE result target already exists")
        tmp = None
        pcm = array.array("h")
        try:
            with tempfile.NamedTemporaryFile(prefix=".ane-", suffix=".wav", dir=output.parent, delete=False) as f:
                tmp = Path(f.name)
            kwargs = {"target_text": request["text"], "reference_wav_path": reference or "",
                      "prompt_wav_path": prompt or "", "prompt_text": prompt_text,
                      "seed": seed, "inference_timesteps": 10, "cfg_value": 2.0,
                      "min_len": 2, "max_len": max_len}
            chunks = self.generator.generate_streaming(**kwargs)
            sample_rate = int(getattr(self.generator, "vae_out_sample_rate", RATE))
            if sample_rate != RATE:
                raise ValueError(f"ANE VAE rate {sample_rate} requires an explicit supported conversion")
            iterator = iter(chunks)
            for chunk in iterator:
                if cancel_event is not None and cancel_event.is_set():
                    close = getattr(iterator, "close", None)
                    if close:
                        close()
                    raise AneCancelled("ANE request cancelled at audio chunk boundary")
                values = chunk.reshape(-1).tolist() if hasattr(chunk, "reshape") else list(chunk)
                for sample in values:
                    value = max(-1.0, min(1.0, float(sample)))
                    pcm.append(int(round(value * (32768 if value < 0 else 32767))))
            if not pcm:
                raise RuntimeError("ANE generator produced no audio chunks")
            with wave.open(str(tmp), "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate)
                wav.writeframes(pcm.tobytes())
            wav = validate_wav(tmp)
            if request["mode"] == "prewarm" and not 2000 <= wav["duration_ms"] <= 5000:
                raise ValueError("ANE prewarm must produce 2000–5000 ms")
            os.link(tmp, output)
            tmp.unlink(); tmp = None
            elapsed = time.perf_counter() - started
            log_data = {"request_id": request["request_id"], "attempt_fingerprint": request["attempt_fingerprint"],
                        "status": "ok", "backend": "coreml_cpu_and_ne", "seed": seed,
                        "parameters": {**allowed, "max_len": max_len}, "wav": wav,
                        "duration_seconds": elapsed, "runtime": "voxcpmane2:0.1.3b1",
                        "decoder_context_mode": self.decoder_context_mode,
                        "runtime_identity": request.get("runtime_identity"),
                        "model_identity": request.get("model_identity") or self.model_identity}
            with log.open("x", encoding="utf-8") as stream:
                json.dump(log_data, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
            return log_data
        except BaseException as exc:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            # Never remove a pre-existing caller artifact.
            if output.exists() and not output.is_symlink() and log.exists() is False:
                output.unlink()
            partial = output.with_name(output.name + ".partial.wav")
            partial_written = False
            if pcm and not output.exists() and not partial.exists() and not partial.is_symlink():
                try:
                    with partial.open("xb") as handle:
                        with wave.open(handle, "wb") as wav:
                            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE)
                            wav.writeframes(pcm.tobytes())
                    partial_written = True
                except (OSError, wave.Error):
                    partial.unlink(missing_ok=True)
            with log.open("x", encoding="utf-8") as stream:
                json.dump({"request_id": request.get("request_id"),
                           "status": "cancelled" if isinstance(exc, AneCancelled) else "error",
                           "partial_path": str(partial) if partial_written else None,
                           "error": f"{type(exc).__name__}: {exc}"}, stream)
                stream.write("\n")
            raise


def serve(worker: AneWorker, source, sink, *, cancel_event: threading.Event | None = None) -> None:
    """Process JSON-lines requests; malformed lines get an explicit response."""
    for line in source:
        request = None
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                result = worker.run(request, cancel_event)
            response = {"protocol": PROTOCOL, "request_id": request.get("request_id"),
                        "status": "ok", "result": result}
        except BaseException as exc:
            response = {"protocol": PROTOCOL, "request_id": request.get("request_id") if isinstance(request,dict) else None,
                        "status": "error", "error_type":type(exc).__name__, "error": f"{type(exc).__name__}: {exc}"}
        sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sink.flush()
        if response["status"] != "ok":
            # A failed generator is not reused; process exit releases CoreML state.
            break
