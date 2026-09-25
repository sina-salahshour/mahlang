"""`mah format`: reprint Mah source in one consistent layout. See
docs/FORMAT.md for the rules and the safety guarantee (only whitespace
between tokens ever changes, and every result is verified before it's
returned).

Pipeline: lex the whole file (`_lex`), recover comments and blank lines
from the text between tokens (`_trivia`), parse a copy with
`import`/`export` blanked out so the parser can read it, and use the AST
only to classify tokens: where statements start, which `{` opens a block,
which `<` is a generic bracket, which `-` is unary. Tokens are then
grouped by their brackets into a tree (`_Node`), turned into a layout
document (`doc.py`), printed, and trailing comments aligned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, is_dataclass

from ..compiler import ast_nodes as ast
from ..compiler.lexer import Lexer, TokenType
from ..compiler.parser import Parser
from .doc import BREAK_PARENT, HARDLINE, LINE, SOFTLINE, Group, Indent, LineSuffix, contains_hard_break, print_doc


@dataclass
class FormatOptions:
    """Layout settings. Built-in defaults for now; a `[format]` section in
    `mah-project.toml` can fill this in later."""

    line_width: int = 100
    indent: int = 4
    max_blank_lines: int = 1


class FormatError(Exception):
    """The source can't be formatted: a syntax error, or (a formatter bug)
    output that fails verification. The message is user-facing."""


# -- tokens and trivia -------------------------------------------------------


@dataclass
class _Tok:
    type: TokenType
    text: str
    start: int
    end: int
    index: int


@dataclass
class _Comment:
    text: str
    blank_lines_before: int


@dataclass
class _Trivia:
    """What sits in the gap before a token."""

    trailing: str | None  # a comment on the previous token's line
    leading: list  # list[_Comment], each on its own line
    blank_lines_before: int  # blank lines between the last comment (or previous token) and the token
    has_newline: bool


def _line_col(text: str, offset: int) -> str:
    line = text.count("\n", 0, offset) + 1
    col = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return f"{line}:{col}"


def _lex(text: str) -> list:
    lexer = Lexer(text)
    toks = []
    while True:
        try:
            tok = lexer.get_next_token()
        except SyntaxError as exc:
            raise FormatError(f"syntax error: {exc}") from None
        if tok.type is TokenType.EOF:
            return toks
        toks.append(_Tok(tok.type, tok.literal, tok.position, tok.position + len(tok.literal), len(toks)))


def _blank_lines(gap: str) -> int:
    return max(0, gap.count("\n") - 1)


def _trivia(text: str, toks: list) -> list:
    """One `_Trivia` per token, plus one for the gap before end of file."""
    result = []
    for i in range(len(toks) + 1):
        start = toks[i - 1].end if i > 0 else 0
        end = toks[i].start if i < len(toks) else len(text)
        gap = text[start:end]
        pieces = re.split(r"(#[^\n]*)", gap)
        segments, comments = pieces[0::2], pieces[1::2]
        trailing = None
        leading = []
        for k, comment in enumerate(comments):
            if k == 0 and i > 0 and "\n" not in segments[0]:
                trailing = comment.rstrip()
            else:
                leading.append(_Comment(comment.rstrip(), _blank_lines(segments[k])))
        result.append(_Trivia(trailing, leading, _blank_lines(segments[-1]), "\n" in gap))
    return result


def _comment_texts(trivia: list) -> list:
    texts = []
    for entry in trivia:
        if entry.trailing is not None:
            texts.append(entry.trailing)
        texts.extend(c.text for c in entry.leading)
    return texts


# -- import/export: preprocessor syntax the parser can't read ----------------


def _blank_module_syntax(text: str, toks: list):
    """Replace `import ...` directives and `export` keywords with spaces, so
    the parser can read the rest with every position unchanged. Returns the
    blanked text, the indices of import-directive and export tokens (each
    starts a top-level statement), and the indices of blanked tokens."""
    chars = list(text)
    starts = set()
    blanked = set()

    def blank(first: int, last: int) -> None:
        for tok in toks[first : last + 1]:
            blanked.add(tok.index)
            for pos in range(tok.start, tok.end):
                chars[pos] = " "

    n = len(toks)
    i = 0
    while i < n:
        tok = toks[i]
        after_dot = i > 0 and toks[i - 1].type is TokenType.DOT
        if tok.type is TokenType.ID and tok.text == "import" and not after_dot:
            end = None
            if i + 1 < n and toks[i + 1].type is TokenType.STRING:
                end = i + 1
            elif (
                i + 3 < n
                and toks[i + 1].type is TokenType.ID
                and toks[i + 2].type is TokenType.ID
                and toks[i + 2].text == "from"
                and toks[i + 3].type is TokenType.STRING
            ):
                end = i + 3
            if end is not None:
                if end + 1 < n and toks[end + 1].type is TokenType.SEMICOLON:
                    end += 1
                blank(i, end)
                starts.add(i)
                i = end + 1
                continue
        if tok.type is TokenType.ID and tok.text == "export" and not after_dot and i + 1 < n:
            following = toks[i + 1].type
            if following in (TokenType.FN, TokenType.LET):
                blank(i, i)
                starts.add(i)
            elif following is TokenType.ID:
                end = i + 1
                if end + 1 < n and toks[end + 1].type is TokenType.SEMICOLON:
                    end += 1
                blank(i, end)
                starts.add(i)
                i = end + 1
                continue
        i += 1
    return "".join(chars), starts, blanked


def _parse(text: str, original: str) -> list:
    parser = Parser(Lexer(text))
    try:
        program = parser.parse_program()
    except SyntaxError as exc:
        raise FormatError(f"syntax error: {exc}") from None
    if parser.errors:
        message, position = parser.errors[0]
        cited = re.search(r"at position '?(\d+)'?", message)
        offset = int(cited.group(1)) if cited else position
        message = re.sub(r" ?at position '?\d+'?", "", message)
        raise FormatError(f"syntax error at {_line_col(original, offset)}: {message}")
    return program


# -- what the AST tells us about tokens --------------------------------------


def _walk(node):
    if is_dataclass(node) and not isinstance(node, type):
        yield node
        for f in fields(node):
            yield from _walk(getattr(node, f.name))
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)


def _is_position_field(name: str) -> bool:
    return name == "position" or name.endswith("_position") or name.endswith("_positions")


def _positions(node):
    """Every source position recorded anywhere in `node`'s subtree."""
    for sub in _walk(node):
        for f in fields(sub):
            if not _is_position_field(f.name):
                continue
            value = getattr(sub, f.name)
            stack = [value]
            while stack:
                item = stack.pop()
                if isinstance(item, int) and not isinstance(item, bool):
                    yield item
                elif isinstance(item, (list, tuple)):
                    stack.extend(item)


def _normalized(node):
    """An AST as nested tuples, positions dropped -- for comparing the
    parse of the input with the parse of the output."""
    if is_dataclass(node) and not isinstance(node, type):
        parts = []
        for f in fields(node):
            if _is_position_field(f.name):
                continue
            value = getattr(node, f.name)
            if f.name == "kwargs":
                # (name, value, the name token's position)
                value = [(name, expr) for name, expr, _position in value]
            parts.append((f.name, _normalized(value)))
        return (type(node).__name__, tuple(parts))
    if isinstance(node, (list, tuple)):
        return tuple(_normalized(item) for item in node)
    return node


@dataclass
class _Facts:
    statement_starts: set = field(default_factory=set)
    brace_kinds: dict = field(default_factory=dict)  # `{` index -> "block" | "match" | "body"
    generic_opens: set = field(default_factory=set)
    unary: set = field(default_factory=set)
    # `if` token index -> the `{` index of the chain's last block. The
    # blocks of one if/elif/else chain are inline or broken together.
    if_chains: dict = field(default_factory=dict)
    chain_blocks: set = field(default_factory=set)


def _facts(program: list, toks: list, module_starts: set) -> _Facts:
    by_pos = {tok.start: tok.index for tok in toks}
    facts = _Facts()

    def index_of(pos):
        return by_pos.get(pos)

    def first_index(node):
        positions = list(_positions(node))
        if not positions:
            return None
        idx = index_of(min(positions))
        # Parentheses have no AST node, so `(g)()` records nothing for its
        # first `(`. A `(` right before a statement's first recorded token
        # can only be part of that statement.
        while idx is not None and idx > 0 and toks[idx - 1].type is TokenType.PAREN_OPEN:
            idx -= 1
        return idx

    def next_brace(after_index):
        for tok in toks[after_index + 1 :]:
            if tok.type is TokenType.BRACE_OPEN:
                return tok.index
        return None

    def add_start(node):
        idx = first_index(node)
        if idx is not None:
            facts.statement_starts.add(idx)

    for stmt in program:
        idx = first_index(stmt)
        if idx is None:
            continue
        if idx - 1 in module_starts:  # `export fn ...`: the statement starts at `export`
            idx -= 1
        facts.statement_starts.add(idx)
    facts.statement_starts |= module_starts

    for node in _walk(program):
        if isinstance(node, ast.Block):
            idx = index_of(node.position)
            # Only a block written with braces has statements of its own:
            # the parser also makes brace-less blocks around a `defer`
            # statement or a `detach` operand, whose contents are part of
            # the enclosing statement.
            if idx is None or toks[idx].type is not TokenType.BRACE_OPEN:
                continue
            facts.brace_kinds.setdefault(idx, "block")
            for stmt in [*node.stmts, *([node.tail] if node.tail is not None else [])]:
                start = first_index(stmt)
                if start is not None and start > idx:
                    facts.statement_starts.add(start)
        elif isinstance(node, ast.MatchStmt):
            last = index_of(max(_positions(node.scrutinee)))
            brace = next_brace(last) if last is not None else None
            if brace is not None:
                facts.brace_kinds[brace] = "match"
            for arm in node.arms:
                add_start(arm.pattern)
        elif isinstance(node, (ast.ImplDecl, ast.TraitDecl)):
            brace = next_brace(index_of(node.position))
            if brace is not None:
                facts.brace_kinds[brace] = "body"
            for method in node.methods:
                facts.statement_starts.add(index_of(method.position))
        elif isinstance(node, ast.IfStmt):
            blocks = [node.then, *(block for _cond, block in node.elifs)]
            if node.else_ is not None:
                blocks.append(node.else_)
            opens = [index_of(block.position) for block in blocks]
            if_idx = index_of(node.position)
            if if_idx is not None and None not in opens:
                facts.if_chains[if_idx] = opens[-1]
                facts.chain_blocks.update(opens)
        elif isinstance(node, ast.Unary):
            facts.unary.add(index_of(node.position))
        elif isinstance(node, ast.NumberLit) and node.value < 0:
            facts.unary.add(index_of(node.position))  # a negative literal pattern: `-1`
        elif isinstance(node, ast.NamedType) and node.args:
            facts.generic_opens.add(index_of(node.position) + 1)
        if isinstance(node, ast.ImplDecl):
            if node.type_args:
                facts.generic_opens.add(index_of(node.type_name_position) + 1)
            if node.trait_args:
                facts.generic_opens.add(index_of(node.trait_name_position) + 1)
        type_params = getattr(node, "type_params", None)
        if type_params and isinstance(type_params, list) and isinstance(type_params[0], ast.TypeParam):
            facts.generic_opens.add(index_of(type_params[0].position) - 1)
    facts.statement_starts.discard(None)
    facts.unary.discard(None)
    return facts


# -- the bracket tree --------------------------------------------------------


@dataclass
class _Node:
    open: _Tok
    kind: str  # "paren" | "bracket" | "angle" | "brace" | "block" | "match" | "body"
    children: list = field(default_factory=list)
    close: _Tok | None = None


_CLOSERS = {
    "paren": TokenType.PAREN_CLOSE,
    "bracket": TokenType.BRACKET_CLOSE,
    "angle": TokenType.GT,
}


def _tree(toks: list, facts: _Facts) -> list:
    root: list = []
    stack: list = []
    for tok in toks:
        children = stack[-1].children if stack else root
        kind = None
        if tok.type is TokenType.PAREN_OPEN:
            kind = "paren"
        elif tok.type is TokenType.BRACKET_OPEN:
            kind = "bracket"
        elif tok.type is TokenType.BRACE_OPEN:
            kind = facts.brace_kinds.get(tok.index, "brace")
        elif tok.type is TokenType.LT and tok.index in facts.generic_opens:
            kind = "angle"
        if kind is not None:
            node = _Node(tok, kind)
            children.append(node)
            stack.append(node)
            continue
        if stack:
            top = stack[-1]
            expected = _CLOSERS.get(top.kind, TokenType.BRACE_CLOSE)
            if tok.type is expected:
                top.close = tok
                stack.pop()
                continue
        children.append(tok)
    if stack:
        raise FormatError("internal formatter error: unbalanced brackets")
    return root


def _first_tok(element) -> _Tok:
    return element.open if isinstance(element, _Node) else element


def _last_tok(element) -> _Tok:
    return element.close if isinstance(element, _Node) else element


# -- building the layout document --------------------------------------------

_CALL_LIKE = {
    TokenType.PRINT,
    TokenType.SIN,
    TokenType.COS,
    TokenType.INPUT,
    TokenType.SLEEP_ASYNC,
    TokenType.SOME,
    TokenType.FN,
}
_INDEXABLE_END = {
    TokenType.ID,
    TokenType.PAREN_CLOSE,
    TokenType.BRACKET_CLOSE,
    TokenType.BRACE_CLOSE,
    TokenType.STRING,
}
_TIGHT_BEFORE = {TokenType.COMMA, TokenType.SEMICOLON, TokenType.DOT, TokenType.COLON}
_RANGE_OPS = {TokenType.DOTDOT, TokenType.DOTDOT_EQ}


_MERGE_CACHE: dict = {}


def _would_merge(a: _Tok, b: _Tok) -> bool:
    """Whether printing `a` and `b` with nothing between them would lex as
    something other than those two tokens."""
    key = (a.type, a.text, b.type, b.text)
    if key not in _MERGE_CACHE:
        try:
            joined = [(t.type, t.text) for t in _lex(a.text + b.text)]
        except FormatError:
            joined = None
        _MERGE_CACHE[key] = joined != [(a.type, a.text), (b.type, b.text)]
    return _MERGE_CACHE[key]


class _Builder:
    def __init__(self, text: str, toks: list, trivia: list, facts: _Facts, options: FormatOptions):
        self.text = text
        self.toks = toks
        self.trivia = trivia
        self.facts = facts
        self.options = options
        self.consumed = set()  # tokens whose leading comments a caller already placed

    # spacing

    def _space(self, left, right) -> bool:
        wanted = self._wanted_space(left, right)
        if not wanted and _would_merge(_last_tok(left), _first_tok(right)):
            return True  # `100.. =>` must not become `100..=>`
        return wanted

    def _wanted_space(self, left, right) -> bool:
        a, b = _last_tok(left), _first_tok(right)
        if b.type in _TIGHT_BEFORE or a.type is TokenType.DOT:
            return False
        if a.type in _RANGE_OPS or b.type in _RANGE_OPS:
            return False
        if a.type is TokenType.BANG or a.index in self.facts.unary:
            return False
        if isinstance(right, _Node):
            if right.kind == "angle":
                return False
            if right.kind == "paren":
                after_generic = isinstance(left, _Node) and left.kind == "angle"
                # Within one statement, `(` after a value is a call (a new
                # statement would start on its own line anyway).
                return not (a.type in _INDEXABLE_END or a.type in _CALL_LIKE or after_generic)
            if right.kind == "bracket":
                same_line = "\n" not in self.text[a.end : b.start]
                return not (a.type in _INDEXABLE_END and same_line)
        return True

    # comments

    def _blank(self, count: int) -> list:
        return [HARDLINE] * min(count, self.options.max_blank_lines)

    def _leading_comment_lines(self, index: int) -> list:
        """A token's own-line comments as `[comment, HARDLINE, ...]`, for a
        caller that has just started a new line."""
        self.consumed.add(index)
        parts = []
        for k, comment in enumerate(self.trivia[index].leading):
            if k > 0:
                parts += self._blank(comment.blank_lines_before)
            parts += [comment.text, HARDLINE]
        if parts:
            parts += self._blank(self.trivia[index].blank_lines_before)
        return parts

    def _token(self, tok: _Tok, with_trailing: bool = True) -> list:
        parts = []
        leading = self.trivia[tok.index].leading
        if leading and tok.index not in self.consumed:
            # A comment in the middle of an expression: on its own line.
            parts.append(BREAK_PARENT)
            for comment in leading:
                parts += [HARDLINE, comment.text]
            parts.append(HARDLINE)
        parts.append(tok.text)
        if with_trailing:
            parts += self._trailing(tok)
        return parts

    def _trailing(self, tok: _Tok) -> list:
        trailing = self.trivia[tok.index + 1].trailing
        if trailing is None:
            return []
        return [LineSuffix(trailing), BREAK_PARENT]

    def _has_comments(self, first: int, last: int) -> bool:
        """Whether any comment sits between token `first` and token `last`
        (exclusive of the gap before `first`)."""
        for i in range(first + 1, last + 1):
            entry = self.trivia[i]
            if entry.trailing is not None or entry.leading:
                return True
        return False

    # elements

    def element(self, element) -> list:
        if isinstance(element, _Node):
            if element.kind in ("block", "match", "body"):
                return self._block(element)
            return self._list(element)
        return self._token(element)

    def sequence(self, elements: list, chain_at_start: bool = False) -> list:
        """Elements laid out on one line, spaced. An if/elif/else chain
        among them becomes one group, so its blocks break together
        (`chain_at_start`: the caller already made that group for a chain
        starting at `elements[0]`)."""
        parts = []
        k = 0
        while k < len(elements):
            element = elements[k]
            if k > 0 and self._space(elements[k - 1], element):
                parts.append(" ")
            chain_end = None if (k == 0 and chain_at_start) else self._if_chain_end(elements, k)
            if chain_end is not None:
                parts.append(Group(self.sequence(elements[k : chain_end + 1], chain_at_start=True)))
                k = chain_end + 1
                continue
            parts += self.element(element)
            k += 1
        return parts

    def _if_chain_end(self, elements: list, k: int):
        """If `elements[k]` is an `if` token, the index in `elements` of its
        chain's last block."""
        element = elements[k]
        if not isinstance(element, _Tok) or element.index not in self.facts.if_chains:
            return None
        last_open = self.facts.if_chains[element.index]
        for j in range(k + 1, len(elements)):
            if isinstance(elements[j], _Node) and elements[j].open.index == last_open:
                return j
        return None

    def _split_items(self, children: list) -> list:
        items, current = [], []
        for child in children:
            current.append(child)
            if isinstance(child, _Tok) and child.type is TokenType.COMMA:
                items.append(current)
                current = []
        if current:
            items.append(current)
        return items

    def _split_statements(self, children: list) -> list:
        statements, current = [], []
        for child in children:
            if current and _first_tok(child).index in self.facts.statement_starts:
                statements.append(current)
                current = []
            current.append(child)
        if current:
            statements.append(current)
        return statements

    def _closer_comments(self, node: _Node) -> list:
        """Own-line comments right before a closing bracket, indented with
        the contents: `[HARDLINE, comment, ...]`."""
        self.consumed.add(node.close.index)
        parts = []
        for k, comment in enumerate(self.trivia[node.close.index].leading):
            parts.append(HARDLINE)
            if k > 0:
                parts += self._blank(comment.blank_lines_before)
            parts.append(comment.text)
        return parts

    def _list(self, node: _Node) -> list:
        opener = self._token(node.open)
        items = self._split_items(node.children)
        closer_comments = self._closer_comments(node)
        # A comment after the closing bracket belongs to the line the group
        # ends on, so it must not force the group itself to break.
        closer = self._token(node.close, with_trailing=False)
        after = self._trailing(node.close)
        if not items:
            if closer_comments:
                return [*opener, Indent(closer_comments), HARDLINE, *closer, *after]
            return [*opener, " " if node.kind == "brace" else "", *closer, *after]
        item_docs = []
        for item in items:
            first = _first_tok(item[0]).index
            item_docs.append([*self._leading_comment_lines(first), *self.sequence(item)])
        if (
            node.kind == "paren"
            and not closer_comments
            and contains_hard_break(item_docs[-1])
            and not any(contains_hard_break(d) for d in item_docs[:-1])
        ):
            # Only the last argument spans lines (a closure, a block, a
            # broken literal): hug it, `f(a, fn(x) {` ... `})`, instead of
            # putting every argument on its own line.
            parts = [*opener]
            for k, item_doc in enumerate(item_docs):
                if k > 0:
                    parts.append(" ")
                parts += item_doc
            return [*parts, *closer, *after]
        edge = LINE if node.kind == "brace" else SOFTLINE
        body = [edge]
        for k, item_doc in enumerate(item_docs):
            if k > 0:
                body.append(LINE)
            body += item_doc
        body += closer_comments
        after_open = self.trivia[node.open.index + 1]
        keep_broken = node.kind in ("brace", "bracket") and after_open.has_newline
        return [Group([*opener, Indent(body), edge, *closer], broken=keep_broken), *after]

    def statements(self, statements: list, allow_leading_blank: bool) -> list:
        parts = []
        for k, statement in enumerate(statements):
            first = _first_tok(statement[0]).index
            entry = self.trivia[first]
            blank = entry.leading[0].blank_lines_before if entry.leading else entry.blank_lines_before
            if k > 0:
                parts.append(HARDLINE)
                parts += self._blank(blank)
            elif allow_leading_blank:
                parts += self._blank(blank)
            parts += self._leading_comment_lines(first)
            parts += self.sequence(statement)
        return parts

    def _block(self, node: _Node) -> list:
        opener = self._token(node.open)
        statements = self._split_statements(node.children)
        closer_comments = self._closer_comments(node)
        closer = self._token(node.close, with_trailing=False)
        after = self._trailing(node.close)
        if not statements:
            if closer_comments:
                return [*opener, Indent(closer_comments), HARDLINE, *closer, *after]
            return [*opener, " ", *closer, *after]
        one_line = "\n" not in self.text[node.open.start : node.close.end]
        if (
            node.kind in ("block", "match")
            and len(statements) == 1
            and one_line
            and not self._has_comments(node.open.index, node.close.index)
        ):
            inline = [*opener, Indent([LINE, *self.statements(statements, False)]), LINE, *closer]
            if node.open.index in self.facts.chain_blocks:
                return [*inline, *after]  # the whole if/elif/else chain is the group
            return [Group(inline), *after]
        body = [HARDLINE, *self.statements(statements, False), *closer_comments]
        return [*opener, Indent(body), HARDLINE, *closer, *after]

    def program(self, root: list) -> list:
        parts = self.statements(self._split_statements(root), False)
        end = self.trivia[len(self.toks)]
        for k, comment in enumerate(end.leading):
            if parts or k > 0:
                parts.append(HARDLINE)
                parts += self._blank(comment.blank_lines_before)
            parts.append(comment.text)
        return parts


def _align_trailing_comments(lines: list) -> list:
    """Lines as text. A run of consecutive lines that all end in a trailing
    comment gets the comments lined up one space after the longest code."""
    out = []
    k = 0
    while k < len(lines):
        if lines[k].comment is None or not lines[k].code:
            out.append(lines[k].code if lines[k].comment is None else lines[k].comment)
            k += 1
            continue
        run_end = k
        while run_end < len(lines) and lines[run_end].comment is not None and lines[run_end].code:
            run_end += 1
        column = max(len(line.code) for line in lines[k:run_end]) + 1
        for line in lines[k:run_end]:
            out.append(line.code.ljust(column) + line.comment)
        k = run_end
    return out


def _render(text: str, options: FormatOptions):
    toks = _lex(text)
    trivia = _trivia(text, toks)
    parse_text, module_starts, _blanked = _blank_module_syntax(text, toks)
    program = _parse(parse_text, text)
    facts = _facts(program, toks, module_starts)
    root = _tree(toks, facts)
    doc = _Builder(text, toks, trivia, facts, options).program(root)
    lines = print_doc(doc, options.line_width, " " * options.indent)
    out = _align_trailing_comments(lines)
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    formatted = "\n".join(out) + "\n" if out else ""
    return formatted, toks, trivia, program


def format_source(text: str, options: FormatOptions | None = None) -> str:
    """Format Mah source. Raises `FormatError` for a syntax error, or if the
    result fails verification (a formatter bug; the input is then left
    alone by every caller)."""
    options = options or FormatOptions()
    formatted, toks, trivia, program = _render(text, options)
    _verify(text, formatted, toks, trivia, program)
    return formatted


def _verify(original: str, formatted: str, toks: list, trivia: list, program: list) -> None:
    def fail(what: str):
        raise FormatError(f"internal formatter error ({what}); the file was left unchanged")

    new_toks = _lex(formatted)
    if [(t.type, t.text) for t in new_toks] != [(t.type, t.text) for t in toks]:
        fail("tokens changed")
    if _comment_texts(_trivia(formatted, new_toks)) != _comment_texts(trivia):
        fail("comments changed")
    parse_text, _starts, _blanked = _blank_module_syntax(formatted, new_toks)
    try:
        new_program = _parse(parse_text, formatted)
    except FormatError:
        fail("output doesn't parse")
    if _normalized(new_program) != _normalized(program):
        fail("meaning changed")
