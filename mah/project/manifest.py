"""M15: `mah-project.toml` -- the project manifest read by `mah run`/`mah
build` in project mode (mah/cli/main.py). Parsing is stdlib-only
(`tomllib`, Python >= 3.11); everything is validated up front in
`load_project` so the CLI only ever has to catch one exception type
(`MahProjectError`) and print its message as-is (it already starts with
`f"{manifest_path}: "`, see each check below)."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field

from ..compiler import typecheck

MANIFEST_NAME = "mah-project.toml"

_PACKAGE_KEYS = {"name", "version", "entry", "lib"}
_TARGET_KEYS = {"name", "profile", "out", "self-contained"}
_RUN_KEYS = {"vm"}
_TYPES_KEYS = {"check"}
_TOP_LEVEL_KEYS = {"package", "target", "run", "dependencies", "types"}
_PROFILES = {"debug", "release"}
VMS = ("python", "rust")

# M43 (docs/PACKAGES.md): [dependencies] entries.
_DEPENDENCY_KEYS = ("github", "tag", "branch", "rev", "path")
_DEP_NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")
_GITHUB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
_REV_RE = re.compile(r"[0-9a-fA-F]{40}")


class MahProjectError(Exception):
    """Raised for any malformed `mah-project.toml` (see `load_project`) or
    project-scaffolding failure (see `mah/project/init.py`). The message is
    already user-facing -- the CLI prints it verbatim after `error: `."""


@dataclass
class Target:
    name: str
    profile: str  # "debug" | "release"
    out: str  # absolute path
    # bundle the Rust runtime into the output (`mah build --self-contained`)
    self_contained: bool = False


@dataclass(frozen=True)
class Dependency:
    """M43: one `[dependencies]` entry (docs/PACKAGES.md)."""

    name: str
    github: str  # "owner/repo", as written
    ref_kind: str  # "tag" | "branch" | "rev" | "default"
    ref: str | None  # None iff ref_kind == "default"; rev lowercased
    path: str  # "" = repository root

    def spec(self) -> tuple:
        """What "the same dependency" means everywhere (the name aside)."""
        return (self.github, self.ref_kind, self.ref, self.path)

    def describe(self) -> str:
        if self.ref_kind == "default":
            text = f"{self.github} default branch"
        elif self.ref_kind == "rev":
            text = f"{self.github} rev {self.ref[:7]}"
        else:
            text = f"{self.github} {self.ref_kind} {self.ref}"
        if self.path:
            text += f", path {self.path}"
        return text


def _toml_type_name(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, dict):
        return "table"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "datetime"


def _value_repr(value) -> str:
    return json.dumps(value) if isinstance(value, str) else _toml_type_name(value)


def parse_dependency(name: str, raw) -> Dependency:
    """Validate one `[dependencies]` entry; raises `ValueError` with the
    message (without the manifest-path prefix) on the first problem, in
    the order of docs/PACKAGES.md's table."""
    if not isinstance(name, str) or _DEP_NAME_RE.fullmatch(name) is None:
        raise ValueError(
            f"dependency name '{name}' must be lowercase letters, digits, '_' and '-', starting with a letter"
        )
    if not isinstance(raw, dict):
        raise ValueError(
            f"dependency '{name}' must be a table, like " + name + ' = { github = "owner/repo", tag = "v1.0" }'
        )
    for key in raw:
        if key not in _DEPENDENCY_KEYS:
            raise ValueError(f"unknown key 'dependencies.{name}.{key}'")
    if "github" not in raw:
        raise ValueError(f"dependency '{name}' needs github = \"owner/repo\"")
    github = raw["github"]
    if isinstance(github, str) and github.endswith(".git"):
        raise ValueError(f"dependency '{name}': write github = \"owner/repo\" without \".git\"")
    if (
        not isinstance(github, str)
        or _GITHUB_RE.fullmatch(github) is None
        or github.split("/", 1)[1] in (".", "..")
    ):
        raise ValueError(f"dependency '{name}': github must be \"owner/repo\" (got {_value_repr(github)})")
    if len([k for k in ("tag", "branch", "rev") if k in raw]) > 1:
        raise ValueError(f"dependency '{name}': use only one of tag, branch and rev")
    ref_kind, ref = "default", None
    if "tag" in raw:
        if not isinstance(raw["tag"], str) or not raw["tag"]:
            raise ValueError(f"dependency '{name}': tag must be a non-empty string")
        ref_kind, ref = "tag", raw["tag"]
    if "branch" in raw:
        if not isinstance(raw["branch"], str) or not raw["branch"]:
            raise ValueError(f"dependency '{name}': branch must be a non-empty string")
        ref_kind, ref = "branch", raw["branch"]
    if "rev" in raw:
        if not isinstance(raw["rev"], str) or _REV_RE.fullmatch(raw["rev"]) is None:
            raise ValueError(f"dependency '{name}': rev must be a full 40-character commit hash")
        ref_kind, ref = "rev", raw["rev"].lower()
    path = ""
    if "path" in raw:
        raw_path = raw["path"]
        bad = ValueError(
            f"dependency '{name}': path must be a relative path inside the repository, "
            f"like \"lib\" (got {_value_repr(raw_path)})"
        )
        if not isinstance(raw_path, str):
            raise bad
        path = raw_path[:-1] if raw_path.endswith("/") else raw_path
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or ":" in path
            or any(seg in ("", ".", "..") for seg in path.split("/"))
        ):
            raise bad
    return Dependency(name=name, github=github, ref_kind=ref_kind, ref=ref, path=path)


@dataclass
class Project:
    root: str  # absolute directory containing the manifest
    manifest_path: str
    name: str
    version: str
    entry: str  # absolute path
    targets: list[Target] = field(default_factory=list)
    # which VM `mah run` uses unless `--vm` says otherwise ([run] vm)
    run_vm: str = "python"
    # M43: name -> Dependency, in sorted name order (docs/PACKAGES.md)
    dependencies: dict = field(default_factory=dict)
    # static type-checking strictness ([types] check) -- see mah/compiler/typecheck.py
    type_check: str = typecheck.DEFAULT_CHECK_LEVEL
    # M43: the library entry used when this project is installed as a
    # package ([package] lib, default src/lib.mh), as an absolute path
    lib: str = ""


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

    lib = package.get("lib", "src/lib.mh")
    if not isinstance(lib, str):
        fail("package.lib must be a string")
    lib_path = os.path.join(root, lib)

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

            self_contained = raw_target.get("self-contained", False)
            if not isinstance(self_contained, bool):
                fail(f"target '{t_name}': self-contained must be true or false")

            seen_names[t_name] = out_path
            seen_outs[resolved_out] = t_name
            targets.append(Target(name=t_name, profile=profile, out=out_path, self_contained=self_contained))

    run = data.get("run", {})
    if not isinstance(run, dict):
        fail("run must be a table, written [run]")
    for key in run:
        if key not in _RUN_KEYS:
            fail(f"unknown key 'run.{key}'")
    run_vm = run.get("vm", "python")
    if run_vm not in VMS:
        fail('run.vm must be "python" or "rust"')

    dependencies = data.get("dependencies", {})
    if not isinstance(dependencies, dict):
        fail("dependencies must be a table")
    parsed_dependencies: dict[str, Dependency] = {}
    for dep_name in sorted(dependencies):
        try:
            parsed_dependencies[dep_name] = parse_dependency(dep_name, dependencies[dep_name])
        except ValueError as e:
            fail(str(e))

    types_table = data.get("types", {})
    if not isinstance(types_table, dict):
        fail("types must be a table, written [types]")
    for key in types_table:
        if key not in _TYPES_KEYS:
            fail(f"unknown key 'types.{key}'")
    type_check = types_table.get("check", typecheck.DEFAULT_CHECK_LEVEL)
    if type_check not in typecheck.CHECK_LEVELS:
        fail('types.check must be "loose", "strict" or "explicit"')

    return Project(
        root=root,
        manifest_path=manifest_path,
        name=name,
        version=version,
        entry=entry_path,
        targets=targets,
        run_vm=run_vm,
        dependencies=parsed_dependencies,
        type_check=type_check,
        lib=lib_path,
    )


def check_level_for(path: str | None) -> str:
    """The `[types] check` level that applies to a source file at `path`:
    the manifest found by searching upward from `path`'s directory, or
    `typecheck.DEFAULT_CHECK_LEVEL` ("loose") when `path` is `None`, no
    manifest is found, or the manifest found is invalid. Files run outside
    a project are always loose -- see docs/TYPES.md's Strictness section."""
    if path is None:
        return typecheck.DEFAULT_CHECK_LEVEL
    manifest_path = find_manifest(os.path.dirname(os.path.abspath(path)))
    if manifest_path is None:
        return typecheck.DEFAULT_CHECK_LEVEL
    try:
        project = load_project(manifest_path)
    except MahProjectError:
        return typecheck.DEFAULT_CHECK_LEVEL
    return project.type_check
