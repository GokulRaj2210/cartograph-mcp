"""MCP server exposing the code graph as agent tools.

Two deliberate choices shape this file.

**Tool docstrings are prompt engineering, not documentation.** They are the only
thing the model sees when deciding which tool to call, so each one says when to
reach for it *and* when to reach for a different one. Vague descriptions here
show up as an agent that greps instead of querying the graph.

**Every tool returns Markdown, not JSON.** See :mod:`cartograph.views` for why.

Configuration is environment-only so the server can be dropped into any MCP
client config with no wrapper script:

    CARTOGRAPH_DB    explicit path to cartograph.db
    CARTOGRAPH_ROOT  repo root to search upwards from (default: cwd)
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from cartograph import views
from cartograph.service import BROAD, PRECISE, Cartograph, IndexNotFoundError, discover_db

INSTRUCTIONS = """\
Cartograph gives you a structural map of this repository: symbol definitions, the
call graph, the import graph, and impact analysis.

Prefer these tools over grep/file-reading whenever the question is structural:
- "where is X defined?"            -> find_symbol
- "what breaks if I change X?"     -> blast_radius
- "who uses X?"                    -> who_calls
- "what does X depend on?"         -> what_it_calls
- "how is this codebase laid out?" -> architecture_overview (start here on a new repo)
- "what should I read alongside X?" -> related_symbols

Call-graph edges are resolved by name, not by type inference, so each one carries
a confidence and the rule that produced it. Treat anything below 0.5 as a lead to
verify by reading the code, not as fact.
"""

server: MCPServer = MCPServer(
    name="cartograph",
    version="0.1.0",
    title="Cartograph code graph",
    instructions=INSTRUCTIONS,
)

_lock = threading.Lock()
_graph: Cartograph | None = None


def _get() -> Cartograph:
    """Open the index once, lazily, and serialise access to the connection."""
    global _graph
    if _graph is None:
        db_env = os.environ.get("CARTOGRAPH_DB")
        root = Path(os.environ.get("CARTOGRAPH_ROOT", ".")).resolve()
        db_path = Path(db_env) if db_env else discover_db(root)
        _graph = Cartograph.load(db_path, root)
    return _graph


def _reset() -> None:
    """Drop the cached index, closing its connection. Used by tests and reloads."""
    global _graph
    if _graph is not None:
        _graph.close()
        _graph = None


def _error(exc: Exception) -> str:
    if isinstance(exc, IndexNotFoundError):
        return (
            "**No index found.** Run `cartograph index /path/to/repo`, then set "
            "`CARTOGRAPH_DB` or `CARTOGRAPH_ROOT` for this server."
        )
    if isinstance(exc, LookupError):
        return (
            f"**Not found:** {exc}\n\nTry `find_symbol` with a partial name, or "
            "`search_code` for a full-text search over names and docstrings."
        )
    return f"**Error:** {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


@server.tool()
def find_symbol(
    name: Annotated[str, Field(description="Symbol name or qualified name, exact or partial")],
    kind: Annotated[
        str | None,
        Field(
            description="Filter by kind: function, method, class, interface, "
            "struct, enum, type, const"
        ),
    ] = None,
    lang: Annotated[
        str | None, Field(description="Filter by language: python, typescript, tsx, javascript, go")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Max results")] = 20,
) -> str:
    """Locate where a symbol is DEFINED, with its file:line, signature and doc.

    This is the right first call for "where is X?" -- it is exact and ranked by
    structural importance, so if a repo has six functions called `run`, the one
    the codebase actually revolves around comes first.

    Use `search_code` instead when you only know roughly what the thing does
    ("the retry logic") rather than what it is called.
    """
    with _lock:
        try:
            symbols = _get().find_symbol(name, kind=kind, lang=lang, limit=limit)
        except Exception as exc:
            return _error(exc)
    return views.symbols_table(symbols)


@server.tool()
def search_code(
    query: Annotated[
        str, Field(description="Free-text query over names, signatures and docstrings")
    ],
    limit: Annotated[int, Field(ge=1, le=100, description="Max results")] = 20,
) -> str:
    """Full-text search across symbol names, signatures and docstrings (BM25).

    Use when you know the intent but not the identifier. Results are re-ranked by
    call-graph importance, so central symbols outrank incidental mentions.
    """
    with _lock:
        try:
            symbols = _get().search(query, limit=limit)
        except Exception as exc:
            return _error(exc)
    return views.symbols_table(symbols)


@server.tool()
def get_symbol(
    symbol: Annotated[
        str,
        Field(
            description="Symbol id, qualified name (`module:Class.method`), "
            "`path:name`, or bare name"
        ),
    ],
    include_source: Annotated[
        bool, Field(description="Include the full source text of the definition")
    ] = False,
    depth: Annotated[int, Field(ge=1, le=4, description="Caller/callee depth to include")] = 1,
) -> str:
    """Full detail for one symbol: signature, doc, members, callers and callees.

    Prefer this over reading the whole file: you get the definition plus its
    immediate graph neighbourhood, which is usually all the context needed to
    make a safe edit. Set `include_source=true` when you intend to modify it.
    """
    with _lock:
        try:
            detail = _get().symbol_detail(symbol, depth=depth, include_source=include_source)
        except Exception as exc:
            return _error(exc)
    return views.symbol_detail_md(detail)


# ---------------------------------------------------------------------------
# traversal
# ---------------------------------------------------------------------------


@server.tool()
def who_calls(
    symbol: Annotated[str, Field(description="Target symbol (name, qualname or id)")],
    depth: Annotated[int, Field(ge=1, le=6, description="Transitive caller depth")] = 2,
    min_confidence: Annotated[
        float, Field(ge=0.0, le=1.0, description="Minimum edge confidence (0.5 = precision-first)")
    ] = PRECISE,
    limit: Annotated[int, Field(ge=1, le=200, description="Max results")] = 50,
) -> str:
    """Reverse call tree: everything that reaches this symbol, transitively.

    The tool to use before changing a signature, tightening a validation, or
    deleting anything. Each edge reports the rule that produced it; treat sub-0.5
    edges as leads rather than facts.
    """
    with _lock:
        try:
            root, neighbours = _get().who_calls(
                symbol, depth=depth, min_confidence=min_confidence, limit=limit
            )
        except Exception as exc:
            return _error(exc)
    return views.neighbour_tree(root, neighbours, direction="up")


@server.tool()
def what_it_calls(
    symbol: Annotated[str, Field(description="Source symbol (name, qualname or id)")],
    depth: Annotated[int, Field(ge=1, le=6, description="Transitive callee depth")] = 2,
    min_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = PRECISE,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> str:
    """Forward call tree: what this symbol depends on, transitively.

    Use it to understand an unfamiliar function without reading every file it
    touches, and to spot the layer a piece of code really sits in.
    """
    with _lock:
        try:
            root, neighbours = _get().calls(
                symbol, depth=depth, min_confidence=min_confidence, limit=limit
            )
        except Exception as exc:
            return _error(exc)
    return views.neighbour_tree(root, neighbours, direction="down")


@server.tool()
def blast_radius(
    target: Annotated[str, Field(description="A file path or a symbol name/qualname")],
    depth: Annotated[int, Field(ge=1, le=6, description="Transitive import/call depth")] = 3,
    limit: Annotated[int, Field(ge=1, le=300, description="Max results")] = 60,
) -> str:
    """Impact analysis: what a change here could break, and which tests to run.

    Combines the reverse import graph with the reverse call graph, then
    highlights test files specifically. Recall-first by design (confidence >=0.3):
    the expensive mistake is a missed impacted test, not an extra one.

    Call this before editing shared code and after finishing, to pick tests.
    """
    with _lock:
        try:
            radius = _get().blast_radius(target, depth=depth, min_confidence=BROAD, limit=limit)
        except Exception as exc:
            return _error(exc)
    return views.blast_radius_md(radius)


@server.tool()
def related_symbols(
    symbol: Annotated[str, Field(description="Anchor symbol (name, qualname or id)")],
    limit: Annotated[int, Field(ge=1, le=50, description="Max results")] = 15,
) -> str:
    """ "What else should I read before touching this?" via the call graph.

    Runs personalized PageRank seeded on the anchor symbol and treats the call
    graph as undirected, so it surfaces collaborators a plain caller/callee list
    misses -- siblings that share a hub, helpers two hops away.

    This is the structural analogue of semantic search, and it needs no
    embeddings: proximity in the call graph *is* the relationship.
    """
    with _lock:
        try:
            root, symbols = _get().related(symbol, limit=limit)
        except Exception as exc:
            return _error(exc)
    body = views.symbols_table(symbols)
    return f"**Structurally related to `{root.qualname}`**\n\n{body}"


# ---------------------------------------------------------------------------
# orientation
# ---------------------------------------------------------------------------


@server.tool()
def file_summary(
    path: Annotated[str, Field(description="File path, or any distinctive part of one")],
) -> str:
    """Outline of one file: what it defines, what it imports, who imports it.

    Cheaper than reading the file when you only need to know whether it is
    relevant, and it adds the reverse-import view that reading cannot give you.
    """
    with _lock:
        try:
            summary = _get().file_summary(path)
        except Exception as exc:
            return _error(exc)
    return views.file_summary_md(summary)


@server.tool()
def architecture_overview(
    include_diagram: Annotated[
        bool, Field(description="Include a Mermaid diagram of the module graph")
    ] = True,
) -> str:
    """Orient yourself in an unfamiliar repo: modules, layers, cycles, hotspots.

    Start here. One call replaces a dozen exploratory file reads: you get module
    sizes and layering, import cycles, the highest-PageRank symbols (the risky
    ones to change) and the repo's entry points.
    """
    with _lock:
        try:
            arch = _get().architecture()
        except Exception as exc:
            return _error(exc)
    return views.architecture_md(arch, mermaid=include_diagram)


@server.tool()
def index_stats() -> str:
    """Index health: size, coverage, and the edge-resolution breakdown by rule.

    Worth a call when graph answers look thin -- a low resolution rate or a stale
    `indexed_at` tells you the index needs rebuilding rather than the code being
    unusual.
    """
    with _lock:
        try:
            stats = _get().stats()
        except Exception as exc:
            return _error(exc)
    return views.stats_md(stats)


# ---------------------------------------------------------------------------
# resources & prompts
# ---------------------------------------------------------------------------


@server.resource("cartograph://architecture", mime_type="text/markdown")
def architecture_resource() -> str:
    """The module graph, cycles and hotspots as an attachable resource."""
    with _lock:
        try:
            return views.architecture_md(_get().architecture())
        except Exception as exc:
            return _error(exc)


@server.resource("cartograph://stats", mime_type="text/markdown")
def stats_resource() -> str:
    """Index health as an attachable resource."""
    with _lock:
        try:
            return views.stats_md(_get().stats())
        except Exception as exc:
            return _error(exc)


@server.prompt()
def orient(task: str = "") -> str:
    """Structured first pass at an unfamiliar codebase, graph-first."""
    goal = f"\n\nThe task at hand: {task}" if task else ""
    return (
        "Orient yourself in this repository using the Cartograph tools before "
        "reading any files.\n\n"
        "1. `architecture_overview` — modules, layering, cycles, hotspots.\n"
        "2. `index_stats` — confirm the index is fresh and well resolved.\n"
        "3. For each hotspot that looks relevant, `get_symbol` then `who_calls`.\n"
        "4. Only then open files, and only the ones the graph pointed at.\n\n"
        "Report the layering you found, the riskiest symbols to change, and any "
        "import cycles worth flagging." + goal
    )


def main() -> None:
    """Entry point for `cartograph serve` and for direct stdio invocation."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
