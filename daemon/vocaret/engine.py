"""Instruction -> edits pipeline, shared by the daemon and the CLI."""

from __future__ import annotations

import logging
import time
from collections import deque

from . import edits as E
from .llm import SYSTEM_PROMPT, LLMError, build_request
from .providers import ModelRunner

log = logging.getLogger("vocaret.engine")

HISTORY_ITEMS = 3
HISTORY_SECONDS = 600


class Engine:
    def __init__(self) -> None:
        self.model = ModelRunner()
        self._history: dict[str, deque] = {}

    def _recent(self, path: str) -> list[dict]:
        items = self._history.get(path)
        if not items:
            return []
        cutoff = time.time() - HISTORY_SECONDS
        return [item for item in items if item["at"] >= cutoff]

    def _remember(self, path: str, transcript: str, reply: str) -> None:
        self._history.setdefault(path, deque(maxlen=HISTORY_ITEMS)).append(
            {"at": time.time(), "transcript": transcript, "reply": reply.strip()})

    def instruct(self, transcript: str, ctx: dict, cfg: dict) -> dict:
        """Turn one spoken instruction into edits for the buffer in ctx.

        ctx: {path, language, indent, text, selection: {start, end}} with
        LSP-style positions. Returns {edits, cursor, undo, note, ...}; edit
        positions refer to the buffer as it was sent.
        """
        transcript = E.squash(transcript)
        if not transcript:
            return {"edits": [], "cursor": None, "undo": False,
                    "note": "I didn't catch anything.", "transcript": ""}

        text = E.normalize(ctx.get("text") or "")
        ctx = dict(ctx, text=text)
        pmap = E.PositionMap(text)
        selection = ctx.get("selection") or {}
        sel_start = pmap.to_index(selection.get("start") or {})
        sel_end = pmap.to_index(selection.get("end") or selection.get("start") or {})
        if sel_end < sel_start:
            sel_start, sel_end = sel_end, sel_start
        path = ctx.get("path") or "(untitled)"
        markers = E.make_markers(text)
        request = build_request(transcript, ctx, sel_start, sel_end, markers,
                                self._recent(path), cfg)

        started = time.monotonic()
        reply = ""
        new_text, caret, parsed = text, sel_start, E.Parsed()
        attempt_request = request
        for attempt in (1, 2):
            reply = self.model.run(SYSTEM_PROMPT, attempt_request, cfg)
            try:
                parsed = E.parse(reply)
                if not parsed.ops and not parsed.undo and not parsed.note:
                    raise E.EditError("The reply contained no operations.")
                new_text, caret = E.apply_ops(text, sel_start, sel_end, parsed.ops, markers)
                break
            except E.EditError as exc:
                log.warning("attempt %d could not be applied: %s\n%s", attempt, exc, reply)
                if attempt == 2:
                    raise LLMError(f"Couldn't apply the edit: {exc}") from exc
                # One retry, telling the model exactly what went wrong.
                attempt_request = (
                    request
                    + "\n\nYour previous reply could not be applied:\n<previous_reply>\n"
                    + reply + "\n</previous_reply>\nProblem: " + str(exc)
                    + "\nReply again with corrected operations in the required format."
                )
        llm_ms = int((time.monotonic() - started) * 1000)

        new_map = E.PositionMap(new_text)
        wire_edits = [
            {"start": pmap.to_position(s), "end": pmap.to_position(e), "text": t}
            for s, e, t in E.diff_edits(text, new_text)
        ]
        if parsed.undo:
            items = self._history.get(path)
            if items:
                items.pop()
        elif wire_edits:
            self._remember(path, transcript, reply)
        return {
            "transcript": transcript,
            "edits": wire_edits,
            "cursor": new_map.to_position(caret) if wire_edits else None,
            "undo": parsed.undo and not wire_edits,
            "note": parsed.note,
            "reply": reply,
            "llm_ms": llm_ms,
        }
