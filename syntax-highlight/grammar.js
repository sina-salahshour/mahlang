/**
 * @file A beautiful language
 * @author Sina Salahshour <sina.salahshour.32@gmail.com>
 * @license MIT
 */

/// <reference types="tree-sitter-cli/dsl" />
// @ts-check

module.exports = grammar({
  name: "mah",

  // Define lexical tokens
  extras: ($) => [/\s/, $.comment],

  conflicts: ($) => [[$.return_stmt]],
  rules: {
    source_file: ($) => repeat($.stmt),

    stmt: ($) =>
      choice(
        $.print_stmt,
        $.declaration,
        $.assignment_or_call,
        $.if_stmt,
        $.while_stmt,
        $.break_stmt,
        $.continue_stmt,
        $.return_stmt,
        $.function_def,
      ),

    print_stmt: ($) => seq("print", "(", optional($.args), ")"),

    declaration: ($) => seq("let", $.identifier, "=", $.expr),

    assignment_or_call: ($) => seq($.identifier, choice($.assignment, $.call)),

    assignment: ($) => seq("=", $.expr),

    call: ($) => seq("(", optional($.args), ")"),

    if_stmt: ($) =>
      seq(
        "if",
        $.expr,
        $.code_block,
        repeat($.elif_block),
        optional($.else_block),
      ),

    elif_block: ($) => seq("elif", $.expr, $.code_block),

    else_block: ($) => seq("else", $.code_block),

    while_stmt: ($) => seq("while", $.expr, $.code_block),

    break_stmt: ($) => "break",

    continue_stmt: ($) => "continue",

    return_stmt: ($) => seq("return", optional($.expr)),

    function_def: ($) => seq("def", $.identifier, "(", ")", $.code_block),

    code_block: ($) => seq("{", repeat($.stmt), "}"),

    args: ($) => seq($.expr, repeat(seq(",", $.expr))),

    expr: ($) => $.condition,

    condition: ($) =>
      seq(
        $.compare,
        repeat(choice(seq("||", $.compare), seq("&&", $.compare))),
      ),

    compare: ($) => seq($.term, optional($.compare_op)),

    compare_op: ($) => choice("==", "!=", "<", ">"),

    term: ($) =>
      seq(
        $.unary,
        repeat(choice(seq("+", $.unary), seq("-", $.unary), seq("%", $.unary))),
      ),

    unary: ($) => choice(seq("-", $.unary), $.power),

    power: ($) => seq($.factor, optional(seq("**", $.power))),

    factor: ($) =>
      choice(
        seq("(", $.expr, ")"),
        $.identifier,
        $.sin_call,
        $.cos_call,
        $.number,
        $.input_call,
      ),

    sin_call: ($) => seq("sin", "(", optional($.args), ")"),

    cos_call: ($) => seq("cos", "(", optional($.args), ")"),

    input_call: ($) => seq("input", "(", ")"),

    identifier: ($) => /[a-zA-Z_]\w*/,

    number: ($) => /\d+(\.\d+)?/,

    comment: ($) => /#.*/,
  },
});
