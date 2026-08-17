"""Language adapters.

Everything a language needs to join the graph lives in one small subclass:
extension mapping, its ``.scm`` query files, how to read a definition's name and
docstring, how to spell a module key, and how to resolve an import to a file.
The indexer itself stays language-agnostic.

Adding a language is intentionally a ~40-line job -- see ``GoAdapter`` for the
shortest complete example.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from tree_sitter import Language, Node, Parser, Query
from tree_sitter_language_pack import get_language, get_parser

QUERY_DIR = Path(__file__).resolve().parent.parent / "queries"

_WS = re.compile(r"\s+")
_TEST_HINTS = ("test", "spec", "__tests__", "fixtures")


@dataclass(frozen=True, slots=True)
class ImportSpec:
    """A raw import, before it is resolved against the file set."""

    raw: str
    module: str
    symbol: str | None = None
    alias: str | None = None


class LanguageAdapter:
    """Base adapter. Defaults are chosen to be correct for C-family syntax."""

    name: ClassVar[str] = ""
    ts_name: ClassVar[str] = ""
    extensions: ClassVar[tuple[str, ...]] = ()
    query_files: ClassVar[tuple[str, ...]] = ()
    #: Filenames that mean "this directory is the module", e.g. __init__.py
    package_files: ClassVar[tuple[str, ...]] = ()
    #: Extensions tried when resolving an extensionless relative import.
    resolve_extensions: ClassVar[tuple[str, ...]] = ()
    #: Leading directories that are build layout, not module identity. Dropping
    #: them makes `src/cartograph/models.py` read as `cartograph.models` -- which
    #: is both what a human calls it *and* what `import cartograph.models`
    #: actually resolves to, so it improves import matching as well as legibility.
    #:
    #: Deliberately just `src`. `lib`, `app` and `packages` are *frequently real
    #: package names* (`src/app/core.py` is genuinely `app.core`), and stripping
    #: one silently breaks every import that names it -- a bug this set already
    #: caused once. Only widen this with a fixture that proves the case.
    source_roots: ClassVar[frozenset[str]] = frozenset({"src"})

    # -- tree-sitter plumbing ------------------------------------------------

    def parser(self) -> Parser:
        return get_parser(self.ts_name)

    def language(self) -> Language:
        return get_language(self.ts_name)

    def queries(self) -> list[Query]:
        return [_compile(self.ts_name, f) for f in self.query_files]

    # -- definitions ---------------------------------------------------------

    def symbol_name(self, node: Node, src: bytes) -> str | None:
        """Name of a captured definition node."""
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            return _text(name_node, src)
        return None

    def signature(self, node: Node, src: bytes, limit: int = 220) -> str | None:
        """Everything from the definition's start up to its body.

        Deliberately syntax-driven rather than grammar-driven: it works for
        `def f(x) -> int:`, `func (s *S) M() error {`, `async m(): Promise<T> {`
        and arrow functions without four bespoke implementations.
        """
        body = _body_node(node)
        end = body.start_byte if body is not None else node.end_byte
        raw = src[node.start_byte : end].decode("utf-8", "replace")
        sig = _WS.sub(" ", raw).strip().rstrip("{:=>").strip()
        if not sig:
            return None
        return sig if len(sig) <= limit else sig[: limit - 1] + "…"

    def docstring(self, node: Node, src: bytes) -> str | None:
        """Default: the comment block immediately above the definition."""
        return _leading_comment(node, src)

    def is_exported(self, name: str, node: Node) -> bool:
        return not name.startswith("_")

    # -- modules -------------------------------------------------------------

    def module_key(self, rel_path: str) -> str:
        """Language-native module identity for a repo-relative path."""
        p = Path(rel_path)
        p = p.parent if p.name in self.package_files else p.with_suffix("")
        return self._strip_source_root(p.parts)

    def _strip_source_root(self, parts: Sequence[str]) -> str:
        trimmed = list(parts)
        while len(trimmed) > 1 and trimmed[0] in self.source_roots:
            trimmed = trimmed[1:]
        return "/".join(trimmed)

    def parse_import(self, node: Node, src: bytes) -> list[ImportSpec]:
        raise NotImplementedError

    def resolve_import(
        self, spec: ImportSpec, importer: str, modules: dict[str, str]
    ) -> str | None:
        """Map an import to a repo-relative file path, or None if external.

        ``modules`` maps module key -> file path for every indexed file.
        """
        raise NotImplementedError

    # -- misc ----------------------------------------------------------------

    def is_test_file(self, rel_path: str) -> bool:
        lowered = rel_path.lower()
        stem = Path(lowered).stem
        return (
            any(part in _TEST_HINTS for part in Path(lowered).parts[:-1])
            or stem.startswith("test_")
            or stem.endswith(("_test", ".test", ".spec", "_spec"))
        )


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


class PythonAdapter(LanguageAdapter):
    name: ClassVar[str] = "python"
    ts_name: ClassVar[str] = "python"
    extensions: ClassVar[tuple[str, ...]] = (".py", ".pyi")
    query_files: ClassVar[tuple[str, ...]] = ("python.scm",)
    package_files: ClassVar[tuple[str, ...]] = ("__init__.py",)
    resolve_extensions: ClassVar[tuple[str, ...]] = (".py", ".pyi")

    def symbol_name(self, node: Node, src: bytes) -> str | None:
        if node.type == "assignment":  # module-level constant
            left = node.child_by_field_name("left")
            if left is None or left.type != "identifier":
                return None
            name = _text(left, src)
            # Only SCREAMING_CASE reads as a constant; skip ordinary globals.
            return name if name.isupper() and len(name) > 1 else None
        return super().symbol_name(node, src)

    def docstring(self, node: Node, src: bytes) -> str | None:
        body = node.child_by_field_name("body")
        if body is not None and body.named_child_count:
            first = body.named_children[0]
            # tree-sitter-python >=0.26 puts the docstring directly in the block;
            # older grammars wrapped it in an expression_statement.
            if first.type == "expression_statement" and first.named_child_count:
                first = first.named_children[0]
            if first.type == "string":
                return _clean_docstring(_text(first, src).strip("\"'"))
        return _leading_comment(node, src)

    def module_key(self, rel_path: str) -> str:
        return super().module_key(rel_path).replace("/", ".")

    def parse_import(self, node: Node, src: bytes) -> list[ImportSpec]:
        raw = _WS.sub(" ", _text(node, src)).strip()
        if node.type == "import_statement":
            out = []
            for child in node.named_children:
                if child.type == "dotted_name":
                    mod = _text(child, src)
                    out.append(ImportSpec(raw, mod, alias=mod.rsplit(".", 1)[-1]))
                elif child.type == "aliased_import":
                    target = child.child_by_field_name("name")
                    alias = child.child_by_field_name("alias")
                    if target is not None:
                        out.append(
                            ImportSpec(
                                raw,
                                _text(target, src),
                                alias=_text(alias, src) if alias else None,
                            )
                        )
            return out

        module_node = node.child_by_field_name("module_name")
        module = _text(module_node, src) if module_node is not None else ""
        names: list[ImportSpec] = []
        # Compare by node id, never by `is`: py-tree-sitter builds a fresh Node
        # wrapper on every access, so identity comparison always returns False
        # and the module itself leaks in as an imported name.
        module_id = module_node.id if module_node is not None else None
        for child in node.named_children:
            if child.id == module_id:
                continue
            if child.type == "dotted_name":
                sym = _text(child, src)
                names.append(ImportSpec(raw, module, symbol=sym, alias=sym))
            elif child.type == "aliased_import":
                target = child.child_by_field_name("name")
                alias = child.child_by_field_name("alias")
                if target is not None:
                    names.append(
                        ImportSpec(
                            raw,
                            module,
                            symbol=_text(target, src),
                            alias=_text(alias, src) if alias else None,
                        )
                    )
        return names or [ImportSpec(raw, module)]

    def resolve_import(
        self, spec: ImportSpec, importer: str, modules: dict[str, str]
    ) -> str | None:
        module = spec.module
        if module.startswith("."):
            base = self.module_key(importer).split(".")
            # `from . import x` sits in the package; each extra dot walks up one.
            up = len(module) - len(module.lstrip("."))
            base = base[: len(base) - up + 1] if up <= len(base) else []
            tail = module.lstrip(".")
            module = ".".join([*base, tail]) if tail else ".".join(base)

        # `from pkg import submodule` must beat `pkg` itself: the more specific
        # target is the one the importing file actually depends on, and pointing
        # at the package's __init__ would lose every edge into the submodule.
        if spec.symbol and f"{module}.{spec.symbol}" in modules:
            return modules[f"{module}.{spec.symbol}"]
        if module in modules:
            return modules[module]
        # src/ layouts: match on a unique dotted suffix.
        return _unique_suffix_match(module, modules, sep=".")


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------


class JavaScriptAdapter(LanguageAdapter):
    name: ClassVar[str] = "javascript"
    ts_name: ClassVar[str] = "javascript"
    extensions: ClassVar[tuple[str, ...]] = (".js", ".jsx", ".mjs", ".cjs")
    query_files: ClassVar[tuple[str, ...]] = ("javascript.scm", "javascript_heritage.scm")
    package_files: ClassVar[tuple[str, ...]] = (
        "index.js",
        "index.jsx",
        "index.mjs",
        "index.ts",
        "index.tsx",
    )
    resolve_extensions: ClassVar[tuple[str, ...]] = (
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".d.ts",
    )

    def symbol_name(self, node: Node, src: bytes) -> str | None:
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            return _text(name_node, src) if name_node is not None else None
        return super().symbol_name(node, src)

    def is_exported(self, name: str, node: Node) -> bool:
        cur: Node | None = node
        for _ in range(6):  # export wrappers are shallow
            if cur is None:
                break
            if cur.type in ("export_statement", "export_clause"):
                return True
            cur = cur.parent
        return not name.startswith("_")

    def parse_import(self, node: Node, src: bytes) -> list[ImportSpec]:
        raw = _WS.sub(" ", _text(node, src)).strip()
        source = node.child_by_field_name("source")
        if source is None:
            return []
        module = _text(source, src).strip("\"'`")
        specs: list[ImportSpec] = []
        for named in _descendants(node, {"import_specifier"}):
            target = named.child_by_field_name("name")
            alias = named.child_by_field_name("alias")
            if target is not None:
                specs.append(
                    ImportSpec(
                        raw,
                        module,
                        symbol=_text(target, src),
                        alias=_text(alias, src) if alias else None,
                    )
                )
        for default in _descendants(node, {"namespace_import"}):
            ident = default.named_children[-1] if default.named_child_count else None
            if ident is not None:
                specs.append(ImportSpec(raw, module, alias=_text(ident, src)))
        if not specs:
            clause = node.child_by_field_name("import_clause") or node
            for ident in clause.named_children:
                if ident.type == "identifier":
                    specs.append(ImportSpec(raw, module, alias=_text(ident, src)))
        return specs or [ImportSpec(raw, module)]

    def resolve_import(
        self, spec: ImportSpec, importer: str, modules: dict[str, str]
    ) -> str | None:
        module = spec.module
        if module.startswith("."):
            target = (Path(importer).parent / module).as_posix()
            target = _normalise_path(target)
        elif module.startswith(("@/", "~/", "#/")):
            target = module[2:]  # common bundler alias for the source root
        else:
            return None  # bare specifier => node_modules

        for candidate in (target, *(f"{target}/index" for _ in (0,))):
            if candidate in modules:
                return modules[candidate]
        return _unique_suffix_match(target, modules, sep="/")


class TypeScriptAdapter(JavaScriptAdapter):
    name: ClassVar[str] = "typescript"
    ts_name: ClassVar[str] = "typescript"
    extensions: ClassVar[tuple[str, ...]] = (".ts", ".mts", ".cts")
    query_files: ClassVar[tuple[str, ...]] = ("javascript.scm", "typescript.scm")


class TsxAdapter(TypeScriptAdapter):
    name: ClassVar[str] = "tsx"
    ts_name: ClassVar[str] = "tsx"
    extensions: ClassVar[tuple[str, ...]] = (".tsx",)


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


class GoAdapter(LanguageAdapter):
    name: ClassVar[str] = "go"
    ts_name: ClassVar[str] = "go"
    extensions: ClassVar[tuple[str, ...]] = (".go",)
    query_files: ClassVar[tuple[str, ...]] = ("go.scm",)

    def symbol_name(self, node: Node, src: bytes) -> str | None:
        if node.type == "field_declaration":  # embedded type
            return None
        return super().symbol_name(node, src)

    def is_exported(self, name: str, node: Node) -> bool:
        return bool(name) and name[0].isupper()

    def module_key(self, rel_path: str) -> str:
        # A Go package *is* its directory.
        return self._strip_source_root(Path(rel_path).parent.parts) or "."

    def parse_import(self, node: Node, src: bytes) -> list[ImportSpec]:
        raw = _WS.sub(" ", _text(node, src)).strip()
        specs: list[ImportSpec] = []
        for imp in _descendants(node, {"import_spec"}):
            path_node = imp.child_by_field_name("path")
            if path_node is None:
                continue
            module = _text(path_node, src).strip('"`')
            alias_node = imp.child_by_field_name("name")
            specs.append(
                ImportSpec(
                    raw,
                    module,
                    alias=_text(alias_node, src) if alias_node else module.rsplit("/", 1)[-1],
                )
            )
        return specs

    def resolve_import(
        self, spec: ImportSpec, importer: str, modules: dict[str, str]
    ) -> str | None:
        # Go import paths are absolute within a module ("host/org/repo/pkg/sub"),
        # so an internal package shows up as a unique directory suffix.
        return _unique_suffix_match(spec.module, modules, sep="/")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ADAPTERS: tuple[LanguageAdapter, ...] = (
    PythonAdapter(),
    TypeScriptAdapter(),
    TsxAdapter(),
    JavaScriptAdapter(),
    GoAdapter(),
)

_BY_EXT: dict[str, LanguageAdapter] = {
    ext: adapter for adapter in ADAPTERS for ext in adapter.extensions
}
_BY_NAME: dict[str, LanguageAdapter] = {adapter.name: adapter for adapter in ADAPTERS}

SUPPORTED_LANGUAGES: tuple[str, ...] = tuple(_BY_NAME)
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(_BY_EXT)


def adapter_for_path(path: Path | str) -> LanguageAdapter | None:
    suffixes = Path(path).suffixes
    if len(suffixes) >= 2 and "".join(suffixes[-2:]) == ".d.ts":
        return _BY_NAME["typescript"]
    return _BY_EXT.get(Path(path).suffix)


def adapter_for_language(name: str) -> LanguageAdapter | None:
    return _BY_NAME.get(name)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@lru_cache(maxsize=32)
def _compile(ts_name: str, query_file: str) -> Query:
    source = (QUERY_DIR / query_file).read_text(encoding="utf-8")
    return Query(get_language(ts_name), source)


def _text(node: Node | None, src: bytes) -> str:
    if node is None:
        return ""
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _body_node(node: Node) -> Node | None:
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    value = node.child_by_field_name("value")
    if value is not None and value.type in ("arrow_function", "function_expression"):
        inner = value.child_by_field_name("body")
        if inner is not None:
            return inner
    return None


def _descendants(node: Node, types: set[str], limit: int = 400) -> list[Node]:
    """Breadth-first search for descendant nodes of the given types."""
    found: list[Node] = []
    queue = list(node.named_children)
    while queue and len(found) < limit:
        cur = queue.pop(0)
        if cur.type in types:
            found.append(cur)
        else:
            queue.extend(cur.named_children)
    return found


#: Wrappers that sit between a definition and its documentation comment.
_DOC_WRAPPERS = frozenset(
    {
        "export_statement",
        "ambient_declaration",
        "lexical_declaration",
        "variable_declaration",
        "decorated_definition",
        "const_declaration",
        "type_declaration",
        "var_declaration",
    }
)


def _leading_comment(node: Node, src: bytes, max_lines: int = 12) -> str | None:
    """Collect the contiguous comment block directly above ``node``.

    Climbs out of declaration wrappers first: in `/** doc */ export function f()`
    the comment is a sibling of the *export_statement*, not of the function, and
    `const f = () => {}` buries the declarator one level deeper still.
    """
    # Climb while the parent is a declaration wrapper and nothing of substance
    # precedes us inside it -- i.e. the wrapper's own leading comment is ours.
    while node.parent is not None and node.parent.type in _DOC_WRAPPERS:
        if node.prev_sibling is not None and node.prev_sibling.type == "comment":
            break  # the comment is already our direct sibling
        node = node.parent

    lines: list[str] = []
    cur = node.prev_sibling
    while cur is not None and cur.type == "comment" and len(lines) < max_lines:
        if cur.end_point[0] + 1 < node.start_point[0]:
            break  # blank line between comment and definition
        lines.append(_text(cur, src))
        node, cur = cur, cur.prev_sibling
    if not lines:
        return None
    return _clean_docstring("\n".join(reversed(lines)))


def _clean_docstring(text: str, limit: int = 500) -> str | None:
    cleaned: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        for prefix in ("///", "//!", "//", "/**", "/*", "*/", "*"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :].strip()
                break
        stripped = stripped.removesuffix("*/").strip()
        cleaned.append(stripped)
    out = "\n".join(cleaned).strip()
    if not out:
        return None
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _normalise_path(path: str) -> str:
    """Resolve `.`/`..` segments without touching the filesystem."""
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _unique_suffix_match(module: str, modules: dict[str, str], sep: str) -> str | None:
    """Match `a.b.c` against `src.a.b.c` when exactly one candidate qualifies.

    Ambiguity is reported as "unresolved" on purpose -- a wrong edge is worse
    than a missing one, because an agent will act on it.
    """
    if not module:
        return None
    needle = sep + module
    hits = [path for key, path in modules.items() if key.endswith(needle)]
    if len(hits) == 1:
        return hits[0]
    tail = module.rsplit(sep, 1)[-1]
    hits = [path for key, path in modules.items() if key == tail or key.endswith(sep + tail)]
    return hits[0] if len(hits) == 1 else None
