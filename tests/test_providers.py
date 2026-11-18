"""Provider tests against local stand-ins (no real accounts, no network).

A tiny HTTP server plays an OpenAI-compatible API and the Anthropic Messages
API; a shell script plays the Codex CLI. These prove Vocaret's side of each
protocol: request shape, key handling, error mapping, retries, parsing.

Run:  python -m pytest tests/test_providers.py     or     python tests/test_providers.py
"""
import json
import os
import stat
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from vocaret import config, providers
from vocaret import edits as E
from vocaret.engine import Engine
from vocaret.llm import LLMError

ROOT = Path(__file__).resolve().parent.parent
REPLY = "=====INSERT\nx = 1\n=====END"
TEST_KEY = "test-key-not-real"
seen: list[dict] = []


class FakeAPI(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if self.path.startswith("/v1/messages"):  # Anthropic Messages API
            # SDK releases differ in which header carries the key.
            if TEST_KEY not in (self.headers.get("x-api-key"),
                                (self.headers.get("Authorization") or "").removeprefix("Bearer ")):
                return self._json(401, {"type": "error", "error": {
                    "type": "authentication_error", "message": "invalid x-api-key"}})
            if body["model"] == "old-model" and "output_config" in body:
                return self._json(400, {"type": "error", "error": {
                    "type": "invalid_request_error", "message": "effort not supported"}})
            return self._json(200, {
                "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                "content": [{"type": "text", "text": REPLY}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
        if not self.path.endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "no such route"}})
        model = body["model"]
        if model != "open-model" and self.headers.get("Authorization") != f"Bearer {TEST_KEY}":
            return self._json(401, {"error": {"message": "Incorrect API key provided"}})
        if model == "plain-model" and "reasoning_effort" in body:
            return self._json(400, {"error": {"message": "Unrecognized request argument: reasoning_effort"}})
        content = REPLY
        if model == "thinking-model":
            content = "<think>\nthe user wants x\n=====INSERT\nwrong\n=====END\n</think>\n" + REPLY
        if model == "parts-model":
            content = [{"type": "text", "text": REPLY}]
        if model == "fenced-model":
            content = "Sure! Here you go:\n```\n===== INSERT =====\n```python\nx = 1\n```\n===== END =====\n```"
        self._json(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})


server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_port}"


def run(**options) -> str:
    cfg = config.resolve({"provider": "custom", "base_url": BASE + "/v1", "timeout": 10, **options})
    return providers.ModelRunner().run("SYSTEM", "REQUEST", cfg)


def fails(**options) -> str:
    try:
        run(**options)
    except LLMError as exc:
        return str(exc)
    raise AssertionError("expected an LLMError")


def test_openai_compatible_request_shape_and_key():
    seen.clear()
    assert run(model="m", api_key=TEST_KEY) == REPLY
    sent = seen[-1]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {TEST_KEY}"
    assert sent["body"]["messages"] == [{"role": "system", "content": "SYSTEM"},
                                        {"role": "user", "content": "REQUEST"}]
    assert sent["body"]["stream"] is False and "temperature" not in sent["body"]


def test_key_from_environment_variable():
    os.environ["VOCARET_TEST_KEY"] = TEST_KEY
    try:
        assert run(model="m", api_key_env="VOCARET_TEST_KEY") == REPLY
    finally:
        del os.environ["VOCARET_TEST_KEY"]


def test_keyless_local_server_and_extra_body():
    seen.clear()
    assert run(model="open-model", extra_body={"top_p": 0.5}) == REPLY
    assert "Authorization" not in seen[-1]["headers"] and seen[-1]["body"]["top_p"] == 0.5


def test_optional_field_rejected_then_retried_without_it():
    seen.clear()
    assert run(model="plain-model", api_key=TEST_KEY,
               extra_body={"reasoning_effort": "low"}) == REPLY
    assert len(seen) == 2 and "reasoning_effort" not in seen[1]["body"]


def test_errors_are_explained():
    assert "Check the API key" in fails(model="m", api_key="wrong")
    assert "could not reach" in fails(model="m", base_url="http://127.0.0.1:9/v1")
    assert "No model chosen" in fails()
    assert "vocaret.baseUrl" in fails(model="m", base_url="", provider="custom")
    missing = fails(provider="openai", model="m", api_key_env="VOCARET_NO_SUCH_VAR")
    assert "No API key for OpenAI" in missing and "VOCARET_NO_SUCH_VAR" in missing
    assert "Unknown provider" in fails(provider="nope")


def test_reasoning_tags_content_parts_and_fences_are_handled():
    assert run(model="thinking-model", api_key=TEST_KEY) == REPLY
    assert run(model="parts-model", api_key=TEST_KEY) == REPLY
    fenced = run(model="fenced-model", api_key=TEST_KEY)
    assert E.parse(fenced).ops == [("insert", "x = 1")]


def test_engine_end_to_end_through_an_http_provider():
    cfg = config.resolve({"provider": "custom", "base_url": BASE + "/v1", "model": "m",
                          "api_key": TEST_KEY})
    pos = {"line": 0, "character": 0}
    result = Engine().instruct("set x to one", {
        "path": "a.py", "language": "python", "text": "", "selection": {"start": pos, "end": pos},
    }, cfg)
    assert result["edits"] == [{"start": pos, "end": pos, "text": "x = 1"}]
    assert TEST_KEY not in json.dumps(result)


def test_anthropic_sdk_provider():
    os.environ["ANTHROPIC_BASE_URL"] = BASE
    try:
        seen.clear()
        cfg = {"provider": "anthropic", "api_key": TEST_KEY, "timeout": 10}
        assert run(**cfg) == REPLY
        sent = seen[-1]
        assert sent["path"].startswith("/v1/messages")
        assert sent["body"]["model"] == "claude-sonnet-5-5"
        assert sent["body"]["system"] == "SYSTEM"
        assert sent["body"]["output_config"] == {"effort": "low"}
        assert sent["body"]["fallbacks"] == "default"
        assert "server-side-fallback" in sent["headers"].get("anthropic-beta", "")
        # A model that rejects the effort setting gets a plain second request.
        seen.clear()
        assert run(**cfg, model="old-model") == REPLY
        assert len(seen) == 2 and "output_config" not in seen[1]["body"]
        assert "fallbacks" not in seen[0]["body"]
        assert "rejected the API key" in fails(**dict(cfg, api_key="wrong"))
    finally:
        del os.environ["ANTHROPIC_BASE_URL"]


def test_codex_cli_plumbing():
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "codex"
        (Path(tmp) / "reply").write_text(REPLY)
        fake.write_text(f"""#!/usr/bin/env bash
# Stand-in for the Codex CLI: records its arguments and stdin.
printf '%s\\n' "$@" > "{tmp}/args"
cat > "{tmp}/stdin"
while [ $# -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then out="$2"; fi
  shift
done
if grep -q FAIL_LOGIN "{tmp}/stdin"; then echo "Not logged in" >&2; exit 1; fi
cp "{tmp}/reply" "$out"
""")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        cfg = config.resolve({"provider": "codex", "codex_command": str(fake), "model": "some-model"})
        assert providers.ModelRunner().run("SYSTEM", "REQUEST", cfg) == REPLY
        args = (Path(tmp) / "args").read_text().split("\n")
        assert args[0] == "exec" and "--model" in args and "some-model" in args and "-" in args
        sent = (Path(tmp) / "stdin").read_text()
        assert sent.startswith("SYSTEM") and sent.rstrip().endswith("REQUEST")
        try:
            providers.ModelRunner().run("SYSTEM", "FAIL_LOGIN", cfg)
        except LLMError as exc:
            assert "codex login" in str(exc)
        else:
            raise AssertionError("expected an LLMError")
    missing = config.resolve({"provider": "codex", "codex_command": "/nonexistent/codex"})
    try:
        providers.ModelRunner().run("S", "R", missing)
    except LLMError as exc:
        assert "Install the Codex CLI" in str(exc)
    else:
        raise AssertionError("expected an LLMError")


def test_presets_are_complete_and_match_the_extension():
    for name, p in providers.PRESETS.items():
        assert p["kind"] in ("claude-code", "codex", "anthropic", "openai"), name
        assert p["label"] and p["detail"] and "default_model" in p, name
        if p["kind"] == "openai" and name != "custom":
            assert p["base_url"].startswith("http"), name
    package = json.loads((ROOT / "extension" / "package.json").read_text())
    enum = package["contributes"]["configuration"]["properties"]["vocaret.provider"]["enum"]
    assert enum == list(providers.PRESETS), "extension provider list is out of sync"


def test_versions_agree():
    import re
    import vocaret

    package = json.loads((ROOT / "extension" / "package.json").read_text())
    pyproject = (ROOT / "daemon" / "pyproject.toml").read_text()
    assert package["version"] == vocaret.__version__
    assert re.search(r'^version = "(.+)"$', pyproject, re.M).group(1) == vocaret.__version__


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(f"{len(tests)} passed")
