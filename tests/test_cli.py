"""CLI smoke tests and concurrency guarantees.

The CLI is also the debugging surface for the MCP server (`--md` prints exactly
what a tool returns), so a broken command means a blind agent integration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cartograph.cli import app
from cartograph.graph.store import GraphStore
from cartograph.indexer.pipeline import IndexResult, index_repo

runner = CliRunner()


@pytest.fixture
def db(indexed: IndexResult) -> str:
    return str(indexed.db_path)


# ---------------------------------------------------------------------------
# indexing
# ---------------------------------------------------------------------------


def test_index_command(scratch_repo: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["index", str(scratch_repo), "--db", str(tmp_path / "g.db"), "-q"])
    assert result.exit_code == 0, result.output
    assert "indexed" in result.output
    assert (tmp_path / "g.db").exists()


def test_index_rejects_a_missing_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["index", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_index_rejects_an_unknown_language(scratch_repo: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["index", str(scratch_repo), "--db", str(tmp_path / "g.db"), "--lang", "cobol"]
    )
    assert result.exit_code == 2
    assert "unknown language" in result.output


def test_index_reports_throughput(scratch_repo: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["index", str(scratch_repo), "--db", str(tmp_path / "g.db")])
    assert "files/s" in result.output


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def test_find(db: str) -> None:
    result = runner.invoke(app, ["find", "normalize", "--db", db])
    assert result.exit_code == 0
    assert "normalize" in result.output


def test_find_missing_symbol_exits_nonzero(db: str) -> None:
    result = runner.invoke(app, ["find", "zzz_not_here", "--db", db])
    assert result.exit_code == 1


def test_find_md_matches_the_mcp_output(db: str) -> None:
    """`--md` is the contract that the CLI shows what the agent sees."""
    result = runner.invoke(app, ["find", "normalize", "--db", db, "--md"])
    assert result.exit_code == 0
    assert "match" in result.output


def test_show(db: str) -> None:
    result = runner.invoke(app, ["show", "app.helpers:normalize", "--db", db])
    assert result.exit_code == 0
    assert "normalize" in result.output


def test_show_with_source(db: str) -> None:
    result = runner.invoke(app, ["show", "app.helpers:normalize", "--db", db, "--source"])
    assert "def normalize" in result.output


def test_callers_and_calls(db: str) -> None:
    up = runner.invoke(app, ["callers", "app.helpers:truncate", "--db", db])
    assert up.exit_code == 0
    assert "Engine.run" in up.output

    down = runner.invoke(app, ["calls", "app.core:Engine.run", "--db", db])
    assert down.exit_code == 0
    assert "truncate" in down.output


def test_blast(db: str) -> None:
    result = runner.invoke(app, ["blast", "src/app/core.py", "--db", db])
    assert result.exit_code == 0
    assert "test_core.py" in result.output


def test_arch(db: str) -> None:
    result = runner.invoke(app, ["arch", "--db", db])
    assert result.exit_code == 0
    assert "Architecture overview" in result.output


def test_arch_without_mermaid(db: str) -> None:
    result = runner.invoke(app, ["arch", "--db", db, "--no-mermaid"])
    assert "mermaid" not in result.output


def test_cycles_exits_nonzero_when_cycles_exist(db: str) -> None:
    """This is the CI-gate contract: a cycle must fail the command."""
    result = runner.invoke(app, ["cycles", "--db", db])
    assert result.exit_code == 1
    assert "cycle" in result.output


def test_cycles_exits_zero_on_a_clean_repo(tmp_path: Path) -> None:
    repo = tmp_path / "clean"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "a.py").write_text("def a():\n    pass\n")
    (repo / "pkg" / "b.py").write_text("from pkg.a import a\n\ndef b():\n    a()\n")
    dbp = tmp_path / "g.db"
    index_repo(repo, db_path=dbp)

    result = runner.invoke(app, ["cycles", "--db", str(dbp)])
    assert result.exit_code == 0
    assert "no import cycles" in result.output


def test_stats(db: str) -> None:
    result = runner.invoke(app, ["stats", "--db", db])
    assert result.exit_code == 0
    assert "Index stats" in result.output


def test_file(db: str) -> None:
    result = runner.invoke(app, ["file", "core.py", "--db", db])
    assert result.exit_code == 0
    assert "Engine" in result.output


def test_related(db: str) -> None:
    result = runner.invoke(app, ["related", "app.helpers:normalize", "--db", db])
    assert result.exit_code == 0


def test_search(db: str) -> None:
    result = runner.invoke(app, ["search", "whitespace", "--db", db])
    assert result.exit_code == 0


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "cartograph" in result.output


def test_query_without_an_index_is_a_clear_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["find", "x", "--db", str(tmp_path / "absent.db")])
    assert result.exit_code == 2
    assert "cartograph index" in result.output


def test_ambiguous_selector_warns(db: str) -> None:
    """`describe` exists on two classes in the fixture; the user must be told."""
    result = runner.invoke(app, ["show", "describe", "--db", db])
    assert result.exit_code == 0
    assert "matches" in result.output


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_reading_while_indexing_does_not_lock(scratch_repo: Path, tmp_path: Path) -> None:
    """Regression: opening an index used to run DDL and take an exclusive lock,
    so a query failed with "database is locked" whenever an indexer held the db --
    precisely the situation when an agent is querying a repo being reindexed."""
    dbp = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=dbp)

    # Hold a reader open, exactly as a long-lived MCP server would.
    with GraphStore.open(dbp, create=False) as reader:
        assert reader.find_symbols("normalize")
        # ...and reindex underneath it.
        index_repo(scratch_repo, db_path=dbp, force=True)
        # The reader must still work afterwards.
        assert reader.find_symbols("normalize")


def test_opening_an_index_writes_nothing(scratch_repo: Path, tmp_path: Path) -> None:
    """A read-only open must not bump mtime or change the file."""
    dbp = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=dbp)
    before = dbp.read_bytes()

    with GraphStore.open(dbp, create=False) as store:
        store.find_symbols("normalize")

    assert dbp.read_bytes() == before, "opening for read modified the database"


def test_two_readers_can_coexist(scratch_repo: Path, tmp_path: Path) -> None:
    dbp = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=dbp)
    with GraphStore.open(dbp, create=False) as a, GraphStore.open(dbp, create=False) as b:
        assert a.counts()["symbols"] == b.counts()["symbols"]


def test_schema_is_not_recreated_on_reopen(scratch_repo: Path, tmp_path: Path) -> None:
    dbp = tmp_path / "g.db"
    index_repo(scratch_repo, db_path=dbp)
    conn = sqlite3.connect(dbp)
    version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    conn.close()
    assert version is not None
    with GraphStore.open(dbp, create=False) as store:
        assert store.get_meta("schema_version") == version[0]
