; Cartograph extraction queries -- JavaScript (also the base for TS/TSX).
; See python.scm for the capture contract.
;
; IMPORTANT: this file is compiled against the javascript, typescript *and* tsx
; grammars, so it may only contain patterns that are valid in all three. Class
; heritage is the notable divergence -- JS spells it `(class_heritage (identifier))`
; while TS wraps it as `(class_heritage (extends_clause value: ...))` -- so those
; patterns live in javascript_heritage.scm and typescript.scm respectively.
; tests/test_queries.py compiles every file against every grammar that loads it.

; --- definitions -----------------------------------------------------------

(function_declaration) @def.function
(generator_function_declaration) @def.function

(class_declaration) @def.class
(method_definition) @def.method

; `const handler = () => {}` and `const f = function () {}` are how most modern
; JS actually declares functions, so they must be first-class definitions.
(variable_declarator value: (arrow_function)) @def.function
(variable_declarator value: (function_expression)) @def.function

; --- references ------------------------------------------------------------

(call_expression function: [(identifier) (member_expression)] @ref.calls)

(new_expression constructor: [(identifier) (member_expression)] @ref.instantiates)

; --- imports ---------------------------------------------------------------

(import_statement) @import
(export_statement source: (string)) @import
