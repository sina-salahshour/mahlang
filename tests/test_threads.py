"""M44 (docs/contracts/M44_threads.md #12.1): threads (`std:thread`,
`detach(t)`), shared variables, semaphores and channels; M45
(docs/contracts/M45_atomic.md #12.1): `atomic { }` transactions and `retry`.

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

    def test_t7_shared_variables_and_atomic(self):
        src = THREAD + """shared let n = 0
shared let xs = []
fn bump() {
    atomic {
        n = n + 1
        n
    }
}
print(atomic {
    bump()
    bump()
})
print(n)
atomic { xs.push("a") }
let mine = xs
atomic { xs.push("b") }
print(xs, mine)
let t = thread.spawn()
print(detach(t) {
    atomic { xs.push("c") }
    atomic { xs.len() }
}.await)
print(xs)
xs = ["reset"]
print(t.run(fn() { xs }).await)
shared let slot = none
try { slot = [detach { 1 }] } catch {
    e: ThreadError => { print(e.kind, e.message, slot) }
}
let p2 = detach { 2 }
try { atomic { slot = [p2] } } catch {
    e: ThreadError => { print(e.kind, e.message, slot) }
}
t.join()
"""
        self.assertEqual(
            run_source(src),
            "2\n2\n[a, b] [a]\n3\n[a, b, c]\n[reset]\n"
            "not_sendable shared variable 'slot' can't hold a Promise none\n"
            "not_sendable shared variable 'slot' can't hold a Promise none\n",
        )

    def test_t8_pool_and_shared_counters(self):
        # the lost-update test: 4 workers incrementing one counter
        src = THREAD + """shared let total = 0
shared let log = []
let pool = thread.spawn(name: "pool", workers: 4)
fn work(n) {
    for let i in 0..100 {
        atomic { total = total + 1 }
    }
    atomic { log.push(n) }
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
    atomic {
        inside = inside + 1
        if inside > most { most = inside }
    }
    sleep_async(20)
    atomic { inside = inside - 1 }
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
                f.write("export shared let hits = 0\nexport fn hit() {\n    atomic { hits = hits + 1 }\n}\n")
            main = os.path.join(td, "main.mh")
            with open(main, "w", encoding="utf-8") as f:
                f.write(
                    'import lib from "lib.mh"\n' + THREAD + "let t = thread.spawn()\nlib.hit()\n"
                    "t.run(lib.hit).await\natomic { lib.hits = lib.hits + 1 }\nprint(lib.hits)\n"
                )
            self.assertEqual(run_file(main), "3\n")

    def _run_modules(self, lib: str, main: str) -> str:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "lib.mh"), "w", encoding="utf-8") as f:
                f.write(lib)
            path = os.path.join(td, "main.mh")
            with open(path, "w", encoding="utf-8") as f:
                f.write(main)
            return run_file(path)

    def test_t13b_modules_keep_contextual_keywords(self):
        # the module's `atomic {` is the keyword although it declares `struct
        # atomic`; `atomic { v: 1 }` stays its struct literal; `retry(k)`
        # calls its function
        lib = (
            "struct atomic { v: Number }\nexport fn retry(n) { n + 1 }\nexport shared let k = 0\n"
            "export fn both() {\n    let s = atomic { v: 1 }\n    atomic {\n        k = retry(k)\n"
            "        k + s.v\n    }\n}\n"
        )
        self.assertEqual(
            self._run_modules(lib, 'import lib from "lib.mh"\nprint(lib.both(), lib.both(), lib.k)\n'), "2 3 2\n"
        )
        # `retry` in a module really retries
        lib = "export shared let k = 0\nexport fn wait_k() {\n    atomic {\n        if k == 0 { retry }\n        k\n    }\n}\n"
        self.assertEqual(
            self._run_modules(lib, 'import lib from "lib.mh"\nlet p = detach { lib.wait_k() }\nlib.k = 4\nprint(p.await)\n'),
            "4\n",
        )
        # the keyword is never rewritten into a reference to `lib`'s `retry`
        lib = (
            "export fn retry(n) { n }\nexport shared let k = 0\nexport fn f() {\n    atomic {\n"
            "        if k == 0 { retry }\n        k\n    }\n}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "lib.mh"), "w", encoding="utf-8") as f:
                f.write(lib)
            path = os.path.join(td, "main.mh")
            with open(path, "w", encoding="utf-8") as f:
                f.write('import "lib.mh"\nprint(f())\n')
            with self.assertRaises(Exception) as cm:
                compile_source(path=path)
        self.assertIn(
            "'retry' here is the keyword (it ends this 'atomic { }' run); rename the variable 'retry'",
            str(cm.exception),
        )

    def test_t14_contextual_words_stay_identifiers(self):
        self.assertEqual(
            run_source(
                "let shared = [1]\nlet atomic = 2\nlet retry = 3\nfn f(atomic) { atomic + 1 }\nfn g() {\n"
                "    let retry = 5\n    retry\n}\nprint(shared, atomic, retry, f(atomic), g())"
            ),
            "[1] 2 3 3 5\n",
        )
        self.assertEqual(run_source("struct atomic { v: Number }\nlet a = atomic { v: 1 }\nprint(a.v)"), "1\n")
        self.assertEqual(run_source('let atomic = true\nif atomic { print("cond") }'), "cond\n")
        self.assertEqual(run_source("shared let n = 1\nlet retry = 1\nprint(atomic { retry + n })"), "2\n")
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

    def test_t19_implicit_calls_share_their_callers_transaction(self):
        src = """shared let n = 0
struct P { x: Number }
impl Printable for P {
    fn to_string(self) {
        atomic { n = n + 1 }
        "P(" + self.x + ", n=" + n + ")"
    }
}
let s = atomic {
    n = 10
    "" + P { x: 1 }
}
print(s, n)
print(P { x: 2 })
"""
        self.assertEqual(run_source(src), "P(1, n=11) 11\nP(2, n=12)\n")

    def test_t20_only_lexical_reads_alias(self):
        src = """shared let xs = [1]
fn count_with(v) {
    let s = xs
    s.push(v)
    s.len()
}
print(count_with(2), xs)
print(atomic {
    xs.push(5)
    [count_with(9), xs.len()]
}, xs)
"""
        self.assertEqual(run_source(src), "2 [1]\n[3, 2] [1, 5]\n")

    # -- M45 (docs/contracts/M45_atomic.md #12.1) ---------------------------

    def test_a1_nested_atomic_composes(self):
        src = """shared let a = 0
shared let b = 0
fn move(n) {
    atomic {
        a = a - n
        b = b + n
    }
}
fn move_twice(n) {
    atomic {
        move(n)
        move(n)
        [a, b]
    }
}
print(move_twice(5), a, b)
let r = atomic {
    a = 100
    try {
        atomic {
            b = 100
            throw RuntimeError.ArgumentError { message: "inner" }
        }
    } catch {
        e => { "caught " + e.message() }
    }
}
print(r, a, b)
"""
        self.assertEqual(run_source(src), "[-10, 10] -10 10\ncaught inner 100 100\n")

    def test_a2_a_throw_publishes_nothing(self):
        src = """shared let xs = [1]
shared let n = 0
try {
    atomic {
        xs.push(2)
        n = 5
        throw RuntimeError.ArgumentError { message: "stop" }
    }
} catch {
    e => { print("caught", e.message()) }
}
print(xs, n)
shared let slot = []
let p = detach { 1 }
try {
    atomic {
        n = 7
        slot.push(p)
    }
} catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
print(slot, n)
"""
        self.assertEqual(
            run_source(src),
            "caught stop\n[1] 0\nnot_sendable | shared variable 'slot' can't hold a Promise\n[] 0\n",
        )

    def test_a3_retry_waits_for_a_change(self):
        src = """shared let box = ""
let waiter = detach {
    atomic {
        if box == "" { retry }
        box
    }
}
box = "filled"
print(waiter.await)
"""
        self.assertEqual(run_source(src), "filled\n")

    def test_a4_retry_as_a_blocking_queue(self):
        src = THREAD + """shared let queue = []
fn take() {
    atomic {
        if queue.len() == 0 { retry }
        queue.pop_start()
    }
}
fn put(v) {
    atomic { queue.push(v) }
}
let t = thread.spawn(name: "consumer")
let got = t.run(fn() {
    let out = []
    for let i in 0..5 { out.push(take()) }
    out
})
for let i in 1..=5 { put(i * 10) }
print(got.await)
t.join()
"""
        self.assertEqual(run_source(src), "[10, 20, 30, 40, 50]\n")

    def test_a5_hopeless_retry_is_stuck(self):
        src = """shared let flag = false
try {
    atomic {
        if !flag { retry }
        1
    }
} catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
try { atomic { retry } } catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
print("end")
"""
        self.assertEqual(
            run_source(src),
            "stuck | the wait can never finish: every thread is waiting\n"
            "stuck | retry can never wake up: this transaction read no shared variable\nend\n",
        )

    def test_a6_side_effects_throw_in_atomic(self):
        src = THREAD + """fn say(s) { print(s) }
fn nap() { sleep_async(1) }
fn wait_for(p) { p.await }
fn spawn_one() { detach { 1 } }
let ch = thread.channel()
fn post(v) { ch.send(v) }
let done = detach { 1 }
let tries = [fn() { say("hi") }, fn() { nap() }, fn() { wait_for(done) }, fn() { spawn_one() }, fn() { post(1) }]
for let f in tries {
    try {
        atomic { f() }
    } catch {
        e: ThreadError => { print(e.kind, "|", e.message) }
    }
}
print(ch.len())
"""
        line = "in_atomic | '{}' can't run inside 'atomic {{ }}': its body may run more than once\n"
        expected = "".join(
            line.format(x) for x in ("io.write", "time.sleep_async", ".await", "detach", "thread.channel_send")
        )
        self.assertEqual(run_source(src), expected + "0\n")

    def test_a8_bank_transfers_keep_the_total(self):
        src = THREAD + """shared let accounts = [100, 100, 100, 100]
fn transfer(from, to, amount) {
    atomic {
        accounts[from] = accounts[from] - amount
        accounts[to] = accounts[to] + amount
    }
}
fn worker(seed) {
    let bad = 0
    for let i in 0..200 {
        transfer((seed + i) % 4, (seed + i * 3 + 1) % 4, 1 + i % 7)
        let total = atomic {
            let s = 0
            for let a in accounts { s = s + a }
            s
        }
        if total != 400 { bad = bad + 1 }
    }
    bad
}
let pool = thread.spawn(workers: 4)
let jobs = []
for let s in 0..4 { jobs.push(pool.run(worker, s)) }
let bad = 0
for let j in jobs { bad = bad + j.await }
let final = accounts
let sum = 0
for let a in final { sum = sum + a }
print(bad, sum, final.len())
pool.join()
"""
        self.assertEqual(run_source(src), "0 400 4\n")

    def _python_vm(self) -> bool:
        return os.environ.get("MAH_TEST_VM") != "rust"

    def test_a9_exclusive_mode_ends_starvation(self):
        from mah.thread_runtime import TX_STATS

        src = THREAD + """shared let hot = 0
shared let stop = false
fn hammer() {
    let n = 0
    while !stop {
        atomic { hot = hot + 1 }
        n = n + 1
    }
    n
}
fn slow() {
    while hot < 50 { }
    atomic {
        let seen = hot
        let s = 0
        for let i in 0..20000 { s = s + i }
        hot = seen + 1000000
        s
    }
}
let pool = thread.spawn(name: "hammers", workers: 3)
let hs = []
for let i in 0..3 { hs.push(pool.run(hammer)) }
let t = thread.spawn(name: "slow")
let s = t.run(slow).await
stop = true
let total = 0
for let h in hs { total = total + h.await }
print(s, hot - total)
pool.join()
t.join()
"""
        TX_STATS["conflicts"] = 0
        TX_STATS["exclusive"] = 0
        self.assertEqual(run_source(src), "199990000 1000000\n")
        if self._python_vm():
            self.assertGreaterEqual(TX_STATS["exclusive"], 1)

    def test_a10_a_join_cycle_is_a_deadlock(self):
        src = THREAD + """let t1 = thread.spawn(name: "a")
let t2 = thread.spawn(name: "b")
let go = thread.channel()
fn join_other(other) {
    go.recv()
    try {
        other.join()
        "joined"
    } catch {
        e: ThreadError => { e.message }
    }
}
let pa = t1.run(join_other, t2)
let pb = t2.run(join_other, t1)
go.send(1)
go.send(2)
let ra = pa.await
let rb = pb.await
let msg = "deadlock: this await would never end (it waits, through threads, for itself)"
print(ra == "joined" | rb == "joined", ra == msg | rb == msg)
"""
        self.assertEqual(run_source(src), "true true\n")

    def test_a11_a_failed_job_drops_its_retry_waits(self):
        src = THREAD + """shared let k = 0
let t = thread.spawn()
let p = detach(t) {
    detach {
        atomic {
            if k == 0 { retry }
            k
        }
    }
    sleep_async(10)
    throw RuntimeError.ArgumentError { message: "boom" }
}
try { p.await } catch {
    e => { print("failed") }
}
k = 1
print(k)
t.join()
"""
        self.assertEqual(run_source(src), "failed\n1\n")

    def test_a12_assigning_a_working_value_copies(self):
        src = """shared let xs = [1]
shared let ys = []
print(atomic {
    ys = xs
    ys.push(2)
    xs.len()
}, xs, ys)
let v = [1]
atomic {
    xs = v
    xs.push(3)
}
print(v, xs)
"""
        self.assertEqual(run_source(src), "1 [1] [1, 2]\n[1] [1, 3]\n")

    def test_a13_a_rerun_never_changes_what_it_assigned_from(self):
        from mah.thread_runtime import TX_STATS

        src = THREAD + """shared let xs = [0]
shared let ys = []
shared let stop = false
fn hammer() {
    let n = 0
    while !stop {
        n = n + 1
        xs = [n]
    }
    n
}
let t = thread.spawn(name: "hammer")
let h = t.run(hammer)
while xs[0] == 0 { }
let v = [1]
let r = atomic {
    let seen = xs
    ys = v
    ys.push(0)
    let i = 0
    while i < 20000 { i = i + 1 }
    seen.len()
}
stop = true
h.await
print(r, v, ys)
t.join()
"""
        TX_STATS["conflicts"] = 0
        TX_STATS["exclusive"] = 0
        self.assertEqual(run_source(src), "1 [1] [1, 0]\n")
        if self._python_vm():
            self.assertGreaterEqual(TX_STATS["conflicts"], 1)

    def test_a14_retry_waiters_wake_in_the_order_they_waited(self):
        # M45 review: one write wakes every waiter of the variable in the
        # order they started waiting, on both VMs (the Python VM used to
        # wake them in set-hash order)
        src = """shared let go = 0
shared let other = 0
fn waiter(i) {
    atomic {
        if i % 2 == 0 {
            let o = other
        }
        if go == 0 { retry }
    }
    print(i)
}
let ps = []
for let i in 0..12 { ps.push(detach waiter(i)) }
go = 1
for let p in ps { p.await }
"""
        self.assertEqual(run_source(src), "".join(f"{i}\n" for i in range(12)))

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

    def test_t25_handle_variables_need_no_atomic(self):
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
            f"Method call on shared variable '{name}' outside 'atomic {{ }}': it would act on a copy; "
            "wrap it in 'atomic { ... }'"
        )

    def e2(self, name: str) -> str:
        return (
            f"Assignment into shared variable '{name}' outside 'atomic {{ }}': it would change a copy; "
            "wrap it in 'atomic { ... }'"
        )

    def e3(self, name: str) -> str:
        return (
            f"'{name} = ...' reads shared variable '{name}' outside 'atomic {{ }}': another thread can "
            "change it in between; wrap it in 'atomic { ... }'"
        )

    def e4(self, what: str) -> str:
        return f"'{what}' can't be used inside 'atomic {{ }}': its body may run more than once"

    E6 = "'retry' is only allowed inside 'atomic { }'"
    E6B = "'retry' here is the keyword (it ends this 'atomic { }' run); rename the variable 'retry'"

    def e8(self, keyword: str) -> str:
        return f"'{keyword}' can't leave an 'atomic {{ }}' block"

    def e9(self, name: str) -> str:
        return (
            f"'{name}' is declared outside 'atomic {{ }}', and changing it there isn't undone when the block "
            "runs again; return what you need as the block's value"
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

    def test_nested_closures_and_defer_inside_atomic(self):
        self.assertIn(self.e1("v"), _compile_error("shared let v = []\natomic { let f = fn() { v.push(1) } }"))
        compile_source(text="shared let v = []\natomic { defer v.push(1) }")

    def test_e4(self):
        for src, what in (
            ("let p = detach { 1 }\natomic { p.await }", ".await"),
            ("atomic { sleep_async(1) }", "sleep_async"),
            ("atomic { print(1) }", "print"),
            ("atomic { detach { 1 } }", "detach"),
            ("let t = 1\natomic { detach(t) { 1 } }", "detach"),
            ('atomic { input("? ") }', "input"),
        ):
            with self.subTest(src=src):
                self.assertIn(self.e4(what), _compile_error(src))
        compile_source(text="atomic { let f = fn(p) { p.await } }")
        compile_source(text="fn input(x) { x }\natomic { input(1) }")

    def test_e5(self):
        self.assertIn(
            "'shared let' is only allowed at the top level of a file",
            _compile_error("fn f() {\n    shared let x = 1\n}"),
        )

    def test_e6(self):
        for src in ("retry", "fn f() {\n    retry\n}", "atomic { let f = fn() { retry } }"):
            with self.subTest(src=src):
                self.assertIn(self.E6, _compile_error(src))
        compile_source(text="let retry = 1\nprint(retry)")

    def test_e6b(self):
        for src in (
            "let retry = 1\natomic {\n    let y = retry\n}",
            "fn g(x) { x }\nfn f(retry) {\n    atomic { g(retry) }\n}",
            "fn retry() { 1 }\natomic { [retry] }",
        ):
            with self.subTest(src=src):
                self.assertIn(self.E6B, _compile_error(src))
        compile_source(text="let retry = 1\nshared let n = 0\natomic { n = retry + n }")

    def test_e7(self):
        self.assertIn(
            "'a' is a shared variable and can't be declared again in the same scope",
            _compile_error("shared let a = 0\nlet a = 1"),
        )

    def test_e8(self):
        for src, keyword in (
            ("fn f() {\n    atomic { return 1 }\n}", "return"),
            ("while true {\n    atomic { break }\n}", "break"),
            ("for let i in 0..3 {\n    atomic { continue }\n}", "continue"),
        ):
            with self.subTest(src=src):
                self.assertIn(self.e8(keyword), _compile_error(src))
        compile_source(text="atomic {\n    for let i in 0..3 { break }\n}")

    def test_e9(self):
        for src, name in (
            ("let count = 0\natomic { count = count + 1 }", "count"),
            ("let v = [1]\natomic { v[0] = 2 }", "v"),
            ("fn f(x) {\n    atomic { x = 1 }\n}", "x"),
            ("struct S { n: Number }\nimpl S {\n    fn bump(self) {\n        atomic { self.n = 1 }\n    }\n}", "self"),
        ):
            with self.subTest(src=src):
                self.assertIn(self.e9(name), _compile_error(src))
        for src in (
            "atomic {\n    let c = 0\n    c = c + 1\n    c\n}",
            "atomic {\n    let c = 0\n    atomic { c = 1 }\n    c\n}",
            "let g = 0\natomic { let f = fn() { g = 1 } }",
        ):
            with self.subTest(src=src):
                compile_source(text=src)

    def test_return_inside_a_threaded_detach(self):
        self.assertIn(
            "can't leave a detached expression",
            _compile_error(THREAD + "let t = 1\nfn f() {\n    detach(t) { return 1 }\n}"),
        )


class WaitForGraphTests(unittest.TestCase):
    def setUp(self):
        from mah.thread_runtime import Pool, ThreadRuntime

        self.rt = ThreadRuntime(None)
        self.Pool = Pool

    def test_join_cycle(self):
        rt = self.rt
        p1 = self.Pool(rt, 1, "a", 1, None)
        p2 = self.Pool(rt, 2, "b", 1, None)
        rt.threads[1] = p1
        rt.threads[2] = p2
        p1.running_set.add(7)
        rt.job_roots[7] = 40
        p2.running_set.add(8)
        rt.job_roots[8] = 50
        rt.awaiting[40] = ("join", 2)
        self.assertTrue(rt.cycle_from(rt.producer_edges(("join", 1)), 50, True))

    def test_a_job_not_started_yet(self):
        rt = self.rt
        rt.awaiting[30] = ("job", 6)
        rt.job_roots[6] = 10
        self.assertFalse(rt.cycle_from(rt.producer_edges(("job", 5)), 10, True))
        rt.job_roots[5] = 30
        self.assertTrue(rt.cycle_from(rt.producer_edges(("job", 5)), 10, True))

    def test_plain_await_cycle_is_not_reported(self):
        rt = self.rt
        rt.awaiting[20] = ("task", 10)
        self.assertFalse(rt.cycle_from(rt.producer_edges(("task", 20)), 10, False))


class _Io:
    def __init__(self):
        import queue

        self.done = queue.Queue()
        self.pending = 0


class AtomicRuntimeTests(unittest.TestCase):
    """M45 (docs/contracts/M45_atomic.md #6): the runtime's transaction
    helpers, driven by hand."""

    def setUp(self):
        from mah.thread_runtime import ThreadRuntime, VmThreads

        self.rt = ThreadRuntime(None)
        self.vt = VmThreads(self.rt, 0, _Io(), None)
        self.rt.vms[0] = self.vt
        self.vt2 = VmThreads(self.rt, 1, _Io(), None)
        self.rt.vms[1] = self.vt2

    def tx(self):
        from mah.runtime_values import Tx

        return Tx(None, None, None)

    def test_1_validation(self):
        from decimal import Decimal

        rt, vt = self.rt, self.vt
        rt.write_shared(vt, 0, Decimal(1))
        tx = self.tx()
        rt.tx_start(vt, tx)
        self.assertEqual(tx.rv, 1)
        tx.reads[0] = 1
        rt.write_shared(vt, 0, Decimal(2))
        self.assertFalse(rt.tx_commit(vt, tx, [(0, Decimal(5))]))
        self.assertEqual(rt.read_shared(0), (Decimal(2), 2))
        tx2 = self.tx()
        rt.tx_start(vt, tx2)
        tx2.reads[0] = 2
        self.assertTrue(rt.tx_commit(vt, tx2, [(0, Decimal(5))]))
        self.assertEqual(rt.read_shared(0), (Decimal(5), 3))

    def test_2_read_only(self):
        from decimal import Decimal

        rt, vt = self.rt, self.vt
        rt.write_shared(vt, 0, Decimal(1))
        tx = self.tx()
        rt.tx_start(vt, tx)
        tx.reads[0] = 1
        rt.write_shared(vt, 0, Decimal(2))
        clock = rt.clock
        self.assertTrue(rt.tx_commit(vt, tx, []))
        self.assertEqual(rt.clock, clock)

    def test_3_retry_wake_ups(self):
        from decimal import Decimal

        from mah.runtime_values import NONE_VALUE, PromiseInstance

        rt, vt = self.rt, self.vt
        rt.write_shared(vt, 0, Decimal(1))
        rt.write_shared(vt, 1, Decimal(1))
        p = rt.retry_wait(vt, {0: rt.read_shared(0)[1]})
        self.assertIsInstance(p, PromiseInstance)
        self.assertEqual(p.variant, "Pending")
        rt.write_shared(vt, 1, Decimal(2))
        self.assertTrue(vt.done.empty())
        rt.write_shared(vt, 0, Decimal(2))
        self.assertEqual(vt.done.get_nowait(), (p, "settle", (True, NONE_VALUE)))
        self.assertIsNone(rt.retry_wait(vt, {0: 1}))

    def test_4_exclusivity_blocks_writers(self):
        import threading
        import time
        from decimal import Decimal

        rt, vt = self.rt, self.vt
        tx = self.tx()
        tx.irrevocable = True
        rt.tx_start(vt, tx)
        writer = threading.Thread(target=rt.write_shared, args=(self.vt2, 0, Decimal(9)))
        writer.start()
        time.sleep(0.2)
        self.assertEqual(rt.read_shared(0)[1], 0)
        self.assertTrue(writer.is_alive())
        rt.end_attempt(tx)
        writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(rt.read_shared(0)[0], Decimal(9))

    def test_5_one_exclusive_at_a_time(self):
        import threading
        import time

        rt, vt = self.rt, self.vt
        tx1 = self.tx()
        tx1.irrevocable = True
        rt.tx_start(vt, tx1)
        tx2 = self.tx()
        tx2.irrevocable = True
        starter = threading.Thread(target=rt.tx_start, args=(self.vt2, tx2))
        starter.start()
        time.sleep(0.2)
        self.assertTrue(starter.is_alive())
        self.assertEqual(rt.excl_owner[0], tx1.serial)
        rt.end_attempt(tx1)
        starter.join(timeout=5)
        self.assertFalse(starter.is_alive())
        self.assertEqual(rt.excl_owner[0], tx2.serial)
        rt.end_attempt(tx2)

    def _task_in(self, attempts, implicit=None):
        from mah.runtime_values import Task, Tx

        task = Task(pc=0, current_frame=None)
        tx = Tx(task, implicit, (0, None, 0, 0))
        tx.depth = 0
        tx.attempts = attempts
        task.tx = tx
        return task, tx

    def test_6_the_ninth_attempt_is_exclusive(self):
        from mah.thread_runtime import ATOMIC_ATTEMPTS

        rt, vt = self.rt, self.vt
        task, tx = self._task_in(ATOMIC_ATTEMPTS)
        vt.begin(task, 0)
        self.assertTrue(tx.irrevocable)
        self.assertEqual(tx.depth, 1)
        self.assertEqual(rt.excl_owner, (tx.serial, 0))
        vt.end(task)
        self.assertIsNone(rt.excl_owner)
        self.assertIsNone(task.tx)
        task, tx = self._task_in(ATOMIC_ATTEMPTS - 1)
        vt.begin(task, 0)
        self.assertFalse(tx.irrevocable)
        self.assertIsNone(rt.excl_owner)
        vt.end(task)
        task, tx = self._task_in(ATOMIC_ATTEMPTS, implicit="to_string")
        vt.begin(task, 0)
        self.assertTrue(tx.irrevocable)
        self.assertEqual(rt.excl_owner, (tx.serial, 0))
        vt.end(task)
        self.assertIsNone(rt.excl_owner)

    def test_7_guards_release_on_error(self):
        from mah.code_interpreter import _Timeout
        from mah.thread_runtime import ATOMIC_ATTEMPTS

        rt, vt = self.rt, self.vt
        tx2 = self.tx()
        tx2.irrevocable = True
        rt.tx_start(self.vt2, tx2)
        task, _tx = self._task_in(ATOMIC_ATTEMPTS)

        def boom():
            raise _Timeout()

        vt.wait_check = boom
        with self.assertRaises(_Timeout):
            vt.begin(task, 0)
        self.assertIsNone(task.tx)
        self.assertEqual(len(rt.excl_queue), 0)
        rt.end_attempt(tx2)
        self.assertIsNone(rt.excl_owner)


class RefusedNativesParityTests(unittest.TestCase):
    def test_the_list(self):
        import re

        import mah.natives
        from mah.thread_runtime import ATOMIC_REFUSED_NATIVES

        self.assertEqual(len(ATOMIC_REFUSED_NATIVES), 53)
        self.assertLessEqual(ATOMIC_REFUSED_NATIVES, set(mah.natives.NATIVES))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "runtime", "src", "vm", "thread.rs")
        if not os.path.exists(path):
            self.skipTest("no Rust tree")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        start = text.find("pub const ATOMIC_REFUSED_NATIVES")
        if start < 0:
            self.skipTest("the Rust VM has no ATOMIC_REFUSED_NATIVES yet (M45 Part 2)")
        end = text.index("];", start)
        self.assertEqual(set(re.findall(r'"([^"]+)"', text[start:end])), set(ATOMIC_REFUSED_NATIVES))


class QuiescenceWindowTests(unittest.TestCase):
    """Review fix (#6.5/#6.10): a finished job stays in `finishing` until its
    worker posts the reply, so a quiescence check another thread runs between the
    job's teardown and that post can't fail the main VM's wait for the reply
    with `stuck`."""

    def test_a_torn_down_job_vm_counts_until_its_reply_is_posted(self):
        import queue

        from mah.thread_runtime import ThreadRuntime, VmThreads

        class _Io:
            def __init__(self):
                self.done = queue.Queue()
                self.pending = 0

        rt = ThreadRuntime(None)
        main = VmThreads(rt, 0, _Io(), None)
        rt.vms[0] = main
        job_vm = VmThreads(rt, 1, _Io(), object())
        job_vm.job_id = 7
        rt.vms[1] = job_vm
        # the main VM waits only for the job's reply: blocked
        main.add_wait(object(), "job", 1)
        rt.about_to_block(main)
        self.assertTrue(main.done.empty())
        # the job ends: teardown, then (before the worker posts the reply)
        # another thread runs the quiescence check
        rt.forget_vm(job_vm)
        with rt.lock:
            rt.check_quiescence()
        self.assertTrue(main.done.empty(), "stuck was posted while the job's reply was on its way")
        with rt.lock:
            rt.finishing.discard(job_vm.job_id)  # as _worker does
            rt.post(main, (None, "job", (True, None)))
        self.assertEqual(main.done.get_nowait()[1], "job")


if __name__ == "__main__":
    unittest.main()
