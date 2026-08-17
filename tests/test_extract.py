"""Per-language extraction: definitions, kinds, nesting, docstrings, imports."""

from __future__ import annotations

from pathlib import Path

import pytest

from cartograph.indexer.extract import extract_file
from cartograph.indexer.languages import adapter_for_language, adapter_for_path
from cartograph.models import ParsedFile


def parse(rel_path: str, source: str) -> ParsedFile:
    adapter = adapter_for_path(rel_path)
    assert adapter is not None, rel_path
    return extract_file(rel_path, source.encode(), adapter)


def kinds(parsed: ParsedFile) -> dict[str, str]:
    return {s.local_qualname: s.kind for s in parsed.symbols}


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def test_python_kinds_and_nesting() -> None:
    parsed = parse(
        "pkg/mod.py",
        '''
"""Module doc."""

TIMEOUT = 30
lowercase_global = 1


class Outer:
    """Outer doc."""

    class Inner:
        def deep(self):
            """Nested two levels."""

    def method(self):
        def closure():
            return 1

        return closure()


def free_function(a, b=2):
    """Top-level."""
''',
    )
    found = kinds(parsed)
    assert found["Outer"] == "class"
    assert found["Outer.Inner"] == "class"
    # A function whose container is a class is a method, however deeply nested.
    assert found["Outer.Inner.deep"] == "method"
    assert found["Outer.method"] == "method"
    # A closure inside a method is a plain function, not a method.
    assert found["Outer.method.closure"] == "function"
    assert found["free_function"] == "function"
    # SCREAMING_CASE only: ordinary module globals are not constants.
    assert found["TIMEOUT"] == "const"
    assert "lowercase_global" not in found


def test_python_docstrings_and_signatures() -> None:
    parsed = parse(
        "m.py",
        '''
def compute(value: int, *, scale: float = 1.0) -> float:
    """Scale a value.

    Longer explanation.
    """
    return value * scale
''',
    )
    sym = next(s for s in parsed.symbols if s.name == "compute")
    assert sym.docstring is not None
    assert sym.docstring.startswith("Scale a value.")
    assert sym.signature == "def compute(value: int, *, scale: float = 1.0) -> float"


def test_python_exported_heuristic() -> None:
    parsed = parse("m.py", "def public():\n    pass\n\ndef _private():\n    pass\n")
    exported = {s.name: s.exported for s in parsed.symbols}
    assert exported["public"] is True
    assert exported["_private"] is False


def test_python_inherits_and_imports() -> None:
    parsed = parse(
        "pkg/child.py",
        "from pkg.base import Base\nimport json as j\n\n\nclass Child(Base):\n    pass\n",
    )
    inherits = [r for r in parsed.references if r.kind == "inherits"]
    assert [r.dst_name for r in inherits] == ["Base"]
    # The reference is attributed to the class itself, via byte containment.
    assert inherits[0].src_local_qualname == "Child"

    modules = {(i.module, i.symbol, i.alias) for i in parsed.imports}
    assert ("pkg.base", "Base", "Base") in modules
    assert ("json", None, "j") in modules


def test_python_module_key_strips_src_only() -> None:
    adapter = adapter_for_language("python")
    assert adapter is not None
    assert adapter.module_key("src/pkg/mod.py") == "pkg.mod"
    assert adapter.module_key("src/pkg/__init__.py") == "pkg"
    # `app` is a real package name and must survive.
    assert adapter.module_key("src/app/core.py") == "app.core"
    assert adapter.module_key("mod.py") == "mod"


def test_noise_callees_are_dropped() -> None:
    """Builtins can never be repo symbols; keeping them poisons the metrics."""
    parsed = parse("m.py", "def f(xs):\n    return len(sorted(xs)) + helper(xs)\n")
    called = {r.dst_name for r in parsed.references if r.kind == "calls"}
    assert called == {"helper"}


# ---------------------------------------------------------------------------
# TypeScript / JavaScript
# ---------------------------------------------------------------------------


def test_typescript_type_level_declarations() -> None:
    parsed = parse(
        "web/api.ts",
        """
import { helper } from './util';

export interface Transport { send(b: string): void }
export type Options = { retries: number };
export enum Mode { Fast, Safe }

export abstract class BaseClient {}

export class Client extends BaseClient implements Transport {
  send(b: string): void { helper(b); }
}

export const make = (o: Options): Client => new Client();
""",
    )
    found = kinds(parsed)
    assert found["Transport"] == "interface"
    assert found["Options"] == "type"
    assert found["Mode"] == "enum"
    assert found["BaseClient"] == "class"
    assert found["Client"] == "class"
    assert found["Client.send"] == "method"
    # Arrow-function consts are real definitions, not variables.
    assert found["make"] == "function"

    inherits = {r.dst_name for r in parsed.references if r.kind == "inherits"}
    assert {"BaseClient", "Transport"} <= inherits
    assert "Client" in {r.dst_name for r in parsed.references if r.kind == "instantiates"}


def test_typescript_export_marks_symbols_exported() -> None:
    parsed = parse("web/x.ts", "export function shown() {}\nfunction hidden() {}\n")
    exported = {s.name: s.exported for s in parsed.symbols}
    assert exported["shown"] is True
    assert exported["hidden"] is True  # no leading underscore; export is a bonus signal


def test_javascript_class_heritage() -> None:
    """The JS-only heritage shape, which TypeScript spells differently."""
    parsed = parse("web/legacy.js", "class A extends B {}\nclass C extends ns.D {}\n")
    inherits = {r.dst_name for r in parsed.references if r.kind == "inherits"}
    assert inherits == {"B", "ns.D"}


def test_jsdoc_leading_comment_becomes_docstring() -> None:
    parsed = parse("web/x.ts", "/** Adds numbers. */\nexport function add(a: number) {}\n")
    sym = next(s for s in parsed.symbols if s.name == "add")
    assert sym.docstring == "Adds numbers."


def test_blank_line_breaks_comment_association() -> None:
    parsed = parse("web/x.ts", "// unrelated banner\n\nexport function add(a: number) {}\n")
    sym = next(s for s in parsed.symbols if s.name == "add")
    assert sym.docstring is None


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def test_go_kinds_and_receivers() -> None:
    parsed = parse(
        "svc/store.go",
        """
package svc

import (
	"strings"
	alias "example.com/x/y"
)

const Limit = 10

type Record struct { ID string }
type Reader interface { Get(id string) Record }
type Alias = Record

type Store struct { items map[string]Record }

func (s *Store) Get(id string) Record { return s.items[id] }

func Normalize(v string) string { return strings.TrimSpace(v) }
""",
    )
    found = kinds(parsed)
    assert found["Record"] == "struct"
    assert found["Reader"] == "interface"
    assert found["Alias"] == "type"
    assert found["Store"] == "struct"
    assert found["Get"] == "method"
    assert found["Normalize"] == "function"
    assert found["Limit"] == "const"

    modules = {(i.module, i.alias) for i in parsed.imports}
    assert ("strings", "strings") in modules
    assert ("example.com/x/y", "alias") in modules


def test_go_exported_is_capitalisation() -> None:
    parsed = parse("svc/x.go", "package svc\n\nfunc Public() {}\nfunc private() {}\n")
    exported = {s.name: s.exported for s in parsed.symbols}
    assert exported["Public"] is True
    assert exported["private"] is False


def test_go_package_is_the_module() -> None:
    adapter = adapter_for_language("go")
    assert adapter is not None
    assert adapter.module_key("internal/store/store.go") == "internal/store"
    assert adapter.module_key("main.go") == "."


# ---------------------------------------------------------------------------
# cross-cutting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_core.py", True),
        ("src/app/core_test.go", True),
        ("web/client.spec.ts", True),
        ("web/client.test.tsx", True),
        ("__tests__/thing.js", True),
        ("src/app/core.py", False),
        ("src/latest/thing.py", False),
    ],
)
def test_test_file_detection(path: str, expected: bool) -> None:
    adapter = adapter_for_path(path)
    assert adapter is not None
    assert adapter.is_test_file(path) is expected


def test_unparseable_source_does_not_raise() -> None:
    """tree-sitter is error-tolerant; extraction must be too."""
    parsed = parse("m.py", "def broken(:\n    ???\n")
    assert isinstance(parsed, ParsedFile)


def test_empty_file() -> None:
    parsed = parse("m.py", "")
    assert parsed.symbols == []
    assert parsed.sha256


def test_sha256_is_content_addressed(tmp_path: Path) -> None:
    a = parse("m.py", "def f():\n    pass\n")
    b = parse("other.py", "def f():\n    pass\n")
    assert a.sha256 == b.sha256
