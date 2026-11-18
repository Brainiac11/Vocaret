"""Command line for Vocaret.

  vocaret daemon            run the background service (editors start it themselves)
  vocaret status | stop     inspect / stop the service
  vocaret doctor            check microphone, speech model and the model provider
  vocaret providers         list the model providers
  vocaret prepare           download/load the speech model ahead of first use
  vocaret listen            record until Enter and print what was heard
  vocaret edit FILE "..."   apply a typed instruction to a file (no editor needed)
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import logging.handlers
import os
import sys
import time

from . import __version__, config


def _setup_logging(to_file: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if to_file:
        path = config.log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=1))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)


def _fix_sandboxed_env() -> None:
    """Undo environment changes made by sandboxed editor launchers.

    The daemon is usually started by an editor. The VS Code snap, for example,
    points XDG_RUNTIME_DIR at a private directory, which hides the PipeWire
    socket from the recorder.
    """
    runtime = config.runtime_dir()
    if runtime and os.environ.get("XDG_RUNTIME_DIR") != runtime:
        os.environ["XDG_RUNTIME_DIR"] = runtime
    for key in ("PIPEWIRE_RUNTIME_DIR", "PULSE_RUNTIME_PATH"):
        if key in os.environ and "snap" in os.environ[key]:
            del os.environ[key]
    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")


def cmd_daemon(args) -> int:
    from . import server

    _fix_sandboxed_env()
    _setup_logging(to_file=True)
    server.serve()
    return 0


def cmd_status(args) -> int:
    from . import server

    if not server.is_running():
        print("vocaret daemon is not running")
        return 1
    for event in server.request({"op": "status"}, timeout=5):
        print(json.dumps(event, indent=2))
    return 0


def cmd_stop(args) -> int:
    from . import server

    if not server.is_running():
        print("vocaret daemon is not running")
        return 0
    list(server.request({"op": "shutdown"}, timeout=5))
    print("stopped")
    return 0


def cmd_doctor(args) -> int:
    from . import edits as E
    from . import providers
    from .audio import Recorder, recorder_command
    from .llm import SYSTEM_PROMPT, LLMError
    from .stt import Transcriber

    cfg = config.resolve({"provider": args.provider, "model": args.model})
    ok = True

    def report(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(("  ok   " if good else "  FAIL ") + label + (f": {detail}" if detail else ""))

    print(f"vocaret {__version__}")
    try:
        cmd = recorder_command(cfg["recorder_command"])
        rec = Recorder(command=cfg["recorder_command"])
        rec.start()
        rec.first_audio.wait(timeout=4.0)
        time.sleep(1.0)
        audio = rec.stop()
        peak = float(abs(audio).max()) if audio.size else 0.0
        report(audio.size > 8000, f"microphone via {cmd[0]}",
               f"{audio.size / 16000:.1f}s captured, peak level {peak:.3f}"
               + (" (silent - is the right input selected and unmuted?)" if peak < 0.002 else ""))
    except Exception as exc:
        report(False, "microphone", str(exc))
    try:
        transcriber = Transcriber()
        started = time.monotonic()
        transcriber.ensure_loaded(cfg)
        report(True, "speech model",
               f"{transcriber.model_name} on {transcriber.device}, "
               f"loaded in {time.monotonic() - started:.1f}s")
    except Exception as exc:
        report(False, "speech model", str(exc))
    try:
        label = providers.preset(cfg)["label"]
        started = time.monotonic()
        # Uses the real system prompt, so this also proves the format works.
        reply = providers.ModelRunner().run(
            SYSTEM_PROMPT,
            "File: probe.py\nLanguage: python\nIndentation: 4 spaces\n"
            "The caret is shown as [[CURSOR]].\nThe file is currently empty.\n\n"
            "<file>\n[[CURSOR]]\n</file>\n\n<spoken>\nset x to one\n</spoken>", cfg)
        good = bool(E.parse(reply).ops)
        report(good, f"model: {label}, {providers.model_for(cfg) or 'default model'}",
               f"answered in {time.monotonic() - started:.1f}s"
               + ("" if good else f" but not in the expected format: {reply[:80]!r}"))
    except LLMError as exc:
        report(False, "model", str(exc))
    print("all good" if ok else "something needs attention (see FAIL above)")
    return 0 if ok else 1


def cmd_prepare(args) -> int:
    """Load the speech model once, downloading it if this is the first run."""
    from .stt import Transcriber

    transcriber = Transcriber()
    started = time.monotonic()
    print("Preparing the speech model (downloads it on first run)...", flush=True)
    transcriber.ensure_loaded(config.resolve())
    print(f"Speech model ready: {transcriber.model_name} on {transcriber.device} "
          f"({time.monotonic() - started:.1f}s)")
    return 0


def cmd_providers(args) -> int:
    from . import providers

    current = config.resolve()["provider"]
    for item in providers.describe():
        key = ""
        if item["needs_key"]:
            key = f"  [{item['key_env']}: {'set' if item['key_in_env'] else 'not set'}]"
        mark = "*" if item["id"] == current else " "
        model = item["default_model"] or "(choose a model)"
        print(f"{mark} {item['id']:<12} {item['label']:<36} {model}{key}")
    return 0


def cmd_listen(args) -> int:
    from .audio import Recorder, load_audio_file
    from .stt import Transcriber

    cfg = config.resolve()
    transcriber = Transcriber()
    if args.file:
        audio = load_audio_file(args.file)
    else:
        rec = Recorder(command=cfg["recorder_command"])
        rec.start()
        transcriber.ensure_loaded(cfg)
        input("Recording... press Enter to stop. ")
        audio = rec.stop()
    started = time.monotonic()
    text = transcriber.transcribe(audio, cfg)
    print(f"[{len(audio) / 16000:.1f}s audio, {time.monotonic() - started:.2f}s to transcribe]")
    print(text)
    return 0


def cmd_edit(args) -> int:
    from . import edits as E
    from .engine import Engine

    cfg = config.resolve({"provider": args.provider, "model": args.model,
                          "effort": args.effort})
    path = args.file
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    norm = E.normalize(text)
    lines = norm.split("\n")
    line = len(lines) - 1 if args.line is None else max(0, args.line - 1)
    col = len(lines[min(line, len(lines) - 1)]) if args.col is None else max(0, args.col - 1)
    pos = {"line": line, "character": col}
    ext = os.path.splitext(path)[1].lstrip(".")
    result = Engine().instruct(args.instruction, {
        "path": path, "language": args.language or ext, "text": text,
        "selection": {"start": pos, "end": pos},
    }, cfg)
    if result.get("note"):
        print("note:", result["note"], file=sys.stderr)
    pmap = E.PositionMap(norm)
    flat = [(pmap.to_index(e["start"]), pmap.to_index(e["end"]), e["text"])
            for e in result["edits"]]
    new = E.apply_edits(norm, flat)
    if args.write:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(new)
        print(f"{path}: {len(flat)} edit(s) applied")
    else:
        sys.stdout.writelines(difflib.unified_diff(
            norm.splitlines(keepends=True), new.splitlines(keepends=True),
            path, path + " (after)"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vocaret", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon").set_defaults(fn=cmd_daemon)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--provider")
    doctor.add_argument("--model")
    doctor.set_defaults(fn=cmd_doctor)
    sub.add_parser("providers").set_defaults(fn=cmd_providers)
    sub.add_parser("prepare").set_defaults(fn=cmd_prepare)
    listen = sub.add_parser("listen")
    listen.add_argument("--file", help="transcribe an audio file instead of the microphone")
    listen.set_defaults(fn=cmd_listen)
    edit = sub.add_parser("edit")
    edit.add_argument("file")
    edit.add_argument("instruction")
    edit.add_argument("--line", type=int, help="caret line (1-based; default: end of file)")
    edit.add_argument("--col", type=int, help="caret column (1-based; default: end of line)")
    edit.add_argument("--language")
    edit.add_argument("--provider")
    edit.add_argument("--model")
    edit.add_argument("--effort")
    edit.add_argument("--write", action="store_true", help="modify the file instead of printing a diff")
    edit.set_defaults(fn=cmd_edit)
    args = parser.parse_args(argv)
    if args.command != "daemon":
        _setup_logging(to_file=False)
        logging.getLogger().setLevel(logging.WARNING)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from .audio import RecorderError
        from .llm import LLMError

        if isinstance(exc, (RecorderError, LLMError)):
            print(f"vocaret: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
