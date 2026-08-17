"""SQLite-backed code graph: writes during indexing, traversal during queries.

Traversal runs as recursive CTEs rather than Python loops, so a depth-4
who-calls over a 100k-edge graph stays inside SQLite's C loop and returns in
single-digit milliseconds. Every traversal is depth- and result-capped because
the consumer is usually an LLM with a finite context window.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from cartograph.models import Neighbour, ParsedFile, Symbol

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION = "1"

#: How long a query waits for the indexer's write lock before giving up. Querying
#: while a reindex runs is normal usage, not an error, so we wait rather than fail.
BUSY_TIMEOUT_S = 15.0

DEFAULT_DB_NAME = "cartograph.db"

_SYMBOL_COLUMNS = """
    s.id, s.name, s.qualname, s.kind, s.lang, f.path,
    s.start_line, s.end_line, s.signature, s.docstring, s.rank
"""

_CALL_KINDS = ("calls", "instantiates")


@dataclass(frozen=True, slots=True)
class RawRef:
    """A staged reference awaiting resolution."""

    id: int
    file_id: int
    src_local: str | None
    dst_name: str
    kind: str
    line: int


@dataclass(frozen=True, slots=True)
class SymbolRow:
    """The slice of a symbol the resolver needs -- kept narrow so the whole
    repo's symbol table fits comfortably in memory during resolution."""

    id: int
    name: str
    qualname: str
    kind: str
    file_id: int
    module: str
    parent_name: str | None


@dataclass(frozen=True, slots=True)
class ImportRow:
    file_id: int
    module: str
    symbol: str | None
    alias: str | None
    dst_file_id: int | None


class GraphStore:
    """Thin, explicit DAO. No ORM: the queries *are* the interesting part."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self.conn = conn
        self.path = path

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls, db_path: Path | str, *, create: bool = True) -> Self:
        """Open an index, creating the schema only if it is not already there.

        Opening must be side-effect-free on an existing index. Writing during a
        read open (DDL, pragmas, a version stamp) means a query fails whenever an
        indexer holds the database -- which is exactly when an agent is querying.
        """
        db_path = Path(db_path)
        if not create and not db_path.exists():
            raise FileNotFoundError(f"no index at {db_path} -- run `cartograph index` first")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the MCP server dispatches tool calls on an
        # anyio worker thread pool. Access is serialised by the caller's lock.
        conn = sqlite3.connect(db_path, check_same_thread=False, timeout=BUSY_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        # Per-connection, so they must be re-applied on every open.
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
        conn.execute("PRAGMA foreign_keys = ON")
        store = cls(conn, db_path)
        if not store._schema_is_current():
            store._create_schema()
        return store

    def _schema_is_current(self) -> bool:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError:
            return False  # meta table absent => fresh or pre-schema file
        return bool(row) and row["value"] == SCHEMA_VERSION

    def _create_schema(self) -> None:
        # WAL is persistent, so setting it once at creation is enough -- and it is
        # what lets a reader and the indexer coexist.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.set_meta("schema_version", SCHEMA_VERSION)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- meta ----------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # -- indexing writes -----------------------------------------------------

    def file_hashes(self) -> dict[str, str]:
        return {
            row["path"]: row["sha256"]
            for row in self.conn.execute("SELECT path, sha256 FROM files")
        }

    def delete_files(self, paths: Iterable[str]) -> int:
        paths = list(paths)
        if not paths:
            return 0
        ids = [
            row["id"]
            for row in self.conn.execute(
                f"SELECT id FROM files WHERE path IN ({_placeholders(paths)})", paths
            )
        ]
        if ids:
            # Contentless FTS5 rows are not reachable by cascade; drop them first.
            self.conn.executemany(
                "INSERT INTO symbol_fts (symbol_fts, rowid, name, qualname, signature, docstring) "
                "SELECT 'delete', id, name, qualname, COALESCE(signature, ''), "
                "COALESCE(docstring, '') FROM symbols WHERE id = ?",
                [(sid,) for sid in self._symbol_ids_for_files(ids)],
            )
        self.conn.execute(f"DELETE FROM files WHERE path IN ({_placeholders(paths)})", paths)
        return len(paths)

    def _symbol_ids_for_files(self, file_ids: Sequence[int]) -> list[int]:
        return [
            row["id"]
            for row in self.conn.execute(
                f"SELECT id FROM symbols WHERE file_id IN ({_placeholders(file_ids)})",
                list(file_ids),
            )
        ]

    def insert_file(self, parsed: ParsedFile) -> int:
        """Insert a freshly parsed file, its symbols and its staged references."""
        cur = self.conn.execute(
            "INSERT INTO files (path, lang, module, sha256, size, lines, is_test, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                parsed.path,
                parsed.lang,
                parsed.module,
                parsed.sha256,
                parsed.size,
                parsed.lines,
                int(parsed.is_test),
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
        )
        file_id = int(cur.lastrowid or 0)

        # Parents must exist before children so parent_id can be set in one pass.
        by_local: dict[str, int] = {}
        for sym in sorted(
            parsed.symbols, key=lambda s: (s.local_qualname.count("."), s.start_line)
        ):
            parent_id = by_local.get(sym.parent) if sym.parent else None
            qualname = f"{parsed.module}:{sym.local_qualname}"
            scur = self.conn.execute(
                "INSERT INTO symbols (file_id, parent_id, name, qualname, kind, lang, "
                "start_line, end_line, signature, docstring, exported) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    parent_id,
                    sym.name,
                    qualname,
                    sym.kind,
                    parsed.lang,
                    sym.start_line,
                    sym.end_line,
                    sym.signature,
                    sym.docstring,
                    int(sym.exported),
                ),
            )
            sid = int(scur.lastrowid or 0)
            by_local[sym.local_qualname] = sid
            self.conn.execute(
                "INSERT INTO symbol_fts (rowid, name, qualname, signature, docstring) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, sym.name, qualname, sym.signature or "", sym.docstring or ""),
            )

        self.conn.executemany(
            "INSERT INTO refs (file_id, src_local, dst_name, kind, line) VALUES (?, ?, ?, ?, ?)",
            [
                (file_id, r.src_local_qualname, r.dst_name, r.kind, r.line)
                for r in parsed.references
            ],
        )
        self.conn.executemany(
            "INSERT INTO imports (file_id, raw, module, symbol, alias, line) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(file_id, i.raw, i.module, i.symbol, i.alias, i.line) for i in parsed.imports],
        )
        return file_id

    def module_map(self) -> dict[str, str]:
        return {
            row["module"]: row["path"]
            for row in self.conn.execute("SELECT module, path FROM files ORDER BY path")
        }

    def file_ids(self) -> dict[str, int]:
        return {row["path"]: row["id"] for row in self.conn.execute("SELECT path, id FROM files")}

    def set_import_targets(self, updates: Sequence[tuple[int | None, int, int]]) -> None:
        """updates: (dst_file_id, external, import_id)"""
        self.conn.executemany(
            "UPDATE imports SET dst_file_id = ?, external = ? WHERE id = ?", updates
        )

    def all_imports(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT i.id, i.file_id, i.module, i.symbol, i.alias, f.path AS importer_path, "
                "f.lang AS importer_lang FROM imports i JOIN files f ON f.id = i.file_id"
            )
        )

    def resolved_imports(self) -> list[ImportRow]:
        return [
            ImportRow(
                file_id=row["file_id"],
                module=row["module"],
                symbol=row["symbol"],
                alias=row["alias"],
                dst_file_id=row["dst_file_id"],
            )
            for row in self.conn.execute(
                "SELECT file_id, module, symbol, alias, dst_file_id FROM imports"
            )
        ]

    def all_refs(self) -> list[RawRef]:
        return [
            RawRef(
                id=row["id"],
                file_id=row["file_id"],
                src_local=row["src_local"],
                dst_name=row["dst_name"],
                kind=row["kind"],
                line=row["line"],
            )
            for row in self.conn.execute(
                "SELECT id, file_id, src_local, dst_name, kind, line FROM refs"
            )
        ]

    def all_symbol_rows(self) -> list[SymbolRow]:
        return [
            SymbolRow(
                id=row["id"],
                name=row["name"],
                qualname=row["qualname"],
                kind=row["kind"],
                file_id=row["file_id"],
                module=row["module"],
                parent_name=row["parent_name"],
            )
            for row in self.conn.execute(
                "SELECT s.id, s.name, s.qualname, s.kind, s.file_id, f.module, "
                "p.name AS parent_name "
                "FROM symbols s JOIN files f ON f.id = s.file_id "
                "LEFT JOIN symbols p ON p.id = s.parent_id"
            )
        ]

    def replace_edges(
        self, edges: Iterable[tuple[int, int | None, str, str, int, float, str]]
    ) -> int:
        """Wholesale edge rebuild: (src_id, dst_id, dst_name, kind, line, confidence, reason)."""
        self.conn.execute("DELETE FROM edges")
        rows = list(edges)
        self.conn.executemany(
            "INSERT INTO edges (src_id, dst_id, dst_name, kind, line, confidence, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return len(rows)

    def set_ranks(self, ranks: dict[int, float]) -> None:
        self.conn.executemany(
            "UPDATE symbols SET rank = ? WHERE id = ?",
            [(score, sid) for sid, score in ranks.items()],
        )

    def optimize(self) -> None:
        self.conn.commit()
        self.conn.execute("INSERT INTO symbol_fts (symbol_fts) VALUES ('optimize')")
        self.conn.execute("ANALYZE")
        self.conn.commit()

    # -- lookups -------------------------------------------------------------

    def find_symbols(
        self,
        name: str,
        *,
        kind: str | None = None,
        lang: str | None = None,
        limit: int = 20,
        exact: bool = False,
    ) -> list[Symbol]:
        """Ranked symbol lookup: exact name, then qualname suffix, then prefix.

        Ordering is by PageRank so `find_symbol("run")` surfaces the `run` that
        the codebase actually revolves around rather than an arbitrary one.
        """
        clauses = ["1 = 1"]
        params: list[Any] = []
        if exact:
            clauses.append("s.name = ?")
            params.append(name)
        else:
            clauses.append("(s.name = ? OR s.qualname = ? OR s.name LIKE ? OR s.qualname LIKE ?)")
            params += [name, name, f"{name}%", f"%{name}"]
        if kind:
            clauses.append("s.kind = ?")
            params.append(kind)
        if lang:
            clauses.append("s.lang = ?")
            params.append(lang)

        sql = f"""
            SELECT {_SYMBOL_COLUMNS},
                   CASE WHEN s.name = ? THEN 0
                        WHEN s.qualname = ? THEN 0
                        WHEN s.qualname LIKE ? THEN 1
                        ELSE 2 END AS tier
            FROM symbols s JOIN files f ON f.id = s.file_id
            WHERE {" AND ".join(clauses)}
            ORDER BY tier, s.rank DESC, LENGTH(s.qualname), s.qualname
            LIMIT ?
        """
        rows = self.conn.execute(sql, [name, name, f"%{name}", *params, limit])
        return [_to_symbol(row) for row in rows]

    def search(self, query: str, *, limit: int = 20) -> list[Symbol]:
        """Full-text search over names, signatures and docstrings."""
        sql = f"""
            SELECT {_SYMBOL_COLUMNS}, bm25(symbol_fts, 8.0, 4.0, 2.0, 1.0) AS score
            FROM symbol_fts
            JOIN symbols s ON s.id = symbol_fts.rowid
            JOIN files f ON f.id = s.file_id
            WHERE symbol_fts MATCH ?
            ORDER BY score + (-3.0 * s.rank) LIMIT ?
        """
        try:
            rows = self.conn.execute(sql, (_fts_query(query), limit)).fetchall()
        except sqlite3.OperationalError:
            return self.find_symbols(query, limit=limit)
        return [_to_symbol(row) for row in rows]

    def get_symbol(self, symbol_id: int) -> Symbol | None:
        row = self.conn.execute(
            f"SELECT {_SYMBOL_COLUMNS} FROM symbols s JOIN files f ON f.id = s.file_id "
            "WHERE s.id = ?",
            (symbol_id,),
        ).fetchone()
        return _to_symbol(row) if row else None

    def resolve_selector(self, selector: str, *, limit: int = 5) -> list[Symbol]:
        """Accept an id, a qualname, a bare name or ``path:name``.

        Agents phrase the same request four different ways; making the server
        tolerant here removes a whole class of retry loops.
        """
        selector = selector.strip()
        if selector.isdigit():
            sym = self.get_symbol(int(selector))
            return [sym] if sym else []
        if ":" in selector:
            exact = self.conn.execute(
                f"SELECT {_SYMBOL_COLUMNS} FROM symbols s JOIN files f ON f.id = s.file_id "
                "WHERE s.qualname = ? ORDER BY s.rank DESC LIMIT ?",
                (selector, limit),
            ).fetchall()
            if exact:
                return [_to_symbol(row) for row in exact]
            path, _, name = selector.rpartition(":")
            rows = self.conn.execute(
                f"SELECT {_SYMBOL_COLUMNS} FROM symbols s JOIN files f ON f.id = s.file_id "
                "WHERE (f.path = ? OR f.path LIKE ?) AND (s.name = ? OR s.qualname LIKE ?) "
                "ORDER BY s.rank DESC LIMIT ?",
                (path, f"%{path}", name, f"%{name}", limit),
            ).fetchall()
            if rows:
                return [_to_symbol(row) for row in rows]
        return self.find_symbols(selector, limit=limit)

    def symbol_source(self, symbol: Symbol, root: Path, *, context_lines: int = 0) -> str:
        file_path = root / symbol.path
        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return f"<unreadable: {exc}>"
        start = max(0, symbol.start_line - 1 - context_lines)
        end = min(len(lines), symbol.end_line + context_lines)
        return "\n".join(lines[start:end])

    # -- traversal -----------------------------------------------------------

    def callers(
        self, symbol_id: int, *, depth: int = 2, min_confidence: float = 0.5, limit: int = 50
    ) -> list[Neighbour]:
        return self._traverse(symbol_id, depth, min_confidence, limit, direction="up")

    def callees(
        self, symbol_id: int, *, depth: int = 2, min_confidence: float = 0.5, limit: int = 50
    ) -> list[Neighbour]:
        return self._traverse(symbol_id, depth, min_confidence, limit, direction="down")

    def _traverse(
        self, symbol_id: int, depth: int, min_confidence: float, limit: int, *, direction: str
    ) -> list[Neighbour]:
        depth = max(1, min(depth, 8))
        near, far = ("dst_id", "src_id") if direction == "up" else ("src_id", "dst_id")
        sql = f"""
            WITH RECURSIVE reach(id, depth, confidence, line, reason) AS (
                SELECT e.{far}, 1, e.confidence, e.line, e.reason
                FROM edges e
                WHERE e.{near} = :root AND e.{far} IS NOT NULL
                  AND e.kind IN {_CALL_KINDS!r} AND e.confidence >= :minc
                UNION
                SELECT e.{far}, r.depth + 1, MIN(e.confidence, r.confidence), e.line, e.reason
                FROM edges e JOIN reach r ON e.{near} = r.id
                WHERE r.depth < :maxd AND e.{far} IS NOT NULL
                  AND e.kind IN {_CALL_KINDS!r} AND e.confidence >= :minc
            )
            SELECT {_SYMBOL_COLUMNS}, MIN(r.depth) AS depth,
                   MAX(r.confidence) AS confidence, MIN(r.line) AS line,
                   MIN(r.reason) AS reason
            FROM reach r
            JOIN symbols s ON s.id = r.id
            JOIN files f ON f.id = s.file_id
            WHERE r.id != :root
            GROUP BY s.id
            ORDER BY depth, s.rank DESC, s.qualname
            LIMIT :limit
        """
        rows = self.conn.execute(
            sql, {"root": symbol_id, "minc": min_confidence, "maxd": depth, "limit": limit}
        )
        return [
            Neighbour(
                symbol=_to_symbol(row),
                depth=row["depth"],
                confidence=row["confidence"],
                via_line=row["line"],
                reason=row["reason"],
            )
            for row in rows
        ]

    def dependent_files(
        self, path: str, *, depth: int = 3, limit: int = 200
    ) -> list[tuple[str, int, bool]]:
        """Files that transitively import ``path``. Returns (path, depth, is_test)."""
        depth = max(1, min(depth, 8))
        sql = """
            WITH RECURSIVE up(file_id, depth) AS (
                SELECT i.file_id, 1 FROM imports i
                JOIN files t ON t.id = i.dst_file_id
                WHERE t.path = :path
                UNION
                SELECT i.file_id, up.depth + 1 FROM imports i
                JOIN up ON i.dst_file_id = up.file_id
                WHERE up.depth < :maxd
            )
            SELECT f.path, MIN(up.depth) AS depth, f.is_test
            FROM up JOIN files f ON f.id = up.file_id
            WHERE f.path != :path
            GROUP BY f.id
            ORDER BY depth, f.is_test DESC, f.path
            LIMIT :limit
        """
        rows = self.conn.execute(sql, {"path": path, "maxd": depth, "limit": limit})
        return [(row["path"], row["depth"], bool(row["is_test"])) for row in rows]

    def symbols_in_file(self, path: str, *, limit: int = 200) -> list[Symbol]:
        rows = self.conn.execute(
            f"SELECT {_SYMBOL_COLUMNS} FROM symbols s JOIN files f ON f.id = s.file_id "
            "WHERE f.path = ? ORDER BY s.start_line LIMIT ?",
            (path, limit),
        )
        return [_to_symbol(row) for row in rows]

    def file_imports(self, path: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT i.raw, i.module, i.symbol, i.external, i.line, t.path AS target "
                "FROM imports i JOIN files f ON f.id = i.file_id "
                "LEFT JOIN files t ON t.id = i.dst_file_id "
                "WHERE f.path = ? ORDER BY i.line",
                (path,),
            )
        )

    def match_files(self, pattern: str, *, limit: int = 20) -> list[str]:
        rows = self.conn.execute(
            "SELECT path FROM files WHERE path = ? OR path LIKE ? ORDER BY LENGTH(path) LIMIT ?",
            (pattern, f"%{pattern}%", limit),
        )
        return [row["path"] for row in rows]

    # -- aggregates ----------------------------------------------------------

    def module_edges(self, *, min_weight: int = 1) -> list[tuple[str, str, int]]:
        """Module-level import graph, aggregated from file-level imports."""
        rows = self.conn.execute(
            "SELECT src.module AS s, dst.module AS d, COUNT(*) AS w "
            "FROM imports i "
            "JOIN files src ON src.id = i.file_id "
            "JOIN files dst ON dst.id = i.dst_file_id "
            "WHERE src.module != dst.module "
            "GROUP BY s, d HAVING w >= ? ORDER BY w DESC",
            (min_weight,),
        )
        return [(row["s"], row["d"], row["w"]) for row in rows]

    def modules(self) -> list[tuple[str, int, int]]:
        rows = self.conn.execute(
            "SELECT f.module, COUNT(DISTINCT f.id) AS files, COUNT(s.id) AS symbols "
            "FROM files f LEFT JOIN symbols s ON s.file_id = f.id "
            "GROUP BY f.module ORDER BY symbols DESC"
        )
        return [(row["module"], row["files"], row["symbols"]) for row in rows]

    def hotspots(self, *, limit: int = 20, kind: str | None = None) -> list[tuple[Symbol, int]]:
        """Highest-PageRank symbols with their in-degree."""
        sql = f"""
            SELECT {_SYMBOL_COLUMNS},
                   (SELECT COUNT(*) FROM edges e WHERE e.dst_id = s.id) AS fan_in
            FROM symbols s JOIN files f ON f.id = s.file_id
            {"WHERE s.kind = ?" if kind else ""}
            ORDER BY s.rank DESC, fan_in DESC LIMIT ?
        """
        params: list[Any] = [kind, limit] if kind else [limit]
        return [(_to_symbol(row), row["fan_in"]) for row in self.conn.execute(sql, params)]

    def entry_points(self, *, limit: int = 20) -> list[Symbol]:
        """Exported symbols nothing else in the repo calls -- the public surface."""
        sql = f"""
            SELECT {_SYMBOL_COLUMNS} FROM symbols s JOIN files f ON f.id = s.file_id
            WHERE s.exported = 1 AND s.kind IN ('function', 'class')
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst_id = s.id)
              AND EXISTS (SELECT 1 FROM edges e WHERE e.src_id = s.id)
            ORDER BY s.rank DESC, s.qualname LIMIT ?
        """
        return [_to_symbol(row) for row in self.conn.execute(sql, (limit,))]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("files", "symbols", "edges", "imports", "refs"):
            row = self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            out[table] = int(row["c"])
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM edges WHERE dst_id IS NOT NULL"
        ).fetchone()
        out["resolved_edges"] = int(row["c"])
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM edges WHERE reason = 'external'"
        ).fetchone()
        out["external_edges"] = int(row["c"])
        row = self.conn.execute("SELECT COALESCE(SUM(lines), 0) AS c FROM files").fetchone()
        out["lines"] = int(row["c"])
        return out

    def counts_by(self, column: str, table: str = "files") -> dict[str, int]:
        if not column.isidentifier() or not table.isidentifier():
            raise ValueError("column/table must be identifiers")
        rows = self.conn.execute(
            f"SELECT {column} AS k, COUNT(*) AS c FROM {table} GROUP BY k ORDER BY c DESC"
        )
        return {str(row["k"]): int(row["c"]) for row in rows}

    def edge_reasons(self) -> dict[str, int]:
        return self.counts_by("reason", "edges")

    def call_edges(self) -> list[tuple[int, int, float]]:
        """Resolved call edges for the ranking pass."""
        rows = self.conn.execute(
            "SELECT src_id, dst_id, confidence FROM edges "
            "WHERE dst_id IS NOT NULL AND kind IN ('calls', 'instantiates')"
        )
        return [(row["src_id"], row["dst_id"], row["confidence"]) for row in rows]

    def all_symbol_ids(self) -> list[int]:
        return [row["id"] for row in self.conn.execute("SELECT id FROM symbols")]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _to_symbol(row: sqlite3.Row) -> Symbol:
    return Symbol(
        id=row["id"],
        name=row["name"],
        qualname=row["qualname"],
        kind=row["kind"],
        lang=row["lang"],
        path=row["path"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        signature=row["signature"],
        docstring=row["docstring"],
        rank=row["rank"],
    )


def _placeholders(items: Sequence[object]) -> str:
    return ", ".join("?" * len(items))


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 expression.

    Users (and agents) type `who_calls`, `"exact phrase"` and `foo OR bar`
    interchangeably; quoting every token keeps FTS5 from choking on syntax
    characters while still allowing prefix search.
    """
    tokens = [t for t in query.replace(":", " ").split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"*' if t.isidentifier() else f'"{t}"' for t in tokens)
