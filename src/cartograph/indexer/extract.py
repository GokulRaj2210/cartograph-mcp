"""Turn one source file into symbols, references and imports.

The extractor is language-agnostic. It knows only the capture contract declared
in ``queries/*.scm`` plus whatever the file's :class:`LanguageAdapter` tells it.

Scoping trick worth knowing about: instead of encoding "which function am I in"
into the queries (which explodes combinatorially across closures, methods, inner
classes and arrow functions), we index every captured definition node by its
tree-sitter node id and then walk a reference's ``parent`` chain until we hit
one. That is O(tree depth) per reference and correct by construction.
"""

from __future__ import annotations

import hashlib

from tree_sitter import Node, QueryCursor

from cartograph.indexer.languages import LanguageAdapter, _text
from cartograph.models import ParsedFile, ParsedImport, ParsedReference, ParsedSymbol, SymbolKind

#: Definitions of these kinds turn a nested function into a "method".
_CONTAINER_KINDS = frozenset({"class", "interface", "struct", "enum"})

#: When one node is captured by several patterns, the highest number wins.
_KIND_SPECIFICITY: dict[str, int] = {
    "type": 1,
    "const": 1,
    "function": 2,
    "method": 3,
    "enum": 4,
    "interface": 5,
    "struct": 5,
    "class": 5,
}

#: Callee names that can never be a repo symbol. Filtering them keeps the
#: unresolved-edge count honest instead of drowning it in `len` and `console.log`.
_NOISE_CALLEES = frozenset(
    {
        # python builtins
        "len",
        "print",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "range",
        "enumerate",
        "zip",
        "isinstance",
        "issubclass",
        "super",
        "getattr",
        "setattr",
        "hasattr",
        "open",
        "sorted",
        "sum",
        "min",
        "max",
        "abs",
        "any",
        "all",
        "map",
        "filter",
        "repr",
        "type",
        "format",
        "next",
        "iter",
        "bytes",
        "id",
        "round",
        "reversed",
        "hash",
        "vars",
        "dir",
        # js/ts + go stdlib-ish
        "require",
        "log",
        "warn",
        "error",
        "info",
        "debug",
        "push",
        "pop",
        "join",
        "split",
        "slice",
        "splice",
        "forEach",
        "reduce",
        "toString",
        "keys",
        "values",
        "entries",
        "then",
        "catch",
        "finally",
        "append",
        "make",
        "new",
        "panic",
        "recover",
        "Printf",
        "Println",
        "Sprintf",
        "Errorf",
        "String",
        "Error",
    }
)

_MAX_REFS_PER_FILE = 20_000


def extract_file(
    rel_path: str,
    source: bytes,
    adapter: LanguageAdapter,
) -> ParsedFile:
    """Parse ``source`` and return everything the graph needs from this file."""
    module = adapter.module_key(rel_path)
    parsed = ParsedFile(
        path=rel_path,
        lang=adapter.name,
        module=module,
        sha256=hashlib.sha256(source).hexdigest(),
        size=len(source),
        lines=source.count(b"\n") + 1 if source else 0,
        is_test=adapter.is_test_file(rel_path),
    )

    tree = adapter.parser().parse(source)
    defs, refs, imports = _run_queries(adapter, tree.root_node)

    # --- definitions ------------------------------------------------------
    # node id -> (kind, node); resolved to symbols with fully-qualified names.
    by_id: dict[int, tuple[str, Node]] = {}
    for kind, node in defs:
        prev = by_id.get(node.id)
        if prev is None or _KIND_SPECIFICITY.get(kind, 0) > _KIND_SPECIFICITY.get(prev[0], 0):
            by_id[node.id] = (kind, node)

    names: dict[int, str] = {}
    symbols: dict[int, ParsedSymbol] = {}
    for node_id, (_kind, node) in by_id.items():
        name = adapter.symbol_name(node, source)
        if not name:
            continue  # e.g. an anonymous default export, or a filtered constant
        names[node_id] = name

    for node_id, (kind, node) in by_id.items():
        name = names.get(node_id)
        if name is None:
            continue
        parent_id = _enclosing_def(node, by_id)
        parent_kind = by_id[parent_id][0] if parent_id is not None else None
        if kind == "function" and parent_kind in _CONTAINER_KINDS:
            kind = "method"
        chain = _qualname_chain(node, by_id, names)
        parent_local = ".".join(chain[:-1]) or None
        symbols[node_id] = ParsedSymbol(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=adapter.signature(node, source),
            docstring=adapter.docstring(node, source),
            parent=parent_local,
            exported=adapter.is_exported(name, node),
        )
    parsed.symbols = list(symbols.values())

    # --- references -------------------------------------------------------
    seen: set[tuple[str | None, str, str, int]] = set()
    for kind, node in refs[:_MAX_REFS_PER_FILE]:
        dst_name = _text(node, source).strip()
        if not dst_name or dst_name.rsplit(".", 1)[-1] in _NOISE_CALLEES:
            continue
        owner_id = _enclosing_def(node, by_id)
        src_local = None
        if owner_id is not None and owner_id in symbols:
            src_local = symbols[owner_id].local_qualname
        line = node.start_point[0] + 1
        key = (src_local, dst_name, kind, line)
        if key in seen:
            continue
        seen.add(key)
        parsed.references.append(
            ParsedReference(
                src_local_qualname=src_local,
                dst_name=_normalise_callee(dst_name),
                kind=kind,  # type: ignore[arg-type]
                line=line,
            )
        )

    # --- imports ----------------------------------------------------------
    for node in imports:
        for spec in adapter.parse_import(node, source):
            parsed.imports.append(
                ParsedImport(
                    raw=spec.raw,
                    module=spec.module,
                    symbol=spec.symbol,
                    alias=spec.alias,
                    line=node.start_point[0] + 1,
                )
            )

    return parsed


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _run_queries(
    adapter: LanguageAdapter, root: Node
) -> tuple[list[tuple[str, Node]], list[tuple[str, Node]], list[Node]]:
    defs: list[tuple[str, Node]] = []
    refs: list[tuple[str, Node]] = []
    imports: list[Node] = []

    for query in adapter.queries():
        captures = QueryCursor(query).captures(root)
        for capture_name, nodes in captures.items():
            head, _, tail = capture_name.partition(".")
            if head == "def":
                defs.extend((tail, node) for node in nodes)
            elif head == "ref":
                refs.extend((tail, node) for node in nodes)
            elif head == "import":
                imports.extend(nodes)
    return defs, refs, imports


def _enclosing_def(node: Node, by_id: dict[int, tuple[str, Node]]) -> int | None:
    """Nearest ancestor that is itself a captured definition."""
    cur = node.parent
    while cur is not None:
        if cur.id in by_id:
            return cur.id
        cur = cur.parent
    return None


def _qualname_chain(
    node: Node, by_id: dict[int, tuple[str, Node]], names: dict[int, str]
) -> list[str]:
    """["Outer", "Inner", "method"] for a nested definition."""
    chain: list[str] = []
    cur: Node | None = node
    while cur is not None:
        if cur.id in by_id and cur.id in names:
            chain.append(names[cur.id])
        cur = cur.parent
    chain.reverse()
    return chain


def _normalise_callee(text: str) -> str:
    """Strip generics/whitespace so `Foo<Bar>` and `pkg . Fn` match `Foo`/`pkg.Fn`."""
    cleaned = text.split("<", 1)[0]
    cleaned = "".join(cleaned.split())
    return cleaned.removesuffix("?").removeprefix("(").removesuffix(")")


def kind_is_container(kind: SymbolKind | str) -> bool:
    return kind in _CONTAINER_KINDS
