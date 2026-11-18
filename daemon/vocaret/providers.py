"""Model backends.

Vocaret needs exactly one thing from a model: given a system prompt and one
request, return text. Four kinds of backend provide that:

  claude-code  Claude through the Claude Code CLI (`claude -p`), using the
               login the user already has: a Claude subscription, no API key.
  codex        OpenAI models through the Codex CLI (`codex exec`), using a
               ChatGPT login: a ChatGPT subscription, no API key.
  anthropic    The Claude API with an API key, via the official SDK.
  openai       Any server that speaks the OpenAI chat-completions protocol.
               OpenAI, OpenRouter, DeepSeek, Gemini, Groq, Mistral, xAI,
               Together, Ollama, LM Studio and "custom" are presets of this.

PRESETS is the single list of providers; the editor extension asks the daemon
for it rather than keeping its own copy.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request

from . import __version__
from .llm import LLMError

log = logging.getLogger("vocaret.providers")

REPO_URL = "https://github.com/Brainiac11/Vocaret"

# kind: which implementation below. key_env: conventional API-key variable.
# default_model "" means the user must choose one; model_hint is an example
# shown when asking. Model names age quickly: defaults here are starting
# points checked against the providers' docs in November 2026.
PRESETS: dict[str, dict] = {
    "claude-code": {
        "kind": "claude-code", "label": "Claude Code (Claude subscription)",
        "default_model": "sonnet",
        "detail": "Uses your Claude Code login. No API key.",
    },
    "codex": {
        "kind": "codex", "label": "Codex CLI (ChatGPT subscription)",
        "default_model": "",
        "detail": "Uses your Codex CLI login (ChatGPT plan). No API key.",
        "model_hint": "gpt-6-luna",
    },
    "anthropic": {
        "kind": "anthropic", "label": "Anthropic API",
        "key_env": "ANTHROPIC_API_KEY", "default_model": "claude-sonnet-5-5",
        "detail": "Claude through the API, billed per token.",
        "model_hint": "claude-sonnet-5-5",
    },
    "openai": {
        "kind": "openai", "label": "OpenAI API",
        "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY",
        "default_model": "gpt-6.1-terra", "effort_param": "reasoning_effort",
        "detail": "GPT models through the API.",
        "model_hint": "gpt-6.1-terra",
    },
    "openrouter": {
        "kind": "openai", "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
        "default_model": "openai/gpt-6.1-terra",
        "headers": {"HTTP-Referer": REPO_URL, "X-OpenRouter-Title": "Vocaret",
                    "X-Title": "Vocaret"},
        "detail": "One key for models from many vendors.",
        "model_hint": "anthropic/claude-sonnet-5.5",
    },
    "deepseek": {
        "kind": "openai", "label": "DeepSeek",
        "base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY",
        "default_model": "",
        "detail": "DeepSeek's API.",
        "model_hint": "deepseek-flash",
    },
    "gemini": {
        "kind": "openai", "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY", "default_model": "gemini-3.8-flash",
        "detail": "Gemini through Google's OpenAI-compatible endpoint.",
        "model_hint": "gemini-3.8-flash",
    },
    "groq": {
        "kind": "openai", "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
        "default_model": "openai/gpt-oss-120b",
        "detail": "Very fast hosted open models.",
        "model_hint": "openai/gpt-oss-120b",
    },
    "mistral": {
        "kind": "openai", "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY",
        "default_model": "mistral-small-latest",
        "detail": "Mistral's API (Codestral and others).",
        "model_hint": "mistral-small-latest",
    },
    "xai": {
        "kind": "openai", "label": "xAI",
        "base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY",
        "default_model": "grok-4.7",
        "detail": "Grok models.",
        "model_hint": "grok-4.7",
    },
    "together": {
        "kind": "openai", "label": "Together AI",
        "base_url": "https://api.together.ai/v1", "key_env": "TOGETHER_API_KEY",
        "default_model": "zai-org/GLM-5.3",
        "detail": "Hosted open models.",
        "model_hint": "zai-org/GLM-5.3",
    },
    "ollama": {
        "kind": "openai", "label": "Ollama (local)",
        "base_url": "http://localhost:11434/v1", "default_model": "",
        "detail": "Models running on this machine. No key, nothing leaves it.",
        "model_hint": "qwen3-coder",
    },
    "lmstudio": {
        "kind": "openai", "label": "LM Studio (local)",
        "base_url": "http://localhost:1234/v1", "default_model": "",
        "detail": "Models running on this machine. No key, nothing leaves it.",
        "model_hint": "the model id shown in LM Studio",
    },
    "custom": {
        "kind": "openai", "label": "Custom OpenAI-compatible endpoint",
        "base_url": "", "default_model": "",
        "detail": "Any server with a /chat/completions endpoint (set the base URL).",
        "model_hint": "the model id your server expects",
    },
}

# Claude models for which the API offers a server-side fallback on a refusal.
_ANTHROPIC_FALLBACK_MODELS = {
    "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5",
    "claude-sonnet-5-5",
}


def preset(cfg: dict) -> dict:
    name = cfg.get("provider") or "claude-code"
    if name not in PRESETS:
        raise LLMError(f'Unknown provider "{name}". Choose one of: ' + ", ".join(PRESETS))
    return PRESETS[name]


def model_for(cfg: dict) -> str:
    model = cfg.get("model") or preset(cfg)["default_model"]
    if not model and preset(cfg)["kind"] != "codex":
        raise LLMError(
            f'No model chosen for {preset(cfg)["label"]}. Set "vocaret.model" '
            "(run \"Vocaret: Choose Provider and Model\").")
    return model


def api_key_for(cfg: dict) -> str:
    """Key from the request (editor secret storage), else the environment."""
    if cfg.get("api_key"):
        return str(cfg["api_key"])
    # VOCARET_API_KEY lets a caller hand over a key without naming the provider.
    if os.environ.get("VOCARET_API_KEY"):
        return os.environ["VOCARET_API_KEY"]
    env = cfg.get("api_key_env") or preset(cfg).get("key_env") or ""
    return os.environ.get(env, "") if env else ""


def effort_for(cfg: dict) -> str:
    """Reasoning effort; "low" is the lowest level every provider accepts."""
    effort = str(cfg.get("effort") or "low")
    return "low" if effort in ("minimal", "none") else effort


def describe() -> list[dict]:
    """Provider list for clients (the extension's picker, `vocaret providers`)."""
    out = []
    for name, p in PRESETS.items():
        key_env = p.get("key_env", "")
        out.append({
            "id": name, "label": p["label"], "detail": p["detail"], "kind": p["kind"],
            "default_model": p["default_model"], "model_hint": p.get("model_hint", ""),
            "key_env": key_env,
            "needs_key": bool(key_env),
            "key_in_env": bool(key_env and os.environ.get(key_env)),
            "needs_base_url": name == "custom",
        })
    return out


def _looks_like_auth_problem(message: str) -> bool:
    return bool(re.search(r"log(ged)?[ -]?in|sign(ed)?[ -]?in|auth|credential|401",
                          message, re.I))


def strip_reasoning(text: str) -> str:
    """Drop inline reasoning some open models emit before their answer."""
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S).strip()


# ----------------------------------------------------------------- CLI kinds

def _find_cli(command: str) -> str:
    """Find a vendor CLI even when the editor's PATH is minimal."""
    found = shutil.which(command)
    if found or os.sep in command:
        return found or command
    home = os.path.expanduser("~")
    for candidate in (f"{home}/.local/bin/{command}", f"{home}/.claude/local/{command}",
                      f"/usr/local/bin/{command}", f"/opt/homebrew/bin/{command}",
                      f"{home}/.npm-global/bin/{command}", f"{home}/.bun/bin/{command}"):
        if os.access(candidate, os.X_OK):
            return candidate
    return command


def resolve_claude(command: str) -> str:
    return _find_cli(command)


def _neutral_cwd() -> str:
    # An empty directory, so no project instructions or settings are picked up.
    path = os.path.join(tempfile.gettempdir(), f"vocaret-cwd-{os.getuid()}")
    os.makedirs(path, exist_ok=True)
    return path


class _Subprocess:
    """Shared plumbing for the CLI backends: run, time out, cancel."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    def _communicate(self, cmd: list[str], stdin: str, cfg: dict, env: dict,
                     missing: str) -> tuple[int, str, str]:
        with self._lock:
            self._cancelled = False
            try:
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, env=env, cwd=_neutral_cwd())
            except FileNotFoundError as exc:
                raise LLMError(missing) from exc
            proc = self._proc
        try:
            out, err = proc.communicate(stdin, timeout=float(cfg["timeout"]))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise LLMError(f"The model did not answer within {cfg['timeout']} seconds.")
        if self._cancelled:
            raise LLMError("Cancelled.")
        return proc.returncode, out, err


class ClaudeCode(_Subprocess):
    def run(self, system: str, request: str, cfg: dict) -> str:
        cmd = [
            _find_cli(cfg["claude_command"]), "-p",
            "--model", model_for(cfg),
            "--effort", effort_for(cfg),
            "--system-prompt", system,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format", "json",
        ]
        env = dict(os.environ)
        if cfg.get("claude_env", "login") == "login":
            # Use the Claude Code login on this machine: drop API-key overrides
            # and anything inherited from an enclosing Claude Code session.
            for key in list(env):
                if key.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE")):
                    del env[key]
        _code, out, err = self._communicate(
            cmd, request, cfg, env,
            f"Could not run '{cfg['claude_command']}'. Install Claude Code and log in "
            "once by running `claude` in a terminal, or pick another provider.")
        try:
            data = json.loads(out)
        except ValueError:
            detail = (err or out or "").strip().splitlines()
            raise LLMError("Claude Code failed: " + (detail[-1] if detail else "no output"))
        # `--output-format json` prints one result object (a list of events in
        # verbose setups); take the final result either way.
        if isinstance(data, list):
            data = next((d for d in reversed(data) if d.get("type") == "result"), {})
        result = data.get("result")
        if data.get("is_error") or not isinstance(result, str):
            message = result if isinstance(result, str) and result else str(
                data.get("subtype") or "unknown error")
            if _looks_like_auth_problem(message):
                message += " (run `claude` in a terminal and log in)"
            raise LLMError("Claude Code returned an error: " + message)
        return result


class Codex(_Subprocess):
    """OpenAI's Codex CLI in non-interactive mode (ChatGPT login)."""

    def run(self, system: str, request: str, cfg: dict) -> str:
        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as handle:
            last_message = handle.name
        binary = _find_cli(cfg["codex_command"])
        cmd = [
            binary, "exec",
            "--skip-git-repo-check",      # we run outside any repository
            "--sandbox", "read-only",
            "--ephemeral",                # keep no session files
            "--cd", _neutral_cwd(),       # an empty directory: no project context
            "-c", f"model_reasoning_effort={json.dumps(effort_for(cfg))}",
            "--output-last-message", last_message,
        ]
        model = cfg.get("model") or ""
        if model:
            cmd += ["--model", model]
        cmd.append("-")  # read the prompt from stdin
        # Codex has no flag to replace its own instructions, so ours travel in
        # the prompt, with a reminder that this is a text-only task.
        prompt = (
            system
            + "\n\nThis is a single text-in, text-out request. Do not run commands, "
            "read files or use any tools; reply with the operations only.\n\n"
            + request
        )
        try:
            code, out, err = self._communicate(
                cmd, prompt, cfg, dict(os.environ),
                f"Could not run '{cfg['codex_command']}'. Install the Codex CLI "
                "(https://developers.openai.com/codex/cli) and sign in with `codex login`, "
                "or pick another provider.")
            try:
                with open(last_message, encoding="utf-8") as handle:
                    reply = handle.read().strip()
            except OSError:
                reply = ""
        finally:
            try:
                os.unlink(last_message)
            except OSError:
                pass
        if code != 0 or not reply:
            detail = [ln for ln in (err or out or "").strip().splitlines() if ln.strip()]
            message = detail[-1] if detail else f"exit code {code}"
            if _looks_like_auth_problem(message) or not self._logged_in(binary):
                message += " (sign in by running `codex login` in a terminal)"
            raise LLMError("Codex CLI failed: " + message)
        return reply


    @staticmethod
    def _logged_in(binary: str) -> bool:
        """`codex login status` exits non-zero when there is no login."""
        try:
            return subprocess.run([binary, "login", "status"], capture_output=True,
                                  timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return True  # can't tell; don't blame the login


# ---------------------------------------------------------------- HTTP kinds

class _Threaded:
    """Run a blocking call in a worker so it can be abandoned on cancel."""

    def __init__(self) -> None:
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _await(self, fn, timeout: float):
        self._cancel.clear()
        box: dict = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:  # handed to the caller below
                box["error"] = exc
            done.set()

        threading.Thread(target=work, daemon=True).start()
        waited = 0.0
        while not done.wait(0.1):
            waited += 0.1
            if self._cancel.is_set():
                raise LLMError("Cancelled.")
            if waited > timeout + 5:
                raise LLMError(f"The model did not answer within {int(timeout)} seconds.")
        if "error" in box:
            raise box["error"]
        return box["value"]


class OpenAICompatible(_Threaded):
    """Any /chat/completions server. Uses only the standard library."""

    def run(self, system: str, request: str, cfg: dict) -> str:
        p = preset(cfg)
        base_url = (cfg.get("base_url") or p.get("base_url") or "").rstrip("/")
        if not base_url:
            raise LLMError('Set "vocaret.baseUrl" to your endpoint, e.g. http://localhost:8080/v1')
        key = api_key_for(cfg)
        if p.get("key_env") and not key:
            raise LLMError(
                f'No API key for {p["label"]}. Run "Vocaret: Set API Key", or set '
                f'{cfg.get("api_key_env") or p["key_env"]} in your environment.')
        body = {
            "model": model_for(cfg),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": request}],
            "stream": False,
        }
        optional = {}
        if p.get("effort_param") and cfg.get("effort"):
            optional[p["effort_param"]] = effort_for(cfg)
        optional.update(cfg.get("extra_body") or {})
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": f"vocaret/{__version__}"}
        headers.update(p.get("headers") or {})
        if key:
            headers["Authorization"] = "Bearer " + key
        url = base_url + "/chat/completions"
        timeout = float(cfg["timeout"])
        try:
            data = self._await(lambda: self._post(url, {**body, **optional}, headers, timeout),
                               timeout)
        except _HTTPFailure as exc:
            # A 400 is usually an optional field this model rejects (reasoning
            # effort on a non-reasoning model, say). Retry once without them.
            if exc.status != 400 or not optional:
                raise LLMError(self._explain(exc, p)) from exc
            log.info("retrying without optional fields after: %s", exc.message)
            try:
                data = self._await(lambda: self._post(url, body, headers, timeout), timeout)
            except _HTTPFailure as again:
                raise LLMError(self._explain(again, p)) from again
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise LLMError(f'{p["label"]} sent an unexpected response'
                           + (f": {message}" if message else "."))
        if isinstance(content, list):  # some servers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise LLMError(f'{p["label"]} returned an empty answer.')
        return strip_reasoning(content)

    @staticmethod
    def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            message = raw.strip()[:300]
            try:
                parsed = json.loads(raw)
                error = parsed.get("error", parsed) if isinstance(parsed, dict) else parsed
                if isinstance(error, dict) and error.get("message"):
                    message = str(error["message"])
                elif isinstance(error, str):
                    message = error
            except ValueError:
                pass
            raise _HTTPFailure(exc.code, message) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise _HTTPFailure(0, f"could not reach {url}: {reason}") from exc
        except ValueError as exc:
            raise _HTTPFailure(0, "the server did not return JSON") from exc

    @staticmethod
    def _explain(exc: "_HTTPFailure", p: dict) -> str:
        hint = ""
        if exc.status in (401, 403):
            hint = ' Check the API key ("Vocaret: Set API Key").'
        elif exc.status == 404:
            hint = " Check the model name and base URL."
        elif exc.status == 429:
            hint = " Rate limit or quota reached."
        prefix = f'{p["label"]} error' + (f" {exc.status}" if exc.status else "")
        return f"{prefix}: {exc.message}{hint}"


class _HTTPFailure(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AnthropicAPI(_Threaded):
    """The Claude API with an API key, through the official SDK."""

    def run(self, system: str, request: str, cfg: dict) -> str:
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError(
                "The Anthropic provider needs the 'anthropic' package. Re-run the Vocaret "
                "setup, or pip install anthropic into its environment.") from exc

        key = api_key_for(cfg)
        timeout = float(cfg["timeout"])
        # Without an explicit key the SDK resolves ANTHROPIC_API_KEY or an
        # `ant auth login` profile on its own.
        client = anthropic.Anthropic(api_key=key, timeout=timeout) if key \
            else anthropic.Anthropic(timeout=timeout)
        model = model_for(cfg)
        base = {
            "model": model,
            "max_tokens": 16000,
            "system": system,
            "messages": [{"role": "user", "content": request}],
        }

        def call(full: bool):
            if not full:
                return client.messages.create(**base)
            params = dict(base, output_config={"effort": effort_for(cfg)})
            if model in _ANTHROPIC_FALLBACK_MODELS:
                # If a safety classifier declines the request, let the API
                # re-run it on its default fallback model inside the same call.
                return client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            return client.messages.create(**params)

        try:
            try:
                response = self._await(lambda: call(True), timeout)
            except anthropic.BadRequestError as exc:
                # Older models reject the effort setting; ask again plainly.
                log.info("retrying with a minimal request after: %s", exc.message)
                response = self._await(lambda: call(False), timeout)
        except anthropic.AuthenticationError as exc:
            raise LLMError('Anthropic rejected the API key. Run "Vocaret: Set API Key".') from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"Anthropic denied the request: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f'Anthropic does not know the model "{model}".') from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic rate limit reached; try again in a moment.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Could not reach the Anthropic API. Check your connection.") from exc
        except anthropic.AnthropicError as exc:
            # Raised e.g. when no credentials can be found at all.
            raise LLMError(f'Anthropic: {exc} (run "Vocaret: Set API Key")') from exc

        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError("The model's answer was cut off (too long).")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise LLMError("Anthropic returned an empty answer.")
        return text


# ------------------------------------------------------------------- facade

_KINDS = {"claude-code": ClaudeCode, "codex": Codex, "anthropic": AnthropicAPI,
          "openai": OpenAICompatible}


class ModelRunner:
    """What the engine talks to: picks the backend per request, supports cancel."""

    def __init__(self) -> None:
        self._backends: dict[str, object] = {}
        self._current = None
        self._lock = threading.Lock()

    def run(self, system: str, request: str, cfg: dict) -> str:
        kind = preset(cfg)["kind"]
        with self._lock:
            backend = self._backends.setdefault(kind, _KINDS[kind]())
            self._current = backend
        return backend.run(system, request, cfg)

    def cancel(self) -> None:
        with self._lock:
            backend = self._current
        if backend is not None:
            backend.cancel()
