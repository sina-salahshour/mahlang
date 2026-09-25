"""`mah format [PATH ...] [--check]` -- see docs/FORMAT.md's CLI section."""

from __future__ import annotations

import os
import sys

from ..project.manifest import find_manifest
from .formatter import FormatError, FormatOptions, format_source

_SKIP_DIRS = {"build", "node_modules", "__pycache__"}


def _mh_files_under(directory: str) -> list:
    found = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        found.extend(os.path.join(root, name) for name in sorted(files) if name.endswith(".mh"))
    return found


def _default_root(cwd: str) -> str:
    manifest = find_manifest(cwd)
    return os.path.dirname(manifest) if manifest else cwd


def run_format(paths: list, check: bool, stdin=None, stdout=None, stderr=None, cwd: str | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    cwd = cwd or os.getcwd()
    options = FormatOptions()

    if paths == ["-"]:
        text = stdin.read()
        try:
            stdout.write(format_source(text, options))
        except FormatError as e:
            print(f"error: can't format <stdin>: {e}", file=stderr)
            return 1
        return 0

    files = []
    for path in paths or [_default_root(cwd)]:
        path = os.path.join(cwd, path)
        if os.path.isdir(path):
            files.extend(_mh_files_under(path))
        elif os.path.isfile(path):
            files.append(path)
        else:
            print(f"error: no such file or directory: {path}", file=stderr)
            return 1

    status = 0
    for path in files:
        display = os.path.relpath(path, cwd)
        if display.startswith(".."):
            display = path
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        try:
            formatted = format_source(text, options)
        except FormatError as e:
            print(f"error: can't format {display}: {e}", file=stderr)
            status = 1
            continue
        if formatted == text:
            continue
        if check:
            print(display, file=stdout)
            status = 1
        else:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(formatted)
            print(f"formatted {display}", file=stdout)
    return status
