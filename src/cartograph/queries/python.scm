; Cartograph extraction queries -- Python
;
; Capture contract (shared by every language, see indexer/extract.py):
;   @def.<kind>        the *definition* node; its name comes from the `name`
;                      field, or from a language hook in extract.py
;   @ref.calls         the callee expression node of a call site
;   @ref.inherits      a supertype name node
;   @ref.instantiates  a constructor name node
;   @import            a whole import statement
;
; Enclosing scope is never encoded in the query: extract.py derives it from
; byte containment against the captured definitions, which keeps these files
; small and makes nesting (closures, methods, inner classes) work for free.

; --- definitions -----------------------------------------------------------

(function_definition) @def.function

(class_definition) @def.class

; Module-level SCREAMING_CASE assignments read as constants. extract.py filters
; on case so ordinary module-level locals don't pollute the graph.
;
; Note: tree-sitter-python >=0.26 makes a module-level assignment a *direct*
; child of (module); older grammars wrapped it in (expression_statement). Both
; shapes are matched so the query survives a grammar bump either way.
(module (assignment left: (identifier)) @def.const)
(module (expression_statement (assignment left: (identifier)) @def.const))

; --- references ------------------------------------------------------------

(call function: [(identifier) (attribute)] @ref.calls)

(class_definition
  superclasses: (argument_list [(identifier) (attribute)] @ref.inherits))

; --- imports ---------------------------------------------------------------

(import_statement) @import
(import_from_statement) @import
