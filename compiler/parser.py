from .lexer import TokenType, Lexer, Token

RULES = {
    "Start": [["Stmt", "Stmts", "@nop"]], 
    "Stmts": [["Stmt", "Stmts"], []], 
    "Stmt": [["Declare"], ["If"], ["While"], [TokenType.BREAK, "@break"], [TokenType.CONTINUE, "@continue"], ["Assign"], ["Print"]], 
    "CodeBlock": [[TokenType.BRACE_OPEN, "Stmts", TokenType.BRACE_CLOSE]], 
    "If": [[TokenType.IF, "Exp", "@save", "CodeBlock", "@jmpfalse", "Elif"]], 
    "Elif": [["@omit", TokenType.ELIF, "Exp", "@save", "CodeBlock", "@jmpfalse", "Elif"], ["Else", "@omit"]], 
    "Else": [[TokenType.ELSE, "@save", "CodeBlock", "@jmptrue"], []], 
    "While": [[TokenType.WHILE, "@save", "@save", "Exp", "@save", "CodeBlock", "@jmpwhile"]], 
    "Declare": [[TokenType.LET, "G", TokenType.ASSIGN, "Exp", "@assign"]], 
    "Assign": [["Id", TokenType.ASSIGN, "Exp", "@assign"]], 
    "Print": [["@init_call", TokenType.PRINT, TokenType.PAREN_OPEN, "Args", TokenType.PAREN_CLOSE, "@call"]], 
    "Args": [["Exp", "ArgsRest"], []], 
    "ArgsRest": [[TokenType.COMA, "Args"], []], 
    "Exp": [["Unary"]], 
    "Unary": [["Cond", "Unary'"]], 
    "Unary'": [[TokenType.AND, "Cond", "@and", "Unary'"], [TokenType.OR, "Cond", "@or", "Unary'"], []], 
    "Cond": [["Add", "Cond'"]], 
    "Cond'": [[TokenType.EQ, "Add", "@eq", "Cond'"], [TokenType.NEQ, "Add", "@neq", "Cond'"], [TokenType.LT, "Add", "@lt", "Cond'"], [TokenType.GT, "Add", "@gt", "Cond'"], []], 
    "Add": [["Mul", "Add'"]], 
    "Add'": [[TokenType.OP_ADD, "Mul", "@add", "Add'"], [TokenType.OP_SUB, "Mul", "@sub", "Add'"], []], 
    "Mul": [["Atom", "Mul'"]], 
    "Mul'": [[TokenType.OP_MUL, "Atom", "@mul", "Mul'"], [TokenType.OP_DIV, "Atom", "@div", "Mul'"], []], 
    "Atom": [[TokenType.PAREN_OPEN, "Exp", TokenType.PAREN_CLOSE], ["Id"], ["@num", TokenType.NUMBER]], 
    "G": [["@setpid", TokenType.IDENTIFIER]], 
    "Id": [["@pid", TokenType.IDENTIFIER]], 
}

PARSE_TABLE = {
"Start":{
    TokenType.WHILE: RULES["Start"][0],
    TokenType.PRINT: RULES["Start"][0],
    TokenType.IF: RULES["Start"][0],
    TokenType.CONTINUE: RULES["Start"][0],
    TokenType.LET: RULES["Start"][0],
    TokenType.BREAK: RULES["Start"][0],
    TokenType.IDENTIFIER: RULES["Start"][0],
}, 
"Stmts":{
    TokenType.WHILE: RULES["Stmts"][0],
    TokenType.PRINT: RULES["Stmts"][0],
    TokenType.IF: RULES["Stmts"][0],
    TokenType.CONTINUE: RULES["Stmts"][0],
    TokenType.LET: RULES["Stmts"][0],
    TokenType.BREAK: RULES["Stmts"][0],
    TokenType.IDENTIFIER: RULES["Stmts"][0],
    TokenType.BRACE_CLOSE: RULES["Stmts"][1],
    TokenType.EOF: RULES["Stmts"][1],
}, 
"Stmt":{
    TokenType.LET: RULES["Stmt"][0],
    TokenType.IF: RULES["Stmt"][1],
    TokenType.WHILE: RULES["Stmt"][2],
    TokenType.BREAK: RULES["Stmt"][3],
    TokenType.CONTINUE: RULES["Stmt"][4],
    TokenType.IDENTIFIER: RULES["Stmt"][5],
    TokenType.PRINT: RULES["Stmt"][6],
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
    TokenType.WHILE: RULES["Elif"][1],
    TokenType.BRACE_CLOSE: RULES["Elif"][1],
    TokenType.IF: RULES["Elif"][1],
    TokenType.EOF: RULES["Elif"][1],
    TokenType.PRINT: RULES["Elif"][1],
    TokenType.CONTINUE: RULES["Elif"][1],
    TokenType.LET: RULES["Elif"][1],
    TokenType.BREAK: RULES["Elif"][1],
    TokenType.IDENTIFIER: RULES["Elif"][1],
}, 
"Else":{
    TokenType.ELSE: RULES["Else"][0],
    TokenType.WHILE: RULES["Else"][1],
    TokenType.BRACE_CLOSE: RULES["Else"][1],
    TokenType.IF: RULES["Else"][1],
    TokenType.EOF: RULES["Else"][1],
    TokenType.PRINT: RULES["Else"][1],
    TokenType.CONTINUE: RULES["Else"][1],
    TokenType.LET: RULES["Else"][1],
    TokenType.BREAK: RULES["Else"][1],
    TokenType.IDENTIFIER: RULES["Else"][1],
}, 
"While":{
    TokenType.WHILE: RULES["While"][0],
}, 
"Declare":{
    TokenType.LET: RULES["Declare"][0],
}, 
"Assign":{
    TokenType.IDENTIFIER: RULES["Assign"][0],
}, 
"Print":{
    TokenType.PRINT: RULES["Print"][0],
}, 
"Args":{
    TokenType.PAREN_OPEN: RULES["Args"][0],
    TokenType.NUMBER: RULES["Args"][0],
    TokenType.IDENTIFIER: RULES["Args"][0],
    TokenType.PAREN_CLOSE: RULES["Args"][1],
}, 
"ArgsRest":{
    TokenType.COMA: RULES["ArgsRest"][0],
    TokenType.PAREN_CLOSE: RULES["ArgsRest"][1],
}, 
"Exp":{
    TokenType.PAREN_OPEN: RULES["Exp"][0],
    TokenType.NUMBER: RULES["Exp"][0],
    TokenType.IDENTIFIER: RULES["Exp"][0],
}, 
"Unary":{
    TokenType.PAREN_OPEN: RULES["Unary"][0],
    TokenType.NUMBER: RULES["Unary"][0],
    TokenType.IDENTIFIER: RULES["Unary"][0],
}, 
"Unary'":{
    TokenType.AND: RULES["Unary'"][0],
    TokenType.OR: RULES["Unary'"][1],
    TokenType.BRACE_OPEN: RULES["Unary'"][2],
    TokenType.WHILE: RULES["Unary'"][2],
    TokenType.COMA: RULES["Unary'"][2],
    TokenType.PAREN_CLOSE: RULES["Unary'"][2],
    TokenType.BRACE_CLOSE: RULES["Unary'"][2],
    TokenType.IF: RULES["Unary'"][2],
    TokenType.EOF: RULES["Unary'"][2],
    TokenType.PRINT: RULES["Unary'"][2],
    TokenType.CONTINUE: RULES["Unary'"][2],
    TokenType.LET: RULES["Unary'"][2],
    TokenType.BREAK: RULES["Unary'"][2],
    TokenType.IDENTIFIER: RULES["Unary'"][2],
}, 
"Cond":{
    TokenType.PAREN_OPEN: RULES["Cond"][0],
    TokenType.NUMBER: RULES["Cond"][0],
    TokenType.IDENTIFIER: RULES["Cond"][0],
}, 
"Cond'":{
    TokenType.EQ: RULES["Cond'"][0],
    TokenType.NEQ: RULES["Cond'"][1],
    TokenType.LT: RULES["Cond'"][2],
    TokenType.GT: RULES["Cond'"][3],
    TokenType.BRACE_OPEN: RULES["Cond'"][4],
    TokenType.WHILE: RULES["Cond'"][4],
    TokenType.COMA: RULES["Cond'"][4],
    TokenType.PAREN_CLOSE: RULES["Cond'"][4],
    TokenType.OR: RULES["Cond'"][4],
    TokenType.BRACE_CLOSE: RULES["Cond'"][4],
    TokenType.IF: RULES["Cond'"][4],
    TokenType.EOF: RULES["Cond'"][4],
    TokenType.PRINT: RULES["Cond'"][4],
    TokenType.CONTINUE: RULES["Cond'"][4],
    TokenType.LET: RULES["Cond'"][4],
    TokenType.AND: RULES["Cond'"][4],
    TokenType.BREAK: RULES["Cond'"][4],
    TokenType.IDENTIFIER: RULES["Cond'"][4],
}, 
"Add":{
    TokenType.PAREN_OPEN: RULES["Add"][0],
    TokenType.NUMBER: RULES["Add"][0],
    TokenType.IDENTIFIER: RULES["Add"][0],
}, 
"Add'":{
    TokenType.OP_ADD: RULES["Add'"][0],
    TokenType.OP_SUB: RULES["Add'"][1],
    TokenType.BRACE_OPEN: RULES["Add'"][2],
    TokenType.WHILE: RULES["Add'"][2],
    TokenType.PAREN_CLOSE: RULES["Add'"][2],
    TokenType.IF: RULES["Add'"][2],
    TokenType.PRINT: RULES["Add'"][2],
    TokenType.EQ: RULES["Add'"][2],
    TokenType.CONTINUE: RULES["Add'"][2],
    TokenType.LET: RULES["Add'"][2],
    TokenType.NEQ: RULES["Add'"][2],
    TokenType.AND: RULES["Add'"][2],
    TokenType.IDENTIFIER: RULES["Add'"][2],
    TokenType.COMA: RULES["Add'"][2],
    TokenType.OR: RULES["Add'"][2],
    TokenType.LT: RULES["Add'"][2],
    TokenType.BRACE_CLOSE: RULES["Add'"][2],
    TokenType.EOF: RULES["Add'"][2],
    TokenType.GT: RULES["Add'"][2],
    TokenType.BREAK: RULES["Add'"][2],
}, 
"Mul":{
    TokenType.PAREN_OPEN: RULES["Mul"][0],
    TokenType.NUMBER: RULES["Mul"][0],
    TokenType.IDENTIFIER: RULES["Mul"][0],
}, 
"Mul'":{
    TokenType.OP_MUL: RULES["Mul'"][0],
    TokenType.OP_DIV: RULES["Mul'"][1],
    TokenType.BRACE_OPEN: RULES["Mul'"][2],
    TokenType.WHILE: RULES["Mul'"][2],
    TokenType.PAREN_CLOSE: RULES["Mul'"][2],
    TokenType.OP_ADD: RULES["Mul'"][2],
    TokenType.IF: RULES["Mul'"][2],
    TokenType.PRINT: RULES["Mul'"][2],
    TokenType.EQ: RULES["Mul'"][2],
    TokenType.CONTINUE: RULES["Mul'"][2],
    TokenType.LET: RULES["Mul'"][2],
    TokenType.NEQ: RULES["Mul'"][2],
    TokenType.AND: RULES["Mul'"][2],
    TokenType.IDENTIFIER: RULES["Mul'"][2],
    TokenType.COMA: RULES["Mul'"][2],
    TokenType.OR: RULES["Mul'"][2],
    TokenType.BRACE_CLOSE: RULES["Mul'"][2],
    TokenType.LT: RULES["Mul'"][2],
    TokenType.EOF: RULES["Mul'"][2],
    TokenType.GT: RULES["Mul'"][2],
    TokenType.OP_SUB: RULES["Mul'"][2],
    TokenType.BREAK: RULES["Mul'"][2],
}, 
"Atom":{
    TokenType.PAREN_OPEN: RULES["Atom"][0],
    TokenType.IDENTIFIER: RULES["Atom"][1],
    TokenType.NUMBER: RULES["Atom"][2],
}, 
"G":{
    TokenType.IDENTIFIER: RULES["G"][0],
}, 
"Id":{
    TokenType.IDENTIFIER: RULES["Id"][0],
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
