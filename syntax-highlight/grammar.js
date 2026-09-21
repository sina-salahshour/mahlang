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
  OR_AND: 1,
  COMPARE: 2,
  ADDITIVE: 3,
  MULTIPLICATIVE: 4,
  UNARY: 5,
  POW: 6,
  POSTFIX: 7,
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
        $.return_stmt,
        $.break_stmt,
        $.continue_stmt,
        $.while_stmt,
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

    return_stmt: ($) => prec.right(seq("return", optional($.expr))),

    break_stmt: ($) => "break",

    continue_stmt: ($) => "continue",

    while_stmt: ($) =>
      seq(
        "while",
        field("condition", $.expr),
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

    _params: ($) => seq($.identifier, repeat(seq(",", $.identifier))),

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
      seq(field("pattern", $._pattern), "=>", field("body", $.block)),

    // -- patterns -----------------------------------------------------
    //
    // A completely separate grammar from expressions, exactly like
    // compiler/parser.py's dedicated `_parse_pattern` family -- `ID {`
    // inside a pattern is never ambiguous with anything else, unlike the
    // scrutinee expression, so no precedence gymnastics are needed here.

    _pattern: ($) =>
      choice(
        $.number,
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
        $.fn_expr,
        $.block,
        $.binary_expr,
        $.unary_expr,
        $.field_access,
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
          field("target", choice($.identifier, $.field_access)),
          "=",
          field("value", $.expr),
        ),
      ),

    paren_expr: ($) => seq("(", $.expr, ")"),

    unary_expr: ($) => prec(PREC.UNARY, seq("-", $.expr)),

    binary_expr: ($) =>
      choice(
        prec.left(PREC.OR_AND, seq($.expr, choice("&", "|"), $.expr)),
        prec.left(
          PREC.COMPARE,
          seq($.expr, choice("==", "!=", "<", ">"), $.expr),
        ),
        prec.left(PREC.ADDITIVE, seq($.expr, choice("+", "-", "%"), $.expr)),
        prec.left(
          PREC.MULTIPLICATIVE,
          seq($.expr, choice("*", "/", "//"), $.expr),
        ),
        prec.right(PREC.POW, seq($.expr, "**", $.expr)),
      ),

    field_access: ($) =>
      prec(PREC.POSTFIX, seq($.expr, ".", field("field", $.identifier))),

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

    _args: ($) => seq($.expr, repeat(seq(",", $.expr))),

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

    // M10 (async): `detach <call>` normally wraps a call expression (see
    // docs/V2_DESIGN.md's M10 milestone -- compiler/parser.py parses this
    // by directly consuming `ID ( args )`, never the general expression
    // grammar, so `.await` binds to the Promise `detach` produces rather
    // than to the inner call's own result). One exception: `detach
    // sleep_async(ms)` -- `sleep_async` isn't a real Closure call at all
    // (a dedicated builtin, see `sleep_async_call` below), and detaching
    // it is purely a codegen-time choice (skip the auto-await a bare
    // `sleep_async(ms)` otherwise gets), not a real Task -- but it's still
    // valid, real syntax the parser accepts, so it needs to parse here
    // too. `.await` itself needs no dedicated rule at all -- it's an
    // ordinary `field_access` with field name "await", already covered
    // generically above -- but only once `detach_expr` itself has already
    // reduced: right after `detach call_expr`, a `.` is ambiguous between
    // extending the inner call into its OWN `field_access` (wrong --
    // would mean `detach (foo().field)`) and finishing `detach_expr` first
    // so the `.` applies to the whole `detach_expr` instead (right --
    // `(detach foo()).field`, matching the real parser's
    // `_parse_postfix_from(DetachExpr(...))`). Giving `detach_expr` a
    // higher precedence than `PREC.POSTFIX` resolves this in favor of
    // reducing `detach_expr` immediately.
    detach_expr: ($) =>
      prec(
        PREC.POSTFIX + 1,
        seq("detach", field("call", choice($.call_expr, $.sleep_async_call))),
      ),

    sleep_async_call: ($) => seq("sleep_async", "(", $.expr, ")"),

    true: ($) => "true",

    false: ($) => "false",

    identifier: ($) => /[a-zA-Z_$]\w*/,

    number: ($) => /\d+(\.\d+)?/,

    string: ($) => /"([^"\\]|\\.)*"/,
  },
});
