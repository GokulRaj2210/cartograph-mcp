"""Query facade.

One place where the graph questions are answered, so the CLI and the MCP server
cannot drift apart. Methods return structured results; formatting lives in
:mod:`cartograph.views`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from cartograph.graph.algorithms import (
    longest_path_layers,
    personalized_pagerank,
    strongly_connected_components,
)
from cartograph.graph.store import DEFAULT_DB_NAME, GraphStore
from cartograph.models import Neighbour, Symbol

#: who_calls/calls default: precision first. An agent *acts* on these answers,
#: so a plausible-but-wrong caller is worse than a missing one.
PRECISE = 0.5
#: blast_radius default: recall first. A missed impacted test is the expensive
#: failure mode; a false positive only costs the reviewer a glance.
BROAD = 0.3


class IndexNotFoundError(RuntimeError):
    pass


@dataclass(slots=True)
class SymbolDetail:
    symbol: Symbol
    callers: list[Neighbour]
    callees: list[Neighbour]
    members: list[Symbol]
    source: str | None = None


@dataclass(slots=True)
class BlastRadius:
    target: str
    kind: str  # "file" | "symbol"
    files: list[tuple[str, int, bool]] = field(default_factory=list)
    symbols: list[Neighbour] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    truncated: bool = False


@dataclass(slots=True)
class Architecture:
    modules: list[tuple[str, int, int]]
    edges: list[tuple[str, str, int]]
    cycles: list[list[str]]
    layers: dict[str, int]
    hotspots: list[tuple[Symbol, int]]
    entry_points: list[Symbol]
    counts: dict[str, int]
    by_lang: dict[str, int]


@dataclass(slots=True)
class FileSummary:
    path: str
    lang: str
    lines: int
    is_test: bool
    symbols: list[Symbol]
    imports: list[tuple[str, str | None, bool]]  # (module, target path, external)
    imported_by: list[str]


class Cartograph:
    """Read-only view over an existing index."""

    def __init__(self, store: GraphStore, root: Path) -> None:
        self.store = store
        self.root = root

    @classmethod
    def load(cls, db_path: Path | str | None = None, root: Path | None = None) -> Self:
        db = Path(db_path) if db_path else discover_db(root or Path.cwd())
        if db is None or not db.exists():
            raise IndexNotFoundError(
                "no Cartograph index found -- run `cartograph index <repo>` first"
            )
        store = GraphStore.open(db, create=False)
        stored_root = store.get_meta("root")
        return cls(store, Path(stored_root) if stored_root else (root or Path.cwd()))

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- lookup --------------------------------------------------------------

    def find_symbol(
        self,
        name: str,
        *,
        kind: str | None = None,
        lang: str | None = None,
        limit: int = 20,
    ) -> list[Symbol]:
        return self.store.find_symbols(name, kind=kind, lang=lang, limit=limit)

    def search(self, query: str, *, limit: int = 20) -> list[Symbol]:
        return self.store.search(query, limit=limit)

    def _one(self, selector: str) -> Symbol:
        matches = self.store.resolve_selector(selector)
        if not matches:
            raise LookupError(f"no symbol matches {selector!r}")
        return matches[0]

    def candidates(self, selector: str, limit: int = 5) -> list[Symbol]:
        return self.store.resolve_selector(selector, limit=limit)

    def symbol_detail(
        self,
        selector: str,
        *,
        depth: int = 1,
        include_source: bool = False,
        min_confidence: float = PRECISE,
        limit: int = 25,
    ) -> SymbolDetail:
        symbol = self._one(selector)
        return SymbolDetail(
            symbol=symbol,
            callers=self.store.callers(
                symbol.id, depth=depth, min_confidence=min_confidence, limit=limit
            ),
            callees=self.store.callees(
                symbol.id, depth=depth, min_confidence=min_confidence, limit=limit
            ),
            members=[
                s
                for s in self.store.symbols_in_file(symbol.path)
                if s.qualname.startswith(f"{symbol.qualname}.")
            ],
            source=(self.store.symbol_source(symbol, self.root) if include_source else None),
        )

    # -- traversal -----------------------------------------------------------

    def who_calls(
        self,
        selector: str,
        *,
        depth: int = 2,
        min_confidence: float = PRECISE,
        limit: int = 50,
    ) -> tuple[Symbol, list[Neighbour]]:
        symbol = self._one(selector)
        return symbol, self.store.callers(
            symbol.id, depth=depth, min_confidence=min_confidence, limit=limit
        )

    def calls(
        self,
        selector: str,
        *,
        depth: int = 2,
        min_confidence: float = PRECISE,
        limit: int = 50,
    ) -> tuple[Symbol, list[Neighbour]]:
        symbol = self._one(selector)
        return symbol, self.store.callees(
            symbol.id, depth=depth, min_confidence=min_confidence, limit=limit
        )

    def related(self, selector: str, *, limit: int = 15) -> tuple[Symbol, list[Symbol]]:
        """Structurally nearby symbols, via personalized PageRank on the call graph.

        This is the "what else should I read before editing this?" query that
        vector search approximates badly -- proximity in the call graph is the
        real relationship, and it needs no embeddings.
        """
        symbol = self._one(selector)
        scores = personalized_pagerank(self.store.call_edges(), [symbol.id])
        top = sorted(scores.items(), key=lambda kv: -kv[1])[: limit * 2]
        out: list[Symbol] = []
        for sid, _ in top:
            found = self.store.get_symbol(sid)
            if found is not None and found.id != symbol.id:
                out.append(found)
            if len(out) >= limit:
                break
        return symbol, out

    def blast_radius(
        self,
        target: str,
        *,
        depth: int = 3,
        min_confidence: float = BROAD,
        limit: int = 60,
    ) -> BlastRadius:
        """What breaks if ``target`` changes: dependent files, callers, and tests."""
        paths = self.store.match_files(target)
        if paths:
            path = paths[0]
            files = self.store.dependent_files(path, depth=depth, limit=limit)
            symbols: list[Neighbour] = []
            for sym in self.store.symbols_in_file(path, limit=40):
                # Only callers *outside* the file are impact: a sibling function
                # in the same file moves with the change, so reporting it as
                # "affected" is noise that buries the real dependents.
                symbols.extend(
                    caller
                    for caller in self.store.callers(
                        sym.id, depth=1, min_confidence=min_confidence, limit=10
                    )
                    if caller.symbol.path != path
                )
            return BlastRadius(
                target=path,
                kind="file",
                files=files,
                symbols=_dedupe_neighbours(symbols)[:limit],
                tests=sorted({p for p, _, is_test in files if is_test}),
                truncated=len(files) >= limit,
            )

        symbol = self._one(target)
        callers = self.store.callers(
            symbol.id, depth=depth, min_confidence=min_confidence, limit=limit
        )
        touched = {n.symbol.path for n in callers} | {symbol.path}
        file_rows = [(p, 1, self._is_test(p)) for p in sorted(touched) if p != symbol.path]
        return BlastRadius(
            target=symbol.qualname,
            kind="symbol",
            files=file_rows,
            symbols=callers,
            tests=sorted({p for p, _, is_test in file_rows if is_test}),
            truncated=len(callers) >= limit,
        )

    def _is_test(self, path: str) -> bool:
        from cartograph.indexer.languages import adapter_for_path

        adapter = adapter_for_path(path)
        return adapter.is_test_file(path) if adapter else False

    # -- overviews -----------------------------------------------------------

    def architecture(self, *, module_limit: int = 40, min_weight: int = 1) -> Architecture:
        edges = self.store.module_edges(min_weight=min_weight)
        pairs = [(src, dst) for src, dst, _ in edges]
        return Architecture(
            modules=self.store.modules()[:module_limit],
            edges=edges,
            cycles=strongly_connected_components(pairs),
            layers=longest_path_layers(pairs),
            hotspots=self.store.hotspots(limit=15),
            entry_points=self.store.entry_points(limit=10),
            counts=self.store.counts(),
            by_lang=self.store.counts_by("lang"),
        )

    def file_summary(self, path_query: str) -> FileSummary:
        paths = self.store.match_files(path_query)
        if not paths:
            raise LookupError(f"no indexed file matches {path_query!r}")
        path = paths[0]
        meta = self.store.conn.execute(
            "SELECT lang, lines, is_test FROM files WHERE path = ?", (path,)
        ).fetchone()
        imports = [
            (row["module"], row["target"], bool(row["external"]))
            for row in self.store.file_imports(path)
        ]
        return FileSummary(
            path=path,
            lang=meta["lang"],
            lines=meta["lines"],
            is_test=bool(meta["is_test"]),
            symbols=self.store.symbols_in_file(path),
            imports=imports,
            imported_by=[p for p, d, _ in self.store.dependent_files(path, depth=1) if d == 1],
        )

    def stats(self) -> dict[str, Any]:
        counts = self.store.counts()
        internal = counts["edges"] - counts["external_edges"]
        return {
            **counts,
            "by_lang": self.store.counts_by("lang"),
            "by_kind": self.store.counts_by("kind", "symbols"),
            "edge_reasons": self.store.edge_reasons(),
            "resolution_rate": (
                counts["resolved_edges"] / counts["edges"] if counts["edges"] else 0.0
            ),
            # The number that actually means something: of the call sites that
            # *could* point at a repo symbol, how many did we land?
            "internal_resolution_rate": (counts["resolved_edges"] / internal if internal else 0.0),
            "root": self.store.get_meta("root"),
            "indexed_at": self.store.get_meta("indexed_at"),
            "db_path": str(self.store.path),
            "db_bytes": self.store.path.stat().st_size if self.store.path.exists() else 0,
        }


def discover_db(start: Path) -> Path | None:
    """Walk up from ``start`` looking for ``.cartograph/cartograph.db``."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        db = candidate / ".cartograph" / DEFAULT_DB_NAME
        if db.exists():
            return db
    return None


def _dedupe_neighbours(items: list[Neighbour]) -> list[Neighbour]:
    best: dict[int, Neighbour] = {}
    for item in items:
        existing = best.get(item.symbol.id)
        if existing is None or item.confidence > existing.confidence:
            best[item.symbol.id] = item
    return sorted(best.values(), key=lambda n: (n.depth, -n.confidence, n.symbol.qualname))
