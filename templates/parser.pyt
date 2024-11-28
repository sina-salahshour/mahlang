from .lexer import TokenType, Lexer, Token

RULES = {
{% GRAMMAR_RULES %}}

PARSE_TABLE = {
{% PARSING_TABLE %}
}

START_SYMBOL = {% START_SYMBOL %}


class Parser:
    table = PARSE_TABLE

    def __init__(self, lexer: Lexer) -> None:
        self.lexer = lexer
        self.actions = {}

    def parse(self):
        stack = [START_SYMBOL, TokenType.EOF]
        current_token = self.lexer.get_next_token()

        while stack:
            top: str | Token = stack.pop(0)

            if isinstance(top, str) and top.startswith("@"):
                self.consume_action(top, current_token)
            elif top == current_token:
                current_token = self.lexer.get_next_token()
            elif top in self.table:
                production = self.table[top].get(current_token.type)
                if production is None:
                    raise SyntaxError(
                        f"Invalid Token {current_token} at position {current_token.position}"
                    )
                stack[:0] = production
            else:
                raise SyntaxError(
                    f"Invalid syntax '{current_token}' at position '{current_token.position}'"
                )

        if current_token != TokenType.EOF:
            raise SyntaxError(
                "Invalid input file. input did not finish after parsing"
            )

    def consume_action(self, action: str, current_token):
        action_fn = self.actions.get(action)
        if not action_fn:
            raise SystemError(f"Action not found '{action}' at position {current_token.position}")

        action_fn(current_token)

    def register_actions(self, name, action):
        self.actions[name] = action
