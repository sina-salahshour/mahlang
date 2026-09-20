# The `.lang` grammar DSL

> **Historical, describes v1 (pre-M0).** As of `docs/V2_DESIGN.md`'s M0
> milestone, `mah.lang` is no longer used to generate `compiler/` (`make
> lang` is a no-op) -- the lexer and parser are hand-written. This
> document remains an accurate description of the retired DSL and the
> LL(1) constraints it enforced, kept for historical reference and
> because `compiler-generator/` itself is untouched.

`mah.lang` (Mah's own grammar) and `compiler-generator/compiler.lang` (the
grammar *of* this DSL, used to bootstrap the tool that reads `mah.lang`) are
both written in the same small format. This is the file `generate.py` turns
into a lexer + LL(1) parser + IR-generator scaffold. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how it fits into the pipeline.

## File shape

```
<token declarations>
--
<grammar rules>
```

## Token section

```
Let: "let";
String: "\"([^\"\\]|\\.)*\"";
Number: "\d+(?:\.\d+)?";
ID: "[a-zA-Z_$](?:\w|$)*";
!Comment: "#.*";
```

- `Name: "regex";` declares a token. The regex is matched with
  `re.match` (anchored at the current position only, not full-string).
- Declaration **order is the match priority** — the lexer tries every rule
  in order and takes the first that matches at the current position. Put
  keywords (`let`, `if`, ...) *before* `ID`, or `ID`'s pattern swallows them.
- A leading `!` (e.g. `!Comment`) marks the token **ignored**: it's still
  lexed (so it can't appear mid-match of anything else) but filtered out
  before the parser sees it. Use this for comments/whitespace-as-token; plain
  whitespace is already skipped unconditionally by the lexer.
- Every token declared here must be referenced (as `#Name`) somewhere in the
  grammar, and every `#Name` used in the grammar must be declared here —
  `generate.py` errors on undefined tokens and warns on unused ones.

## Grammar section

```
Rule ->
    Alt1Symbol1 Alt1Symbol2 @action1
|   Alt2Symbol1 @action2
|   #e
;
```

- `Name -> ... ;` defines a nonterminal. `|` separates alternative
  productions. The **first** rule in the file is the start symbol.
- A bare identifier (`Stmt`, `Expr`, ...) refers to another nonterminal.
- `#TokenName` matches a terminal (must exist in the token section).
- `#e` is the empty/epsilon alternative — matches nothing.
- `@name` is a **semantic action**: not consumed from input, just a hook
  point. When the parser's stack pops an `@name` symbol it calls whatever
  Python function was registered for `@name` via
  `ir.action("name")` (in `actions.py` for Mah; in `generate.py` itself for
  the meta-grammar), passing the *current lookahead token*. Actions can
  appear anywhere in a production, including between other symbols — this
  is how Mah runs code generation interleaved with parsing instead of after
  building a tree. See [ARCHITECTURE.md](ARCHITECTURE.md)'s IR-generation
  section and `actions.py` for the actual action implementations.

## The LL(1) constraint

`generate.py` builds one parsing-table entry per `(nonterminal, lookahead
token)` pair by unioning the FIRST set of each alternative (and, for an
alternative that can derive empty, the nonterminal's FOLLOW set). **If two
alternatives of the same rule would claim the same lookahead token, grammar
generation fails** with `"Error: the grammar is not LL(1)"`. In practice
this means, when adding syntax:

- **No two alternatives may start with the same token or nonterminal** whose
  FIRST set overlaps. `mah.lang`'s `Factor -> ID MaybeCallWithValue | Sin |
  Cos | Num | Str | Input` works because every alternative starts with a
  distinct token.
- **No left recursion.** `Exp -> Exp #Add Term` is not representable
  directly; the existing grammar always uses the standard
  left-factored-into-right-recursion trick instead
  (`Exp -> Term Exp'`, `Exp' -> #Add Term @add Exp' | #e`), rebuilding
  left-associativity by having the semantic action combine eagerly on each
  step rather than relying on tree shape.
- **At most one alternative may derive empty (`#e`)** per rule, and that
  rule's FOLLOW set must not overlap the FIRST sets of its other
  alternatives (e.g. an "optional else" is fine because `#Else` can't also
  start whatever comes after an `if`).
- Precedence and associativity are entirely encoded by **how many
  nonterminal layers you write** (`Condition > Compare > Exp > Term > Unary
  > Pow > Factor` today, lowest to highest precedence), not by any
  declarative precedence table — adding an operator means picking which
  existing layer it belongs to, or inserting a new layer.

These constraints are the main reason a v2 grammar redesign (pattern
matching, struct/enum literals, blocks-as-expressions) is a real design
problem and not just "add more rules": several natural-looking Rust-like
constructs (e.g. a bare block `{ ... }` usable both as a statement and as an
expression, or `if` as an expression) are not directly LL(1)-expressible
without care, and match arms need lookahead that plain FIRST-set disjointness
may not give you for free (see the design discussion in the v2 planning
notes / PR description once that work starts).

## Regenerating

```sh
make lang
# equivalent to:
python ./compiler-generator/generate.py ./mah.lang
```

Rewrites `compiler/lexer.py`, `compiler/parser.py`, `compiler/ir_generator.py`
in place from the templates in `compiler-generator/templates/`. Nothing
else needs regenerating for a grammar change — `actions.py` is hand-written
and just needs its `@action` set kept in sync with what `mah.lang` uses.
