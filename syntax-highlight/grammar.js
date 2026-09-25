/**
 * @file A beautiful language
 * @author Sina Salahshour <sina.salahshour.32@gmail.com>
 * @license MIT
 */

/// <reference types="tree-sitter-cli/dsl" />
// @ts-check

// Precedence levels for `expr`'s binary/unary/postfix forms, loosest to
// tightest binding -- mirrors compiler/parser.py's hand-written recursive
// descent chain (_parse_or_and -> _parse_compare -> _parse_additive ->
// _parse_multiplicative -> _parse_unary -> _parse_pow -> _parse_primary),
// re-expressed the idiomatic tree-sitter way (one left-recursive
// `binary_expr`/`unary_expr` rule per level) instead of mirroring the
// parser's nested nonterminal layers. Field access is a postfix applied to
// any primary and binds tighter than everything, including `**` and unary
// `-` (`-p.x` is `-(p.x)`, `p.x ** 2` is `(p.x) ** 2`).
const PREC = {
  ASSIGN: 0,
  // Range expressions (`5..10`, `5..=10`, `1..`, `..10`) bind the loosest
  // of any binary-shaped operator -- looser even than `&`/`|` (previously
  // the loosest). Not the same slot as ASSIGN: assignment is a completely
  // separate production (`place "=" expr`, restricted to an
  // identifier/field_access target) that never shares a parse state with
  // the range/binary operator chain, so the two don't need to be
  // distinguished from each other numerically, only RANGE needs to sit
  // below OR_AND.
  RANGE: 1,
  OR_AND: 2,
  COMPARE: 3,
  ADDITIVE: 4,
  MULTIPLICATIVE: 5,
  UNARY: 6,
  POW: 7,
  POSTFIX: 8,
};

module.exports = grammar({
  name: "mah",

  word: ($) => $.identifier,

  extras: ($) => [/\s/, $.comment, ";"],

  conflicts: ($) => [
    // A bare identifier immediately followed by `{` is genuinely ambiguous
    // to a context-free grammar: it could be the start of a struct literal
    // (`Point { x: 1 }`) OR just a plain identifier value immediately
    // followed by an unrelated `{ ... }` block belonging to an enclosing
    // construct (most commonly `if`/`while`/`match`'s condition/subject,
    // e.g. `while flag { ... }`, where `flag` must NOT swallow the loop
    // body as if it were struct fields). compiler/parser.py resolves this
    // with a stateful flag (`_struct_literal_allowed`, disabled specifically
    // in condition/subject position -- see docs/V2_DESIGN.md's M2
    // milestone) that a context-free GLR grammar can't cheaply replicate
    // (see this milestone's scope note). Declaring the conflict here
    // instead lets GLR fork both interpretations at parse time and keep
    // whichever one is actually well-formed -- in practice the struct
    // literal branch dies immediately whenever the `{ ... }` isn't
    // field-init-shaped (the overwhelmingly common case: any real loop/if
    // body), and the plain-identifier branch dies whenever it IS
    // field-init-shaped but a bare identifier was expected to end there.
    // The one truly-ambiguous case (both branches parse successfully, e.g.
    // the deliberately out-of-scope `if Point { x: 1 } { ... }`) is broken
    // by `struct_literal`'s default (tied, first-listed-wins) precedence,
    // which is an accepted, documented cosmetic imprecision here.
    [$.expr, $.struct_literal],
    // The same shape of ambiguity, one token earlier: `identifier '.'
    // identifier` is the shared prefix of both `field_access` (built by
    // reducing the first identifier to a complete `expr`, then extending it
    // with `.field`) and `enum_literal` (a flat, un-reduced
    // `type '.' variant '{' ... '}'` sequence). Without this being a tied,
    // declared conflict, the parser statically commits to the enum_literal
    // reading the moment it sees the `.` (since that shift's implied
    // precedence otherwise beats reducing the bare identifier), which then
    // hard-fails on completely ordinary code like `a.x + b.x` the instant
    // no `{` turns up after the second identifier. Declaring it lets GLR
    // keep the `field_access` branch alive as a fallback.
    [$.expr, $.enum_literal],
    // `range_expr`'s "both bounds" (`5..10`) and "open end" (`5..`)
    // alternatives share the prefix `$.expr ".."` -- right after `5..`, a
    // following `10` is ambiguous between "this is the range's right
    // bound" (extend "both bounds") and "the range already ended at
    // `5..`; `10` starts an unrelated new top-level statement"
    // (`source_file` is `repeat($._stmt)`, and a bare number is a valid
    // `expr_stmt` on its own). At equal precedence this resolves
    // deterministically but WRONG (confirmed with
    // `tree-sitter parse --debug=normal` on a minimal reproducer: `5..10`
    // parsed as two separate statements, `5..` then a stray `10`) --
    // same-precedence shift/reduce ties resolve the way left-associativity
    // wants them resolved for a genuinely repeated operator, which doesn't
    // fit here (there's no operator to the right of `10`, just the choice
    // of whether to consume it at all). Declaring the conflict forces GLR
    // to fork both readings; `range_expr`'s `prec.dynamic(1, ...)` on the
    // "both bounds" alternative (see its comment below) then picks that
    // reading whenever both forks produce a complete, error-free parse --
    // which also leaves the *other* ambiguity (an open-ended range's
    // missing right bound swallowing a following `{ ... }` that's really
    // an enclosing `if`/`while`/`match`'s block, e.g.
    // `if x == 1.. { print(1) }`) to resolve itself correctly: that block
    // is only ever reachable as a `range_expr` bound in the fork where
    // doing so leaves the enclosing `if`/`while` with no `then`/`body`
    // block of its own, so GLR discards that fork as unparseable.
    [$.range_expr],
  ],

  rules: {
    source_file: ($) => repeat($._stmt),

    comment: ($) => /#.*/,

    // -- statements -------------------------------------------------------

    _stmt: ($) =>
      choice(
        $.import_stmt,
        $.export_stmt,
        $.let_stmt,
        $.struct_decl,
        $.enum_decl,
        $.trait_decl,
        $.impl_decl,
        $.return_stmt,
        $.break_stmt,
        $.continue_stmt,
        $.print_stmt,
        $.defer_stmt,
        $.expr_stmt,
      ),

    // Sugar handled entirely by preprocessor.py (never reaches the real
    // lexer/parser) but real syntax that appears in on-disk .mh files
    // (see examples/import_demo.mh, examples/mathlib.mh) -- included here
    // purely so those files highlight correctly, not because
    // compiler/lexer.py or compiler/parser.py know about it.
    import_stmt: ($) =>
      seq(
        "import",
        optional(seq(field("alias", $.identifier), "from")),
        field("path", $.string),
      ),

    export_stmt: ($) =>
      seq("export", choice($.let_stmt, $.fn_stmt, $.identifier)),

    let_stmt: ($) =>
      seq("let", field("name", $.identifier), "=", field("value", $.expr)),

    struct_decl: ($) =>
      seq(
        "struct",
        field("name", $.identifier),
        "{",
        optional($._name_list),
        "}",
      ),

    _name_list: ($) => seq($.identifier, repeat(seq(",", $.identifier))),

    enum_decl: ($) =>
      seq(
        "enum",
        field("name", $.identifier),
        "{",
        optional($._enum_variants),
        "}",
      ),

    _enum_variants: ($) => seq($.enum_variant, repeat(seq(",", $.enum_variant))),

    enum_variant: ($) =>
      seq(
        field("name", $.identifier),
        optional(seq("{", optional($._name_list), "}")),
      ),

    // Trait declaration (top-level item). Items are `fn NAME(params)`
    // optionally followed by a block -- unlike `impl_item` below, the body
    // is optional here: a trait item with no block is a *required* method
    // (signature only, implementors must supply a body); one with a block
    // is a *default* method (the trait itself supplies a body, which
    // implementors may override). A `self`-less item (`fn unit()`) is a
    // static/associated function, same shape either way -- `self` is an
    // ordinary parameter name syntactically, not distinguished in the
    // grammar (see the highlight queries for how it's special-cased).
    // Optional `;` between items falls out of `;` already being declared
    // in `extras` (see top of file), same as everywhere else in Mah.
    trait_decl: ($) =>
      seq(
        "trait",
        field("name", $.identifier),
        "{",
        repeat($.trait_item),
        "}",
      ),

    trait_item: ($) =>
      seq(
        "fn",
        field("name", $.identifier),
        "(",
        optional($._params),
        ")",
        optional(field("body", $.block)),
      ),

    // Impl block (top-level item), two forms:
    //   `impl Rect { ... }`         -- inherent impl; `name` is the target type.
    //   `impl Shape for Rect { ... }` -- trait impl; `name` is the trait,
    //                                    `target` is the implementing type.
    // Every item requires a body (unlike `trait_item`) -- an impl always
    // provides concrete method implementations.
    impl_decl: ($) =>
      seq(
        "impl",
        field("name", $.identifier),
        optional(seq("for", field("target", $.identifier))),
        "{",
        repeat($.impl_item),
        "}",
      ),

    impl_item: ($) =>
      seq(
        "fn",
        field("name", $.identifier),
        "(",
        optional($._params),
        ")",
        field("body", $.block),
      ),

    return_stmt: ($) => prec.right(seq("return", optional($.expr))),

    // `break value` -- the enclosing loop's value. The real parser only
    // takes a value that starts on the `break`'s own line; a highlighter
    // doesn't need that distinction.
    break_stmt: ($) => prec.right(seq("break", optional($.expr))),

    continue_stmt: ($) => "continue",

    // Loops are expressions (`let x = while true { break 10 }`), like
    // `if`/`match`.
    while_expr: ($) =>
      seq(
        "while",
        field("condition", $.expr),
        field("body", $.block),
      ),

    // `for let value[, let index] in iterable { ... }`
    for_expr: ($) =>
      seq(
        "for",
        "let",
        field("value", $.identifier),
        optional(seq(",", "let", field("index", $.identifier))),
        "in",
        field("iterable", $.expr),
        field("body", $.block),
      ),

    print_stmt: ($) => seq("print", "(", optional($._args), ")"),

    // M10 (async): `defer <stmt>` desugars (in the real hand-written
    // parser -- compiler/parser.py's `_parse_defer_stmt`) into a
    // synthesized zero-arg closure wrapping any `_STATEMENT_LEADING`
    // statement, a single expression-statement, a single assignment, or a
    // full `{ ... }` block -- not byte-for-byte as permissive here (a
    // highlighter doesn't need it), just structurally reasonable for the
    // common forms (`defer print(...)`, `defer foo()`, `defer x = 1`,
    // `defer { ... }`). Was landed in M9 but never added to this grammar
    // at all -- a real pre-existing gap this milestone also fixes.
    //
    // `$._stmt` alone (no separate `$.block` alternative) covers the
    // `{ ... }` block form too, via `_stmt`'s own `expr_stmt -> expr ->
    // block` path -- adding `$.block` directly here made the block form
    // reachable two ways and was an unresolvable grammar conflict.
    defer_stmt: ($) => seq("defer", $._stmt),

    expr_stmt: ($) => $.expr,

    block: ($) => seq("{", repeat($._stmt), "}"),

    // A bare, named `fn` declaration used as a statement (sugar for
    // `let NAME = fn NAME(...) { ... }` -- see docs/V2_DESIGN.md's M1
    // milestone). Also the form `export fn ...` wraps.
    fn_stmt: ($) =>
      seq(
        "fn",
        field("name", $.identifier),
        "(",
        optional($._params),
        ")",
        field("body", $.block),
      ),

    fn_expr: ($) =>
      seq(
        "fn",
        optional(field("name", $.identifier)),
        "(",
        optional($._params),
        ")",
        field("body", $.block),
      ),

    _params: ($) => seq($.param, repeat(seq(",", $.param))),

    // A single parameter, with an optional default value (M11: default
    // parameter values, see `fn area(w, h = 1, scale = 1) { ... }`). The
    // default is any expression -- a literal, a struct literal
    // (`b = B { n: 0 }`), an anonymous `fn` expression (`f = fn(v) { v }`),
    // etc. -- so it's just `$.expr`, same as everywhere else a value is
    // expected. `self` is an ordinary parameter name here too, same as
    // before this milestone (see the highlight queries for how it's
    // special-cased); it's simply never given a default in practice.
    param: ($) =>
      seq(
        field("name", $.identifier),
        optional(seq("=", field("default", $.expr))),
      ),

    if_expr: ($) =>
      seq(
        "if",
        field("condition", $.expr),
        field("then", $.block),
        repeat($.elif_clause),
        optional($.else_clause),
      ),

    elif_clause: ($) =>
      seq("elif", field("condition", $.expr), field("body", $.block)),

    else_clause: ($) => seq("else", field("body", $.block)),

    match_expr: ($) =>
      seq("match", field("subject", $.expr), "{", repeat($.match_arm), "}"),

    match_arm: ($) =>
      seq(
        field("pattern", $._pattern),
        optional(seq("if", field("guard", $.expr))),
        "=>",
        field("body", $.block),
      ),

    // -- patterns -----------------------------------------------------
    //
    // A completely separate grammar from expressions, exactly like
    // compiler/parser.py's dedicated `_parse_pattern` family -- `ID {`
    // inside a pattern is never ambiguous with anything else, unlike the
    // scrutinee expression, so no precedence gymnastics are needed here.

    _pattern: ($) =>
      choice(
        $.range_pattern,
        $.number,
        $.negative_number,
        $.string,
        $.true,
        $.false,
        $.wildcard_pattern,
        $.some_pattern,
        $.none_pattern,
        $.struct_pattern,
        $.enum_pattern,
        $.identifier,
      ),

    // A negative number literal pattern (`-5 => { ... }`, and as a range
    // bound: `-10..-5`). Patterns are their own grammar, entirely separate
    // from `$.expr` (see the module comment above `_pattern`), so this
    // can't reuse `unary_expr` -- it's a small dedicated rule instead,
    // exactly like `wildcard_pattern`/`none_pattern` below.
    negative_number: ($) => seq("-", $.number),

    // Range pattern (match-arm only, a different grammar from `range_expr`
    // above): `1..10`, `10..=15` (both bounds), `..1`/`..=1` (open start),
    // `15..` (open end). Bounds are number or string literals only (never
    // a general pattern/expression), and a number bound may be negative
    // (`-10..-5`) -- see `_range_pattern_bound`. Listed first in
    // `_pattern`'s choice above (rather than after, the way `range_expr`
    // is appended after `binary_expr` in `expr`'s list) purely so a bound
    // followed by `..`/`..=` doesn't need a tie-break against the bare
    // `$.number`/`$.negative_number`/`$.string` alternatives: ordinary LR
    // lookahead already resolves "bound then `..`" vs "bound alone" (shift
    // vs reduce) without needing a declared conflict, unlike `range_expr`
    // in the sibling expression grammar (whose ambiguity is with `$.block`,
    // not with another alternative of the same rule).
    range_pattern: ($) =>
      choice(
        seq(
          $._range_pattern_bound,
          choice("..", "..="),
          $._range_pattern_bound,
        ),
        seq($._range_pattern_bound, choice("..", "..=")),
        seq(choice("..", "..="), $._range_pattern_bound),
      ),

    _range_pattern_bound: ($) => choice($.number, $.negative_number, $.string),

    wildcard_pattern: ($) => "_",

    some_pattern: ($) => seq("some", "(", $._pattern, ")"),

    none_pattern: ($) => "none",

    struct_pattern: ($) =>
      seq(
        field("type", $.identifier),
        "{",
        optional($._pattern_fields),
        "}",
      ),

    enum_pattern: ($) =>
      seq(
        field("type", $.identifier),
        ".",
        field("variant", $.identifier),
        optional(seq("{", optional($._pattern_fields), "}")),
      ),

    _pattern_fields: ($) =>
      seq($._pattern_field, repeat(seq(",", $._pattern_field))),

    _pattern_field: ($) =>
      seq(field("name", $.identifier), optional(seq(":", $._pattern))),

    // -- expressions --------------------------------------------------

    expr: ($) =>
      choice(
        $.assignment_expr,
        $.if_expr,
        $.match_expr,
        $.while_expr,
        $.for_expr,
        $.fn_expr,
        $.block,
        $.range_expr,
        $.binary_expr,
        $.unary_expr,
        $.field_access,
        $.index_expr,
        $.vector_literal,
        $.map_literal,
        $.call_expr,
        $.struct_literal,
        $.enum_literal,
        $.some_expr,
        $.none_expr,
        $.sin_call,
        $.cos_call,
        $.input_call,
        $.detach_expr,
        $.sleep_async_call,
        $.paren_expr,
        $.identifier,
        $.number,
        $.string,
        $.true,
        $.false,
      ),

    // Assignment (`place = expr` / `place.field = expr`) is real Mah
    // syntax only at statement level (compiler/parser.py's
    // `_parse_block_items` special-cases `ID`/`FieldAccess` immediately
    // followed by `=`), never as a nested sub-expression value. Folding it
    // into `expr` as its own lowest-precedence alternative -- rather than
    // a separate `_place "=" expr` statement form sharing the
    // `field_access`/`identifier` nodes with `expr` -- sidesteps a classic
    // GLR node-sharing conflict; it is a deliberately more permissive
    // grammar than the real language (accepting assignment nested inside
    // a larger expression), matching this milestone's documented
    // "more permissive is fine" scope allowance.
    assignment_expr: ($) =>
      prec.right(
        PREC.ASSIGN,
        seq(
          field("target", choice($.identifier, $.field_access, $.index_expr)),
          "=",
          field("value", $.expr),
        ),
      ),

    paren_expr: ($) => seq("(", $.expr, ")"),

    // Prefix `!` (logical not) sits at the same precedence level as unary
    // `-`, exactly per this milestone's spec (`!!x`, `!a == b` parses as
    // `(!a) == b` since COMPARE is looser than UNARY, `if !(x < 3) { }`).
    unary_expr: ($) => prec(PREC.UNARY, seq(choice("-", "!"), $.expr)),

    binary_expr: ($) =>
      choice(
        prec.left(PREC.OR_AND, seq($.expr, choice("&", "|"), $.expr)),
        prec.left(
          PREC.COMPARE,
          seq($.expr, choice("==", "!=", "<", ">", "<=", ">="), $.expr),
        ),
        prec.left(PREC.ADDITIVE, seq($.expr, choice("+", "-", "%"), $.expr)),
        prec.left(
          PREC.MULTIPLICATIVE,
          seq($.expr, choice("*", "/", "//"), $.expr),
        ),
        prec.right(PREC.POW, seq($.expr, "**", $.expr)),
      ),

    // Range expressions: `5..10` / `5..=10` (both bounds), `1..` (open
    // end), `..10` / `..=10` (open start). The loosest-binding operator in
    // the language (see PREC.RANGE) -- `1..n + 1` is `1..(n + 1)` since
    // `+` (ADDITIVE) reduces the right bound before `..` ever applies.
    //
    // The "both bounds" alternative is deliberately NOT wrapped in
    // `prec.left`/associativity -- it's left as a plain `prec()` (non-
    // associative) so that its shift/reduce tie against "open end" (they
    // share the prefix `$.expr ".."`) is a genuine, undecided conflict
    // rather than one tree-sitter would otherwise resolve statically and
    // silently (see the `[$.range_expr]` conflict declared above for why
    // that matters and what was actually observed when this was first
    // tried with `prec.left` at equal precedence). `prec.dynamic(1, ...)`
    // then breaks the tie in GLR's favor once both forks a genuine
    // conflict produces are complete, well-formed parses -- e.g. for
    // `5..10`, preferring the "both bounds" reading over "`5..`, then a
    // separate `10` statement". It has no effect on forks where only ONE
    // side is well-formed (e.g. `if x == 1.. { print(1) }`, where
    // extending into the block leaves the enclosing `if` without a `then`
    // block, so that fork simply fails to parse and dynamic precedence
    // never enters into it).
    range_expr: ($) =>
      choice(
        prec.dynamic(
          1,
          prec(PREC.RANGE, seq($.expr, choice("..", "..="), $.expr)),
        ),
        prec(PREC.RANGE, seq($.expr, choice("..", "..="))),
        prec.left(PREC.RANGE, seq(choice("..", "..="), $.expr)),
      ),

    field_access: ($) =>
      prec(PREC.POSTFIX, seq($.expr, ".", field("field", $.identifier))),

    // M19: `x[k]`. The real parser only treats `[` as indexing when it's on
    // the same line as `x` (otherwise it starts a Vector literal on a new
    // statement); a highlighter doesn't need that distinction.
    index_expr: ($) =>
      prec(
        PREC.POSTFIX,
        seq(field("object", $.expr), "[", field("index", $.expr), "]"),
      ),

    // M19: `[1, 2, 3]`, `[]`.
    vector_literal: ($) =>
      seq("[", optional(seq($.expr, repeat(seq(",", $.expr)), optional(","))), "]"),

    // M19: `["a": 1, "b": 2]`, `[:]`.
    map_literal: ($) =>
      seq(
        "[",
        choice(":", seq($.map_pair, repeat(seq(",", $.map_pair)), optional(","))),
        "]",
      ),

    map_pair: ($) => seq(field("key", $.expr), ":", field("value", $.expr)),

    // compiler/parser.py's real `Call.callee` is always a bare identifier
    // -- but `field("function", ...)` also accepts a `field_access` chain
    // here, purely to highlight the namespaced-import call sugar
    // (`math.square(n)`, see preprocessor.py and examples/import_demo.mh)
    // that's rewritten away to a plain identifier call before the real
    // lexer/parser ever sees it. More permissive than the core language,
    // matching this milestone's documented scope allowance.
    call_expr: ($) =>
      prec(
        PREC.POSTFIX,
        seq(
          field("function", choice($.identifier, $.field_access)),
          "(",
          optional($._args),
          ")",
        ),
      ),

    // M11 (keyword arguments): an argument list is a mix of ordinary
    // positional expressions and `name: value` keyword arguments (see
    // `keyword_argument` below) -- real Mah requires the keyword ones to
    // come after all positional ones (`area(2, scale: 3)`, never
    // `area(scale: 3, 2)`), but this grammar doesn't enforce that
    // ordering, matching this file's established, documented convention of
    // being deliberately more permissive than the real hand-written parser
    // where enforcing it would cost real grammar complexity for no
    // highlighting benefit (see e.g. `assignment_expr`, `call_expr`'s
    // `function` field above).
    _args: ($) => seq($._arg, repeat(seq(",", $._arg))),

    _arg: ($) => choice($.expr, $.keyword_argument),

    // `name: value` keyword argument at a call site (`area(w: 2, h: 5)`,
    // `r.scaled(k: 3, add: 1)`, `print("a", sep: ", ")`,
    // `detach slow(ms: 1, v: 7)`). Shares its `name ":" value` shape with
    // `_field_init` (struct-literal field init), but the two never mix --
    // `_field_init` only ever appears inside a `{ ... }` (struct/enum
    // literal), `keyword_argument` only inside a call's `( ... )` -- so a
    // struct literal passed as a plain positional argument
    // (`f(Point { x: 1, y: 2 })`) is unaffected: `Point { x: 1, y: 2 }`
    // parses as an ordinary `$.expr` (a `struct_literal`), matched by the
    // `_arg` alternative, not this one.
    keyword_argument: ($) =>
      seq(field("name", $.identifier), ":", field("value", $.expr)),

    // Deliberately NOT wrapped in `prec(PREC.POSTFIX, ...)` -- see the
    // `conflicts` entry above. Left at the default precedence so it ties
    // with the plain-`$.identifier` alternative in `expr`'s choice list
    // (rather than statically out-ranking it, which would make the parser
    // always commit to "struct literal" and never even attempt the
    // plain-identifier reading, breaking every `while`/`if` whose body
    // isn't field-init-shaped).
    struct_literal: ($) =>
      seq(field("type", $.identifier), "{", optional($._field_inits), "}"),

    _field_inits: ($) => seq($._field_init, repeat(seq(",", $._field_init))),

    _field_init: ($) =>
      seq(field("name", $.identifier), ":", field("value", $.expr)),

    // `TypeName.Variant` (unit, e.g. `Shape.Empty`) is deliberately NOT part
    // of this rule -- it's indistinguishable, without type information, from
    // an ordinary `field_access` chain (`a.b`), and compiler/parser.py
    // itself resolves that same way: `_parse_postfix_from` only builds an
    // `EnumLit`/enum-literal node when a struct-shaped `{ ... }` immediately
    // follows a `.member`; a bare `TypeName.Variant` with nothing after it
    // is parsed as, and IS, a `FieldAccess` node in the real AST too. So
    // `enum_literal` here only ever matches the struct-shaped form (braces
    // mandatory), and a bare unit-variant reference like `Shape.Empty` is
    // covered by `field_access` below, exactly matching real Mah semantics
    // -- not just a convenient tree-sitter simplification.
    // Also deliberately left at default precedence (not wrapped in
    // `prec(PREC.POSTFIX, ...)`) -- see the `conflicts` entry above; it
    // needs to tie with `field_access`'s claim on the shared
    // `identifier '.' identifier` prefix, not out-rank it.
    enum_literal: ($) =>
      seq(
        field("type", $.identifier),
        ".",
        field("variant", $.identifier),
        "{",
        optional($._field_inits),
        "}",
      ),

    some_expr: ($) => seq("some", "(", $.expr, ")"),

    none_expr: ($) => "none",

    sin_call: ($) => seq("sin", "(", $.expr, ")"),

    cos_call: ($) => seq("cos", "(", $.expr, ")"),

    input_call: ($) => seq("input", "(", ")"),

    // `detach <operand>`: any expression can be detached (compiler/
    // parser.py's `_parse_detach` -- a call is detached directly, anything
    // else is wrapped in a closure). `.await` needs no dedicated rule --
    // it's an ordinary `field_access` with field name "await" -- but it
    // must apply to the Promise `detach` produces, not to the operand:
    // right after `detach foo()`, a `.` is ambiguous between extending the
    // operand and finishing `detach_expr` first. Giving `detach_expr` a
    // higher precedence than `PREC.POSTFIX` reduces it immediately, so
    // `detach foo().await` is `(detach foo()).await` (and, likewise,
    // `detach a + b` is `(detach a) + b`, as in the real parser). The real
    // parser detaches a whole non-await postfix chain (`detach s.f` is
    // detach of `s.f`); this grammar stops at the first `.`, which only
    // affects the shape of the highlight tree, not the colors.
    detach_expr: ($) =>
      prec(PREC.POSTFIX + 1, seq("detach", field("operand", $.expr))),

    sleep_async_call: ($) => seq("sleep_async", "(", $.expr, ")"),

    true: ($) => "true",

    false: ($) => "false",

    identifier: ($) => /[a-zA-Z_$]\w*/,

    number: ($) => /\d+(\.\d+)?/,

    string: ($) => /"([^"\\]|\\.)*"/,
  },
});
