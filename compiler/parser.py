from .lexer import TokenType, Lexer, Token

RULES = {
    "E": [["T", "E'"]], 
    "E'": [[TokenType.OP_ADD, "T", "@add", "E'"], []], 
    "T": [["F", "T'"]], 
    "T'": [[TokenType.OP_MUL, "F", "@mul", "T'"], []], 
    "F": [[TokenType.PAREN_OPEN, "E", TokenType.PAREN_CLOSE], ["@pid", TokenType.IDENTIFIER]], 
}

PARSE_TABLE = {
"E":{
    TokenType.IDENTIFIER: RULES["E"][0],
    TokenType.PAREN_OPEN: RULES["E"][0],
}, 
"E'":{
    TokenType.OP_ADD: RULES["E'"][0],
    TokenType.PAREN_CLOSE: [],
    TokenType.EOF: [],
}, 
"T":{
    TokenType.IDENTIFIER: RULES["T"][0],
    TokenType.PAREN_OPEN: RULES["T"][0],
}, 
"T'":{
    TokenType.OP_MUL: RULES["T'"][0],
    TokenType.OP_ADD: [],
    TokenType.PAREN_CLOSE: [],
    TokenType.EOF: [],
}, 
"F":{
    TokenType.PAREN_OPEN: RULES["F"][0],
    TokenType.IDENTIFIER: RULES["F"][1],
}, 

}

START_SYMBOL = "E"


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
                raise SyntaxError(f"Invalid input file")

        if current_token != TokenType.EOF:
            raise SyntaxError("Invalid input file")

    def consume_action(self, action: str, current_token):
        action_fn = self.actions.get(action)
        if not action_fn:
            raise RuntimeError(f"Error: Action not found '{action}'")

        action_fn(current_token)

    def register_actions(self, name, action):
        self.actions[name] = action
