"""End-to-end tests for M19: `Vector` and `Map` -- literals (`[1, 2]`,
`["a": 1]`, `[]`, `[:]`), indexing through the `Index`/`IndexAssign`
system traits (`x[k]`, `x[k] = v`), the native methods, iteration (prelude),
`Map.entries()`, and bytecode 1.3's `vector`/`map` opcodes.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode
from mah.bytecode.encode import encode
from mah.bytecode.format import MahcFormatError
from mah.bytecode.program import FunctionDecl, Instr, Program
from mah.code_interpreter import run_program
from mah.compiler.ast_nodes import AssignStmt, ExprStmt, Index, LetStmt, MapLit, VectorLit
from mah.preprocessor import preprocess
from tests.support import compile_program, parse_source, run_source


def parse(source: str):
    program, parser = parse_source(source)
    if parser.errors:
        raise SyntaxError(parser.errors[0][0])
    return program


class ParsingTests(unittest.TestCase):
    def test_literals(self):
        cases = {
            "[]": (VectorLit, 0),
            "[1]": (VectorLit, 1),
            "[1, 2, 3,]": (VectorLit, 3),
            "[:]": (MapLit, 0),
            '["a": 1]': (MapLit, 1),
            '["a": 1, "b": 2,]': (MapLit, 2),
        }
        for src, (node_type, count) in cases.items():
            with self.subTest(src=src):
                (stmt,) = parse(f"let x = {src}")
                self.assertIsInstance(stmt.value, node_type)
                items = stmt.value.items if node_type is VectorLit else stmt.value.pairs
                self.assertEqual(len(items), count)

    def test_index_and_index_assignment(self):
        (stmt,) = parse("print(a.b[0][1].c)")
        field = stmt.args[0]
        self.assertIsInstance(field.obj, Index)
        self.assertIsInstance(field.obj.obj, Index)
        (stmt,) = parse('m["k"] = 1')
        self.assertIsInstance(stmt, AssignStmt)
        self.assertIsInstance(stmt.target, Index)

    def test_bracket_on_the_next_line_starts_a_new_statement(self):
        stmts = parse("foo()\n[1, 2].len()")
        self.assertEqual(len(stmts), 2)
        self.assertIsInstance(stmts[1].value.obj, VectorLit)

    def test_struct_literals_allowed_inside_brackets_in_a_condition(self):
        stmts = parse("if [P { x: 1 }].len() > 0 { }\nfor let v in [P { x: 1 }] { }")
        self.assertEqual(len(stmts), 2)

    def test_mixed_literal_errors(self):
        with self.assertRaisesRegex(SyntaxError, r"Every item in a Map literal needs 'key: value'"):
            parse('let m = ["a": 1, 2]')
        with self.assertRaisesRegex(SyntaxError, r"A Vector literal can't contain 'key: value' items"):
            parse('let v = [1, "a": 2]')

    def test_break_value_can_be_a_vector_literal(self):
        (stmt,) = parse("let x = while true { break [1] }")
        self.assertIsInstance(stmt.value.body.stmts[0].value, VectorLit)


class VectorTests(unittest.TestCase):
    def test_literal_print_and_len(self):
        self.assertEqual(run_source("let v = [1, 2, 3]\nprint(v, v.len(), [], [].len())"), "[1, 2, 3] 3 [] 0\n")

    def test_zero_indexed_read_and_write(self):
        src = "let v = [10, 20, 30]\nv[1] = 21\nprint(v[0], v[1], v[2])"
        self.assertEqual(run_source(src), "10 21 30\n")

    def test_missing_index_reads_none(self):
        src = "let v = [1, 2]\nprint(v[2], v[-3], v[0.5], v[100], [][-1])"
        self.assertEqual(run_source(src), "none none none none none\n")

    def test_negative_indices_count_from_the_end(self):
        src = """
        let v = [10, 20, 30]
        print(v[-1], v[-2], v[-3])
        v[-1] = 31
        v[-3] = 11
        print(v)
        """
        self.assertEqual(run_source(src), "30 20 10\n[11, 20, 31]\n")

    def test_push_pop_both_ends(self):
        src = """
        let v = [2]
        v.push(3)
        v.push_start(1)
        print(v)
        print(v.pop(), v.pop_start(), v)
        print(v.pop(), v.pop(), v.pop_start(), v)
        """
        self.assertEqual(run_source(src), "[1, 2, 3]\n3 1 [2]\n2 none none []\n")

    def test_push_returns_none(self):
        self.assertEqual(run_source("print([].push(1))"), "none\n")

    def test_reference_semantics(self):
        src = "let a = [1]\nlet b = a\nb.push(2)\nprint(a)\nprint([1] == [1], a == b)"
        self.assertEqual(run_source(src), "[1, 2]\nfalse true\n")

    def test_nested_and_mixed_values(self):
        src = 'let v = [[1, 2], "s", true, none, some(1), fn(x) { x }]\nprint(v)\nprint(v[0][1])'
        self.assertEqual(run_source(src), "[[1, 2], s, true, none, some(1), <fn>]\n2\n")

    def test_index_assignment_on_nested(self):
        src = "let grid = [[0, 0], [0, 0]]\ngrid[1][0] = 5\nprint(grid)"
        self.assertEqual(run_source(src), "[[0, 0], [5, 0]]\n")

    def test_evaluation_order_of_index_assignment(self):
        src = """
        fn log(x, v) { print(x); v }
        let vec = [0]
        log("target", vec)[log("key", 0)] = log("value", 1)
        print(vec)
        """
        self.assertEqual(run_source(src), "value\ntarget\nkey\n[1]\n")

    def test_iteration(self):
        src = "for let v, let i in [10, 20] { print(i, v) }"
        self.assertEqual(run_source(src), "0 10\n1 20\n")

    def test_iteration_sees_items_pushed_during_the_loop(self):
        src = "let v = [1]\nfor let x in v { if x < 3 { v.push(x + 1) } print(x) }"
        self.assertEqual(run_source(src), "1\n2\n3\n")

    def test_adapters(self):
        src = "print([1, 2, 3, 4].filter(fn(x) { x % 2 == 0 }).map(fn(x) { x * 10 }).reduce(fn(a, b) { a + b }))"
        self.assertEqual(run_source(src), "60\n")

    def test_static_path_calls(self):
        self.assertEqual(run_source("print(Vector.len([1, 2]))"), "2\n")

    def test_string_concatenation_formats_the_vector(self):
        self.assertEqual(run_source('print("v=" + [1, 2])'), "v=[1, 2]\n")


class SliceTests(unittest.TestCase):
    SRC = "let x = [0, 1, 2, 3, 4, 5]\n"

    def _check(self, expr, expected):
        self.assertEqual(run_source(self.SRC + f"print({expr})"), expected + "\n")

    def test_range_kinds(self):
        self._check("x[1..3]", "[1, 2]")
        self._check("x[1..=3]", "[1, 2, 3]")
        self._check("x[2..]", "[2, 3, 4, 5]")
        self._check("x[..2]", "[0, 1]")
        self._check("x[..=2]", "[0, 1, 2]")

    def test_bounds_past_either_end_are_clamped(self):
        self._check("x[1..10]", "[1, 2, 3, 4, 5]")
        self._check("x[..40]", "[0, 1, 2, 3, 4, 5]")
        self._check("x[10..]", "[]")
        self._check("x[-100..2]", "[0, 1]")
        self._check("x[4..2]", "[]")
        self._check("[][0..5]", "[]")

    def test_negative_bounds_count_from_the_end(self):
        self._check("x[-2..]", "[4, 5]")
        self._check("x[..-1]", "[0, 1, 2, 3, 4]")
        self._check("x[..=-1]", "[0, 1, 2, 3, 4, 5]")
        self._check("x[-3..-1]", "[3, 4]")

    def test_slice_is_a_copy(self):
        src = self.SRC + "let s = x[1..3]\ns[0] = 99\ns.push(7)\nprint(x, s)"
        self.assertEqual(run_source(src), "[0, 1, 2, 3, 4, 5] [99, 2, 7]\n")

    def test_slice_of_a_variable_range(self):
        src = self.SRC + "let r = 1..3\nlet n = 2\nprint(x[r], x[n..n + 2])"
        self.assertEqual(run_source(src), "[1, 2] [2, 3]\n")

    def test_bad_bounds(self):
        with self.assertRaisesRegex(Exception, r"Vector slice bounds must be integer Numbers, got 1.5"):
            run_source("print([1][1.5..2])")
        with self.assertRaisesRegex(Exception, r"Vector slice bounds must be integer Numbers, got String"):
            run_source('print([1]["a".."b"])')

    def test_assigning_to_a_slice_is_an_error(self):
        with self.assertRaisesRegex(Exception, r"Can't assign to a Vector slice"):
            run_source("let v = [1, 2]\nv[0..1] = 5")


class ReduceCollectTests(unittest.TestCase):
    def test_reduce_without_arguments_collects_into_a_vector(self):
        src = 'print((1..5).reduce(), "abc".reduce(), [1, 2].map(fn(v) { v * 3 }).reduce())'
        self.assertEqual(run_source(src), "[1, 2, 3, 4] [a, b, c] [3, 6]\n")

    def test_empty_iterable_collects_to_an_empty_vector(self):
        self.assertEqual(run_source("print((3..3).reduce(), [].reduce())"), "[] []\n")

    def test_collected_vector_is_a_real_vector(self):
        src = 'let v = ["b": 1, "a": 2].reduce()\nv.push("c")\nprint(v, v[-1], v.len())'
        self.assertEqual(run_source(src), "[b, a, c] c 3\n")

    def test_collecting_a_vector_copies_it(self):
        src = "let a = [1]\nlet b = a.reduce()\nb.push(2)\nprint(a, b)"
        self.assertEqual(run_source(src), "[1] [1, 2]\n")

    def test_reduce_with_a_function_is_unchanged(self):
        self.assertEqual(run_source("print((1..=4).reduce(fn(a, b) { a + b }), [].reduce(fn(a, b) { a }))"), "10 none\n")

    def test_initial_without_a_function_is_an_error(self):
        with self.assertRaisesRegex(Exception, r"reduce_needs_a_function_when_given_an_initial_value"):
            run_source("print((1..3).reduce(initial: 5))")


class CopyTests(unittest.TestCase):
    def test_vector_copy_is_independent(self):
        src = "let a = [1, 2]\nlet b = a.copy()\nb.push(3)\nb[0] = 9\nprint(a, b, a == b)"
        self.assertEqual(run_source(src), "[1, 2] [9, 2, 3] false\n")

    def test_map_copy_is_independent_and_keeps_order(self):
        src = """
        let a = ["x": 1, "y": 2]
        let b = a.copy()
        b["z"] = 3
        b["x"] = 10
        b.remove("y")
        print(a, b)
        """
        self.assertEqual(run_source(src), "[x: 1, y: 2] [x: 10, z: 3]\n")

    def test_copy_is_shallow(self):
        src = "let a = [[1]]\nlet b = a.copy()\nb[0].push(2)\nprint(a)"
        self.assertEqual(run_source(src), "[[1, 2]]\n")
        src = 'let a = ["k": [1]]\nlet b = a.copy()\nb["k"].push(2)\nprint(a)'
        self.assertEqual(run_source(src), "[k: [1, 2]]\n")

    def test_copy_of_empty(self):
        self.assertEqual(run_source("print([].copy(), [:].copy())"), "[] [:]\n")

    def test_deep_copy(self):
        src = """
        struct P { xs }
        let a = [[1], ["k": P { xs: [2] }], some([3])]
        let b = a.copy(deep: true)
        b[0].push(9)
        b[1]["k"].xs.push(8)
        match b[2] { some(v) => { v.push(4) } }
        print(a)
        print(b)
        """
        self.assertEqual(
            run_source(src),
            "[[1], [k: P { xs: [2] }], some([3])]\n[[1, 9], [k: P { xs: [2, 8] }], some([3, 4])]\n",
        )

    def test_deep_map_copy(self):
        src = 'let m = ["v": [1]]\nlet n = m.copy(deep: true)\nn["v"].push(2)\nprint(m, n)'
        self.assertEqual(run_source(src), "[v: [1]] [v: [1, 2]]\n")

    def test_deep_copy_keeps_sharing_and_handles_cycles(self):
        src = """
        let inner = [1]
        let a = [inner, inner]
        let b = a.copy(deep: true)
        print(b[0] == b[1], b[0] == inner)
        let cyc = [1]
        cyc.push(cyc)
        let c = cyc.copy(deep: true)
        print(c[1] == c, c[1] == cyc)
        """
        self.assertEqual(run_source(src), "true false\ntrue false\n")

    def test_deep_copy_shares_functions_and_none(self):
        src = "let f = fn() { 1 }\nlet v = [f, none]\nlet w = v.copy(deep: true)\nprint(w[0] == f, w[1] == none)"
        self.assertEqual(run_source(src), "true true\n")

    def test_deep_can_be_positional_or_false(self):
        src = "let a = [[1]]\nlet b = a.copy(true)\nlet c = a.copy(deep: false)\nc[0].push(2)\nprint(a, b)"
        self.assertEqual(run_source(src), "[[1, 2]] [[1]]\n")

    def test_static_path_with_keyword(self):
        self.assertEqual(run_source('print(Map.copy(["a": [1]], deep: true))'), "[a: [1]]\n")

    def test_copy_argument_errors(self):
        with self.assertRaisesRegex(Exception, r"method 'copy' got an unexpected keyword argument 'depth'"):
            run_source("[1].copy(depth: true)")
        with self.assertRaisesRegex(Exception, r"method 'copy' takes at most 1 positional arguments but 2 were given"):
            run_source("[1].copy(true, 1)")
        with self.assertRaisesRegex(Exception, r"method 'copy' got multiple values for argument 'deep'"):
            run_source("[1].copy(true, deep: true)")

    def test_other_natives_still_reject_keywords(self):
        with self.assertRaisesRegex(Exception, r"method 'push' got an unexpected keyword argument 'value'"):
            run_source("[].push(value: 1)")


class StringIndexTests(unittest.TestCase):
    def test_characters(self):
        src = 'let s = "héllo"\nprint(s[0], s[1], s[4], s[-1], s[-5])'
        self.assertEqual(run_source(src), "h é o o h\n")

    def test_missing_index_reads_none(self):
        src = 'let s = "ab"\nprint(s[2], s[-3], s[0.5], ""[0])'
        self.assertEqual(run_source(src), "none none none none\n")

    def test_slices(self):
        src = 'let s = "hello"\nprint(s[1..3], s[..2], s[2..], s[-3..], s[..=-2], s[1..100])'
        self.assertEqual(run_source(src), "el he llo llo hell ello\n")

    def test_empty_slices(self):
        src = 'let s = "hello"\nprint("|" + s[9..] + s[3..1] + ""[0..2] + "|")'
        self.assertEqual(run_source(src), "||\n")

    def test_result_is_a_string(self):
        src = 'let s = "abc"\nprint(s[0] + s[1..].len(), s[0..2] == "ab")'
        self.assertEqual(run_source(src), "a2 true\n")

    def test_trait_qualified_call(self):
        self.assertEqual(run_source('print(Index.index("ab", 1))'), "b\n")

    def test_strings_are_immutable(self):
        with self.assertRaisesRegex(Exception, r"'String' does not implement trait 'IndexAssign'"):
            run_source('let s = "ab"\ns[0] = "c"')

    def test_bad_indices(self):
        with self.assertRaisesRegex(Exception, r"String index must be a Number, got String"):
            run_source('print("ab"["x"])')
        with self.assertRaisesRegex(Exception, r"String slice bounds must be integer Numbers, got 0.5"):
            run_source('print("ab"[0.5..1])')

    def test_cannot_reimplement_index_for_string(self):
        with self.assertRaisesRegex(Exception, r"Cannot implement built-in trait 'Index' for built-in type 'String'"):
            run_source("impl Index for String { fn index(self, k) { 1 } }")


class MapTests(unittest.TestCase):
    def test_literal_read_write(self):
        src = """
        let m = ["a": 1, "b": 2]
        m["c"] = 3
        m["a"] = 10
        print(m, m["a"], m.len())
        """
        self.assertEqual(run_source(src), "[a: 10, b: 2, c: 3] 10 3\n")

    def test_empty_map(self):
        self.assertEqual(run_source("let m = [:]\nprint(m, m.len())\nm[1] = 2\nprint(m)"), "[:] 0\n[1: 2]\n")

    def test_missing_key_reads_none(self):
        self.assertEqual(run_source('print(["a": 1]["b"])'), "none\n")

    def test_key_types_stay_distinct(self):
        src = 'let m = [1: "number", "1": "string", true: "bool"]\nprint(m[1], m["1"], m[true], m.len())'
        self.assertEqual(run_source(src), "number string bool 3\n")

    def test_equal_numbers_are_the_same_key(self):
        src = "let m = [1: 1]\nm[1.0] = 2\nprint(m, m.len())"
        self.assertEqual(run_source(src), "[1: 2] 1\n")

    def test_duplicate_literal_keys_last_wins_first_position(self):
        self.assertEqual(run_source('print(["a": 1, "b": 2, "a": 3])'), "[a: 3, b: 2]\n")

    def test_has_remove(self):
        src = """
        let m = ["a": 1, "b": 2]
        print(m.has("a"), m.has("z"))
        print(m.remove("a"), m.remove("a"), m, m.has("a"))
        """
        self.assertEqual(run_source(src), "true false\n1 none [b: 2] false\n")

    def test_keys_values_entries(self):
        src = """
        let m = ["x": 1, "y": 2]
        print(m.keys(), m.values())
        for let e in m.entries() { print(e.key, e.value) }
        print(m.entries())
        """
        self.assertEqual(
            run_source(src),
            "[x, y] [1, 2]\nx 1\ny 2\n[MapEntry { key: x, value: 1 }, MapEntry { key: y, value: 2 }]\n",
        )

    def test_entry_pattern(self):
        src = 'for let e in ["k": 5].entries() { match e { MapEntry { key, value } => { print(key, value) } } }'
        self.assertEqual(run_source(src), "k 5\n")

    def test_for_loop_gives_keys_in_insertion_order(self):
        src = 'let m = ["b": 1, "a": 2]\nm["c"] = 3\nfor let k, let i in m { print(i, k, m[k]) }'
        self.assertEqual(run_source(src), "0 b 1\n1 a 2\n2 c 3\n")

    def test_removing_while_iterating_is_safe(self):
        src = 'let m = ["a": 1, "b": 2]\nfor let k in m { m.remove(k) }\nprint(m)'
        self.assertEqual(run_source(src), "[:]\n")

    def test_values_and_keys_are_snapshots(self):
        src = 'let m = ["a": 1]\nlet ks = m.keys()\nm["b"] = 2\nprint(ks, m.keys())'
        self.assertEqual(run_source(src), "[a] [a, b]\n")

    def test_map_of_vectors(self):
        src = 'let m = ["xs": [1]]\nm["xs"].push(2)\nprint(m["xs"][1])'
        self.assertEqual(run_source(src), "2\n")

    def test_iterable_methods_on_map(self):
        src = 'print(["a": 1, "b": 2].map(fn(k) { k + "!" }).reduce(fn(a, b) { a + b }))'
        self.assertEqual(run_source(src), "a!b!\n")


class UserIndexTests(unittest.TestCase):
    def test_user_type_implements_index_and_index_assign(self):
        src = """
        struct Point { x, y }
        struct Grid { w, cells }
        impl Index for Grid {
            fn index(self, p) { self.cells[p.y * self.w + p.x] }
        }
        impl IndexAssign for Grid {
            fn index_assign(self, p, v) { self.cells[p.y * self.w + p.x] = v }
        }
        let g = Grid { w: 2, cells: [0, 0, 0, 0] }
        g[Point { x: 1, y: 1 }] = 9
        print(g[Point { x: 1, y: 1 }], g.cells)
        """
        self.assertEqual(run_source(src), "9 [0, 0, 0, 9]\n")

    def test_trait_qualified_calls(self):
        self.assertEqual(run_source("print(Index.index([5, 6], 1))"), "6\n")


class ErrorTests(unittest.TestCase):
    def test_indexing_a_non_indexable(self):
        with self.assertRaisesRegex(Exception, r"'Number' does not implement trait 'Index'"):
            run_source("print(5[0])")
        with self.assertRaisesRegex(Exception, r"'String' does not implement trait 'IndexAssign'"):
            run_source('let s = "ab"\ns[0] = "c"')

    def test_vector_index_must_be_a_number(self):
        with self.assertRaisesRegex(Exception, r"Vector index must be a Number, got String"):
            run_source('print([1]["a"])')

    def test_vector_write_out_of_range(self):
        with self.assertRaisesRegex(Exception, r"Vector index 3 is out of range for a Vector of length 1"):
            run_source("let v = [1]\nv[3] = 2")
        # -1 is valid now (the last item); -2 is past the start
        with self.assertRaisesRegex(Exception, r"Vector index -2 is out of range for a Vector of length 1"):
            run_source("let v = [1]\nv[-2] = 2")
        with self.assertRaisesRegex(Exception, r"Vector index -1 is out of range for a Vector of length 0"):
            run_source("let v = []\nv[-1] = 2")

    def test_bad_map_keys(self):
        for src in ("print([[1]: 2])", "let m = [:]\nm[[1]] = 1", "print([:][none])", "print([:].has([:]))"):
            with self.subTest(src=src):
                with self.assertRaisesRegex(Exception, r"Map keys must be a String, Number, or Bool"):
                    run_source(src)

    def test_method_arity(self):
        with self.assertRaisesRegex(Exception, r"method 'push' accepts 1 arguments but 0 was given"):
            run_source("[].push()")

    def test_builtin_names_are_reserved(self):
        for src in ("struct Map { x }", "enum Vector { A }", "trait Index { fn index(self, k) }"):
            with self.subTest(src=src):
                with self.assertRaises(Exception):
                    run_source(src)

    def test_cannot_implement_builtin_trait_for_builtin_type(self):
        with self.assertRaisesRegex(Exception, r"Cannot implement built-in trait 'Index' for built-in type 'Number'"):
            run_source("impl Index for Number { fn index(self, k) { 1 } }")

    def test_index_impl_must_have_the_right_shape(self):
        with self.assertRaises(Exception):
            run_source("struct G { }\nimpl Index for G { fn index(self) { 1 } }")


class BytecodeTests(unittest.TestCase):
    def test_literals_compile_to_vector_and_map_opcodes(self):
        program = compile_program(text='let v = [1, 2]\nlet m = ["a": 1]\nprint(v, m)')
        ops = [instr.op for instr in program.code]
        self.assertIn("vector", ops)
        self.assertIn("map", ops)
        self.assertEqual(program.minor, 3)
        self.assertEqual(decode(encode(program)), program)

    def test_prelude_only_when_needed(self):
        self.assertIsNone(preprocess(None, "let v = [1]\nv[0] = 2\nprint(v.len())").prelude_start)
        self.assertIsNotNone(preprocess(None, 'print(["a": 1].entries())').prelude_start)
        self.assertIsNotNone(preprocess(None, "for let x in [1] { }").prelude_start)

    def _program(self, code, minor=3):
        fns = [FunctionDecl(entry=0, slot_count=3, param_count=0, name=None)]
        return Program(
            strings=[], constants=[], types=[], natives=[], functions=fns, code=code, debug=None, minor=minor
        )

    def test_minor_2_file_with_vector_is_rejected(self):
        program = self._program([Instr("vector", ((), (0, 0))), Instr("halt", ())], minor=2)
        with self.assertRaisesRegex(MahcFormatError, r"requires minor version >= 3"):
            decode(encode(program))

    def test_map_needs_an_even_number_of_addresses(self):
        program = self._program([Instr("map", (((0, 0),), (0, 1))), Instr("halt", ())])
        with self.assertRaisesRegex(MahcFormatError, r"even number of addresses"):
            decode(encode(program))

    def test_hand_built_program_runs(self):
        program = self._program([Instr("map", ((), (0, 0))), Instr("vector", (((0, 0),), (0, 1))), Instr("halt", ())])
        run_program(decode(encode(program)))  # no error


if __name__ == "__main__":
    unittest.main()
