"""Every ``.scm`` file must compile against every grammar that loads it.

This is the regression test for a bug that cost real debugging time: a pattern
valid in the JavaScript grammar (`(class_heritage (identifier))`) is an
*Impossible pattern* in TypeScript, which wraps supertypes in an
`extends_clause`. Because ``javascript.scm`` is shared by the js, ts and tsx
adapters, that one line raised QueryError for every TypeScript file -- and the
indexer's per-file error handling turned it into "TypeScript silently produces
no symbols" rather than a crash.

Two lessons are encoded here:
  1. compile every query eagerly in CI, per grammar;
  2. a query that yields zero captures on real code is a bug, not a valid state.
"""

from __future__ import annotations

import pytest
from tree_sitter import Query, QueryError

from cartograph.indexer.languages import ADAPTERS, QUERY_DIR, LanguageAdapter

SAMPLES: dict[str, bytes] = {
    "python": b"""
from os import path

import json


class Base:
    def method(self):
        return helper()

def helper():
    return Base()
""",
    "javascript": b"""
import { a } from './m.js';
class A extends B { m() { return new C(); } }
const f = (x) => a(x);
function g() { return f(1); }
""",
    "typescript": b"""
import { a } from './m';
interface I extends J { m(): void }
type T = string;
enum E { X }
class A extends B implements I { m(): void { a(); } }
const f = (x: number): number => x;
""",
    "tsx": b"""
import { a } from './m';
interface I { m(): void }
class A implements I { m(): void { a(); } }
const F = () => <div onClick={a}>hi</div>;
""",
    "go": b"""
package p

import "strings"

type S struct { X int }
type R interface { Get() S }

func (s *S) Get() S { return S{X: Norm("a")} }
func Norm(v string) string { return strings.TrimSpace(v) }
""",
}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.name)
def test_query_files_compile(adapter: LanguageAdapter) -> None:
    """A shared query file must be valid for *this* adapter's grammar."""
    language = adapter.language()
    for query_file in adapter.query_files:
        source = (QUERY_DIR / query_file).read_text(encoding="utf-8")
        try:
            Query(language, source)
        except QueryError as exc:  # pragma: no cover - only on regression
            pytest.fail(f"{query_file} does not compile for {adapter.ts_name}: {exc}")


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.name)
def test_queries_capture_something(adapter: LanguageAdapter) -> None:
    """A compiling query that captures nothing is the failure mode we missed."""
    from cartograph.indexer.extract import extract_file

    source = SAMPLES[adapter.name]
    ext = adapter.extensions[0]
    parsed = extract_file(f"sample{ext}", source, adapter)

    assert parsed.symbols, f"{adapter.name}: extracted no symbols"
    assert parsed.references, f"{adapter.name}: extracted no references"
    if adapter.name != "go":  # the Go sample's only import is stdlib, still parsed
        assert parsed.imports, f"{adapter.name}: extracted no imports"


def test_every_query_file_is_used() -> None:
    """No orphaned .scm files: each one is loaded by at least one adapter."""
    referenced = {qf for adapter in ADAPTERS for qf in adapter.query_files}
    on_disk = {p.name for p in QUERY_DIR.glob("*.scm")}
    assert on_disk == referenced, f"unused or missing query files: {on_disk ^ referenced}"
