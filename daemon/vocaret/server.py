"""The Vocaret daemon: a Unix-socket server shared by all editor windows.

Protocol: a client connects, sends ONE JSON line (a request with an "op"),
and reads JSON lines (events) until the server closes the connection.

  start     begin recording; the connection stays open and streams
            {"event":"recording"}, {"event":"level",...} and finally
            {"event":"stopped","reason":"manual|silence|max|cancelled"}.
            If the client disconnects, recording is cancelled (no hot mic).
  finish    stop recording, transcribe, ask the model; streams "transcribing",
            "transcript", "generating" and then "result" or "error".
  instruct  same as finish but from typed text instead of audio.
  cancel    drop the current recording and/or abandon the running model call.
  providers list the model backends.   status  report state.   shutdown  exit.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import socketserver
import threading
import time

from . import __version__, config, providers
from .audio import Recorder, RecorderError
from .engine import Engine
from .llm import LLMError
from .stt import Transcriber

log = logging.getLogger("vocaret.server")


class Daemon:
    def __init__(self) -> None:
        self.transcriber = Transcriber()
        self.engine = Engine()
        self.recorder: Recorder | None = None
        self.rec_lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.active = 0
        self.server: socketserver.BaseServer | None = None

    # ------------------------------------------------------------ operations

    def op_start(self, req: dict, send) -> None:
        cfg = config.resolve(req.get("options"))
        events: queue.Queue = queue.Queue()
        recorder = Recorder(
            silence_seconds=cfg["silence_seconds"],
            max_seconds=cfg["max_seconds"],
            on_level=lambda level: events.put(("level", level)),
            on_autostop=lambda reason: events.put(("stopped", reason)),
            command=cfg["recorder_command"],
        )
        with self.rec_lock:
            if self.recorder is not None:
                self.recorder.cancel()
            recorder.start()
            self.recorder = recorder
        # Load the speech model while the user is talking.
        threading.Thread(target=self._warm, args=(cfg,), daemon=True).start()
        # Report "recording" only once audio is really flowing, so the editor
        # never tells the user to speak before the microphone is open.
        if not recorder.first_audio.wait(timeout=4.0):
            recorder.cancel()
            with self.rec_lock:
                if self.recorder is recorder:
                    self.recorder = None
            raise RecorderError("The microphone did not deliver any audio. "
                                "Check the input device in your sound settings.")
        if not send({"event": "recording"}):
            recorder.cancel()
            return
        last_level = 0.0
        while True:
            try:
                kind, value = events.get(timeout=0.2)
            except queue.Empty:
                kind, value = None, None
            if kind == "stopped" or (kind is None and recorder.stop_reason):
                send({"event": "stopped", "reason": recorder.stop_reason or value})
                return
            if kind == "level" and time.monotonic() - last_level >= 0.15:
                last_level = time.monotonic()
                if not send({"event": "level", "value": round(value, 3)}):
                    # The editor went away mid-recording: never leave the mic open.
                    recorder.cancel()
                    return
            if not recorder.running and recorder.stop_reason is None:
                recorder.stop_reason = "error"
                send({"event": "stopped", "reason": "error"})
                return

    def _warm(self, cfg: dict) -> None:
        try:
            self.transcriber.ensure_loaded(cfg)
        except Exception as exc:
            log.warning("speech model warm-up failed: %s", exc)

    def op_finish(self, req: dict, send) -> None:
        cfg = config.resolve(req.get("options"))
        ctx = req.get("context") or {}
        with self.rec_lock:
            recorder, self.recorder = self.recorder, None
        if recorder is None:
            send({"event": "error", "message": "Nothing is being recorded."})
            return
        if recorder.stop_reason == "cancelled":
            send({"event": "error", "message": "Cancelled."})
            return
        audio = recorder.stop()
        send({"event": "transcribing", "seconds": round(len(audio) / 16000, 1)})
        started = time.monotonic()
        transcript = self.transcriber.transcribe(audio, cfg, ctx.get("text") or "")
        stt_ms = int((time.monotonic() - started) * 1000)
        log.info("transcript (%d ms): %s", stt_ms, transcript)
        send({"event": "transcript", "text": transcript, "stt_ms": stt_ms})
        self._instruct(transcript, ctx, cfg, send, {"stt_ms": stt_ms})

    def op_instruct(self, req: dict, send) -> None:
        cfg = config.resolve(req.get("options"))
        self._instruct(str(req.get("text") or ""), req.get("context") or {}, cfg, send, {})

    def _instruct(self, transcript: str, ctx: dict, cfg: dict, send, extra: dict) -> None:
        if transcript.strip():
            send({"event": "generating"})
        result = self.engine.instruct(transcript, ctx, cfg)
        log.info("reply (%s ms):\n%s", result.get("llm_ms"), result.get("reply", ""))
        send({"event": "result", **extra, **result})

    def op_cancel(self, req: dict, send) -> None:
        with self.rec_lock:
            recorder, self.recorder = self.recorder, None
        if recorder is not None:
            recorder.cancel()
        self.engine.model.cancel()
        send({"event": "cancelled"})

    def op_status(self, req: dict, send) -> None:
        send({
            "event": "status",
            "version": __version__,
            "pid": os.getpid(),
            "recording": bool(self.recorder and self.recorder.running),
            "speech_model": self.transcriber.model_name,
            "device": self.transcriber.device,
        })

    def op_providers(self, req: dict, send) -> None:
        send({"event": "providers", "providers": providers.describe()})

    def op_warm(self, req: dict, send) -> None:
        cfg = config.resolve(req.get("options"))
        self.transcriber.ensure_loaded(cfg)
        self.op_status(req, send)

    def op_shutdown(self, req: dict, send) -> None:
        send({"event": "bye"})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    # --------------------------------------------------------------- plumbing

    def handle(self, req: dict, send) -> None:
        handler = getattr(self, "op_" + str(req.get("op")), None)
        if handler is None:
            send({"event": "error", "message": f"Unknown op: {req.get('op')!r}"})
            return
        self.active += 1
        self.last_activity = time.monotonic()
        try:
            handler(req, send)
        except (RecorderError, LLMError) as exc:
            send({"event": "error", "message": str(exc)})
        except Exception as exc:  # keep the daemon alive, report the failure
            log.exception("request failed")
            send({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self.active -= 1
            self.last_activity = time.monotonic()

    def idle_watch(self, minutes: float) -> None:
        if minutes <= 0:
            return
        while True:
            time.sleep(30)
            idle = time.monotonic() - self.last_activity
            if self.active == 0 and idle > minutes * 60:
                log.info("idle for %.0f minutes, exiting", idle / 60)
                self.server.shutdown()
                return


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return

        def send(event: dict) -> bool:
            try:
                self.wfile.write((json.dumps(event) + "\n").encode())
                self.wfile.flush()
                return True
            except OSError:
                return False

        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except ValueError as exc:
            send({"event": "error", "message": f"Bad request: {exc}"})
            return
        self.server.daemon_state.handle(req, send)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def is_running(path: str | None = None) -> bool:
    path = path or config.socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(path)
        return True
    except OSError:
        return False


def serve() -> None:
    path = config.socket_path()
    if is_running(path):
        raise SystemExit(f"vocaret daemon is already running ({path})")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    daemon = Daemon()
    old_umask = os.umask(0o077)  # the socket is for this user only
    try:
        server = _Server(path, _Handler)
    finally:
        os.umask(old_umask)
    server.daemon_state = daemon
    daemon.server = server
    cfg = config.resolve()
    threading.Thread(target=daemon.idle_watch, args=(float(cfg["idle_minutes"]),),
                     daemon=True).start()
    log.info("vocaret %s listening on %s", __version__, path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            os.unlink(path)
        except OSError:
            pass


def request(req: dict, path: str | None = None, timeout: float | None = None):
    """Client side: send one request, yield events until the server closes."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path or config.socket_path())
        s.sendall((json.dumps(req) + "\n").encode())
        with s.makefile("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)
