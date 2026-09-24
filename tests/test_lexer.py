"""Unit tests for compiler/lexer.py. See docs/TESTING.md."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.compiler.lexer import Lexer, TokenType


def token_types(source: str) -> list:
    lexer = Lexer(source)
    types = []
    while True:
        tok = lexer.get_next_token()
        types.append(tok.type)
        if tok.type is TokenType.EOF:
            break
    return types


class LexerTests(unittest.TestCase):
    def test_keywords_vs_identifiers(self):
        self.assertEqual(
            token_types("let fn if elif else while break continue return true false"),
            [
                TokenType.LET, TokenType.FN, TokenType.IF, TokenType.ELIF,
                TokenType.ELSE, TokenType.WHILE, TokenType.BREAK,
                TokenType.CONTINUE, TokenType.RETURN, TokenType.TRUE,
                TokenType.FALSE, TokenType.EOF,
            ],
        )
        self.assertEqual(token_types("letter"), [TokenType.ID, TokenType.EOF])
        self.assertEqual(token_types("fnord"), [TokenType.ID, TokenType.EOF])

    def test_two_char_operators_are_not_split(self):
        self.assertEqual(
            token_types("** // == !="),
            [TokenType.POW, TokenType.TRUEDIV, TokenType.EQ, TokenType.NEQ, TokenType.EOF],
        )

    def test_number_with_and_without_fraction(self):
        lexer = Lexer("42 3.14")
        first = lexer.get_next_token()
        second = lexer.get_next_token()
        self.assertEqual((first.type, first.literal), (TokenType.NUMBER, "42"))
        self.assertEqual((second.type, second.literal), (TokenType.NUMBER, "3.14"))

    def test_string_literal_keeps_escapes_raw(self):
        lexer = Lexer(r'"a\nb"')
        tok = lexer.get_next_token()
        self.assertEqual(tok.type, TokenType.STRING)
        self.assertEqual(tok.literal, r'"a\nb"')  # unescaping happens in the parser

    def test_comments_and_whitespace_are_skipped(self):
        self.assertEqual(
            token_types("  # a comment\n  let  # trailing\n  x"),
            [TokenType.LET, TokenType.ID, TokenType.EOF],
        )

    def test_and_or_are_single_ascii_chars(self):
        self.assertEqual(token_types("& |"), [TokenType.AND, TokenType.OR, TokenType.EOF])

    def test_invalid_character_raises(self):
        with self.assertRaises(SyntaxError):
            token_types("@")

    def test_trait_impl_for_are_keywords_self_stays_a_plain_id(self):
        # M12: `self`/`Self` deliberately stay plain `ID`s (see
        # ast_nodes.py's M12 note) -- only `trait`/`impl`/`for` become
        # reserved keyword tokens.
        self.assertEqual(
            token_types("trait impl for self"),
            [TokenType.TRAIT, TokenType.IMPL, TokenType.FOR, TokenType.ID, TokenType.EOF],
        )


if __name__ == "__main__":
    unittest.main()
