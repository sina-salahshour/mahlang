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
        # M33: `input` prints its prompt and reads a String.
        out = run_file(example_path("decimal_to_binary.mh"), stdin="13")
        self.assertEqual(out, "decimal: 1101\n")

    def test_new_decimal_to_binary(self):
        out = run_file(example_path("new_decimal_to_binary.mh"), stdin="13")
        self.assertEqual(out, "decimal: 1101\n")

    def test_binary_to_decimal(self):
        out = run_file(example_path("binary_to_decimal.mh"), stdin="1101")
        self.assertEqual(out, "binary: 13\n")

    def test_iterators(self):
        out = run_file(example_path("iterators.mh"))
        self.assertEqual(
            out,
            "2..5\n"
            "2..=5\n"
            "10..\n"
            "..3\n"
            "single digit\n"
            "double digit\n"
            "big\n"
            "35\n"
            "hheelllloo\n"
            "3\n",
        )

    def test_collections(self):
        out = run_file(example_path("collections.mh"))
        self.assertEqual(
            out,
            "[0, 10, 20, 30, 40] 5\n11 none\n40 0\n"
            "0 11\n1 20\n2 30\n122\n"
            "[ada: 37, alan: 41, grace: 85] 37 none\n"
            "ada is 37\nalan is 41\ngrace is 85\n"
            "[ada, alan, grace] [37, 41, 85]\n"
            "ada: 37\nalan: 41\ngrace: 85\n"
            "true 41 false\n"
            "# [., #, ., .]\n",
        )

    def test_loops(self):
        out = run_file(example_path("loops.mh"))
        self.assertEqual(
            out,
            "1\n2\n3\n"
            "0 m\n1 a\n2 h\n"
            "multiple of three: 3\nmultiple of three: 9\nmultiple of three: 12\n"
            "10\n8\nnone\n"
            "3 ...\n2 ...\n1 ...\nliftoff\n",
        )

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

    def test_std_math(self):
        # M27: the first standard library module.
        out = run_file(example_path("std_math.mh"))
        self.assertEqual(
            out,
            "1.414213562373095048801688724\n"
            "3.1416 -3 3\n"
            "45\n"
            "3 2.718\n"
            "log(0) is undefined\n"
            "5\n"
            "0 1\n",
        )

    def test_string_methods(self):
        # M29: the String methods.
        out = run_file(example_path("string_methods.mh"))
        self.assertEqual(
            out,
            "MAH v0.2.0\n01. Sina\n02. Claude\nmajor version: 0 next: 1\n"
            "not a number none\nsome(2) -1 mAh\na -> b -> c\n",
        )

    def test_data_formats(self):
        # M30: std:csv -> FromCsvRow -> std:json, with std:path.
        out = run_file(example_path("data_formats.mh"))
        self.assertEqual(
            out,
            'writing data/scores.json\n'
            '{\n'
            '  "source": "scores.csv",\n'
            '  "players": [\n'
            '    {\n'
            '      "name": "ada",\n'
            '      "score": 91,\n'
            '      "team": "red"\n'
            '    },\n'
            '    {\n'
            '      "name": "bo",\n'
            '      "score": 78,\n'
            '      "team": "blue"\n'
            '    },\n'
            '    {\n'
            '      "name": "Chen, Li",\n'
            '      "score": 85,\n'
            '      "team": "red"\n'
            '    }\n'
            '  ],\n'
            '  "teams": {\n'
            '    "red": 176,\n'
            '    "blue": 78\n'
            '  }\n'
            '}\n'
            'Chen, Li scored 85\n'
        )

    def test_random_collections(self):
        # M31: seeded std:random (the same on both VMs) and std:collections.
        out = run_file(example_path("random_collections.mh"))
        self.assertEqual(
            out,
            'hand: [QS, KC, AS, AH, AD]\n'
            'suits in hand: Set[S, C, H, D] flush? false\n'
            'print order: [map/1, invoice/2, report/3, photo/3, memo/3]\n'
            'last printed: Deque[report, photo, memo]\n'
            'dice: [1, 3, 1]\n'
        )

    def test_regex_log(self):
        # M32: std:regex -- named groups, flags, templates, a function
        # replacement, and a RegexError.
        out = run_file(example_path("regex_log.mh"))
        self.assertEqual(
            out,
            '12:01:07 info  ada   login ok\n'
            '12:03:44 warn  bo    password retry 2\n'
            '12:04:02 error bo    locked out (3 failures)\n'
            '12:09:31 info  chen  login ok\n'
            'levels: [INFO: 2, WARN: 1, ERROR: 1]\n'
            'from 10.0.0.x and 192.168.1.x\n'
            '<RETRY>, then <LOCKED>\n'
            'missing ) at position 0 in "(\\d+"\n'
        )

    def test_timers(self):
        # M34: std:async's all/race/timeout/intervals and std:time's dates.
        out = run_file(example_path("timers.mh"))
        self.assertEqual(
            out,
            '[build done, test done, lint done]\n'
            'fastest: quick done\n'
            'deploy gave up: timed out after 50 ms\n'
            'ticker ran: true | elapsed under a second: true\n'
            'Monday 28 September 2026, 09:30 -> Thu 01 Oct, 11:00\n'
            'in between: 3d 1h 30m 0s\n'
            'no such date\n'
        )

    def test_files(self):
        # M35: std:fs in a temporary directory it removes again.
        out = run_file(example_path("files.mh"))
        self.assertEqual(
            out,
            'src/main.mh - 2 line(s)\n'
            'src/util/math.mh - 1 line(s)\n'
            'log: compile\n'
            'log: link\n'
            'log: done\n'
            'README.md file\n'
            'build.log file\n'
            'src dir\n'
            'not_found: no such file or directory\n'
        )

    def test_process(self):
        # M36: std:process -- no program arguments, no MAH_DEMO_NAME.
        out = run_file(example_path("process.mh"))
        self.assertEqual(
            out,
            'arguments: []\n'
            'hello, world\n'
            'from echo - exit code 0\n'
            'HI CHILD\n'
            'false 2 oops\n'
            'not_found: no-such-program-mah: no such file or directory\n'
        )

    def test_bytes(self):
        # M37: Bytes, hex/base64 and binary files (in a temporary directory).
        out = run_file(example_path("bytes.mh"))
        self.assertEqual(
            out,
            'Bytes[68 69] 2 104 none\n'
            'Bytes[48 69 21] some(Hi!) true\n'
            'Bytes[01 02 fa] Bytes[fb ff] Bytes[00 01 02 fa fb ff 48 69 21]\n'
            'sum: 759\n'
            '000102fafbff AAEC+vv/\n'
            'true some(Hi!)\n'
            'invalid_hex: from_hex: not valid hexadecimal: "xyz"\n'
            'none h\ufffdi some(2)\n'
            '8 true\n'
            'Bytes[00 01 02] 5 0\n'
        )

    def test_decorators(self):
        # M41b: decorators as metadata.
        self.assertEqual(
            run_file(example_path("decorators.mh")),
            "Fetch one user.\nGET /users/{id}\n[tag:path]\n",
        )

    def test_hooks(self):
        # M41c: rest parameters, function-item impls and the four hooks.
        self.assertEqual(
            run_file(example_path("hooks.mh")),
            "call greet\nhi ann!\ncall greet\nhi bob?\n2\n[HELLO]\n"
            "ann@example.com\nbob@example.com\ntoo young\nlogs every call\n"
            "a: 0 items, 0 options\nb: 3 items, 1 options\n",
        )

    def test_reflection(self):
        # M41a: type values, `##` docs, spread calls, std:reflect, json.decode.
        out = run_file(example_path("reflection.mh"))
        self.assertEqual(
            out,
            "User Number true false\n"
            "greet - Greets someone.\n"
            "  name has a default: false constant default: none\n"
            "  greeting has a default: true constant default: some(hello)\n"
            "  punctuation has a default: true constant default: some(!)\n"
            "User - A registered account.\n"
            "  name - The login name.\n"
            "  age - \n"
            "  email - Optional: a missing key decodes to `none`.\n"
            "welcome, ada!\n"
            "hello, bo?\n"
            "ada 37 none\n"
            "bad user: expected a Number for User.age, got String\n"
            "bad user: missing field 'name' for User\n"
        )


    def test_http_client(self):
        # M39: std:url, and std:http against a server the example runs itself
        self.assertEqual(
            run_file(example_path("http_client.mh")),
            "example.com 443 /docs/guide lang=en install\n"
            "https://example.com/api/v2?q=1\n"
            "q=mah+lang&page=2 café\n"
            "200 application/json GET /hello?name=mah HTTP/1.1\n"
            "true POST /items HTTP/1.1\n"
            "unsupported_scheme\n",
        )


if __name__ == "__main__":
    unittest.main()
