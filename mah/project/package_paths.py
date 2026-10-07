"""M43 (docs/PACKAGES.md): lexical helpers for paths inside installed
packages, `<project>/.mah/packages/<name>/...`. Imported at top level by
the preprocessor and the bytecode lowerer, so this module must not import
anything else from `mah` (it keeps the import graph acyclic)."""

from __future__ import annotations

import os
import re

PKG_PREFIX = "pkg:"
NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")


def package_of_path(path) -> tuple[str, str, str] | None:
    """`(project_root, name, rel)` when `abspath(path)`'s components contain
    `.mah`, `packages`, NAME, then at least one more component; the FIRST
    such occurrence counts. `rel` uses '/'. Purely lexical: no file access."""
    if not path or path.startswith("<"):
        return None
    full = os.path.abspath(path)
    drive, rest = os.path.splitdrive(full)
    parts = rest.replace("\\", "/").split("/")
    for i in range(len(parts) - 3):
        if parts[i] == ".mah" and parts[i + 1] == "packages" and parts[i + 2] and len(parts) > i + 3:
            tail = [p for p in parts[i + 3 :]]
            if not tail or not tail[0]:
                continue
            root = drive + ("/".join(parts[:i]) or "/")
            if os.sep != "/":
                root = root.replace("/", os.sep)
            return root, parts[i + 2], "/".join(tail)
    return None


def package_label(path) -> str | None:
    """'pkg:NAME/REL' for a file inside an installed package, else None."""
    found = package_of_path(path)
    if found is None:
        return None
    _root, name, rel = found
    return f"{PKG_PREFIX}{name}/{rel}"
