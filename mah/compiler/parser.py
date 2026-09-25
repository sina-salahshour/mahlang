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

from .ast_nodes import (
    AssignStmt,
    Binary,
    BindPat,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ContinueStmt,
    CosExpr,
    DeferStmt,
    DetachExpr,
    EnumDecl,
    EnumLit,
    EnumPat,
    ErrorNode,
    ExprStmt,
    FieldAccess,
    FnExpr,
    Ident,
    IfStmt,
    ImplDecl,
    InputExpr,
    LetStmt,
    MatchArm,
    MatchStmt,
    MethodCall,
    MethodDecl,
    NumberLit,
    PrintStmt,
    RangePat,
    ReturnStmt,
    SinExpr,
    SleepAsyncExpr,
    StringLit,
    StructDecl,
    StructLit,
    StructPat,
    TraitDecl,
    Unary,
    WhileStmt,
    WildcardPat,
)
from .lexer import Lexer, Token, TokenType

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
    TokenType.FN,
    TokenType.SIN,
    TokenType.COS,
    TokenType.INPUT,
    TokenType.DETACH,
    TokenType.SLEEP_ASYNC,
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

# M5: statement-leading tokens still handled by parse_stmt's own dedicated
# logic, unchanged. IF/MATCH are deliberately NOT here any more -- they
# must flow through the general expression path (parse_expr ->
# _parse_primary) so they can be used as expressions/tails, with
# _parse_block_items below handling the "semicolon required unless
# block-shaped or last in the block" disambiguation. WHILE stays here: it's
# never expression-capable (no meaningful "value" for a loop), so it's
# parsed exactly as before, always appended directly to `stmts`.
_STATEMENT_LEADING = {
    TokenType.LET,
    TokenType.STRUCT,
    TokenType.ENUM,
    TokenType.RETURN,
    TokenType.BREAK,
    TokenType.CONTINUE,
    TokenType.WHILE,
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
_SYNC_TOKENS = _STATEMENT_LEADING | {TokenType.FN, TokenType.IF, TokenType.MATCH}


class Parser:
    def __init__(self, lexer: Lexer) -> None:
        self.lexer = lexer
        self.current: Token = lexer.get_next_token()
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
        return tok

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
                if self.current.type in _STATEMENT_LEADING:
                    stmts.append(self.parse_stmt())
                    continue

                if self.current.type is TokenType.FN:
                    fn_expr = self._parse_fn_expr()
                    if fn_expr.name is not None:
                        stmts.append(
                            LetStmt(
                                name=fn_expr.name,
                                value=fn_expr,
                                position=fn_expr.position,
                                name_position=fn_expr.name_position,
                            )
                        )
                        continue
                    expr = fn_expr  # anonymous fn: falls through to the general handling below
                else:
                    expr = self.parse_expr()  # handles IF, MATCH, bare `{`, ID (ident/call/
                    # field-chain/struct-lit/enum-lit), literals, etc. via
                    # _parse_primary.

                if self.current.type is TokenType.ASSIGN and isinstance(expr, (Ident, FieldAccess)):
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
                        Block,
                        Call,
                        FnExpr,
                        DetachExpr,
                        SleepAsyncExpr,
                        FieldAccess,
                        MethodCall,
                    ),
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
            self.expect(TokenType.ASSIGN)
            value = self.parse_expr()
            return LetStmt(
                name=name_tok.literal,
                value=value,
                position=tok.position,
                name_position=name_tok.position,
            )

        if tok.type is TokenType.STRUCT:
            return self._parse_struct_decl()

        if tok.type is TokenType.ENUM:
            return self._parse_enum_decl()

        if tok.type is TokenType.WHILE:
            self.advance()
            cond = self._parse_condition_expr()
            body = self.parse_block()
            return WhileStmt(cond=cond, body=body, position=tok.position)

        if tok.type is TokenType.BREAK:
            self.advance()
            return BreakStmt(position=tok.position)

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
            if self.current.type is TokenType.ASSIGN and isinstance(expr, (Ident, FieldAccess)):
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

    def _parse_match_arm(self) -> MatchArm:
        pattern = self._parse_pattern()
        arrow_tok = self.expect(TokenType.FAT_ARROW)
        body = self.parse_block()
        return MatchArm(pattern=pattern, body=body, position=arrow_tok.position)

    # -- patterns ------------------------------------------------------
    #
    # Pattern parsing is a completely separate grammar from expressions --
    # its own dedicated recursive-descent functions, never routed through
    # parse_expr/_parse_primary -- so `ID {` inside a pattern is never
    # ambiguous with anything (unlike the scrutinee expression above): it
    # always means "struct pattern," unconditionally, no suppression flag
    # needed here.

    def _parse_pattern(self):
        tok = self.current

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

    def _parse_struct_decl(self) -> StructDecl:
        struct_tok = self.advance()  # STRUCT
        name_tok = self.expect(TokenType.ID)
        self.expect(TokenType.BRACE_OPEN)
        fields = []
        field_positions = []
        if self.current.type is TokenType.ID:
            field_tok = self.advance()
            fields.append(field_tok.literal)
            field_positions.append(field_tok.position)
            while self.current.type is TokenType.COMMA:
                self.advance()
                field_tok = self.expect(TokenType.ID)
                fields.append(field_tok.literal)
                field_positions.append(field_tok.position)
        self.expect(TokenType.BRACE_CLOSE)
        return StructDecl(
            name=name_tok.literal,
            fields=fields,
            position=struct_tok.position,
            name_position=name_tok.position,
            field_positions=field_positions,
        )

    def _parse_enum_decl(self) -> EnumDecl:
        enum_tok = self.advance()  # ENUM
        name_tok = self.expect(TokenType.ID)
        self.expect(TokenType.BRACE_OPEN)
        variants = []
        variant_positions = []
        variant_field_positions_list = []
        if self.current.type is not TokenType.BRACE_CLOSE:
            variant_name, variant_fields, variant_pos, variant_field_positions = self._parse_enum_variant()
            variants.append((variant_name, variant_fields))
            variant_positions.append(variant_pos)
            variant_field_positions_list.append(variant_field_positions)
            while self.current.type is TokenType.COMMA:
                self.advance()
                variant_name, variant_fields, variant_pos, variant_field_positions = self._parse_enum_variant()
                variants.append((variant_name, variant_fields))
                variant_positions.append(variant_pos)
                variant_field_positions_list.append(variant_field_positions)
        self.expect(TokenType.BRACE_CLOSE)
        return EnumDecl(
            name=name_tok.literal,
            variants=variants,
            position=enum_tok.position,
            name_position=name_tok.position,
            variant_positions=variant_positions,
            variant_field_positions=variant_field_positions_list,
        )

    def _parse_enum_variant(self):
        name_tok = self.expect(TokenType.ID)
        if self.current.type is TokenType.BRACE_OPEN:
            self.advance()
            fields = []
            field_positions = []
            if self.current.type is TokenType.ID:
                field_tok = self.advance()
                fields.append(field_tok.literal)
                field_positions.append(field_tok.position)
                while self.current.type is TokenType.COMMA:
                    self.advance()
                    field_tok = self.expect(TokenType.ID)
                    fields.append(field_tok.literal)
                    field_positions.append(field_tok.position)
            self.expect(TokenType.BRACE_CLOSE)
            return (name_tok.literal, fields, name_tok.position, field_positions)
        return (name_tok.literal, [], name_tok.position, [])

    def _parse_param_list(self) -> tuple:
        """M12: consumes `(` ... `)` and returns `(params, param_positions,
        defaults)` -- factored out of `_parse_fn_expr` so `_parse_method_decl`
        (trait/impl `fn` items) can share the exact same parameter-list
        grammar, rather than a second, independently-maintained copy of it.

        M16: each parameter may be followed by `= expr` giving its default
        value -- `defaults` is parallel to `params`/`param_positions`, each
        entry either that expression or `None`. Struct literals are allowed
        in a default expression (there's no `if`/`while`-condition-style
        ambiguity here)."""
        self.expect(TokenType.PAREN_OPEN)
        params = []
        param_positions = []
        defaults = []
        if self.current.type is TokenType.ID:
            param_tok = self.advance()
            params.append(param_tok.literal)
            param_positions.append(param_tok.position)
            defaults.append(self._parse_optional_default())
            while self.current.type is TokenType.COMMA:
                self.advance()
                param_tok = self.expect(TokenType.ID)
                params.append(param_tok.literal)
                param_positions.append(param_tok.position)
                defaults.append(self._parse_optional_default())
        self.expect(TokenType.PAREN_CLOSE)
        return params, param_positions, defaults

    def _parse_optional_default(self):
        if self.current.type is TokenType.ASSIGN:
            self.advance()
            return self.parse_expr()
        return None

    def _parse_fn_expr(self) -> FnExpr:
        fn_tok = self.advance()  # FN
        name = None
        name_position = None
        if self.current.type is TokenType.ID:
            name_tok = self.advance()
            name = name_tok.literal
            name_position = name_tok.position
        params, param_positions, defaults = self._parse_param_list()
        body = self.parse_block()
        return FnExpr(
            name=name,
            params=params,
            body=body,
            position=fn_tok.position,
            name_position=name_position,
            param_positions=param_positions,
            defaults=defaults,
        )

    # -- M12: trait / impl / method decls ---------------------------------

    def _parse_trait_decl(self) -> TraitDecl:
        trait_tok = self.advance()  # TRAIT
        name_tok = self.expect(TokenType.ID)
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
        )

    def _parse_impl_decl(self) -> ImplDecl:
        impl_tok = self.advance()  # IMPL
        first_tok = self.expect(TokenType.ID)
        if self.current.type is TokenType.FOR:
            self.advance()
            second_tok = self.expect(TokenType.ID)
            trait_name = first_tok.literal
            trait_name_position = first_tok.position
            type_name = second_tok.literal
            type_name_position = second_tok.position
        else:
            trait_name = None
            trait_name_position = None
            type_name = first_tok.literal
            type_name_position = first_tok.position
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
        )

    def _parse_method_decl(self, require_body: bool) -> MethodDecl:
        fn_tok = self.expect(TokenType.FN)
        name_tok = self.expect(TokenType.ID)
        params, param_positions, defaults = self._parse_param_list()
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

        if tok.type is TokenType.BRACE_OPEN:
            return self._parse_postfix_from(self.parse_block())

        if tok.type is TokenType.ID:
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

        if tok.type is TokenType.SIN:
            self.advance()
            args = self._parse_no_kwargs_args(tok, "sin")
            if len(args) != 1:
                raise SyntaxError(f"'sin' can only have one argument")
            return self._parse_postfix_from(SinExpr(arg=args[0], position=tok.position))

        if tok.type is TokenType.COS:
            self.advance()
            args = self._parse_no_kwargs_args(tok, "cos")
            if len(args) != 1:
                raise SyntaxError(f"'cos' can only have one argument")
            return self._parse_postfix_from(CosExpr(arg=args[0], position=tok.position))

        if tok.type is TokenType.INPUT:
            self.advance()
            self.expect(TokenType.PAREN_OPEN)
            self.expect(TokenType.PAREN_CLOSE)
            return self._parse_postfix_from(InputExpr(position=tok.position))

        if tok.type is TokenType.DETACH:
            self.advance()
            if self.current.type is TokenType.SLEEP_ASYNC:
                # `detach sleep_async(ms)` -- the one builtin-shaped
                # exception to "detach wraps a plain ID(...) call": see
                # DetachExpr's own comment and codegen.py for why this
                # compiles completely differently from an ordinary
                # detached call.
                sleep_tok = self.advance()
                args = self._parse_no_kwargs_args(sleep_tok, "sleep_async")
                if len(args) != 1:
                    raise SyntaxError(f"'sleep_async' can only have one argument")
                inner = SleepAsyncExpr(arg=args[0], position=sleep_tok.position)
                return self._parse_postfix_from(DetachExpr(call=inner, position=tok.position))
            return self._parse_detach_operand(tok)

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

        raise SyntaxError(f"Invalid syntax '{tok}' at position '{tok.position}'")

    # -- helpers -----------------------------------------------------------

    def _parse_detach_operand(self, detach_tok: Token):
        """M13: `detach obj.method(args)` -- lifts M10's original
        `detach name(args)`-only restriction (which is still handled by a
        dedicated, simpler branch in `_parse_primary` right before this is
        called) to any call chain rooted at a plain identifier: `detach
        Type.method(...)` (a static path), `detach obj.a().b(...)`, etc.

        Algorithm: parse a leading `ID` (optionally immediately called,
        `ID(args)`) as `base`, then collect every following `.name` /
        `.name(args)` postfix step *without* building any AST for them yet
        (`steps`, a flat list of `(name_tok, args_or_None)`). `k` is the
        index of the LAST step that has args (a real call) -- everything
        from `base` up to and including step `k` is what actually gets
        detached (wrapped in one `DetachExpr`); everything after step `k`
        (necessarily all bare field accesses, e.g. a trailing `.await`) is
        rebuilt as ordinary `FieldAccess` nodes on top of that `DetachExpr`,
        exactly as `detach work().await` already worked pre-M13. No step
        after `k` can itself be a call, by definition of `k` being the
        *last* one that is.

        If there is no call anywhere at all (`k == -1` and `base` is a bare
        `Ident`, e.g. `detach p.f`), that's a syntax error -- `detach`
        always needs *some* call to actually detach."""
        base_tok = self.expect(TokenType.ID)
        if self.current.type is TokenType.PAREN_OPEN:
            args, kwargs = self._parse_paren_args()
            base = Call(
                callee=Ident(name=base_tok.literal, position=base_tok.position),
                args=args,
                position=base_tok.position,
                kwargs=kwargs,
            )
        else:
            base = Ident(name=base_tok.literal, position=base_tok.position)

        steps: list = []  # list[(name_tok, call_info_or_None)] -- call_info = (args, kwargs)
        while self.current.type is TokenType.DOT:
            self.advance()
            name_tok = self.expect(TokenType.ID)
            if self.current.type is TokenType.PAREN_OPEN:
                steps.append((name_tok, self._parse_paren_args()))
            else:
                steps.append((name_tok, None))

        k = -1
        for index, (_name_tok, call_info) in enumerate(steps):
            if call_info is not None:
                k = index
        if k == -1 and not isinstance(base, Call):
            raise SyntaxError(f"'detach' needs a function or method call at position '{detach_tok.position}'")

        node = base
        for name_tok, call_info in steps[: k + 1]:
            if call_info is None:
                node = FieldAccess(obj=node, field=name_tok.literal, position=name_tok.position)
            else:
                args, kwargs = call_info
                node = MethodCall(
                    obj=node, method=name_tok.literal, args=args, position=name_tok.position, kwargs=kwargs
                )
        node = DetachExpr(call=node, position=detach_tok.position)

        for name_tok, _call_info in steps[k + 1 :]:
            node = FieldAccess(obj=node, field=name_tok.literal, position=name_tok.position)

        return self._parse_postfix_from(node)

    def _parse_postfix_from(self, base):
        while self.current.type is TokenType.DOT:
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
        """M16: `sin`/`cos`/`sleep_async` -- built-ins with a fixed,
        unnamed single parameter -- never accept keyword arguments."""
        args, kwargs = self._parse_paren_args()
        if kwargs:
            raise SyntaxError(f"'{label}' doesn't take keyword arguments at position '{tok.position}'")
        return args

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
