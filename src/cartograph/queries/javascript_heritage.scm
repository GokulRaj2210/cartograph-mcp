; JavaScript-only: `class A extends B` puts the supertype directly under
; class_heritage. TypeScript wraps it in an extends_clause instead, so this
; pattern is *invalid* against the TS grammar and must stay in its own file.

(class_heritage [(identifier) (member_expression)] @ref.inherits)
