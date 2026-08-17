"""Command line interface.

The CLI exists for two reasons: it is how you build and inspect an index, and it
is how you debug what the agent will see. Every query command has a `--md` flag
that prints the *exact* Markdown the matching MCP tool would return, so the
agent's view is never a black box.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from cartograph import __version__, views
from cartograph.indexer.languages import SUPPORTED_LANGUAGES
from cartograph.indexer.pipeline import default_db_path, index_repo
from cartograph.service import BROAD, PRECISE, Cartograph, IndexNotFoundError

app = typer.Typer(
    name="cartograph",
    help="Agent-native code intelligence: turn any repo into a queryable code graph.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

DbOption = Annotated[
    Path | None,
    typer.Option("--db", help="Index location (default: <repo>/.cartograph/cartograph.db)"),
]
MdOption = Annotated[
    bool, typer.Option("--md", help="Print the exact Markdown the MCP tool returns")
]


def _load(db: Path | None) -> Cartograph:
    try:
        return Cartograph.load(db)
    except IndexNotFoundError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc


def _resolve(graph: Cartograph, selector: str) -> None:
    """Warn when a selector was ambiguous, so results are never silently wrong."""
    matches = graph.candidates(selector, limit=4)
    if len(matches) > 1:
        others = ", ".join(f"[dim]{m.qualname}[/dim]" for m in matches[1:4])
        err.print(
            f"[yellow]note[/yellow] {len(matches)} matches; using the top-ranked. Others: {others}"
        )


@app.command()
def index(
    path: Annotated[Path, typer.Argument(help="Repository root to index")] = Path(),
    db: DbOption = None,
    lang: Annotated[
        list[str] | None,
        typer.Option("--lang", "-l", help=f"Restrict languages ({', '.join(SUPPORTED_LANGUAGES)})"),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Full reindex, ignoring hashes")] = False,
    jobs: Annotated[int, typer.Option("--jobs", "-j", min=1, max=64, help="Parser threads")] = 8,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print the summary")] = False,
) -> None:
    """Build or update the code graph for a repository."""
    root = path.resolve()
    if not root.is_dir():
        err.print(f"[red]{root} is not a directory[/red]")
        raise typer.Exit(code=2)

    languages = frozenset(lang) if lang else None
    if languages and (unknown := languages - set(SUPPORTED_LANGUAGES)):
        err.print(f"[red]unknown language(s): {', '.join(sorted(unknown))}[/red]")
        raise typer.Exit(code=2)

    status = console.status("[bold]indexing[/bold]") if not quiet else None

    def progress(stage: str, done: int, total: int) -> None:
        if status is not None:
            suffix = f" {done}/{total}" if total else ""
            status.update(f"[bold]{stage}[/bold]{suffix}")

    if status is not None:
        status.start()
    try:
        result = index_repo(
            root,
            db_path=db,
            languages=languages,
            force=force,
            jobs=jobs,
            progress=progress if status else None,
        )
    finally:
        if status is not None:
            status.stop()

    stats = result.stats
    console.print(
        f"[green]indexed[/green] [bold]{root.name}[/bold] in {stats.duration_s:.2f}s "
        f"→ [cyan]{result.db_path}[/cyan]"
    )
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_row(
        "files",
        f"{stats.files_indexed} indexed, {stats.files_skipped} unchanged,"
        f" {stats.files_removed} removed, {stats.files_scanned} scanned",
    )
    table.add_row("symbols", str(stats.symbols))
    table.add_row(
        "edges",
        f"{stats.edges} · {stats.internal_resolution_rate * 100:.1f}% of in-repo call sites "
        f"resolved · {stats.external_edges} external"
        + (
            f" · {result.resolution.module_level_dropped} module-level refs skipped"
            if result.resolution.module_level_dropped
            else ""
        ),
    )
    table.add_row("languages", ", ".join(f"{k} {v}" for k, v in stats.by_lang.items()) or "—")
    if stats.files_indexed:
        table.add_row(
            "throughput", f"{stats.files_indexed / max(stats.duration_s, 1e-6):.0f} files/s"
        )
    console.print(table)

    if result.resolution.by_reason:
        console.print(
            "[dim]resolution rules:[/dim] "
            + "  ".join(
                f"{k}={v}"
                for k, v in sorted(result.resolution.by_reason.items(), key=lambda kv: -kv[1])
            )
        )

    if result.failures:
        err.print(f"[yellow]{len(result.failures)} file(s) failed to parse:[/yellow]")
        for failed_path, reason in result.failures[:10]:
            err.print(f"  [dim]{failed_path}[/dim] — {reason}")
        if len(result.failures) > 10:
            err.print(f"  [dim]… and {len(result.failures) - 10} more[/dim]")


@app.command("find")
def find_cmd(
    name: Annotated[str, typer.Argument(help="Symbol name (exact or partial)")],
    kind: Annotated[str | None, typer.Option("--kind", "-k")] = None,
    lang: Annotated[str | None, typer.Option("--lang", "-l")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    db: DbOption = None,
    md: MdOption = False,
) -> None:
    """Find where a symbol is defined."""
    with _load(db) as graph:
        symbols = graph.find_symbol(name, kind=kind, lang=lang, limit=limit)
        if md:
            console.print(views.symbols_table(symbols))
            return
        if not symbols:
            err.print("[yellow]no matches[/yellow] — try `cartograph search`")
            raise typer.Exit(code=1)
        table = Table("symbol", "kind", "location", "rank", box=None)
        for sym in symbols:
            table.add_row(sym.qualname, sym.kind, sym.location, f"{sym.rank:.4f}")
        console.print(table)


@app.command("search")
def search_cmd(
    query: Annotated[str, typer.Argument(help="Free text over names, signatures, docstrings")],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    db: DbOption = None,
    md: MdOption = False,
) -> None:
    """Full-text search over the symbol table."""
    with _load(db) as graph:
        symbols = graph.search(query, limit=limit)
        if md:
            console.print(views.symbols_table(symbols))
            return
        table = Table("symbol", "kind", "location", box=None)
        for sym in symbols:
            table.add_row(sym.qualname, sym.kind, sym.location)
        console.print(table)


@app.command("show")
def show_cmd(
    symbol: Annotated[str, typer.Argument(help="Symbol name, qualname, `path:name`, or id")],
    source: Annotated[
        bool, typer.Option("--source", "-s", help="Include the definition body")
    ] = False,
    depth: Annotated[int, typer.Option("--depth", "-d", min=1, max=4)] = 1,
    db: DbOption = None,
) -> None:
    """Show a symbol with its signature, doc and immediate neighbourhood."""
    with _load(db) as graph:
        _resolve(graph, symbol)
        try:
            detail = graph.symbol_detail(symbol, depth=depth, include_source=source)
        except LookupError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(views.symbol_detail_md(detail))


@app.command("callers")
def callers_cmd(
    symbol: Annotated[str, typer.Argument()],
    depth: Annotated[int, typer.Option("--depth", "-d", min=1, max=6)] = 2,
    min_confidence: Annotated[
        float, typer.Option("--min-confidence", "-c", min=0.0, max=1.0)
    ] = PRECISE,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    db: DbOption = None,
) -> None:
    """Who calls this symbol (reverse call tree)."""
    with _load(db) as graph:
        _resolve(graph, symbol)
        try:
            root, neighbours = graph.who_calls(
                symbol, depth=depth, min_confidence=min_confidence, limit=limit
            )
        except LookupError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(views.neighbour_tree(root, neighbours, direction="up"))


@app.command("calls")
def calls_cmd(
    symbol: Annotated[str, typer.Argument()],
    depth: Annotated[int, typer.Option("--depth", "-d", min=1, max=6)] = 2,
    min_confidence: Annotated[
        float, typer.Option("--min-confidence", "-c", min=0.0, max=1.0)
    ] = PRECISE,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    db: DbOption = None,
) -> None:
    """What this symbol calls (forward call tree)."""
    with _load(db) as graph:
        _resolve(graph, symbol)
        try:
            root, neighbours = graph.calls(
                symbol, depth=depth, min_confidence=min_confidence, limit=limit
            )
        except LookupError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(views.neighbour_tree(root, neighbours, direction="down"))


@app.command("blast")
def blast_cmd(
    target: Annotated[str, typer.Argument(help="File path or symbol name")],
    depth: Annotated[int, typer.Option("--depth", "-d", min=1, max=6)] = 3,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 60,
    db: DbOption = None,
) -> None:
    """Impact analysis: what a change here could break, and which tests to run."""
    with _load(db) as graph:
        try:
            radius = graph.blast_radius(target, depth=depth, min_confidence=BROAD, limit=limit)
        except LookupError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(views.blast_radius_md(radius))


@app.command("related")
def related_cmd(
    symbol: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 15,
    db: DbOption = None,
) -> None:
    """Structurally related symbols (personalized PageRank on the call graph)."""
    with _load(db) as graph:
        try:
            root, symbols = graph.related(symbol, limit=limit)
        except LookupError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[bold]related to[/bold] {root.qualname}")
        console.print(views.symbols_table(symbols))


@app.command("file")
def file_cmd(
    path: Annotated[str, typer.Argument(help="File path or a distinctive part of one")],
    db: DbOption = None,
) -> None:
    """Outline one file: definitions, imports, importers."""
    with _load(db) as graph:
        try:
            summary = graph.file_summary(path)
        except LookupError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(views.file_summary_md(summary))


@app.command("arch")
def arch_cmd(
    mermaid: Annotated[bool, typer.Option("--mermaid/--no-mermaid")] = True,
    min_weight: Annotated[
        int, typer.Option("--min-weight", min=1, help="Hide weak module edges")
    ] = 1,
    db: DbOption = None,
) -> None:
    """Architecture overview: modules, layers, cycles, hotspots, entry points."""
    with _load(db) as graph:
        arch = graph.architecture(min_weight=min_weight)
        console.print(views.architecture_md(arch, mermaid=mermaid, budget=6000))


@app.command("cycles")
def cycles_cmd(db: DbOption = None) -> None:
    """List import cycles. Exits non-zero if any exist -- usable as a CI gate."""
    with _load(db) as graph:
        arch = graph.architecture()
        if not arch.cycles:
            console.print("[green]no import cycles[/green]")
            return
        console.print(f"[red]{len(arch.cycles)} import cycle(s)[/red]")
        for cycle in arch.cycles:
            console.print("  " + " → ".join(cycle))
        raise typer.Exit(code=1)


@app.command("stats")
def stats_cmd(db: DbOption = None) -> None:
    """Index health and the edge-resolution breakdown by rule."""
    with _load(db) as graph:
        console.print(views.stats_md(graph.stats()))


@app.command("serve")
def serve_cmd(
    path: Annotated[Path, typer.Argument(help="Repository root")] = Path(),
    db: DbOption = None,
    auto_index: Annotated[
        bool, typer.Option("--auto-index/--no-auto-index", help="Index first if no index exists")
    ] = True,
) -> None:
    """Run the MCP server over stdio (this is what an agent connects to)."""
    import os

    root = path.resolve()
    db_path = db or default_db_path(root)
    if auto_index and not db_path.exists():
        err.print(f"[dim]no index at {db_path}; indexing {root}…[/dim]")
        index_repo(root, db_path=db_path)
    os.environ["CARTOGRAPH_DB"] = str(db_path)
    os.environ["CARTOGRAPH_ROOT"] = str(root)

    from cartograph.mcp_server.server import main as serve_main

    serve_main()


@app.command("version")
def version_cmd() -> None:
    """Print the version."""
    console.print(f"cartograph {__version__}")


if __name__ == "__main__":
    app()
