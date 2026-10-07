"""M43: the language server and `pkg:` imports (docs/PACKAGES.md,
docs/contracts/M43_packages.md section 13, tests 36-40). Packages are
installed by hand (`fake_install`), so nothing here needs git."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.lsp import analysis  # noqa: E402
from tests.test_packages import GREET_DEP, GREET_FILES, GREET_TOML, _write, fake_install, project  # noqa: E402


def _pos(text, needle, delta=0):
    idx = text.index(needle) + delta
    line = text.count("\n", 0, idx)
    return line, idx - (text.rfind("\n", 0, idx) + 1)


class LspPackageTests(unittest.TestCase):
    MAIN = 'import greet from "pkg:greet"\nprint(greet.hello("mah"))\n'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.root = project(self.tmp, GREET_TOML, self.MAIN)
        self.main = os.path.join(self.root, "src", "main.mh")

    def tearDown(self):
        self._tmp.cleanup()

    def install(self):
        fake_install(self.root, "greet", GREET_FILES, dep=GREET_DEP)

    # 36
    def test_definition_of_the_import_string(self):
        self.install()
        line, col = _pos(self.MAIN, '"pkg:greet"', 2)
        result = analysis.get_definition(self.MAIN, line, col, self.main)
        self.assertIsNotNone(result)
        self.assertEqual(result["path"], os.path.join(self.root, ".mah", "packages", "greet", "src", "lib.mh"))

    # 37
    def test_diagnostic_without_a_lock(self):
        src = 'import "pkg:greet"\n'
        diagnostics = analysis.get_diagnostics(src, self.main)
        messages = [d["message"] for d in diagnostics]
        self.assertIn(
            "mah-project.toml has [dependencies] but there's no mah-lock.toml; run `mah install`", messages
        )
        diag = diagnostics[messages.index(
            "mah-project.toml has [dependencies] but there's no mah-lock.toml; run `mah install`"
        )]
        self.assertEqual(diag["range"]["start"], {"line": 0, "character": 7})
        self.assertEqual(diag["range"]["end"], {"line": 0, "character": 18})

    def _labels(self, src):
        line, col = 0, len(src.split("\n")[0])
        return analysis.get_completions(src, self.main, line, col)

    # 38
    def test_completion(self):
        self.install()
        items = self._labels('import x from "pkg:')
        by_label = {i["label"]: i for i in items}
        self.assertIn("pkg:greet", by_label)
        self.assertEqual(by_label["pkg:greet"]["detail"], "acme/greet tag v1")
        self.assertFalse(any(label.startswith("std:") for label in by_label))
        labels = [i["label"] for i in self._labels('import x from "p')]
        self.assertIn("pkg:greet", labels)
        # `std:` items are offered while the typed text could still become
        # `std:` (unchanged), so with nothing typed yet both kinds are listed.
        labels = [i["label"] for i in self._labels('import x from "')]
        self.assertIn("pkg:greet", labels)
        self.assertIn("std:math", labels)
        labels = [i["label"] for i in self._labels('import x from "pkg:greet/')]
        self.assertEqual(labels, ["src"])
        labels = sorted(i["label"] for i in self._labels('import x from "pkg:greet/src/'))
        self.assertEqual(labels, ["extra", "lib"])
        # review: an invalid `pkg:` path never lists anything outside the package
        for partial in ("pkg:greet/../", "pkg:greet/../../../", "pkg:greet/./src/"):
            self.assertEqual(self._labels(f'import x from "{partial}'), [], partial)

    def test_completion_inside_a_package_file(self):
        self.install()
        lib = os.path.join(self.root, ".mah", "packages", "greet", "src", "lib.mh")
        src = 'import x from "pkg:'
        labels = [i["label"] for i in analysis.get_completions(src, lib, 0, len(src))]
        self.assertEqual(labels, ["pkg:greet"])

    # 39
    def test_hover_on_a_package_function(self):
        self.install()
        line, col = _pos(self.MAIN, "hello")
        hover = analysis.get_hover(self.MAIN, line, col, self.main)
        self.assertIsNotNone(hover)
        self.assertIn("declared in `pkg:greet/src/lib.mh`", hover["contents"]["value"])

    # 40
    def test_rename_of_a_package_function_is_refused(self):
        self.install()
        line, col = _pos(self.MAIN, "hello")
        self.assertIsNone(analysis.get_rename_edits(self.MAIN, line, col, "hi", self.main))

    def test_type_diagnostics_in_packages_are_dropped(self):
        files = dict(GREET_FILES)
        files["src/lib.mh"] = 'export fn hello(name: String) -> Number { return "hello " + name }\n'
        fake_install(self.root, "greet", files, dep=GREET_DEP)
        _write(
            os.path.join(self.root, "mah-project.toml"),
            '[package]\nname = "app"\nversion = "0.1.0"\n\n[types]\ncheck = "strict"\n\n[dependencies]\n' + GREET_TOML,
        )
        self.assertEqual(analysis.get_diagnostics(self.MAIN, self.main), [])


if __name__ == "__main__":
    unittest.main()
