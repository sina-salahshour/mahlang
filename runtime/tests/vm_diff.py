#!/usr/bin/env python3
"""Differential test: every `examples/*.mh` file, plus a batch of small
inline programs covering runtime-error message paths, plus a batch of
malformed `.mahc` files (built by editing bytes of a compiled file, like
`tests/test_bytecode.py` does) -- run through both `python3 -m mah runc`
and `runtime/target/release/mah-vm run` with the same stdin, and compare
exit code, stdout, and stderr exactly.

Usage (from the repo root or anywhere):
    python3 runtime/tests/vm_diff.py [--vm PATH] [-v]

This is the real acceptance test for the Rust VM port (see the spec this
was written from: runtime/src/{decode,vm,bundle,main}.rs must behave
identically to `mah/code_interpreter.py` + `mah/bytecode/decode.py` +
`mah/natives.py`). Python 3 stdlib only -- no pytest/unittest dependency
(just enough structure to print a pass/fail summary and every mismatch).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLES_DIR = os.path.join(REPO_ROOT, "examples")
DEFAULT_VM = os.path.join(REPO_ROOT, "runtime", "target", "release", "mah-vm")

# Example programs that call `input()` at least once -- fed "12\n" on
# stdin; everything else gets empty stdin.
EXAMPLES_NEEDING_INPUT = {
    "binary_to_decimal.mh",
    "decimal_to_binary.mh",
    "new_decimal_to_binary.mh",
}


class Result:
    def __init__(self, name: str):
        self.name = name
        self.passed = True
        self.detail = ""

    def fail(self, detail: str) -> None:
        self.passed = False
        self.detail = detail


def run_python_mahc(mahc_path: str, stdin_data: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mah", "runc", mahc_path],
        input=stdin_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": REPO_ROOT},
        timeout=30,
    )


def run_rust_mahc(vm_path: str, mahc_path: str, stdin_data: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [vm_path, "run", mahc_path],
        input=stdin_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        timeout=30,
    )


def compare(name: str, py: subprocess.CompletedProcess, rs: subprocess.CompletedProcess) -> Result:
    r = Result(name)
    problems = []
    if py.returncode != rs.returncode:
        problems.append(f"exit code: python={py.returncode} rust={rs.returncode}")
    if py.stdout != rs.stdout:
        problems.append(f"stdout differs:\n  python={py.stdout!r}\n  rust  ={rs.stdout!r}")
    if py.stderr != rs.stderr:
        problems.append(f"stderr differs:\n  python={py.stderr!r}\n  rust  ={rs.stderr!r}")
    if problems:
        r.fail("; ".join(problems))
    return r


def compile_example(path: str, tmpdir: str) -> str:
    out = os.path.join(tmpdir, os.path.basename(path) + ".mahc")
    subprocess.run(
        [sys.executable, "-m", "mah", "build", path, "-o", out],
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": REPO_ROOT},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return out


def compile_source(text: str, tmpdir: str, name: str, target: str = "debug") -> str:
    src = os.path.join(tmpdir, f"{name}.mh")
    with open(src, "w") as f:
        f.write(text)
    out = os.path.join(tmpdir, f"{name}.mahc")
    subprocess.run(
        [sys.executable, "-m", "mah", "build", src, "-o", out, "--target", target],
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": REPO_ROOT},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return out


def run_examples(vm_path: str, tmpdir: str, verbose: bool) -> list[Result]:
    results = []
    for fname in sorted(f for f in os.listdir(EXAMPLES_DIR) if f.endswith(".mh")):
        path = os.path.join(EXAMPLES_DIR, fname)
        try:
            mahc = compile_example(path, tmpdir)
        except subprocess.CalledProcessError as e:
            r = Result(fname)
            r.fail(f"failed to compile: {e.stderr.decode('utf-8', 'replace')}")
            results.append(r)
            continue
        stdin_data = b"12\n" if fname in EXAMPLES_NEEDING_INPUT else b""
        py = run_python_mahc(mahc, stdin_data)
        rs = run_rust_mahc(vm_path, mahc, stdin_data)
        r = compare(fname, py, rs)
        results.append(r)
        if verbose:
            print(f"  {'ok' if r.passed else 'FAIL'}  {fname}")
    return results


# ---------------------------------------------------------------------------
# ~30 small inline programs covering runtime-error message paths.
# ---------------------------------------------------------------------------

RUNTIME_ERROR_PROGRAMS: list[tuple[str, str, bytes]] = [
    ("call_too_few_args", """
fn f(a, b) { return a + b }
f(1)
""", b""),
    ("call_too_many_args", """
fn f(a, b) { return a + b }
f(1, 2, 3)
""", b""),
    ("call_non_function", """
let x = 5
x()
""", b""),
    ("detach_non_function", """
let x = 5
detach x()
""", b""),
    ("kwargs_unexpected", """
fn f(a) { return a }
f(a: 1, b: 2)
""", b""),
    ("kwargs_multiple_values", """
fn f(a) { return a }
f(1, a: 2)
""", b""),
    ("kwargs_missing_required", """
fn f(a, b) { return a + b }
f(a: 1)
""", b""),
    ("kwargs_too_many_positional", """
fn f(a, b = 2) { return a + b }
f(1, 2, 3)
""", b""),
    ("method_no_such_method", """
struct Point { x, y }
let p = Point { x: 1, y: 2 }
p.frobnicate()
""", b""),
    ("method_field_not_function", """
struct Box { value }
let b = Box { value: 5 }
b.value()
""", b""),
    ("static_function_as_method", """
struct S {}
trait T { fn f() {} }
impl T for S { fn f() { return 1 } }
let s = S {}
s.f()
""", b""),
    ("ambiguous_trait_method", """
struct S {}
trait A { fn go(self) {} }
trait B { fn go(self) {} }
impl A for S { fn go(self) { return 1 } }
impl B for S { fn go(self) { return 2 } }
let s = S {}
s.go()
""", b""),
    ("no_field", """
struct Point { x, y }
let p = Point { x: 1, y: 2 }
print(p.z)
""", b""),
    ("setfield_no_field", """
struct Point { x, y }
let p = Point { x: 1, y: 2 }
p.z = 3
""", b""),
    ("getfield_non_struct", """
let x = 5
print(x.y)
""", b""),
    ("division_by_zero", """
let x = 1 / 0
""", b""),
    ("idiv_by_zero", """
let x = 1 // 0
""", b""),
    ("mod_by_zero", """
let x = 1 % 0
""", b""),
    ("pow_zero_neg", """
let n = -1
let x = 0 ** n
""", b""),
    ("add_type_mismatch", """
struct S {}
let x = S {} + 1
""", b""),
    ("compare_type_mismatch", """
struct S {}
let x = S {} < 1
""", b""),
    ("negate_non_number", """
let x = -"hi"
""", b""),
    ("char_at_out_of_range", """
let s = "hi"
print(s.char_at(10))
""", b""),
    ("char_at_bad_type", """
let s = "hi"
print(s.char_at("x"))
""", b""),
    ("map_key_bad_type", """
let m = [:]
struct S {}
m[S {}] = 1
""", b""),
    ("vector_index_assign_out_of_range", """
let v = [1, 2]
v[10] = 5
""", b""),
    ("await_non_promise", """
let x = 5
let y = x.await
""", b""),
    ("match_fail", """
let x = 5
match x {
    100 => { print("no") }
}
""", b""),
    ("printable_returns_non_string", """
struct S {}
impl Printable for S {
    fn to_string(self) { return 5 }
}
print(S {})
""", b""),
    ("nested_error_in_to_string", """
struct S {}
impl Printable for S {
    fn to_string(self) { return 1 / 0 }
}
print(S {})
""", b""),
    ("error_inside_detach", """
fn boom() { return 1 / 0 }
let p = detach boom()
""", b""),
    ("input_end_of_input", """
let x = input()
""", b""),
    ("vector_slice_assign", """
let v = [1, 2, 3]
v[0..1] = [9]
""", b""),
    # M27: std:math -- values from every 1.5 native, then its errors.
    ("std_math_values", """
import math from "std:math"
print(math.tan(1), math.asin(0.5), math.acos(0.5), math.atan(2), math.atan2(0 - 1, 0 - 1))
print(math.exp(2), math.log(10), math.log10(2), math.sqrt(2), math.round(math.pi, 5))
""", b""),
    ("std_math_log_domain", """
import math from "std:math"
print(math.log(0 - 1))
""", b""),
    ("std_math_exp_overflow", """
import math from "std:math"
print(math.exp(100000))
""", b""),
    ("std_math_native_type_error", """
import math from "std:math"
let s: Unknown = "x"
print(math.tan(s))
""", b""),
    # M29: the String methods and Vector.join (mah/string_methods.py).
    ("string_methods", """
let u: Unknown = "  Hello, World  "
print(u.trim(), "|" + u.trim_start() + "|", "|" + u.trim_end() + "|")
print("a,b,,c".split(","), "  one two   three ".split(), " a b c ".split(limit: 1), "k=v=w".split("=", 1))
print("7".pad_start(3, "0"), "ab".pad_end(5, "xy") + "|", "abc".pad_start(2), "日本語".pad_start(5, "*"))
print("aXbXc".replace("X", "-"), "aXbXc".replace_all("X", "-"), "ab".replace("", "x"), "ab".replace_all("", "-"))
print("hello".starts_with("he"), "hello".ends_with("lo"), "hello".contains("ell"), "hello".contains("z"))
print("héllo wörld".index_of("w"), "hello".index_of("z"), "x".index_of(""), "ab".repeat(3), "ab".repeat(0) + "|")
print("Straße".to_upper(), "ÀB".to_lower(), "ΣΑΣ".to_lower())
print("a\r\nb\n".lines(), "".lines(), "a\n\nb".lines(), "x\ry".lines().len())
print(["x", 1, true, none, [2]].join(", "), [1, 2].join(), [].join("-") + "|")
print(" 42 ".parse_number(), "-1.5e2".parse_number(), "abc".parse_number(), ".5".parse_number(), "1.".parse_number())
print("+.5E-3".parse_number(), "1e123456".parse_number(), "1e99999".parse_number() > 1, "0x10".parse_number())
print("a\u00a0b\u2003c".split(), " \u3000x\u2028".trim() + "|", "\u001cx".trim().len())
print("42".to_number() + 1, "x".index_of("x").unwrap(), "x".index_of("y").unwrap_or(0 - 1))
""", b""),
    ("string_method_errors", """
let u: Unknown = 5
print(try { "ab".repeat(0 - 1) } catch { e => { e.message() } })
print(try { "ab".repeat(1.5) } catch { e => { e.message() } })
print(try { "a".split("") } catch { e => { e.message() } })
print(try { "a".split(limit: "2") } catch { e => { e.message() } })
print(try { "a".contains(u) } catch { e => { e.message() } })
print(try { "x".pad_start(3, "") } catch { e => { e.message() } })
print(try { [1].join(u) } catch { e => { e.message() } })
print(try { "x".index_of("y").unwrap() } catch { e => { e.message() } })
"abc".to_number()
""", b""),
    ("std_math_sqrt_negative", """
import math from "std:math"
print(math.sqrt(0 - 4))
""", b""),
    # M30: std:path, std:json, std:csv -- and through them the 1.7 natives.
    ("std_path", """
import path from "std:path"
print(path.normalize("a/./b/../c//d/"), path.normalize("C:/x/../y"), path.dirname("/a"), path.basename("a/b/"))
print(path.extension("a.tar.gz"), path.stem("a.tar.gz"), path.join("a", "/b"), path.relative("/a/b/c", "/a/x"))
""", b""),
    ("std_json", """
import json from "std:json"
struct P { x, y }
enum S {
    C { r },
    E
}
let v = json.parse("{\\"a\\": [1, -2.5e1, true, null, \\"\\\\u00e9\\\\ud83d\\\\ude00\\\\n\\"], \\"b\\": {}}")
print(v, json.stringify(v))
print(json.stringify([P { x: 1, y: [:] }, S.C { r: 2 }, S.E, some("\\u0001")], indent: 2))
for let bad in ["[1,]", "{\\"a\\" 1}", "01", "\\"\\\\ud800\\"", "[\\n  tru"] {
    print(try { json.parse(bad) } catch { e => { e.message() } })
}
""", b""),
    ("std_json_uncaught", """
import json from "std:json"
json.parse("[1, 2")
""", b""),
    ("std_json_native_type_error", """
import json from "std:json"
let u: Unknown = 5
json.parse(u)
""", b""),
    # M31: std:random's shared generator (seeded output must match exactly)
    # and std:collections.
    ("std_random", """
import random from "std:random"
random.seed(2024)
print(random.random(), random.uniform(0 - 1, 1), random.randint(1, 1000000), random.randint(0, 18446744073709551615))
let v = [1, 2, 3, 4, 5, 6, 7, 8, 9]
random.shuffle(v)
print(v, random.sample(v, 4), random.choice(["a", "b", "c"]), random.shuffled(["a", "b", "c", "d"]))
let r = random.Rng.new(0 - 12345)
print(r.random(), r.randint(0 - 3, 3), random.Rng.new(18446744073709551615).random())
print(try { random.randint(3, 1) } catch { e => { e.message } })
print(try { random.seed(1.5) } catch { e => { e.message } })
print(try { random.Rng { state: [1, 2, 3] }.random() } catch { e => { e.message } })
""", b""),
    ("std_random_uncaught", """
import random from "std:random"
random.choice([])
""", b""),
    ("std_collections", """
import "std:collections"
let s = Set.of([3, 1, 3])
s.add(2)
let d = Deque.of([1, 2])
d.push_front(0)
let q = PriorityQueue.of(["pear", "fig", "apple"], fn(w) { w.len() })
print(s, s.union(Set.of([9])), d, d.pop_back(), q, q.pop(), q.peek())
""", b""),
    # M32: std:regex -- the canonical form must match identically on Python's
    # `re` and Rust's `regex` crate (a seeded batch of random patterns too).
    ("std_regex", """
import regex from "std:regex"
let date = regex.compile("(?<y>\\\\d{4})-(?<m>\\\\d\\\\d)")
print(date.find_all("2026-09, 2027-10").map(fn(m) { m.group("y") + "/" + m.group(2) }).reduce())
print(date.replace_all("2026-09", "$m.$y"), regex.compile("\\\\s*;\\\\s*").split("a ; b;c"))
print(regex.compile("\\\\bé\\\\w*", "i").find_all("é éa aé ÉB").len(), regex.compile("^x|y$", "m").find_all("xy\\nyx\\n").len())
print(regex.compile("a*").find_all("baaé").map(fn(m) { m.start + "-" + m.end }).reduce(), regex.compile("\\\\B").find_all("").len())
print(regex.compile("(a)|(b)").find("xb"), regex.compile("[^a-c\\\\d]+?", "i").find_all("xAz9é").map(fn(m) { m.text }).reduce())
print(try { regex.compile("(?=x)") } catch { e => { e.message() } })
""", b""),
    ("std_regex_random", """
import random from "std:random"
import regex from "std:regex"
random.seed(5)
let pieces = ["(a|b)", "(ab|a)", "((a)|b)", "(?:(a)|bc)", "((ab)+)", "a", "b", "\\\\w", "[^b]", "(?<x>[ac]+?)", "(^a|b)", "(a$|c)", "(\\\\bb)", "é", "\\\\s", "(?:)", "\\\\B"]
let quants = ["", "", "*", "+", "?", "{2}", "{1,3}", "*?", "+?", "??", "{0,2}", "{2,}?"]
let alphabet = ["a", "b", "c", " ", "é", "\\n", "A"]
for let n in 0..60 {
    let parts = []
    for let j in 0..random.randint(1, 4) {
        parts.push(random.choice(pieces) + random.choice(quants))
    }
    let r = try regex.compile(parts.join(), random.choice(["", "i", "m", "s"])) else none
    if r == none { continue }
    for let k in 0..4 {
        let t = []
        for let i in 0..random.randint(0, 8) {
            t.push(random.choice(alphabet))
        }
        print(r.find_all(t.join()).map(fn(m) { m.start + "-" + m.end + ":" + m.groups }).reduce())
    }
}
""", b""),
    ("std_regex_uncaught", """
import regex from "std:regex"
regex.compile("a{5000}")
""", b""),
    # M33: the async `input` -- lines as Strings, prompts, CRLF, EndOfInput,
    # and a detached input waiting alongside a timer.
    ("input_lines", """
let a = input("first? ")
let b = input()
print("[" + a + "]", b.len(), try input() else "eof")
print(try { input() } catch { e: EndOfInput => { e.message() } })
""", b"one two\r\nx\n"),
    ("input_detached", """
let p = detach input("? ")
let q = detach input()
sleep_async(20)
print("tick")
print(q.await, p.await)
print(try (detach input()).await else "no more")
""", b"alpha\nbeta\n"),
    ("input_prompt_type_error", """
let u: Unknown = 1
input(u)
""", b""),
    # M34: std:time's calendar and formatting, and std:async's ordering.
    ("std_time", """
import time from "std:time"
for let ts in [0, 951782400, 1790597925.318, 0 - 86400.5, 253402300799] {
    let d = time.utc(ts)
    print(d, d.weekday(), d.day_of_year(), time.format(d, "%a %d %b %Y %H:%M:%S.%f"))
}
print(time.parse("Thu, 29 Feb 2024 13:05", "%a, %d %b %Y %H:%M"), time.parse_iso("2024-02-29T13:05:09.007Z").timestamp())
print(try { time.parse("2023-02-29", "%Y-%m-%d") } catch { e: time.TimeError => { e.message() } }, time.duration_text(10807))
""", b""),
    ("std_async", """
import async from "std:async"
fn after(ms: Number, value: Unknown) -> Unknown {
    sleep_async(ms)
    value
}
print(async.all([detach after(30, "a"), detach after(5, "b")]), async.race([detach after(40, 1), detach after(5, 2)]))
print(try { async.timeout(detach after(200, "late"), 10) } catch { e: async.TimeoutError => { e.message() } })
let log = []
async.set_timeout(fn() { log.push("b") }, 20)
async.set_timeout(fn() { log.push("a") }, 5)
async.clear_timeout(async.set_timeout(fn() { log.push("never") }, 10))
sleep_async(50)
print(log)
""", b""),
    # M35: std:fs -- results and error kinds (never the temporary path).
    ("std_fs", """
import fs from "std:fs"
let dir = fs.temp_dir()
fs.write_text(dir + "/a.txt", "one\\r\\ntwo\\n")
fs.mkdir(dir + "/sub/deep", parents: true)
fs.write_text(dir + "/sub/deep/x.mh", "é")
fs.copy(dir + "/a.txt", dir + "/sub/b.txt")
let f = fs.open(dir + "/a.txt")
print(f.read_line(), f.lines().reduce(), f.read_line())
f.close()
print(fs.list_dir(dir), fs.info(dir + "/sub/deep/x.mh").size, fs.is_dir(dir + "/sub"), fs.exists(dir + "/zz"))
print(fs.glob(dir + "/**/*.*").map(fn(p) { p[dir.len()..] }).reduce())
for let path in [dir + "/zz", dir + "/sub", dir + "/a.txt/x"] {
    print(try { fs.read_text(path) } catch { e: fs.FsError => { e.kind + " " + e.op + ": " + e.description } })
}
print(try { fs.remove(dir + "/sub") } catch { e: fs.FsError => { e.kind } }, try { f.read_line() } catch { e: fs.FsError => { e.kind } })
fs.remove(dir, recursive: true)
print(fs.exists(dir))
""", b""),
    # M36: std:process -- running programs, the environment table, errors.
    ("std_process", """
import process from "std:process"
let o = process.run("sh", ["-c", "printf out; printf err 1>&2; exit 3"])
print(o.code, o.stdout, o.stderr, o.ok())
print(process.run("cat", stdin: "in\\n").stdout, process.args().len())
process.env_set("MAH_DIFF", "1")
print(process.env_get("MAH_DIFF").unwrap(), process.env_get("MAH_DIFF_NOPE") == none)
print(process.shell("printf %s \\"$MAH_DIFF$MAH_EXTRA\\"", env: some(["MAH_EXTRA": "x"])).stdout)
process.env_remove("MAH_DIFF")
print(process.shell("printf %s \\"[$MAH_DIFF]\\"").stdout, process.env().keys().len() > 0)
print(process.run("sh", ["-c", "kill -9 $$"]).code, process.shell("echo a | tr a b").stdout.trim())
print(try { process.run("definitely-not-a-program-mah") } catch { e: process.ProcessError => { e.kind + " " + e.message() } })
print(try { process.env_set("A=B", "x") } catch { e: RuntimeError => { e.message() } })
process.exit(3)
""", b""),
    # M41a: type values, `##` docs and META through std:reflect, spread
    # calls, and json.decode -- including their error messages.
    ("std_reflect", """
import reflect from "std:reflect"
import json from "std:json"
struct MyErr { m: String }
impl Error for MyErr { fn message(self) { self.m } }
fn foo() { 1 }
## Adds.
## Twice.
fn add(
    a: Number,
    ## the second
    b: Number = 1,
    c = foo()
) -> Number throws MyErr { a + b }
fn first<T>(v: Vector<T>) -> T { v[0] }
fn g(f: fn(Number) -> String) { }
let s = reflect.signature(add)
print(s.name, s.doc, s.params.len(), s.params[1].doc, s.params[1].default, s.params[2].type, s.returns, s.throws)
print(reflect.signature(first).type_params, reflect.signature(first).params[0].type, reflect.signature(g).params[0].type)
print(reflect.signature(fn(x) { x }).name == "", reflect.signature(reflect.call).params.len())
## A user.
struct User {
    ## The name.
    name: String,
    tags: Vector<String>,
    age
}
enum Shape {
    ## A circle.
    Circle { r: Number },
    Empty
}
impl User {
    fn new(n: String) -> User { User { name: n, tags: [], age: 1 } }
    fn greet(self) -> String { "hi " + self.name }
}
impl Printable for User { fn to_string(self) { "User!" } }
print(reflect.schema(User))
print(reflect.schema(Shape), reflect.schema(Number), reflect.schema(Option))
let u = User.new("a")
for let m in reflect.methods(User) { print(m.name, m.is_method, m.trait_name) }
print(reflect.methods(User)[0].function(u), reflect.implements(User, "Printable"), reflect.implements(User, "FromJson"), reflect.implements(Number, "Printable"))
print(reflect.type_of(3) == Number, reflect.type_of("a") == String, reflect.type_of(u) == User, reflect.type_of(none), reflect.type_of([1]) == Vector, reflect.type_of(some(1)) == Option, reflect.type_of(User), Type, Function, None)
print(reflect.construct(User, ["name": "a", "tags": [], "age": 1]), reflect.construct_variant(Shape, "Circle", ["r": 2]), reflect.construct_variant(Option, "none", [:]))
for let bad in [["name": "a"], ["name": "a", "tags": [], "age": 1, "x": 0]] {
    print(try { reflect.construct(User, bad) } catch { e: reflect.ReflectError => { e.message() } })
}
print(try { reflect.construct_variant(Shape, "Nope", [:]) } catch { e: reflect.ReflectError => { e.message() } })
print(try { reflect.construct(Number, [:]) } catch { e: reflect.ReflectError => { e.message() } })
print(reflect.call(add, [1], ["b": 5]), reflect.call(add, [1]))
fn f(a, b = 2, c = 3) { a + b + c }
print(f(...[1, 10]), f(1, **["c": 100]), f(...[1], b: 5, **["c": 0]), u.greet(...[]))
for let bad in [fn() { f(1, b: 1, **["b": 2]) }, fn() { f(...3) }, fn() { f(**3) }, fn() { f(**[1: 2]) }, fn() { f(1, **["a": 2]) }] {
    print(try { bad() } catch { e: RuntimeError => { e.message() } })
}
struct P { x: Number, y: Option<Number>, tags: Vector<String>, inner: Option<P> }
print(json.decode(P, json.parse("{\\"x\\": 1, \\"tags\\": [\\"a\\"], \\"inner\\": {\\"x\\": 2, \\"tags\\": []}}")))
for let text in ["{\\"x\\": \\"1\\", \\"tags\\": []}", "{\\"tags\\": []}", "{\\"x\\": 1, \\"tags\\": [1]}", "[]"] {
    print(try { json.parse_as(P, text) } catch { e: json.JsonError => { e.message() } })
}
print(json.decode(Shape, json.parse(json.stringify(Shape.Circle { r: 2 }))), json.decode(Shape, "Empty"), json.decode(Vector, [1, "a"]))
print(try { json.stringify(User) } catch { e: json.JsonError => { e.message() } })
print(try { [User: 1] } catch { e: RuntimeError => { e.message() } })
print(User == User, User == Shape, User != Number, [User, Number].copy(deep: true))
""", b""),
    # M41b: decorators as metadata -- every target kind, `find`, evaluation
    # order, and a decorator that throws (an uncaught error at startup).
    ("decorators", """
import reflect from "std:reflect"
struct Route { method: String, path: String }
fn get(path: String) -> Route { Route { method: "GET", path: path } }
fn tag(t: String) -> String { print("eval " + t); "tag:" + t }
fn parts(t) {
    match reflect.schema(t) {
        some(reflect.Schema.Struct { type: a, doc: b, type_params: c, fields: fields, decorators: ds }) => { [ds, fields] }
        some(reflect.Schema.Enum { type: a, doc: b, type_params: c, variants: vs, decorators: ds }) => { [ds, vs] }
        _ => { [] }
    }
}
print("first statement")
## Fetch.
@get("/users/{id}")
@tag("users")
fn get_user(@tag("path") id: Number, verbose: Bool = false) -> Number { id }
@tag("model")
struct User { @tag("json") name: String, age: Number }
enum Shape { @tag("round") Circle { r: Number }, Empty }
impl User { @tag("m") fn hello(self, @tag("hp") x = 1) -> String { "hi" } }
let s = reflect.signature(get_user)
print(s.doc, s.decorators[0].path, s.decorators[1], s.params[0].decorators, s.params[1].decorators)
let u = parts(User)
let sh = parts(Shape)
print(u[0], u[1][0].decorators, u[1][1].decorators, sh[1][0].decorators, sh[1][1].decorators)
let m = reflect.signature(reflect.methods(User)[0].function)
print(m.decorators, m.params[1].decorators)
print(reflect.find(s.decorators, Route), reflect.find(s.decorators, Number), reflect.find(s.decorators, get) == none)
s.decorators.push(1)
print(reflect.signature(get_user).decorators.len())
struct Oops { message: String }
impl Error for Oops { fn message(self) -> String { self.message } }
fn boom(x) { throw Oops { message: "boom " + x } }
@boom("late")
fn doomed() { }
""", b""),
    # M41c: rest parameters, function-item impls and the four hooks.
    ("hooks", """
import reflect from "std:reflect"
fn show(a, b = 2, ...r, **k) { print(a, b, r, k) }
show(1)
show(1, 3, 4, 5, x: 6)
show(1, b: 9, y: 1)
show(...[1, 2, 3], **["z": 0])
print(show.arity(), reflect.signature(show).params.len(), reflect.signature(show).rest, reflect.signature(show).kwrest)
fn one(a) { a }
fn only_rest(...r) { r }
for let bad in [fn() { show(b: 1) }, fn() { one(1, 2) }, fn() { one(1, z: 2) }, fn() { only_rest(x: 1) }] {
    print(try { bad() } catch { e: RuntimeError => { e.message() } })
}
fn log(f, info: reflect.FnInfo) { fn(...args, **kw) { print("-> " + info.name); f(...args, **kw) } }
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }
impl log { fn describe(self) -> String { "logs" } }
trait Describe { fn name(self) -> String }
@log
fn greet(name: String, punct: String = "!") { "hi " + name + punct }
impl Describe for greet { fn name(self) -> String { "greet" } }
print(greet("a"), greet("b", punct: "?"), greet.name(), log.describe())
print(try { greet.describe() } catch { e: RuntimeError => { e.message() } })
print(reflect.signature(greet).params.len(), reflect.signature(greet).decorators.len(), reflect.type_of(log) == Function)
struct Obj { v: Number }
impl Obj { @log fn m(self) { self.v } }
print(Obj { v: 4 }.m())
fn trim(v, info) { v }
impl reflect.WrapParam for trim { fn transform(self, v, info) { info.name + "#" + info.index.to_string() + ":" + v.trim() } }
fn size(v, info) { v }
impl reflect.WrapParam for size { fn transform(self, v, info) { v.len() } }
fn hi(a, @trim name: String, @size ...more) { name + more.to_string() }
print(hi(1, "  x "), hi(1, name: " y "), reflect.call(hi, [1, " z ", 7, 8]), hi(...[1, " w ", 9]))
fn upper(v, info) { v }
impl reflect.WrapField for upper { fn set(self, v, info) { v.to_upper() } }
struct Positive {}
impl reflect.WrapStruct for Positive {
    fn construct(self, v, info) {
        if v.age < 0 { throw RuntimeError.ArgumentError { message: "age < 0" } }
        v
    }
}
fn positive() -> Positive { Positive {} }
@positive()
struct User { @upper name: String, age: Number }
let u = User { name: "ann", age: 3 }
u.name = "bob"
print(u.name, reflect.construct(User, ["name": "cy", "age": 1]).name)
print(try { User { name: "x", age: 0 - 1 } } catch { e: RuntimeError => { e.message() } })
struct Plain { a: Number }
let p = Plain { a: 1 }
p.a = 2
print(p.a)
""", b""),
    ("hooks_wrap_returns_a_non_function", """
import reflect from "std:reflect"
fn bad(f, info) { 5 }
impl reflect.WrapFn for bad { fn wrap(self, f, info) { 5 } }
print("never printed: the decorator phase runs first")
@bad
fn doomed() { }
""", b""),
    # M37: Bytes -- methods, indexing, operators, std:bytes, and every
    # error message (same text on both VMs).
    ("bytes", """
import bytes from "std:bytes"
let b = "h\u00e9llo".to_bytes()
print(b, b.len(), b[0], b[-1], b[99], b[1.5], b[1..3], b[..-2], b[10..])
b[0] = 72
b.push(33)
print(b.to_text(), b.to_hex(), b.to_base64(), b.pop(), b.to_vector())
let c = b.copy()
print(c == b, c == b.to_vector(), b + c, b.index_of("l".to_bytes()), b.index_of("".to_bytes()), b.index_of("zz".to_bytes()))
b.extend(b)
print(b.len(), [b].copy(deep: true)[0] == b, Bytes, bytes.new(), bytes.new(3, 255))
let total = 0
for let x in bytes.from_vector([1, 2, 3]) { total = total + x }
print(total, bytes.concat([bytes.new(1), "a".to_bytes()]), "x" + bytes.new(1))
for let t in ["", "Zg==", "Zm8=", "Zm9v", "Zm9vYg==", "Zh==", "Zg=", "Z===", "Zg==Zg==", "Z=g=", "a b="] {
    print(t, try { bytes.from_base64(t).to_hex() } catch { e: bytes.BytesError => { e.kind } })
}
for let t in ["", "00fFaB", "abc", "zz", "0g"] {
    print(t, try { bytes.from_hex(t).to_hex() } catch { e: bytes.BytesError => { e.message() } })
}
for let h in ["80", "c3", "e282", "eda080", "f4908080", "c0af", "61ff62", "f09f98", "f09f9880"] {
    let raw = bytes.from_hex(h)
    print(h, raw.to_text(), raw.to_text_lossy().to_bytes().to_hex())
}
let probes = [
    fn() { b[0] = 256 },
    fn() { b[0] = "a" },
    fn() { b[0] = 1.5 },
    fn() { b[99] = 1 },
    fn() { b[0..1] = 1 },
    fn() { b["x"] },
    fn() { b.push(0 - 1) },
    fn() { b.extend([1]) },
    fn() { b.index_of("a") },
    fn() { b + 1 },
    fn() { bytes.new(0 - 1) },
    fn() { bytes.new(1.5) },
    fn() { bytes.new("a") },
    fn() { bytes.new(1, 300) },
    fn() { bytes.from_vector([1, "a"]) },
    fn() { bytes.from_vector([1, 2.5]) },
    fn() { [b: 1] },
]
for let p in probes {
    print(try { p() } catch { e: RuntimeError => { e.message() } })
}
""", b""),
    # M37: binary std:fs.
    ("std_fs_bytes", """
import fs from "std:fs"
import bytes from "std:bytes"
let dir = fs.temp_dir()
let p = dir + "/x.bin"
fs.write_bytes(p, bytes.from_vector([0, 1, 255, 10, 13]))
fs.append_bytes(p, bytes.from_hex("fffe"))
print(fs.read_bytes(p), try { fs.read_text(p) } catch { e: fs.FsError => { e.kind } })
let f = fs.open(p)
print(f.read_bytes(2), try { f.read_line() } catch { e: fs.FsError => { e.kind } }, f.read_bytes(max: 0), f.read_bytes(100), f.read_bytes())
print(try { f.write_bytes(bytes.new(1)) } catch { e: fs.FsError => { e.description } })
print(try { f.read_bytes(1.5) } catch { e: RuntimeError => { e.message() } })
print(try { f.read_bytes("a") } catch { e: RuntimeError => { e.message() } })
f.close()
print(try { f.read_bytes() } catch { e: fs.FsError => { e.kind } })
let w = fs.open(p, "w")
w.write_bytes("ab".to_bytes())
w.write("c")
print(try { w.read_bytes() } catch { e: fs.FsError => { e.description } })
w.close()
print(fs.read_text(p), try { fs.read_bytes(dir + "/zz") } catch { e: fs.FsError => { e.kind + " " + e.op } })
print(try { fs.write_bytes(p, "text") } catch { e: RuntimeError => { e.message() } })
fs.remove(dir, recursive: true)
""", b""),
    # M38: std:socket -- TCP on 127.0.0.1, results, error kinds and messages.
    ("std_socket", """
import socket from "std:socket"
let server = socket.listen(0)
let incoming = detach server.accept()
let client = socket.connect("127.0.0.1", server.port)
let conn = incoming.await
print(client.peer_host, client.peer_port == server.port, conn.peer_port == client.local_port)
client.send_text("one\\r\\ntwo\\nrest")
print(conn.read_line(), conn.read_line(), conn.buffer, conn.recv(2), conn.recv())
conn.send("hi".to_bytes())
print(client.recv(), client.recv_exactly(0))
client.send_text("abc")
client.shutdown()
print(try { conn.recv_exactly(5) } catch { e: socket.SocketError => { e.kind + " " + e.message().replace_all(":" + conn.peer_port, ":PORT") } })
print(conn.recv(), conn.recv().len(), conn.read_line())
print(conn.recv(timeout: 30))
conn.send_text("late")
print(client.recv(timeout: 1000), try { client.read_line(timeout: 0) } catch { e: socket.SocketError => { e.kind } })
print(try { server.accept(timeout: 30) } catch { e: socket.SocketError => { e.kind + " " + e.description } })
print(client.to_string().starts_with("Socket(127.0.0.1:"), server.to_string().starts_with("Listener(127.0.0.1:"), server.host)
let waiting = detach conn.recv()
sleep_async(100)
conn.close()
conn.close()
print(try { waiting.await } catch { e: socket.SocketError => { e.kind + " " + e.op + " " + e.description } })
print(try { conn.send_text("x") } catch { e: socket.SocketError => { e.kind } })
let accepting = detach server.accept()
sleep_async(100)
server.close()
print(try { accepting.await } catch { e: socket.SocketError => { e.kind + " " + e.op } })
client.close()
let port = server.port
print(try { socket.connect("127.0.0.1", port) } catch { e: socket.SocketError => { e.kind + ": " + e.message().replace_all(":" + port, ":PORT") } })
let first = socket.listen(0)
print(try { socket.listen(first.port) } catch { e: socket.SocketError => { e.kind + ": " + e.description } })
first.close()
print(try { socket.connect("no-such-host.invalid", 80) } catch { e: socket.SocketError => { e.kind + ": " + e.description } })
let u: Unknown = "x"
let n: Unknown = 1.5
let probes = [
    fn() { socket.connect(1, 80) },
    fn() { socket.connect("127.0.0.1", u) },
    fn() { socket.connect("127.0.0.1", 0) },
    fn() { socket.connect("127.0.0.1", 70000) },
    fn() { socket.connect("127.0.0.1", 80, u) },
    fn() { socket.connect("127.0.0.1", 80, n) },
    fn() { socket.connect("127.0.0.1", 80, -1) },
    fn() { socket.listen(n) },
    fn() { socket.listen(-1) },
    fn() { socket.listen(0, n) },
    fn() { socket.listen(0, "127.0.0.1", 0) },
    fn() { socket.listen(0, "127.0.0.1", u) },
    fn() { socket.Listener { id: u, host: "h", port: 1 }.accept() },
    fn() { socket.Listener { id: 1, host: "h", port: 1 }.accept(u) },
    fn() { socket.Socket { id: 1, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }.recv(0) },
    fn() { socket.Socket { id: 1, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }.recv(u) },
    fn() { socket.Socket { id: 1, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }.send(u) },
]
for let p in probes {
    print(try { p() } catch { e: RuntimeError => { e.message() } })
}
""", b""),
    # M42: TLS servers -- tls_server_config's file checks (no certificates
    # here, so no handshake), start_tls_server on ids that aren't open, and
    # every argument error.
    ("std_tls_server", """
import socket from "std:socket"
import fs from "std:fs"
let dir = fs.temp_dir()
let cert = dir + "/cert.pem"
let key = dir + "/key.pem"
fs.write_text(cert, "-----BEGIN CERTIFICATE-----\\nAAAA\\n-----END CERTIFICATE-----\\n")
fs.write_text(dir + "/empty.pem", "nothing\\n")
fs.write_text(dir + "/enc.pem", "-----BEGIN ENCRYPTED PRIVATE KEY-----\\nAAAA\\n")
fs.write_text(dir + "/rsa_enc.pem", "-----BEGIN RSA PRIVATE KEY-----\\nProc-Type: 4,ENCRYPTED\\nAAAA\\n")
fs.write_text(key, "-----BEGIN PRIVATE KEY-----\\nAAAA\\n-----END PRIVATE KEY-----\\n")
fn load(c: String, k: String) -> String {
    try { socket.tls_server_config(dir + "/" + c, dir + "/" + k); "loaded" } catch {
        e: socket.SocketError => { e.kind + " " + e.op + ": " + e.description + " @ " + e.address.replace_all(dir, "DIR") }
    }
}
print(load("missing.pem", "key.pem"))
print(load("cert.pem", "missing.pem"))
print(load("empty.pem", "key.pem"))
print(load("cert.pem", "enc.pem"))
print(load("cert.pem", "rsa_enc.pem"))
print(load("cert.pem", "cert.pem"))
print(load("cert.pem", "key.pem"))
let cfg = socket.TlsServerConfig { id: 12345, cert_path: "c", key_path: "k" }
let sock = socket.Socket { id: 999, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }
print(try { sock.start_tls_server(cfg, 1000) } catch { e: socket.SocketError => { e.kind + ": " + e.message() } })
print(cfg, try { cfg.close() } catch { e: socket.SocketError => { e.kind } })
let u: Unknown = "x"
let n: Unknown = 1.5
let probes = [
    fn() { socket.tls_server_config(1, "k") },
    fn() { socket.tls_server_config("c", 1) },
    fn() { socket.Socket { id: u, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }.start_tls_server(cfg) },
    fn() { sock.start_tls_server(socket.TlsServerConfig { id: u, cert_path: "c", key_path: "k" }) },
    fn() { sock.start_tls_server(cfg, u) },
    fn() { sock.start_tls_server(cfg, n) },
    fn() { sock.start_tls_server(cfg, -1) },
    fn() { socket.Socket { id: 1, peer_host: "h", peer_port: 1, local_port: 1, buffer: "x".to_bytes() }.start_tls_server(cfg) },
]
for let p in probes {
    print(try { p() } catch { e: RuntimeError => { e.message() } })
}
fs.remove(dir, recursive: true)
""", b""),
    # M39: socket.start_tls's errors, std:url and an std:http round trip on 127.0.0.1.
    ("std_tls_url_http", """
import socket from "std:socket"
import url from "std:url"
import http from "std:http"
let u: Unknown = "x"
let probes = [
    fn() { socket.Socket { id: 1, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }.start_tls(5) },
    fn() { socket.Socket { id: 1, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }.start_tls("h", u) },
    fn() { socket.Socket { id: 1, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }.start_tls("h", -1) },
    fn() { socket.Socket { id: 1, peer_host: "h", peer_port: 1, local_port: 1, buffer: "x".to_bytes() }.start_tls("h") },
]
for let p in probes {
    print(try { p() } catch { e: RuntimeError => { e.message() } })
}
print(try { socket.Socket { id: 77, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }.start_tls("h") } catch { e: socket.SocketError => { e.message() } })
let server = socket.listen(0)
fn plain() {
    let conn = server.accept()
    conn.send_text("HTTP/1.1 400 Bad Request\\r\\n\\r\\n")
    conn.recv(65536, 2000)
    conn.close()
}
let done = detach plain()
let c = socket.connect("127.0.0.1", server.port)
print(try { c.start_tls("localhost", 5000) } catch { e: socket.SocketError => { e.kind + " " + e.description } })
c.close()
done.await
let b = url.parse("HTTP://u@Example.com:8080/a/b/c?x=1#f")
print(b, b.host, b.port, b.query, b.fragment, b.origin(), b.request_target())
print(b.resolve("../d?y=2"), b.resolve("//other/p"), b.resolve("/x/./y/../z"))
print(url.encode("a b&ü"), url.decode("a%20b%26%C3%BC"), url.encode_query(["q": "a b", "n": [1, 2]]))
print(url.parse_query("a=1&b=x+y&c"), try { url.decode("%e9") } catch { e: url.UrlError => { e.message() } })
fn serve() {
    let conn = server.accept()
    let line = conn.read_line()
    let h = conn.read_line()
    while h != "" { h = conn.read_line() }
    conn.send_text("HTTP/1.1 200 OK\\r\\nTransfer-Encoding: chunked\\r\\nX-A: 1\\r\\n\\r\\n3\\r\\nabc\\r\\n0\\r\\n\\r\\n")
    conn.close()
    print(line)
}
let served = detach serve()
let r = http.get("http://127.0.0.1:" + server.port + "/p?q=1")
served.await
print(r.status, r.reason, r.text(), r.header("x-a"), r.headers.len())
server.close()
print(try { http.get("gopher://h/") } catch { e: http.HttpError => { e.kind + ": " + e.message() } })
""", b""),
    ("std_http_server", """
import http from "std:http"
import socket from "std:socket"
fn handle(req: http.Request) -> Unknown {
    if req.path == "/form" { return http.Reply.json(req.form()) }
    if req.path == "/stream" {
        return http.Reply.stream(fn(w: http.BodyWriter) { w.write("ab"); w.write("c".to_bytes()) })
    }
    if req.path == "/bad" { return "not a reply" }
    http.Reply.text("hi " + req.path)
}
fn read_reply(c: socket.Socket) -> String {
    let out = c.read_line(5000)
    let n = 0
    let line = c.read_line(5000)
    while line != "" {
        if !line.starts_with("Date:") { out = out + " | " + line }
        if line.starts_with("Content-Length:") { n = line[16..].trim().to_number() }
        line = c.read_line(5000)
    }
    if n > 0 { out = out + " | " + c.recv_exactly(n, 5000).to_text_lossy() }
    out
}
fn raw(server: http.Server, text: String) -> socket.Socket {
    let c = socket.connect("127.0.0.1", server.port, 5000)
    c.send_text(text)
    c
}
let server = http.serve(0, handle, max_body: 64)
let r = http.get(server.url("/x"))
print(r.status, r.text(), r.header("content-type"), r.header("content-length"))
let k = raw(server, "GET /a HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n")
print(read_reply(k))
k.send_text("GET /b HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n")
print(read_reply(k))
k.close()
print(http.post(server.url("/form"), form: ["a": "1 2", "b": "é"]).text())
let bad = raw(server, "BAD\\r\\n\\r\\n")
print(read_reply(bad))
bad.close()
let big = raw(server, "POST / HTTP/1.1\\r\\nHost: x\\r\\nContent-Length: 100\\r\\n\\r\\nabcdef")
print(read_reply(big))
big.close()
let s = raw(server, "GET /stream HTTP/1.1\\r\\nHost: x\\r\\nConnection: close\\r\\n\\r\\n")
print(read_reply(s))
let rest = "".to_bytes()
let chunk = s.recv(65536, 5000)
while chunk.len() > 0 {
    rest.extend(chunk)
    chunk = s.recv(65536, 5000)
}
print(rest)
s.close()
let failed = http.get(server.url("/bad"))
print(failed.status, failed.text())
print(http.reason_phrase(200), http.reason_phrase(431), "[" + http.reason_phrase(299) + "]", http.http_date(86400))
print(server.connections() >= 0)
server.close()
print(server.connections())
""", b""),
    # M44 (docs/contracts/M44_threads.md #12.2): threads, shared variables,
    # locks, semaphores and channels. No case has more than one unobserved
    # failing job (#2.3).
    ("threads_basic", """
import thread from "std:thread"
let t = thread.spawn(name: "worker")
fn square(n) { n * n }
let p = detach(t) square(7)
print(p.await)
print(t.run(square, 9).await)
print(t, t.name, t.workers, t.capacity)
print(thread.id(), thread.name())
print(detach(t) { [thread.id(), thread.name()] }.await)
let u = thread.spawn()
print(u.name, u.id)
t.join()
u.join()
print(t.pending(), thread.cores() >= 1)
# T2: globals are copied at queue time
let t2 = thread.spawn()
let t2_counter = 0
let t2_items = [1, 2]
let t2_p = detach(t2) {
    t2_counter = t2_counter + 100
    t2_items.push(3)
    [t2_counter, t2_items.len()]
}
t2_counter = 5
print(t2_p.await)
print(t2_counter, t2_items)
# T3: identity and cycles
struct Node { value: Number, next: Unknown }
let t3 = thread.spawn()
let t3_a = Node { value: 1, next: none }
let t3_b = Node { value: 2, next: t3_a }
t3_a.next = t3_b
let t3_pair = [t3_a, t3_a]
let t3_r = t3.run(fn(v) {
    v[0].value = 10
    [v[1].value, v[0].next.next.value, v[0].next.value]
}, t3_pair)
print(t3_r.await, t3_a.value)
# T4: closures copy their frames
let t4 = thread.spawn()
fn make_counter() {
    let n = 0
    fn() {
        n = n + 1
        n
    }
}
let t4_c = make_counter()
t4_c()
print(t4.run(fn() {
    t4_c()
    t4_c()
}).await, t4_c())
# T5: methods and Printable travel with the snapshot
let t5 = thread.spawn()
struct P { x: Number }
impl P {
    fn double(self) { self.x * 2 }
}
impl Printable for P {
    fn to_string(self) { "P(" + self.x + ")" }
}
print(t5.run(fn(p) { p.double() }, P { x: 4 }).await)
print(detach(t5) { "got " + P { x: 1 } }.await)
""", b""),
    ("threads_errors", """
import thread from "std:thread"
let t = thread.spawn()
struct Oops { why: String }
impl Error for Oops {
    fn message(self) { "oops: " + self.why }
}
let q = detach(t) { throw Oops { why: "late" } }
try { q.await } catch {
    e: Oops => { print("caught", e.why, e.message()) }
}
let pr = detach sleep_async(1)
try { t.run(fn(x) { x }, [pr]) } catch {
    e: ThreadError => { print(e.kind) }
}
let g = detach sleep_async(1)
print(detach(t) {
    try {
        g.await
        "awaited"
    } catch {
        e: ThreadError => { e.kind }
    }
}.await)
let done = detach { 42 }
print(detach(t) { done.await + 1 }.await)
let bad = detach(t) { [detach { 1 }] }
try { bad.await } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
""", b""),
    ("threads_shared", """
import thread from "std:thread"
shared let n = 0
shared let xs = []
fn bump() {
    lock n {
        n = n + 1
        n
    }
}
print(lock n {
    bump()
    bump()
})
print(n)
lock xs { xs.push("a") }
let mine = xs
lock xs { xs.push("b") }
print(xs, mine)
let t = thread.spawn()
print(detach(t) {
    lock xs { xs.push("c") }
    lock xs { xs.len() }
}.await)
print(xs)
xs = ["reset"]
print(t.run(fn() { xs }).await)
try {
    lock xs {
        xs.push("d")
        throw RuntimeError.ArgumentError { message: "stop" }
    }
} catch {
    e => { print("caught", e.message()) }
}
print(xs)
shared let slot = none
try { slot = [detach { 1 }] } catch {
    e: ThreadError => { print(e.kind, e.message, slot) }
}
""", b""),
    ("threads_pool", """
import thread from "std:thread"
shared let total = 0
shared let log = []
let pool = thread.spawn(name: "pool", workers: 4)
fn work(n) {
    for let i in 0..100 {
        lock total { total = total + 1 }
    }
    lock log { log.push(n) }
    n * 2
}
let jobs = []
for let n in 0..8 { jobs.push(pool.run(work, n)) }
let doubled = 0
for let j in jobs { doubled = doubled + j.await }
let snapshot = log
let sum = 0
for let n in snapshot { sum = sum + n }
print(total, snapshot.len(), sum, doubled)
pool.join()
""", b""),
    ("threads_deadlock", """
shared let a = 0
shared let b = 0
let p1 = detach {
    lock a {
        sleep_async(20)
        lock b { "p1 got both" }
    }
}
let p2 = detach {
    lock b {
        sleep_async(60)
        try {
            lock a { "p2 got both" }
        } catch {
            e: ThreadError => { e.kind + " | " + e.message }
        }
    }
}
print(p1.await)
print(p2.await)
""", b""),
    ("threads_semaphore", """
import thread from "std:thread"
let s = thread.semaphore(1)
print(s.try_acquire(), s.try_acquire(), s.available())
s.release()
print(s.available(), s)
try { s.release() } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
let gate = thread.semaphore(2)
shared let inside = 0
shared let most = 0
fn job(n) {
    gate.acquire()
    defer gate.release()
    lock inside, most {
        inside = inside + 1
        if inside > most { most = inside }
    }
    sleep_async(20)
    lock inside { inside = inside - 1 }
    n
}
let pool = thread.spawn(workers: 4)
let ps = []
for let n in 0..6 { ps.push(pool.run(job, n)) }
let total = 0
for let p in ps { total = total + p.await }
print(most <= 2, most >= 1, inside, total, gate.available())
pool.join()
""", b""),
    ("threads_channels", """
import thread from "std:thread"
let c1 = thread.channel(capacity: 1)
c1.send("a")
print(c1.len())
let blocked = detach c1.send("b")
print(c1.recv(), c1.recv())
blocked.await
c1.close()
print(c1.try_recv(), c1.closed(), c1.len())
try { c1.send("x") } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
try { c1.recv() } catch {
    e: ThreadError => { print(e.kind) }
}
let tasks = thread.channel()
let results = thread.channel()
let workers = thread.spawn(name: "workers", workers: 3)
fn worker() {
    let sum = 0
    for let job in tasks { sum = sum + job }
    results.send(sum)
}
for let i in 0..3 { workers.run(worker) }
for let n in 1..=10 { tasks.send(n) }
tasks.close()
let total = 0
for let i in 0..3 { total = total + results.recv() }
print(total)
let r = thread.channel(capacity: 0)
let receiver = detach r.recv()
r.send("hand-off")
print(receiver.await, r.try_recv())
workers.join()
""", b""),
    ("threads_close", """
import thread from "std:thread"
let solo = thread.spawn(name: "solo", capacity: 1)
let started = thread.channel()
let gate = thread.channel()
let first = detach(solo) {
    started.send(1)
    gate.recv()
}
started.recv()
let second = detach(solo) { "second" }
try { detach(solo) { "third" } } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
print(solo.pending())
solo.close(cancel: true)
try { second.await } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
try { solo.run(fn() { 1 }) } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
gate.send("go")
print(first.await)
solo.join()
print(solo.pending())
let me = thread.spawn(name: "me")
print(detach(me) {
    try {
        me.join()
        "joined"
    } catch {
        e: ThreadError => { e.message }
    }
}.await)
me.join()
""", b""),
    ("threads_exit", """
import thread from "std:thread"
import process from "std:process"
let t = thread.spawn()
print("before")
let p = detach(t) { process.exit(3) }
p.await
print("never")
""", b""),
    ("threads_keepalive", """
import thread from "std:thread"
let t = thread.spawn()
detach(t) {
    sleep_async(100)
    print("late")
}
print("main done")
""", b""),
    ("threads_uncaught", """
import thread from "std:thread"
let t = thread.spawn()
let p = detach(t) { throw RuntimeError.ArgumentError { message: "bad" } }
""", b""),
    ("threads_implicit", """
shared let n = 0
struct P { x: Number }
impl Printable for P {
    fn to_string(self) {
        lock n { n = n + 1 }
        "P(" + self.x + ", n=" + n + ")"
    }
}
lock n {
    n = 10
    print(P { x: 1 })
}
print(n)
""", b""),
    ("threads_reads", """
shared let xs = [1]
fn count_with(v) {
    let s = xs
    s.push(v)
    s.len()
}
print(count_with(2), xs)
print(lock xs {
    xs.push(5)
    [count_with(9), xs.len()]
}, xs)
""", b""),
    ("threads_writeback", """
shared let slot = []
struct Boom { why: String }
impl Error for Boom {
    fn message(self) { "boom: " + self.why }
}
try {
    lock slot {
        slot.push(detach { 1 })
        throw Boom { why: "first" }
    }
} catch {
    e: Boom => { print("caught", e.message()) }
    e: ThreadError => { print("wrong", e.kind) }
}
print(slot)
try {
    lock slot { slot.push(detach { 2 }) }
} catch {
    e: ThreadError => { print(e.kind) }
}
print(slot)
""", b""),
    ("threads_await_deadlock", """
shared let x = 0
let p = none
try {
    lock x {
        p = detach { x = 1 }
        p.await
    }
} catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
p.await
print(x)
""", b""),
    ("threads_await_deadlock_job", """
import thread from "std:thread"
shared let x = 0
let t = thread.spawn()
try {
    lock x { detach(t) { x = 1 }.await }
} catch {
    e: ThreadError => { print(e.kind) }
}
t.join()
print("joined")
""", b""),
    ("threads_stuck", """
import thread from "std:thread"
let ch = thread.channel()
try { ch.recv() } catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
let t = thread.spawn()
let p = detach(t) { ch.recv() }
try { p.await } catch {
    e: ThreadError => { print(e.kind) }
}
let s = thread.semaphore(1)
s.acquire()
try { s.acquire() } catch {
    e: ThreadError => { print(e.kind) }
}
print("end")
""", b""),
    ("threads_handles", """
import thread from "std:thread"
shared let jobs = thread.channel()
shared let gate: thread.Semaphore = thread.semaphore(1)
jobs.send(1)
gate.acquire()
print(jobs.recv(), gate.available(), jobs.len())
gate.release()
""", b""),
    ("threads_teardown_lock", """
import thread from "std:thread"
shared let k = 0
let t = thread.spawn()
let p = detach(t) {
    detach {
        lock k { sleep_async(50) }
    }
    detach {
        lock k { k = 1 }
    }
    sleep_async(10)
    throw RuntimeError.ArgumentError { message: "boom" }
}
try { p.await } catch {
    e => { print("failed") }
}
lock k { print(k) }
""", b""),
    ("std_csv", """
import csv from "std:csv"
print(csv.parse("a,\\"b,c\\"\\r\\n\\n\\"q\\"\\"x\\",\\n"), csv.parse_records("n,v\\nx,1\\n"))
print(csv.stringify([["a b", "c,d"], ["say \\"hi\\"", none], [""]]))
print(try { csv.parse("a\\n\\"b") } catch { e => { e.message() } })
""", b""),
]


def run_inline_programs(vm_path: str, tmpdir: str, verbose: bool) -> list[Result]:
    results = []
    for name, source, stdin_data in RUNTIME_ERROR_PROGRAMS:
        try:
            mahc = compile_source(source, tmpdir, name)
        except subprocess.CalledProcessError as e:
            r = Result(name)
            r.fail(f"failed to compile: {e.stderr.decode('utf-8', 'replace')}")
            results.append(r)
            continue
        py = run_python_mahc(mahc, stdin_data)
        rs = run_rust_mahc(vm_path, mahc, stdin_data)
        r = compare(name, py, rs)
        results.append(r)
        if verbose:
            print(f"  {'ok' if r.passed else 'FAIL'}  {name}")
    return results


# ---------------------------------------------------------------------------
# Malformed files -- built by editing bytes of a compiled file.
# ---------------------------------------------------------------------------

def build_malformed_cases(tmpdir: str) -> list[tuple[str, bytes]]:
    good_path = compile_source("print(1)\n", tmpdir, "good_for_malformed")
    with open(good_path, "rb") as f:
        good = f.read()
    # Strip the shebang line so byte offsets below are predictable.
    nl = good.index(b"\n")
    body = good[nl + 1 :]

    cases = []
    cases.append(("bad_magic", b"XXXX" + body[4:]))
    major_bad = bytearray(body)
    major_bad[4:6] = (99).to_bytes(2, "little")
    cases.append(("bad_major_version", bytes(major_bad)))
    minor_bad = bytearray(body)
    minor_bad[6:8] = (999).to_bytes(2, "little")
    cases.append(("bad_minor_version", bytes(minor_bad)))
    # M27: a newer file whose natives this VM lacks -- both VMs must name
    # them the same way (`math.tan` respelled as a pretend `math.zzz`).
    std_path = compile_source('import math from "std:math"\nprint(math.tan(1))\n', tmpdir, "std_for_malformed")
    with open(std_path, "rb") as f:
        std = f.read()
    std_body = bytearray(std[std.index(b"\n") + 1 :].replace(b"math.tan", b"math.zzz"))
    std_body[6:8] = (7).to_bytes(2, "little")
    cases.append(("newer_minor_names_missing_natives", bytes(std_body)))
    # M41a: a META section that doesn't describe this file's functions --
    # both VMs must say so the same way; a duplicate is refused too.
    meta_body = bytes(body[:7]) + bytes([14]) + bytes(body[8:])
    cases.append(("meta_wrong_function_count", meta_body + bytes([0x82, 1, 5])))
    cases.append(("meta_duplicate", meta_body + bytes([0x82, 1, 5, 0x82, 1, 5])))
    # M41b: `decorate` with an unknown kind, a field index out of range, a
    # non-user type -- both VMs must refuse them with the same message.
    deco_path = compile_source("fn d(x) { x }\nstruct S { @d(1) a }\n", tmpdir, "decorate_for_malformed")
    with open(deco_path, "rb") as f:
        deco = f.read()
    deco_body = deco[deco.index(b"\n") + 1 :]
    at = deco_body.index(bytes([0x3D, 3, 3, 0, 1]))
    for label, edit in (("kind_5", (0, 5)), ("field_out_of_range", (2, 7)), ("not_a_user_type", (1, 1))):
        patched = bytearray(deco_body)
        patched[at + 1 + edit[0]] = edit[1]
        cases.append((f"decorate_{label}", bytes(patched)))
    # M41c: PARAMS rest flags (both VMs refuse them with the same message),
    # `paramhooks` operands, and function-item keys.
    rest_path = compile_source("fn f(a, ...r, **k) { }\nprint(1)\n", tmpdir, "rest_for_malformed")
    with open(rest_path, "rb") as f:
        rest_file = f.read()
    rest_body = rest_file[rest_file.index(b"\n") + 1 :]
    import re

    found = re.search(rb"\x03.\x00.\x02.\x04", rest_body)
    assert found is not None
    flags_at = found.start()
    for label, edits in (
        # offsets: 0 nparams, then (name, flags) pairs -- flags at 2 (a), 4 (r), 6 (k)
        ("rest_with_default", {6: 5}),
        ("both_rest_flags", {6: 6}),
        ("undefined_flag_bit", {6: 8}),
        ("positional_rest_first", {2: 2}),
        ("keyword_rest_not_last", {4: 4}),
    ):
        patched = bytearray(rest_body)
        for offset, value in edits.items():
            patched[flags_at + offset] = value
        cases.append((f"params_{label}", bytes(patched)))
    old_minor = bytearray(rest_body)
    old_minor[6:8] = (15).to_bytes(2, "little")
    cases.append(("params_rest_flag_in_minor_15", bytes(old_minor)))
    sys.path.insert(0, REPO_ROOT)
    from mah.bytecode.decode import decode as py_decode
    from mah.bytecode.encode import encode as py_encode
    from mah.bytecode.program import Instr

    hook_src = (
        "import reflect from \"std:reflect\"\n"
        "fn tag(x) { x }\nfn f(@tag(1) a) { a }\n"
        "trait Tr { fn m(self) -> String }\nimpl Tr for tag { fn m(self) -> String { \"x\" } }\n"
    )
    hook_path = compile_source(hook_src, tmpdir, "hooks_for_malformed")
    with open(hook_path, "rb") as f:
        hook_program = py_decode(f.read())
    at = next(i for i, instr in enumerate(hook_program.code) if instr.op == "paramhooks")
    fn_index, param_index, dest = hook_program.code[at].args
    for label, args in (("function_out_of_range", (999, 0, dest)), ("param_out_of_range", (fn_index, 9, dest))):
        patched = py_decode(py_encode(hook_program))
        patched.code[at] = Instr("paramhooks", args)
        cases.append((f"paramhooks_{label}", py_encode(patched)))
    at = next(
        i
        for i, instr in enumerate(hook_program.code)
        if instr.op == "defmethod" and hook_program.strings[instr.args[1]].startswith("fn#")
    )
    for label, key in (("out_of_range", "fn#9999"), ("not_a_number", "fn#x"), ("leading_zero", "fn#01")):
        patched = py_decode(py_encode(hook_program))
        string_index = patched.code[at].args[1]
        patched.strings[string_index] = key
        cases.append((f"defmethod_fn_key_{label}", py_encode(patched)))
    cases.append(("truncated_at_10_bytes", body[:10]))
    cases.append(("truncated_at_magic", body[:2]))
    cases.append(("empty_file", b""))

    # Unknown opcode: find the CODE section (id 0x06) and flip its first
    # instruction's opcode byte to an unused value (0x7E, never assigned).
    idx = body.index(bytes([0x06]))
    # section header is (id u8, length varuint); payload starts after that.
    # Since this file is tiny, the length varuint is a single byte.
    length = body[idx + 1]
    payload_start = idx + 2
    corrupted = bytearray(body)
    # payload = count varuint (1 byte, since few instructions) + opcodes...
    opcode_pos = payload_start + 1
    corrupted[opcode_pos] = 0x7E
    cases.append(("unknown_opcode", bytes(corrupted)))
    _ = length
    return cases


def run_malformed_cases(vm_path: str, tmpdir: str, verbose: bool) -> list[Result]:
    results = []
    for name, data in build_malformed_cases(tmpdir):
        path = os.path.join(tmpdir, f"malformed_{name}.mahc")
        with open(path, "wb") as f:
            f.write(data)
        py = run_python_mahc(path, b"")
        rs = run_rust_mahc(vm_path, path, b"")
        r = compare(f"malformed:{name}", py, rs)
        results.append(r)
        if verbose:
            print(f"  {'ok' if r.passed else 'FAIL'}  malformed:{name}")
    return results


# ---------------------------------------------------------------------------
# M28: `mah test` outcomes -- one test file, every test run by both VMs'
# per-test entry points (`run_test_bytes` / `mah-vm test FILE N`), comparing
# the test's stdout and its outcome text (docs/MAHC_FORMAT.md #6.10).
# ---------------------------------------------------------------------------

TEST_FILE = """import "std:test"

fn helper(x) {
    assert_eq(x, 3)
}

let counter = [0]

test "passes" {
    assert_eq(1 + 2, 3)
    counter.push(1)
}

test "fails with output" {
    print("some output")
    assert_eq(2 + 2, 5, "math")
}

test "fails in a helper" {
    helper(4)
}

test "skipped" {
    skip("later")
}

test "runtime error" {
    let v = [1]
    v[5]
}

test "uncaught user error" {
    fail("stop")
}

test "async" {
    sleep_async(5)
    assert(true)
}

test "leftover timers" {
    let p = detach sleep_async(50)
}

test "isolated" {
    assert_eq(counter.len(), 1)
}
"""

_PY_RUN_TEST = (
    "import sys\n"
    "from mah.code_interpreter import run_test_bytes\n"
    "from mah.test_outcome import format_outcome\n"
    "outcome = run_test_bytes(open(sys.argv[1], 'rb').read(), int(sys.argv[2]))\n"
    "sys.stdout.flush()\n"
    "sys.stderr.write(format_outcome(outcome))\n"
)


def run_test_outcomes(vm_path: str, tmpdir: str, verbose: bool) -> list[Result]:
    env = {**os.environ, "PYTHONPATH": REPO_ROOT}
    src = os.path.join(tmpdir, "outcomes.test.mh")
    with open(src, "w") as f:
        f.write(TEST_FILE)
    mahc = os.path.join(tmpdir, "outcomes.mahc")
    build = (
        "import sys\nfrom mah.compiler.driver import compile_to_bytes\nfrom mah.bytecode.decode import decode\n"
        "data = compile_to_bytes(path=sys.argv[1], test=True)\nopen(sys.argv[2], 'wb').write(data)\n"
        "print(len(decode(data).tests))\n"
    )
    count = int(
        subprocess.run(
            [sys.executable, "-c", build, src, mahc], check=True, capture_output=True, text=True, cwd=REPO_ROOT, env=env
        ).stdout
    )
    results = []
    for i in range(count):
        py = subprocess.run(
            [sys.executable, "-c", _PY_RUN_TEST, mahc, str(i)], capture_output=True, cwd=REPO_ROOT, env=env, timeout=30
        )
        rs = subprocess.run([vm_path, "test", mahc, str(i)], capture_output=True, cwd=REPO_ROOT, timeout=30)
        r = compare(f"test:{i}", py, rs)
        results.append(r)
        if verbose:
            print(f"  {'ok' if r.passed else 'FAIL'}  test:{i}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm", default=DEFAULT_VM, help="path to the mah-vm binary")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not os.path.isfile(args.vm):
        print(f"error: mah-vm binary not found at {args.vm} (build it with `cargo build --release`)", file=sys.stderr)
        return 2

    all_results: list[Result] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        print("== examples/*.mh ==")
        all_results += run_examples(args.vm, tmpdir, args.verbose)
        print("== inline runtime-error programs ==")
        all_results += run_inline_programs(args.vm, tmpdir, args.verbose)
        print("== malformed files ==")
        all_results += run_malformed_cases(args.vm, tmpdir, args.verbose)
        print("== mah test outcomes ==")
        all_results += run_test_outcomes(args.vm, tmpdir, args.verbose)

    passed = [r for r in all_results if r.passed]
    failed = [r for r in all_results if not r.passed]
    print()
    print(f"{len(passed)} passed, {len(failed)} failed, {len(all_results)} total")
    if failed:
        print()
        print("Failures:")
        for r in failed:
            print(f"- {r.name}: {r.detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
