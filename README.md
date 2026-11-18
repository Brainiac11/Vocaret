# Vocaret

Voice typing for code, for VS Code and its forks (Cursor, VSCodium, Windsurf).

You keep writing code yourself. When saying something is faster than typing
it, press **Ctrl+Alt+Space**, describe it, press **Ctrl+Alt+Space** again, and
it is typed at your caret.

> "Okay, create a public method called test with parameters of string x and
> string y, and then return x plus y."

```java
public String test(String x, String y) {
    return x + y;
}
```

> "Now put that inside a class Foo, with a constructor that accepts a string
> called s and prints out s."

```java
public class Foo {
    public Foo(String s) {
        System.out.println(s);
    }

    public String test(String x, String y) {
        return x + y;
    }
}
```

It writes what you described and nothing more, in the style of the file you
are in. It is a dictation tool, not an agent: one spoken instruction, one
edit, one undo step.

- **Speech recognition is local.** Whisper runs on your machine; audio never
  leaves it.
- **Bring your own model.** Use a subscription you already pay for (Claude
  through Claude Code, ChatGPT through the Codex CLI), an API key (Anthropic,
  OpenAI, OpenRouter, DeepSeek, Gemini, Groq, Mistral, xAI, Together), or a
  model running locally (Ollama, LM Studio, any OpenAI-compatible server).
- **Open source**, MIT licensed.

## Install

Requirements: Linux (macOS is best-effort and untested), Python 3.10+, a
microphone, and one model provider from the table below.

**From a checkout** (installs the speech engine and the extension):

```bash
git clone https://github.com/Brainiac11/Vocaret.git
```

```bash
cd Vocaret && ./install.sh
```

**From a `.vsix`** (Releases page, or a marketplace once published): install
the extension, press Ctrl+Alt+Space, and accept the prompt to set up the
speech engine. It installs a private Python environment in
`~/.local/share/vocaret` (about 0.4 GB, or about 2.5 GB with NVIDIA GPU
support) and downloads a Whisper model on first use.

`./install.sh --cpu` skips the NVIDIA libraries, `--no-editors` skips the
extension, `--uninstall` removes everything.

## Use

| Key | What it does |
| --- | --- |
| **Ctrl+Alt+Space** | Start listening; press again when you've finished speaking |
| **Esc** while listening | Cancel |
| Click the status bar item while it works | Cancel |
| **Ctrl+Z** or saying "undo that" | Revert the last voice edit |

The status bar (bottom right) shows `Listening` with a level meter, then what
it heard, then `Done`. Whatever changed is highlighted briefly.

Things you can say:

- New code at the caret: "add a private field called name of type string",
  "loop over the items and print each one", "define a function second that
  returns two".
- Changes to existing code: "rename x to count", "make that method static",
  "delete the constructor", "put that inside a class Foo".
- With a selection: "wrap this in a try catch that catches IO exception".
- You can talk the way you think. Fillers and corrections are fine: "make a
  function called foo, no wait, call it bar".

Names are cased for the language (`Foo`, `getUserName`, `get_user_name`), and
misheard words are matched against names already in the file.

## Choosing a model

Run **Vocaret: Choose Provider and Model** from the command palette. It lists
the providers, asks for an API key if one is needed, and asks for a model.

| Provider (`vocaret.provider`) | How you pay | What you need |
| --- | --- | --- |
| `claude-code` (default) | Claude subscription | [Claude Code](https://claude.com/claude-code) installed and logged in |
| `codex` | ChatGPT subscription | [Codex CLI](https://github.com/openai/codex) installed, `codex login` |
| `anthropic` | API key | `ANTHROPIC_API_KEY` |
| `openai` | API key | `OPENAI_API_KEY` |
| `openrouter` | API key | `OPENROUTER_API_KEY` |
| `deepseek` | API key | `DEEPSEEK_API_KEY` |
| `gemini` | API key | `GEMINI_API_KEY` |
| `groq` | API key | `GROQ_API_KEY` |
| `mistral` | API key | `MISTRAL_API_KEY` |
| `xai` | API key | `XAI_API_KEY` |
| `together` | API key | `TOGETHER_API_KEY` |
| `ollama` | free, local | Ollama running with a model pulled |
| `lmstudio` | free, local | LM Studio's local server running |
| `custom` | varies | any OpenAI-compatible endpoint; set `vocaret.baseUrl` |

- **API keys** are stored in the editor's secret storage by **Vocaret: Set
  API Key** and never written to settings. Alternatively leave it unset and
  export the environment variable in the table (or name another one in
  `vocaret.apiKeyEnv`).
- **Models.** `vocaret.model` takes the model name exactly as the provider
  spells it. Leave it empty to use the built-in default where there is one
  (`vocaret providers` lists them). Model line-ups change often, so treat the
  defaults as starting points.
- **Which model?** Latency matters more than raw capability here: the task is
  small and you are waiting for it. Fast mid-size models do well; in testing
  with Claude, Sonnet answered in about 1.5 s and passed all behaviour cases.
  Run `tests/model_cases.py PROVIDER MODEL` to see how a model does on 13
  realistic spoken instructions.
- **Privacy.** With a hosted provider, the transcript and the open file's
  text are sent to that provider for each instruction. With `ollama`,
  `lmstudio` or a local `custom` endpoint nothing leaves your machine.

## Settings

| Setting | Default | |
| --- | --- | --- |
| `vocaret.provider` | `claude-code` | See the table above. |
| `vocaret.model` | | Model name; empty uses the provider's default. |
| `vocaret.effort` | `low` | Reasoning effort, where the provider has one. |
| `vocaret.baseUrl` | | Endpoint for `custom`, or an override (proxy) for a preset. |
| `vocaret.apiKeyEnv` | | Environment variable holding the key. |
| `vocaret.extraBody` | `{}` | Extra JSON fields for OpenAI-compatible requests. |
| `vocaret.timeoutSeconds` | `90` | Give up on the model after this long. |
| `vocaret.autoStopSilenceSeconds` | `0` | Finish by itself after this much silence. Off by default so thinking pauses don't cut you off. |
| `vocaret.speechModel` | `auto` | `large-v3-turbo` on an NVIDIA GPU, `small.en` on CPU. |
| `vocaret.device` | `auto` | `cuda` or `cpu`. |
| `vocaret.language` | `en` | Spoken language. |
| `vocaret.recorderCommand` | | Custom capture command, to pick a specific microphone. |

To change the shortcut, open Keyboard Shortcuts and search for "Vocaret".

## How it works

```
editor extension ──unix socket──> vocaret daemon
  caret, file text                  microphone ─> Whisper (local) ─> transcript
  applies edits  <──────────────    transcript + file ─> your model ─> edits
```

- The model answers with small insert/replace operations. The daemon resolves
  them against your buffer, diffs old against new, and sends the editor only
  the minimal position-based edits, applied as one undo step. If you typed
  while it was working, the instruction is redone against the new text.
- One daemon serves all editor windows. It starts on first use, keeps the
  speech model loaded, and exits after 30 idle minutes (freeing GPU memory).
- The model gets no tools and no access to anything but the text of the open
  file. It cannot run commands or touch other files.

## Command line

```bash
vocaret doctor                       # check microphone, speech model, provider
vocaret providers                    # list providers and their default models
vocaret listen                       # record until Enter, print what was heard
vocaret edit Foo.java "add a main method that prints hello" --write
vocaret status | stop
```

Defaults for the command line and daemon live in
`~/.config/vocaret/config.json` (same keys as `daemon/vocaret/config.py`).
The daemon log is `~/.local/state/vocaret/daemon.log`.

## Troubleshooting

- **It hears nothing**: run `vocaret doctor`. A peak level near 0 means the
  wrong input device is selected in your system sound settings (or set
  `vocaret.recorderCommand`).
- **Model errors**: the message says what the provider answered. For
  subscriptions, run `claude` or `codex login` in a terminal once; for API
  providers, check the key and the model name.
- **Slow first instruction**: the speech model loads on first use (a few
  seconds), then stays loaded.
- **A model keeps answering in the wrong format**: smaller models sometimes
  do. Try a larger one, and please open an issue with the log (**Vocaret:
  Show Log**).

## Limits

- Linux first. macOS recording goes through SoX or ffmpeg and has not been
  tested; Windows is not supported yet.
- VS Code-family editors only. JetBrains IDEs would need their own plugin; the
  daemon and its socket protocol (`daemon/vocaret/server.py`) are
  editor-agnostic, and contributions are welcome.
- It edits the open file only; it does not create files or run anything.
- Very large files (over ~120k characters) are sent as a window of lines
  around the caret.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Publishing notes for maintainers are
in [docs/PUBLISHING.md](docs/PUBLISHING.md).

## License

[MIT](LICENSE)
