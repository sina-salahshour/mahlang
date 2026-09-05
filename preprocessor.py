"""Import preprocessor for the Mah language.

Mah's compiler is a single-pass, order-dependent, syntax-directed translator
generated from ``mah.lang``; adding an ``import`` construct to the grammar
itself would be invasive. Instead this module implements ``import`` as a
lightweight, C ``#include``-style preprocessor that runs *before* the lexer:

    import "relative/path.mh"

Each import directive (a statement occupying its own line, with an optional
trailing ``;``) is replaced by the *contents* of the referenced file, resolved
relative to the importing file's directory. Imports are resolved recursively;
each file is inlined at most once (include-guard semantics), which also makes
import cycles safe.

The result is a single combined source string plus a *source map* that lets
callers translate an offset in the combined text back to the original
``(file, offset)`` it came from. This is what makes cross-file error reporting
and go-to-definition possible.

This module is dependency-free (standard library only).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

# A whole-line import directive: `import "path"` with optional `;` and comment.
IMPORT_RE = re.compile(
    r'^(?P<lead>\s*)import\s+"(?P<path>(?:[^"\\]|\\.)*)"\s*;?\s*(?:#.*)?$'
)

BUFFER_PATH = "<buffer>"


def _decode_path(literal: str) -> str:
    try:
        return bytes(literal, "utf-8").decode("unicode_escape")
    except Exception:  # noqa: BLE001 - fall back to the raw literal
        return literal


@dataclass
class ImportSite:
    """An ``import`` directive found in the *entry* file."""

    literal: str          # the path text as written (without quotes)
    resolved: Optional[str]  # absolute path it resolves to (may not exist)
    exists: bool
    offset: int           # entry-file offset of the opening quote
    length: int           # length covering both quotes
    line: int             # 0-based line of the directive in the entry file


@dataclass
class Segment:
    """A contiguous slice of the combined text and where it came from."""

    start: int            # start offset in the combined text
    length: int
    path: str             # origin file (absolute) or BUFFER_PATH
    src_offset: int       # offset in the origin file mapped to ``start``
    root_import: Optional[int]  # index into entry_imports, or None for entry text


@dataclass
class Preprocessed:
    text: str
    segments: list
    files: dict            # path -> source text (entry + every inlined file)
    entry_path: str
    entry_imports: list    # ImportSite, entry-file directives only
    errors: list           # (message, entry_offset, length) for bad imports

    # -- source map queries ------------------------------------------------
    def map_to_source(self, offset: int):
        """Translate a combined-text offset to ``(path, source_offset)``."""
        for segment in self.segments:
            if segment.start <= offset < segment.start + segment.length:
                return segment.path, segment.src_offset + (offset - segment.start)
        if self.segments:
            last = self.segments[-1]
            return last.path, last.src_offset + last.length
        return self.entry_path, offset

    def root_import_for(self, offset: int) -> Optional[ImportSite]:
        """The entry-file import that pulled in the text at ``offset`` (if any)."""
        for segment in self.segments:
            if segment.start <= offset < segment.start + segment.length:
                if segment.root_import is None:
                    return None
                return self.entry_imports[segment.root_import]
        return None

    def entry_to_combined(self, entry_offset: int) -> Optional[int]:
        """Translate an offset in the entry file to the combined text."""
        for segment in self.segments:
            if segment.path != self.entry_path:
                continue
            if segment.src_offset <= entry_offset < segment.src_offset + segment.length:
                return segment.start + (entry_offset - segment.src_offset)
        # Allow a cursor sitting at the end of the entry's last token.
        for segment in self.segments:
            if segment.path != self.entry_path:
                continue
            if entry_offset == segment.src_offset + segment.length:
                return segment.start + segment.length
        return None


def preprocess(path: Optional[str], text: Optional[str] = None) -> Preprocessed:
    """Resolve imports starting from ``path`` (or in-memory ``text``).

    If ``text`` is given it is used as the entry file's contents (so unsaved
    editor buffers can be analyzed); imported files are always read from disk.
    """
    entry_path = os.path.abspath(path) if path else BUFFER_PATH
    base_dir = os.path.dirname(entry_path) if path else os.getcwd()

    if text is None:
        with open(entry_path, encoding="utf-8") as handle:
            text = handle.read()

    files: dict[str, str] = {entry_path: text}
    segments: list[Segment] = []
    entry_imports: list[ImportSite] = []
    errors: list[tuple] = []
    included: set[str] = {entry_path}
    combined: list[str] = []
    state = {"len": 0}

    def emit(piece: str, fpath: str, src_offset: int, root: Optional[int]) -> None:
        if not piece:
            return
        segments.append(Segment(state["len"], len(piece), fpath, src_offset, root))
        combined.append(piece)
        state["len"] += len(piece)

    def directory_of(fpath: str) -> str:
        return os.path.dirname(fpath) if fpath != BUFFER_PATH else base_dir

    def process(fpath: str, source: str, root: Optional[int], is_entry: bool) -> None:
        run_start: Optional[int] = None
        line_offset = 0
        for line in source.splitlines(keepends=True):
            body = line.rstrip("\n").rstrip("\r")
            match = IMPORT_RE.match(body)
            if match:
                # Flush the run of ordinary lines accumulated so far.
                if run_start is not None:
                    emit(source[run_start:line_offset], fpath, run_start, root)
                    run_start = None

                literal = match.group("path")
                quote_start = line_offset + (match.start("path") - 1)
                quote_length = (match.end("path") + 1) - (match.start("path") - 1)
                resolved = os.path.abspath(
                    os.path.join(directory_of(fpath), _decode_path(literal))
                )
                exists = os.path.isfile(resolved)

                child_root = root
                if is_entry:
                    entry_imports.append(
                        ImportSite(
                            literal=literal,
                            resolved=resolved,
                            exists=exists,
                            offset=quote_start,
                            length=quote_length,
                            line=source.count("\n", 0, line_offset),
                        )
                    )
                    child_root = len(entry_imports) - 1

                if not exists:
                    if is_entry:
                        errors.append(
                            (
                                f"cannot find imported file '{literal}'",
                                quote_start,
                                quote_length,
                            )
                        )
                elif resolved in included:
                    pass  # already inlined once; dedup like an include guard
                else:
                    included.add(resolved)
                    sub_source: Optional[str]
                    try:
                        with open(resolved, encoding="utf-8") as handle:
                            sub_source = handle.read()
                    except OSError as error:
                        sub_source = None
                        if is_entry:
                            errors.append(
                                (
                                    f"cannot read imported file '{literal}': {error}",
                                    quote_start,
                                    quote_length,
                                )
                            )
                    if sub_source is not None:
                        files[resolved] = sub_source
                        process(resolved, sub_source, child_root, is_entry=False)
                        # Separator so the last imported token can't merge with
                        # whatever follows the import directive.
                        emit("\n", fpath, line_offset, child_root)
            else:
                if run_start is None:
                    run_start = line_offset
            line_offset += len(line)

        if run_start is not None:
            emit(source[run_start:line_offset], fpath, run_start, root)

    process(entry_path, text, root=None, is_entry=True)

    return Preprocessed(
        text="".join(combined),
        segments=segments,
        files=files,
        entry_path=entry_path,
        entry_imports=entry_imports,
        errors=errors,
    )
