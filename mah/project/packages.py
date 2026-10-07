"""M43 (docs/PACKAGES.md, docs/contracts/M43_packages.md): packages from
GitHub repositories -- the lock file `mah-lock.toml`, the installed state
`.mah/installed.toml`, the content hash, and `PackageContext`, which
resolves `pkg:` imports for the preprocessor and the LSP.

Fetching (`fetch.py`) and `mah install` (`install.py`) build on this; the
compiler only ever reads files here, never fetches anything."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass

from .manifest import MANIFEST_NAME, Dependency, MahProjectError, find_manifest, load_project, parse_dependency
from .package_paths import NAME_RE, PKG_PREFIX, package_of_path

LOCK_NAME = "mah-lock.toml"
MAH_DIR = ".mah"
PACKAGES_DIR = "packages"
INSTALLED_NAME = "installed.toml"
LOCK_VERSION = 1

LOCK_HEADER = "# mah-lock.toml -- written by `mah install`. Commit it; don't edit it by hand."
INSTALLED_HEADER = "# written by `mah install`; delete .mah/ to reinstall everything"

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")


class PackageError(Exception):
    """A user-facing package problem; the message is printed as-is."""


@dataclass(frozen=True)
class LockedPackage:
    dep: Dependency
    commit: str
    hash: str
    dependencies: tuple[str, ...]


def packages_dir(root: str) -> str:
    return os.path.join(root, MAH_DIR, PACKAGES_DIR)


def package_root(root: str, name: str) -> str:
    return os.path.join(root, MAH_DIR, PACKAGES_DIR, name)


# --------------------------------------------------------------------------
# Writing the lock and the installed state
# --------------------------------------------------------------------------


def _q(text: str) -> str:
    # json.dumps gives a valid TOML basic string (non-ASCII as \uXXXX).
    return json.dumps(text)


def _entries_text(header: str, packages, with_dependencies: bool) -> str:
    items = packages.values() if isinstance(packages, dict) else packages
    blocks = [f"{header}\nversion = {LOCK_VERSION}\n"]
    for pkg in sorted(items, key=lambda p: p.dep.name):
        dep = pkg.dep
        lines = ["[[package]]", f"name = {_q(dep.name)}", f"github = {_q(dep.github)}"]
        if dep.ref_kind != "default":
            lines.append(f"{dep.ref_kind} = {_q(dep.ref)}")
        if dep.path:
            lines.append(f"path = {_q(dep.path)}")
        lines.append(f"commit = {_q(pkg.commit)}")
        lines.append(f"hash = {_q(pkg.hash)}")
        if with_dependencies:
            lines.append("dependencies = [" + ", ".join(_q(d) for d in pkg.dependencies) + "]")
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


def lock_text(packages) -> str:
    """The exact text of `mah-lock.toml` for `packages` (a dict name ->
    LockedPackage, or an iterable of them)."""
    return _entries_text(LOCK_HEADER, packages, with_dependencies=True)


def installed_text(packages) -> str:
    return _entries_text(INSTALLED_HEADER, packages, with_dependencies=False)


def _write_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_lock(root: str, packages) -> str:
    """Write `mah-lock.toml` atomically; returns its path."""
    path = os.path.join(root, LOCK_NAME)
    _write_atomic(path, lock_text(packages))
    return path


def write_installed(root: str, packages) -> str:
    path = os.path.join(root, MAH_DIR, INSTALLED_NAME)
    _write_atomic(path, installed_text(packages))
    return path


# --------------------------------------------------------------------------
# Reading them
# --------------------------------------------------------------------------


class _Invalid(Exception):
    pass


def _parse_entries(data: dict, with_dependencies: bool) -> dict:
    if "version" not in data:
        raise _Invalid("missing version")
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _Invalid("bad version")
    if version > LOCK_VERSION:
        raise PackageError(f"mah-lock.toml was written by a newer mah (lock version {version}); upgrade mah")
    raw_packages = data.get("package", [])
    if not isinstance(raw_packages, list):
        raise _Invalid("bad package")
    result: dict[str, LockedPackage] = {}
    allowed = {"name", "github", "tag", "branch", "rev", "path", "commit", "hash"}
    if with_dependencies:
        allowed.add("dependencies")
    for index, raw in enumerate(raw_packages):
        if not isinstance(raw, dict):
            raise _Invalid(f"package #{index + 1}: bad package")
        name = raw.get("name")
        if name is None:
            raise _Invalid(f"package #{index + 1}: missing name")
        if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
            raise _Invalid(f"package #{index + 1}: bad name")
        label = f"package {name}"
        for key in raw:
            if key not in allowed:
                raise _Invalid(f"{label}: bad {key}")
        if "github" not in raw:
            raise _Invalid(f"{label}: missing github")
        try:
            parse_dependency(name, {"github": raw["github"]})
        except ValueError:
            raise _Invalid(f"{label}: bad github") from None
        spec = {"github": raw["github"]}
        for key in ("tag", "branch", "rev", "path"):
            if key not in raw:
                continue
            try:
                parse_dependency(name, {"github": "a/b", key: raw[key]})
            except ValueError:
                raise _Invalid(f"{label}: bad {key}") from None
            spec[key] = raw[key]
        try:
            dep = parse_dependency(name, spec)
        except ValueError:
            raise _Invalid(f"{label}: bad {[k for k in ('tag', 'branch', 'rev') if k in raw][-1]}") from None
        if dep.path != spec.get("path", ""):
            raise _Invalid(f"{label}: bad path")
        for key, pattern in (("commit", _COMMIT_RE), ("hash", _HASH_RE)):
            if key not in raw:
                raise _Invalid(f"{label}: missing {key}")
            if not isinstance(raw[key], str) or pattern.fullmatch(raw[key]) is None:
                raise _Invalid(f"{label}: bad {key}")
        deps: tuple = ()
        if with_dependencies:
            if "dependencies" not in raw:
                raise _Invalid(f"{label}: missing dependencies")
            raw_deps = raw["dependencies"]
            if not isinstance(raw_deps, list) or not all(
                isinstance(d, str) and NAME_RE.fullmatch(d) for d in raw_deps
            ):
                raise _Invalid(f"{label}: bad dependencies")
            deps = tuple(raw_deps)
        if name in result:
            raise _Invalid(f"duplicate package '{name}'")
        result[name] = LockedPackage(dep=dep, commit=raw["commit"], hash=raw["hash"], dependencies=deps)
    return dict(sorted(result.items()))


def _invalid_lock(detail: str) -> PackageError:
    return PackageError(f"mah-lock.toml is invalid ({detail}); delete it and run `mah install`")


def read_lock(root: str) -> dict | None:
    """`mah-lock.toml` as name -> LockedPackage (sorted), or None when there
    is no lock. Raises `PackageError` for an invalid or newer lock."""
    path = os.path.join(root, LOCK_NAME)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise _invalid_lock(str(e)) from None
    except tomllib.TOMLDecodeError as e:
        raise _invalid_lock(str(e)) from None
    try:
        return _parse_entries(data, with_dependencies=True)
    except _Invalid as e:
        raise _invalid_lock(str(e)) from None


def read_installed(root: str) -> dict:
    """`.mah/installed.toml` as name -> LockedPackage (no dependencies);
    `{}` when it's missing or invalid ("nothing installed")."""
    path = os.path.join(root, MAH_DIR, INSTALLED_NAME)
    try:
        with open(path, "rb") as f:
            data = tomllib.loads(f.read().decode("utf-8"))
        return _parse_entries(data, with_dependencies=False)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, _Invalid, PackageError):
        return {}


def lock_out_of_date(deps: dict, lock: dict) -> str | None:
    """Why `lock` doesn't match the root's `[dependencies]`, or None."""
    for name in sorted(deps):
        locked = lock.get(name)
        if locked is None:
            return f"'{name}' was added"
        if locked.dep.spec() != deps[name].spec():
            return f"'{name}' was changed"
    return None


def same_install(a: LockedPackage, b: LockedPackage) -> bool:
    return a.dep.spec() == b.dep.spec() and a.commit == b.commit and a.hash == b.hash


# --------------------------------------------------------------------------
# The content hash
# --------------------------------------------------------------------------


def hash_files(entries) -> str:
    """entries: iterable of (rel: str with '/', data: bytes)."""
    h = hashlib.sha256()
    for rel, data in sorted(entries, key=lambda e: e[0].encode("utf-8")):
        h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return "sha256:" + h.hexdigest()


def tree_files(directory: str):
    """Every regular file under `directory` (no symlinks; symlinked
    directories aren't followed) as (rel with '/', bytes)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames.sort()
        for fname in sorted(filenames):
            full = os.path.join(dirpath, fname)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, directory).replace(os.sep, "/")
            with open(full, "rb") as f:
                out.append((rel, f.read()))
    return out


def hash_tree(directory: str) -> str:
    return hash_files(tree_files(directory))


# --------------------------------------------------------------------------
# A package's own manifest
# --------------------------------------------------------------------------


def _strip_manifest_prefix(message: str, manifest_path: str) -> str:
    prefix = f"{manifest_path}: "
    return message[len(prefix):] if message.startswith(prefix) else message


def load_package_manifest(pkg_root: str, name: str):
    """The package's `Project`, or None without a manifest; raises
    `PackageError` for an invalid one."""
    manifest_path = os.path.join(pkg_root, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        return None
    try:
        return load_project(manifest_path)
    except MahProjectError as e:
        raise PackageError(
            f"package '{name}' has an invalid mah-project.toml: {_strip_manifest_prefix(str(e), manifest_path)}"
        ) from None


def library_entry(pkg_root: str, name: str | None = None) -> str:
    """The package's library file: its manifest's `[package] lib`, or
    `lib.mh` when the package root has no manifest."""
    if name is None:
        name = os.path.basename(os.path.normpath(pkg_root))
    project = load_package_manifest(pkg_root, name)
    if project is None:
        return os.path.join(pkg_root, "lib.mh")
    return project.lib


def package_dependencies(pkg_root: str, name: str) -> list:
    """The package's own `[dependencies]` (sorted), [] without a manifest."""
    project = load_package_manifest(pkg_root, name)
    if project is None:
        return []
    return [project.dependencies[n] for n in sorted(project.dependencies)]


# --------------------------------------------------------------------------
# Resolving `pkg:` imports
# --------------------------------------------------------------------------


def parse_literal(literal: str):
    """`(name, rel_or_None)` for a valid `pkg:` literal, else None."""
    if not literal.startswith(PKG_PREFIX):
        return None
    body = literal[len(PKG_PREFIX):]
    name, sep, rel = body.partition("/")
    if NAME_RE.fullmatch(name) is None:
        return None
    if not sep:
        return name, None
    if "\\" in rel or any(seg in ("", ".", "..") for seg in rel.split("/")):
        return None
    return name, rel


_UNSET = object()


class PackageContext:
    """`pkg:` resolution for one preprocess call (docs/PACKAGES.md 4.2/4.3).
    `root` is None when no project was found; everything else is loaded
    lazily and cached."""

    def __init__(self, root: str | None):
        self.root = root
        self._project = _UNSET
        self._project_error = None
        self._lock = _UNSET
        self._lock_error = None
        self._installed = None

    @classmethod
    def find(cls, start: str):
        found = package_of_path(start)
        if found is not None and os.path.isfile(os.path.join(found[0], MANIFEST_NAME)):
            return cls(found[0])
        manifest = find_manifest(os.path.dirname(start))
        if manifest is None:
            return None
        return cls(os.path.dirname(manifest))

    # -- lazily loaded state ------------------------------------------------
    def project(self):
        """The root project; raises `PackageError` for an invalid manifest."""
        if self._project is _UNSET:
            try:
                self._project = load_project(os.path.join(self.root, MANIFEST_NAME))
            except MahProjectError as e:
                self._project = None
                self._project_error = str(e)
        if self._project is None:
            raise PackageError(self._project_error)
        return self._project

    def lock(self):
        if self._lock is _UNSET:
            try:
                self._lock = read_lock(self.root)
            except PackageError as e:
                self._lock = None
                self._lock_error = str(e)
        if self._lock_error is not None:
            raise PackageError(self._lock_error)
        return self._lock

    def installed(self) -> dict:
        if self._installed is None:
            self._installed = read_installed(self.root)
        return self._installed

    def importer_package(self, importer: str) -> str | None:
        found = package_of_path(importer)
        if found is None or self.root is None:
            return None
        if os.path.normcase(os.path.abspath(found[0])) != os.path.normcase(os.path.abspath(self.root)):
            return None
        return found[1]

    def importable_names(self, importer: str) -> dict:
        """name -> Dependency of what `importer` may import as `pkg:NAME`
        (for completion). Raises `PackageError` when that can't be known."""
        project = self.project()
        pkg = self.importer_package(importer)
        if pkg is None:
            return dict(project.dependencies)
        lock = self.lock() or {}
        names = {}
        own = lock.get(pkg)
        if own is not None:
            names[pkg] = own.dep
            for dep_name in own.dependencies:
                if dep_name in lock:
                    names[dep_name] = lock[dep_name].dep
        return dict(sorted(names.items()))

    # -- resolution ---------------------------------------------------------
    def resolve(self, importer_path: str, literal: str):
        """`(absolute path, None)` or `(None, message)`."""
        try:
            return self._resolve(importer_path, literal), None
        except PackageError as e:
            return None, str(e)

    def _resolve(self, importer: str, literal: str) -> str:
        parsed = parse_literal(literal)
        if parsed is None:
            raise PackageError(
                f"invalid package import '{literal}': write \"pkg:NAME\" or \"pkg:NAME/path/to/file.mh\""
            )
        name, rel = parsed
        if self.root is None:
            label = "<buffer>" if importer.startswith("<") or os.path.basename(importer) == "<buffer>" else os.path.basename(importer)
            raise PackageError(f"package imports need a project, but no mah-project.toml was found for '{label}'")
        project = self.project()
        importer_pkg = self.importer_package(importer)

        if importer_pkg is None and name not in project.dependencies:
            message = f"'{name}' isn't in the [dependencies] of mah-project.toml"
            try:
                lock = self.lock()
            except PackageError:
                lock = None
            if lock:
                users = sorted(p for p, locked in lock.items() if name in locked.dependencies)
                if users:
                    message += f" (it's installed only for '{users[0]}')"
            raise PackageError(message)

        lock = self.lock()
        if lock is None:
            if project.dependencies:
                raise PackageError(
                    "mah-project.toml has [dependencies] but there's no mah-lock.toml; run `mah install`"
                )
            lock = {}
        reason = lock_out_of_date(project.dependencies, lock)
        if reason is not None:
            raise PackageError(f"mah-lock.toml is out of date with mah-project.toml ({reason}); run `mah install`")

        if importer_pkg is not None and importer_pkg != name:
            own = lock.get(importer_pkg)
            if own is None or name not in own.dependencies:
                raise PackageError(
                    f"package '{importer_pkg}' imports '{PKG_PREFIX}{name}', but '{name}' isn't in its [dependencies]"
                )

        pkg_root = package_root(self.root, name)
        entry = self.installed().get(name)
        locked = lock.get(name)
        if not os.path.isdir(pkg_root) or entry is None or locked is None:
            raise PackageError(f"package '{name}' isn't installed; run `mah install`")
        if not same_install(entry, locked):
            raise PackageError(f"package '{name}' in .mah/packages doesn't match mah-lock.toml; run `mah install`")

        if rel is None:
            target = library_entry(pkg_root, name)
            if not os.path.isfile(target):
                shown = os.path.relpath(target, pkg_root).replace(os.sep, "/")
                raise PackageError(
                    f"package '{name}' has no library file {shown}; import one of its files as "
                    f"\"pkg:{name}/path/to/file.mh\""
                )
            return os.path.abspath(target)
        candidate = os.path.join(pkg_root, *rel.split("/"))
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
        if not rel.endswith(".mh"):
            if os.path.isfile(candidate + ".mh"):
                return os.path.abspath(candidate + ".mh")
            rel = rel + ".mh"
        raise PackageError(f"package '{name}' has no file '{rel}'")
