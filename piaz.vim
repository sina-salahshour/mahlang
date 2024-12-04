if exists("b:current_syntax")
  finish
endif

syntax clear

syntax keyword piazKeyword if elif else while let break continue return def

syntax match piazOperator /[+\-*\/%&|!<>]=\?/
syntax keyword piazOperator print input
syntax match piazOperator /[=]/

syntax match piazDelimiter /[(){},]/

syntax match piazFunction /\$\w\+/
syntax match piazIdentifier /\<[a-zA-Z_]\w*\>/

syntax match piazNumber /\d\+/

syn match   piazComment	"#.*$"

highlight link piazKeyword Keyword
highlight link piazOperator Operator
highlight link piazDelimiter Delimiter
highlight link piazFunction Function
highlight link piazIdentifier Identifier
highlight link piazNumber Number
highlight link piazComment Comment

let b:current_syntax = "piaz"
