---
title: "Why Mah is hand-written, pure Python, and dependency-free"
date: 2026-09-15
description: "A few deliberate constraints run through the whole project: no parser generator, no third-party runtime dependencies, and closures over a real activation-record model."
tags: [design]
---

Mah exists as a from-the-ground-up exploration of how a language and its
tooling actually work, and a few choices run through the whole project on
purpose.

## Pure Python, standard library only

The lexer, parser, resolver, codegen, VM, and the LSP server import
nothing beyond Python's own standard library — no parser-generator
library, no LSP framework, no third-party CLI library (the `mah`
command's `run`/`build`/`format`/`lsp` subcommands are plain `argparse`).
Clone the repo, and everything runs with nothing to `pip install`. The
only place the project reaches for `npm`/Node at all is the optional VS
Code extension client, since that's simply what a VS Code extension is —
the language server it talks to is still pure Python.

## Hand-written, not generated

There's no grammar DSL feeding a parser generator anymore. An earlier
version of the project worked that way (`compiler-generator/`, kept only
as history — see the [prototype changelog](/blog/v0-0-1-the-first-prototype)).
The current compiler is entirely hand-written, which is slower to build
by hand but means every stage — lexing, parsing, resolving, codegen,
lowering to bytecode, the VM — is something you can actually read and
reason about end to end.

## Heap-allocated closures over a native call stack

Every function call gets a heap-allocated `Frame`, linked to its
lexically enclosing frame by a static chain pointer (and, at runtime, a
caller-return chain — the classic SCP/DCP activation-record technique).
That's what lets Mah closures capture outer variables **by reference,
like JavaScript** — not by value/name like Python:

```mah
let counter = fn() {
    let count = 0
    return fn() {
        count = count + 1
        return count
    }
}
let next = counter()
print(next())   # 1
print(next())   # 2
```

A closure that outlives the call that created it still sees later
mutations of its captured variables. There's no garbage collector yet,
but the object model (heap `Frame`s, `Closure`s, `StructInstance`s,
`EnumInstance`s, all with reference semantics) is deliberately shaped so
one can be added later without a redesign.

## A forgiving parser, for tooling's sake

The parser recovers from a syntax error instead of aborting the whole
parse, producing an error node in place of what it couldn't read and
continuing — so the language server can report every mistake in a file
in one pass, not just the first one. (Running a file, as opposed to
editing it, still refuses outright if there's any parse error.)
