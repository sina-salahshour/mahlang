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
    EnumDecl,
    EnumLit,
    EnumPat,
    ErrorNode,
    ExprStmt,
    FieldAccess,
    FnExpr,
    Ident,
    IfStmt,
    InputExpr,
    LetStmt,
    MatchArm,
    MatchStmt,
    NumberLit,
    PrintStmt,
    ReturnStmt,
    SinExpr,
    StringLit,
    StructDecl,
    StructLit,
    StructPat,
    Unary,
    WhileStmt,
    WildcardPat,
)
from .lexer import Lexer, Token, TokenType

_COMPARE_OPS = {
    TokenType.EQ: "eq",
    TokenType.NEQ: "neq",
    TokenType.LT: "lt",
    TokenType.GT: "gt",
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
                if isinstance(expr, (IfStmt, MatchStmt, Block, Call, FnExpr)):
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
        `let`/`struct`/`enum`/`return`/`break`/`continue`/`while`/`print`.
        Everything else (a bare identifier/call/field-chain, `if`, `match`,
        a bare `{ }` block, `fn`, assignment, and any other expression) is
        now handled directly by `_parse_block_items`, which is the only
        caller of this method -- see that method and its module-level
        `_STATEMENT_LEADING` set for why."""
        tok = self.current

        if tok.type is TokenType.PRINT:
            self.advance()
            args = self._parse_paren_args()
            return PrintStmt(args=args, position=tok.position)

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

        raise SyntaxError(f"Invalid syntax '{tok}' at position '{tok.position}'")

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

        if tok.type is TokenType.NUMBER:
            self.advance()
            return NumberLit(value=Decimal(tok.literal), position=tok.position)

        if tok.type is TokenType.STRING:
            self.advance()
            raw = tok.literal[1:-1]
            value = bytes(raw, "utf-8").decode("unicode_escape")
            return StringLit(value=value, position=tok.position)

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
                if self.current.type is TokenType.BRACE_OPEN:
                    self.advance()
                    fields = self._parse_pattern_field_list()
                    self.expect(TokenType.BRACE_CLOSE)
                return EnumPat(
                    type_name=tok.literal,
                    variant=variant_tok.literal,
                    fields=fields,
                    position=tok.position,
                )
            return BindPat(name=tok.literal, position=tok.position)

        raise SyntaxError(f"Invalid syntax '{tok}' at position '{tok.position}'")

    def _parse_struct_pat(self, name_tok: Token) -> StructPat:
        self.expect(TokenType.BRACE_OPEN)
        fields = self._parse_pattern_field_list()
        self.expect(TokenType.BRACE_CLOSE)
        return StructPat(type_name=name_tok.literal, fields=fields, position=name_tok.position)

    def _parse_pattern_field_list(self) -> list:
        fields = []
        if self.current.type is not TokenType.BRACE_CLOSE:
            fields.append(self._parse_pattern_field())
            while self.current.type is TokenType.COMMA:
                self.advance()
                fields.append(self._parse_pattern_field())
        return fields

    def _parse_pattern_field(self):
        name_tok = self.expect(TokenType.ID)
        if self.current.type is TokenType.COLON:
            self.advance()
            sub = self._parse_pattern()
        else:
            # Shorthand `x` means `x: x` -- bind field x's value to a fresh
            # local variable named x.
            sub = BindPat(name=name_tok.literal, position=name_tok.position)
        return (name_tok.literal, sub)

    def _parse_struct_decl(self) -> StructDecl:
        struct_tok = self.advance()  # STRUCT
        name_tok = self.expect(TokenType.ID)
        self.expect(TokenType.BRACE_OPEN)
        fields = []
        if self.current.type is TokenType.ID:
            fields.append(self.advance().literal)
            while self.current.type is TokenType.COMMA:
                self.advance()
                fields.append(self.expect(TokenType.ID).literal)
        self.expect(TokenType.BRACE_CLOSE)
        return StructDecl(name=name_tok.literal, fields=fields, position=struct_tok.position)

    def _parse_enum_decl(self) -> EnumDecl:
        enum_tok = self.advance()  # ENUM
        name_tok = self.expect(TokenType.ID)
        self.expect(TokenType.BRACE_OPEN)
        variants = []
        if self.current.type is not TokenType.BRACE_CLOSE:
            variants.append(self._parse_enum_variant())
            while self.current.type is TokenType.COMMA:
                self.advance()
                variants.append(self._parse_enum_variant())
        self.expect(TokenType.BRACE_CLOSE)
        return EnumDecl(name=name_tok.literal, variants=variants, position=enum_tok.position)

    def _parse_enum_variant(self):
        name_tok = self.expect(TokenType.ID)
        if self.current.type is TokenType.BRACE_OPEN:
            self.advance()
            fields = []
            if self.current.type is TokenType.ID:
                fields.append(self.advance().literal)
                while self.current.type is TokenType.COMMA:
                    self.advance()
                    fields.append(self.expect(TokenType.ID).literal)
            self.expect(TokenType.BRACE_CLOSE)
            return (name_tok.literal, fields)
        return (name_tok.literal, [])

    def _parse_fn_expr(self) -> FnExpr:
        fn_tok = self.advance()  # FN
        name = None
        name_position = None
        if self.current.type is TokenType.ID:
            name_tok = self.advance()
            name = name_tok.literal
            name_position = name_tok.position
        self.expect(TokenType.PAREN_OPEN)
        params = []
        param_positions = []
        if self.current.type is TokenType.ID:
            param_tok = self.advance()
            params.append(param_tok.literal)
            param_positions.append(param_tok.position)
            while self.current.type is TokenType.COMMA:
                self.advance()
                param_tok = self.expect(TokenType.ID)
                params.append(param_tok.literal)
                param_positions.append(param_tok.position)
        self.expect(TokenType.PAREN_CLOSE)
        body = self.parse_block()
        return FnExpr(
            name=name,
            params=params,
            body=body,
            position=fn_tok.position,
            name_position=name_position,
            param_positions=param_positions,
        )

    # -- expressions (precedence chain, lowest to highest binding) --------

    def parse_expr(self):
        return self._parse_or_and()

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
                args = self._parse_paren_args()
                callee = Ident(name=tok.literal, position=tok.position)
                node = Call(callee=callee, args=args, position=tok.position)
            elif self.current.type is TokenType.BRACE_OPEN and self._struct_literal_allowed:
                node = self._parse_struct_lit(tok)
            else:
                node = Ident(name=tok.literal, position=tok.position)
            return self._parse_postfix_from(node)

        if tok.type is TokenType.FN:
            return self._parse_postfix_from(self._parse_fn_expr())

        if tok.type is TokenType.SIN:
            self.advance()
            args = self._parse_paren_args()
            if len(args) != 1:
                raise SyntaxError(f"'sin' can only have one argument")
            return self._parse_postfix_from(SinExpr(arg=args[0], position=tok.position))

        if tok.type is TokenType.COS:
            self.advance()
            args = self._parse_paren_args()
            if len(args) != 1:
                raise SyntaxError(f"'cos' can only have one argument")
            return self._parse_postfix_from(CosExpr(arg=args[0], position=tok.position))

        if tok.type is TokenType.INPUT:
            self.advance()
            self.expect(TokenType.PAREN_OPEN)
            self.expect(TokenType.PAREN_CLOSE)
            return self._parse_postfix_from(InputExpr(position=tok.position))

        if tok.type is TokenType.NUMBER:
            self.advance()
            return self._parse_postfix_from(NumberLit(value=Decimal(tok.literal), position=tok.position))

        if tok.type is TokenType.STRING:
            self.advance()
            raw = tok.literal[1:-1]
            value = bytes(raw, "utf-8").decode("unicode_escape")
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
                fields = self._parse_field_list()
                self.expect(TokenType.BRACE_CLOSE)
                base = EnumLit(
                    type_name=base.name,
                    variant=field_tok.literal,
                    fields=fields,
                    position=field_tok.position,
                )
            else:
                base = FieldAccess(obj=base, field=field_tok.literal, position=field_tok.position)
        return base

    def _parse_field_list(self):
        fields = []
        if self.current.type is not TokenType.BRACE_CLOSE:
            fields.append(self._parse_one_field())
            while self.current.type is TokenType.COMMA:
                self.advance()
                fields.append(self._parse_one_field())
        return fields

    def _parse_one_field(self):
        name_tok = self.expect(TokenType.ID)
        self.expect(TokenType.COLON)
        value = self.parse_expr()
        return (name_tok.literal, value)

    def _parse_struct_lit(self, name_tok: Token) -> StructLit:
        self.expect(TokenType.BRACE_OPEN)
        fields = self._parse_field_list()
        self.expect(TokenType.BRACE_CLOSE)
        return StructLit(type_name=name_tok.literal, fields=fields, position=name_tok.position)

    def _parse_paren_args(self) -> list:
        self.expect(TokenType.PAREN_OPEN)
        args = []
        old = self._struct_literal_allowed
        self._struct_literal_allowed = True
        try:
            if self.current.type is not TokenType.PAREN_CLOSE:
                args.append(self.parse_expr())
                while self.current.type is TokenType.COMMA:
                    self.advance()
                    args.append(self.parse_expr())
        finally:
            self._struct_literal_allowed = old
        self.expect(TokenType.PAREN_CLOSE)
        return args
