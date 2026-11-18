"""Defaults and per-request options.

Every request may carry an ``options`` object; anything it leaves out falls
back to ``~/.config/vocaret/config.json`` and then to DEFAULTS. The editor
extension sends its settings this way, so the editor's settings UI is the one
place a user normally configures things.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    # Which model backend writes the code. See providers.PRESETS for the list:
    # "claude-code" and "codex" use a subscription through the vendor's CLI,
    # the others are HTTP APIs that need a key (or a local server).
    "provider": "claude-code",
    # Model name for that provider. Empty = the provider's default.
    "model": "",
    # Reasoning effort, where the provider has such a knob. Dictation wants speed.
    "effort": "low",
    # API key for HTTP providers. Normally supplied per request by the editor
    # (from its secret storage) or read from the provider's environment variable.
    "api_key": "",
    # Environment variable to read the key from; empty = the provider's usual one.
    "api_key_env": "",
    # Endpoint for the "custom" provider (any OpenAI-compatible API), or an
    # override for a preset, e.g. "http://localhost:8080/v1".
    "base_url": "",
    # Extra JSON fields merged into OpenAI-compatible request bodies.
    "extra_body": {},
    # Seconds before a model call is abandoned.
    "timeout": 90,
    # Commands for the subscription CLIs.
    "claude_command": "claude",
    "codex_command": "codex",
    # "login": use the Claude Code login (subscription) and ignore API-key
    # environment variables. "inherit": pass the environment through untouched.
    "claude_env": "login",
    # "auto" picks a GPU model when CUDA works, a small CPU model otherwise.
    "whisper_model": "auto",
    "whisper_model_gpu": "large-v3-turbo",
    "whisper_model_cpu": "small.en",
    # "auto" | "cuda" | "cpu"
    "device": "auto",
    # Spoken language code; empty string lets Whisper detect it.
    "language": "en",
    # Custom capture command that writes raw 16 kHz mono s16le PCM to stdout,
    # e.g. "arecord -q -D hw:1 -f S16_LE -r 16000 -c 1 -t raw -". Empty = auto.
    "recorder_command": "",
    # Stop recording after this many seconds of silence once speech was heard.
    # 0 disables it: recording ends only when the user toggles again.
    "silence_seconds": 0,
    # Hard cap on one recording.
    "max_seconds": 180,
    # Daemon exits after this many idle minutes (frees RAM/VRAM). 0 = never.
    "idle_minutes": 30,
    # Files larger than this many characters are sent as a window around the caret.
    "max_file_chars": 120_000,
    "window_lines": 400,
}

# Never echoed back to clients or written to logs.
SECRET_KEYS = ("api_key",)


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vocaret" / "config.json"


def load_file_config() -> dict:
    try:
        data = json.loads(config_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve(options: dict | None = None) -> dict:
    cfg = dict(DEFAULTS)
    for source in (load_file_config(), options or {}):
        for key, value in source.items():
            if key in DEFAULTS and value is not None and value != "":
                cfg[key] = value
    # "language" is the one option where empty string is meaningful.
    if options and options.get("language") == "":
        cfg["language"] = ""
    if not isinstance(cfg["extra_body"], dict):
        cfg["extra_body"] = {}
    return cfg


def runtime_dir() -> str | None:
    """The user's real runtime directory.

    Derived from the uid rather than $XDG_RUNTIME_DIR because sandboxed
    launchers (the VS Code snap, for one) point that variable at a private
    subdirectory, and every editor has to agree on one socket.
    """
    path = f"/run/user/{os.getuid()}"
    return path if os.path.isdir(path) else None


def socket_path() -> str:
    override = os.environ.get("VOCARET_SOCKET")
    if override:
        return override
    runtime = runtime_dir()
    if runtime:
        return os.path.join(runtime, "vocaret.sock")
    return f"/tmp/vocaret-{os.getuid()}.sock"


def log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "vocaret" / "daemon.log"
