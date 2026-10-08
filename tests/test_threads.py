"""M44 (docs/contracts/M44_threads.md #12.1): threads (`std:thread`,
`detach(t)`), shared variables and `lock`, semaphores and channels.

Every program runs through tests/support.py, so `make test-rust` runs them
on the Rust VM too. The compile-error tests and the wait-for graph tests are
Python-only.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import compile_bytes, compile_source, run_file, run_source  # noqa: E402

THREAD = 'import thread from "std:thread"\n'


class ThreadProgramTests(unittest.TestCase):
    maxDiff = None

    def test_t1_basics(self):
        src = THREAD + """let t = thread.spawn(name: "worker")
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
"""
        self.assertEqual(
            run_source(src), "49\n81\nThread(worker) worker 1 none\n0 main\n[1, worker]\nthread-2 2\n0 true\n"
        )

    def test_t2_globals_are_copied_at_queue_time(self):
        src = THREAD + """let t = thread.spawn()
let counter = 0
let items = [1, 2]
let p = detach(t) {
    counter = counter + 100
    items.push(3)
    [counter, items.len()]
}
counter = 5
print(p.await)
print(counter, items)
"""
        self.assertEqual(run_source(src), "[100, 3]\n5 [1, 2]\n")

    def test_t3_identity_and_cycles(self):
        src = THREAD + """struct Node { value: Number, next: Unknown }
let t = thread.spawn()
let a = Node { value: 1, next: none }
let b = Node { value: 2, next: a }
a.next = b
let pair = [a, a]
let r = t.run(fn(v) {
    v[0].value = 10
    [v[1].value, v[0].next.next.value, v[0].next.value]
}, pair)
print(r.await, a.value)
"""
        self.assertEqual(run_source(src), "[10, 10, 2] 1\n")

    def test_t4_closures_copy_their_frames(self):
        src = THREAD + """let t = thread.spawn()
fn make_counter() {
    let n = 0
    fn() {
        n = n + 1
        n
    }
}
let c = make_counter()
c()
print(t.run(fn() {
    c()
    c()
}).await, c())
"""
        self.assertEqual(run_source(src), "3 2\n")

    def test_t5_methods_travel_with_the_snapshot(self):
        src = THREAD + """let t = thread.spawn()
struct P { x: Number }
impl P {
    fn double(self) { self.x * 2 }
}
impl Printable for P {
    fn to_string(self) { "P(" + self.x + ")" }
}
print(t.run(fn(p) { p.double() }, P { x: 4 }).await)
print(detach(t) { "got " + P { x: 1 } }.await)
"""
        self.assertEqual(run_source(src), "8\ngot P(1)\n")

    def test_t6_errors_and_promises(self):
        src = THREAD + """let t = thread.spawn()
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
"""
        self.assertEqual(
            run_source(src),
            "caught late oops: late\nnot_sendable\nforeign_promise\n43\n"
            "not_sendable a Promise can't be sent to another thread\n",
        )

    def test_t7_shared_variables_and_lock(self):
        src = THREAD + """shared let n = 0
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
"""
        self.assertEqual(
            run_source(src),
            "2\n2\n[a, b] [a]\n3\n[a, b, c]\n[reset]\ncaught stop\n[reset, d]\n"
            "not_sendable shared variable 'slot' can't hold a Promise none\n",
        )

    def test_t8_pool_and_shared_counters(self):
        src = THREAD + """shared let total = 0
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
"""
        self.assertEqual(run_source(src), "800 8 28 56\n")

    def test_t9_deadlock_detection(self):
        src = """shared let a = 0
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
"""
        self.assertEqual(run_source(src), "p1 got both\ndeadlock | deadlock: waiting for 'a' would never end\n")

    def test_t10_semaphores(self):
        src = THREAD + """let s = thread.semaphore(1)
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
"""
        self.assertEqual(
            run_source(src),
            "true false 0\n1 Semaphore(1/1)\n"
            "over_release release without a matching acquire (all 1 permits are free)\ntrue true 0 15 2\n",
        )

    def test_t11_channels(self):
        src = THREAD + """let c1 = thread.channel(capacity: 1)
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
"""
        self.assertEqual(
            run_source(src), "1\na b\nnone true 0\nclosed the channel is closed\nclosed\n55\nhand-off none\n"
        )

    def test_t12_capacity_cancel_close_join(self):
        src = THREAD + """let solo = thread.spawn(name: "solo", capacity: 1)
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
"""
        self.assertEqual(
            run_source(src),
            "full thread 'solo' is full (2 jobs queued or running)\n2\n"
            "cancelled thread 'solo' was closed before this job started\nclosed thread 'solo' is closed\n"
            "go\n0\ndeadlock: a thread can't join itself\n",
        )

    def test_t13_modules(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "lib.mh"), "w", encoding="utf-8") as f:
                f.write("export shared let hits = 0\nexport fn hit() {\n    lock hits { hits = hits + 1 }\n}\n")
            main = os.path.join(td, "main.mh")
            with open(main, "w", encoding="utf-8") as f:
                f.write(
                    'import lib from "lib.mh"\n' + THREAD + "let t = thread.spawn()\nlib.hit()\n"
                    "t.run(lib.hit).await\nlock lib.hits { lib.hits = lib.hits + 1 }\nprint(lib.hits)\n"
                )
            self.assertEqual(run_file(main), "3\n")

    def test_t14_contextual_words_stay_identifiers(self):
        self.assertEqual(
            run_source("let shared = [1]\nlet lock = 2\nfn f(lock) { lock + 1 }\nprint(shared, lock, f(lock))"),
            "[1] 2 3\n",
        )
        self.assertEqual(run_source("print(detach (1 + 2).await)"), "3\n")

    def test_t15_print_never_tears(self):
        src = THREAD + """let pool = thread.spawn(workers: 4)
fn shout(n) {
    for let i in 0..25 { print("line " + n + "-" + i) }
}
for let n in 0..4 { pool.run(shout, n) }
pool.join()
"""
        out = run_source(src)
        self.assertEqual(sorted(out.splitlines()), sorted(f"line {n}-{i}" for n in range(4) for i in range(25)))

    def test_t19_implicit_calls_share_their_callers_task(self):
        src = """shared let n = 0
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
"""
        self.assertEqual(run_source(src), "P(1, n=11)\n11\n")

    def test_t20_only_lexically_locked_reads_alias(self):
        src = """shared let xs = [1]
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
"""
        self.assertEqual(run_source(src), "2 [1]\n[3, 2] [1, 5]\n")

    def test_t21_failed_write_back_never_replaces_the_error(self):
        src = """shared let slot = []
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
"""
        self.assertEqual(run_source(src), "caught boom: first\n[]\nnot_sendable\n[]\n")

    def test_t22_await_cycle_through_a_lock(self):
        src = """shared let x = 0
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
"""
        self.assertEqual(
            run_source(src),
            "deadlock | deadlock: this await would never end (it waits, through locks or threads, for itself)\n1\n",
        )

    def test_t23_await_cycle_across_threads(self):
        src = THREAD + """shared let x = 0
let t = thread.spawn()
try {
    lock x { detach(t) { x = 1 }.await }
} catch {
    e: ThreadError => { print(e.kind) }
}
t.join()
print("joined")
"""
        self.assertEqual(run_source(src), "deadlock\njoined\n")

    def test_t24_quiescence(self):
        src = THREAD + """let ch = thread.channel()
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
"""
        self.assertEqual(
            run_source(src), "stuck | the wait can never finish: every thread is waiting\nstuck\nstuck\nend\n"
        )

    def test_t25_handle_variables_need_no_lock(self):
        src = THREAD + """shared let jobs = thread.channel()
shared let gate: thread.Semaphore = thread.semaphore(1)
jobs.send(1)
gate.acquire()
print(jobs.recv(), gate.available(), jobs.len())
gate.release()
"""
        self.assertEqual(run_source(src), "1 0 0\n")

    def test_t26_a_job_that_just_ended_is_never_stuck(self):
        # Review fix (#6.10 `finishing`): between a job VM's teardown and its
        # reply, the job is neither a live VM nor a pending start; a VM that
        # blocked in that window (here: main, awaiting that reply) used to
        # get a false `stuck`. The Rust VM hit it in about a third of runs.
        src = THREAD + """let ch = thread.channel()
let t1 = thread.spawn(name: "a")
let t2 = thread.spawn(name: "b")
fn spin(n) {
    let s = 0
    for let k in 0..n { s = s + k }
    s
}
let stuck = 0
let total = 0
for let i in 0..1000 {
    let w = i % 40
    let b = detach(t2) {
        spin(w)
        ch.recv()
    }
    let a = detach(t1) spin(20)
    try { a.await } catch {
        e: ThreadError => { stuck = stuck + 1 }
    }
    ch.send(i)
    try { total = total + b.await } catch {
        e: ThreadError => { stuck = stuck + 1 }
    }
}
print(stuck, total)
"""
        self.assertEqual(run_source(src), "0 499500\n")

    def test_t27_a_failed_jobs_locks_never_pass_to_its_own_tasks(self):
        # Review fix (#6.6): the teardown granted a lock the dying job VM
        # owned to another task of that same VM, whose grant was then
        # dropped -- the lock was never free again (main got `stuck`).
        src = THREAD + """shared let k = 0
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
"""
        self.assertEqual(run_source(src), "failed\n0\n")


def _run_compiled(src: str):
    """Compile `src` to a temporary .mahc and run it in a subprocess on the
    VM `MAH_TEST_VM` selects: (exit code, stdout, stderr)."""
    data = compile_bytes(text=src)
    fd, path = tempfile.mkstemp(suffix=".mahc")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if os.environ.get("MAH_TEST_VM") == "rust":
            from mah.rust_vm import find_vm

            cmd = [find_vm(), "run", path]
        else:
            cmd = [sys.executable, "-m", "mah", "runc", path]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(cmd, capture_output=True, cwd=root, timeout=60)
    finally:
        os.unlink(path)
    return result.returncode, result.stdout.decode(), result.stderr.decode()


class ThreadProcessTests(unittest.TestCase):
    def test_t16_exit_from_a_job(self):
        src = (
            THREAD
            + 'import process from "std:process"\nlet t = thread.spawn()\nprint("before")\n'
            + "let p = detach(t) { process.exit(3) }\np.await\nprint(\"never\")\n"
        )
        self.assertEqual(_run_compiled(src), (3, "before\n", ""))

    def test_t17_a_job_keeps_the_program_alive(self):
        src = THREAD + 'let t = thread.spawn()\ndetach(t) {\n    sleep_async(100)\n    print("late")\n}\nprint("main done")\n'
        self.assertEqual(_run_compiled(src), (0, "main done\nlate\n", ""))

    def test_t18_an_uncaught_job_error_is_located_like_a_task_error(self):
        a = THREAD + 'let t = thread.spawn()\nlet p = detach(t) { throw RuntimeError.ArgumentError { message: "bad" } }\n'
        b = a.replace("detach(t)", "detach   ")
        ra = _run_compiled(a)
        rb = _run_compiled(b)
        self.assertEqual(ra[0], 1)
        self.assertEqual(ra[1], "")
        self.assertEqual(ra, rb)
        self.assertTrue(ra[2].startswith("RuntimeError: bad at position #3:"), ra[2])


def _compile_error(src: str) -> str:
    try:
        compile_source(text=src)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    raise AssertionError(f"expected a compile error for {src!r}")


def _compile_error_path(src: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "main.mh")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        try:
            compile_source(path=path)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
    raise AssertionError(f"expected a compile error for {src!r}")


def _compiles(src: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "main.mh")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        compile_source(path=path)


class SharedCompileErrorTests(unittest.TestCase):
    def e1(self, name: str) -> str:
        return (
            f"Method call on shared variable '{name}' outside 'lock {name} {{ }}': it would act on a copy; "
            f"write 'lock {name} {{ ... }}'"
        )

    def e2(self, name: str) -> str:
        return (
            f"Assignment into shared variable '{name}' outside 'lock {name} {{ }}': it would change a copy; "
            f"write 'lock {name} {{ ... }}'"
        )

    def e3(self, name: str) -> str:
        return (
            f"'{name} = ...' reads shared variable '{name}' outside 'lock {name} {{ }}': another thread can "
            f"change it in between; write 'lock {name} {{ ... }}'"
        )

    def test_e1_method_call(self):
        self.assertIn(self.e1("v"), _compile_error("shared let v = []\nv.push(1)"))

    def test_e1_through_an_index(self):
        self.assertIn(self.e1("v"), _compile_error("shared let v = [[1]]\nv[0].push(2)"))

    def test_e2_index(self):
        self.assertIn(self.e2("v"), _compile_error("shared let v = [1]\nv[0] = 2"))

    def test_e2_field(self):
        self.assertIn(self.e2("s"), _compile_error("struct S { a: Number }\nshared let s = S { a: 1 }\ns.a = 2"))

    def test_e3(self):
        self.assertIn(self.e3("n"), _compile_error("shared let n = 0\nn = n + 1"))

    def test_e3_through_a_closure(self):
        self.assertIn(self.e3("n"), _compile_error("shared let n = 0\nn = (fn() { n })()"))

    def test_e1_on_a_non_handle(self):
        self.assertIn(self.e1("c"), _compile_error("shared let c = none\nc.send(1)"))

    def test_handle_variables(self):
        _compiles(THREAD + "shared let c = thread.channel()\nc.send(1)")
        self.assertIn(
            self.e1("c"), _compile_error_path(THREAD + "shared let c = thread.channel()\nc.id.to_string()")
        )

    def test_e4(self):
        self.assertIn(
            "'lock' takes shared variables, and 'm' is not one", _compile_error("let m = 0\nlock m { }")
        )
        self.assertIn(
            "'lock' takes shared variables, and 'p.a' is not one",
            _compile_error("struct S { a: Number }\nlet p = S { a: 1 }\nlock p.a { }"),
        )

    def test_e5(self):
        self.assertIn(
            "'shared let' is only allowed at the top level of a file",
            _compile_error("fn f() {\n    shared let x = 1\n}"),
        )

    def test_e6(self):
        self.assertIn("'a' is locked twice in one 'lock'", _compile_error("shared let a = 0\nlock a, a { }"))

    def test_e7(self):
        self.assertIn(
            "'a' is a shared variable and can't be declared again in the same scope",
            _compile_error("shared let a = 0\nlet a = 1"),
        )

    def test_nested_closures_and_detach_inside_a_lock(self):
        self.assertIn(self.e1("v"), _compile_error("shared let v = []\nlock v { let f = fn() { v.push(1) } }"))
        self.assertIn(self.e1("v"), _compile_error("shared let v = []\nlock v { detach { v.push(1) } }"))
        compile_source(text="shared let v = []\nlock v { defer v.push(1) }")

    def test_return_inside_a_threaded_detach(self):
        self.assertIn(
            "can't leave a detached expression",
            _compile_error(THREAD + "let t = 1\nfn f() {\n    detach(t) { return 1 }\n}"),
        )


class WaitForGraphTests(unittest.TestCase):
    def setUp(self):
        from mah.thread_runtime import Pool, ThreadRuntime, _LockState

        self.rt = ThreadRuntime(None)
        self._LockState = _LockState
        self.Pool = Pool

    def own(self, k, task):
        state = self.rt.locks.setdefault(k, self._LockState())
        state.owner = (task, None)

    def test_lock_lock(self):
        rt = self.rt
        self.own(1, 10)
        self.own(2, 20)
        rt.waiting_on[20] = 1  # 20 waits for 10's lock
        self.assertTrue(rt.cycle(20, 10))  # 10 asking for 20's lock closes it

    def test_await_task_then_lock_back(self):
        rt = self.rt
        self.own(1, 10)
        rt.waiting_on[20] = 1
        self.assertTrue(rt.cycle_from(rt.producer_edges(("task", 20)), 10, False))

    def test_await_job_whose_root_waits_on_my_lock(self):
        rt = self.rt
        self.own(1, 10)
        rt.waiting_on[30] = 1
        self.assertFalse(rt.cycle_from(rt.producer_edges(("job", 5)), 10, True))  # not started
        rt.job_roots[5] = 30
        self.assertTrue(rt.cycle_from(rt.producer_edges(("job", 5)), 10, True))

    def test_join(self):
        rt = self.rt
        pool = self.Pool(rt, 1, "p", 1, None)
        rt.threads[1] = pool
        pool.running_set.add(7)
        rt.job_roots[7] = 40
        self.own(1, 10)
        rt.waiting_on[40] = 1
        self.assertTrue(rt.cycle_from(rt.producer_edges(("join", 1)), 10, True))

    def test_plain_await_cycle_is_not_reported(self):
        rt = self.rt
        rt.awaiting[20] = ("task", 10)
        self.assertFalse(rt.cycle_from(rt.producer_edges(("task", 20)), 10, False))


if __name__ == "__main__":
    unittest.main()
