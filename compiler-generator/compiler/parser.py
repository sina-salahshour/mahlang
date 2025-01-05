from .lexer import TokenType, Lexer, Token

RULES = {'Lang': [['@token_read_init', 'Tokens', '@token_read_end', TokenType.SEP, '@grammar_read_init', 'Grammar', '@grammar_read_end']], 'Tokens': [['Token', TokenType.SEMICOLON, 'Tokens'], []], 'Token': [['TokenModifiers', '@token_name', TokenType.ID, TokenType.COLON, '@token_rule', TokenType.STRING, '@save_token']], 'TokenModifiers': [['@token_ignore_flag', TokenType.TOKEN_IGNORE_FLAG], []], 'Grammar': [['Rule', TokenType.SEMICOLON, 'Grammar'], []], 'Rule': [['@grammar_name', TokenType.ID, TokenType.TRANSITION, 'SingleRuleResult', 'RuleResult', '@save_rule']], 'RuleResult': [['@next_rule_result', TokenType.PIPE, 'SingleRuleResult', 'RuleResult'], []], 'SingleRuleResult': [['VariableCombination', 'VariableCombinations']], 'VariableCombinations': [['VariableCombination', 'VariableCombinations'], []], 'VariableCombination': [['@rule_variable', TokenType.EPSILON], ['@rule_variable', TokenType.TOKEN_NAME], ['@rule_variable', TokenType.ID], ['@rule_variable', TokenType.ACTION]]}

PARSE_TABLE = {'Lang': {TokenType.ID: RULES["Lang"][0], TokenType.TOKEN_IGNORE_FLAG: RULES["Lang"][0], TokenType.SEP: RULES["Lang"][0], TokenType.EOF: RULES["Lang"][0]}, 'Tokens': {TokenType.ID: RULES["Tokens"][0], TokenType.TOKEN_IGNORE_FLAG: RULES["Tokens"][0], TokenType.SEP: RULES["Tokens"][1]}, 'Token': {TokenType.ID: RULES["Token"][0], TokenType.TOKEN_IGNORE_FLAG: RULES["Token"][0], TokenType.SEMICOLON: RULES["Token"][0]}, 'TokenModifiers': {TokenType.TOKEN_IGNORE_FLAG: RULES["TokenModifiers"][0], TokenType.ID: RULES["TokenModifiers"][1]}, 'Grammar': {TokenType.ID: RULES["Grammar"][0], TokenType.EOF: RULES["Grammar"][1]}, 'Rule': {TokenType.ID: RULES["Rule"][0]}, 'RuleResult': {TokenType.PIPE: RULES["RuleResult"][0], TokenType.SEMICOLON: RULES["RuleResult"][1]}, 'SingleRuleResult': {TokenType.ID: RULES["SingleRuleResult"][0], TokenType.EPSILON: RULES["SingleRuleResult"][0], TokenType.TOKEN_NAME: RULES["SingleRuleResult"][0], TokenType.ACTION: RULES["SingleRuleResult"][0]}, 'VariableCombinations': {TokenType.ID: RULES["VariableCombinations"][0], TokenType.EPSILON: RULES["VariableCombinations"][0], TokenType.TOKEN_NAME: RULES["VariableCombinations"][0], TokenType.ACTION: RULES["VariableCombinations"][0], TokenType.SEMICOLON: RULES["VariableCombinations"][1], TokenType.PIPE: RULES["VariableCombinations"][1]}, 'VariableCombination': {TokenType.EPSILON: RULES["VariableCombination"][0], TokenType.TOKEN_NAME: RULES["VariableCombination"][1], TokenType.ID: RULES["VariableCombination"][2], TokenType.ACTION: RULES["VariableCombination"][3]}}

START_SYMBOL = "Lang"


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
