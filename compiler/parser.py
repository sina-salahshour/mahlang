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
    Block,
    BlockStmt,
    BoolLit,
    BreakStmt,
    Call,
    ContinueStmt,
    CosExpr,
    EnumDecl,
    EnumLit,
    ExprStmt,
    FieldAccess,
    FnExpr,
    Ident,
    IfStmt,
    InputExpr,
    LetStmt,
    NumberLit,
    PrintStmt,
    ReturnStmt,
    SinExpr,
    StringLit,
    StructDecl,
    StructLit,
    Unary,
    WhileStmt,
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
        stmts = self._parse_stmts_until(TokenType.EOF)
        self.expect(TokenType.EOF)
        return stmts

    def _parse_stmts_until(self, end_type: TokenType) -> list:
        stmts = []
        while True:
            while self.current.type is TokenType.SEMICOLON:
                self.advance()
            if self.current.type is end_type:
                break
            stmts.append(self.parse_stmt())
        return stmts

    def parse_block(self) -> Block:
        open_tok = self.expect(TokenType.BRACE_OPEN)
        stmts = self._parse_stmts_until(TokenType.BRACE_CLOSE)
        self.expect(TokenType.BRACE_CLOSE)
        return Block(stmts=stmts, position=open_tok.position)

    # -- statements ---------------------------------------------------------

    def parse_stmt(self):
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
            return LetStmt(name=name_tok.literal, value=value, position=tok.position)

        if tok.type is TokenType.ID:
            self.advance()
            if self.current.type is TokenType.ASSIGN:
                self.advance()
                value = self.parse_expr()
                target = Ident(name=tok.literal, position=tok.position)
                return AssignStmt(target=target, value=value, position=tok.position)
            if self.current.type is TokenType.DOT:
                base = Ident(name=tok.literal, position=tok.position)
                target = self._parse_postfix_from(base)
                self.expect(TokenType.ASSIGN)
                value = self.parse_expr()
                return AssignStmt(target=target, value=value, position=tok.position)
            if self.current.type is TokenType.PAREN_OPEN:
                args = self._parse_paren_args()
                callee = Ident(name=tok.literal, position=tok.position)
                call = Call(callee=callee, args=args, position=tok.position)
                return ExprStmt(value=call, position=tok.position)
            raise SyntaxError(
                f"Invalid syntax '{self.current}' at position '{self.current.position}'"
            )

        if tok.type is TokenType.STRUCT:
            return self._parse_struct_decl()

        if tok.type is TokenType.ENUM:
            return self._parse_enum_decl()

        if tok.type is TokenType.IF:
            return self._parse_if()

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

        if tok.type is TokenType.FN:
            fn_expr = self._parse_fn_expr()
            if fn_expr.name is not None:
                return LetStmt(name=fn_expr.name, value=fn_expr, position=fn_expr.position)
            return ExprStmt(value=fn_expr, position=fn_expr.position)

        if tok.type is TokenType.BRACE_OPEN:
            block = self.parse_block()
            return BlockStmt(block=block, position=tok.position)

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
        if self.current.type is TokenType.ID:
            name = self.advance().literal
        self.expect(TokenType.PAREN_OPEN)
        params = []
        if self.current.type is TokenType.ID:
            params.append(self.advance().literal)
            while self.current.type is TokenType.COMMA:
                self.advance()
                params.append(self.expect(TokenType.ID).literal)
        self.expect(TokenType.PAREN_CLOSE)
        body = self.parse_block()
        return FnExpr(name=name, params=params, body=body, position=fn_tok.position)

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
