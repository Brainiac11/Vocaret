# Vocaret

Voice typing for code. You keep writing code yourself; when it's faster to say
something than to type it, press **Ctrl+Alt+Space**, describe it, and press
**Ctrl+Alt+Space** again. The code appears at your caret.

> "create a public method called test with parameters string x and string y
> that returns x plus y"

```java
public String test(String x, String y) {
    return x + y;
}
```

> "put that inside a class Foo, with a constructor that takes a string s and
> prints it"

It writes what you described and nothing more, in the style of the file you're
in. Each voice instruction is one undo step (Ctrl+Z), and saying "undo that"
works too.

## Getting started

1. Install the extension and press **Ctrl+Alt+Space** in a file. The first
   time, Vocaret offers to set up its speech engine: a private Python
   environment in `~/.local/share/vocaret` plus a Whisper model. Speech
   recognition runs on your machine; audio never leaves it.
2. Run **Vocaret: Choose Provider and Model** to pick what writes the code.

Requires Linux (macOS is best-effort), Python 3.10+ and a microphone.

## Bring your own model

| Provider | How you pay |
| --- | --- |
| Claude Code (default) | your Claude subscription |
| Codex CLI | your ChatGPT subscription |
| Anthropic, OpenAI, OpenRouter, DeepSeek, Gemini, Groq, Mistral, xAI, Together | API key |
| Ollama, LM Studio | free, runs locally |
| Custom | any OpenAI-compatible endpoint |

API keys are kept in the editor's secret storage (**Vocaret: Set API Key**),
or read from the provider's usual environment variable. With a hosted
provider, the transcript and the open file's text are sent to it for each
instruction; with a local one nothing leaves your machine.

## Keys and commands

| Key | What it does |
| --- | --- |
| Ctrl+Alt+Space | Start listening; press again to finish |
| Esc (while listening) | Cancel |
| Click the status bar item while it's working | Cancel |

Commands (Ctrl+Shift+P): **Vocaret: Choose Provider and Model**, **Vocaret:
Set API Key**, **Vocaret: Type an Instruction Instead of Speaking**,
**Vocaret: Check Setup**, **Vocaret: Show Log**, **Vocaret: Install or Update
Speech Engine**, **Vocaret: Restart Background Service**.

## What you can say

- "add a private field called name of type string"
- "loop over the items and print each one"
- "rename x to count", "make that method static", "delete the constructor"
- select code, then "wrap this in a try catch that catches IO exception"
- fillers and corrections are fine: "call it foo, no wait, bar"

Vocaret is open source (MIT). Documentation, issues and source:
https://github.com/Brainiac11/Vocaret
