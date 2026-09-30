---
title: Modules & imports
order: 11
section: Language
---

```mah
# mathlib.mh
export fn square(n) { return n ** 2 }
export let answer = 42

fn helper() { return 1 }    # private: not visible to importers
export helper                # ...unless explicitly exported
```

```mah
import math from "mathlib"   # namespaced -- math.square(4), math.answer
# or
import "mathlib"             # flat -- square(4) directly in scope
```

- Only `export`ed names are visible to importers.
- Paths are relative to the importing file; the `.mh` extension is
  optional in the import path.
- Each file is inlined **at most once**, so diamond imports and import
  cycles are safe.
- Errors inside an imported file are reported with their real
  `file:line:column`, not the importing file's position.

## Types are module-scoped too

`struct`, `enum` and `trait` names behave like `fn` and `let` names: private
to their module unless exported. Two modules (or a module and your program)
can each declare their own `Request` without clashing.

```mah
# geometry.mh
export struct Point { x: Number, y: Number }
export enum Shape { Circle { r: Number }, Empty }
export trait Named { fn name(self) -> String }
```

```mah
# main.mh
import geo from "geometry"
struct Mine { a: Number }                         # your own names never clash
impl geo.Named for Mine { fn name(self) -> String { "mine" } }
let p: geo.Point = geo.Point { x: 1, y: 2 }       # annotation and literal
let s = geo.Shape.Circle { r: 2 }
match s {
    geo.Shape.Circle { r } => { print(r) }        # pattern
    _ => { }
}
print(geo.Point, p)                               # Point Point { x: 1, y: 2 }
```

With a flat import the bare name is in scope (`Point`). A type prints, and
shows in error messages, under the name it was declared with. The built-in
types and the prelude's (`Range`, `Iterator`, `Printable`, `Error`, ...) stay
global. A type a module doesn't export can't be named from outside: it gives
the same "not exported" error as a private function.

## How it works

`import`/`export` are handled by a **preprocessor** that inlines
imported files into one combined source text before the lexer or parser
ever run, tracking original file positions for error messages. That's
also why `mah format` treats `import`/`export` lines specially (see
[Formatter](/docs/formatter)) — they're not part of the grammar the
parser sees.

```mah
# main.mh
import "mathlib.mh"                  # bring exported names in directly
import m from "mathlib"              # or namespaced (the .mh is optional)
print(square(3), m.answer)
```

## The standard library

`import "std:math"` / `import math from "std:math"` import a module of the
standard library the same way. See [Standard library](/docs/standard-library).
