"""Shared fixtures.

The sample repo under ``tests/fixtures/sample_repo`` is a deliberately small
multi-language codebase with *known* structure: a three-layer Python package, a
TypeScript pair, a Go package, and one intentional import cycle. Assertions
throughout the suite reference those known relationships by name, so a
regression in extraction or resolution shows up as a specific failed edge rather
than a moved number.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from cartograph.indexer.pipeline import IndexResult, index_repo
from cartograph.service import Cartograph

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture(scope="session")
def sample_repo() -> Path:
    assert FIXTURE_REPO.is_dir(), f"missing fixture repo at {FIXTURE_REPO}"
    return FIXTURE_REPO


@pytest.fixture(scope="session")
def indexed(sample_repo: Path, tmp_path_factory: pytest.TempPathFactory) -> IndexResult:
    """Index the fixture repo once per session (it is cheap, but not free)."""
    db = tmp_path_factory.mktemp("index") / "cartograph.db"
    return index_repo(sample_repo, db_path=db)


@pytest.fixture
def graph(indexed: IndexResult) -> Iterator[Cartograph]:
    with Cartograph.load(indexed.db_path) as cg:
        yield cg


@pytest.fixture
def scratch_repo(sample_repo: Path, tmp_path: Path) -> Path:
    """A writable copy of the fixture repo, for incremental-indexing tests."""
    target = tmp_path / "repo"
    shutil.copytree(sample_repo, target)
    return target


@pytest.fixture
def anyio_backend() -> str:
    """Run `@pytest.mark.anyio` tests on asyncio only (the MCP SDK's default)."""
    return "asyncio"
