import sys
from os import error
from pathlib import Path

from compiler.ir_generator import IRGenerator
from compiler.lexer import Lexer, Token
from compiler.parser import Parser


class NonQuoteStr(str):
    def __repr__(self):
        return str(self)


file_name = sys.argv[1]

ROOT_DIR = Path(__file__).parent
TEMPLATE_DIR = ROOT_DIR / "templates"

DEST_DIR = Path(".") if len(sys.argv) < 3 else Path(sys.argv[2])
with open(TEMPLATE_DIR / "lexer.pyt") as f:
    lexer_template = f.read()
with open(TEMPLATE_DIR / "parser.pyt") as f:
    parser_template = f.read()
with open(TEMPLATE_DIR / "ir_generator.pyt") as f:
    ir_template = f.read()

with open(file_name) as f:
    input_string = f.read()

used_tokens = set()

lexer = Lexer(input_string)
parser = Parser(lexer)
ir = IRGenerator(parser)

current_token_name = ""
current_token_string = ""
is_current_token_ignored = False

tokens = []

current_rule_name = ""
current_rule_result = []
current_rule_results = []
start_symbol = ""

grammar = {}


@ir.action("token_read_init")
def _(_: Token):

    pass


@ir.action("token_name")
def _(name: Token):
    global current_token_name
    current_token_name = name.literal


@ir.action("token_rule")
def _(rule: Token):
    global current_token_string
    current_token_string = rule.literal


@ir.action("token_ignore_flag")
def _(_: Token):
    global is_current_token_ignored
    is_current_token_ignored = True


@ir.action("save_token")
def _(_: Token):
    global current_token_name
    global current_token_string
    global is_current_token_ignored
    tokens.append((current_token_name, current_token_string, is_current_token_ignored))
    current_token_name = ""
    current_token_string = ""
    is_current_token_ignored = False


@ir.action("token_read_end")
def _(_: Token):
    pass


@ir.action("grammar_read_init")
def _(_: Token):
    pass


@ir.action("grammar_name")
def _(name: Token):
    global current_rule_name
    global start_symbol
    if not start_symbol:
        start_symbol = name.literal
    current_rule_name = name.literal


@ir.action("rule_variable")
def _(var: Token):
    if var.literal == "#e":
        return
    if var.literal.startswith("#"):
        used_tokens.add(var.literal[1:])
        current_rule_result.append(NonQuoteStr("TokenType." + var.literal[1:]))
    else:
        current_rule_result.append(var.literal)


@ir.action("next_rule_result")
def _(_: Token):
    global current_rule_result
    current_rule_results.append(current_rule_result)
    current_rule_result = []


@ir.action("save_rule")
def _(_: Token):
    global current_rule_result
    global current_rule_results
    global current_rule_name
    current_rule_results.append(current_rule_result)
    current_rule_result = []

    grammar[current_rule_name] = current_rule_results
    current_rule_results = []
    current_rule_name = ""


@ir.action("grammar_read_end")
def _(_: Token):
    pass


ir.generate()


token_types = "\n    ".join(
    [f"{token_name} = '{token_name}'" for (token_name, _, _) in tokens]
)
lexer_template = lexer_template.replace(
    "{% TOKEN_TYPES %}",
    f"    {token_types}",
)
token_rules = "\n    ".join(
    [
        f"TokenType.{token_name} : r{token_rule},"
        for (token_name, token_rule, _) in tokens
    ]
)
lexer_template = lexer_template.replace(
    "{% TOKEN_RULES %}",
    f"    {token_rules}",
)
ignored_tokens_string = "\n    ".join(
    [f"TokenType.{token_name}," for (token_name, _, is_ignored) in tokens if is_ignored]
)
lexer_template = lexer_template.replace(
    "{% IGNORED_TOKENS %}",
    f"    {ignored_tokens_string}",
)

parser_template = parser_template.replace(
    "{% GRAMMAR_RULES %}",
    f"{str(grammar).replace("#", "TokenType.")}",
)

first_table = {}


def find_first(variable: str, checked: set) -> tuple[set, bool]:
    res = set()
    rules = grammar[variable]
    has_lambda = False
    for rule in rules:
        rule = rule.copy()
        while True:
            while len(rule) and rule[0].startswith("@"):
                rule.pop(0)
            if not rule:
                has_lambda = True
                break
            if isinstance(rule[0], NonQuoteStr):
                res.add(rule[0])
                break
            elif variable in checked:
                break
            else:
                child_variable = rule.pop(0)
                child_first, child_has_lambda = find_first(
                    child_variable, checked | {variable}
                )
                res = res | child_first
                if not child_has_lambda:
                    break
    return res, has_lambda


def find_follows(variable: str, checked: set) -> set:
    res = set()
    if variable == start_symbol:
        res.add(NonQuoteStr("TokenType.EOF"))

    for rule_key, rules in grammar.items():
        for rule_list in rules:
            for index, rule in enumerate(rule_list):
                if rule == variable:
                    to_check = rule_list[index + 1 :]
                    while True:
                        while len(to_check) and to_check[0].startswith("@"):
                            to_check.pop(0)
                        if not to_check:
                            if variable in checked:
                                break
                            res = res | find_follows(rule_key, checked | {variable})
                            break
                        if isinstance(to_check[0], NonQuoteStr):
                            res.add(to_check[0])
                            break
                        else:
                            child_res, child_has_lambda = find_first(
                                to_check.pop(0), set()
                            )
                            res = res | child_res
                            if not child_has_lambda:
                                break
                            else:
                                continue

    return res


parsing_table = {}

for key, rules in grammar.items():
    key_res = set()
    parsing_table[key] = {}
    for index, rule_set in enumerate(rules):
        res = set()
        has_lambda = False
        for rule in rule_set:
            if rule.startswith("@"):
                continue
            elif isinstance(rule, NonQuoteStr):
                res.add(rule)
                break
            else:
                rule_first, rule_has_lambda = find_first(rule, set())
                has_lambda = rule_has_lambda
                res = res | rule_first
                if not rule_has_lambda:
                    break
        else:
            has_lambda = True
        if len(res & key_res):
            raise Exception("Error: the grammar is not LL(1)", res & key_res, key)
        key_res = key_res | res
        for item in res:
            parsing_table[key][item] = NonQuoteStr(f'RULES["{key}"][{index}]')
        if has_lambda:
            res = find_follows(key, set())
            for item in res:
                parsing_table[key][item] = NonQuoteStr(f'RULES["{key}"][{index}]')
            if len(res.intersection(key_res)):
                raise Exception(
                    "Error: the grammar is not LL(1)", key, res.intersection(key_res)
                )
        key_res = key_res | res

parser_template = parser_template.replace(
    "{% PARSING_TABLE %}",
    f"{str(parsing_table)}",
)
parser_template = parser_template.replace(
    "{% START_SYMBOL %}",
    f'"{start_symbol}"',
)

defined_tokens = set(map(lambda x: x[0], filter(lambda x: not x[2], tokens)))
undefined_tokens = used_tokens - defined_tokens
if undefined_tokens:
    raise Exception(f"Undefined Token[s] '{', '.join(undefined_tokens)}'")
unused_tokens = defined_tokens - used_tokens
if unused_tokens:
    print(f"Warning: Unused Token[s] '{', '.join(unused_tokens)}'")

COMPILER_DIR = DEST_DIR / "compiler"

try:
    COMPILER_DIR.mkdir()
except Exception:
    ...
(COMPILER_DIR / "__init__.py").touch()
with open(COMPILER_DIR / "lexer.py", "w") as f:
    f.write(lexer_template)
with open(COMPILER_DIR / "parser.py", "w") as f:
    f.write(parser_template)
with open(COMPILER_DIR / "ir_generator.py", "w") as f:
    f.write(ir_template)
