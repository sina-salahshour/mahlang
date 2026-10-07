"""M43 (docs/PACKAGES.md): fetching packages from GitHub with the `git`
CLI. Files are read from git objects (`ls-tree` + `cat-file --batch`), never
from a checkout, so `.gitattributes` and `core.autocrlf` can't change the
installed bytes and the content hash is the same on every machine."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess

from .manifest import Dependency
from .packages import PackageError


class GitError(Exception):
    """A git command failed; the message is git's first stderr line."""


def repo_url(github: str) -> str:
    base = os.environ.get("MAH_GITHUB_URL_BASE") or "https://github.com"
    return base.rstrip("/") + "/" + github + ".git"


def git_env(url: str) -> dict:
    """The environment for git: never prompt, and send `GITHUB_TOKEN` (only
    to https://github.com/) through git's environment config, never on the
    command line."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    token = env.get("GITHUB_TOKEN")
    if token and url.startswith("https://github.com/"):
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        credentials = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        env[f"GIT_CONFIG_KEY_{n}"] = "http.extraHeader"
        env[f"GIT_CONFIG_VALUE_{n}"] = "Authorization: Basic " + credentials
        env["GIT_CONFIG_COUNT"] = str(n + 1)
    return env


def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        raise PackageError("mah install needs git to fetch packages, and git wasn't found on PATH")
    return git


def _detail(stderr: bytes) -> str:
    for line in stderr.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            return line[len("fatal: "):] if line.startswith("fatal: ") else line
    return "git failed"


def _run_git(url: str, args: list, stdin_data: bytes | None = None) -> bytes:
    git = _git_executable()
    command = [git, "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60", *args]
    if stdin_data is None:
        result = subprocess.run(command, env=git_env(url), stdin=subprocess.DEVNULL, capture_output=True)
    else:
        result = subprocess.run(command, env=git_env(url), input=stdin_data, capture_output=True)
    if result.returncode != 0:
        raise GitError(_detail(result.stderr))
    return result.stdout


def resolve_commit(dep: Dependency) -> str:
    """The commit `dep`'s tag, branch or default branch points to now."""
    if dep.ref_kind == "rev":
        return dep.ref
    url = repo_url(dep.github)
    if dep.ref_kind == "tag":
        patterns = [f"refs/tags/{dep.ref}", f"refs/tags/{dep.ref}^{{}}"]
    elif dep.ref_kind == "branch":
        patterns = [f"refs/heads/{dep.ref}"]
    else:
        patterns = ["HEAD"]
    try:
        out = _run_git(url, ["ls-remote", url, *patterns])
    except GitError as e:
        raise PackageError(f"couldn't reach {dep.github} ({url}): {e}") from None
    refs = {}
    for line in out.decode("utf-8", "replace").splitlines():
        sha, sep, ref = line.partition("\t")
        if sep:
            refs[ref.strip()] = sha.strip()
    if dep.ref_kind == "tag":
        commit = refs.get(f"refs/tags/{dep.ref}^{{}}") or refs.get(f"refs/tags/{dep.ref}")
        if commit is None:
            raise PackageError(f"tag '{dep.ref}' not found in {dep.github}")
    elif dep.ref_kind == "branch":
        commit = refs.get(f"refs/heads/{dep.ref}")
        if commit is None:
            raise PackageError(f"branch '{dep.ref}' not found in {dep.github}")
    else:
        commit = refs.get("HEAD")
        if commit is None:
            raise PackageError(f"{dep.github} has no default branch")
    return commit.lower()


def fetch_files(dep: Dependency, commit: str, scratch_dir: str):
    """`(files, skipped)`: the regular files under `dep.path` at `commit` as
    `[(rel, bytes)]` in ls-tree order, and the relative paths of symlinks
    and submodules that were skipped."""
    url = repo_url(dep.github)
    os.makedirs(scratch_dir, exist_ok=True)
    repo = os.path.join(scratch_dir, "repo.git")
    if os.path.exists(repo):
        shutil.rmtree(repo)
    c7 = commit[:7]
    try:
        _run_git(url, ["init", "--bare", "-q", repo])
    except GitError as e:
        raise PackageError(f"couldn't fetch {dep.github} ({url}): {e}") from None
    try:
        _run_git(url, ["-C", repo, "fetch", "-q", "--no-tags", "--depth=1", url, commit])
    except GitError as e:
        detail = str(e)
        if "not our ref" in detail or "couldn't find remote ref" in detail:
            raise PackageError(f"commit {c7} not found in {dep.github}") from None
        raise PackageError(f"couldn't fetch {dep.github} ({url}): {detail}") from None
    try:
        kind = _run_git(url, ["-C", repo, "cat-file", "-t", commit]).decode().strip()
    except GitError:
        kind = ""
    if kind != "commit":
        raise PackageError(f"{dep.describe()} doesn't point to a commit")
    listing = _run_git(url, ["-C", repo, "ls-tree", "-r", "-z", "--full-tree", commit])

    prefix = dep.path + "/" if dep.path else ""
    blobs = []  # (rel, oid)
    skipped = []
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        meta, _tab, raw_path = raw.partition(b"\t")
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            raise PackageError(f"{dep.github} has a file name that isn't UTF-8") from None
        if prefix:
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
        else:
            rel = path
        if ".mah" in rel.split("/"):
            continue
        mode, otype, oid = meta.decode("ascii").split(" ")
        if mode in ("100644", "100755") and otype == "blob":
            blobs.append((rel, oid))
        elif mode in ("120000", "160000"):
            skipped.append(rel)
    if not blobs:
        if not dep.path:
            raise PackageError(f"{dep.describe()} has no files")
        raise PackageError(f"'{dep.path}' isn't a directory in {dep.github} at {c7}")

    stdin_data = ("\n".join(oid for _rel, oid in blobs) + "\n").encode("ascii")
    out = _run_git(url, ["-C", repo, "cat-file", "--batch"], stdin_data=stdin_data)
    files = []
    pos = 0
    for rel, oid in blobs:
        newline = out.index(b"\n", pos)
        header = out[pos:newline].decode("ascii").split(" ")
        if len(header) != 3 or header[1] != "blob":
            raise PackageError(f"couldn't read {rel} from {dep.github} at {c7}")
        size = int(header[2])
        start = newline + 1
        files.append((rel, out[start:start + size]))
        pos = start + size + 1
    return files, skipped
