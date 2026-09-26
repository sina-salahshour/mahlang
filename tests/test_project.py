"""Tests for M15: `mah init`, `mah-project.toml` manifests
(mah/project/manifest.py), and project-aware `mah run`/`mah build`
(mah/cli/main.py). See docs/TESTING.md for the testing policy this file is
part of, and /tmp .../M15_SPEC.md (the milestone spec) for the exact
behavior each test below pins down.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode
from mah.cli.main import main as cli_main
from mah.project.init import init_project, sanitize_package_name
from mah.project.manifest import MahProjectError, find_manifest, load_project
from tests.support import compile_source

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES_DOC = os.path.join(_REPO_ROOT, "mah", "project", "templates", "docs", "mah-language.md")


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


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

class InitTests(unittest.TestCase):
    def test_init_creates_expected_files(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                rc, out, _err = _run_main(["init", "foo"])
            self.assertEqual(rc, 0)
            self.assertIn("Created Mah project 'foo'", out)
            self.assertIn("cd foo && mah run", out)

            created = set()
            for root, _dirs, files in os.walk(os.path.join(td, "foo")):
                for name in files:
                    created.add(os.path.relpath(os.path.join(root, name), os.path.join(td, "foo")))
            self.assertEqual(
                created,
                {"mah-project.toml", os.path.join("src", "main.mh"), ".gitignore", "AGENTS.md", "CLAUDE.md",
                 os.path.join("docs", "mah-language.md")},
            )

    def test_generated_manifest_loads(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "foo"])
            root = os.path.join(td, "foo")
            manifest_path = os.path.join(root, "mah-project.toml")
            project = load_project(manifest_path)
            self.assertEqual(project.name, "foo")
            self.assertEqual(project.version, "0.1.0")
            self.assertEqual(project.entry, os.path.join(root, "src", "main.mh"))
            self.assertEqual(
                [(t.name, t.profile, t.out) for t in project.targets],
                [
                    ("debug", "debug", os.path.join(root, "build", "foo-debug.mahc")),
                    ("release", "release", os.path.join(root, "build", "foo.mahc")),
                ],
            )
            self.assertEqual(project.dependencies, {})
            self.assertEqual([t.self_contained for t in project.targets], [False, False])
            self.assertEqual(project.run_vm, "python")

            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                        self.assertNotIn("{{", f.read(), msg=name)

    def test_init_no_args_sanitizes_cwd_name(self):
        with tempfile.TemporaryDirectory() as parent:
            target = os.path.join(parent, "My App")
            os.makedirs(target)
            with _chdir(target):
                rc, out, _err = _run_main(["init"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(os.path.join(target, "mah-project.toml")))
            project = load_project(os.path.join(target, "mah-project.toml"))
            self.assertEqual(project.name, "my-app")
            self.assertIn("Next: mah run", out)

    def test_init_skips_existing_main(self):
        with tempfile.TemporaryDirectory() as td:
            directory = os.path.join(td, "proj")
            os.makedirs(os.path.join(directory, "src"))
            with open(os.path.join(directory, "src", "main.mh"), "w") as f:
                f.write("print(1)")
            rc, out, _err = _run_main(["init", directory])
            self.assertEqual(rc, 0)
            with open(os.path.join(directory, "src", "main.mh")) as f:
                self.assertEqual(f.read(), "print(1)")
            self.assertIn("skipped (already exist):", out)
            self.assertIn("src/main.mh", out.split("skipped (already exist):", 1)[1])
            self.assertTrue(os.path.exists(os.path.join(directory, "mah-project.toml")))
            self.assertTrue(os.path.exists(os.path.join(directory, "AGENTS.md")))

    def test_init_refuses_existing_project(self):
        with tempfile.TemporaryDirectory() as td:
            directory = os.path.join(td, "proj")
            os.makedirs(directory)
            with open(os.path.join(directory, "mah-project.toml"), "w") as f:
                f.write("[package]\nname = \"x\"\nversion = \"1\"\n")
            rc, _out, err = _run_main(["init", directory])
            self.assertEqual(rc, 1)
            self.assertIn("already a Mah project", err)
            self.assertEqual(os.listdir(directory), ["mah-project.toml"])

    def test_sanitize_package_name(self):
        self.assertEqual(sanitize_package_name("My App!"), "my-app")
        self.assertEqual(sanitize_package_name("foo"), "foo")
        self.assertEqual(sanitize_package_name("___"), "___")
        self.assertEqual(sanitize_package_name("!!!"), "mah-project")

    def test_generated_project_runs(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "foo"])
                rc, out, _err = _run_main(["run", "foo"])
            self.assertEqual(rc, 0)
            self.assertEqual(out, "Hello, foo!\n")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

class RunProjectTests(unittest.TestCase):
    def test_run_no_args_uses_cwd_project(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                name = sanitize_package_name(os.path.basename(os.path.abspath(td)))
                rc, out, _err = _run_main(["run"])
            self.assertEqual(rc, 0)
            self.assertEqual(out, f"Hello, {name}!\n")

    def test_run_from_subdirectory_searches_upward(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                name = sanitize_package_name(os.path.basename(os.path.abspath(td)))
                os.makedirs(os.path.join("src", "deeper"))
                with _chdir(os.path.join("src", "deeper")):
                    rc, out, _err = _run_main(["run"])
            self.assertEqual(rc, 0)
            self.assertEqual(out, f"Hello, {name}!\n")

    def test_run_custom_entry(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                os.makedirs("app")
                with open("app/start.mh", "w") as f:
                    f.write('print("started")')
                with open("mah-project.toml") as f:
                    manifest = f.read()
                manifest = manifest.replace('entry = "src/main.mh"', 'entry = "app/start.mh"')
                with open("mah-project.toml", "w") as f:
                    f.write(manifest)
                rc, out, _err = _run_main(["run"])
            self.assertEqual(rc, 0)
            self.assertEqual(out, "started\n")

    def test_run_missing_entry(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                os.remove(os.path.join("src", "main.mh"))
                rc, _out, err = _run_main(["run"])
            self.assertEqual(rc, 2)
            self.assertIn("does not exist", err)

    def test_run_no_manifest_anywhere(self):
        with tempfile.TemporaryDirectory() as td:
            real_td = os.path.realpath(td)
            if find_manifest(real_td) is not None:
                self.skipTest("a mah-project.toml exists above the temp dir")
            with _chdir(td):
                rc, _out, err = _run_main(["run"])
            self.assertEqual(rc, 2)
            self.assertIn("no mah-project.toml found", err)

    def test_run_explicit_file_in_project_dir_runs_just_that_file(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                with open("other.mh", "w") as f:
                    f.write('print("just this file")')
                rc, out, _err = _run_main(["run", "other.mh"])
            self.assertEqual(rc, 0)
            self.assertEqual(out, "just this file\n")


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

class BuildProjectTests(unittest.TestCase):
    def test_build_writes_both_targets(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                name = sanitize_package_name(os.path.basename(os.path.abspath(td)))
                rc, out, _err = _run_main(["build"])
                self.assertEqual(rc, 0)
                self.assertEqual(len(out.strip().splitlines()), 2)
                self.assertIn("built debug (debug) ->", out)
                self.assertIn("built release (release) ->", out)

                debug_path = os.path.join("build", f"{name}-debug.mahc")
                release_path = os.path.join("build", f"{name}.mahc")
                self.assertTrue(os.path.exists(debug_path))
                self.assertTrue(os.path.exists(release_path))

                for path in (debug_path, release_path):
                    rc, out, _err = _run_main(["runc", path])
                    self.assertEqual(rc, 0)
                    self.assertEqual(out, f"Hello, {name}!\n")

                with open(debug_path, "rb") as f:
                    self.assertIsNotNone(decode(f.read()).debug)
                with open(release_path, "rb") as f:
                    self.assertIsNone(decode(f.read()).debug)

    def test_build_target_release_only(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                name = sanitize_package_name(os.path.basename(os.path.abspath(td)))
                rc, out, _err = _run_main(["build", "--target", "release"])
                self.assertEqual(rc, 0)
                self.assertEqual(len(out.strip().splitlines()), 1)
                self.assertFalse(os.path.exists(os.path.join("build", f"{name}-debug.mahc")))
                self.assertTrue(os.path.exists(os.path.join("build", f"{name}.mahc")))

    def test_build_unknown_target(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                rc, _out, err = _run_main(["build", "--target", "nope"])
            self.assertEqual(rc, 2)
            self.assertIn("available: debug, release", err)

    def test_build_out_not_allowed_in_project_mode(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                rc, _out, err = _run_main(["build", "-o", "x.mahc"])
            self.assertEqual(rc, 2)
            self.assertIn("--out can't be used", err)

    def test_build_file_mode_target_validation(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "some.mh")
            with open(src, "w") as f:
                f.write('print("hi")')

            rc, _out, err = _run_main(["build", src, "--target", "nope"])
            self.assertEqual(rc, 2)
            self.assertIn("--target must be 'debug' or 'release'", err)

            rc, _out, _err = _run_main(["build", src, "--target", "release"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(os.path.join(td, "some.mahc")))


# ---------------------------------------------------------------------------
# manifest validation
# ---------------------------------------------------------------------------

# A stand-in `mah-vm` (MAH_VM points to it): enough for the CLI to take
# its version and to show that it was the one asked to run the program.
_FAKE_VM = """#!/bin/sh
if [ "$1" = --version ]; then echo "mah-vm 9.9.9 (fake-target)"; exit 0; fi
echo "fake vm ran $1"
"""


@unittest.skipUnless(os.name == "posix", "the stand-in mah-vm is a shell script")
class RustRuntimeSettingsTests(unittest.TestCase):
    """`[run] vm` and `self-contained = true` (the real runtime is covered
    by tests/test_rust_vm.py)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        vm = os.path.join(self.td.name, "fake-mah-vm")
        with open(vm, "w") as f:
            f.write(_FAKE_VM)
        os.chmod(vm, 0o755)
        self.old_vm = os.environ.get("MAH_VM")
        os.environ["MAH_VM"] = vm
        self.root = os.path.join(self.td.name, "proj")
        with _chdir(self.td.name):
            _run_main(["init", "proj"])
        self.manifest = os.path.join(self.root, "mah-project.toml")

    def tearDown(self):
        if self.old_vm is None:
            os.environ.pop("MAH_VM", None)
        else:
            os.environ["MAH_VM"] = self.old_vm
        self.td.cleanup()

    def _edit_manifest(self, old, new):
        with open(self.manifest) as f:
            text = f.read()
        self.assertIn(old, text)
        with open(self.manifest, "w") as f:
            f.write(text.replace(old, new))

    def _run_captured(self, argv):
        # the rust path runs a subprocess, which writes to the real fd 1
        with tempfile.TemporaryFile("w+") as out:
            old = os.dup(1)
            os.dup2(out.fileno(), 1)
            try:
                rc = cli_main(argv)
                sys.stdout.flush()
            finally:
                os.dup2(old, 1)
                os.close(old)
            out.seek(0)
            return rc, out.read()

    def test_run_uses_the_manifest_vm(self):
        self._edit_manifest('vm = "python"', 'vm = "rust"')
        with _chdir(self.root):
            rc, out = self._run_captured(["run"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("fake vm ran "), out)

    def test_vm_flag_overrides_the_manifest(self):
        self._edit_manifest('vm = "python"', 'vm = "rust"')
        with _chdir(self.root):
            rc, out = self._run_captured(["run", "--vm", "python"])
        self.assertEqual((rc, out), (0, "Hello, proj!\n"))

    def test_default_manifest_runs_on_python(self):
        with _chdir(self.root):
            rc, out = self._run_captured(["run"])
        self.assertEqual((rc, out), (0, "Hello, proj!\n"))

    def test_self_contained_target(self):
        self._edit_manifest(
            "# [[target]]\n# name = \"dist\"\n# profile = \"release\"\n# out = \"build/{name}\"\n"
            "# self-contained = true".replace("{name}", "proj"),
            '[[target]]\nname = "dist"\nprofile = "release"\nout = "build/proj"\nself-contained = true',
        )
        with _chdir(self.root):
            rc, out, err = _run_main(["build"])
        self.assertEqual(rc, 0, msg=err)
        self.assertIn("built dist (release, self-contained) -> build/proj", out)
        self.assertIn("built release (release) -> build/proj.mahc", out)
        with open(os.path.join(self.root, "build", "proj"), "rb") as f:
            self.assertTrue(f.read().startswith(b"#!/bin/sh\n# mah-bundle v1\n# vm-version: 9.9.9\n"))
        with open(os.path.join(self.root, "build", "proj.mahc"), "rb") as f:
            self.assertTrue(f.read().startswith(b"#!/usr/bin/env -S mah runc\n"))


class ManifestValidationTests(unittest.TestCase):
    def _write(self, td, text):
        path = os.path.join(td, "mah-project.toml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def _assert_error(self, path, substring):
        with self.assertRaises(MahProjectError) as cm:
            load_project(path)
        self.assertIn(substring, str(cm.exception))
        self.assertTrue(str(cm.exception).startswith(f"{path}: "))

    def test_invalid_toml(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, "this is not [valid toml")
            self._assert_error(path, "invalid TOML:")

    def test_missing_package(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, "[dependencies]\n")
            self._assert_error(path, "missing [package] table")

    def test_missing_version(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, '[package]\nname = "x"\n')
            self._assert_error(path, "package.version must be a non-empty string")

    def test_version_not_a_string(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, '[package]\nname = "x"\nversion = 1\n')
            self._assert_error(path, "package.version must be a non-empty string")

    def test_unknown_top_level_key_targets(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td, '[package]\nname = "x"\nversion = "1"\n\n[[targets]]\nname = "a"\n'
            )
            self._assert_error(path, "did you mean [[target]]?")

    def test_unknown_package_key(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td, '[package]\nname = "x"\nversion = "1"\nauthor = "me"\n'
            )
            self._assert_error(path, "unknown key 'package.author'")

    def test_self_contained_must_be_a_bool(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td,
                '[package]\nname = "x"\nversion = "1"\n\n'
                '[[target]]\nname = "a"\nprofile = "release"\nout = "a"\nself-contained = "yes"\n',
            )
            self._assert_error(path, "target 'a': self-contained must be true or false")

    def test_run_vm_must_be_python_or_rust(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, '[package]\nname = "x"\nversion = "1"\n\n[run]\nvm = "jvm"\n')
            self._assert_error(path, 'run.vm must be "python" or "rust"')

    def test_unknown_run_key(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, '[package]\nname = "x"\nversion = "1"\n\n[run]\nfast = true\n')
            self._assert_error(path, "unknown key 'run.fast'")

    def test_run_and_self_contained_settings_load(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td,
                '[package]\nname = "x"\nversion = "1"\n\n[run]\nvm = "rust"\n\n'
                '[[target]]\nname = "a"\nprofile = "release"\nout = "a"\nself-contained = true\n',
            )
            project = load_project(path)
            self.assertEqual(project.run_vm, "rust")
            self.assertTrue(project.targets[0].self_contained)

    def test_invalid_profile(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td,
                '[package]\nname = "x"\nversion = "1"\n\n'
                '[[target]]\nname = "a"\nprofile = "fast"\nout = "a.mahc"\n',
            )
            self._assert_error(path, 'profile must be "debug" or "release"')

    def test_duplicate_target_names(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td,
                '[package]\nname = "x"\nversion = "1"\n\n'
                '[[target]]\nname = "a"\nprofile = "debug"\nout = "a.mahc"\n\n'
                '[[target]]\nname = "a"\nprofile = "release"\nout = "b.mahc"\n',
            )
            self._assert_error(path, "duplicate target name 'a'")

    def test_duplicate_target_out(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td,
                '[package]\nname = "x"\nversion = "1"\n\n'
                '[[target]]\nname = "a"\nprofile = "debug"\nout = "same.mahc"\n\n'
                '[[target]]\nname = "b"\nprofile = "release"\nout = "same.mahc"\n',
            )
            self._assert_error(path, "write the same file")

    def test_target_as_single_table(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td,
                '[package]\nname = "x"\nversion = "1"\n\n'
                '[target]\nname = "a"\nprofile = "debug"\nout = "a.mahc"\n',
            )
            self._assert_error(path, "array of tables")

    def test_nonempty_dependencies(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td, '[package]\nname = "x"\nversion = "1"\n\n[dependencies]\nfoo = "1.0"\n'
            )
            self._assert_error(path, "aren't supported yet")

    def test_entry_defaults_to_src_main_mh(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, '[package]\nname = "x"\nversion = "1"\n')
            project = load_project(path)
            self.assertEqual(project.entry, os.path.join(td, "src", "main.mh"))


# ---------------------------------------------------------------------------
# docs sanity
# ---------------------------------------------------------------------------

class DocsSanityTests(unittest.TestCase):
    def test_every_mah_code_block_in_template_docs_compiles(self):
        with open(_TEMPLATES_DOC, encoding="utf-8") as f:
            text = f.read()
        blocks = re.findall(r"```mah\n(.*?)```", text, re.DOTALL)
        self.assertGreaterEqual(len(blocks), 7)

        checked = 0
        failures = []
        for block in blocks:
            if "match value {" in block or "obj.method" in block or block.startswith("# mathlib.mh"):
                continue
            checked += 1
            try:
                compile_source(text=block)
            except Exception as e:  # noqa: BLE001 -- report every failure, don't stop at the first
                failures.append((block, e))

        self.assertGreaterEqual(checked, 7, "extraction likely broken -- too few blocks checked")
        if failures:
            details = "\n---\n".join(f"{e}\n{block}" for block, e in failures)
            self.fail(f"{len(failures)} doc code block(s) failed to compile:\n{details}")


class InitHintQuotingTests(unittest.TestCase):
    """Added during verification: the printed `Next: cd ... && mah run` hint
    must be pasteable for a directory whose name needs shell quoting."""

    def test_directory_with_space_is_shell_quoted(self):
        with tempfile.TemporaryDirectory() as tmp, _chdir(tmp):
            rc, out, _err = _run_main(["init", "My Tool"])
            self.assertEqual(rc, 0)
            self.assertIn("Next: cd 'My Tool' && mah run", out)


class TemplateDriftTests(unittest.TestCase):
    """The project templates (mah/project/templates/) document the language
    for users and LLMs, so they must be updated whenever the language
    changes. These tests catch the drift that can be detected mechanically:
    a keyword, built-in type, or system trait the language reference never
    mentions, and the agent docs disagreeing with the manifest template about
    the entry point. (Doc examples that stop compiling are caught by
    DocsSanityTests above.) If one fails after a language change, update
    `docs/mah-language.md` -- including its "Not available" list if you
    removed a limitation -- rather than loosening the test."""

    def _doc(self):
        with open(_TEMPLATES_DOC, encoding="utf-8") as f:
            return f.read()

    def test_every_keyword_is_documented(self):
        from mah.compiler.lexer import KEYWORDS

        doc = self._doc()
        missing = sorted(kw for kw in KEYWORDS if not re.search(rf"\b{re.escape(kw)}\b", doc))
        self.assertEqual(missing, [], "keywords missing from docs/mah-language.md")

    def test_every_builtin_type_and_system_trait_is_documented(self):
        from mah.runtime_values import BUILTIN_TYPE_NAMES, SYSTEM_TRAITS

        doc = self._doc()
        names = list(BUILTIN_TYPE_NAMES) + list(SYSTEM_TRAITS)
        for trait_methods in SYSTEM_TRAITS.values():
            names += list(trait_methods)
        missing = sorted(n for n in names if not re.search(rf"\b{re.escape(n)}\b", doc))
        self.assertEqual(missing, [], "built-in types/system traits missing from docs/mah-language.md")

    def test_agent_docs_match_manifest_entry(self):
        import tomllib

        templates = os.path.dirname(os.path.dirname(_TEMPLATES_DOC))
        with open(os.path.join(templates, "mah-project.toml"), encoding="utf-8") as f:
            entry = tomllib.loads(f.read().replace("{{name}}", "x"))["package"]["entry"]
        self.assertTrue(os.path.exists(os.path.join(templates, entry)), f"template has no {entry}")
        with open(os.path.join(templates, "AGENTS.md"), encoding="utf-8") as f:
            self.assertIn(f"`{entry}`", f.read())


if __name__ == "__main__":
    unittest.main()
