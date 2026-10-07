"""M43: packages from GitHub repositories (docs/PACKAGES.md,
docs/contracts/M43_packages.md section 13). Everything here runs without
network: `mah install` is tested with real git against local bare
repositories served over `file://` (`FakeGitHub`), and the compile-time
tests install packages by hand (`fake_install`)."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.cli.main import main as cli_main  # noqa: E402
from mah.compiler.driver import compile_to_bytes  # noqa: E402
from mah.project.fetch import git_env  # noqa: E402
from mah.project.manifest import Dependency, MahProjectError, load_project  # noqa: E402
from mah.project.package_paths import package_label, package_of_path  # noqa: E402
from mah.project.packages import (  # noqa: E402
    LockedPackage,
    PackageError,
    hash_files,
    hash_tree,
    lock_out_of_date,
    lock_text,
    read_lock,
    write_installed,
    write_lock,
)
from tests.support import _run_capturing  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HAS_GIT = shutil.which("git") is not None
VM = "rust" if os.environ.get("MAH_TEST_VM") == "rust" else "python"


def _rust_vm_available() -> bool:
    from mah.rust_vm import RustVmNotFound, find_vm

    try:
        find_vm()
        return True
    except RustVmNotFound:
        return False


_SKIP_RUST = VM == "rust" and not _rust_vm_available()


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _mah(args, cwd, env=None):
    """Run the real CLI in a subprocess (so a Rust-VM run's output is
    captured too); returns `(rc, stdout, stderr)`."""
    full_env = dict(os.environ if env is None else env)
    full_env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + full_env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "mah", *args], cwd=cwd, env=full_env, capture_output=True, text=True
    )
    return result.returncode, result.stdout, result.stderr


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode, **({} if isinstance(content, bytes) else {"encoding": "utf-8", "newline": "\n"})) as f:
        f.write(content)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


GREET_FILES = {
    "mah-project.toml": '[package]\nname = "greet"\nversion = "1.0.0"\n',
    "src/lib.mh": 'export fn hello(name) { return "hello " + name }\n',
    "src/extra.mh": "export let answer = 42\n",
    "src/lib.test.mh": 'import "lib.mh"\nimport "std:test"\n\ntest "hello" {\n    assert_eq(hello("x"), "hello x")\n}\n',
    "README.md": "# greet\n",
}

GREET_DEP = Dependency(name="greet", github="acme/greet", ref_kind="tag", ref="v1", path="")


def project(tmp, deps_toml, main_src, extra_manifest=""):
    """A project in `tmp/app` with `[dependencies]` = `deps_toml`."""
    root = os.path.join(tmp, "app")
    _write(
        os.path.join(root, "mah-project.toml"),
        f'[package]\nname = "app"\nversion = "0.1.0"\n{extra_manifest}\n[dependencies]\n{deps_toml}',
    )
    if main_src is not None:
        _write(os.path.join(root, "src", "main.mh"), main_src)
    return root


def fake_install(root, name, files, dep=None, deps=(), lock_entries=None):
    """Install `files` as package `name` without git, recording it in the
    lock and the installed state (`commit = "a"*40`)."""
    dep = dep or Dependency(name=name, github=f"acme/{name}", ref_kind="tag", ref="v1", path="")
    pkg_root = os.path.join(root, ".mah", "packages", name)
    for rel, content in files.items():
        _write(os.path.join(pkg_root, *rel.split("/")), content)
    entries = [(rel, c if isinstance(c, bytes) else c.encode("utf-8")) for rel, c in files.items()]
    locked = LockedPackage(dep=dep, commit="a" * 40, hash=hash_files(entries), dependencies=tuple(deps))
    try:
        lock = read_lock(root) or {}
    except PackageError:
        lock = {}
    lock[name] = locked
    write_lock(root, lock)
    os.makedirs(os.path.join(root, ".mah"), exist_ok=True)
    write_installed(root, lock)
    return locked


GREET_TOML = 'greet = { github = "acme/greet", tag = "v1" }\n'


class FakeGitHub:
    """Bare repositories at `tmp/gh/<owner>/<repo>.git`, each with a work
    directory (its `origin`) used to make commits."""

    GIT_CONFIG = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]

    def __init__(self, tmpdir):
        self.gh = os.path.join(tmpdir, "gh")
        self.work = os.path.join(tmpdir, "gh-work")
        os.makedirs(self.gh, exist_ok=True)

    def git(self, cwd, *args):
        env = dict(os.environ)
        env.pop("GIT_CONFIG_COUNT", None)
        result = subprocess.run(
            ["git", *self.GIT_CONFIG, *args], cwd=cwd, env=env, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise AssertionError(f"git {args} failed: {result.stderr}")
        return result.stdout.strip()

    def _work(self, owner, repo):
        return os.path.join(self.work, owner, repo)

    def _write_files(self, work, files):
        for rel, content in files.items():
            _write(os.path.join(work, *rel.split("/")), content)

    def make_repo(self, owner, repo, files):
        work = self._work(owner, repo)
        os.makedirs(work)
        self.git(work, "init", "-q", "-b", "main")
        self._write_files(work, files)
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", "first")
        bare = os.path.join(self.gh, owner, repo + ".git")
        os.makedirs(os.path.dirname(bare), exist_ok=True)
        self.git(self.work, "clone", "-q", "--bare", work, bare)
        self.git(work, "remote", "add", "origin", bare)
        self.git(work, "fetch", "-q", "origin")
        return self.git(work, "rev-parse", "HEAD")

    def commit(self, owner, repo, files, remove=()):
        work = self._work(owner, repo)
        self._write_files(work, files)
        for rel in remove:
            os.remove(os.path.join(work, *rel.split("/")))
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", "more")
        self.git(work, "push", "-q", "origin", "main")
        return self.git(work, "rev-parse", "HEAD")

    def tag(self, owner, repo, name, sha=None, annotated=False):
        work = self._work(owner, repo)
        args = ["tag"]
        if annotated:
            args += ["-a", "-m", name]
        args.append(name)
        if sha:
            args.append(sha)
        self.git(work, *args)
        self.git(work, "push", "-q", "origin", name)

    def branch(self, owner, repo, name, sha):
        work = self._work(owner, repo)
        self.git(work, "branch", name, sha)
        self.git(work, "push", "-q", "origin", name)

    def symlink(self, owner, repo, link, target):
        work = self._work(owner, repo)
        os.symlink(target, os.path.join(work, *link.split("/")))
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", "link")
        self.git(work, "push", "-q", "origin", "main")
        return self.git(work, "rev-parse", "HEAD")


class _EnvTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self._saved_env = dict(os.environ)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GIT_CONFIG_COUNT", None)
        self.gh = FakeGitHub(self.tmp)
        os.environ["MAH_GITHUB_URL_BASE"] = "file://" + self.gh.gh

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def install(self, root, *args):
        with _chdir(root):
            return _run_main(["install", *args])

    def run_project(self, root):
        return _mah(["run", "--vm", VM], cwd=root)


# ---------------------------------------------------------------------------
# 1-3: the manifest
# ---------------------------------------------------------------------------


class ManifestDependencyTests(unittest.TestCase):
    def _load(self, deps, extra=""):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "mah-project.toml")
            _write(path, f'[package]\nname = "x"\nversion = "1"\n{extra}\n[dependencies]\n{deps}')
            return load_project(path), path

    def _error(self, deps, message, extra=""):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "mah-project.toml")
            _write(path, f'[package]\nname = "x"\nversion = "1"\n{extra}\n[dependencies]\n{deps}')
            with self.assertRaises(MahProjectError) as cm:
                load_project(path)
            self.assertEqual(str(cm.exception), f"{path}: {message}")

    def test_four_forms(self):
        p, _ = self._load(
            'json5  = { github = "acme/mah-json5", tag = "v1.2.0" }\n'
            'utils  = { github = "acme/monorepo", branch = "main", path = "packages/utils" }\n'
            'pinned = { github = "acme/thing", rev = "0123456789ABCDEF0123456789abcdef01234567" }\n'
            'latest = { github = "acme/other" }\n'
            'lib = { github = "acme/lib", path = "lib/" }\n'
        )
        self.assertEqual(list(p.dependencies), ["json5", "latest", "lib", "pinned", "utils"])
        d = p.dependencies
        self.assertEqual(d["json5"], Dependency("json5", "acme/mah-json5", "tag", "v1.2.0", ""))
        self.assertEqual(d["utils"], Dependency("utils", "acme/monorepo", "branch", "main", "packages/utils"))
        self.assertEqual(d["pinned"].ref, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(d["pinned"].ref_kind, "rev")
        self.assertEqual(d["latest"], Dependency("latest", "acme/other", "default", None, ""))
        self.assertEqual(d["lib"].path, "lib")
        self.assertEqual(d["json5"].describe(), "acme/mah-json5 tag v1.2.0")
        self.assertEqual(d["utils"].describe(), "acme/monorepo branch main, path packages/utils")
        self.assertEqual(d["pinned"].describe(), "acme/thing rev 0123456")
        self.assertEqual(d["latest"].describe(), "acme/other default branch")
        self.assertEqual(d["json5"].spec(), ("acme/mah-json5", "tag", "v1.2.0", ""))

    def test_dependencies_not_a_table(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "mah-project.toml")
            _write(path, 'dependencies = 3\n\n[package]\nname = "x"\nversion = "1"\n')
            with self.assertRaises(MahProjectError) as cm:
                load_project(path)
            self.assertEqual(str(cm.exception), f"{path}: dependencies must be a table")

    def test_bad_name(self):
        self._error(
            'Foo = { github = "acme/greet" }\n',
            "dependency name 'Foo' must be lowercase letters, digits, '_' and '-', starting with a letter",
        )

    def test_not_a_table(self):
        self._error(
            'foo = "1.0"\n',
            'dependency \'foo\' must be a table, like foo = { github = "owner/repo", tag = "v1.0" }',
        )

    def test_unknown_key(self):
        self._error('foo = { github = "acme/greet", version = "1" }\n', "unknown key 'dependencies.foo.version'")

    def test_no_github(self):
        self._error('foo = { tag = "v1" }\n', "dependency 'foo' needs github = \"owner/repo\"")

    def test_github_dot_git(self):
        self._error(
            'foo = { github = "acme/greet.git" }\n', "dependency 'foo': write github = \"owner/repo\" without \".git\""
        )

    def test_github_bad(self):
        self._error('foo = { github = "acme" }\n', "dependency 'foo': github must be \"owner/repo\" (got \"acme\")")
        self._error("foo = { github = 3 }\n", "dependency 'foo': github must be \"owner/repo\" (got integer)")
        self._error('foo = { github = "acme/.." }\n', "dependency 'foo': github must be \"owner/repo\" (got \"acme/..\")")

    def test_two_refs(self):
        self._error(
            'foo = { github = "acme/greet", tag = "v1", branch = "main" }\n',
            "dependency 'foo': use only one of tag, branch and rev",
        )

    def test_bad_tag_branch(self):
        self._error('foo = { github = "acme/greet", tag = "" }\n', "dependency 'foo': tag must be a non-empty string")
        self._error('foo = { github = "acme/greet", branch = 1 }\n', "dependency 'foo': branch must be a non-empty string")

    def test_bad_rev(self):
        self._error(
            'foo = { github = "acme/greet", rev = "abc" }\n',
            "dependency 'foo': rev must be a full 40-character commit hash",
        )

    def test_bad_paths(self):
        for raw in ('""', '"/x"', '"a/../b"', '"a\\\\b"', '"./a"'):
            shown = raw
            self._error(
                f'foo = {{ github = "acme/greet", path = {raw} }}\n',
                f'dependency \'foo\': path must be a relative path inside the repository, like "lib" (got {shown})',
            )

    def test_lib(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "mah-project.toml")
            _write(path, '[package]\nname = "x"\nversion = "1"\nlib = "lib/main.mh"\n')
            self.assertEqual(load_project(path).lib, os.path.join(td, "lib/main.mh"))
            _write(path, '[package]\nname = "x"\nversion = "1"\n')
            self.assertEqual(load_project(path).lib, os.path.join(td, "src/lib.mh"))
            _write(path, '[package]\nname = "x"\nversion = "1"\nlib = 3\n')
            with self.assertRaises(MahProjectError) as cm:
                load_project(path)
            self.assertEqual(str(cm.exception), f"{path}: package.lib must be a string")


# ---------------------------------------------------------------------------
# 4-8: lock, hash, paths, git environment
# ---------------------------------------------------------------------------


class LockHashPathTests(unittest.TestCase):
    def test_pinned_hashes(self):
        self.assertEqual(
            hash_files([("src/lib.mh", b"export fn hi() { return 1 }\n"), ("README.md", b"hi\n")]),
            "sha256:24e49e36d4e16f5d6eb1a8f28697aa0218a8df1660577b347eeef9c8a43b6498",
        )
        self.assertEqual(
            hash_files([("a.mh", b"")]), "sha256:455e7441c84a31e91d1ec696f78618c91d0c1ebd2f3b5fcf6834590e72946675"
        )

    def test_hash_tree(self):
        with tempfile.TemporaryDirectory() as td:
            _write(os.path.join(td, "src", "lib.mh"), b"export fn hi() { return 1 }\n")
            _write(os.path.join(td, "README.md"), b"hi\n")
            expected = "sha256:24e49e36d4e16f5d6eb1a8f28697aa0218a8df1660577b347eeef9c8a43b6498"
            self.assertEqual(hash_tree(td), expected)
            os.symlink("README.md", os.path.join(td, "link"))
            os.symlink("src", os.path.join(td, "linkdir"))
            self.assertEqual(hash_tree(td), expected)
            _write(os.path.join(td, "README.md"), b"hj\n")
            self.assertNotEqual(hash_tree(td), expected)

    def _example(self):
        greet = LockedPackage(
            Dependency("greet", "acme/greet", "tag", "v1", ""),
            "3aaab8a9e960398e6603109e12700059417f1d7d",
            "sha256:24e49e36d4e16f5d6eb1a8f28697aa0218a8df1660577b347eeef9c8a43b6498",
            ("util",),
        )
        util = LockedPackage(
            Dependency("util", "acme/mono", "branch", "main", "packages/util"),
            "9f00e12" + "0" * 33,
            "sha256:" + "1" * 64,
            (),
        )
        return {"util": util, "greet": greet}

    def test_lock_text(self):
        expected = (
            "# mah-lock.toml -- written by `mah install`. Commit it; don't edit it by hand.\n"
            "version = 1\n"
            "\n"
            "[[package]]\n"
            'name = "greet"\n'
            'github = "acme/greet"\n'
            'tag = "v1"\n'
            'commit = "3aaab8a9e960398e6603109e12700059417f1d7d"\n'
            'hash = "sha256:24e49e36d4e16f5d6eb1a8f28697aa0218a8df1660577b347eeef9c8a43b6498"\n'
            'dependencies = ["util"]\n'
            "\n"
            "[[package]]\n"
            'name = "util"\n'
            'github = "acme/mono"\n'
            'branch = "main"\n'
            'path = "packages/util"\n'
            f'commit = "9f00e12{"0" * 33}"\n'
            f'hash = "sha256:{"1" * 64}"\n'
            "dependencies = []\n"
        )
        self.assertEqual(lock_text(self._example()), expected)
        self.assertEqual(
            lock_text({}),
            "# mah-lock.toml -- written by `mah install`. Commit it; don't edit it by hand.\nversion = 1\n",
        )

    def test_lock_roundtrip_and_errors(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_lock(td))
            example = self._example()
            write_lock(td, example)
            self.assertEqual(read_lock(td), dict(sorted(example.items())))
            path = os.path.join(td, "mah-lock.toml")
            _write(path, "version = 2\n")
            with self.assertRaises(PackageError) as cm:
                read_lock(td)
            self.assertEqual(str(cm.exception), "mah-lock.toml was written by a newer mah (lock version 2); upgrade mah")
            _write(
                path,
                'version = 1\n\n[[package]]\nname = "greet"\ngithub = "acme/greet"\ntag = "v1"\n'
                f'hash = "sha256:{"0" * 64}"\ndependencies = []\n',
            )
            with self.assertRaises(PackageError) as cm:
                read_lock(td)
            self.assertEqual(
                str(cm.exception),
                "mah-lock.toml is invalid (package greet: missing commit); delete it and run `mah install`",
            )
            _write(path, "[[package]]\n")
            with self.assertRaises(PackageError) as cm:
                read_lock(td)
            self.assertIn("(missing version)", str(cm.exception))

    def test_package_paths(self):
        self.assertEqual(package_of_path("/p/.mah/packages/greet/src/lib.mh"), ("/p", "greet", "src/lib.mh"))
        self.assertEqual(package_label("/p/.mah/packages/greet/src/lib.mh"), "pkg:greet/src/lib.mh")
        self.assertIsNone(package_of_path("/p/src/a.mh"))
        self.assertIsNone(package_label("/p/src/a.mh"))
        self.assertIsNone(package_of_path("/p/.mah/packages/greet"))

    def test_lock_out_of_date(self):
        v1 = Dependency("greet", "acme/greet", "tag", "v1", "")
        v2 = Dependency("greet", "acme/greet", "tag", "v2", "")
        locked = {"greet": LockedPackage(v1, "a" * 40, "sha256:" + "0" * 64, ())}
        self.assertEqual(lock_out_of_date({"greet": v1}, {}), "'greet' was added")
        self.assertEqual(lock_out_of_date({"greet": v2}, locked), "'greet' was changed")
        self.assertIsNone(lock_out_of_date({"greet": v1}, locked))
        extra = dict(locked, other=LockedPackage(v2, "b" * 40, "sha256:" + "0" * 64, ()))
        self.assertIsNone(lock_out_of_date({"greet": v1}, extra))

    def test_git_env(self):
        saved = dict(os.environ)
        try:
            os.environ.pop("GITHUB_TOKEN", None)
            for key in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:
                del os.environ[key]
            env = git_env("https://github.com/a/b.git")
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertNotIn("GIT_CONFIG_COUNT", env)
            os.environ["GITHUB_TOKEN"] = "tok"
            env = git_env("https://github.com/a/b.git")
            self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: Basic eC1hY2Nlc3MtdG9rZW46dG9r")
            os.environ["GIT_CONFIG_COUNT"] = "2"
            env = git_env("https://github.com/a/b.git")
            self.assertEqual(env["GIT_CONFIG_KEY_2"], "http.extraHeader")
            self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
            del os.environ["GIT_CONFIG_COUNT"]
            env = git_env("file:///tmp/gh/a/b.git")
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertNotIn("GIT_CONFIG_COUNT", env)
            self.assertNotIn("GIT_CONFIG_KEY_0", env)
            os.environ.pop("GITHUB_TOKEN")
            env = git_env("https://github.com/a/b.git")
            self.assertNotIn("GIT_CONFIG_COUNT", env)
        finally:
            os.environ.clear()
            os.environ.update(saved)


# ---------------------------------------------------------------------------
# 9-18: compile-time resolution (fake_install, no git)
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP_RUST, "mah-vm isn't built")
class CompileResolutionTests(_EnvTestCase):
    def greet_project(self, main_src, files=None, extra_manifest=""):
        root = project(self.tmp, GREET_TOML, main_src, extra_manifest)
        fake_install(root, "greet", files or GREET_FILES, dep=GREET_DEP)
        return root

    def compile_error(self, root, rel="src/main.mh"):
        with self.assertRaises(SyntaxError) as cm:
            compile_to_bytes(path=os.path.join(root, rel))
        return str(cm.exception)

    def run_ok(self, root):
        rc, out, err = self.run_project(root)
        self.assertEqual(rc, 0, err)
        return out

    # 9
    def test_namespaced_and_flat(self):
        root = self.greet_project('import greet from "pkg:greet"\nprint(greet.hello("mah"))\n')
        self.assertEqual(self.run_ok(root), "hello mah\n")
        _write(os.path.join(root, "src", "main.mh"), 'import "pkg:greet/src/extra"\nprint(answer)\n')
        self.assertEqual(self.run_ok(root), "42\n")
        _write(
            os.path.join(root, "src", "main.mh"),
            'import a from "pkg:greet"\nimport b from "pkg:greet/src/lib.mh"\nprint(a.hello("x"))\nprint(b.hello("y"))\n',
        )
        self.assertEqual(self.run_ok(root), "hello x\nhello y\n")

    # 10
    def test_library_entry_variants(self):
        root = project(self.tmp, 'plain = { github = "acme/plain" }\nother = { github = "acme/other" }\nnolib = { github = "acme/nolib" }\n', None)
        fake_install(root, "plain", {"lib.mh": "export let v = 1\n"}, dep=Dependency("plain", "acme/plain", "default", None, ""))
        fake_install(
            root,
            "other",
            {"mah-project.toml": '[package]\nname = "o"\nversion = "1"\nlib = "main.mh"\n', "main.mh": "export let v = 2\n"},
            dep=Dependency("other", "acme/other", "default", None, ""),
        )
        fake_install(
            root,
            "nolib",
            {"mah-project.toml": '[package]\nname = "n"\nversion = "1"\n', "x.mh": "export let v = 3\n"},
            dep=Dependency("nolib", "acme/nolib", "default", None, ""),
        )
        _write(os.path.join(root, "src", "main.mh"), 'import p from "pkg:plain"\nimport o from "pkg:other"\nprint(p.v + o.v)\n')
        self.assertEqual(self.run_ok(root), "3\n")
        _write(os.path.join(root, "src", "main.mh"), 'import n from "pkg:nolib"\n')
        self.assertEqual(
            self.compile_error(root),
            "package 'nolib' has no library file src/lib.mh; import one of its files as "
            '"pkg:nolib/path/to/file.mh" at position #1:15',
        )

    # 11
    def test_resolution_errors(self):
        root = self.greet_project("")
        main = os.path.join(root, "src", "main.mh")
        invalid = 'write "pkg:NAME" or "pkg:NAME/path/to/file.mh"'
        for literal in ("pkg:", "pkg:Foo", "pkg:greet/../x"):
            _write(main, f'import g from "{literal}"\n')
            self.assertEqual(
                self.compile_error(root), f"invalid package import '{literal}': {invalid} at position #1:15"
            )
        _write(main, 'import "pkg:greet/nope"\n')
        self.assertEqual(self.compile_error(root), "package 'greet' has no file 'nope.mh' at position #1:8")
        _write(main, 'import "pkg:greet/src/lib.test.mh"\n')
        self.assertIn("can't import the test file", self.compile_error(root))
        _write(main, 'import "pkg:other"\n')
        self.assertEqual(
            self.compile_error(root), "'other' isn't in the [dependencies] of mah-project.toml at position #1:8"
        )

    def test_no_project(self):
        lone = os.path.join(self.tmp, "lone")
        os.makedirs(lone)
        _write(os.path.join(lone, "x.mh"), 'import "pkg:greet"\n')
        with self.assertRaises(SyntaxError) as cm:
            compile_to_bytes(path=os.path.join(lone, "x.mh"))
        self.assertEqual(
            str(cm.exception),
            "package imports need a project, but no mah-project.toml was found for 'x.mh' at position #1:8",
        )

    def test_installed_only_for(self):
        root = project(self.tmp, 'a = { github = "acme/a" }\n', 'import "pkg:b"\n')
        fake_install(root, "b", {"lib.mh": "export let v = 1\n"}, dep=Dependency("b", "acme/b", "default", None, ""))
        fake_install(
            root, "a", {"lib.mh": 'import "pkg:b"\n'}, dep=Dependency("a", "acme/a", "default", None, ""), deps=("b",)
        )
        self.assertEqual(
            self.compile_error(root),
            "'b' isn't in the [dependencies] of mah-project.toml (it's installed only for 'a') at position #1:8",
        )

    def test_lock_states(self):
        root = project(self.tmp, GREET_TOML, 'import "pkg:greet"\n')
        self.assertEqual(
            self.compile_error(root),
            "mah-project.toml has [dependencies] but there's no mah-lock.toml; run `mah install` at position #1:8",
        )
        fake_install(root, "greet", GREET_FILES, dep=GREET_DEP)
        manifest = os.path.join(root, "mah-project.toml")
        _write(manifest, _read(manifest) + 'other = { github = "acme/other" }\n')
        self.assertEqual(
            self.compile_error(root),
            "mah-lock.toml is out of date with mah-project.toml ('other' was added); run `mah install` at position #1:8",
        )
        _write(manifest, _read(manifest).replace('other = { github = "acme/other" }\n', "").replace('"v1"', '"v2"'))
        self.assertEqual(
            self.compile_error(root),
            "mah-lock.toml is out of date with mah-project.toml ('greet' was changed); run `mah install` at position #1:8",
        )
        _write(manifest, _read(manifest).replace('"v2"', '"v1"'))
        installed = os.path.join(root, ".mah", "installed.toml")
        _write(installed, _read(installed).replace("a" * 40, "b" * 40))
        self.assertEqual(
            self.compile_error(root),
            "package 'greet' in .mah/packages doesn't match mah-lock.toml; run `mah install` at position #1:8",
        )
        shutil.rmtree(os.path.join(root, ".mah"))
        self.assertEqual(
            self.compile_error(root), "package 'greet' isn't installed; run `mah install` at position #1:8"
        )

    # 12
    def test_error_in_imported_file(self):
        root = self.greet_project('import "api.mh"\n')
        _write(os.path.join(root, "src", "api.mh"), 'import "pkg:missing"\n')
        self.assertEqual(
            self.compile_error(root),
            "'missing' isn't in the [dependencies] of mah-project.toml (in 'api.mh') at position #1:8",
        )

    # 13
    def test_package_scoping(self):
        root = project(self.tmp, 'a = { github = "acme/a" }\n', 'import a from "pkg:a"\nprint(a.v)\n')
        dep = lambda n: Dependency(n, f"acme/{n}", "default", None, "")  # noqa: E731
        fake_install(root, "b", {"lib.mh": "export let v = 7\n"}, dep=dep("b"))
        fake_install(root, "c", {"lib.mh": "export let v = 8\n"}, dep=dep("c"))
        a_manifest = '[package]\nname = "a"\nversion = "1"\n'
        a_lib = {"mah-project.toml": a_manifest, "src/lib.mh": 'import b from "pkg:b"\nexport let v = b.v\n'}
        fake_install(root, "a", a_lib, dep=dep("a"), deps=("b",))
        self.assertEqual(self.run_ok(root), "7\n")
        cases = [
            ('import c from "pkg:c"\nexport let v = c.v\n',
             "package 'a' imports 'pkg:c', but 'c' isn't in its [dependencies] (in 'pkg:a/src/lib.mh')"),
            ('import "../../../src/main.mh"\nexport let v = 1\n',
             "import '../../../src/main.mh' leaves package 'a'; import other packages as \"pkg:NAME\" (in 'pkg:a/src/lib.mh')"),
            ('import "nope"\nexport let v = 1\n', "cannot find imported file 'nope' (in 'pkg:a/src/lib.mh')"),
        ]
        for source, message in cases:
            fake_install(root, "a", {"mah-project.toml": a_manifest, "src/lib.mh": source}, dep=dep("a"), deps=("b",))
            self.assertEqual(self.compile_error(root), f"{message} at position #1:15")

    # 14
    def test_laziness(self):
        root = project(self.tmp, GREET_TOML, 'print("no packages")\n')
        self.assertEqual(self.run_ok(root), "no packages\n")

    # 15
    def test_runtime_and_compile_locations(self):
        files = dict(GREET_FILES)
        files["src/lib.mh"] = "export fn boom() { let v = [1]; return v[5] + 1 }\n"
        root = self.greet_project('import greet from "pkg:greet"\ngreet.boom()\n', files)
        _out, exc = _run_capturing(compile_to_bytes(path=os.path.join(root, "src", "main.mh")), "")
        self.assertIsNotNone(exc)
        self.assertIn("pkg:greet/src/lib.mh#1:", str(exc))
        rc, _out, err = self.run_project(root)
        self.assertEqual(rc, 1)
        self.assertIn("pkg:greet/src/lib.mh#1:", err)
        files["src/lib.mh"] = "export fn boom() {\n  let = \n}\n"
        fake_install(root, "greet", files, dep=GREET_DEP)
        self.assertRegex(self.compile_error(root), r"pkg:greet/src/lib\.mh#\d+:\d+")

    # 16
    def test_build_and_runc_without_mah_dir(self):
        root = self.greet_project(
            'import greet from "pkg:greet"\nprint(greet.hello("mah"))\n',
            extra_manifest='\n[[target]]\nname = "debug"\nprofile = "debug"\nout = "build/app.mahc"\n',
        )
        rc, _out, err = _mah(["build"], cwd=root)
        self.assertEqual(rc, 0, err)
        out_path = os.path.join(root, "build", "app.mahc")
        with open(out_path, "rb") as f:
            program = decode(f.read())
        names = [program.strings[i] for i in program.debug.files]
        self.assertIn("pkg:greet/src/lib.mh", names)
        shutil.rmtree(os.path.join(root, ".mah"))
        rc, out, err = _mah(["runc", "--vm", VM, out_path], cwd=self.tmp)
        self.assertEqual((rc, out), (0, "hello mah\n"), err)

    # 17
    def test_mah_test_and_format_skip_packages(self):
        root = self.greet_project('import greet from "pkg:greet"\nprint(greet.hello("mah"))\n')
        _write(
            os.path.join(root, "src", "main.test.mh"),
            'import "std:test"\n\ntest "one" {\n    assert_eq(1, 1)\n}\n',
        )
        _write(os.path.join(root, ".mah", "packages", "greet", "src", "ugly.mh"), "let   x=1\n")
        rc, out, err = _mah(["test", "--vm", VM], cwd=root)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("running 1 test\n", out)
        rc, out, err = _mah(["format", "--check"], cwd=root)
        self.assertEqual(rc, 0, out + err)

    # 18
    def test_strict_project_ignores_package_type_errors(self):
        bad = 'export fn hello(name: String) -> Number { return "hello " + name }\n'
        files = dict(GREET_FILES)
        files["src/lib.mh"] = bad
        root = self.greet_project(
            'import greet from "pkg:greet"\nprint(greet.hello("mah"))\n', files, extra_manifest='\n[types]\ncheck = "strict"\n'
        )
        # the same code in the root project is a type error
        _write(os.path.join(root, "src", "own.mh"), bad)
        rc, out, _err = _mah(["check", os.path.join(root, "src", "own.mh")], cwd=root)
        self.assertEqual(rc, 1, out)
        rc, out, err = _mah(["check"], cwd=root)
        self.assertEqual((rc, out), (0, "no type errors\n"), err)
        self.assertEqual(self.run_ok(root), "hello mah\n")


# ---------------------------------------------------------------------------
# 19-35: mah install (git)
# ---------------------------------------------------------------------------


def _all_bytes(files):
    return [(rel, c if isinstance(c, bytes) else c.encode("utf-8")) for rel, c in files.items()]


@unittest.skipUnless(_HAS_GIT, "needs git")
@unittest.skipIf(_SKIP_RUST, "mah-vm isn't built")
class InstallTests(_EnvTestCase):
    MAIN = 'import greet from "pkg:greet"\nprint(greet.hello("mah"))\n'

    def greet(self):
        sha = self.gh.make_repo("acme", "greet", GREET_FILES)
        self.gh.tag("acme", "greet", "v1")
        return sha

    def lock(self, root):
        return read_lock(root)

    # 19
    def test_tag_install_and_rerun(self):
        sha = self.greet()
        root = project(self.tmp, GREET_TOML, self.MAIN)
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f"installed greet {sha[:7]} (acme/greet tag v1)\nwrote mah-lock.toml\n")
        lock = self.lock(root)
        self.assertEqual(lock["greet"].commit, sha)
        self.assertEqual(lock["greet"].hash, hash_files(_all_bytes(GREET_FILES)))
        self.assertTrue(os.path.isfile(os.path.join(root, ".mah", "packages", "greet", "src", "lib.mh")))
        self.assertEqual(_read(os.path.join(root, ".mah", ".gitignore")), "*\n")
        self.assertFalse(os.path.exists(os.path.join(root, ".mah", "tmp")))
        rc, out, err = self.run_project(root)
        self.assertEqual((rc, out), (0, "hello mah\n"), err)
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f"kept greet {sha[:7]} (acme/greet tag v1)\nmah-lock.toml is up to date\n")

    # 20
    def test_annotated_tag(self):
        sha = self.gh.make_repo("acme", "greet", GREET_FILES)
        self.gh.tag("acme", "greet", "v1", annotated=True)
        root = project(self.tmp, GREET_TOML, self.MAIN)
        rc, _out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.lock(root)["greet"].commit, sha)

    # 21
    def test_branch_and_update(self):
        sha = self.greet()
        root = project(self.tmp, 'greet = { github = "acme/greet", branch = "main" }\n', self.MAIN)
        self.assertEqual(self.install(root)[0], 0)
        self.assertEqual(self.lock(root)["greet"].commit, sha)
        sha2 = self.gh.commit("acme", "greet", {"src/extra.mh": "export let answer = 43\n"})
        rc, out, _err = self.install(root)
        self.assertEqual(rc, 0)
        self.assertIn("kept greet", out)
        self.assertEqual(self.lock(root)["greet"].commit, sha)
        rc, out, _err = self.install(root, "--update")
        self.assertEqual(rc, 0)
        self.assertIn(f"installed greet {sha2[:7]}", out)
        self.assertEqual(self.lock(root)["greet"].commit, sha2)
        sha3 = self.gh.commit("acme", "greet", {"src/extra.mh": "export let answer = 44\n"})
        rc, out, _err = self.install(root, "--update", "greet")
        self.assertEqual(rc, 0)
        self.assertIn(f"installed greet {sha3[:7]}", out)
        rc, _out, err = self.install(root, "--update", "nope")
        self.assertEqual((rc, err), (2, "error: --update: no package named 'nope'\n"))

    # 22
    def test_rev(self):
        sha = self.greet()
        self.gh.commit("acme", "greet", {"src/new.mh": "export let n = 1\n"})
        root = project(self.tmp, f'greet = {{ github = "acme/greet", rev = "{sha}" }}\n', self.MAIN)
        rc, _out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.lock(root)["greet"].commit, sha)
        self.assertFalse(os.path.exists(os.path.join(root, ".mah", "packages", "greet", "src", "new.mh")))
        self.assertEqual(hash_tree(os.path.join(root, ".mah", "packages", "greet")), hash_files(_all_bytes(GREET_FILES)))

    # 23
    def test_default_branch(self):
        sha = self.greet()
        root = project(self.tmp, 'greet = { github = "acme/greet" }\n', self.MAIN)
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"installed greet {sha[:7]} (acme/greet default branch)", out)
        self.assertEqual(self.lock(root)["greet"].commit, sha)

    # 24
    def test_path(self):
        sha = self.gh.make_repo(
            "acme", "mono", {"packages/util/lib.mh": "export let u = 5\n", "other/x.mh": "export let x = 1\n"}
        )
        root = project(
            self.tmp, 'util = { github = "acme/mono", path = "packages/util" }\n', 'import u from "pkg:util"\nprint(u.u)\n'
        )
        rc, _out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertEqual(os.listdir(os.path.join(root, ".mah", "packages", "util")), ["lib.mh"])
        rc, out, err = self.run_project(root)
        self.assertEqual((rc, out), (0, "5\n"), err)
        _write(os.path.join(root, "mah-project.toml"), _read(os.path.join(root, "mah-project.toml")).replace("packages/util", "nope"))
        rc, _out, err = self.install(root)
        self.assertEqual((rc, err), (1, f"error: package 'util': 'nope' isn't a directory in acme/mono at {sha[:7]}\n"))

    # 25
    def test_frozen(self):
        self.greet()
        root = project(self.tmp, 'greet = { github = "acme/greet", branch = "main" }\n', self.MAIN)
        rc, _out, err = self.install(root, "--frozen")
        self.assertEqual((rc, err), (1, "error: --frozen: there's no mah-lock.toml; run `mah install` first\n"))
        self.assertEqual(self.install(root)[0], 0)
        locked_commit = self.lock(root)["greet"].commit
        manifest = os.path.join(root, "mah-project.toml")
        original = _read(manifest)
        _write(manifest, original.replace('branch = "main"', 'tag = "v1"'))
        rc, _out, err = self.install(root, "--frozen")
        self.assertEqual(
            (rc, err), (1, "error: --frozen: mah-lock.toml is out of date with mah-project.toml ('greet' was changed)\n")
        )
        _write(manifest, original)
        shutil.rmtree(os.path.join(root, ".mah"))
        self.gh.commit("acme", "greet", {"src/extra.mh": "export let answer = 43\n"})
        lock_path = os.path.join(root, "mah-lock.toml")
        with open(lock_path, "rb") as f:
            before = f.read()
        mtime = os.stat(lock_path).st_mtime_ns
        time.sleep(0.01)
        rc, out, err = self.install(root, "--frozen")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"installed greet {locked_commit[:7]}", out)
        with open(lock_path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.stat(lock_path).st_mtime_ns, mtime)
        self.assertEqual(_read(os.path.join(root, ".mah", "packages", "greet", "src", "extra.mh")), "export let answer = 42\n")
        rc, _out, err = self.install(root, "--frozen", "--update")
        self.assertEqual((rc, err), (2, "error: --frozen and --update can't be used together\n"))

    # 26
    def test_offline(self):
        sha = self.greet()
        root = project(self.tmp, GREET_TOML, self.MAIN)
        self.assertEqual(self.install(root)[0], 0)
        empty = os.path.join(self.tmp, "empty-bin")
        os.makedirs(empty)
        os.environ["MAH_GITHUB_URL_BASE"] = "file://" + os.path.join(self.tmp, "nowhere")
        os.environ["PATH"] = empty
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"kept greet {sha[:7]}", out)
        rc, out, err = self.run_project(root)
        self.assertEqual((rc, out), (0, "hello mah\n"), err)
        shutil.rmtree(os.path.join(root, ".mah"))
        rc, _out, err = self.install(root)
        self.assertEqual(
            (rc, err),
            (1, "error: package 'greet': mah install needs git to fetch packages, and git wasn't found on PATH\n"),
        )

    # 27
    def test_local_edit_is_repaired(self):
        sha = self.greet()
        root = project(self.tmp, GREET_TOML, self.MAIN)
        self.assertEqual(self.install(root)[0], 0)
        lib = os.path.join(root, ".mah", "packages", "greet", "src", "lib.mh")
        _write(lib, "changed\n")
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"reinstalled greet {sha[:7]} (acme/greet tag v1): its files had been changed\n", out)
        self.assertEqual(_read(lib), GREET_FILES["src/lib.mh"])

    # 28
    def test_hash_mismatch(self):
        sha = self.greet()
        root = project(self.tmp, GREET_TOML, self.MAIN)
        self.assertEqual(self.install(root)[0], 0)
        lock_path = os.path.join(root, "mah-lock.toml")
        text = _read(lock_path)
        good = self.lock(root)["greet"].hash
        _write(lock_path, text.replace(good, "sha256:" + "0" * 64))
        shutil.rmtree(os.path.join(root, ".mah"))
        rc, _out, err = self.install(root)
        self.assertEqual(rc, 1)
        self.assertIn(f"content hash mismatch at {sha[:7]}: mah-lock.toml has sha256:000", err)
        self.assertEqual(_read(lock_path), text.replace(good, "sha256:" + "0" * 64))

    # 29
    def test_removal_and_nothing(self):
        self.greet()
        root = project(self.tmp, GREET_TOML, self.MAIN)
        self.assertEqual(self.install(root)[0], 0)
        _write(os.path.join(root, "mah-project.toml"), _read(os.path.join(root, "mah-project.toml")).replace(GREET_TOML, ""))
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertIn("removed greet\n", out)
        self.assertFalse(os.path.exists(os.path.join(root, ".mah", "packages", "greet")))
        self.assertEqual(
            _read(os.path.join(root, "mah-lock.toml")),
            "# mah-lock.toml -- written by `mah install`. Commit it; don't edit it by hand.\nversion = 1\n",
        )
        bare = project(os.path.join(self.tmp, "bare"), "", 'print(1)\n')
        rc, out, _err = self.install(bare)
        self.assertEqual((rc, out), (0, "no dependencies to install\n"))
        self.assertFalse(os.path.exists(os.path.join(bare, ".mah")))
        self.assertFalse(os.path.exists(os.path.join(bare, "mah-lock.toml")))

    # 30
    def test_fetch_errors(self):
        self.greet()
        cases = [
            ('greet = { github = "acme/greet", tag = "v9" }\n', "error: package 'greet': tag 'v9' not found in acme/greet\n"),
            ('greet = { github = "acme/greet", branch = "dev" }\n', "error: package 'greet': branch 'dev' not found in acme/greet\n"),
            (f'greet = {{ github = "acme/greet", rev = "{"1" * 40}" }}\n', "error: package 'greet': commit 1111111 not found in acme/greet\n"),
        ]
        for i, (deps, message) in enumerate(cases):
            root = project(os.path.join(self.tmp, f"p{i}"), deps, self.MAIN)
            rc, _out, err = self.install(root)
            self.assertEqual((rc, err), (1, message))
            self.assertFalse(os.path.exists(os.path.join(root, "mah-lock.toml")))
        root = project(os.path.join(self.tmp, "p9"), 'nope = { github = "acme/nope" }\n', self.MAIN)
        rc, _out, err = self.install(root)
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error: package 'nope': couldn't reach acme/nope (file://"), err)

    def _transitive_repos(self):
        self.gh.make_repo("acme", "b", {"lib.mh": "export let v = 1\n"})
        self.gh.tag("acme", "b", "v1")
        self.gh.commit("acme", "b", {"lib.mh": "export let v = 2\n"})
        self.gh.tag("acme", "b", "v2")
        for name, tag in (("a", "v1"), ("c", "v2")):
            self.gh.make_repo(
                "acme",
                name,
                {
                    "mah-project.toml": f'[package]\nname = "{name}"\nversion = "1"\n\n[dependencies]\nb = {{ github = "acme/b", tag = "{tag}" }}\n',
                    "src/lib.mh": 'import b from "pkg:b"\nexport let v = b.v\n',
                },
            )
            self.gh.tag("acme", name, "v1")

    # 31
    def test_transitive(self):
        self._transitive_repos()
        root = project(self.tmp, 'a = { github = "acme/a", tag = "v1" }\n', 'import a from "pkg:a"\nprint(a.v)\n')
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("installed a "), out)
        self.assertTrue(lines[1].startswith("installed b "), out)
        self.assertEqual(self.lock(root)["a"].dependencies, ("b",))
        rc, out, err = self.run_project(root)
        self.assertEqual((rc, out), (0, "1\n"), err)
        _write(os.path.join(root, "src", "main.mh"), 'import b from "pkg:b"\nprint(b.v)\n')
        rc, _out, err = self.run_project(root)
        self.assertNotEqual(rc, 0)
        self.assertIn("(it's installed only for 'a')", err)

    # 32
    def test_conflict_and_root_override(self):
        self._transitive_repos()
        deps = 'a = { github = "acme/a", tag = "v1" }\nc = { github = "acme/c", tag = "v1" }\n'
        root = project(self.tmp, deps, 'import a from "pkg:a"\nprint(a.v)\n')
        rc, _out, err = self.install(root)
        self.assertEqual(
            (rc, err),
            (
                1,
                "error: packages 'a' and 'c' both depend on 'b' but ask for different versions: "
                "acme/b tag v1 vs acme/b tag v2; add 'b' to your [dependencies] to choose one\n",
            ),
        )
        self.assertFalse(os.path.exists(os.path.join(root, "mah-lock.toml")))
        _write(os.path.join(root, "mah-project.toml"), _read(os.path.join(root, "mah-project.toml")) + 'b = { github = "acme/b", tag = "v2" }\n')
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertIn("note: 'a' asks for 'b' as acme/b tag v1, but mah-project.toml chooses acme/b tag v2\n", out)
        rc, out, err = self.run_project(root)
        self.assertEqual((rc, out), (0, "2\n"), err)

    # 33
    def test_symlink_skipped(self):
        self.gh.make_repo("acme", "greet", GREET_FILES)
        sha = self.gh.symlink("acme", "greet", "src/link.mh", "lib.mh")
        root = project(self.tmp, 'greet = { github = "acme/greet" }\n', self.MAIN)
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertIn("note: greet: skipped src/link.mh (only regular files are installed)\n", out)
        self.assertFalse(os.path.lexists(os.path.join(root, ".mah", "packages", "greet", "src", "link.mh")))
        lock = self.lock(root)["greet"]
        self.assertEqual(lock.commit, sha)
        self.assertEqual(lock.hash, hash_files(_all_bytes(GREET_FILES)))

    # 34
    def test_invalid_package_manifest(self):
        files = dict(GREET_FILES)
        files["mah-project.toml"] = 'bogus = 1\n\n[package]\nname = "greet"\nversion = "1"\n'
        self.gh.make_repo("acme", "greet", files)
        self.gh.tag("acme", "greet", "v1")
        root = project(self.tmp, GREET_TOML, self.MAIN)
        rc, _out, err = self.install(root)
        self.assertEqual(
            (rc, err),
            (1, "error: package 'greet': package 'greet' has an invalid mah-project.toml: unknown key 'bogus'\n"),
        )

    # 35
    def test_library_only_root(self):
        self.greet()
        root = project(self.tmp, GREET_TOML, None)
        rc, out, err = self.install(root)
        self.assertEqual(rc, 0, err)
        self.assertIn("wrote mah-lock.toml", out)
        rc, out, err = _run_main(["install", root])
        self.assertEqual(rc, 0, err)


if __name__ == "__main__":
    unittest.main()
