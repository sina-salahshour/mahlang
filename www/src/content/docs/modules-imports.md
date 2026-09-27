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

- Only `export`ed `fn`/`let` names are visible to importers. `struct`,
  `enum`, and `trait` declarations are global across all imported files
  and need no `export`.
- Paths are relative to the importing file; the `.mh` extension is
  optional in the import path.
- Each file is inlined **at most once**, so diamond imports and import
  cycles are safe.
- Errors inside an imported file are reported with their real
  `file:line:column`, not the importing file's position.

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
