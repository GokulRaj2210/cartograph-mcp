"""The index pipeline: discover -> parse -> store -> resolve -> rank.

Incremental by content hash. A file is reparsed only when its sha256 moved, but
*resolution and ranking always run over the whole graph* -- see the note in
schema.sql. That split is the point: parsing is the expensive part and is
avoided; resolution is cheap and must be global to stay correct.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from cartograph.graph import resolver
from cartograph.graph.algorithms import pagerank
from cartograph.graph.store import DEFAULT_DB_NAME, GraphStore
from cartograph.indexer.extract import extract_file
from cartograph.indexer.languages import adapter_for_path
from cartograph.indexer.walker import discover
from cartograph.models import IndexStats, ParsedFile

ProgressFn = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class IndexResult:
    stats: IndexStats
    resolution: resolver.ResolutionReport
    db_path: Path
    #: (path, reason) for files that could not be parsed. Always surfaced to the
    #: caller -- a quietly skipped file is an invisible hole in the graph.
    failures: tuple[tuple[str, str], ...] = ()


def default_db_path(root: Path) -> Path:
    return root / ".cartograph" / DEFAULT_DB_NAME


def index_repo(
    root: Path,
    *,
    db_path: Path | None = None,
    languages: frozenset[str] | None = None,
    force: bool = False,
    jobs: int = 8,
    progress: ProgressFn | None = None,
) -> IndexResult:
    root = root.resolve()
    db_path = db_path or default_db_path(root)
    started = time.perf_counter()
    stats = IndexStats()

    paths = discover(root, languages)
    stats.files_scanned = len(paths)

    with GraphStore.open(db_path) as store:
        known = {} if force else store.file_hashes()
        if force:
            store.delete_files(list(store.file_hashes()))

        rel_paths = [p.relative_to(root).as_posix() for p in paths]
        present = set(rel_paths)

        removed = [path for path in known if path not in present]
        if removed:
            store.delete_files(removed)
            stats.files_removed = len(removed)

        todo: list[tuple[Path, str]] = []
        for path, rel in zip(paths, rel_paths, strict=True):
            digest = _sha256(path)
            if digest is None:
                continue
            if known.get(rel) == digest:
                stats.files_skipped += 1
                continue
            todo.append((path, rel))

        if todo:
            store.delete_files([rel for _, rel in todo if rel in known])

        parsed_files, failures = _parse_all(todo, jobs=jobs, progress=progress)

        with store.transaction():
            for parsed in parsed_files:
                store.insert_file(parsed)
                stats.files_indexed += 1
                stats.symbols += len(parsed.symbols)
                stats.imports += len(parsed.imports)
                stats.by_lang[parsed.lang] = stats.by_lang.get(parsed.lang, 0) + 1

        # Resolution and ranking are global, so they are skipped only when the
        # inputs are provably unchanged: no file was added, reparsed or removed
        # means `refs` and `symbols` are bit-identical, and resolution is a pure
        # function of those two tables. This keeps a no-op reindex ~instant
        # (7.5s -> ~0.4s on Django) without ever serving a stale graph.
        graph_inputs_changed = bool(parsed_files) or stats.files_removed > 0
        if graph_inputs_changed:
            if progress:
                progress("resolving imports", 0, 0)
            with store.transaction():
                resolver.resolve_imports(store)

            if progress:
                progress("resolving call graph", 0, 0)
            with store.transaction():
                report = resolver.resolve_references(store)

            if progress:
                progress("ranking symbols", 0, 0)
            with store.transaction():
                ranks = pagerank(store.call_edges(), store.all_symbol_ids())
                store.set_ranks(ranks)
        else:
            report = resolver.report_from_store(store)

        counts = store.counts()
        stats.symbols = counts["symbols"]
        stats.imports = counts["imports"]
        stats.edges = report.edges
        stats.resolved_edges = report.resolved
        stats.external_edges = counts["external_edges"]
        stats.by_lang = store.counts_by("lang")

        store.set_meta("root", str(root))
        store.set_meta("indexed_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        store.set_meta("languages", ",".join(sorted(stats.by_lang)))
        if graph_inputs_changed:
            store.optimize()  # ANALYZE + FTS merge; pointless if nothing moved
        else:
            store.conn.commit()

    stats.duration_s = time.perf_counter() - started
    return IndexResult(stats=stats, resolution=report, db_path=db_path, failures=tuple(failures))


def _parse_all(
    todo: list[tuple[Path, str]], *, jobs: int, progress: ProgressFn | None
) -> tuple[list[ParsedFile], list[tuple[str, str]]]:
    """Parse in a thread pool: py-tree-sitter drops the GIL inside `parse`, so
    threads genuinely overlap here and we avoid the cost of pickling ASTs
    across processes."""
    failures: list[tuple[str, str]] = []
    if not todo:
        return [], failures
    total = len(todo)
    out: list[ParsedFile] = []

    def work(item: tuple[Path, str]) -> ParsedFile | None:
        path, rel = item
        adapter = adapter_for_path(path)
        if adapter is None:
            return None
        try:
            source = path.read_bytes()
        except OSError as exc:
            failures.append((rel, f"read: {exc}"))
            return None
        try:
            return extract_file(rel, source, adapter)
        except Exception as exc:
            # Recorded, never swallowed: a silent skip here once hid a broken
            # tree-sitter query that dropped an entire language from the index.
            failures.append((rel, f"{type(exc).__name__}: {exc}"))
            return None

    if jobs <= 1:
        results = (work(item) for item in todo)
        for done, parsed in enumerate(results, start=1):
            if parsed is not None:
                out.append(parsed)
            if progress:
                progress("parsing", done, total)
        return out, failures

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for done, parsed in enumerate(pool.map(work, todo), start=1):
            if parsed is not None:
                out.append(parsed)
            if progress and (done % 25 == 0 or done == total):
                progress("parsing", done, total)
    return out, failures


def _sha256(path: Path) -> str | None:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
