#!/usr/bin/env python
"""Benchmark indexing throughput and query latency on real repositories.

Reports cold index time, warm (no-op) reindex time, database size and median
query latency, so the numbers in the README are reproducible rather than vibes.

Usage:
    python scripts/bench.py                       # benchmark this repo
    python scripts/bench.py ~/code/django ~/code/gin
    python scripts/bench.py --clone              # clone the README's repos first
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from cartograph.indexer.pipeline import index_repo
from cartograph.service import Cartograph

#: The repositories quoted in the README, chosen for size and language spread.
REFERENCE_REPOS = [
    ("django", "https://github.com/django/django"),
    ("gin", "https://github.com/gin-gonic/gin"),
    ("flask", "https://github.com/pallets/flask"),
]

QUERY_SAMPLES = 5


def clone_reference_repos(into: Path) -> list[Path]:
    into.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, url in REFERENCE_REPOS:
        target = into / name
        if not target.exists():
            print(f"cloning {name}…", file=sys.stderr)
            subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", url, str(target)], check=True
            )
        paths.append(target)
    return paths


def median_ms(fn: Callable[[], object], samples: int = QUERY_SAMPLES) -> float:
    fn()  # warm the page cache and any prepared statements
    timings = []
    for _ in range(samples):
        start = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - start) * 1000)
    return statistics.median(timings)


def benchmark(path: Path, workdir: Path) -> dict[str, object] | None:
    db = workdir / f"{path.name}.db"
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db) + suffix)
        if candidate.exists():
            candidate.unlink()

    start = time.perf_counter()
    result = index_repo(path, db_path=db, force=True)
    cold = time.perf_counter() - start
    if result.stats.files_indexed == 0:
        print(f"skipping {path.name}: nothing indexable", file=sys.stderr)
        return None

    start = time.perf_counter()
    index_repo(path, db_path=db)
    warm = time.perf_counter() - start

    with Cartograph.load(db) as graph:
        stats = graph.stats()
        # Anchor queries on the repo's own busiest symbol so depth actually bites.
        anchor = graph.store.hotspots(limit=1)[0][0]
        latency = {
            "find_symbol": median_ms(lambda: graph.find_symbol(anchor.name, limit=20)),
            "who_calls d3": median_ms(lambda: graph.store.callers(anchor.id, depth=3, limit=50)),
            "blast_radius": median_ms(lambda: graph.blast_radius(anchor.qualname)),
            "architecture": median_ms(lambda: graph.architecture()),
        }

    return {
        "repo": path.name,
        "files": stats["files"],
        "kloc": stats["lines"] / 1000,
        "symbols": stats["symbols"],
        "edges": stats["edges"],
        "cold": cold,
        "warm": warm,
        "db_mb": db.stat().st_size / 1e6,
        "internal": stats["internal_resolution_rate"] * 100,
        "langs": stats["by_lang"],
        "latency": latency,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="*", type=Path, help="repositories to benchmark")
    parser.add_argument(
        "--clone", action="store_true", help="clone the README's reference repos first"
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(tempfile.gettempdir()) / "cartograph-bench",
        help="where to clone reference repos",
    )
    args = parser.parse_args()

    targets: list[Path] = list(args.repos)
    if args.clone:
        targets = clone_reference_repos(args.cache) + targets
    if not targets:
        targets = [Path.cwd()]

    workdir = Path(tempfile.mkdtemp(prefix="cartograph-bench-"))
    try:
        rows = [row for target in targets if (row := benchmark(target.resolve(), workdir))]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not rows:
        return 1

    print()
    print(
        f"| {'Repo':<12} | {'Files':>6} | {'KLOC':>6} | {'Symbols':>8} | {'Edges':>8} "
        f"| {'Cold':>6} | {'Warm':>6} | {'DB':>8} | {'Internal':>8} |"
    )
    print(
        f"|{'-' * 14}|{'-' * 8}|{'-' * 8}|{'-' * 10}|{'-' * 10}|"
        f"{'-' * 8}|{'-' * 8}|{'-' * 10}|{'-' * 10}|"
    )
    for r in rows:
        print(
            f"| {r['repo']:<12} | {r['files']:>6,} | {r['kloc']:>6.0f} | {r['symbols']:>8,} "
            f"| {r['edges']:>8,} | {r['cold']:>5.2f}s | {r['warm']:>5.2f}s "
            f"| {r['db_mb']:>6.1f}MB | {r['internal']:>7.1f}% |"
        )

    print()
    labels = list(rows[0]["latency"])  # type: ignore[arg-type]
    print(f"| {'Repo':<12} | " + " | ".join(f"{label:>14}" for label in labels) + " |")
    print(f"|{'-' * 14}|" + "|".join(f"{'-' * 16}" for _ in labels) + "|")
    for r in rows:
        cells = " | ".join(
            f"{value:>12.1f}ms"
            for value in r["latency"].values()  # type: ignore[union-attr]
        )
        print(f"| {r['repo']:<12} | {cells} |")

    print()
    for r in rows:
        print(f"{r['repo']}: languages {r['langs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
