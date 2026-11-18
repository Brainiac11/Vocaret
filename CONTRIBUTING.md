# Contributing

Thanks for wanting to improve Vocaret. Issues and pull requests are welcome.

## Layout

```
daemon/vocaret/     Python background service and CLI
  audio.py          microphone capture, auto-stop on silence
  stt.py            local speech recognition (faster-whisper)
  llm.py            the system prompt and request builder (model-agnostic)
  providers.py      model backends: CLI subscriptions, HTTP APIs, local servers
  edits.py          parses the model's operations, resolves and diffs them
  engine.py         instruction -> edits pipeline
  server.py         Unix-socket protocol the editors talk to
extension/          VS Code extension (plain JavaScript, no build step)
scripts/            VSIX builder, icon generator
tests/              unit, provider, model-behaviour and in-editor tests
install.sh          installer; also what the extension runs to set itself up
```

## Developing

```bash
./install.sh                       # installs your checkout
PY=~/.local/share/vocaret/venv/bin/python

PYTHONPATH=daemon $PY tests/test_edits.py       # edit resolution
PYTHONPATH=daemon $PY tests/test_providers.py   # providers, against local stand-ins
```

Those two need no accounts and no network, and run in CI. The others talk to
real services and are run by hand:

```bash
PYTHONPATH=daemon $PY tests/model_cases.py                # 13 spoken instructions against your configured provider
PYTHONPATH=daemon $PY tests/model_cases.py openrouter some/model
PYTHONPATH=daemon $PY tests/daemon_flow.py speech.wav     # audio file -> daemon -> code
tests/run_vscode_test.sh speech.wav                       # the extension inside a real VS Code
```

To try extension changes, open `extension/` in VS Code and press F5, or re-run
`./install.sh` and reload the window.

## Adding a provider

If it speaks the OpenAI chat-completions protocol, it is one entry in
`PRESETS` in `daemon/vocaret/providers.py` plus the same id in the
`vocaret.provider` enum in `extension/package.json` (a test checks they
match). Anything else is a small class with `run(system, request, cfg)` and
`cancel()`.

`tests/model_cases.py` is the bar for a model being usable: the prompt in
`llm.py` should not be tuned for one model at the cost of others.

## Guidelines

- Vocaret is a dictation tool: one spoken instruction, one edit, exactly what
  was said. Features that make it act on its own (running commands, editing
  other files) are out of scope.
- Audio stays on the user's machine. API keys are never logged or written to
  settings.
- Keep the version in `daemon/pyproject.toml`, `daemon/vocaret/__init__.py`
  and `extension/package.json` identical; the extension uses it to detect an
  outdated daemon.
