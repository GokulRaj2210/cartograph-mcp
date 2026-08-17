"""Source-file discovery.

Prefers ``git ls-files`` when the target is a git repo: it is faster than an
os.walk and it already implements the full ``.gitignore`` semantics (nested
ignore files, negations, ``core.excludesFile``) that a hand-rolled matcher
always gets subtly wrong. Falls back to a filtered walk otherwise.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

from cartograph.indexer.languages import adapter_for_path

#: Directories skipped even when git tracks them (vendored or generated code).
DEFAULT_IGNORES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "vendor",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        "target",
        "out",
        ".next",
        ".nuxt",
        ".svelte-kit",
        "site-packages",
        "third_party",
        ".idea",
        ".vscode",
        "coverage",
        ".cartograph",
    }
)

#: Generated/bundled files: huge, machine-written, and useless as graph nodes.
SKIP_SUFFIXES = (".min.js", ".min.mjs", ".bundle.js", "_pb2.py", "_pb2.pyi", ".pb.go", "_pb.js")

MAX_FILE_BYTES = 1_500_000


def discover(
    root: Path,
    languages: frozenset[str] | None = None,
    max_bytes: int = MAX_FILE_BYTES,
) -> list[Path]:
    """Return the source files under ``root`` that Cartograph can index."""
    root = root.resolve()
    candidates = _git_files(root) if _is_git_repo(root) else _walk(root)

    out: list[Path] = []
    for path in candidates:
        rel = path.relative_to(root).as_posix()
        if any(part in DEFAULT_IGNORES for part in Path(rel).parts):
            continue
        if rel.endswith(SKIP_SUFFIXES):
            continue
        adapter = adapter_for_path(path)
        if adapter is None or (languages is not None and adapter.name not in languages):
            continue
        try:
            if path.stat().st_size > max_bytes or not path.is_file():
                continue
        except OSError:
            continue
        out.append(path)
    return sorted(out)


def _is_git_repo(root: Path) -> bool:
    return (root / ".git").exists()


def _git_files(root: Path) -> Iterator[Path]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        yield from _walk(root)
        return
    if proc.returncode != 0:
        yield from _walk(root)
        return
    for line in proc.stdout.splitlines():
        if line:
            yield root / line


def _walk(root: Path) -> Iterator[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            # Skip dotfiles and dot-directories outright; nothing we index is hidden.
            if entry.name.startswith(".") and (entry.name in DEFAULT_IGNORES or entry.is_dir()):
                continue
            if entry.is_dir():
                if entry.name not in DEFAULT_IGNORES:
                    stack.append(entry)
            else:
                yield entry
