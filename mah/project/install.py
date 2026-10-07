"""M43 (docs/PACKAGES.md): `mah install` -- fetch the project's
`[dependencies]` (and theirs) into `.mah/packages/`, and pin them in
`mah-lock.toml`."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import replace

from .fetch import GitError, fetch_files, resolve_commit
from .manifest import MANIFEST_NAME, MahProjectError, find_manifest, load_project
from .packages import (
    LOCK_NAME,
    MAH_DIR,
    INSTALLED_NAME,
    LockedPackage,
    PackageError,
    hash_files,
    hash_tree,
    lock_out_of_date,
    lock_text,
    package_dependencies,
    package_root,
    packages_dir,
    read_installed,
    read_lock,
    write_installed,
    write_lock,
)


class _Failure(Exception):
    """An install error, already formatted (without `error: `)."""


def _bracket(names) -> str:
    return "[" + ", ".join(names) + "]"


def find_project_root(directory: str | None, err) -> tuple:
    """`(project, None)` or `(None, exit code)` with the error printed; the
    messages are `_resolve_project`'s, but the entry file needn't exist."""
    if directory is None:
        manifest_path = find_manifest(os.getcwd())
        if manifest_path is None:
            print(f"error: no FILE given and no {MANIFEST_NAME} found in {os.getcwd()} or its parents", file=err)
            return None, 2
    else:
        manifest_path = os.path.join(directory, MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            print(f"error: {directory} has no {MANIFEST_NAME}", file=err)
            return None, 2
    try:
        return load_project(os.path.abspath(manifest_path)), None
    except MahProjectError as e:
        print(f"error: {e}", file=err)
        return None, 2


class _Installer:
    def __init__(self, root: str, installed: dict, out):
        self.root = root
        self.installed = installed
        self.out = out
        self.replaced_any = False

    def ensure_installed(self, dep, commit: str, expected: str | None) -> str:
        directory = package_root(self.root, dep.name)
        state = self.installed.get(dep.name)
        describe = f"{dep.name} {commit[:7]} ({dep.describe()})"
        changed = False
        if os.path.isdir(directory):
            h = hash_tree(directory)
            if expected is not None and h == expected:
                print(f"kept {describe}", file=self.out)
                return h
            same_state = state is not None and state.commit == commit and state.dep.spec() == dep.spec()
            if expected is None and same_state and h == state.hash:
                print(f"kept {describe}", file=self.out)
                return h
            changed = same_state and h != state.hash
        scratch = os.path.join(self.root, MAH_DIR, "tmp", dep.name)
        if os.path.exists(scratch):
            shutil.rmtree(scratch)
        files, skipped = fetch_files(dep, commit, scratch)
        h = hash_files(files)
        if expected is not None and h != expected:
            raise PackageError(
                f"content hash mismatch at {commit[:7]}: mah-lock.toml has {expected}, the fetched files "
                f"hash to {h}; if the repository's history was rewritten, run `mah install --update {dep.name}`"
            )
        if not self.replaced_any:
            self.replaced_any = True
            installed_path = os.path.join(self.root, MAH_DIR, INSTALLED_NAME)
            if os.path.exists(installed_path):
                os.remove(installed_path)
        tree = os.path.join(scratch, "tree")
        for rel, data in files:
            target = os.path.join(tree, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
        os.makedirs(tree, exist_ok=True)
        if os.path.isdir(directory) and not os.path.islink(directory):
            shutil.rmtree(directory)
        elif os.path.lexists(directory):
            os.remove(directory)
        os.replace(tree, directory)
        for rel in skipped:
            print(f"note: {dep.name}: skipped {rel} (only regular files are installed)", file=self.out)
        if changed:
            print(f"reinstalled {describe}: its files had been changed", file=self.out)
        else:
            print(f"installed {describe}", file=self.out)
        return h


def _sub_dependencies(root: str, name: str) -> list:
    return package_dependencies(package_root(root, name), name)


def install(root: str, frozen: bool = False, update=None, out=None, err=None) -> int:
    """`mah install` for the project at `root`; returns the exit code."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        project = load_project(os.path.join(root, MANIFEST_NAME))
    except MahProjectError as e:
        print(f"error: {e}", file=err)
        return 2
    root = project.root
    try:
        lock = read_lock(root)
    except PackageError as e:
        print(f"error: {e}", file=err)
        return 1
    installed = read_installed(root)
    deps = project.dependencies

    if not deps and lock is None:
        print("no dependencies to install", file=out)
        return 0

    if frozen:
        if lock is None:
            print("error: --frozen: there's no mah-lock.toml; run `mah install` first", file=err)
            return 1
        reason = lock_out_of_date(deps, lock)
        if reason is not None:
            print(f"error: --frozen: mah-lock.toml is out of date with mah-project.toml ({reason})", file=err)
            return 1

    if update:
        for name in update:
            if name not in deps and not (lock and name in lock):
                print(f"error: --update: no package named '{name}'", file=err)
                return 2

    mah_dir = os.path.join(root, MAH_DIR)
    os.makedirs(packages_dir(root), exist_ok=True)
    gitignore = os.path.join(mah_dir, ".gitignore")
    if not os.path.exists(gitignore):
        with open(gitignore, "w", encoding="utf-8", newline="\n") as f:
            f.write("*\n")

    installer = _Installer(root, installed, out)
    current = None
    try:
        try:
            chosen: dict = {}
            if frozen:
                for name in sorted(lock):
                    current = name
                    locked = lock[name]
                    h = installer.ensure_installed(locked.dep, locked.commit, locked.hash)
                    chosen[name] = replace(locked, hash=h)
                current = None
                for name in sorted(lock):
                    current = name
                    names = sorted(d.name for d in _sub_dependencies(root, name))
                    current = None
                    if tuple(names) != tuple(lock[name].dependencies):
                        raise _Failure(
                            f"--frozen: mah-lock.toml is out of date (package '{name}' now depends on {_bracket(names)})"
                        )
            else:
                requested: dict = {}
                queue = sorted(deps)
                noted: set = set()
                while queue:
                    name = queue.pop(0)
                    if name in chosen:
                        continue
                    current = name
                    dep = deps[name] if name in deps else requested[name][0]
                    updating = update is not None and (update == [] or name in update)
                    locked = lock.get(name) if lock and not updating else None
                    if locked is not None and locked.dep.spec() == dep.spec():
                        commit, expected = locked.commit, locked.hash
                    else:
                        commit, expected = resolve_commit(dep), None
                    h = installer.ensure_installed(dep, commit, expected)
                    sub = _sub_dependencies(root, name)
                    current = None
                    for s in sub:
                        if s.name in deps:
                            if s.spec() != deps[s.name].spec() and (name, s.name) not in noted:
                                print(
                                    f"note: '{name}' asks for '{s.name}' as {s.describe()}, "
                                    f"but mah-project.toml chooses {deps[s.name].describe()}",
                                    file=out,
                                )
                                noted.add((name, s.name))
                        elif s.name in requested:
                            first, requester = requested[s.name]
                            if s.spec() != first.spec():
                                raise _Failure(
                                    f"packages '{requester}' and '{name}' both depend on '{s.name}' but ask for "
                                    f"different versions: {first.describe()} vs {s.describe()}; add '{s.name}' "
                                    f"to your [dependencies] to choose one"
                                )
                        else:
                            requested[s.name] = (s, name)
                            queue.append(s.name)
                    chosen[name] = LockedPackage(
                        dep=replace(dep, name=name), commit=commit, hash=h, dependencies=tuple(s.name for s in sub)
                    )

            current = None
            pkgs = packages_dir(root)
            for entry in sorted(os.listdir(pkgs)):
                if entry in chosen:
                    continue
                full = os.path.join(pkgs, entry)
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
                print(f"removed {entry}", file=out)

            write_installed(root, chosen)

            if not frozen:
                text = lock_text(chosen)
                lock_path = os.path.join(root, LOCK_NAME)
                try:
                    with open(lock_path, encoding="utf-8", newline="") as f:
                        existing = f.read()
                except OSError:
                    existing = None
                if existing == text:
                    print("mah-lock.toml is up to date", file=out)
                else:
                    write_lock(root, chosen)
                    print("wrote mah-lock.toml", file=out)
        except (PackageError, GitError) as e:
            prefix = f"package '{current}': " if current is not None else ""
            print(f"error: {prefix}{e}", file=err)
            return 1
        except _Failure as e:
            print(f"error: {e}", file=err)
            return 1
    finally:
        tmp = os.path.join(mah_dir, "tmp")
        if os.path.exists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    return 0
