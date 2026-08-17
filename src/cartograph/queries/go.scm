; Cartograph extraction queries -- Go.
; See python.scm for the capture contract.

; --- definitions -----------------------------------------------------------

(function_declaration) @def.function
(method_declaration) @def.method

; Ordered most-specific-first; extract.py keeps the most specific kind when a
; node is captured by several patterns.
(type_spec type: (struct_type)) @def.struct
(type_spec type: (interface_type)) @def.interface
(type_spec) @def.type
(type_alias) @def.type

(const_spec) @def.const

; --- references ------------------------------------------------------------

(call_expression function: [(identifier) (selector_expression)] @ref.calls)

; Struct literals (`Foo{...}`) are Go's closest thing to instantiation.
(composite_literal type: [(type_identifier) (qualified_type)] @ref.instantiates)

; An embedded type in a struct/interface is Go's composition-as-inheritance.
(field_declaration type: (type_identifier) !name) @ref.inherits

; --- imports ---------------------------------------------------------------

(import_declaration) @import
