"""Local speech-to-text with faster-whisper. Audio never leaves the machine."""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import re
import sys
import threading
import time
from collections import Counter

import numpy as np

log = logging.getLogger("vocaret.stt")

_cuda_libs_loaded = False

# Words Whisper should expect; steers it toward programming vocabulary.
BASE_HINT = (
    "Dictating programming instructions: public static void, String, int, boolean, "
    "constructor, parameter, argument, return, class, method, function, for loop, "
    "if else, null, async, await, import."
)


def _preload_cuda_libs() -> None:
    """Make pip-installed cuBLAS/cuDNN visible to CTranslate2.

    CTranslate2 dlopen()s them lazily by soname, and LD_LIBRARY_PATH is read
    only at process start, so load them by full path up front instead.
    """
    global _cuda_libs_loaded
    if _cuda_libs_loaded:
        return
    _cuda_libs_loaded = True
    for site in sys.path:
        base = os.path.join(site, "nvidia")
        if not os.path.isdir(base):
            continue
        for pattern in ("cublas/lib/libcublasLt.so.*", "cublas/lib/libcublas.so.*",
                        "cudnn/lib/libcudnn*.so.*"):
            for lib in sorted(glob.glob(os.path.join(base, pattern))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError as exc:
                    log.debug("could not preload %s: %s", lib, exc)


def identifier_hint(text: str, limit: int = 60) -> str:
    """Most frequent identifiers in the open file, so Whisper spells them right."""
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")
    common = [w for w, _ in Counter(words).most_common(limit * 2)]
    seen: list[str] = []
    for word in common:
        if word.lower() not in {s.lower() for s in seen}:
            seen.append(word)
        if len(seen) >= limit:
            break
    return ", ".join(seen)


class Transcriber:
    def __init__(self) -> None:
        self._model = None
        self._key: tuple | None = None
        self._lock = threading.Lock()
        self.device: str | None = None
        self.model_name: str | None = None

    def _build(self, name: str, device: str):
        from faster_whisper import WhisperModel as _WhisperModel

        def WhisperModel(*args, **kwargs):
            # Use the cached copy without touching the network when there is
            # one; only a model that was never downloaded goes to the hub.
            try:
                return _WhisperModel(*args, local_files_only=True, **kwargs)
            except Exception:
                log.info("downloading speech model %s (first use only)", name)
                return _WhisperModel(*args, **kwargs)

        if device == "cuda":
            _preload_cuda_libs()
            model = WhisperModel(name, device="cuda", compute_type="float16")
            # CUDA library problems only surface on first inference; force one.
            segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)
            list(segments)
            return model
        threads = max(4, min(8, (os.cpu_count() or 8) // 2))
        return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)

    def ensure_loaded(self, cfg: dict) -> None:
        key = (cfg["whisper_model"], cfg["whisper_model_gpu"], cfg["whisper_model_cpu"],
               cfg["device"])
        with self._lock:
            if self._model is not None and self._key == key:
                return
            started = time.monotonic()
            explicit = cfg["whisper_model"] if cfg["whisper_model"] != "auto" else None
            attempts: list[tuple[str, str]] = []
            if cfg["device"] in ("auto", "cuda"):
                attempts.append((explicit or cfg["whisper_model_gpu"], "cuda"))
            if cfg["device"] in ("auto", "cpu"):
                attempts.append((explicit or cfg["whisper_model_cpu"], "cpu"))
            last_error: Exception | None = None
            for name, device in attempts:
                try:
                    self._model = self._build(name, device)
                    self._key, self.device, self.model_name = key, device, name
                    log.info("loaded whisper %s on %s in %.1fs", name, device,
                             time.monotonic() - started)
                    return
                except Exception as exc:  # fall through to the next attempt
                    last_error = exc
                    log.warning("whisper %s on %s unavailable: %s", name, device, exc)
            raise RuntimeError(f"Could not load a speech model: {last_error}")

    def transcribe(self, audio: np.ndarray, cfg: dict, file_text: str = "") -> str:
        self.ensure_loaded(cfg)
        if audio.size < 1600:  # under 0.1 s
            return ""
        hint = BASE_HINT
        identifiers = identifier_hint(file_text)
        if identifiers:
            hint += " Names in this file: " + identifiers + "."
        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=cfg["language"] or None,
                beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 700},
                initial_prompt=hint,
                condition_on_previous_text=False,
            )
            text = " ".join(seg.text.strip() for seg in segments)
        return re.sub(r"\s+", " ", text).strip()
