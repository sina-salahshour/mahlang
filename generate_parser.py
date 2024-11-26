import os
import sys

file_name = sys.argv[1]


with open("./templates/lexer.pyt") as f:
    lexer_template = f.read()
with open("./templates/parser.pyt") as f:
    parser_template = f.read()
with open("./templates/ir_generator.pyt") as f:
    ir_template = f.read()

with open(file_name) as f:
    input_string = f.read()

raw_tokens, raw_grammar = input_string.split("\n--\n")

tokens = []
separator_tokens = []

for token_line in raw_tokens.split("\n"):
    is_separator = token_line.startswith("!")
    if is_separator:
        token_line = token_line[1:]
    token_name, _, token_rule = token_line.partition(":")
    tokens.append((token_name.strip(), token_rule.strip()))
    if is_separator:
        separator_tokens.append((token_name.strip(), token_rule.strip()))

token_types = "\n    ".join(
    [f"{token_name} = '{token_name}'" for token_name, _ in tokens]
)
lexer_template = lexer_template.replace(
    "{% TOKEN_TYPES %}",
    f"    {token_types}",
)
token_rules = "\n    ".join(
    [f"TokenType.{token_name} : r{token_rule}," for token_name, token_rule in tokens]
)
lexer_template = lexer_template.replace(
    "{% TOKEN_RULES %}",
    f"    {token_rules}",
)
token_separators = "\n    ".join(
    [f"TOKEN_RULES[TokenType.{token_name}]," for token_name, _ in separator_tokens]
)
lexer_template = lexer_template.replace(
    "{% TOKEN_SEPARATORS %}",
    f"    {token_separators}",
)

# --

grammar = {}
grammar_str = ""
start_symbol = None


def to_token_type(grammar_token: str) -> str:
    return f"TokenType.{grammar_token[1:]}"


def to_literal_str(grammar_var: str) -> str:
    return f'"{grammar_var.strip()}"'


for grammar_line in raw_grammar.strip().split("\n"):
    grammar_name, raw_rules = grammar_line.split("->")
    grammar_name = grammar_name.strip()
    if not start_symbol:
        start_symbol = grammar_name
    rules = []
    rules_list = []

    for raw_rule in raw_rules.split("|"):
        rule_list = list(
            map(
                lambda var: (
                    to_token_type(var) if var.startswith("#") else to_literal_str(var)
                ),
                filter(
                    lambda var: var != "#e",
                    raw_rule.strip().split(),
                ),
            )
        )

        rule = ", ".join(rule_list)
        rules.append(f"[{rule}]")
        rules_list.append(rule_list)
    grammar_str += f'    "{grammar_name}": [{", ".join(rules)}], \n'
    grammar[grammar_name] = rules_list

parser_template = parser_template.replace(
    "{% GRAMMAR_RULES %}",
    f"{grammar_str}",
)

first_table = {}


def find_first(variable: str, checked: set) -> tuple[set, bool]:
    res = set()
    rules = grammar[variable]
    has_lambda = False
    for rule in rules:
        rule = rule.copy()
        while True:
            while len(rule) and rule[0].startswith('"@'):
                rule.pop(0)
            if not rule:
                has_lambda = True
                break
            if not rule[0].startswith('"'):
                res.add(rule[0])
                break
            elif variable in checked:
                break
            else:
                child_variable = rule.pop(0)[1:-1]
                child_first, child_has_lambda = find_first(
                    child_variable, checked | {variable}
                )
                if not child_has_lambda:
                    res = res | child_first
                    break
                else:
                    continue
    return res, has_lambda


def find_follows(variable: str, checked: set) -> set:
    res = set()
    if variable == start_symbol:
        res.add("TokenType.EOF")

    for rule_key, rules in grammar.items():
        for rule_list in rules:
            for index, rule in enumerate(rule_list):
                if rule[1:-1] == variable:
                    to_check = rule_list[index + 1 :]
                    while True:
                        while len(to_check) and to_check[0].startswith('"@'):
                            to_check.pop(0)
                        if not to_check:
                            if variable in checked:
                                break
                            res = res | find_follows(rule_key, checked | {variable})
                            break
                        if not to_check[0].startswith('"'):
                            res.add(to_check[0])
                            break
                        else:
                            child_res, child_has_lambda = find_first(
                                to_check.pop()[1:-1], set()
                            )
                            res = res | child_res
                            if not child_has_lambda:
                                break
                            else:
                                continue

    return res


parsing_table = {}

# TokenType.IDENTIFIER: RULES["E"][0],
for key, rules in grammar.items():
    parsing_table[key] = {}
    for index, rule_set in enumerate(rules):
        res = set()
        has_lambda = False
        for rule in rule_set:
            if rule.startswith('"@'):
                continue
            elif not rule.startswith('"'):
                res.add(rule)
                break
            else:
                rule_first, rule_has_lambda = find_first(rule[1:-1], set())
                has_lambda = rule_has_lambda
                res = res | rule_first
                if not rule_has_lambda:
                    break
        else:
            has_lambda = True
        for item in res:
            parsing_table[key][item] = f'RULES["{key}"][{index}]'
        if has_lambda:
            res = find_follows(key, set())
            for item in res:
                parsing_table[key][item] = f"[]"

parsing_table_string = ""
for key, fields in parsing_table.items():
    field_string = ""
    for terminal, field in fields.items():
        field_string += f"    {terminal}: {field},\n"
    field_string = "{\n" + field_string + "}"
    parsing_table_string += f'"{key}":{field_string}, \n'

parser_template = parser_template.replace(
    "{% PARSING_TABLE %}",
    f"{parsing_table_string}",
)
parser_template = parser_template.replace(
    "{% START_SYMBOL %}",
    f'"{start_symbol}"',
)

try:
    os.mkdir("compiler")
except Exception:
    ...

with open("./compiler/lexer.py", "w") as f:
    f.write(lexer_template)
with open("./compiler/parser.py", "w") as f:
    f.write(parser_template)
with open("./compiler/ir_generator.py", "w") as f:
    f.write(ir_template)
with open("./compiler/__init__.py", "w") as f:
    ...
