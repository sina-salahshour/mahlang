"""M15: `mah-project.toml` -- the project manifest read by `mah run`/`mah
build` in project mode (mah/cli/main.py). Parsing is stdlib-only
(`tomllib`, Python >= 3.11); everything is validated up front in
`load_project` so the CLI only ever has to catch one exception type
(`MahProjectError`) and print its message as-is (it already starts with
`f"{manifest_path}: "`, see each check below)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

MANIFEST_NAME = "mah-project.toml"

_PACKAGE_KEYS = {"name", "version", "entry"}
_TARGET_KEYS = {"name", "profile", "out"}
_TOP_LEVEL_KEYS = {"package", "target", "dependencies"}
_PROFILES = {"debug", "release"}


class MahProjectError(Exception):
    """Raised for any malformed `mah-project.toml` (see `load_project`) or
    project-scaffolding failure (see `mah/project/init.py`). The message is
    already user-facing -- the CLI prints it verbatim after `error: `."""


@dataclass
class Target:
    name: str
    profile: str  # "debug" | "release"
    out: str  # absolute path


@dataclass
class Project:
    root: str  # absolute directory containing the manifest
    manifest_path: str
    name: str
    version: str
    entry: str  # absolute path
    targets: list[Target] = field(default_factory=list)
    dependencies: dict = field(default_factory=dict)


def find_manifest(start_dir: str) -> str | None:
    """Search `start_dir`, then each parent up to the filesystem root, for
    a `mah-project.toml`. Returns the first one found's path, or `None`."""
    current = os.path.abspath(start_dir)
    while True:
        candidate = os.path.join(current, MANIFEST_NAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def load_project(manifest_path: str) -> Project:
    """Parse and fully validate `manifest_path`, raising `MahProjectError`
    (message prefixed with `f"{manifest_path}: "`) on the first problem
    found."""
    def fail(message: str):
        raise MahProjectError(f"{manifest_path}: {message}")

    with open(manifest_path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            fail(f"invalid TOML: {e}")

    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            hint = " (did you mean [[target]]?)" if key == "targets" else ""
            fail(f"unknown key '{key}'{hint}")

    if "package" not in data:
        fail("missing [package] table")
    package = data["package"]

    for key in package:
        if key not in _PACKAGE_KEYS:
            fail(f"unknown key 'package.{key}'")

    name = package.get("name")
    if not isinstance(name, str) or not name:
        fail("package.name must be a non-empty string")

    version = package.get("version")
    if not isinstance(version, str) or not version:
        fail("package.version must be a non-empty string")

    root = os.path.dirname(manifest_path)

    entry = package.get("entry", "src/main.mh")
    if not isinstance(entry, str):
        fail("package.entry must be a string")
    entry_path = os.path.join(root, entry)

    targets: list[Target] = []
    raw_targets = data.get("target")
    if raw_targets is not None:
        if not isinstance(raw_targets, list):
            fail("target must be an array of tables, written [[target]]")
        seen_names: dict[str, str] = {}
        seen_outs: dict[str, str] = {}
        for i, raw_target in enumerate(raw_targets):
            for key in raw_target:
                if key not in _TARGET_KEYS:
                    fail(f"unknown key 'target[{i}].{key}'")

            t_name = raw_target.get("name")
            if not isinstance(t_name, str) or not t_name:
                fail(f"target[{i}] must have a non-empty string 'name'")
            if t_name in seen_names:
                fail(f"duplicate target name '{t_name}'")

            profile = raw_target.get("profile")
            if profile not in _PROFILES:
                fail(f"target '{t_name}': profile must be \"debug\" or \"release\"")

            out = raw_target.get("out")
            if not isinstance(out, str) or not out:
                fail(f"target '{t_name}' must have a non-empty string 'out'")
            out_path = os.path.join(root, out)
            resolved_out = os.path.normpath(out_path)
            if resolved_out in seen_outs:
                other = seen_outs[resolved_out]
                fail(f"targets '{other}' and '{t_name}' write the same file {out_path}")

            seen_names[t_name] = out_path
            seen_outs[resolved_out] = t_name
            targets.append(Target(name=t_name, profile=profile, out=out_path))

    dependencies = data.get("dependencies", {})
    if not isinstance(dependencies, dict):
        fail("dependencies must be a table")
    if dependencies:
        fail("third-party dependencies aren't supported yet; leave [dependencies] empty")

    return Project(
        root=root,
        manifest_path=manifest_path,
        name=name,
        version=version,
        entry=entry_path,
        targets=targets,
        dependencies=dependencies,
    )
