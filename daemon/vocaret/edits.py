"""Turn the model's reply into concrete, minimal text edits.

The model answers in a small line-based operation format (see llm.SYSTEM_PROMPT).
This module parses it, resolves each operation against the buffer, applies them
to get the new buffer, and then diffs old against new. The editor only ever
receives that diff as position-based edits, so however the model phrased its
operations the edits are non-overlapping and touch only what changed.

All internal work is on "\\n"-normalised text with code-point indices.
Positions crossing the wire are LSP-style {line, character} with UTF-16
columns, which is what editors use and which is independent of CRLF.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

T_INSERT = "=====INSERT"
T_REPLACE = "=====REPLACE"
T_WITH = "=====WITH"
T_END = "=====END"
T_UNDO = "=====UNDO"
T_NOTE = "=====NOTE"


class EditError(ValueError):
    """The model's operations could not be applied to the buffer."""


@dataclass
class Markers:
    cursor: str = "[[CURSOR]]"
    sel_start: str = "[[SEL_START]]"
    sel_end: str = "[[SEL_END]]"

    def all(self) -> tuple[str, str, str]:
        return (self.cursor, self.sel_start, self.sel_end)

    def strip(self, text: str) -> str:
        for marker in self.all():
            text = text.replace(marker, "")
        return text


def make_markers(text: str) -> Markers:
    """Markers that do not occur in the file (so they can't be confused with it)."""
    suffix = ""
    n = 0
    while True:
        markers = Markers(f"[[CURSOR{suffix}]]", f"[[SEL_START{suffix}]]",
                          f"[[SEL_END{suffix}]]")
        if not any(m in text for m in markers.all()):
            return markers
        n += 1
        suffix = f"_{n}"


@dataclass
class Parsed:
    ops: list[tuple] = field(default_factory=list)  # ("insert", text) | ("replace", old, new)
    undo: bool = False
    note: str = ""
    stray: str = ""


_TOKEN = re.compile(r"^\s*={3,}\s*(INSERT|REPLACE|WITH|END|UNDO|NOTE)\b[ \t:]*(.*)$")


def _unfence(lines: list[str]) -> list[str]:
    """Drop a Markdown code fence a model wrapped around a block's content."""
    body = [i for i, ln in enumerate(lines) if ln.strip()]
    if (len(body) >= 2 and lines[body[0]].strip().startswith("```")
            and lines[body[-1]].strip() == "```"):
        return lines[body[0] + 1:body[-1]]
    return lines


def parse(reply: str) -> Parsed:
    """Parse the operation format. Tolerant of the slips weaker models make:
    spacing or extra "=" in the markers, code fences, chatter between blocks."""
    parsed = Parsed()
    state = None
    buf: list[str] = []
    old: list[str] = []
    stray: list[str] = []
    for line in reply.replace("\r\n", "\n").split("\n"):
        match = _TOKEN.match(line)
        token, rest = (match.group(1), match.group(2).strip()) if match else (None, "")
        if token in ("INSERT", "REPLACE", "WITH", "END", "UNDO") and rest.strip("= "):
            token = None  # a marker line carries nothing else; this is content
        if state is None:
            if token == "INSERT":
                state, buf = "insert", []
            elif token == "REPLACE":
                state, buf = "old", []
            elif token == "UNDO":
                parsed.undo = True
            elif token == "NOTE":
                parsed.note = (parsed.note + " " + rest.rstrip("= ")).strip()
            elif line.strip() and not line.strip().startswith("```"):
                stray.append(line)
        elif state == "insert":
            if token == "END":
                parsed.ops.append(("insert", "\n".join(_unfence(buf))))
                state = None
            else:
                buf.append(line)
        elif state == "old":
            if token == "WITH":
                old, buf, state = _unfence(buf), [], "new"
            else:
                buf.append(line)
        elif state == "new":
            if token == "END":
                parsed.ops.append(("replace", "\n".join(old), "\n".join(_unfence(buf))))
                state = None
            else:
                buf.append(line)
    if state is not None:
        raise EditError("The reply was cut off in the middle of an operation.")
    parsed.stray = "\n".join(stray).strip()
    return parsed


# ---------------------------------------------------------------- resolving

def _line_bounds(text: str, index: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return start, len(text) if end == -1 else end


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _place_insert(text: str, sel_start: int, sel_end: int, new: str) -> tuple[int, int, str, int]:
    """Decide where an INSERT lands. Returns (start, end, text, caret_offset_in_text)."""
    if sel_start != sel_end:
        return sel_start, sel_end, new, len(new)
    line_start, line_end = _line_bounds(text, sel_start)
    prefix = text[line_start:sel_start]
    suffix = text[sel_start:line_end]
    if prefix.strip():
        # Caret sits after code: literal insertion at the caret.
        return sel_start, sel_end, new, len(new)

    lines = new.split("\n")
    first_nonblank = next((ln for ln in lines if ln.strip()), "")
    start = sel_start
    if prefix and _indent_width(first_nonblank) == 0:
        # The model left the first line unindented, relying on the caret's
        # existing indentation. If later lines are indented relative to zero
        # as well, shift them under the caret; if they are already absolute,
        # leave them alone.
        rest = [ln for ln in lines[1:] if ln.strip()]
        if rest and min(_indent_width(ln) for ln in rest) < len(prefix):
            lines = [lines[0]] + [prefix + ln if ln.strip() else ln for ln in lines[1:]]
        new = "\n".join(lines)
    else:
        # Fully indented text replaces the whitespace before the caret.
        start = line_start
    caret = len(new)
    if suffix.strip():
        # Code follows on this line: keep it on its own line below the insert.
        new = new + "\n" + prefix
    return start, sel_end, new, caret


def _find_all(text: str, needle: str) -> list[int]:
    found, at = [], text.find(needle)
    while at != -1:
        found.append(at)
        at = text.find(needle, at + 1)
    return found


def _fuzzy_find(text: str, old: str) -> list[tuple[int, int]]:
    """Match `old` line by line, ignoring leading/trailing whitespace per line."""
    want = [ln.strip() for ln in old.split("\n")]
    while want and not want[0]:
        want.pop(0)
    while want and not want[-1]:
        want.pop()
    if not want:
        return []
    lines = text.split("\n")
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)
    spans = []
    for i in range(len(lines) - len(want) + 1):
        if all(lines[i + k].strip() == want[k] for k in range(len(want))):
            spans.append((starts[i], starts[i + len(want)] - 1))
    return spans


def _locate(text: str, old: str, caret: int) -> tuple[int, int]:
    spans = [(at, at + len(old)) for at in _find_all(text, old)]
    if not spans:
        spans = _fuzzy_find(text, old)
    if not spans:
        snippet = old.strip().split("\n")[0][:60]
        raise EditError(f'REPLACE text was not found in the file (starts with "{snippet}").')

    def distance(span: tuple[int, int]) -> int:
        if span[0] <= caret <= span[1]:
            return 0
        return min(abs(span[0] - caret), abs(span[1] - caret))

    return min(spans, key=distance)


def apply_ops(text: str, sel_start: int, sel_end: int, ops: list[tuple],
              markers: Markers) -> tuple[str, int]:
    """Apply parsed operations. Returns (new_text, caret) in code-point indices."""
    resolved = []  # (start, end, new_text, caret_offset_or_None, is_insert)
    for op in ops:
        if op[0] == "replace" and not markers.strip(op[1]).strip():
            op = ("insert", op[2])  # REPLACE of nothing is an insertion
        if op[0] == "insert":
            # Keep (only) the caret marker through placement, then read it off.
            body = op[1].replace(markers.sel_start, "").replace(markers.sel_end, "")
            head, sep, rest = body.partition(markers.cursor)
            body = head + sep + rest.replace(markers.cursor, "")
            start, end, new, caret = _place_insert(text, sel_start, sel_end, body)
            marked = new.find(markers.cursor)
            if marked != -1:
                new, caret = new.replace(markers.cursor, ""), marked
            resolved.append((start, end, new, caret, True))
        else:
            old = markers.strip(op[1])
            raw_new = op[2]
            explicit = raw_new.find(markers.cursor)
            new = markers.strip(raw_new)
            start, end = _locate(text, old, sel_start)
            caret = len(markers.strip(raw_new[:explicit])) if explicit != -1 else None
            resolved.append((start, end, new, caret, False))

    resolved.sort(key=lambda r: (r[0], r[1]))
    for prev, cur in zip(resolved, resolved[1:]):
        if cur[0] < prev[1]:
            raise EditError("Two operations overlap; fold them into one REPLACE.")

    out: list[str] = []
    at = 0
    out_len = 0
    explicit_caret = None
    insert_caret = None
    for start, end, new, caret, is_insert in resolved:
        out.append(text[at:start])
        out_len += start - at
        if is_insert:
            insert_caret = out_len + caret
        elif caret is not None and explicit_caret is None:
            explicit_caret = out_len + caret
        out.append(new)
        out_len += len(new)
        at = end
    out.append(text[at:])
    new_text = "".join(out)
    if explicit_caret is not None:
        caret = explicit_caret
    elif insert_caret is not None:
        caret = insert_caret
    else:
        # No stated caret: keep it where it was, carried through the changes.
        caret = map_index(diff_edits(text, new_text), sel_start)
    return new_text, max(0, min(caret, len(new_text)))


def map_index(edits: list[tuple[int, int, str]], index: int) -> int:
    """Where an index in the old text ends up after the edits."""
    shift = 0
    for start, end, new in sorted(edits):
        if index <= start:
            break
        if index < end:
            return start + shift + len(new)
        shift += len(new) - (end - start)
    return index + shift


# ------------------------------------------------------------------ diffing

def _keepends(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def diff_edits(old: str, new: str) -> list[tuple[int, int, str]]:
    """Minimal (start, end, replacement) edits turning old into new."""
    if old == new:
        return []
    a, b = _keepends(old), _keepends(new)
    # Trim identical leading/trailing lines first so difflib sees little.
    head = 0
    while head < min(len(a), len(b)) and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < min(len(a), len(b)) - head and a[-1 - tail] == b[-1 - tail]:
        tail += 1
    a_mid, b_mid = a[head:len(a) - tail], b[head:len(b) - tail]
    offsets = [sum(len(ln) for ln in a[:head])]
    for ln in a_mid:
        offsets.append(offsets[-1] + len(ln))

    edits = []
    matcher = difflib.SequenceMatcher(None, a_mid, b_mid, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_chunk = "".join(a_mid[i1:i2])
        new_chunk = "".join(b_mid[j1:j2])
        # Trim what the chunks share so the edit is as small as possible.
        p = 0
        while p < min(len(old_chunk), len(new_chunk)) and old_chunk[p] == new_chunk[p]:
            p += 1
        s = 0
        while (s < min(len(old_chunk), len(new_chunk)) - p
               and old_chunk[-1 - s] == new_chunk[-1 - s]):
            s += 1
        edits.append((offsets[i1] + p, offsets[i2] - s, new_chunk[p:len(new_chunk) - s]))
    return edits


def apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    out, at = [], 0
    for start, end, new in sorted(edits):
        out.append(text[at:start])
        out.append(new)
        at = end
    out.append(text[at:])
    return "".join(out)


# ------------------------------------------------- positions and line endings

def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _u16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _u16_to_cp(line: str, col16: int) -> int:
    if line.isascii():
        return min(col16, len(line))
    units = 0
    for i, c in enumerate(line):
        if units >= col16:
            return i
        units += 2 if ord(c) > 0xFFFF else 1
    return len(line)


class PositionMap:
    """Convert between code-point indices and {line, character(UTF-16)}."""

    def __init__(self, text: str):
        self.text = text
        self.lines = text.split("\n")
        self.starts = [0]
        for ln in self.lines[:-1]:
            self.starts.append(self.starts[-1] + len(ln) + 1)

    def to_index(self, pos: dict) -> int:
        line = max(0, min(int(pos.get("line", 0)), len(self.lines) - 1))
        col = _u16_to_cp(self.lines[line], max(0, int(pos.get("character", 0))))
        return self.starts[line] + col

    def to_position(self, index: int) -> dict:
        index = max(0, min(index, len(self.text)))
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= index:
                lo = mid
            else:
                hi = mid - 1
        col = index - self.starts[lo]
        return {"line": lo, "character": _u16_len(self.lines[lo][:col])}


_WS = re.compile(r"\s+")


def squash(text: str) -> str:
    return _WS.sub(" ", text).strip()
