; Cartograph extraction queries -- TypeScript / TSX.
; Loaded *in addition to* javascript.scm (see indexer/languages.py), so this
; file only carries the type-level syntax that TS adds.

; --- definitions -----------------------------------------------------------

(interface_declaration) @def.interface
(type_alias_declaration) @def.type
(enum_declaration) @def.enum
(abstract_class_declaration) @def.class

; Ambient / overload declarations: no body, but they are the public contract
; an agent most often needs to read.
(function_signature) @def.function
(method_signature) @def.method

; --- references ------------------------------------------------------------

; `interface I extends J`
(extends_type_clause type: [(type_identifier) (generic_type) (nested_type_identifier)] @ref.inherits)

; `class K implements I`
(implements_clause [(type_identifier) (generic_type) (nested_type_identifier)] @ref.inherits)

; `class K extends Base` -- TS wraps the supertype in an extends_clause, unlike JS.
(extends_clause value: [(identifier) (member_expression)] @ref.inherits)
