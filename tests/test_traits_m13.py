"""End-to-end behavioral tests for M13: lifting three M12 trait
limitations -- calling a closure stored in a struct/enum field
(`p.f(args)`), `detach` on any call chain (`detach obj.method(args)`), and
(separately, resolver-level here) the best-effort static type hints that
power the LSP's method hover/go-to-definition/completion. See the M13 spec
and `docs/TRAITS.md`'s "Known limitations" section.

Uses `run_source`/`parse_source` like `tests/test_traits.py` (never
re-plumbs the pipeline directly) for parts 1-2; resolver hint tests
resolve directly with `Parser`/`Resolver`, like `tests/support.compile_source`
does internally, since `resolver.method_call_index` isn't exposed any other
way.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.compiler.lexer import Lexer
from mah.compiler.parser import Parser
from mah.compiler.resolve import Resolver
from tests.support import parse_source, run_source


def _resolve(src: str) -> Resolver:
    parser = Parser(Lexer(src))
    program = parser.parse_program()
    resolver = Resolver()
    resolver.resolve_program(program)
    return resolver


class FieldClosureCallTests(unittest.TestCase):
    """M13 part 1: `p.f(args)` calling a closure stored in a field."""

    def test_closure_stored_in_a_struct_field_is_callable(self):
        src = (
            'struct Btn { label, on_click }\n'
            'let b = Btn { label: "ok", on_click: fn(x) { "clicked " + x } }\n'
            'print(b.on_click(3))'
        )
        self.assertEqual(run_source(src), "clicked 3\n")

    def test_method_wins_over_a_same_named_field(self):
        src = (
            'struct S { f }\n'
            'impl S { fn f(self) { "method" } }\n'
            'print(S { f: fn() { "field" } }.f())'
        )
        self.assertEqual(run_source(src), "method\n")

    def test_static_fn_of_the_same_name_does_not_block_the_field(self):
        src = (
            'struct S { f }\n'
            'impl S { fn f() { "static" } }\n'
            'print(S { f: fn() { "field" } }.f())\n'
            'print(S.f())'
        )
        self.assertEqual(run_source(src), "field\nstatic\n")

    def test_closure_field_captures_and_mutates_state(self):
        src = (
            'struct C { n, inc }\n'
            'let c = C { n: 0, inc: none }\n'
            'c.inc = fn() { c.n = c.n + 1 }\n'
            'c.inc()\n'
            'c.inc()\n'
            'print(c.n)'
        )
        self.assertEqual(run_source(src), "2\n")

    def test_enum_variant_field_closure(self):
        src = (
            'enum E { Handler { run } }\n'
            'let h = E.Handler { run: fn(a, b) { a + b } }\n'
            'print(h.run(2, 3))'
        )
        self.assertEqual(run_source(src), "5\n")

    def test_field_not_a_function_raises(self):
        src = "struct S { f }\nS { f: 5 }.f()"
        with self.assertRaises(Exception) as cm:
            run_source(src)
        self.assertIn("Field 'f' of 'S' is not a function (it holds a Number)", str(cm.exception))

    def test_field_closure_arity_error(self):
        src = "struct S { f }\nS { f: fn(a) { a } }.f()"
        with self.assertRaises(Exception) as cm:
            run_source(src)
        self.assertIn("'f' accepts 1 arguments but 0 was given", str(cm.exception))

    def test_trait_qualified_call_never_falls_back_to_a_field(self):
        src = "trait T { fn f(self) }\nstruct S { f }\nT.f(S { f: fn() { 1 } })"
        with self.assertRaises(Exception) as cm:
            run_source(src)
        self.assertIn("does not implement trait 'T'", str(cm.exception))

    def test_ambiguity_still_wins_over_the_field(self):
        src = (
            "trait A { fn f(self) }\n"
            "trait B { fn f(self) }\n"
            "struct S { f }\n"
            "impl A for S { fn f(self) { 1 } }\n"
            "impl B for S { fn f(self) { 2 } }\n"
            "S { f: fn() { 3 } }.f()"
        )
        with self.assertRaises(Exception) as cm:
            run_source(src)
        self.assertIn("ambiguous", str(cm.exception))


class DetachAnyCallChainTests(unittest.TestCase):
    """M13 part 2: `detach` on any call chain, not just `name(args)`."""

    def test_dynamic_method_detach_and_await(self):
        src = """
        struct W { v }
        impl W { fn slow(self, k) { sleep_async(10); self.v * k } }
        let w = W { v: 7 }
        let p = detach w.slow(3)
        print("started")
        print(p.await)
        """
        self.assertEqual(run_source(src), "started\n21\n")

    def test_inline_await_on_a_detached_method_chain(self):
        src = """
        struct W { v }
        impl W { fn me(self) { self } fn get(self) { sleep_async(5); self.v } }
        let w = W { v: 4 }
        print(detach w.me().get().await)
        """
        self.assertEqual(run_source(src), "4\n")

    def test_static_path_detach(self):
        src = """
        struct P { v }
        impl P { fn make(v) { sleep_async(5); P { v: v } } }
        let p = detach P.make(9)
        print(p.await.v)
        """
        self.assertEqual(run_source(src), "9\n")

    def test_trait_qualified_detach(self):
        src = """
        trait T { fn go(self) }
        struct S { }
        impl T for S { fn go(self) { sleep_async(5); "went" } }
        let s = S {}
        print((detach T.go(s)).await)
        """
        self.assertEqual(run_source(src), "went\n")

    def test_native_method_detach(self):
        src = 'let n = 5\nprint((detach n.to_string()).await + "!")'
        self.assertEqual(run_source(src), "5!\n")

    def test_field_closure_detach(self):
        src = """
        struct S { f }
        let s = S { f: fn(x) { sleep_async(5); x + 1 } }
        print((detach s.f(1)).await)
        """
        self.assertEqual(run_source(src), "2\n")

    def test_two_detached_method_calls_interleave(self):
        src = """
        struct W { name, ms }
        impl W { fn run(self) { sleep_async(self.ms); print(self.name); self.ms } }
        let a = W { name: "slow", ms: 30 }
        let b = W { name: "fast", ms: 5 }
        let pa = detach a.run()
        let pb = detach b.run()
        print(pa.await + pb.await)
        """
        self.assertEqual(run_source(src), "fast\nslow\n35\n")

    def test_detach_of_a_field_access_detaches_the_whole_chain(self):
        # Intentional change: this used to be a parse error (no call to
        # detach). Now any expression can be detached.
        src = "struct S { f }\nlet s = S { f: 1 }\nlet p = detach s.f\nprint(p.await)"
        self.assertEqual(run_source(src), "1\n")


class ResolverTypeHintTests(unittest.TestCase):
    """M13's resolver-level 'method indexes'/type hints -- inspect
    `resolver.method_call_index` directly (there's no other way to observe
    this purely advisory, LSP-facing bookkeeping)."""

    def test_known_receiver_type_narrows_to_one_candidate(self):
        src = (
            "struct R { w }\n"
            "impl R { fn new(w) { Self { w: w } } fn area(self) { self.w } }\n"
            "let r = R.new(2)\n"
            "r.area()"
        )
        resolver = _resolve(src)
        pos = src.index("area", src.index("r.area"))
        info = resolver.method_call_index[pos]
        self.assertEqual(info["receiver_type"], "R")
        self.assertEqual(info["candidates"], [("impl", "R", None)])

    def test_unknown_receiver_lists_every_implementer(self):
        src = (
            "trait S { fn area(self) }\n"
            "struct A { }\n"
            "struct B { }\n"
            "impl S for A { fn area(self) { 1 } }\n"
            "impl S for B { fn area(self) { 2 } }\n"
            "fn f(x) { x.area() }"
        )
        resolver = _resolve(src)
        pos = src.index("area", src.index("x.area"))
        info = resolver.method_call_index[pos]
        self.assertIsNone(info["receiver_type"])
        self.assertEqual(info["candidates"], [("impl", "A", "S"), ("impl", "B", "S")])

    def test_reassignment_drops_the_hint(self):
        src = (
            "struct A { }\n"
            "struct B { }\n"
            "impl A { fn m(self) { 1 } }\n"
            "impl B { fn m(self) { 2 } }\n"
            "let x = A {}\n"
            "x = B {}\n"
            "x.m()"
        )
        resolver = _resolve(src)
        pos = src.rindex("m()")
        info = resolver.method_call_index[pos]
        self.assertIsNone(info["receiver_type"])
        self.assertEqual(len(info["candidates"]), 2)

    def test_self_inside_a_trait_default_body(self):
        src = "trait T { fn a(self) fn b(self) { self.a() } }"
        resolver = _resolve(src)
        pos = src.index("a", src.index("self.a"))
        info = resolver.method_call_index[pos]
        self.assertEqual(info["candidates"], [("trait", "T")])


class DeeperScenarioTests(unittest.TestCase):
    """Added during verification, beyond the spec's own test list."""

    def test_detach_self_method_twice_inside_a_method(self):
        src = """
        struct W { v }
        impl W {
            fn slow(self) { sleep_async(5); self.v }
            fn twice(self) { let a = detach self.slow(); let b = detach self.slow(); a.await + b.await }
        }
        print(W { v: 4 }.twice())
        """
        self.assertEqual(run_source(src), "8\n")

    def test_detached_method_runs_its_defer_on_completion(self):
        src = """
        struct W { }
        impl W { fn go(self) { defer print("cleanup"); sleep_async(5); "done" } }
        let w = W {}
        let p = detach w.go()
        print("main")
        print(p.await)
        """
        self.assertEqual(run_source(src), "main\ncleanup\ndone\n")

    def test_field_closure_returning_a_closure(self):
        src = """
        struct S { mk }
        let s = S { mk: fn(a) { fn(b) { a * b } } }
        let f = s.mk(6)
        print(f(7))
        """
        self.assertEqual(run_source(src), "42\n")

    def test_method_call_on_awaited_detached_method_result(self):
        src = """
        struct W { v }
        impl W { fn get(self) { sleep_async(2); self } fn val(self) { self.v } }
        let w = W { v: 8 }
        print((detach w.get()).await.val())
        """
        self.assertEqual(run_source(src), "8\n")


class CompletionRegressionTests(unittest.TestCase):
    """M13 changed the namespace-completion step to fall through for
    non-namespace receivers -- make sure real namespaces still complete
    exclusively, and that `self.` inside an impl completes that type."""

    def _complete(self, text, needle, path=None):
        from mah.lsp import analysis

        idx = text.index(needle) + len(needle)
        line = text.count("\n", 0, idx)
        col = idx - (text.rfind("\n", 0, idx) + 1)
        return sorted(i["label"] for i in analysis.get_completions(text, path, line, col))

    def test_namespace_completion_still_exclusive(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "import_demo.mh")
        with open(path) as f:
            text = f.read()
        self.assertEqual(self._complete(text, "math.", path), ["answer", "cube", "is_even", "square"])

    def test_self_completion_inside_impl(self):
        src = "struct P { x }\nimpl P {\n\tfn a(self) { 1 }\n\tfn b(self) { self. }\n}\n"
        self.assertEqual(self._complete(src, "self."), ["a", "b", "x"])


if __name__ == "__main__":
    unittest.main()
