"""Golden-output tests for everything under examples/ -- these are the
project's existing demo programs, run end to end through the real
pipeline. If a milestone changes language behavior on purpose, update the
expected output here deliberately (and say so in the commit/PR), don't
just delete the assertion.

See docs/TESTING.md for the testing policy this file is part of.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import example_path, run_file

PRIMES_UNDER_100 = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47,
                     53, 59, 61, 67, 71, 73, 79, 83, 89, 97]


class ExampleTests(unittest.TestCase):
    def test_strings(self):
        out = run_file(example_path("strings.mh"))
        self.assertEqual(
            out,
            # M16: `print(greeting, target)` now joins its arguments with a
            # single space (the default `sep`) instead of one per line.
            "Hello World\n"
            "Hello World!\n"
            "greeting is indeed Hello\n"
            "greeting and target are different\n"
            "Developer Sina\n"
            "Empty string matched!\n"
            "First line\n"
            "Second line\n"
            "*****\n",
        )

    def test_import_demo(self):
        out = run_file(example_path("import_demo.mh"))
        self.assertEqual(out, "16\n64\n42\nn is even\n")

    def test_new_prime_numbers(self):
        out = run_file(example_path("new_prime_numbers.mh"))
        self.assertEqual([int(n) for n in out.split()], PRIMES_UNDER_100)

    def test_prime_numbers(self):
        out = run_file(example_path("prime_numbers.mh"))
        self.assertEqual([int(n) for n in out.split()], PRIMES_UNDER_100)

    def test_decimal_to_binary(self):
        out = run_file(example_path("decimal_to_binary.mh"), stdin="13")
        self.assertEqual(out.strip(), "1101")

    def test_new_decimal_to_binary(self):
        out = run_file(example_path("new_decimal_to_binary.mh"), stdin="13")
        self.assertEqual(out.strip(), "1101")

    def test_binary_to_decimal(self):
        out = run_file(example_path("binary_to_decimal.mh"), stdin="1101")
        self.assertEqual(out.strip(), "13")

    def test_traits(self):
        out = run_file(example_path("traits.mh"))
        self.assertEqual(
            out,
            "a shape with area 15\n"
            "a shape with area 12\n"
            "Rect(5x6)\n"
            "42\n"
            "27\n"
            "15\n"
            "Rect(5x5)\n",
        )


if __name__ == "__main__":
    unittest.main()
