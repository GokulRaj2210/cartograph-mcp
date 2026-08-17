"""Incremental indexing: fast when nothing changed, never stale when it did.

The design claim under test: parsing is skipped per-file by content hash, but
edge resolution is recomputed globally on every run. That combination is what
makes "reindex after every edit" both cheap and trustworthy. If resolution were
also incremental, an edge could keep pointing at a symbol that had moved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cartograph.graph.store import GraphStore
from cartograph.indexer.pipeline import index_repo
from cartograph.service import Cartograph


def edge_names(db: Path) -> set[tuple[str, str]]:
    conn = sqlite3.connect(db)
    rows = set(
        conn.execute("SELECT s.qualname, e.dst_name FROM edges e JOIN symbols s ON s.id = e.src_id")
    )
    conn.close()
    return rows  # type: ignore[return-value]


def qualnames(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    rows = {row[0] for row in conn.execute("SELECT qualname FROM symbols")}
    conn.close()
    return rows


def test_unchanged_files_are_skipped(scratch_repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    first = index_repo(scratch_repo, db_path=db)
    assert first.stats.files_indexed > 0
    assert first.stats.files_skipped == 0

    second = index_repo(scratch_repo, db_path=db)
    assert second.stats.files_indexed == 0
    assert second.stats.files_skipped == first.stats.files_indexed


def test_graph_is_identical_after_a_noop_reindex(scratch_repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=db)
    before = (qualnames(db), edge_names(db))
    index_repo(scratch_repo, db_path=db)
    assert (qualnames(db), edge_names(db)) == before


def test_editing_a_file_reparses_only_that_file(scratch_repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    total = index_repo(scratch_repo, db_path=db).stats.files_indexed

    target = scratch_repo / "src" / "app" / "helpers.py"
    target.write_text(
        target.read_text() + "\n\ndef added_helper() -> None:\n    normalize('x')\n",
        encoding="utf-8",
    )
    result = index_repo(scratch_repo, db_path=db)

    assert result.stats.files_indexed == 1
    assert result.stats.files_skipped == total - 1
    assert "app.helpers:added_helper" in qualnames(db)


def test_removing_a_symbol_removes_it_from_the_graph(scratch_repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=db)
    assert "app.helpers:truncate" in qualnames(db)

    target = scratch_repo / "src" / "app" / "helpers.py"
    target.write_text("def normalize(text):\n    return text.strip()\n", encoding="utf-8")
    index_repo(scratch_repo, db_path=db)

    assert "app.helpers:truncate" not in qualnames(db)


def test_edges_never_point_at_a_deleted_symbol(scratch_repo: Path, tmp_path: Path) -> None:
    """The invariant global re-resolution exists to guarantee."""
    db = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=db)

    (scratch_repo / "src" / "app" / "helpers.py").write_text(
        "def normalize(text):\n    return text\n", encoding="utf-8"
    )
    index_repo(scratch_repo, db_path=db)

    conn = sqlite3.connect(db)
    orphans = conn.execute(
        "SELECT COUNT(*) FROM edges e "
        "WHERE e.dst_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM symbols s WHERE s.id = e.dst_id)"
    ).fetchone()[0]
    dangling_src = conn.execute(
        "SELECT COUNT(*) FROM edges e "
        "WHERE NOT EXISTS (SELECT 1 FROM symbols s WHERE s.id = e.src_id)"
    ).fetchone()[0]
    conn.close()
    assert orphans == 0
    assert dangling_src == 0


def test_deleting_a_file_removes_its_rows(scratch_repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=db)

    (scratch_repo / "web" / "client.ts").unlink()
    result = index_repo(scratch_repo, db_path=db)

    assert result.stats.files_removed == 1
    assert not any(q.startswith("web/client:") for q in qualnames(db))

    conn = sqlite3.connect(db)
    leftover = conn.execute(
        "SELECT COUNT(*) FROM refs WHERE file_id NOT IN (SELECT id FROM files)"
    ).fetchone()[0]
    conn.close()
    assert leftover == 0, "refs outlived their file"


def test_moving_a_symbol_between_files_repoints_edges(tmp_path: Path) -> None:
    """A symbol moving house is the classic stale-edge trap."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "old.py").write_text("def moved():\n    pass\n")
    (repo / "pkg" / "new.py").write_text("")
    (repo / "pkg" / "user.py").write_text("from pkg.old import moved\n\ndef go():\n    moved()\n")
    db = tmp_path / "g.db"
    index_repo(repo, db_path=db)

    def target_of_go() -> str | None:
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT d.qualname FROM edges e "
            "JOIN symbols s ON s.id = e.src_id "
            "LEFT JOIN symbols d ON d.id = e.dst_id "
            "WHERE s.name = 'go' AND e.dst_name = 'moved'"
        ).fetchone()
        conn.close()
        return row[0] if row else None

    assert target_of_go() == "pkg.old:moved"

    # Move the definition and update the import.
    (repo / "pkg" / "old.py").write_text("")
    (repo / "pkg" / "new.py").write_text("def moved():\n    pass\n")
    (repo / "pkg" / "user.py").write_text("from pkg.new import moved\n\ndef go():\n    moved()\n")
    index_repo(repo, db_path=db)

    assert target_of_go() == "pkg.new:moved"


def test_force_rebuilds_everything(scratch_repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    first = index_repo(scratch_repo, db_path=db)
    forced = index_repo(scratch_repo, db_path=db, force=True)
    assert forced.stats.files_indexed == first.stats.files_indexed
    assert forced.stats.files_skipped == 0


def test_ranks_are_recomputed_after_an_edit(scratch_repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=db)
    with Cartograph.load(db) as graph:
        before = graph.find_symbol("truncate")[0].rank

    # Add ten new callers of `truncate`; its PageRank must rise.
    extra = "\n".join(f"def caller_{i}():\n    return truncate('x')\n" for i in range(10))
    target = scratch_repo / "src" / "app" / "helpers.py"
    target.write_text(target.read_text() + "\n" + extra, encoding="utf-8")
    index_repo(scratch_repo, db_path=db)

    with Cartograph.load(db) as graph:
        after = graph.find_symbol("truncate")[0].rank
    assert after > before


def test_language_filter_restricts_the_index(scratch_repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    result = index_repo(scratch_repo, db_path=db, languages=frozenset({"go"}))
    assert set(result.stats.by_lang) == {"go"}


def test_fts_rows_do_not_leak_after_reindex(scratch_repo: Path, tmp_path: Path) -> None:
    """Contentless FTS5 needs explicit deletes, or the index keeps ghost rowids.

    Note the invariant is *rowid integrity*, not "no hits for the old name": a
    surviving symbol whose docstring mentions `truncate` is a legitimate match.
    """
    db = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=db)

    (scratch_repo / "src" / "app" / "helpers.py").write_text(
        "def normalize(text):\n    return text\n", encoding="utf-8"
    )
    index_repo(scratch_repo, db_path=db)

    conn = sqlite3.connect(db)
    hits = {
        row[0]
        for row in conn.execute("SELECT rowid FROM symbol_fts WHERE symbol_fts MATCH 'truncate'")
    }
    live = {row[0] for row in conn.execute("SELECT id FROM symbols")}
    conn.close()
    assert hits <= live, f"FTS returned rowids with no symbol row: {hits - live}"

    with GraphStore.open(db, create=False) as store:
        # The deleted definition itself must be gone from the symbol table.
        assert store.find_symbols("truncate", exact=True) == []
        # ...and nothing FTS returns may be a dangling reference.
        assert all(sym.name != "truncate" for sym in store.search("truncate"))


def test_index_is_portable_across_processes(scratch_repo: Path, tmp_path: Path) -> None:
    """The db is the whole artifact -- it must be readable with no extra state."""
    db = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=db)
    with Cartograph.load(db) as graph:
        assert graph.stats()["files"] > 0
    with Cartograph.load(db) as reopened:
        assert reopened.find_symbol("normalize")


def test_empty_repo_does_not_crash(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    result = index_repo(repo, db_path=tmp_path / "g.db")
    assert result.stats.files_indexed == 0
    assert result.stats.edges == 0


def test_no_parse_failures_on_the_fixture_repo(scratch_repo: Path, tmp_path: Path) -> None:
    """A silent parse failure once hid an entire missing language."""
    result = index_repo(scratch_repo, db_path=tmp_path / "g.db")
    assert result.failures == ()


def test_noop_reindex_skips_resolution_but_reports_the_same_graph(
    scratch_repo: Path, tmp_path: Path
) -> None:
    """The skip must be invisible: identical stats, identical edges.

    Resolution is skipped only when no file was added, reparsed or removed, so
    `refs` and `symbols` are unchanged and re-running it could not differ. This
    test is what makes that reasoning safe to rely on.
    """
    db = tmp_path / "g.db"
    first = index_repo(scratch_repo, db_path=db)
    before = edge_names(db)

    second = index_repo(scratch_repo, db_path=db)

    assert second.stats.files_indexed == 0
    assert edge_names(db) == before
    assert second.stats.edges == first.stats.edges
    assert second.stats.resolved_edges == first.stats.resolved_edges
    assert second.stats.external_edges == first.stats.external_edges
    assert second.resolution.by_reason == first.resolution.by_reason


def test_noop_reindex_is_much_faster_than_a_cold_one(scratch_repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    cold = index_repo(scratch_repo, db_path=db).stats.duration_s
    warm = index_repo(scratch_repo, db_path=db).stats.duration_s
    assert warm < cold


def test_a_single_edit_still_reresolves_globally(scratch_repo: Path, tmp_path: Path) -> None:
    """Touching one file must repair edges in *other* files that point into it."""
    db = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=db)

    # Rename the target of an edge that originates in core.py.
    helpers = scratch_repo / "src" / "app" / "helpers.py"
    helpers.write_text(
        helpers.read_text().replace("def truncate(", "def shorten("), encoding="utf-8"
    )
    result = index_repo(scratch_repo, db_path=db)

    assert result.stats.files_indexed == 1, "only helpers.py changed"
    # ...yet core.py's edge to the old name must now be unresolved.
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT e.dst_id, e.reason FROM edges e JOIN symbols s ON s.id = e.src_id "
        "WHERE s.qualname = 'app.core:Engine.run' AND e.dst_name = 'truncate'"
    ).fetchone()
    conn.close()
    assert row is not None, "the call site itself should still be recorded"
    assert row[0] is None, "edge must not still resolve to the renamed symbol"
