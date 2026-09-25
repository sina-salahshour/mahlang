"""M15: `mah init` -- scaffold a new Mah project from the templates in
mah/project/templates/. Every file under that directory is copied to the
same relative path in the new project (`{{name}}` replaced by the package
name), so adding, moving, or removing a template file needs no code change
here -- see `_template_files`. The one rename: a file called `gitignore`
becomes `.gitignore` (a real dotfile in the source tree would be ignored by
git itself). Templates are located relative to this module's `__file__`
rather than any installed-package root, so this still works once `mah/` is
copied wholesale by an installer.

The templates document the language for users and LLMs
(`docs/mah-language.md`, `AGENTS.md`), so they must change whenever the
language does -- `tests/test_project.py`'s template tests catch the drift
they can detect (doc examples that stop compiling, keywords/builtins the
reference never mentions)."""

from __future__ import annotations

import os
import re

from .manifest import MANIFEST_NAME, MahProjectError

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

# Template files whose destination name differs from their source name.
_RENAMES = {"gitignore": ".gitignore"}


def _template_files() -> list:
    """`(template path, destination path)` pairs, both relative (with `/`
    separators), for every file under `_TEMPLATES_DIR`: the manifest first,
    then everything else sorted by path, so output order is stable."""
    pairs = []
    for dirpath, dirnames, filenames in os.walk(_TEMPLATES_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            rel = os.path.relpath(os.path.join(dirpath, filename), _TEMPLATES_DIR).replace(os.sep, "/")
            pairs.append((rel, _RENAMES.get(rel, rel)))
    pairs.sort(key=lambda pair: (pair[1] != MANIFEST_NAME, pair[1]))
    return pairs


def sanitize_package_name(dirname: str) -> str:
    """Turn a directory's basename into a valid-looking package name:
    lowercase, every run of characters outside `[a-z0-9_-]` collapsed to a
    single `-`, leading/trailing `-` stripped, and `"mah-project"` if
    nothing's left (e.g. `"!!!"`)."""
    name = re.sub(r"[^a-z0-9_-]+", "-", dirname.lower()).strip("-")
    return name or "mah-project"


def init_project(directory: str) -> tuple[str, list[str], list[str]]:
    """Scaffold a new project in `directory` (created, with its parents, if
    it doesn't already exist). Returns `(package_name, created_relpaths,
    skipped_relpaths)`; an existing `mah-project.toml` fails the whole call
    (`MahProjectError`) before anything is created. An existing destination
    file for any other template is left untouched and reported as
    skipped."""
    abs_dir = os.path.abspath(directory)
    os.makedirs(abs_dir, exist_ok=True)

    manifest_dest = os.path.join(abs_dir, MANIFEST_NAME)
    if os.path.exists(manifest_dest):
        raise MahProjectError(f"{directory} is already a Mah project ({MANIFEST_NAME} exists)")

    package_name = sanitize_package_name(os.path.basename(abs_dir))

    created: list[str] = []
    skipped: list[str] = []
    for template_rel, dest_rel in _template_files():
        dest_path = os.path.join(abs_dir, dest_rel)
        if os.path.exists(dest_path):
            skipped.append(dest_rel)
            continue

        with open(os.path.join(_TEMPLATES_DIR, template_rel), encoding="utf-8") as f:
            content = f.read()
        content = content.replace("{{name}}", package_name)

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        created.append(dest_rel)

    return package_name, created, skipped
