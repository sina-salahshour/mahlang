---
title: "v0.1.0: runnable bytecode and the Rust VM"
date: 2026-09-26
description: "A second, from-scratch Rust implementation of the .mahc bytecode machine, plus self-contained executables that run without mah or Python installed."
tags: [changelog]
version: "0.1.0"
---

This is the current release. Mah's compiled output stops being
Python-only: `runtime/` is a complete second implementation of the
`.mahc` bytecode machine, written in Rust with **only the standard
library** — no crates, matching the rest of the project's "nothing to
install" philosophy.

## Two VMs, one bytecode format

```sh
make vm                                   # cargo build --release -> mah-vm
mah run --vm rust ./examples/structs.mh   # compile with Python, run on mah-vm
mah runc --vm rust ./examples/structs.mahc
```

The Python VM (`mah/code_interpreter.py`) stays the reference
implementation: the Rust one has to print the same output, fail with the
same messages and exit codes, and reject the same malformed files. That
includes matching Python's `decimal` arithmetic digit-for-digit —
`decimal.rs` implements Mah's `Number` (28 significant digits,
round-half-even) from scratch on its own big integers, including
correctly-rounded `exp`/`ln` for fractional exponents.

## Self-contained executables

```sh
mah build --self-contained ./examples/structs.mh -o structs
./structs        # one file, runs without mah or Python installed
```

A self-contained build is a `/bin/sh` script with the `mah-vm` runtime
and the compiled bytecode appended. On first run it checks the platform,
extracts and caches the runtime under `~/.cache/mah/vm/`, then executes
itself through it — later runs, and other programs built with the same
runtime, reuse the cached copy. `mah dis` prints which runtime a file
carries before disassembling it.

## Runnable `.mahc` files directly

`mah build` output now starts with a `#!/usr/bin/env -S mah runc` line
and is marked executable, so with `mah` on your `PATH` a compiled file
runs on its own: `./structs.mahc`, no `mah runc` needed.

## What's next

The static type checker's first version has since landed on `main`:
inference, generics, `mah check`, and the `[types] check` levels
(`loose`/`strict`/`explicit`). See [Types](/docs/types). Typing method
calls, traits, and the standard library comes next.
