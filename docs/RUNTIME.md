# IR and runtime

Companion to [ARCHITECTURE.md](ARCHITECTURE.md). This is the instruction
format `actions.py` emits and `code_interpreter.py` executes, and the memory
model behind it — the part of the system a v2 redesign (heap, closures,
recursion, structs/enums) most directly replaces.

## Instruction format

Every instruction is a plain 4-tuple `(op, arg1, arg2, dest)`, stored by
position in a flat Python list (`ir.sstack`, capped at 400 live slots — see
`IRGenerator.write_code`, which raises `RuntimeError("CodeBlock is full")`
past that). `arg1`/`arg2`/`dest` are either literal values (numbers written
by `ld`) or **addresses into the data array** described below; `None` fills
unused tuple positions. Both `code_interpreter.run_code`'s `match operation`
and `mah.py build`'s pretty-printer just render/interpret this tuple shape
directly — there's no separate instruction encoding.

| op | args | meaning |
|---|---|---|
| `ld` | `(ld, value, None, dest)` | load a literal (number/string) into `dest` |
| `=` | `(=, src, None, dest)` | copy `stack[src]` into `stack[dest]` |
| `+ - * / // % **` | `(op, lhs_addr, rhs_addr, dest)` | binary arithmetic; note `lhs`/`rhs` are read as `b = stack[lhs]; a = stack[rhs]` (swapped) because operands are popped off `ir.stack` in reverse push order |
| `eq neq lt gt and or` | same shape | comparison/logical, result is `int` 0/1 |
| `neg` | `(neg, src, None, dest)` | unary minus |
| `sin` `cos` | `(op, src, None, dest)` | builtin math |
| `print` | `(print, src, None, None)` | one instruction per argument |
| `input` | `(input, None, None, dest)` | blocks reading digit characters from stdin until a non-digit |
| `jmp` | `(jmp, None, None, target_pc)` | unconditional jump |
| `jmpf` / `jmpt` | `(op, cond_addr, None, target_pc)` | conditional jump on `stack[cond_addr]` falsy/truthy |
| `call` | `(call, None, None, target_pc)` | push `pc` onto the tiny return stack, jump |
| `ret` | `(ret, None, None, None)` | pop the return stack, jump back |
| `(None, None, None, None)` | — | halt (also emitted as a `nop` placeholder before backpatching) |
| `"while"` marker | `(while, None, None, None)` | never executed — a sentinel `write_code` leaves at the loop's condition-check slot so `break`/`continue` can find it later by scanning `ir.sstack` (see below) |

## Memory: one flat array, addresses assigned once, forever

`code_interpreter.run_code` allocates a single Python list, `stack`, of
**1500** slots and never grows or frees it. Three disjoint address ranges,
by convention only (nothing enforces the boundaries at runtime):

- **`0..~400`** — code (a *separate* array, `ir.sstack`, not the same
  object as the interpreter's `stack`; the interpreter is handed
  `ir.sstack[:400]` as its instruction list).
- **`400..600`** (`ir.variable_pointer`, starts at 400) — every `let` and
  every function parameter gets the next free slot here, **once, at compile
  time, for the entire program**. `IRGenerator.declare_variable` just bumps
  a counter; nothing ever reclaims a slot.
- **`600..1000`** (`ir.tmp_pointer`, starts at 600) — every intermediate
  expression result gets a fresh slot the same way (`get_temp_address`).
  Also never reclaimed — a long/looping program can in principle exhaust
  this before really doing much, since even a `while` loop body's temporaries
  keep consuming *new* addresses each time it's compiled, not each time it
  *runs* (the loop body is compiled once, so in practice this is bounded by
  program *text* size, not iteration count — but a deeply nested expression
  or many local variables can still hit the 1000-address ceiling with no
  error message beyond an out-of-range write).
- **`1000+`** (`sp` in `code_interpreter.run_code`) — the *only* piece of
  call-stack-like state: a single integer that increments on `call` and
  decrements on `ret`, storing just the caller's `pc`. No frame pointer, no
  saved locals, no argument-passing convention beyond "copy each argument
  into the callee's fixed parameter addresses right before jumping."

### Why this breaks recursion and closures

Because parameter/local addresses are assigned **once per declaration site**
(not once per call), a recursive call and its caller share the exact same
memory for their "different" `n`. The `call`/`ret` pair correctly saves and
restores *which instruction to resume at*, but nothing saves and restores
*variable values* — so by the time the inner call returns, the outer call's
locals have been overwritten. There is no way to give a closure something
to capture either: there is no per-call activation record, and no runtime
representation of "a function value" at all (functions are resolved to a
fixed code address + a fixed arg-address list at compile time, stored
directly in the IR-generator's scope dict — never as a value on `ir.stack`
you could assign to a variable).

A v2 runtime needs real **activation records** (one per call, allocated on
a real stack or heap, addressed relative to a frame/base pointer that
changes per call) before recursion, closures, or first-class functions are
possible — see the design notes for how `scp`/`dcp` (static/dynamic chain
pointers) are meant to solve this.

## Control flow: backpatching

`if`/`while` compile a placeholder instruction *before* the jump target is
known, remember its address on `ir.stack`, then overwrite it in place once
the target is known:

- `@save` writes a blank `(None, None, None, None)` and pushes its address.
- After the guarded block compiles (so `ir.code_pointer` now points at "the
  instruction after"), `@jmpfalse`/`@jmptrue`/`@jmp` pop that address and
  `write_code(code, address=that_addr)` to fill it in with a real jump.
- `while` needs two placeholders (`@savewhile` writes a `("while", ...)`
  sentinel *twice* — once for "jump to condition re-check", once as the
  scannable marker `break`/`continue` look for) plus the condition-check
  address, all threaded through `ir.stack` and stitched together by
  `@jmpwhile` once the loop body's end address is known.
- `break`/`continue` don't know their loop's address structurally (there's
  no AST to walk up); they **scan `ir.stack` for the most recent address
  whose `ir.sstack[addr]` is a `("while", ...)` tuple** (see `actions.py`'s
  `break`/`continue` actions) — `break` targets the first one found,
  `continue` the second (the pair `@savewhile @savewhile` pushes two, one
  behind the other, for exactly this purpose). This works but is a clear
  sign of the missing AST: nesting depth is tracked by *counting sentinel
  values on a stack*, not by any structural loop reference.

## Interpreter loop

`code_interpreter.run_code` is a flat `while True: match operation` fetch
loop over `code_block[pc]`, `pc` incrementing by 1 each step unless a jump
op sets it directly. It halts on the `(None, None, None, None)` sentinel.
`_to_str` centralizes "print a Decimal without a trailing `.0` for whole
numbers." There's no error handling here beyond Python's own exceptions
propagating up to `mah.py`'s `except Exception` in `main()`, which uses the
position embedded in error messages (`"at position N"`) plus the
preprocessor's source map to report a real file:line:column.
