; Keywords
;
; Most keywords are anonymous tokens embedded in a multi-element rule
; (e.g. `while_stmt: seq("while", ...)`) and are queried as string
; literals below. A few (`break`, `continue`, `none`, `_`, `true`,
; `false`) are each a rule whose ENTIRE body is a single bare string --
; tree-sitter doesn't expose a separate anonymous token for those (the
; named node itself IS the leaf), so they must be queried by node type
; instead; referencing them as string literals is a query compile error.
"let" @keyword
"struct" @keyword
"enum" @keyword
"return" @keyword
"while" @keyword
"fn" @keyword
"if" @keyword
"elif" @keyword
"else" @keyword
"match" @keyword
"some" @keyword
"import" @keyword
"export" @keyword
"from" @keyword
"defer" @keyword
"detach" @keyword
(break_stmt) @keyword
(continue_stmt) @keyword
(none_expr) @keyword
(none_pattern) @keyword

; Booleans
(true) @boolean
(false) @boolean

; Built-in functions
"print" @function.builtin
"sin" @function.builtin
"cos" @function.builtin
"input" @function.builtin
"sleep_async" @function.builtin

; M10 (async): `.await` is not its own grammar rule (an ordinary
; `field_access` with field name "await" covers it structurally -- see
; grammar.js's `detach_expr` comment) so it can't be targeted by node type
; the way `none`/`_`/`true`/`false` are above; a query anchored on the
; literal field text isn't straightforward without a grammar change, so
; `.await` is left highlighted the same as any other field access rather
; than forcing one just for coloring.

; Operators
"+" @operator
"-" @operator
"*" @operator
"/" @operator
"//" @operator
"%" @operator
"**" @operator
"==" @operator
"!=" @operator
"=>" @operator
"<" @operator
">" @operator
"&" @operator
"|" @operator
"=" @operator

; Punctuation
;
; `;` (a statement separator, purely optional/no-op in Mah -- see
; compiler/parser.py's `_parse_block_items`) is declared as an `extras`
; token, not a grammar-rule token, so it produces no node the tree
; exposes at all and can't be queried/highlighted distinctly.
"." @punctuation.delimiter
":" @punctuation.delimiter
"," @punctuation.delimiter
"{" @punctuation.bracket
"}" @punctuation.bracket
"(" @punctuation.bracket
")" @punctuation.bracket

; Wildcard pattern
(wildcard_pattern) @variable.builtin

; Identifiers and literals -- the generic fallback. Deliberately listed
; BEFORE the more specific overrides below: per tree-sitter highlight
; query convention, when two patterns match the same node, the one that
; appears LATER in the file wins, so the specific captures (type,
; property, function) that follow correctly take precedence over this
; catch-all for the same identifier.
(identifier) @variable
(number) @number
(string) @string
(comment) @comment

; Type names -- struct/enum declarations and any reference to a
; struct/enum type name in a literal or pattern.
(struct_decl name: (identifier) @type)
(enum_decl name: (identifier) @type)
(struct_literal type: (identifier) @type)
(enum_literal type: (identifier) @type)
(struct_pattern type: (identifier) @type)
(enum_pattern type: (identifier) @type)
(enum_variant name: (identifier) @type)

; Enum/struct member (variant/field) names.
;
; `_field_init`/`_pattern_field` are hidden rules (leading `_`), so their
; own `name`/`value` fields are hoisted directly onto the enclosing
; struct_literal/struct_pattern/enum_literal/enum_pattern node rather than
; appearing on a node of their own -- query them there.
(enum_literal variant: (identifier) @property)
(enum_pattern variant: (identifier) @property)
(field_access field: (identifier) @property)
(struct_literal name: (identifier) @property)
(enum_literal name: (identifier) @property)
(struct_pattern name: (identifier) @property)
(enum_pattern name: (identifier) @property)

; Function names -- declarations and call sites.
(fn_stmt name: (identifier) @function)
(fn_expr name: (identifier) @function)
(call_expr function: (identifier) @function)
(call_expr function: (field_access field: (identifier) @function))
