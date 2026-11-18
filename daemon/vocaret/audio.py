"""Microphone capture.

Recording is delegated to the system recorder (`pw-record` on PipeWire,
`arecord` as a fallback) writing raw 16 kHz mono PCM to a pipe. That avoids a
native audio dependency and follows whatever input device the desktop has
selected.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable

import numpy as np

RATE = 16000
CHUNK_SECONDS = 0.1
CHUNK_BYTES = int(RATE * CHUNK_SECONDS) * 2


class RecorderError(RuntimeError):
    pass


def recorder_command(custom: str = "") -> list[str]:
    """Command that writes raw 16 kHz mono s16le PCM to stdout."""
    if custom:
        return shlex.split(custom)
    sox = ["rec", "-q", "-r", str(RATE), "-c", "1", "-b", "16", "-e", "signed-integer",
           "-t", "raw", "-"]
    if sys.platform == "darwin":
        # macOS support is best-effort: SoX, or ffmpeg's AVFoundation input.
        if shutil.which("rec"):
            return sox
        if shutil.which("ffmpeg"):
            return ["ffmpeg", "-v", "error", "-f", "avfoundation", "-i", ":default",
                    "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
        raise RecorderError("No recorder found. Install SoX (`brew install sox`) or ffmpeg.")
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(RATE), "--channels", "1",
                "--format", "s16", "-"]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                "-t", "raw", "-"]
    if shutil.which("rec"):
        return sox
    raise RecorderError(
        "No recorder found. Install pipewire-bin (pw-record), alsa-utils (arecord) or sox."
    )


def strip_container_header(raw: bytes) -> bytes:
    """Some pw-record versions put a WAV header on the pipe; drop it if present."""
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        idx = raw.find(b"data", 12, 4096)
        if idx != -1:
            return raw[idx + 8:]
    return raw


class Recorder:
    """One recording. start() -> (events via callbacks) -> stop() returns audio."""

    def __init__(
        self,
        silence_seconds: float = 0,
        max_seconds: float = 180,
        on_level: Callable[[float], None] | None = None,
        on_autostop: Callable[[str], None] | None = None,
        command: str = "",
    ):
        self.command = command
        self.silence_seconds = float(silence_seconds or 0)
        self.max_seconds = float(max_seconds or 180)
        self.on_level = on_level
        self.on_autostop = on_autostop
        self._proc: subprocess.Popen | None = None
        self._chunks: list[bytes] = []
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.first_audio = threading.Event()
        self._last_speech: float | None = None
        self._lock = threading.Lock()
        self.stop_reason: str | None = None

    def start(self) -> None:
        cmd = recorder_command(self.command)
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except OSError as exc:
            raise RecorderError(f"Could not start {cmd[0]}: {exc}") from exc
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        # Time is measured in audio captured, not wall clock, so the result
        # does not depend on how the recorder batches its output.
        started = 0.0
        heard = 0
        last_check = 0.0
        pending = b""
        while not self._stopping.is_set():
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                break
            pending += data
            usable = len(pending) - (len(pending) % 2)
            chunk, pending = pending[:usable], pending[usable:]
            if not chunk:
                continue
            with self._lock:
                self._chunks.append(chunk)
            self.first_audio.set()
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
            heard += samples.size
            now = heard / RATE
            silent_for = None
            if self.silence_seconds > 0 and now - last_check >= 0.3:
                last_check = now
                silent_for = self._silence_after_speech(now)
            if self.on_level:
                try:
                    self.on_level(min(1.0, rms * 8.0))
                except Exception:
                    pass
            reason = None
            if now - started >= self.max_seconds:
                reason = "max"
            elif silent_for is not None and silent_for >= self.silence_seconds:
                reason = "silence"
            if reason:
                self.stop_reason = reason
                self._terminate()
                if self.on_autostop:
                    try:
                        self.on_autostop(reason)
                    except Exception:
                        pass
                return

    def _silence_after_speech(self, now: float) -> float | None:
        """Seconds of silence since the speaker last said something.

        None until speech has been heard at all, so a quiet start never ends
        the recording. Uses the Silero voice detector that ships with
        faster-whisper: plain loudness thresholds cut people off mid-sentence
        whenever their volume dips.
        """
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        window = int((self.silence_seconds + 8.0) * RATE) * 2
        with self._lock:
            raw = b"".join(self._chunks)[-window:]
        audio = np.frombuffer(raw[len(raw) % 2:], dtype=np.int16).astype(np.float32) / 32768.0
        try:
            spans = get_speech_timestamps(
                audio, VadOptions(min_silence_duration_ms=200, speech_pad_ms=60))
        except Exception:
            return None
        if spans:
            self._last_speech = now - (len(audio) - spans[-1]["end"]) / RATE
        if self._last_speech is None:
            return None
        return now - self._last_speech

    def _terminate(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def stop(self) -> np.ndarray:
        """Stop recording and return float32 mono audio at 16 kHz."""
        if self.stop_reason is None:
            self.stop_reason = "manual"
        self._stopping.set()
        self._terminate()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        proc = self._proc
        with self._lock:
            raw = b"".join(self._chunks)
        if not raw and proc is not None:
            err = b""
            try:
                err = proc.stderr.read() if proc.stderr else b""
            except Exception:
                pass
            detail = err.decode(errors="replace").strip().splitlines()
            raise RecorderError(
                "The microphone produced no audio"
                + (f": {detail[-1]}" if detail else ".")
            )
        raw = strip_container_header(raw)
        raw = raw[: len(raw) - (len(raw) % 2)]
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def cancel(self) -> None:
        self.stop_reason = "cancelled"
        self._stopping.set()
        self._terminate()


def load_audio_file(path: str) -> np.ndarray:
    """Decode an audio file to float32 mono 16 kHz (used by the CLI and tests)."""
    if shutil.which("ffmpeg"):
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1",
             "-ar", str(RATE), "-"],
            capture_output=True,
        )
        if out.returncode != 0:
            raise RecorderError(out.stderr.decode(errors="replace").strip() or "ffmpeg failed")
        return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    import wave

    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2:
            raise RecorderError("Without ffmpeg only 16-bit WAV files can be read.")
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        data = data.reshape(-1, wav.getnchannels()).mean(axis=1) / 32768.0
        rate = wav.getframerate()
    if rate != RATE:
        target = np.linspace(0, len(data) - 1, int(len(data) * RATE / rate))
        data = np.interp(target, np.arange(len(data)), data)
    return data.astype(np.float32)
