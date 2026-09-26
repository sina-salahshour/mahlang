#!/usr/bin/env python3
"""Differential test for `runtime/src/decimal.rs` (Mah's `Number`) against
Python's `decimal` module, run under the exact context the reference VM
relies on (Python's own *default* `Context`, which already has
`prec=28, rounding=ROUND_HALF_EVEN, Emax=999999, Emin=-999999,
traps=[InvalidOperation, DivisionByZero, Overflow]` -- see
`mah/code_interpreter.py`, which never touches `decimal.getcontext()`).

Builds `runtime/src/bin/dec_oracle.rs` (`cargo build --release --bin
dec_oracle`), generates a large batch of random test cases (seeded, so
this is reproducible), computes each case's expected line with Python's
`decimal`, runs every case through a single `dec_oracle` process, and
reports mismatches.

Usage (from the repo root or anywhere):
    python3 runtime/tests/decimal_diff.py [-v] [--cases N] [--seed N]

Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import decimal
import os
import subprocess
import sys
import time
from decimal import Decimal as D

# Some boundary/subnormal cases legitimately involve ~1e6-digit integers
# (e.g. `10 ** 999999`) -- lift Python's default int<->str digit-count
# guard (only relevant on 3.11+; harmless no-op call otherwise).
if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME_DIR = os.path.join(REPO_ROOT, "runtime")
ORACLE_BIN = os.path.join(RUNTIME_DIR, "target", "release", "dec_oracle")

# ---------------------------------------------------------------------------
# The exact context the reference VM runs under -- see module docstring.
# ---------------------------------------------------------------------------

CTX = decimal.Context(
    prec=28,
    rounding=decimal.ROUND_HALF_EVEN,
    Emax=999999,
    Emin=-999999,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
)
decimal.setcontext(CTX)


def format_number(v: D) -> str:
    """Exactly `mah/code_interpreter.py`'s `_format_number`
    (`str(int(v))` for an integral value, else `format(v.normalize(), "f")`)
    -- but computed via `as_tuple()` string manipulation instead of an
    actual `int(v)` conversion. CPython's Decimal->int conversion is
    quadratic-ish and takes *seconds* for the ~1e6-digit integers that
    legitimately show up here (e.g. `10 ** 999999`); `as_tuple()` gives the
    exact same digits without that cost."""
    if v == v.to_integral_value():
        sign, digits, exponent = v.as_tuple()
        digit_str = "".join(map(str, digits))
        if exponent >= 0:
            full = digit_str + "0" * exponent
        else:
            cut = len(digit_str) + exponent
            full = digit_str[:cut] if cut > 0 else "0"
        full = full.lstrip("0") or "0"
        if full == "0":
            sign = 0
        return ("-" if sign else "") + full
    return format(v.normalize(), "f")


def safe_format(fn) -> str:
    try:
        return format_number(fn())
    except Exception as e:  # decimal signal exceptions: str(e) is exactly
        # e.g. "[<class 'decimal.InvalidOperation'>]"
        return f"ERR {e}"


DIV_BY_ZERO = "ERR [<class 'decimal.DivisionByZero'>]"
INVALID_OP = "ERR [<class 'decimal.InvalidOperation'>]"


# ---------------------------------------------------------------------------
# Value generators (decimal-literal strings, in the same syntax our Rust
# `Decimal::parse` and Python's `Decimal(str)` both accept).
# ---------------------------------------------------------------------------


def rand_digit_string(rng, n: int) -> str:
    if n <= 0:
        return "0"
    first = rng.choice("123456789")
    rest = "".join(rng.choice("0123456789") for _ in range(n - 1))
    return first + rest


def digits_value(rng, lo: int, hi: int) -> str:
    """A value with between `lo` and `hi` significant digits, random
    decimal-point placement, random sign, and occasionally an `E` suffix
    (small exponent -- this does not target the boundary/subnormal range,
    see `boundary_value` for that)."""
    n = rng.randint(lo, hi)
    digits = rand_digit_string(rng, n)
    frac_len = rng.randint(0, n)
    if frac_len == 0:
        s = digits
    elif frac_len == n:
        s = "0." + digits
    else:
        s = digits[: n - frac_len] + "." + digits[n - frac_len :]
    if rng.random() < 0.5:
        s = "-" + s
    if rng.random() < 0.3:
        e = rng.randint(-20, 20)
        s = f"{s}E{e}"
    return s


def small_int(rng) -> str:
    return str(rng.randint(-100_000, 100_000))


def general_value(rng) -> str:
    c = rng.random()
    if c < 0.12:
        return "0"
    if c < 0.4:
        return small_int(rng)
    if c < 0.75:
        return digits_value(rng, 1, 28)
    return digits_value(rng, 29, 40)


def boundary_value(rng) -> str:
    """Values near +-Emax/Emin and in the subnormal range -- deliberately
    a small, separate pool (see the module docstring in decimal_diff.py's
    generation section): formatting such a value can produce an ~1e6
    character string, so we keep the *count* of these cases bounded."""
    n = rng.randint(1, 6)
    digits = rand_digit_string(rng, n)
    sign = "-" if rng.random() < 0.5 else ""
    c = rng.random()
    if c < 0.25:
        e = rng.randint(999_990, 999_999)
    elif c < 0.5:
        e = rng.randint(-999_999, -999_990)
    elif c < 0.75:
        e = rng.randint(-1_000_035, -1_000_020)  # around Etiny (subnormal)
    else:
        e = rng.randint(-500_000, 500_000)
    return f"{sign}{digits}E{e}"


def pow_base(rng) -> str:
    """Base in (0, 1e6)."""
    if rng.random() < 0.35:
        return str(rng.randint(1, 999_999))
    n = rng.uniform(1e-6, 999_999)
    return f"{n:.{rng.randint(1, 10)}f}"


def pow_exponent(rng) -> str:
    """Exponent in (-100, 100)."""
    if rng.random() < 0.5:
        return str(rng.randint(-100, 100))
    n = rng.uniform(-100, 100)
    return f"{n:.{rng.randint(1, 8)}f}"


def pow_int_large(rng) -> tuple[str, str]:
    """A 6-digit integer base raised to an integer exponent up to +-90 --
    the specific shape (integer base, integer exponent, a result well
    beyond 28 digits) where CPython's `Decimal.__pow__` uses libmpdec's
    `_mpd_qpow_int` binary-exponentiation-with-double-rounding algorithm
    rather than an exact-then-round computation (see
    `pow_integer_mpdec` in decimal.rs); heavily overrepresented here on
    purpose since it's the one path known to be easy to get subtly wrong."""
    base = rng.randint(100_000, 999_999)
    exp = rng.randint(1, 90)
    if rng.random() < 0.5:
        exp = -exp
    return str(base), str(exp)


def exp_arg(rng) -> str:
    """`exp()` argument in (-2_400_000, 2_400_000), including small ones."""
    c = rng.random()
    if c < 0.25:
        return digits_value(rng, 1, 10)
    if c < 0.6:
        n = rng.uniform(-2_400_000, 2_400_000)
        return f"{n:.6f}"
    return str(rng.randint(-2_400_000, 2_400_000))


def ln_arg(rng) -> str:
    """`ln()` argument across the full valid range, including some <= 0
    (error) cases."""
    c = rng.random()
    if c < 0.1:
        return rng.choice(["0", "-1", "-0.5", str(-rng.randint(1, 1000))])
    if c < 0.4:
        return digits_value(rng, 1, 28).lstrip("-") or "0"
    if c < 0.7:
        return digits_value(rng, 29, 40).lstrip("-") or "0"
    return boundary_value(rng).lstrip("-")


POW_SPECIAL = [
    ("0", "0"),
    ("0", "-1"),
    ("0", "2"),
    ("0", "0.5"),
    ("0", "-2.5"),
    ("1", "0"),
    ("1", "123456789012345"),
    ("1", "-5"),
    ("1", "0.5"),
    ("-1", "3"),
    ("-1", "4"),
    ("-1", "1000000000000000000000000000001"),
    ("-2", "3"),
    ("-2", "0.5"),
    ("-8", "0.5"),
    ("2", "100"),
    ("2", "-1"),
    ("3", "-2"),
    ("10", "30"),
    ("10", "1000000"),
    ("10", "999999"),
    ("4", "0.5"),
    ("2", "2.25"),
    ("2", "0.1"),
    ("10", "30"),
    # Confirmed (via int-power, 200-digit `decimal` recomputation, and
    # `ctx.power`) cases where CPython's libmpdec integer-power algorithm's
    # double rounding differs from the mathematically-exact-then-round-once
    # answer in the last digit -- decimal.rs must replicate the former.
    ("514488", "21"),
    ("985219", "-67"),
]

WORKED_EXAMPLES = [
    ("pow", "2", "100"),
    ("add", "0.1", "0.2"),
    ("div", "1", "3"),
    ("div", "2", "3"),
    ("idiv", "-7", "2"),
    ("rem", "-7", "3"),
    ("rem", "7.5", "-2"),
    ("pow", "2", "0.5"),
    ("pow", "2", "-1"),
    ("pow", "3", "-2"),
    ("pow", "10", "30"),
    ("fmt", "1234567890123456789012345678901234567890", None),
    ("fmt", "0.1234567890123456789012345678901", None),
    ("fmt", "1.50", None),
    ("pow", "4", "0.5"),
    ("pow", "2", "2.25"),
    ("pow", "-8", "0.5"),
    ("pow", "0", "0"),
    ("pow", "10", "1000000"),
    ("idiv", "1000000000000000000000000000000", "0.001"),
    ("rem", "1000000000000000000000000000000", "0.001"),
    ("pow", "2", "0.1"),
    ("div", "1E-999999", "10"),
]


# ---------------------------------------------------------------------------
# Case generation
# ---------------------------------------------------------------------------


def gen_cases(rng, n_each: dict) -> list[tuple[str, str, str | None]]:
    cases: list[tuple[str, str, str | None]] = list(WORKED_EXAMPLES)
    for a, b in POW_SPECIAL:
        cases.append(("pow", a, b))

    for _ in range(n_each["add"]):
        cases.append(("add", general_value(rng), general_value(rng)))
    for _ in range(n_each["sub"]):
        cases.append(("sub", general_value(rng), general_value(rng)))
    for _ in range(n_each["mul"]):
        cases.append(("mul", general_value(rng), general_value(rng)))
    for _ in range(n_each["div"]):
        b = general_value(rng)
        if rng.random() < 0.05:
            b = "0"
        cases.append(("div", general_value(rng), b))
    for _ in range(n_each["idiv"]):
        b = general_value(rng)
        if rng.random() < 0.05:
            b = "0"
        cases.append(("idiv", general_value(rng), b))
    for _ in range(n_each["rem"]):
        b = general_value(rng)
        if rng.random() < 0.05:
            b = "0"
        cases.append(("rem", general_value(rng), b))
    for _ in range(n_each["neg"]):
        cases.append(("neg", general_value(rng), None))
    for _ in range(n_each["pow"]):
        # Half of all random pow cases specifically target 6-digit
        # integer-base ^ integer-exponent(<=90) -- see `pow_int_large`'s
        # docstring for why this shape gets outsized coverage.
        if rng.random() < 0.5:
            base, exp = pow_int_large(rng)
        else:
            base, exp = pow_base(rng), pow_exponent(rng)
        cases.append(("pow", base, exp))
    for _ in range(n_each["exp"]):
        cases.append(("exp", exp_arg(rng), None))
    for _ in range(n_each["ln"]):
        cases.append(("ln", ln_arg(rng), None))
    for _ in range(n_each["fmt"]):
        cases.append(("fmt", general_value(rng), None))
    for _ in range(n_each["cmp"]):
        cases.append(("cmp", general_value(rng), general_value(rng)))
    for _ in range(n_each["f64"]):
        cases.append(("f64", general_value(rng), None))
    for _ in range(n_each["int"]):
        cases.append(("int", general_value(rng), None))

    # A modest, separately-bounded pool of boundary/subnormal cases (see
    # `boundary_value`'s docstring for why this is kept small).
    boundary_ops = ["add", "mul", "div", "fmt", "cmp", "pow", "exp", "ln"]
    for _ in range(n_each["boundary"]):
        op = rng.choice(boundary_ops)
        if op in ("fmt",):
            cases.append((op, boundary_value(rng), None))
        elif op == "pow":
            cases.append((op, boundary_value(rng), pow_exponent(rng)))
        elif op == "exp":
            cases.append((op, str(rng.randint(-2_400_000, 2_400_000)), None))
        elif op == "ln":
            cases.append((op, boundary_value(rng).lstrip("-"), None))
        elif op in ("cmp",):
            cases.append((op, boundary_value(rng), general_value(rng)))
        else:
            cases.append((op, boundary_value(rng), general_value(rng)))

    return cases


# ---------------------------------------------------------------------------
# Expected-output computation (Python `decimal`, same context)
# ---------------------------------------------------------------------------


def expected_line(op: str, a_str: str, b_str: str | None) -> str:
    a = D(a_str)
    b = D(b_str) if b_str is not None else None

    if op == "add":
        return safe_format(lambda: a + b)
    if op == "sub":
        return safe_format(lambda: a - b)
    if op == "mul":
        return safe_format(lambda: a * b)
    if op == "div":
        if b == 0:
            return DIV_BY_ZERO
        return safe_format(lambda: a / b)
    if op == "idiv":
        if b == 0:
            return DIV_BY_ZERO
        return safe_format(lambda: a // b)
    if op == "rem":
        if b == 0:
            return DIV_BY_ZERO
        return safe_format(lambda: a % b)
    if op == "pow":
        if a == 0 and b < 0:
            # Python's decimal returns untrapped Infinity here; we
            # deliberately raise instead (Mah has no Infinity value) -- see
            # runtime/src/decimal.rs's `pow` docs and
            # mah/code_interpreter.py's `_binop`.
            return DIV_BY_ZERO
        return safe_format(lambda: a**b)
    if op == "neg":
        return safe_format(lambda: -a)
    if op == "exp":
        return safe_format(a.exp)
    if op == "ln":
        if a == 0:
            # Python's decimal returns untrapped -Infinity for ln(0); we
            # deliberately raise InvalidOperation (see decimal.rs's `ln`
            # docs: "Err(InvalidOperation) for self <= 0").
            return INVALID_OP
        return safe_format(a.ln)
    if op == "fmt":
        r = format_number(a)
        # decimal.rs deliberately has no signed zero (every Decimal is kept
        # canonical: "zero is always +0" -- see the module docs), so
        # formatting a raw, never-rounded, wildly out-of-range literal that
        # only underflows to zero *during formatting itself* (e.g.
        # "-1757E-1000032") intentionally diverges from Python's "-0" here.
        # This can't happen for any *arithmetic result* (those always
        # collapse to canonical +0 the moment they round to zero).
        return "0" if r == "-0" else r
    if op == "cmp":
        if a < b:
            return "-1"
        if a > b:
            return "1"
        return "0"
    if op == "f64":
        f = float(a)
        if f in (float("inf"), float("-inf")):
            return INVALID_OP
        return format_number(D(repr(f)))
    if op == "int":
        is_int = a == a.to_integral_value()
        iv = int(a)
        if -(2**63) <= iv <= 2**63 - 1:
            return f"{str(is_int).lower()} {iv}"
        return f"{str(is_int).lower()} -"
    raise ValueError(f"unknown op {op!r}")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def build_oracle(verbose: bool) -> None:
    if verbose:
        print("Building dec_oracle (cargo build --release --bin dec_oracle)...")
    subprocess.run(
        ["cargo", "build", "--release", "--bin", "dec_oracle"],
        cwd=RUNTIME_DIR,
        check=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1234567)
    ap.add_argument(
        "--cases",
        type=int,
        default=1800,
        help="base count per category (there are ~13 categories, so the "
        "total is roughly 13x this plus a smaller boundary pool)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()

    if not args.skip_build:
        build_oracle(args.verbose)
    if not os.path.exists(ORACLE_BIN):
        print(f"error: oracle binary not found at {ORACLE_BIN}", file=sys.stderr)
        return 2

    rng = __import__("random").Random(args.seed)
    n_each = {
        "add": args.cases,
        "sub": args.cases,
        "mul": args.cases,
        "div": args.cases,
        "idiv": max(args.cases // 2, 200),
        "rem": max(args.cases // 2, 200),
        "neg": max(args.cases // 2, 200),
        "pow": args.cases * 2,
        "exp": args.cases,
        "ln": args.cases,
        "fmt": max(args.cases // 2, 200),
        "cmp": max(args.cases // 2, 200),
        "f64": max(args.cases // 2, 200),
        "int": max(args.cases // 2, 200),
        "boundary": 200,
    }

    t0 = time.time()
    cases = gen_cases(rng, n_each)
    if args.verbose:
        print(f"Generated {len(cases)} cases in {time.time() - t0:.2f}s")

    t1 = time.time()
    expected = [expected_line(op, a, b) for (op, a, b) in cases]
    if args.verbose:
        print(f"Computed expected results in {time.time() - t1:.2f}s")

    input_lines = []
    for op, a, b in cases:
        if b is None:
            input_lines.append(f"{op} {a}")
        else:
            input_lines.append(f"{op} {a} {b}")
    input_data = ("\n".join(input_lines) + "\n").encode("utf-8")

    t2 = time.time()
    proc = subprocess.run(
        [ORACLE_BIN],
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
    )
    if args.verbose:
        print(f"Ran oracle in {time.time() - t2:.2f}s")
    if proc.returncode != 0:
        print("dec_oracle exited nonzero:", proc.returncode, file=sys.stderr)
        print(proc.stderr.decode("utf-8", "replace")[:4000], file=sys.stderr)
        return 2

    actual = proc.stdout.decode("utf-8").split("\n")
    if actual and actual[-1] == "":
        actual.pop()

    if len(actual) != len(expected):
        print(
            f"error: got {len(actual)} output lines, expected {len(expected)} "
            "(oracle crashed or desynced?)",
            file=sys.stderr,
        )
        print(proc.stderr.decode("utf-8", "replace")[:4000], file=sys.stderr)
        return 2

    mismatches = []
    for i, (case, exp, act) in enumerate(zip(cases, expected, actual)):
        if exp != act:
            mismatches.append((i, case, exp, act))

    total = len(cases)
    print(f"Total cases: {total}")
    print(f"Mismatches: {len(mismatches)}")
    print(f"Wall time: {time.time() - t0:.2f}s")

    if mismatches:
        print("\nFirst 30 mismatches:")

        def trunc(s: str, n: int = 200) -> str:
            return s if len(s) <= n else s[:n] + f"...<{len(s)} chars>"

        for idx, case, exp, act in mismatches[:30]:
            op, a, b = case
            line = f"{op} {a}" + (f" {b}" if b is not None else "")
            print(f"  #{idx}: {trunc(line)}")
            print(f"      expected: {trunc(exp)}")
            print(f"      actual:   {trunc(act)}")
        return 1

    print("OK: no mismatches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
