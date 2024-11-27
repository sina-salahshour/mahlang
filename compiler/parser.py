from .lexer import TokenType, Lexer, Token

RULES = {
    "Start": [["Stmt", "Stmts", "@nop"]], 
    "Stmts": [["Stmt", "Stmts"], []], 
    "Stmt": [["Assign"], ["If"], ["While"], [TokenType.BREAK, "@break"], [TokenType.CONTINUE, "@continue"]], 
    "CodeBlock": [[TokenType.BRACE_OPEN, "Stmts", TokenType.BRACE_CLOSE]], 
    "If": [[TokenType.IF, "E", "@save", "CodeBlock", "@jmpfalse", "Elif"]], 
    "Elif": [[TokenType.ELIF, "E", "@save", "CodeBlock", "@jmpfalse", "Elif"], ["Else"]], 
    "Else": [[TokenType.ELSE, "@save", "CodeBlock", "@jmp"], []], 
    "While": [[TokenType.WHILE, "@save", "@save", "E", "@save", "CodeBlock", "@jmpwhile"]], 
    "Assign": [["G", TokenType.ASSIGN, "E", "@assign"]], 
    "E": [["T", "E'"]], 
    "E'": [[TokenType.OP_ADD, "T", "@add", "E'"], []], 
    "T": [["F", "T'"]], 
    "T'": [[TokenType.OP_MUL, "F", "@mul", "T'"], []], 
    "F": [[TokenType.PAREN_OPEN, "E", TokenType.PAREN_CLOSE], ["@pid", TokenType.IDENTIFIER], ["@num", TokenType.NUMBER]], 
    "G": [["@setpid", TokenType.IDENTIFIER]], 
}

PARSE_TABLE = {
"Start":{
    TokenType.IDENTIFIER: RULES["Start"][0],
    TokenType.BREAK: RULES["Start"][0],
    TokenType.CONTINUE: RULES["Start"][0],
    TokenType.WHILE: RULES["Start"][0],
    TokenType.IF: RULES["Start"][0],
}, 
"Stmts":{
    TokenType.IDENTIFIER: RULES["Stmts"][0],
    TokenType.BREAK: RULES["Stmts"][0],
    TokenType.CONTINUE: RULES["Stmts"][0],
    TokenType.WHILE: RULES["Stmts"][0],
    TokenType.IF: RULES["Stmts"][0],
    TokenType.EOF: RULES["Stmts"][1],
    TokenType.BRACE_CLOSE: RULES["Stmts"][1],
}, 
"Stmt":{
    TokenType.IDENTIFIER: RULES["Stmt"][0],
    TokenType.IF: RULES["Stmt"][1],
    TokenType.WHILE: RULES["Stmt"][2],
    TokenType.BREAK: RULES["Stmt"][3],
    TokenType.CONTINUE: RULES["Stmt"][4],
}, 
"CodeBlock":{
    TokenType.BRACE_OPEN: RULES["CodeBlock"][0],
}, 
"If":{
    TokenType.IF: RULES["If"][0],
}, 
"Elif":{
    TokenType.ELIF: RULES["Elif"][0],
    TokenType.ELSE: RULES["Elif"][1],
    TokenType.IDENTIFIER: RULES["Elif"][1],
    TokenType.BRACE_CLOSE: RULES["Elif"][1],
    TokenType.CONTINUE: RULES["Elif"][1],
    TokenType.EOF: RULES["Elif"][1],
    TokenType.WHILE: RULES["Elif"][1],
    TokenType.BREAK: RULES["Elif"][1],
    TokenType.IF: RULES["Elif"][1],
}, 
"Else":{
    TokenType.ELSE: RULES["Else"][0],
    TokenType.IDENTIFIER: RULES["Else"][1],
    TokenType.BRACE_CLOSE: RULES["Else"][1],
    TokenType.CONTINUE: RULES["Else"][1],
    TokenType.EOF: RULES["Else"][1],
    TokenType.WHILE: RULES["Else"][1],
    TokenType.BREAK: RULES["Else"][1],
    TokenType.IF: RULES["Else"][1],
}, 
"While":{
    TokenType.WHILE: RULES["While"][0],
}, 
"Assign":{
    TokenType.IDENTIFIER: RULES["Assign"][0],
}, 
"E":{
    TokenType.NUMBER: RULES["E"][0],
    TokenType.IDENTIFIER: RULES["E"][0],
    TokenType.PAREN_OPEN: RULES["E"][0],
}, 
"E'":{
    TokenType.OP_ADD: RULES["E'"][0],
    TokenType.IDENTIFIER: RULES["E'"][1],
    TokenType.WHILE: RULES["E'"][1],
    TokenType.BREAK: RULES["E'"][1],
    TokenType.IF: RULES["E'"][1],
    TokenType.BRACE_OPEN: RULES["E'"][1],
    TokenType.BRACE_CLOSE: RULES["E'"][1],
    TokenType.EOF: RULES["E'"][1],
    TokenType.CONTINUE: RULES["E'"][1],
    TokenType.PAREN_CLOSE: RULES["E'"][1],
}, 
"T":{
    TokenType.NUMBER: RULES["T"][0],
    TokenType.IDENTIFIER: RULES["T"][0],
    TokenType.PAREN_OPEN: RULES["T"][0],
}, 
"T'":{
    TokenType.OP_MUL: RULES["T'"][0],
    TokenType.IDENTIFIER: RULES["T'"][1],
    TokenType.BRACE_CLOSE: RULES["T'"][1],
    TokenType.WHILE: RULES["T'"][1],
    TokenType.IF: RULES["T'"][1],
    TokenType.BREAK: RULES["T'"][1],
    TokenType.BRACE_OPEN: RULES["T'"][1],
    TokenType.CONTINUE: RULES["T'"][1],
    TokenType.EOF: RULES["T'"][1],
    TokenType.PAREN_CLOSE: RULES["T'"][1],
    TokenType.OP_ADD: RULES["T'"][1],
}, 
"F":{
    TokenType.PAREN_OPEN: RULES["F"][0],
    TokenType.IDENTIFIER: RULES["F"][1],
    TokenType.NUMBER: RULES["F"][2],
}, 
"G":{
    TokenType.IDENTIFIER: RULES["G"][0],
}, 

}

START_SYMBOL = "Start"


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
