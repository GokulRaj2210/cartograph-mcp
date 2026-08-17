"""Core value types shared by the indexer, the graph store and the MCP layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SymbolKind = Literal[
    "function",
    "method",
    "class",
    "interface",
    "struct",
    "enum",
    "type",
    "const",
]

EdgeKind = Literal["calls", "inherits", "instantiates"]


@dataclass(slots=True)
class ParsedSymbol:
    """A definition extracted from a single file, before graph resolution."""

    name: str
    kind: SymbolKind
    start_line: int
    end_line: int
    signature: str | None = None
    docstring: str | None = None
    parent: str | None = None  # local name of the enclosing container symbol
    exported: bool = True

    @property
    def local_qualname(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


@dataclass(slots=True)
class ParsedReference:
    """A use-site: a call, an instantiation or an inheritance mention."""

    src_local_qualname: str | None  # None = module/top-level scope
    dst_name: str  # raw text at the use site, e.g. "store.who_calls"
    kind: EdgeKind
    line: int

    @property
    def dst_tail(self) -> str:
        """Last dotted segment -- what we actually match against symbol names."""
        return self.dst_name.rsplit(".", 1)[-1]

    @property
    def dst_receiver(self) -> str | None:
        """The part before the final segment, if the reference is qualified."""
        return self.dst_name.rsplit(".", 1)[0] if "." in self.dst_name else None


@dataclass(slots=True)
class ParsedImport:
    raw: str
    module: str
    symbol: str | None = None
    alias: str | None = None
    line: int = 1


@dataclass(slots=True)
class ParsedFile:
    path: str
    lang: str
    module: str
    sha256: str
    size: int
    lines: int
    is_test: bool
    symbols: list[ParsedSymbol] = field(default_factory=list)
    references: list[ParsedReference] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class Symbol:
    """A symbol as stored in the graph."""

    id: int
    name: str
    qualname: str
    kind: str
    lang: str
    path: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    rank: float

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}"


@dataclass(slots=True, frozen=True)
class Neighbour:
    """A symbol reached during traversal, with provenance."""

    symbol: Symbol
    depth: int
    confidence: float
    via_line: int
    reason: str


@dataclass(slots=True)
class IndexStats:
    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    symbols: int = 0
    edges: int = 0
    resolved_edges: int = 0
    external_edges: int = 0
    imports: int = 0
    duration_s: float = 0.0
    by_lang: dict[str, int] = field(default_factory=dict)

    @property
    def resolution_rate(self) -> float:
        return self.resolved_edges / self.edges if self.edges else 0.0

    @property
    def internal_resolution_rate(self) -> float:
        """Resolution over call sites that could point at a repo symbol."""
        internal = self.edges - self.external_edges
        return self.resolved_edges / internal if internal else 0.0
