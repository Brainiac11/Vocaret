# Changelog

## 0.2.0

- **Model-agnostic.** Pick the service that writes the code with
  *Vocaret: Choose Provider and Model*:
  - subscriptions through the vendor's CLI: Claude Code (Claude plans) and
    Codex CLI (ChatGPT plans);
  - API keys: Anthropic, OpenAI, OpenRouter, DeepSeek, Google Gemini, Groq,
    Mistral, xAI, Together AI;
  - local models: Ollama and LM Studio;
  - any other OpenAI-compatible endpoint via `vocaret.baseUrl`.
- API keys are kept in the editor's secret storage (*Vocaret: Set API Key*)
  or read from the provider's usual environment variable.
- The extension can install and update its own speech engine, so installing
  it from a marketplace is enough.
- More forgiving parsing of model replies (code fences, marker spacing,
  inline reasoning tags) so smaller models work.
- Best-effort macOS recording through SoX or ffmpeg.

## 0.1.0

- First version: local Whisper speech recognition, Claude through Claude
  Code, VS Code / Cursor extension, one undo step per spoken instruction.
