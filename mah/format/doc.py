"""A small Wadler/prettier-style document model and printer, used by
`mah format` (see docs/FORMAT.md).

A document is built from:

- `str`: literal text (never containing a newline);
- `list`: concatenation;
- `LINE` (a space when its group is flat, a newline when broken),
  `SOFTLINE` (nothing / a newline), `HARDLINE` (always a newline, and it
  forces every enclosing group to break);
- `Group(contents)`: printed flat if it fits in the remaining width,
  otherwise broken, decided outermost first;
- `Indent(contents)`: newlines inside it indent one level deeper;
- `LineSuffix(text)`: a trailing comment, held back until the next newline
  and printed at the end of the line it was attached to;
- `BREAK_PARENT`: forces every enclosing group to break.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Line:
    soft: bool = False
    hard: bool = False


LINE = Line()
SOFTLINE = Line(soft=True)
HARDLINE = Line(hard=True)


@dataclass
class Group:
    contents: object
    broken: bool = False


@dataclass
class Indent:
    contents: object


@dataclass
class LineSuffix:
    text: str


class _BreakParent:
    def __repr__(self) -> str:
        return "BREAK_PARENT"


BREAK_PARENT = _BreakParent()


def propagate_breaks(doc) -> bool:
    """Mark every group that contains a hard line or `BREAK_PARENT` as
    broken. Returns whether `doc` itself contains one."""
    if isinstance(doc, str) or isinstance(doc, LineSuffix):
        return False
    if doc is BREAK_PARENT:
        return True
    if isinstance(doc, Line):
        return doc.hard
    if isinstance(doc, list):
        found = False
        for part in doc:
            if propagate_breaks(part):
                found = True
        return found
    if isinstance(doc, Indent):
        return propagate_breaks(doc.contents)
    if isinstance(doc, Group):
        if propagate_breaks(doc.contents):
            doc.broken = True
        return doc.broken
    raise TypeError(f"not a doc: {doc!r}")


def contains_hard_break(doc) -> bool:
    """Whether `doc` will certainly print a newline (after
    `propagate_breaks`, since that's what marks groups broken)."""
    if isinstance(doc, (str, LineSuffix)):
        return False
    if doc is BREAK_PARENT:
        return True
    if isinstance(doc, Line):
        return doc.hard
    if isinstance(doc, list):
        return any(contains_hard_break(part) for part in doc)
    if isinstance(doc, Indent):
        return contains_hard_break(doc.contents)
    if isinstance(doc, Group):
        return contains_hard_break(doc.contents)
    raise TypeError(f"not a doc: {doc!r}")


@dataclass
class PrintedLine:
    code: str
    comment: str | None = None


_FLAT, _BREAK = "flat", "break"


def _fits(next_cmd, rest: list, width: int) -> bool:
    """Whether `next_cmd` (in flat mode) plus the rest of the current line
    fits in `width` columns. The rest is read from `rest` (the printer's
    command stack, top at the end) until the first newline."""
    pending = [next_cmd]
    rest_index = len(rest) - 1
    while width >= 0:
        if not pending:
            if rest_index < 0:
                return True
            pending.append(rest[rest_index])
            rest_index -= 1
            continue
        indent, mode, doc = pending.pop()
        if isinstance(doc, str):
            width -= len(doc)
        elif isinstance(doc, list):
            for part in reversed(doc):
                pending.append((indent, mode, part))
        elif isinstance(doc, Indent):
            pending.append((indent + 1, mode, doc.contents))
        elif isinstance(doc, Group):
            pending.append((indent, _BREAK if doc.broken else mode, doc.contents))
        elif isinstance(doc, Line):
            if mode == _BREAK or doc.hard:
                return True
            if not doc.soft:
                width -= 1
    return False


def print_doc(doc, width: int, indent_text: str) -> list[PrintedLine]:
    """Lay `doc` out. Returns the lines, each with its trailing comment (if
    any) kept apart so the caller can align them."""
    propagate_breaks(doc)
    lines: list[PrintedLine] = []
    parts: list[str] = []
    column = 0
    suffixes: list[str] = []
    stack = [(0, _BREAK, doc)]

    def end_line():
        code = "".join(parts).rstrip()
        comment = " ".join(suffixes) if suffixes else None
        lines.append(PrintedLine(code, comment))
        parts.clear()
        suffixes.clear()

    while stack:
        indent, mode, current = stack.pop()
        if isinstance(current, str):
            parts.append(current)
            column += len(current)
        elif isinstance(current, list):
            for part in reversed(current):
                stack.append((indent, mode, part))
        elif isinstance(current, Indent):
            stack.append((indent + 1, mode, current.contents))
        elif isinstance(current, Group):
            if current.broken:
                stack.append((indent, _BREAK, current.contents))
            elif mode == _FLAT or _fits((indent, _FLAT, current.contents), stack, width - column):
                stack.append((indent, _FLAT, current.contents))
            else:
                stack.append((indent, _BREAK, current.contents))
        elif isinstance(current, Line):
            if mode == _FLAT and not current.hard:
                if not current.soft:
                    parts.append(" ")
                    column += 1
            else:
                end_line()
                parts.append(indent_text * indent)
                column = len(indent_text) * indent
        elif isinstance(current, LineSuffix):
            suffixes.append(current.text)
        elif current is BREAK_PARENT:
            pass
        else:
            raise TypeError(f"not a doc: {current!r}")
    end_line()
    return lines
