"""Hand-written recursive-descent parser for Mah (replaces the generated
LL(1) table-driven parser -- see docs/V2_DESIGN.md's M0 milestone).

Produces the AST in ast_nodes.py; does not emit any code itself (that's
resolve.py + codegen.py). Precedence chain matches v1's grammar exactly
(mah.lang, now historical): or/and < compare < additive(+,-,%) <
multiplicative(*,/,//) < unary(-) < pow(**) < primary -- including v1's
non-standard choice of grouping `or`/`and` at one shared precedence level
and `%` with `+`/`-`; M0 preserves v1's behavior, quirks included.
"""

from __future__ import annotations

import codecs
import re
from decimal import Decimal
from typing import Optional

from .ast_nodes import (
    TestDecl,
    NativeCall,
    AssignStmt,
    Binary,
    BindPat,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ContinueStmt,
    DeferStmt,
    DetachExpr,
    EnumDecl,
    EnumLit,
    EnumPat,
    ErrorNode,
    ExprStmt,
    ForStmt,
    Index,
    MapLit,
    VectorLit,
    FieldAccess,
    FnExpr,
    Ident,
    IfStmt,
    ImplDecl,
    LetStmt,
    LockAcquire,
    LockExpr,
    LockRelease,
    MatchArm,
    MatchStmt,
    MethodCall,
    MethodDecl,
    NamedType,
    FnType,
    TypeParam,
    NumberLit,
    PrintStmt,
    RangePat,
    ReturnStmt,
    SleepAsyncExpr,
    SpreadArg,
    StringLit,
    StructDecl,
    StructLit,
    StructPat,
    ThrowExpr,
    TraitDecl,
    TryExpr,
    TypePat,
    Unary,
    WhileStmt,
    WildcardPat,
)
from .lexer import Lexer, Token, TokenType

# M41b: the one message for a decorator anywhere it isn't allowed.
DECORATOR_MISPLACED = (
    "decorators are only allowed on top-level functions, structs and enums, "
    "impl methods, their parameters, fields and variants"
)

# A backslash escape: \uXXXX, \UXXXXXXXX, \xXX, a 1-3 digit octal escape,
# or a backslash followed by any single ASCII character. A backslash before
# a non-ASCII character is left as written (like any unknown escape).
_ESCAPE_RE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|[0-7]{1,3}|[\x00-\x7f])")


def decode_string_literal(raw: str) -> str:
    r"""Process the backslash escapes in a string literal's source text
    (quotes already stripped), with Python's `unicode_escape` meanings
    (`\n`, `\t`, `\"`, `\\`, `\u00e9`, `\x41`, ...), while leaving every
    other character exactly as written. M17 fix: the old
    `bytes(raw, "utf-8").decode("unicode_escape")` decoded the whole
    literal's UTF-8 bytes as Latin-1, which split a raw non-ASCII character
    like `é` into two wrong characters (`"héllo".len()` was 6). Decoding
    only the escape sequences, one at a time, keeps raw text intact and
    `\u`/`\x` escapes working."""
    return _ESCAPE_RE.sub(lambda m: codecs.decode(m.group(0), "unicode_escape"), raw)


_COMPARE_OPS = {
    TokenType.EQ: "eq",
    TokenType.NEQ: "neq",
    TokenType.LT: "lt",
    TokenType.GT: "gt",
    TokenType.LE: "le",
    TokenType.GE: "ge",
}
# M17: tokens that can begin an expression (everything `_parse_unary`/
# `_parse_primary` accepts, minus `{`, which `_can_start_range_end`
# special-cases) -- the only tokens that can be a range's end value.
_RANGE_END_STARTERS = {
    TokenType.NUMBER,
    TokenType.BRACKET_OPEN,
    TokenType.STRING,
    TokenType.ID,
    TokenType.PAREN_OPEN,
    TokenType.SUB,
    TokenType.BANG,
    TokenType.TRUE,
    TokenType.FALSE,
    TokenType.NONE,
    TokenType.SOME,
    TokenType.IF,
    TokenType.MATCH,
    TokenType.WHILE,
    TokenType.FOR,
    TokenType.FN,
    TokenType.DETACH,
    TokenType.SLEEP_ASYNC,
    # M25: `try`/`throw` are expressions too, so they're valid range ends
    # exactly like `if`/`match`/`detach` above.
    TokenType.TRY,
    TokenType.THROW,
}
_ADDITIVE_OPS = {
    TokenType.ADD: "+",
    TokenType.SUB: "-",
    TokenType.MOD: "%",
}
_MULTIPLICATIVE_OPS = {
    TokenType.MUL: "*",
    TokenType.DIV: "/",
    TokenType.TRUEDIV: "//",
}
# Tokens that can never start an expression -- used to detect a bare
# `return` with no value. More permissive than v1 (which required a
# literal `;` after a value-less `return`): any of these also works,
# which only *accepts* strictly more valid-v1-equivalent programs.
_EXPR_STOPPERS = {TokenType.SEMICOLON, TokenType.BRACE_CLOSE, TokenType.EOF}

# M44 (docs/contracts/M44_threads.md #3.1): after `detach (x)`, a token of one
# of these types on the same line starts the operand of the thread form
# `detach(t) expr` (BRACE_OPEN only where a struct literal is allowed).
_DETACH_THREAD_OPERAND_START = {
    TokenType.ID,
    TokenType.NUMBER,
    TokenType.STRING,
    TokenType.TRUE,
    TokenType.FALSE,
    TokenType.NONE,
    TokenType.SOME,
    TokenType.FN,
    TokenType.DETACH,
    TokenType.SLEEP_ASYNC,
    TokenType.IF,
    TokenType.MATCH,
    TokenType.FOR,
    TokenType.WHILE,
    TokenType.TRY,
    TokenType.THROW,
    TokenType.BANG,
}
# ...and these, on the same line, are a compile error (a statement can't be
# the operand).
_DETACH_THREAD_STATEMENTS = {
    TokenType.PRINT,
    TokenType.LET,
    TokenType.RETURN,
    TokenType.DEFER,
    TokenType.BREAK,
    TokenType.CONTINUE,
}
_DETACH_NEEDS_EXPR = "detach(t) needs an expression; write 'detach(t) { ... }'"
_DETACH_SAME_LINE = "detach(t) and its operand must be on the same line; write 'detach(t) {' on one line"

# M5: statement-leading tokens still handled by parse_stmt's own dedicated
# logic, unchanged. IF/MATCH are deliberately NOT here any more -- they
# must flow through the general expression path (parse_expr ->
# _parse_primary) so they can be used as expressions/tails, with
# _parse_block_items below handling the "semicolon required unless
# block-shaped or last in the block" disambiguation. WHILE/FOR aren't here
# either: loops are expressions too (their value is the `break` value that
# ended them, `let x = while true { break 10 }`).
_STATEMENT_LEADING = {
    TokenType.LET,
    TokenType.STRUCT,
    TokenType.ENUM,
    TokenType.RETURN,
    TokenType.BREAK,
    TokenType.CONTINUE,
    TokenType.PRINT,
    TokenType.DEFER,
    # M12: `trait`/`impl` decls -- always statement-leading, dispatched by
    # parse_stmt to _parse_trait_decl/_parse_impl_decl. Also becoming sync
    # tokens (via _SYNC_TOKENS below) is exactly what we want: a syntax
    # error before a `trait`/`impl` should resynchronize at its start.
    TokenType.TRAIT,
    TokenType.IMPL,
}

# M6: recovery points `_synchronize` will stop at after a syntax error --
# `_STATEMENT_LEADING` plus FN/IF/MATCH (which also start recognizable
# constructs but are handled through the expression path, not
# `_STATEMENT_LEADING`, since M5). See `_synchronize`'s docstring for the
# termination argument.
_SYNC_TOKENS = _STATEMENT_LEADING | {
    TokenType.FN,
    TokenType.IF,
    TokenType.MATCH,
    TokenType.WHILE,
    TokenType.FOR,
    # M25: `try`/`throw` flow through the expression path like `match`
    # (see `_parse_primary`), so they're recovery points too.
    TokenType.TRY,
    TokenType.THROW,
}


class Parser:
    def __init__(self, lexer: Lexer, allow_tests: bool = False) -> None:
        self.lexer = lexer
        # M28: `test "name" { ... }` blocks are parsed only when compiling a
        # `.test.mh` file (and only at the top level); elsewhere `test` is an
        # ordinary name.
        self.allow_tests = allow_tests
        self.current: Token = lexer.get_next_token()
        # M19: the most recently consumed token -- `_on_same_line` needs its
        # end to tell `x[0]` (indexing) from `x` then a `[...]` literal
        # starting the next statement.
        self._prev: Token | None = None
        # See docs/V2_DESIGN.md's M2 milestone: a bare struct literal is
        # disallowed directly in an `if`/`while` condition position (the
        # same restriction Rust and Go apply), since `if x { ... }` would
        # otherwise be ambiguous between "struct literal on x" and "if-body
        # block". Temporarily set False by `_parse_condition_expr`, and
        # temporarily restored to True inside `(...)`/call-argument
        # positions where the ambiguity can't occur.
        self._struct_literal_allowed = True
        # M6: syntax errors collected during parsing instead of raised --
        # `(message, position)` per error, in the order encountered. Kept
        # separate from `Resolver`'s errors on purpose (resolve stays
        # single-exception, stop-at-first-error -- see docs/V2_DESIGN.md's
        # M6 milestone for why forgiving-ness is parser-only).
        self.errors: list[tuple[str, int]] = []

    def advance(self) -> Token:
        tok = self.current
        self.current = self.lexer.get_next_token()
        self._prev = tok
        return tok

    def _on_same_line(self) -> bool:
        """M19: whether the current token starts on the line the previous
        token ended on."""
        if self._prev is None:
            return True
        start = self._prev.position + len(self._prev.literal)
        return "\n" not in self.lexer.input_str[start : self.current.position]

    def expect(self, token_type: TokenType) -> Token:
        if self.current.type is not token_type:
            raise SyntaxError(
                f"Invalid syntax '{self.current}' at position '{self.current.position}'"
            )
        return self.advance()

    # -- program / blocks -------------------------------------------------

    def parse_program(self) -> list:
        stmts, tail = self._parse_block_items(TokenType.EOF)
        if tail is not None:
            # A top-level program has no caller to hand a "value" to --
            # fold a trailing tail into an ordinary discarded ExprStmt.
            stmts.append(ExprStmt(value=tail, position=tail.position))
        self.expect(TokenType.EOF)
        return stmts

    def parse_block(self) -> Block:
        open_tok = self.expect(TokenType.BRACE_OPEN)
        stmts, tail = self._parse_block_items(TokenType.BRACE_CLOSE)
        self.expect(TokenType.BRACE_CLOSE)
        return Block(stmts=stmts, tail=tail, position=open_tok.position)

    def _parse_block_items(self, end_type: TokenType) -> tuple:
        """Shared item-parsing loop for both a top-level program and a
        `{ }` block -- see docs/V2_DESIGN.md's M5 milestone for the exact
        disambiguation rule this implements (matching Rust's): a
        block-shaped statement (`if`/`match`/bare `{ }`) needs no trailing
        `;` unless it's the last item in its enclosing block, in which case
        presence/absence of `;` decides tail-vs-discarded; any other
        expression still needs an explicit `;` to be "just a statement"
        when it isn't the block's last item. Returns `(stmts, tail)` --
        `tail` is `None` unless the last item was an expression with no
        trailing `;` immediately before `end_type`.

        M6 note: also stops on `EOF` even when `end_type` is `BRACE_CLOSE`
        -- an unclosed block (source ends before its `}` ever appears) must
        not loop forever re-attempting to parse "one more item" out of
        nothing. Breaking here means the caller's own `self.expect(end_type)`
        (in `parse_block`) then fails against `EOF` and raises -- but that
        raise does NOT escape all the way out uncaught the way a first
        glance suggests: it propagates to whichever *enclosing*
        `_parse_block_items` call's `try` is on the stack (the one that was
        parsing the expression/statement that contained this block), which
        catches it exactly like any other item failure, records one more
        `ErrorNode`, and (thanks to this same EOF check) breaks cleanly in
        turn. For N levels of unclosed nesting this cascades N times, each
        level contributing one `ErrorNode` pointing at the same EOF
        position -- verbose (duplicate-looking messages) but never a hang,
        and `parse_program()` itself always returns normally once `EOF` is
        reached, never raises, for this case. Deduplicating same-position
        cascaded messages would be a reasonable future LSP polish item, not
        a correctness requirement."""
        stmts = []
        tail = None
        while True:
            while self.current.type is TokenType.SEMICOLON:
                self.advance()
            if self.current.type is end_type or self.current.type is TokenType.EOF:
                break

            try:
                if self.current.type is TokenType.AT:
                    stmts.append(self._parse_decorated_item(end_type))
                    continue

                if self._at_shared_let():
                    # M44: `shared let NAME = value` (top level only -- the
                    # resolver's E5).
                    shared_tok = self.advance()
                    stmt = self.parse_stmt()
                    stmt.shared = True
                    stmt.position = shared_tok.position
                    stmts.append(stmt)
                    continue

                if self.current.type in _STATEMENT_LEADING:
                    stmts.append(self.parse_stmt())
                    continue

                if end_type is TokenType.EOF and self._at_test_decl():
                    stmts.append(self._parse_test_decl())
                    continue

                if self._at_extern_fn():
                    stmts.append(self._parse_extern_fn(top_level=end_type is TokenType.EOF))
                    continue

                if self.current.type is TokenType.FN:
                    top_level_decl = end_type is TokenType.EOF and self.lexer.peek_token().type is TokenType.ID
                    fn_expr = self._parse_fn_expr(decl=top_level_decl)
                    if fn_expr.name is not None:
                        stmts.append(
                            LetStmt(
                                name=fn_expr.name,
                                value=fn_expr,
                                position=fn_expr.position,
                                name_position=fn_expr.name_position,
                                is_decl=True,
                            )
                        )
                        continue
                    expr = fn_expr  # anonymous fn: falls through to the general handling below
                else:
                    expr = self.parse_expr()  # handles IF, MATCH, bare `{`, ID (ident/call/
                    # field-chain/struct-lit/enum-lit), literals, etc. via
                    # _parse_primary.

                if self.current.type is TokenType.ASSIGN and isinstance(expr, (Ident, FieldAccess, Index)):
                    self.advance()
                    value = self.parse_expr()
                    stmts.append(AssignStmt(target=expr, value=value, position=expr.position))
                    continue
                if self.current.type is TokenType.SEMICOLON:
                    self.advance()
                    stmts.append(ExprStmt(value=expr, position=expr.position))
                    continue
                if self.current.type is end_type:
                    tail = expr
                    break  # nothing may follow a tail -- it must be the last item
                if isinstance(
                    expr,
                    (
                        IfStmt,
                        MatchStmt,
                        WhileStmt,
                        ForStmt,
                        Block,
                        Call,
                        FnExpr,
                        DetachExpr,
                        SleepAsyncExpr,
                        FieldAccess,
                        MethodCall,
                        LockExpr,
                    ),
                ) or (
                    # M25: only the catch form (`try { } catch { }`) is
                    # block-shaped (ends in the catch-arms' own `}`) --
                    # both `else` forms end in an arbitrary fallback
                    # expression, so they need a semicolon like any other
                    # expression statement (`throw x` does too, and needs
                    # no special-casing here: it simply isn't one of the
                    # exempted shapes, so it falls straight through to the
                    # ordinary "needs a semicolon" handling below).
                    isinstance(expr, TryExpr) and expr.fallback is None
                ):
                    # Block-shaped (if/match/bare block): no semicolon required
                    # when not last (Rust's rule). Call/anonymous-FnExpr are
                    # ALSO exempted here for a Mah-specific reason, not Rust's:
                    # pre-M5, `parse_stmt`'s dedicated ID-led-call and FN
                    # branches made a bare call statement (`foo(1)`) and a bare
                    # anonymous-fn statement free-standing, needing no trailing
                    # `;` regardless of what followed (the enclosing loop never
                    # required one between statements) -- see e.g.
                    # examples/match.mh's `describe_number(0)` / `(1)` / `(42)`
                    # sequence, with no semicolons, which must keep working
                    # byte-for-byte. This does not affect Call/FnExpr in *tail*
                    # position (the `end_type` branch above already handles
                    # that before this check ever runs), only "statement,
                    # followed immediately by more code, no semicolon."
                    #
                    # M10: DetachExpr/SleepAsyncExpr are exempted for the exact
                    # same reason as Call (they're call-shaped, keyword-prefixed
                    # forms most naturally written bare -- `detach foo()`, both
                    # of the design doc's own worked examples). FieldAccess is
                    # exempted too, specifically for `.await` used as a bare
                    # statement (`sleep_async(ms).await` / `p.await` followed by
                    # more code, no semicolon) -- see docs/NEXT_PHASES.md's
                    # "Async" section's own worked examples, none of which use
                    # semicolons.
                    #
                    # M12: MethodCall is call-shaped too, same reasoning as
                    # Call above (`p.scale(10)` as a bare statement, followed
                    # by more code, needs no semicolon).
                    stmts.append(ExprStmt(value=expr, position=expr.position))
                    continue
                raise SyntaxError(
                    f"Invalid syntax '{self.current}' at position '{self.current.position}'"
                )
            except SyntaxError as exc:
                # M6: don't let one bad item abort the whole parse -- record
                # the error, substitute an ErrorNode (wrapped in ExprStmt) so
                # every downstream consumer has something structurally valid
                # to walk past, and skip ahead to a safe resumption point.
                # `self.current.position` at the moment of the catch is used
                # rather than re-parsing the message string for an embedded
                # "at position 'N'" -- every raise site in this file leaves
                # `self.current` sitting at (or very near) the offending
                # token, since no dispatch branch advances past a token it's
                # about to reject, so this is precise enough without the
                # string-parsing round-trip. Recovery granularity is this
                # whole item (e.g. one `let`, one `match`), never a
                # sub-expression -- see docs/V2_DESIGN.md's M6 milestone.
                error_position = self.current.position
                self.errors.append((str(exc), error_position))
                stmts.append(
                    ExprStmt(
                        value=ErrorNode(message=str(exc), position=error_position),
                        position=error_position,
                    )
                )
                before = self.current.position
                self._synchronize(end_type)
                if self.current.position == before and self._not_at_sync_point(end_type):
                    # Belt-and-suspenders: the reasoning in _synchronize's
                    # docstring says this can't happen, but a language
                    # server must never hang on a parser bug, so force
                    # progress anyway rather than trust the proof blindly.
                    # Deliberately re-checks the *same* stopping condition
                    # `_synchronize`'s own loop uses (not just "did the
                    # position move") -- a zero-token `_synchronize` call
                    # that started (and ends) already sitting on a sync
                    # token, a `;`, `end_type`, or EOF is not stuck, it's
                    # already correctly resynced (e.g. an expression-needs-
                    # a-semicolon error where the very next token happens
                    # to be `print`/`let`/etc.); forcing an advance there
                    # would wrongly eat that legitimate resumption token.
                    self.advance()
        return stmts, tail

    # -- M44: `shared let` / `lock` (contextual words) ----------------------

    def _next_on_same_line(self, tok: Token, nxt: Token) -> bool:
        start = tok.position + len(tok.literal)
        return "\n" not in self.lexer.input_str[start : nxt.position]

    def _at_shared_let(self) -> bool:
        tok = self.current
        if tok.type is not TokenType.ID or tok.literal != "shared":
            return False
        nxt = self.lexer.peek_token()
        return nxt.type is TokenType.LET and self._next_on_same_line(tok, nxt)

    def _at_lock(self, tok: Token) -> bool:
        """`tok` is the current token: an ID `lock` followed by an ID on the
        same line starts a `lock` expression."""
        if tok.literal != "lock":
            return False
        nxt = self.lexer.peek_token()
        return nxt.type is TokenType.ID and self._next_on_same_line(tok, nxt)

    def _parse_lock(self, lock_tok: Token) -> LockExpr:
        """M44: `lock a, b.c { body }` -- desugared into a block that, per
        target in order, acquires it and defers its release; the user's
        body is the block's tail (docs/contracts/M44_threads.md #8.3)."""
        targets = []
        while True:
            name_tok = self.expect(TokenType.ID)
            target = Ident(name=name_tok.literal, position=name_tok.position)
            text = name_tok.literal
            while self.current.type is TokenType.DOT:
                self.advance()
                field_tok = self.expect(TokenType.ID)
                target = FieldAccess(obj=target, field=field_tok.literal, position=field_tok.position)
                text += "." + field_tok.literal
            targets.append((target, text, name_tok.position))
            if self.current.type is not TokenType.COMMA:
                break
            self.advance()
        body = self.parse_block()
        stmts = []
        for _target, text, position in targets:
            stmts.append(ExprStmt(value=LockAcquire(name=text, position=position), position=position))
            release = FnExpr(
                name=None,
                params=[],
                body=Block(
                    stmts=[ExprStmt(value=LockRelease(name=text, position=position), position=position)],
                    position=position,
                    tail=None,
                ),
                position=lock_tok.position,
                name_position=None,
                param_positions=[],
            )
            stmts.append(DeferStmt(closure_expr=release, position=lock_tok.position))
        block = Block(stmts=stmts, tail=body, position=lock_tok.position)
        return LockExpr(targets=[t for t, _text, _pos in targets], block=block, position=lock_tok.position)

    # -- M41b: decorators --------------------------------------------------

    def _decorator_error(self, position: int) -> SyntaxError:
        return SyntaxError(f"{DECORATOR_MISPLACED} at position '{position}'")

    def _parse_decorators(self) -> list:
        """M41b: `{ "@" NAME { "." NAME } [ "(" call_args ")" ] }` -- the
        decorator expressions, built like the same text in expression
        position: `@a` an Ident, `@a.b` a FieldAccess, `@a(x)` a Call and
        `@a.b(x)` a MethodCall (so the resolver, checker and LSP treat every
        name in it as an ordinary reference)."""
        out = []
        while self.current.type is TokenType.AT:
            self.advance()
            name_tok = self.expect(TokenType.ID)
            base = Ident(name=name_tok.literal, position=name_tok.position)
            called = False
            while self.current.type is TokenType.DOT:
                self.advance()
                field_tok = self.expect(TokenType.ID)
                if self.current.type is TokenType.PAREN_OPEN:
                    args, kwargs = self._parse_paren_args()
                    base = MethodCall(
                        obj=base, method=field_tok.literal, args=args, position=field_tok.position, kwargs=kwargs
                    )
                    called = True
                    break
                base = FieldAccess(obj=base, field=field_tok.literal, position=field_tok.position)
            if not called and self.current.type is TokenType.PAREN_OPEN:
                paren_tok = self.current
                args, kwargs = self._parse_paren_args()
                base = Call(callee=base, args=args, position=paren_tok.position, kwargs=kwargs)
            out.append(base)
        return out

    def _parse_decorated_item(self, end_type: TokenType):
        """M41b: decorators before a top-level `fn`/`extern fn`/`struct`/
        `enum`. (Fields, variants, parameters and impl methods parse their
        own.) Anywhere else -- nested, before `let`/`trait`/`impl`/a
        statement or a closure -- is the one 'only allowed on' error."""
        first = self.current
        if end_type is not TokenType.EOF:
            raise self._decorator_error(first.position)
        decorators = self._parse_decorators()
        tok = self.current
        if tok.type is TokenType.STRUCT:
            node = self._parse_struct_decl(doc_at=first.position)
            node.decorators = decorators
            return node
        if tok.type is TokenType.ENUM:
            node = self._parse_enum_decl(doc_at=first.position)
            node.decorators = decorators
            return node
        if self._at_extern_fn():
            stmt = self._parse_extern_fn(top_level=True, doc_at=first.position)
            stmt.value.decorators = decorators
            return stmt
        if tok.type is TokenType.FN and self.lexer.peek_token().type is TokenType.ID:
            fn_expr = self._parse_fn_expr(decl=True, doc_at=first.position)
            fn_expr.decorators = decorators
            return LetStmt(
                name=fn_expr.name,
                value=fn_expr,
                position=fn_expr.position,
                name_position=fn_expr.name_position,
                is_decl=True,
            )
        raise self._decorator_error(first.position)

    def _synchronize(self, end_type: TokenType) -> None:
        """M6 error recovery: discard tokens until reaching a safe
        resumption point -- a statement-leading keyword (`_SYNC_TOKENS`), a
        `;` (consumed, since it's a natural statement boundary), the
        current call's own `end_type` (left alone -- `_parse_block_items`'s
        own `if self.current.type is end_type: break` check, run at the
        top of its loop, is what actually consumes/reacts to it), or `EOF`
        (always a hard stop, regardless of `end_type`, in case a block
        never gets its closing `}` at all).

        `end_type` matters here, not just a bare "`}` is always special"
        rule: a `{ }` block recurses into this same method with
        `end_type=BRACE_CLOSE`, so leaving a `}` alone there is correct --
        the enclosing `parse_block`/`_parse_block_items` call is right
        there to consume it. But `parse_program`'s own call has
        `end_type=EOF`: a stray, unmatched `}` at the top level has no
        enclosing block waiting to consume it, so if `_synchronize` still
        refused to touch it (the M6 design's original, simpler sketch --
        "a `}` is always left alone"), it would sit there forever, this
        loop would do nothing on every attempt, and the belt-and-suspenders
        check above would *also* refuse to force past it (mistaking "sitting
        on a `}`" for "already safely resynced" in every case) -- a genuine
        infinite loop, caught by this milestone's own pathological-input
        termination test rather than by the design sketch's reasoning,
        which only proved termination for *tokens that start no construct
        at all*, not for a `}` with no matching `{`. Tying "is this `}` a
        safe stop" to *this call's own* `end_type` fixes it: at top level
        a stray `}` is just more garbage to skip over like any other token.

        Termination is otherwise guaranteed the way the original design
        reasoned: `_parse_block_items`'s outer loop already checks
        `if self.current.type is end_type: break` *before* attempting to
        parse an item, so `self.current` is never already `end_type`/`EOF`
        at the point an item-parse is attempted. Every dispatch branch for
        a recognized construct (`LET`, `STRUCT`, `IF`, ...) always consumes
        its leading token before it's possible for a deeper sub-parse to
        fail -- so whenever a parse attempt fails *without having consumed
        anything from the current token onward*, the current token doesn't
        start any recognized construct at all (falls through to
        `_parse_primary`'s final `raise SyntaxError`), and such a token is
        by definition not a sync point either (if it were, some dispatch
        branch would have matched and consumed it, or -- for `end_type`
        specifically -- the outer loop's own check would already have
        broken out before the attempt was ever made) -- so this loop is
        guaranteed to consume at least one token in that case. (One other
        raise site -- `_parse_block_items`'s own "needs a semicolon"
        fallback -- can fire with `self.current` already sitting on a sync
        token, e.g. `5 + 3\nprint(...)`: here `_synchronize`'s loop
        correctly does nothing at all, zero iterations, because we were
        already resynced the moment the error was raised, not because
        anything is stuck -- see `_not_at_sync_point`, which the caller
        uses to tell these two zero-progress cases apart.) The caller
        still double-checks real progress was made (see above) rather than
        trusting this proof unconditionally.
        """
        while self._not_at_sync_point(end_type):
            self.advance()
        if self.current.type is TokenType.SEMICOLON:
            self.advance()

    def _not_at_sync_point(self, end_type: TokenType) -> bool:
        return (
            self.current.type not in _SYNC_TOKENS
            and self.current.type is not TokenType.SEMICOLON
            and self.current.type is not end_type
            and self.current.type is not TokenType.EOF
        )

    # -- statements ---------------------------------------------------------

    def parse_stmt(self):
        """M5: only handles the statement forms in `_STATEMENT_LEADING` --
        `let`/`struct`/`enum`/`return`/`break`/`continue`/`while`/`print`/
        `trait`/`impl` (M12). Everything else (a bare identifier/call/
        field-chain/method-call, `if`, `match`, a bare `{ }` block, `fn`,
        assignment, and any other expression) is now handled directly by
        `_parse_block_items`, which is the only caller of this method --
        see that method and its module-level `_STATEMENT_LEADING` set for
        why."""
        tok = self.current

        if tok.type is TokenType.PRINT:
            self.advance()
            args, kwargs = self._parse_paren_args()
            self._reject_spread(args, kwargs, "print", tok.position)
            sep = None
            end = None
            for name, value, name_position in kwargs:
                if name == "sep":
                    sep = value
                elif name == "end":
                    end = value
                else:
                    raise SyntaxError(
                        f"print() got an unexpected keyword argument '{name}' at position '{name_position}'"
                    )
            return PrintStmt(args=args, position=tok.position, sep=sep, end=end)

        if tok.type is TokenType.LET:
            self.advance()
            name_tok = self.expect(TokenType.ID)
            type_ann = None
            if self.current.type is TokenType.COLON:
                self.advance()
                type_ann = self._parse_type()
            self.expect(TokenType.ASSIGN)
            value = self.parse_expr()
            return LetStmt(
                name=name_tok.literal,
                value=value,
                position=tok.position,
                name_position=name_tok.position,
                type_ann=type_ann,
            )

        if tok.type is TokenType.STRUCT:
            return self._parse_struct_decl()

        if tok.type is TokenType.ENUM:
            return self._parse_enum_decl()

        if tok.type is TokenType.BREAK:
            self.advance()
            # `break value`: the value must start on the `break`'s own line
            # (the same rule as a range's end, see `_can_start_range_end`),
            # so a bare `break` followed by more code on the next line
            # stays a bare `break`.
            value = None
            if self._can_start_range_end(tok):
                value = self.parse_expr()
            return BreakStmt(position=tok.position, value=value)

        if tok.type is TokenType.CONTINUE:
            self.advance()
            return ContinueStmt(position=tok.position)

        if tok.type is TokenType.RETURN:
            self.advance()
            if self.current.type in _EXPR_STOPPERS:
                return ReturnStmt(value=None, position=tok.position)
            value = self.parse_expr()
            return ReturnStmt(value=value, position=tok.position)

        if tok.type is TokenType.DEFER:
            return self._parse_defer_stmt()

        if tok.type is TokenType.TRAIT:
            return self._parse_trait_decl()

        if tok.type is TokenType.IMPL:
            return self._parse_impl_decl()

        raise SyntaxError(f"Invalid syntax '{tok}' at position '{tok.position}'")

    def _parse_defer_stmt(self) -> DeferStmt:
        """M9: `defer <stmt>` desugars into pushing a synthesized, always-
        anonymous, zero-param `FnExpr` wrapping the deferred statement's
        body -- see docs/V2_DESIGN.md's M9 milestone (whose grammar sketch
        is `defer_stmt := "defer" stmt`, a general statement). Surface
        forms: a `_STATEMENT_LEADING` statement (most commonly
        `defer print(...)`, but any of `let`/`return`/`break`/`continue`/
        `while`/`print`/`struct`/`enum` parse the same way any of those do
        elsewhere), a single expression-statement (`defer foo()`), a
        single assignment (`defer x = 5`), or a full `{ ... }` block for
        multiple deferred actions (which can itself contain nested
        `defer`/`if`/etc. via the normal `parse_block`). `print` in
        particular can't be parsed via `parse_expr()` at all (it's a
        dedicated statement form, not an expression) -- hence the
        dedicated `_STATEMENT_LEADING` branch below, rather than just
        expr/assign/block."""
        defer_tok = self.advance()  # DEFER
        if self.current.type is TokenType.BRACE_OPEN:
            inner_block = self.parse_block()
        elif self.current.type in _STATEMENT_LEADING:
            inner_stmt = self.parse_stmt()
            inner_block = Block(stmts=[inner_stmt], position=inner_stmt.position, tail=None)
        else:
            expr = self.parse_expr()
            if self.current.type is TokenType.ASSIGN and isinstance(expr, (Ident, FieldAccess, Index)):
                self.advance()
                value = self.parse_expr()
                inner_stmt = AssignStmt(target=expr, value=value, position=expr.position)
            else:
                inner_stmt = ExprStmt(value=expr, position=expr.position)
            inner_block = Block(stmts=[inner_stmt], position=inner_stmt.position, tail=None)
        closure_expr = FnExpr(
            name=None,
            params=[],
            body=inner_block,
            position=defer_tok.position,
            name_position=None,
            param_positions=[],
        )
        return DeferStmt(closure_expr=closure_expr, position=defer_tok.position)

    def _parse_if(self) -> IfStmt:
        if_tok = self.advance()  # IF
        cond = self._parse_condition_expr()
        then = self.parse_block()
        elifs = []
        while self.current.type is TokenType.ELIF:
            self.advance()
            econd = self._parse_condition_expr()
            eblock = self.parse_block()
            elifs.append((econd, eblock))
        else_ = None
        if self.current.type is TokenType.ELSE:
            self.advance()
            else_ = self.parse_block()
        return IfStmt(cond=cond, then=then, elifs=elifs, else_=else_, position=if_tok.position)

    def _parse_while(self) -> WhileStmt:
        while_tok = self.advance()  # WHILE
        cond = self._parse_condition_expr()
        body = self.parse_block()
        return WhileStmt(cond=cond, body=body, position=while_tok.position)

    def _parse_for(self) -> ForStmt:
        """`for let value[, let index] in iterable { body }`. The iterable is
        a condition-style expression (no bare struct literal, since `{`
        opens the body)."""
        for_tok = self.advance()  # FOR
        self._expect_for_let()
        value_tok = self.expect(TokenType.ID)
        value_type = None
        if self.current.type is TokenType.COLON:
            self.advance()
            value_type = self._parse_type()
        index_tok = None
        index_type = None
        if self.current.type is TokenType.COMMA:
            self.advance()
            self._expect_for_let()
            index_tok = self.expect(TokenType.ID)
            if self.current.type is TokenType.COLON:
                self.advance()
                index_type = self._parse_type()
        self.expect(TokenType.IN)
        iterable = self._parse_condition_expr()
        body = self.parse_block()
        return ForStmt(
            value_name=value_tok.literal,
            index_name=index_tok.literal if index_tok is not None else None,
            iterable=iterable,
            body=body,
            position=for_tok.position,
            value_position=value_tok.position,
            index_position=index_tok.position if index_tok is not None else None,
            value_type=value_type,
            index_type=index_type,
        )

    def _expect_for_let(self) -> None:
        if self.current.type is not TokenType.LET:
            raise SyntaxError(
                f"Expected 'let' before a 'for' loop variable (write 'for let x in ...' "
                f"or 'for let x, let i in ...') at position '{self.current.position}'"
            )
        self.advance()

    def _parse_condition_expr(self):
        """Parse an `if`/`while`/`elif` condition with bare struct-literal
        parsing disabled (see docs/V2_DESIGN.md's M2 milestone) -- the
        classic `if x { ... }` ambiguity between a struct literal on `x`
        and the if-body block. Parenthesizing (`if (X { ... }.f) { ... }`)
        re-enables struct-literal parsing inside the parens."""
        old = self._struct_literal_allowed
        self._struct_literal_allowed = False
        try:
            return self.parse_expr()
        finally:
            self._struct_literal_allowed = old

    def _parse_match(self) -> MatchStmt:
        match_tok = self.advance()  # MATCH
        # Same struct-literal-vs-block-opener ambiguity M2/M3 solved for
        # if/while conditions (`match Point { x: 1, y: 2 } { ... }`) --
        # reuse the exact same suppression helper, no new mechanism needed.
        scrutinee = self._parse_condition_expr()
        self.expect(TokenType.BRACE_OPEN)
        arms = []
        while self.current.type is not TokenType.BRACE_CLOSE:
            arms.append(self._parse_match_arm())
        self.expect(TokenType.BRACE_CLOSE)
        return MatchStmt(scrutinee=scrutinee, arms=arms, position=match_tok.position)

    def _parse_match_arm(self, allow_type_test: bool = False) -> MatchArm:
        """M25: `allow_type_test` is set only by `_parse_try`'s catch-arm
        loop -- a `match` arm's own top-level pattern never allows a
        type-test (only a `catch` arm's does, see `_parse_pattern`)."""
        pattern = self._parse_pattern(allow_type_test=allow_type_test)
        guard = None
        if self.current.type is TokenType.IF:
            # `pattern if cond => { ... }`: the arm only matches when the
            # pattern does and then `cond` (which can use its bindings) is
            # truthy. `=>` ends the condition, so no struct-literal
            # suppression is needed here.
            self.advance()
            guard = self.parse_expr()
        arrow_tok = self.expect(TokenType.FAT_ARROW)
        body = self.parse_block()
        return MatchArm(pattern=pattern, body=body, position=arrow_tok.position, guard=guard)

    # -- M25: throw / try / catch (see docs/ERRORS.md) -------------------

    def _parse_try(self, try_tok: Token) -> TryExpr:
        """`try` is already consumed (`try_tok` is its token). Three forms
        (docs/ERRORS.md, spec grammar):
        - `try { body } catch { arms }`
        - `try { body } else fallback`
        - `try expr else fallback`            (expr not starting with `{`)
        `catch`/`else` here: `else` is the ordinary ELSE token (shared with
        `if`/`elif`); `catch` is contextual (stays an ID, matched by
        `literal`, so `let catch = 1` still works elsewhere)."""
        if self.current.type is TokenType.BRACE_OPEN:
            body = self.parse_block()
            if self.current.type is TokenType.ID and self.current.literal == "catch":
                catch_tok = self.advance()
                self.expect(TokenType.BRACE_OPEN)
                arms = []
                while self.current.type is not TokenType.BRACE_CLOSE:
                    arms.append(self._parse_match_arm(allow_type_test=True))
                self.expect(TokenType.BRACE_CLOSE)
                return TryExpr(
                    body=body,
                    arms=arms,
                    fallback=None,
                    position=try_tok.position,
                    handler_position=catch_tok.position,
                )
            if self.current.type is TokenType.ELSE:
                else_tok = self.advance()
                fallback = self.parse_expr()
                return TryExpr(
                    body=body,
                    arms=[],
                    fallback=fallback,
                    position=try_tok.position,
                    handler_position=else_tok.position,
                )
            raise SyntaxError(
                f"expected 'catch' or 'else' after the 'try' block at position '{self.current.position}'"
            )
        # `try expr else fallback` -- reaching here means the current token
        # isn't `{`, so `parse_expr()` can't accidentally swallow a
        # trailing catch/else block form; the grammar's "expr not starting
        # with '{'" restriction holds automatically.
        body = self.parse_expr()
        else_tok = self.expect(TokenType.ELSE)
        fallback = self.parse_expr()
        return TryExpr(
            body=body,
            arms=[],
            fallback=fallback,
            position=try_tok.position,
            handler_position=else_tok.position,
        )

    def _parse_throws_clause(self) -> Optional[list]:
        """M25: `throws_clause := "throws" ( "never" | type { "|" type } )`,
        accepted (and stored -- ignored until M26's checker) after a
        parameter list's optional `-> type`, in `fn` expressions/decls,
        method decls, and `fn(...)` types. `throws` is contextual (stays
        an ID token). Returns `None` (no clause), `[]` (`throws never`),
        or a list of parsed types."""
        if not (self.current.type is TokenType.ID and self.current.literal == "throws"):
            return None
        self.advance()  # `throws`
        if self.current.type is TokenType.ID and self.current.literal == "never":
            self.advance()
            return []
        throws = [self._parse_type()]
        while self.current.type is TokenType.OR:
            self.advance()
            throws.append(self._parse_type())
        return throws

    # -- patterns ------------------------------------------------------
    #
    # Pattern parsing is a completely separate grammar from expressions --
    # its own dedicated recursive-descent functions, never routed through
    # parse_expr/_parse_primary -- so `ID {` inside a pattern is never
    # ambiguous with anything (unlike the scrutinee expression above): it
    # always means "struct pattern," unconditionally, no suppression flag
    # needed here.

    def _parse_pattern(self, allow_type_test: bool = False):
        tok = self.current

        # M25: `name: Type` / `_: Type` (a "type-test" pattern) is legal
        # only as the *top-level* pattern of a catch arm (see
        # `_parse_match_arm`'s `allow_type_test` parameter) -- checked
        # before anything else in this function since it's the only
        # pattern kind that starts like a plain `BindPat`/`WildcardPat`
        # (an `ID`) but takes a different path on what follows. Anywhere
        # else, an `ID :` in a pattern falls through to the ordinary `ID`
        # branch below, keeping today's (pre-M25) behavior unchanged --
        # `BindPat`, then whatever error the caller gives on the stray
        # `:` it left unconsumed.
        if (
            allow_type_test
            and tok.type is TokenType.ID
            and self.lexer.peek_token().type is TokenType.COLON
        ):
            self.advance()  # the name (or `_`) token
            self.advance()  # COLON
            type_tok = self.expect(TokenType.ID)
            return TypePat(
                name=None if tok.literal == "_" else tok.literal,
                type_name=type_tok.literal,
                position=tok.position,
                type_position=type_tok.position,
            )

        if tok.type is TokenType.NONE:
            self.advance()
            return EnumPat(type_name="Option", variant="none", fields=[], position=tok.position)

        if tok.type is TokenType.SOME:
            self.advance()
            self.expect(TokenType.PAREN_OPEN)
            inner = self._parse_pattern()
            self.expect(TokenType.PAREN_CLOSE)
            return EnumPat(
                type_name="Option", variant="some", fields=[("value", inner)], position=tok.position
            )

        # M17: a pattern starting with `..`/`..=` -- a one-sided range with
        # no lower bound (`..1`, `..=10`). A required literal bound follows
        # (`SyntaxError` if missing/not a literal).
        if tok.type in (TokenType.DOTDOT, TokenType.DOTDOT_EQ):
            self.advance()
            hi = self._parse_range_pattern_bound_required(tok)
            return RangePat(lo=None, hi=hi, inclusive=tok.type is TokenType.DOTDOT_EQ, position=tok.position)

        # M17: `-N` -- a negated number literal, usable as a plain literal
        # pattern and as a range bound below. `-` followed by anything else
        # is the ordinary syntax-error path (falls through to the final
        # raise, `self.current` still sitting on the `-` token since
        # nothing was consumed).
        if tok.type is TokenType.SUB and self.lexer.peek_token().type is TokenType.NUMBER:
            self.advance()
            num_tok = self.advance()
            lit = NumberLit(value=-Decimal(num_tok.literal), position=tok.position)
            return self._maybe_range_pattern(lit)

        if tok.type is TokenType.NUMBER:
            self.advance()
            lit = NumberLit(value=Decimal(tok.literal), position=tok.position)
            return self._maybe_range_pattern(lit)

        if tok.type is TokenType.STRING:
            self.advance()
            raw = tok.literal[1:-1]
            value = decode_string_literal(raw)
            lit = StringLit(value=value, position=tok.position)
            return self._maybe_range_pattern(lit)

        if tok.type is TokenType.TRUE:
            self.advance()
            return BoolLit(value=True, position=tok.position)

        if tok.type is TokenType.FALSE:
            self.advance()
            return BoolLit(value=False, position=tok.position)

        if tok.type is TokenType.ID:
            self.advance()
            if tok.literal == "_":
                return WildcardPat(position=tok.position)
            if self.current.type is TokenType.BRACE_OPEN:
                return self._parse_struct_pat(tok)
            if self.current.type is TokenType.DOT:
                self.advance()
                variant_tok = self.expect(TokenType.ID)
                fields = []
                field_name_positions = []
                if self.current.type is TokenType.BRACE_OPEN:
                    self.advance()
                    fields, field_name_positions = self._parse_pattern_field_list()
                    self.expect(TokenType.BRACE_CLOSE)
                return EnumPat(
                    type_name=tok.literal,
                    variant=variant_tok.literal,
                    fields=fields,
                    position=tok.position,
                    variant_position=variant_tok.position,
                    field_name_positions=field_name_positions,
                )
            return BindPat(name=tok.literal, position=tok.position)

        raise SyntaxError(f"Invalid syntax '{tok}' at position '{tok.position}'")

    # -- M17: range patterns -------------------------------------------

    def _pattern_bound_starts(self) -> bool:
        """Whether the current token can start a range-pattern bound --
        a NUMBER, a negative NUMBER (`SUB` then `NUMBER`), or a STRING.
        Bounds are literals only (no identifiers/expressions)."""
        if self.current.type in (TokenType.NUMBER, TokenType.STRING):
            return True
        return self.current.type is TokenType.SUB and self.lexer.peek_token().type is TokenType.NUMBER

    def _parse_pattern_bound(self):
        """Consume one range-pattern bound -- only called once
        `_pattern_bound_starts()` has confirmed there is one."""
        if self.current.type is TokenType.SUB:
            self.advance()
            num_tok = self.expect(TokenType.NUMBER)
            return NumberLit(value=-Decimal(num_tok.literal), position=num_tok.position)
        if self.current.type is TokenType.NUMBER:
            num_tok = self.advance()
            return NumberLit(value=Decimal(num_tok.literal), position=num_tok.position)
        str_tok = self.expect(TokenType.STRING)
        raw = str_tok.literal[1:-1]
        value = decode_string_literal(raw)
        return StringLit(value=value, position=str_tok.position)

    def _parse_range_pattern_bound_required(self, op_tok: Token):
        """The bound following a PREFIX `..`/`..=` (`..1`, `..=10`) --
        always required (a one-sided range needs its one bound)."""
        if not self._pattern_bound_starts():
            if op_tok.type is TokenType.DOTDOT_EQ:
                raise SyntaxError(f"'..=' needs an end value at position '{op_tok.position}'")
            raise SyntaxError(f"a range pattern starting with '..' needs an end value at position '{op_tok.position}'")
        return self._parse_pattern_bound()

    def _maybe_range_pattern(self, lit):
        """After parsing a literal pattern (`lit`), check for a following
        `..`/`..=` turning it into a `RangePat`'s lower bound -- `1..10`,
        `1..`, `1..=10`. Otherwise `lit` is an ordinary literal pattern."""
        if self.current.type not in (TokenType.DOTDOT, TokenType.DOTDOT_EQ):
            return lit
        op_tok = self.advance()
        if self._pattern_bound_starts():
            hi = self._parse_pattern_bound()
            return RangePat(lo=lit, hi=hi, inclusive=op_tok.type is TokenType.DOTDOT_EQ, position=lit.position)
        if op_tok.type is TokenType.DOTDOT_EQ:
            raise SyntaxError(f"'..=' needs an end value at position '{op_tok.position}'")
        return RangePat(lo=lit, hi=None, inclusive=False, position=lit.position)

    def _parse_struct_pat(self, name_tok: Token) -> StructPat:
        self.expect(TokenType.BRACE_OPEN)
        fields, field_name_positions = self._parse_pattern_field_list()
        self.expect(TokenType.BRACE_CLOSE)
        return StructPat(
            type_name=name_tok.literal,
            fields=fields,
            position=name_tok.position,
            field_name_positions=field_name_positions,
        )

    def _parse_pattern_field_list(self) -> tuple:
        fields = []
        field_name_positions = []
        if self.current.type is not TokenType.BRACE_CLOSE:
            name, sub, pos = self._parse_pattern_field()
            fields.append((name, sub))
            field_name_positions.append(pos)
            while self.current.type is TokenType.COMMA:
                self.advance()
                name, sub, pos = self._parse_pattern_field()
                fields.append((name, sub))
                field_name_positions.append(pos)
        return fields, field_name_positions

    def _parse_pattern_field(self):
        name_tok = self.expect(TokenType.ID)
        if self.current.type is TokenType.COLON:
            self.advance()
            sub = self._parse_pattern()
            return (name_tok.literal, sub, name_tok.position)
        # Shorthand `x` means `x: x` -- bind field x's value to a fresh
        # local variable named x. Deliberately no field-name position here
        # (`None`) -- see StructPat.field_name_positions's docstring in
        # ast_nodes.py for why shorthand stays a pure variable-binding
        # rename, never a field-rename target.
        sub = BindPat(name=name_tok.literal, position=name_tok.position)
        return (name_tok.literal, sub, None)

    def _parse_field_decl(self, allow_decorators: bool = False):
        """M21: `NAME [":" type]` -- a struct field or enum variant field.
        Returns `(name, position, type_or_None, doc_or_None, decorators)`
        (M41a: the `##` doc comment above the field; M41b: the decorators
        before it, only a struct field may have them)."""
        decorators = []
        doc_at = None
        if self.current.type is TokenType.AT:
            if not allow_decorators:
                raise self._decorator_error(self.current.position)
            doc_at = self.current.position
            decorators = self._parse_decorators()
        field_tok = self.expect(TokenType.ID)
        doc = self.lexer.doc_above(doc_at if doc_at is not None else field_tok.position)
        ftype = None
        if self.current.type is TokenType.COLON:
            self.advance()
            ftype = self._parse_type()
        return field_tok.literal, field_tok.position, ftype, doc, decorators

    def _parse_struct_decl(self, doc_at: Optional[int] = None) -> StructDecl:
        struct_tok = self.advance()  # STRUCT
        doc = self.lexer.doc_above(doc_at if doc_at is not None else struct_tok.position)
        name_tok = self.expect(TokenType.ID)
        type_params = self._parse_type_params()
        self.expect(TokenType.BRACE_OPEN)
        fields = []
        field_positions = []
        field_types = []
        field_docs = []
        field_decorators = []
        if self.current.type in (TokenType.ID, TokenType.AT):
            fname, fpos, ftype, fdoc, fdecs = self._parse_field_decl(allow_decorators=True)
            fields.append(fname)
            field_positions.append(fpos)
            field_types.append(ftype)
            field_docs.append(fdoc)
            field_decorators.append(fdecs)
            while self.current.type is TokenType.COMMA:
                self.advance()
                fname, fpos, ftype, fdoc, fdecs = self._parse_field_decl(allow_decorators=True)
                fields.append(fname)
                field_positions.append(fpos)
                field_types.append(ftype)
                field_docs.append(fdoc)
                field_decorators.append(fdecs)
        self.expect(TokenType.BRACE_CLOSE)
        return StructDecl(
            name=name_tok.literal,
            fields=fields,
            position=struct_tok.position,
            name_position=name_tok.position,
            field_positions=field_positions,
            type_params=type_params,
            field_types=field_types,
            doc=doc,
            field_docs=field_docs,
            field_decorators=field_decorators,
        )

    def _parse_enum_decl(self, doc_at: Optional[int] = None) -> EnumDecl:
        enum_tok = self.advance()  # ENUM
        doc = self.lexer.doc_above(doc_at if doc_at is not None else enum_tok.position)
        name_tok = self.expect(TokenType.ID)
        type_params = self._parse_type_params()
        self.expect(TokenType.BRACE_OPEN)
        variants = []
        variant_positions = []
        variant_field_positions_list = []
        variant_field_types_list = []
        variant_docs = []
        variant_decorators = []
        if self.current.type is not TokenType.BRACE_CLOSE:
            (
                variant_name,
                variant_fields,
                variant_pos,
                variant_field_positions,
                variant_field_types,
                variant_doc,
                variant_decs,
            ) = self._parse_enum_variant()
            variant_decorators.append(variant_decs)
            variants.append((variant_name, variant_fields))
            variant_positions.append(variant_pos)
            variant_field_positions_list.append(variant_field_positions)
            variant_field_types_list.append(variant_field_types)
            variant_docs.append(variant_doc)
            while self.current.type is TokenType.COMMA:
                self.advance()
                (
                    variant_name,
                    variant_fields,
                    variant_pos,
                    variant_field_positions,
                    variant_field_types,
                    variant_doc,
                    variant_decs,
                ) = self._parse_enum_variant()
                variant_decorators.append(variant_decs)
                variants.append((variant_name, variant_fields))
                variant_positions.append(variant_pos)
                variant_field_positions_list.append(variant_field_positions)
                variant_field_types_list.append(variant_field_types)
                variant_docs.append(variant_doc)
        self.expect(TokenType.BRACE_CLOSE)
        return EnumDecl(
            name=name_tok.literal,
            variants=variants,
            position=enum_tok.position,
            name_position=name_tok.position,
            variant_positions=variant_positions,
            variant_field_positions=variant_field_positions_list,
            type_params=type_params,
            variant_field_types=variant_field_types_list,
            doc=doc,
            variant_docs=variant_docs,
            variant_decorators=variant_decorators,
        )

    def _parse_enum_variant(self):
        decorators = []
        doc_at = None
        if self.current.type is TokenType.AT:
            doc_at = self.current.position
            decorators = self._parse_decorators()
        name_tok = self.expect(TokenType.ID)
        doc = self.lexer.doc_above(doc_at if doc_at is not None else name_tok.position)
        if self.current.type is TokenType.BRACE_OPEN:
            self.advance()
            fields = []
            field_positions = []
            field_types = []
            if self.current.type in (TokenType.ID, TokenType.AT):
                fname, fpos, ftype, _fdoc, _fdecs = self._parse_field_decl()
                fields.append(fname)
                field_positions.append(fpos)
                field_types.append(ftype)
                while self.current.type is TokenType.COMMA:
                    self.advance()
                    fname, fpos, ftype, _fdoc, _fdecs = self._parse_field_decl()
                    fields.append(fname)
                    field_positions.append(fpos)
                    field_types.append(ftype)
            self.expect(TokenType.BRACE_CLOSE)
            return (name_tok.literal, fields, name_tok.position, field_positions, field_types, doc, decorators)
        return (name_tok.literal, [], name_tok.position, [], [], doc, decorators)

    def _parse_param_list(self, allow_decorators: bool = False) -> tuple:
        """M12: consumes `(` ... `)` and returns `(params, param_positions,
        param_types, defaults)` -- factored out of `_parse_fn_expr` so
        `_parse_method_decl` (trait/impl `fn` items) can share the exact
        same parameter-list grammar, rather than a second, independently-
        maintained copy of it.

        M16: each parameter may be followed by `= expr` giving its default
        value -- `defaults` is parallel to `params`/`param_positions`, each
        entry either that expression or `None`. Struct literals are allowed
        in a default expression (there's no `if`/`while`-condition-style
        ambiguity here).

        M21 (see docs/TYPES.md): each parameter may also be preceded by
        `: type` (before the default) -- `param_types` is parallel too, one
        entry (a TypeExpr or `None`) per parameter. `self` can't be
        annotated (it's always `Self`).

        M41b: `allow_decorators` (a top-level fn's or an impl method's list)
        lets each parameter be preceded by decorators; the result's sixth
        element is parallel to the others (one list per parameter)."""
        self.expect(TokenType.PAREN_OPEN)
        params = []
        param_positions = []
        param_types = []
        defaults = []
        docs = []
        decorators = []
        rest = 0
        first = True
        while self.current.type is not TokenType.PAREN_CLOSE:
            if not first:
                self.expect(TokenType.COMMA)
            first = False
            name, pos, ptype, default, doc, decs, kind = self._parse_one_param(allow_decorators)
            if kind:
                if default is not None:
                    raise SyntaxError(f"a rest parameter can't have a default value at position '{pos}'")
                if name == "self":
                    raise SyntaxError(f"'self' can't be a rest parameter at position '{pos}'")
                if rest & kind:
                    which = "'...'" if kind == 1 else "'**'"
                    raise SyntaxError(f"a function can have only one {which} rest parameter at position '{pos}'")
                if kind == 1 and rest & 2:
                    raise SyntaxError(
                        f"the '...' rest parameter must come before the '**' rest parameter at position '{pos}'"
                    )
                rest |= kind
            elif rest:
                raise SyntaxError(
                    f"a rest parameter ('...' or '**') must come after every ordinary parameter at position '{pos}'"
                )
            params.append(name)
            param_positions.append(pos)
            param_types.append(ptype)
            defaults.append(default)
            docs.append(doc)
            decorators.append(decs)
        self.expect(TokenType.PAREN_CLOSE)
        return params, param_positions, param_types, defaults, docs, decorators, rest

    def _parse_one_param(self, allow_decorators: bool = False):
        decorators = []
        doc_at = None
        if self.current.type is TokenType.AT:
            if not allow_decorators:
                raise self._decorator_error(self.current.position)
            doc_at = self.current.position
            decorators = self._parse_decorators()
        # M41c: `...name` / `**name` -- a rest parameter. `**` is an operator
        # everywhere else, but right at the start of a parameter (after `(`
        # or `,`, which is the only place this runs) it is the marker.
        kind = 0
        if self.current.type is TokenType.ELLIPSIS:
            kind = 1
            self.advance()
        elif self.current.type is TokenType.POW:
            kind = 2
            self.advance()
        param_tok = self.expect(TokenType.ID)
        doc = self.lexer.doc_above(doc_at if doc_at is not None else param_tok.position)
        ptype = None
        if self.current.type is TokenType.COLON:
            colon_tok = self.current
            if param_tok.literal == "self":
                raise SyntaxError(
                    f"'self' can't have a type annotation (it's always Self) "
                    f"at position '{colon_tok.position}'"
                )
            self.advance()
            ptype = self._parse_type()
        default = self._parse_optional_default()
        return param_tok.literal, param_tok.position, ptype, default, doc, decorators, kind

    def _parse_optional_default(self):
        if self.current.type is TokenType.ASSIGN:
            self.advance()
            return self.parse_expr()
        return None

    # -- M21: types (syntax only -- see docs/TYPES.md) ---------------------
    #
    # `type := "fn" "(" [type {"," type}] ")" ["->" type] | NAME ["<" type
    # {"," type} ">"] | "(" type ")"`. Reached only from positions the
    # grammar unambiguously introduces with `:`, `->`, or right after
    # `fn`/`struct`/`enum`/`trait`/`impl` NAME -- `<`/`>` never collide with
    # the comparison operators, which only ever appear inside an ordinary
    # expression, a completely different parse path.

    def _parse_type(self):
        if self.current.type is TokenType.FN:
            fn_tok = self.advance()
            self.expect(TokenType.PAREN_OPEN)
            params = []
            if self.current.type is not TokenType.PAREN_CLOSE:
                params.append(self._parse_type())
                while self.current.type is TokenType.COMMA:
                    self.advance()
                    params.append(self._parse_type())
            self.expect(TokenType.PAREN_CLOSE)
            ret = None
            if self.current.type is TokenType.ARROW:
                self.advance()
                ret = self._parse_type()
            throws = self._parse_throws_clause()
            return FnType(params=params, ret=ret, position=fn_tok.position, throws=throws)
        if self.current.type is TokenType.PAREN_OPEN:
            self.advance()
            inner = self._parse_type()
            self.expect(TokenType.PAREN_CLOSE)
            return inner
        name_tok = self.expect(TokenType.ID)
        args = []
        if self.current.type is TokenType.LT:
            args = self._parse_type_args()
        return NamedType(name=name_tok.literal, args=args, position=name_tok.position)

    def _parse_type_args(self) -> list:
        """The `<...>` after a NAME in type position -- `Vector<Number>`,
        `Map<K, V>`. Assumes the current token is `<`."""
        self.expect(TokenType.LT)
        args = [self._parse_type()]
        while self.current.type is TokenType.COMMA:
            self.advance()
            args.append(self._parse_type())
        self.expect(TokenType.GT)
        return args

    def _parse_type_params(self) -> list:
        """`type_params := "<" type_param {"," type_param} ">"`. Returns
        `[]` when the current token isn't `<` (every caller's generic
        parameter list is optional)."""
        if self.current.type is not TokenType.LT:
            return []
        self.advance()  # LT
        params = [self._parse_type_param()]
        while self.current.type is TokenType.COMMA:
            self.advance()
            params.append(self._parse_type_param())
        self.expect(TokenType.GT)
        return params

    def _parse_type_param(self) -> TypeParam:
        """`type_param := NAME [":" type {"+" type}] ["=" type]`. Bounds are
        parsed as ordinary types -- the resolver (not the parser) rejects a
        bound that doesn't name a declared trait."""
        name_tok = self.expect(TokenType.ID)
        bounds = []
        if self.current.type is TokenType.COLON:
            self.advance()
            bounds.append(self._parse_type())
            while self.current.type is TokenType.ADD:
                self.advance()
                bounds.append(self._parse_type())
        default = None
        if self.current.type is TokenType.ASSIGN:
            self.advance()
            default = self._parse_type()
        return TypeParam(name=name_tok.literal, bounds=bounds, default=default, position=name_tok.position)

    def _parse_fn_expr(self, decl: bool = False, doc_at: Optional[int] = None) -> FnExpr:
        """`decl`: a top-level named `fn` declaration -- its parameters may
        carry decorators (M41b)."""
        fn_tok = self.advance()  # FN
        doc = self.lexer.doc_above(doc_at if doc_at is not None else fn_tok.position)
        name = None
        name_position = None
        if self.current.type is TokenType.ID:
            name_tok = self.advance()
            name = name_tok.literal
            name_position = name_tok.position
        type_params = self._parse_type_params()
        params, param_positions, param_types, defaults, param_docs, param_decorators, rest = self._parse_param_list(
            allow_decorators=decl
        )
        return_type = None
        if self.current.type is TokenType.ARROW:
            self.advance()
            return_type = self._parse_type()
        throws = self._parse_throws_clause()
        body = self.parse_block()
        return FnExpr(
            name=name,
            params=params,
            body=body,
            position=fn_tok.position,
            name_position=name_position,
            param_positions=param_positions,
            defaults=defaults,
            type_params=type_params,
            param_types=param_types,
            return_type=return_type,
            throws=throws,
            doc=doc,
            param_docs=param_docs,
            param_decorators=param_decorators,
            rest=rest,
        )

    # -- M28: test blocks ------------------------------------------------------

    def _at_test_decl(self) -> bool:
        if not (
            self.current.type is TokenType.ID
            and self.current.literal == "test"
            and self.lexer.peek_token().type is TokenType.STRING
        ):
            return False
        if not self.allow_tests:
            raise SyntaxError(
                f"'test' blocks are only allowed in *.test.mh files at position '{self.current.position}'"
            )
        return True

    def _parse_test_decl(self) -> TestDecl:
        """`test "name" { body }` (docs/MAH_TEST.md): the body becomes a
        parameterless function, so it can `return`, `.await` and throw."""
        test_tok = self.advance()  # `test`
        name_tok = self.advance()  # STRING
        body = self.parse_block()
        fn = FnExpr(
            name=None,
            params=[],
            body=body,
            position=test_tok.position,
            param_positions=[],
            defaults=[],
            type_params=[],
            param_types=[],
        )
        return TestDecl(
            name=decode_string_literal(name_tok.literal[1:-1]),
            fn=fn,
            position=test_tok.position,
            name_position=name_tok.position,
        )

    # -- M27: extern fn ------------------------------------------------------

    def _at_extern_fn(self) -> bool:
        """`extern` is contextual (still an ordinary name elsewhere): it
        starts a declaration only when directly followed by `fn`."""
        return (
            self.current.type is TokenType.ID
            and self.current.literal == "extern"
            and self.lexer.peek_token().type is TokenType.FN
        )

    def _parse_extern_fn(self, top_level: bool = False, doc_at: Optional[int] = None) -> LetStmt:
        """`extern fn NAME[<T>](params) [-> T] [throws E] = "module.native"`
        (docs/STDLIB.md, Phase 0) -- desugared to an ordinary named function
        whose body calls the native with its parameters. Only standard
        library modules may use it (the preprocessor enforces that);
        parameters can't have defaults, since a native's arity is fixed."""
        extern_tok = self.advance()  # `extern`
        doc = self.lexer.doc_above(doc_at if doc_at is not None else extern_tok.position)
        self.advance()  # FN
        name_tok = self.expect(TokenType.ID)
        type_params = self._parse_type_params()
        params, param_positions, param_types, defaults, param_docs, param_decorators, rest = self._parse_param_list(
            allow_decorators=top_level
        )
        if any(d is not None for d in defaults):
            raise SyntaxError(f"an extern fn's parameters can't have defaults at position '{name_tok.position}'")
        if rest:
            raise SyntaxError(f"an extern fn can't have rest parameters at position '{name_tok.position}'")
        return_type = None
        if self.current.type is TokenType.ARROW:
            self.advance()
            return_type = self._parse_type()
        throws = self._parse_throws_clause()
        self.expect(TokenType.ASSIGN)
        native_tok = self.expect(TokenType.STRING)
        native = native_tok.literal[1:-1] if native_tok.literal.startswith('"') else native_tok.literal
        args = [Ident(name=p, position=pos) for p, pos in zip(params, param_positions)]
        body = Block(stmts=[], position=native_tok.position, tail=NativeCall(native=native, args=args, position=native_tok.position))
        fn = FnExpr(
            name=name_tok.literal,
            params=params,
            body=body,
            position=extern_tok.position,
            name_position=name_tok.position,
            param_positions=param_positions,
            defaults=defaults,
            type_params=type_params,
            param_types=param_types,
            return_type=return_type,
            throws=throws,
            doc=doc,
            param_docs=param_docs,
            param_decorators=param_decorators,
        )
        fn.native = native
        return LetStmt(
            name=fn.name, value=fn, position=extern_tok.position, name_position=name_tok.position, is_decl=True
        )

    # -- M12: trait / impl / method decls ---------------------------------

    def _parse_trait_decl(self) -> TraitDecl:
        trait_tok = self.advance()  # TRAIT
        doc = self.lexer.doc_above(trait_tok.position)
        name_tok = self.expect(TokenType.ID)
        type_params = self._parse_type_params()
        self.expect(TokenType.BRACE_OPEN)
        methods = []
        while True:
            while self.current.type is TokenType.SEMICOLON:
                self.advance()
            if self.current.type is TokenType.BRACE_CLOSE:
                break
            methods.append(self._parse_method_decl(require_body=False))
        close_tok = self.expect(TokenType.BRACE_CLOSE)
        return TraitDecl(
            name=name_tok.literal,
            methods=methods,
            position=trait_tok.position,
            name_position=name_tok.position,
            end_position=close_tok.position,
            type_params=type_params,
            doc=doc,
        )

    def _parse_impl_decl(self) -> ImplDecl:
        impl_tok = self.advance()  # IMPL
        type_params = self._parse_type_params()
        first_tok = self.expect(TokenType.ID)
        first_args = self._parse_type_args() if self.current.type is TokenType.LT else []
        if self.current.type is TokenType.FOR:
            self.advance()
            second_tok = self.expect(TokenType.ID)
            second_args = self._parse_type_args() if self.current.type is TokenType.LT else []
            trait_name = first_tok.literal
            trait_name_position = first_tok.position
            trait_args = first_args
            type_name = second_tok.literal
            type_name_position = second_tok.position
            type_args = second_args
        else:
            trait_name = None
            trait_name_position = None
            trait_args = []
            type_name = first_tok.literal
            type_name_position = first_tok.position
            type_args = first_args
        self.expect(TokenType.BRACE_OPEN)
        methods = []
        while True:
            while self.current.type is TokenType.SEMICOLON:
                self.advance()
            if self.current.type is TokenType.BRACE_CLOSE:
                break
            methods.append(self._parse_method_decl(require_body=True))
        close_tok = self.expect(TokenType.BRACE_CLOSE)
        return ImplDecl(
            type_name=type_name,
            trait_name=trait_name,
            methods=methods,
            position=impl_tok.position,
            type_name_position=type_name_position,
            trait_name_position=trait_name_position,
            end_position=close_tok.position,
            type_params=type_params,
            type_args=type_args,
            trait_args=trait_args,
        )

    def _parse_method_decl(self, require_body: bool) -> MethodDecl:
        decorators = []
        doc_at = None
        if self.current.type is TokenType.AT:
            if not require_body:  # a trait's method
                raise self._decorator_error(self.current.position)
            doc_at = self.current.position
            decorators = self._parse_decorators()
        fn_tok = self.expect(TokenType.FN)
        doc = self.lexer.doc_above(doc_at if doc_at is not None else fn_tok.position)
        name_tok = self.expect(TokenType.ID)
        type_params = self._parse_type_params()
        params, param_positions, param_types, defaults, param_docs, param_decorators, rest = self._parse_param_list(
            allow_decorators=require_body
        )
        return_type = None
        if self.current.type is TokenType.ARROW:
            self.advance()
            return_type = self._parse_type()
        throws = self._parse_throws_clause()
        if self.current.type is TokenType.BRACE_OPEN:
            body = self.parse_block()
            fn = FnExpr(
                name=name_tok.literal,
                params=params,
                body=body,
                position=fn_tok.position,
                name_position=name_tok.position,
                param_positions=param_positions,
                defaults=defaults,
                type_params=type_params,
                param_types=param_types,
                return_type=return_type,
                throws=throws,
                doc=doc,
                param_docs=param_docs,
                decorators=decorators,
                param_decorators=param_decorators,
                rest=rest,
            )
        elif require_body:
            raise SyntaxError(
                f"Method '{name_tok.literal}' in an impl block needs a body "
                f"at position '{self.current.position}'"
            )
        else:
            fn = None
        return MethodDecl(
            name=name_tok.literal,
            params=params,
            fn=fn,
            position=fn_tok.position,
            name_position=name_tok.position,
            param_positions=param_positions,
            defaults=defaults,
            type_params=type_params,
            param_types=param_types,
            return_type=return_type,
            throws=throws,
            doc=doc,
            param_docs=param_docs,
            decorators=decorators,
            param_decorators=param_decorators,
            rest=rest,
        )

    # -- expressions (precedence chain, lowest to highest binding) --------

    def parse_expr(self):
        return self._parse_range()

    def _can_start_range_end(self, op_tok: Token) -> bool:
        """M17: whether the current token is the end value of the range whose
        `..`/`..=` operator is `op_tok` (for a suffix range, this is what
        tells `1..5` from an open-ended `1..`). It is only when both:

        - the token can begin an expression (`_RANGE_END_STARTERS`; `{` only
          while a bare struct literal is allowed here, since in an
          `if`/`while`/`match` head it opens the body), and
        - it starts on the **same line** as the operator. Mah has no
          significant newlines, so without this rule `let f = 1..` followed
          by `foo(f)` on the next line would silently parse as
          `1..foo(f)`, and one followed by `let ...` would be a syntax
          error."""
        tok = self.current
        if tok.type is TokenType.BRACE_OPEN:
            if not self._struct_literal_allowed:
                return False
        elif tok.type not in _RANGE_END_STARTERS:
            return False
        between = self.lexer.input_str[op_tok.position + len(op_tok.literal) : tok.position]
        return "\n" not in between

    def _parse_range(self):
        """M17: ranges (`a..b`, `a..=b`, `a..`, `..b`, `..=b`) are the
        LOWEST-precedence expression form -- desugared here, at parse time,
        into `StructLit` nodes naming the prelude's `Range`/`FromRange`/
        `ToRange` types (no new expression AST node). Both the range's
        `start` and `end` parse at `_parse_or_and`'s level (the old top of
        the precedence chain), so `1..n + 1` is `1..(n + 1)` and
        `1..10.map(f)` is `1..(10.map(f))` (postfix/method calls bind
        tighter than `..`) -- see docs/MAHC_FORMAT.md-adjacent
        docs/mah-language.md for the worked examples."""
        if self.current.type in (TokenType.DOTDOT, TokenType.DOTDOT_EQ):
            op_tok = self.advance()
            if not self._can_start_range_end(op_tok):
                raise SyntaxError(
                    f"a range starting with '..' needs an end value at position '{op_tok.position}'"
                )
            end = self._parse_or_and()
            return StructLit(
                type_name="ToRange",
                fields=[
                    ("end", end),
                    ("inclusive", BoolLit(value=op_tok.type is TokenType.DOTDOT_EQ, position=op_tok.position)),
                ],
                position=op_tok.position,
                field_name_positions=[],
            )
        left = self._parse_or_and()
        if self.current.type in (TokenType.DOTDOT, TokenType.DOTDOT_EQ):
            op_tok = self.advance()
            if not self._can_start_range_end(op_tok):
                if op_tok.type is TokenType.DOTDOT_EQ:
                    raise SyntaxError(f"'..=' needs an end value at position '{op_tok.position}'")
                return StructLit(
                    type_name="FromRange",
                    fields=[("start", left)],
                    position=op_tok.position,
                    field_name_positions=[],
                )
            end = self._parse_or_and()
            node = StructLit(
                type_name="Range",
                fields=[
                    ("start", left),
                    ("end", end),
                    ("inclusive", BoolLit(value=op_tok.type is TokenType.DOTDOT_EQ, position=op_tok.position)),
                ],
                position=op_tok.position,
                field_name_positions=[],
            )
            if self.current.type in (TokenType.DOTDOT, TokenType.DOTDOT_EQ):
                raise SyntaxError(f"ranges can't be chained at position '{self.current.position}'")
            return node
        return left

    def _parse_or_and(self):
        left = self._parse_compare()
        while self.current.type in (TokenType.OR, TokenType.AND):
            op_tok = self.advance()
            right = self._parse_compare()
            op = "or" if op_tok.type is TokenType.OR else "and"
            left = Binary(op=op, lhs=left, rhs=right, position=op_tok.position)
        return left

    def _parse_compare(self):
        left = self._parse_additive()
        while self.current.type in _COMPARE_OPS:
            op_tok = self.advance()
            right = self._parse_additive()
            left = Binary(op=_COMPARE_OPS[op_tok.type], lhs=left, rhs=right, position=op_tok.position)
        return left

    def _parse_additive(self):
        left = self._parse_multiplicative()
        while self.current.type in _ADDITIVE_OPS:
            op_tok = self.advance()
            right = self._parse_multiplicative()
            left = Binary(op=_ADDITIVE_OPS[op_tok.type], lhs=left, rhs=right, position=op_tok.position)
        return left

    def _parse_multiplicative(self):
        left = self._parse_unary()
        while self.current.type in _MULTIPLICATIVE_OPS:
            op_tok = self.advance()
            right = self._parse_unary()
            left = Binary(op=_MULTIPLICATIVE_OPS[op_tok.type], lhs=left, rhs=right, position=op_tok.position)
        return left

    def _parse_unary(self):
        if self.current.type is TokenType.SUB:
            op_tok = self.advance()
            operand = self._parse_unary()
            return Unary(op="-", operand=operand, position=op_tok.position)
        # M17: `!` at the same precedence level as unary `-` -- `!!x`,
        # `-!x`, `!-x` all parse (each branch recurses back into
        # `_parse_unary`, so either prefix can stack with the other or
        # itself). Postfix (`.`/call) binds tighter: `!x.y()` is
        # `!(x.y())`, since `_parse_pow`/`_parse_primary` (further down the
        # chain) already consume the whole postfix chain before a `!`
        # wrapping it ever gets a chance to.
        if self.current.type is TokenType.BANG:
            op_tok = self.advance()
            operand = self._parse_unary()
            return Unary(op="!", operand=operand, position=op_tok.position)
        return self._parse_pow()

    def _parse_pow(self):
        left = self._parse_primary()
        if self.current.type is TokenType.POW:
            op_tok = self.advance()
            right = self._parse_pow()  # right-associative
            return Binary(op="**", lhs=left, rhs=right, position=op_tok.position)
        return left

    def _parse_primary(self):
        tok = self.current

        if tok.type is TokenType.PAREN_OPEN:
            self.advance()
            # Once inside parens, the if/while struct-literal ambiguity
            # can't occur (a matching `)` unambiguously ends the
            # expression) -- re-enable struct-literal parsing here.
            old = self._struct_literal_allowed
            self._struct_literal_allowed = True
            try:
                expr = self.parse_expr()
            finally:
                self._struct_literal_allowed = old
            self.expect(TokenType.PAREN_CLOSE)
            return self._parse_postfix_from(expr)

        if tok.type is TokenType.IF:
            return self._parse_postfix_from(self._parse_if())

        if tok.type is TokenType.MATCH:
            return self._parse_postfix_from(self._parse_match())

        if tok.type is TokenType.TRY:
            self.advance()
            return self._parse_postfix_from(self._parse_try(tok))

        if tok.type is TokenType.THROW:
            # M25: `throw`'s operand is a full `parse_expr()`, which already
            # handles any postfix chain on it (`throw e.foo()`) -- the
            # `ThrowExpr` node itself is never postfixed (nothing sensible
            # could follow a Never-typed expression).
            self.advance()
            value = self.parse_expr()
            return ThrowExpr(value=value, position=tok.position)

        if tok.type is TokenType.WHILE:
            return self._parse_postfix_from(self._parse_while())

        if tok.type is TokenType.FOR:
            return self._parse_postfix_from(self._parse_for())

        if tok.type is TokenType.BRACE_OPEN:
            return self._parse_postfix_from(self.parse_block())

        if tok.type is TokenType.BRACKET_OPEN:
            return self._parse_postfix_from(self._parse_bracket_literal())

        if tok.type is TokenType.ID:
            if self._at_lock(tok):
                self.advance()
                return self._parse_lock(tok)
            self.advance()
            if self.current.type is TokenType.PAREN_OPEN:
                args, kwargs = self._parse_paren_args()
                callee = Ident(name=tok.literal, position=tok.position)
                node = Call(callee=callee, args=args, position=tok.position, kwargs=kwargs)
            elif self.current.type is TokenType.BRACE_OPEN and self._struct_literal_allowed:
                node = self._parse_struct_lit(tok)
            else:
                node = Ident(name=tok.literal, position=tok.position)
            return self._parse_postfix_from(node)

        if tok.type is TokenType.FN:
            return self._parse_postfix_from(self._parse_fn_expr())

        if tok.type is TokenType.DETACH:
            self.advance()
            return self._parse_detach(tok)

        if tok.type is TokenType.SLEEP_ASYNC:
            self.advance()
            args = self._parse_no_kwargs_args(tok, "sleep_async")
            if len(args) != 1:
                raise SyntaxError(f"'sleep_async' can only have one argument")
            return self._parse_postfix_from(SleepAsyncExpr(arg=args[0], position=tok.position))

        if tok.type is TokenType.NUMBER:
            self.advance()
            return self._parse_postfix_from(NumberLit(value=Decimal(tok.literal), position=tok.position))

        if tok.type is TokenType.STRING:
            self.advance()
            raw = tok.literal[1:-1]
            value = decode_string_literal(raw)
            return self._parse_postfix_from(StringLit(value=value, position=tok.position))

        if tok.type is TokenType.TRUE:
            self.advance()
            return self._parse_postfix_from(BoolLit(value=True, position=tok.position))

        if tok.type is TokenType.FALSE:
            self.advance()
            return self._parse_postfix_from(BoolLit(value=False, position=tok.position))

        if tok.type is TokenType.NONE:
            self.advance()
            return self._parse_postfix_from(
                EnumLit(type_name="Option", variant="none", fields=[], position=tok.position)
            )

        if tok.type is TokenType.SOME:
            self.advance()
            self.expect(TokenType.PAREN_OPEN)
            value = self.parse_expr()
            self.expect(TokenType.PAREN_CLOSE)
            return self._parse_postfix_from(
                EnumLit(
                    type_name="Option",
                    variant="some",
                    fields=[("value", value)],
                    position=tok.position,
                )
            )

        if tok.type is TokenType.AT:
            raise self._decorator_error(tok.position)
        raise SyntaxError(f"Invalid syntax '{tok}' at position '{tok.position}'")

    # -- helpers -----------------------------------------------------------

    def _parse_detach(self, detach_tok: Token):
        """`detach <operand>`, where the operand is any primary expression
        with its postfix chain: a call (`detach f(x)`, `detach
        obj.m(x)`), `detach sleep_async(ms)`, or anything else (`detach {
        ... }`, `detach for ...`, `detach while ...`, `detach if ...`,
        `detach (a + b)`, `detach v[0]`).

        A trailing `.await` belongs to the Promise, not to the operand, so
        `detach f().await` still awaits what `detach` produced (as it did
        since M10). Those steps are peeled off the operand and put back on
        top of the `DetachExpr`.

        A call (or `sleep_async`) is detached directly: its callee and
        arguments are evaluated now, in the current task, and only the call
        runs as a new task. Any other operand is wrapped in a synthesized
        zero-param closure and that closure's call is detached, the same
        desugaring `defer` uses for its body, so the whole expression runs
        in the new task and captures enclosing variables by reference.

        M44 (docs/contracts/M44_threads.md #3.1): `detach(t) expr` runs `expr`
        on the thread `t` -- the thread form, recognized when the `)` is
        followed, on the same line, by a token that starts an operand (see
        `_DETACH_THREAD_OPERAND_START`). Its operand is always wrapped in a
        closure (also a call), so everything in it is evaluated on the
        thread. Any other `detach (x)...` keeps its old meaning."""
        thread = None
        thread_position = None
        paren_head = None
        if self.current.type is TokenType.PAREN_OPEN:
            paren_tok = self.advance()
            old = self._struct_literal_allowed
            self._struct_literal_allowed = True
            try:
                inner = self.parse_expr()
            finally:
                self._struct_literal_allowed = old
            self.expect(TokenType.PAREN_CLOSE)
            nxt = self.current
            same_line = self._on_same_line()
            if same_line and (
                nxt.type in _DETACH_THREAD_OPERAND_START
                or (nxt.type is TokenType.BRACE_OPEN and self._struct_literal_allowed)
            ):
                thread = inner
                thread_position = paren_tok.position
                operand = self._parse_unary() if nxt.type is TokenType.BANG else self._parse_primary()
            elif same_line and nxt.type in _DETACH_THREAD_STATEMENTS:
                raise SyntaxError(f"{_DETACH_NEEDS_EXPR} at position '{nxt.position}'")
            elif nxt.type is TokenType.BRACE_OPEN and not same_line and self._is_name_chain(inner):
                raise SyntaxError(f"{_DETACH_SAME_LINE} at position '{nxt.position}'")
            else:
                operand = self._parse_postfix_from(inner)
                paren_head = inner
        else:
            operand = self._parse_primary()
        awaits = []
        while isinstance(operand, FieldAccess) and operand.field == "await":
            awaits.append(operand)
            operand = operand.obj
        if thread is not None or not isinstance(operand, (Call, MethodCall, SleepAsyncExpr)):
            closure = FnExpr(
                name=None,
                params=[],
                body=Block(stmts=[], position=operand.position, tail=operand),
                position=detach_tok.position,
                name_position=None,
                param_positions=[],
                detached=True,
            )
            operand = Call(callee=closure, args=[], position=detach_tok.position, kwargs=[])
        node = DetachExpr(
            call=operand,
            position=detach_tok.position,
            thread=thread,
            thread_position=thread_position,
            paren_head=paren_head,
        )
        for await_node in reversed(awaits):
            node = FieldAccess(obj=node, field="await", position=await_node.position)
        return node

    @staticmethod
    def _is_name_chain(node) -> bool:
        while isinstance(node, FieldAccess):
            node = node.obj
        return isinstance(node, Ident)

    def _parse_bracket_literal(self):
        """M19: `[a, b]` (Vector), `[k: v, ...]` (Map), `[]`, `[:]`. The
        first item decides which: a `:` after it makes a Map. A trailing
        comma is allowed. Brackets delimit their contents, so a bare
        struct literal is fine inside, as in parentheses."""
        open_tok = self.advance()  # BRACKET_OPEN
        old = self._struct_literal_allowed
        self._struct_literal_allowed = True
        try:
            if self.current.type is TokenType.COLON:
                self.advance()
                self.expect(TokenType.BRACKET_CLOSE)
                return MapLit(pairs=[], position=open_tok.position)
            if self.current.type is TokenType.BRACKET_CLOSE:
                self.advance()
                return VectorLit(items=[], position=open_tok.position)
            first = self.parse_expr()
            if self.current.type is TokenType.COLON:
                self.advance()
                pairs = [(first, self.parse_expr())]
                while self.current.type is TokenType.COMMA:
                    self.advance()
                    if self.current.type is TokenType.BRACKET_CLOSE:
                        break
                    key = self.parse_expr()
                    if self.current.type is not TokenType.COLON:
                        raise SyntaxError(
                            f"Every item in a Map literal needs 'key: value' at position '{self.current.position}'"
                        )
                    self.advance()
                    pairs.append((key, self.parse_expr()))
                self.expect(TokenType.BRACKET_CLOSE)
                return MapLit(pairs=pairs, position=open_tok.position)
            items = [first]
            while self.current.type is TokenType.COMMA:
                self.advance()
                if self.current.type is TokenType.BRACKET_CLOSE:
                    break
                items.append(self.parse_expr())
                if self.current.type is TokenType.COLON:
                    raise SyntaxError(
                        f"A Vector literal can't contain 'key: value' items (a Map literal needs one "
                        f"for every item) at position '{self.current.position}'"
                    )
            self.expect(TokenType.BRACKET_CLOSE)
            return VectorLit(items=items, position=open_tok.position)
        finally:
            self._struct_literal_allowed = old

    def _parse_index(self, base):
        """M19: `base[key]`. Brackets delimit the key, so a bare struct
        literal is fine inside."""
        open_tok = self.advance()  # BRACKET_OPEN
        old = self._struct_literal_allowed
        self._struct_literal_allowed = True
        try:
            key = self.parse_expr()
        finally:
            self._struct_literal_allowed = old
        self.expect(TokenType.BRACKET_CLOSE)
        return Index(obj=base, key=key, position=open_tok.position)

    def _parse_postfix_from(self, base):
        """`.field`, `.method(args)`, (M19) `[key]`, and a call `(args)` on
        any expression (`f(1)(2)`, `handlers[0](x)`, `(fn(x) { x })(5)`),
        left to right. A plain `name(args)` never gets here: `_parse_primary`
        builds that call itself. An index's `[` and a call's `(` must be on
        the same line as what they apply to: Mah has no significant
        newlines, so otherwise `foo()` followed by a line starting with
        `[1, 2].len()` or `(a + b)` would silently continue the expression."""
        while True:
            if self.current.type is TokenType.BRACKET_OPEN and self._on_same_line():
                base = self._parse_index(base)
                continue
            if self.current.type is TokenType.PAREN_OPEN and self._on_same_line():
                paren_tok = self.current
                args, kwargs = self._parse_paren_args()
                base = Call(callee=base, args=args, position=paren_tok.position, kwargs=kwargs)
                continue
            if self.current.type is not TokenType.DOT:
                break
            self.advance()
            field_tok = self.expect(TokenType.ID)
            if (
                isinstance(base, Ident)
                and self.current.type is TokenType.BRACE_OPEN
                and self._struct_literal_allowed
            ):
                self.advance()  # BRACE_OPEN
                fields, field_name_positions = self._parse_field_list()
                self.expect(TokenType.BRACE_CLOSE)
                base = EnumLit(
                    type_name=base.name,
                    variant=field_tok.literal,
                    fields=fields,
                    position=field_tok.position,
                    type_name_position=base.position,
                    field_name_positions=field_name_positions,
                )
            elif self.current.type is TokenType.PAREN_OPEN:
                # M12: `expr.method(args)` -- a method call. `base` may be
                # any expression already built up by this same postfix loop
                # (an Ident, a FieldAccess, another MethodCall, ...), so
                # chains like `a.b().c.d()` fall out for free -- each `.`
                # is handled one at a time, left to right, exactly like the
                # existing FieldAccess branch below.
                args, kwargs = self._parse_paren_args()
                base = MethodCall(
                    obj=base, method=field_tok.literal, args=args, position=field_tok.position, kwargs=kwargs
                )
            else:
                base = FieldAccess(obj=base, field=field_tok.literal, position=field_tok.position)
        return base

    def _parse_field_list(self):
        fields = []
        field_name_positions = []
        if self.current.type is not TokenType.BRACE_CLOSE:
            name, value, pos = self._parse_one_field()
            fields.append((name, value))
            field_name_positions.append(pos)
            while self.current.type is TokenType.COMMA:
                self.advance()
                name, value, pos = self._parse_one_field()
                fields.append((name, value))
                field_name_positions.append(pos)
        return fields, field_name_positions

    def _parse_one_field(self):
        name_tok = self.expect(TokenType.ID)
        self.expect(TokenType.COLON)
        value = self.parse_expr()
        return (name_tok.literal, value, name_tok.position)

    def _parse_struct_lit(self, name_tok: Token) -> StructLit:
        self.expect(TokenType.BRACE_OPEN)
        fields, field_name_positions = self._parse_field_list()
        self.expect(TokenType.BRACE_CLOSE)
        return StructLit(
            type_name=name_tok.literal,
            fields=fields,
            position=name_tok.position,
            field_name_positions=field_name_positions,
        )

    def _parse_no_kwargs_args(self, tok: Token, label: str) -> list:
        """M16: `sleep_async` -- a built-in with a fixed, unnamed single
        parameter -- never accepts keyword arguments. (M27: `sin`/`cos`
        are ordinary calls now, checked by the resolver.)"""
        args, kwargs = self._parse_paren_args()
        self._reject_spread(args, kwargs, label, tok.position)
        if kwargs:
            raise SyntaxError(f"'{label}' doesn't take keyword arguments at position '{tok.position}'")
        return args

    @staticmethod
    def _reject_spread(args: list, kwargs: list, label: str, position: int) -> None:
        """M41a: `print` and `sleep_async` aren't ordinary calls, so they
        don't take spread arguments (`f(...xs, **m)`)."""
        if any(isinstance(a, SpreadArg) for a in args) or any(name is None for name, _v, _p in kwargs):
            raise SyntaxError(f"'{label}' doesn't take spread arguments at position '{position}'")

    def _is_kwarg_start(self) -> bool:
        """M16: an argument-list item is a keyword argument exactly when
        it's `ID COLON` -- distinguished from a bare `ID` (an ordinary
        variable reference) and from `ID { ... }` (a struct literal, whose
        first field also starts `ID COLON` one token later, but only after
        a `{`, which `peek_token` -- one token of lookahead -- never sees
        here) by peeking one token ahead without consuming it."""
        return self.current.type is TokenType.ID and self.lexer.peek_token().type is TokenType.COLON

    def _parse_paren_args(self) -> tuple:
        """Consumes `(` ... `)` and returns `(args, kwargs)` -- `args` the
        positional argument expressions, in order; `kwargs` a parallel list
        of `(name, value_expr, name_position)` for every `name: expr` item,
        in source order, after every positional one. M16: enforces the two
        purely-syntactic call-site rules (a keyword argument can't be
        followed by a positional one; the same keyword can't appear twice
        in one call) -- everything else about a call's arguments (unknown
        keyword, missing required parameter, ...) is dynamic and checked at
        runtime instead, since the callee isn't known statically here."""
        self.expect(TokenType.PAREN_OPEN)
        args = []
        kwargs = []
        seen_kwargs: set = set()
        old = self._struct_literal_allowed
        self._struct_literal_allowed = True
        try:
            if self.current.type is not TokenType.PAREN_CLOSE:
                self._parse_one_arg(args, kwargs, seen_kwargs)
                while self.current.type is TokenType.COMMA:
                    self.advance()
                    self._parse_one_arg(args, kwargs, seen_kwargs)
        finally:
            self._struct_literal_allowed = old
        self.expect(TokenType.PAREN_CLOSE)
        return args, kwargs

    def _parse_one_arg(self, args: list, kwargs: list, seen_kwargs: set) -> None:
        # M41a: `...expr` (positional spread) and `**expr` (keyword spread)
        # start an argument item. `**` is the exponent operator everywhere
        # else, but an expression can't start with it, so at the start of an
        # item (right after `(` or `,`) it can only mean a keyword spread --
        # decided here by position, not in the lexer.
        if self.current.type is TokenType.ELLIPSIS:
            tok = self.advance()
            if kwargs:
                raise SyntaxError(
                    f"positional argument after a keyword argument at position '{tok.position}'"
                )
            args.append(SpreadArg(value=self.parse_expr(), keyword=False, position=tok.position))
            return
        if self.current.type is TokenType.POW:
            tok = self.advance()
            value = self.parse_expr()
            kwargs.append((None, SpreadArg(value=value, keyword=True, position=tok.position), tok.position))
            return
        if self._is_kwarg_start():
            name_tok = self.advance()
            self.expect(TokenType.COLON)
            if name_tok.literal in seen_kwargs:
                raise SyntaxError(
                    f"keyword argument '{name_tok.literal}' given more than once "
                    f"at position '{name_tok.position}'"
                )
            seen_kwargs.add(name_tok.literal)
            value = self.parse_expr()
            kwargs.append((name_tok.literal, value, name_tok.position))
            return
        if kwargs:
            raise SyntaxError(
                f"positional argument after a keyword argument at position '{self.current.position}'"
            )
        args.append(self.parse_expr())
