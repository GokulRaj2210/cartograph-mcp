"""MCP surface tests.

Two layers, deliberately:

* **In-process** tests call the tool functions directly. Fast, and they assert
  the *content contract* -- what the model actually receives.
* **Protocol** tests spawn the real server over stdio and drive it with the MCP
  client. Slower, but they are the only way to catch schema-generation problems,
  a broken entry point, or anything written to stdout that would corrupt the
  JSON-RPC stream (a classic way to break an stdio MCP server).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from cartograph.indexer.pipeline import IndexResult
from cartograph.mcp_server import server as mcp_server

# ---------------------------------------------------------------------------
# in-process: tool behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def wired(indexed: IndexResult, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the module-level server at the fixture index, and clean up after.

    Closing on teardown matters: dropping the reference alone leaks the sqlite
    connection, and enough leaked connections turn into "database is locked".
    """
    monkeypatch.setenv("CARTOGRAPH_DB", str(indexed.db_path))
    mcp_server._reset()
    yield
    mcp_server._reset()


def call(_tool: str, **kwargs: object) -> str:
    """Invoke a tool through its registered function.

    The leading underscore matters: several tools take a `name` parameter, so a
    conventionally named first argument would collide with the tool's own kwargs.
    """
    fn = getattr(mcp_server, _tool)
    result = fn(**kwargs)
    assert isinstance(result, str)
    return result


def test_find_symbol_tool(wired: None) -> None:
    out = call("find_symbol", name="normalize")
    assert "app.helpers:normalize" in out
    assert "src/app/helpers.py" in out


def test_find_symbol_tool_filters(wired: None) -> None:
    out = call("find_symbol", name="send", lang="typescript")
    assert "web/client" in out


def test_get_symbol_tool_includes_source_on_request(wired: None) -> None:
    out = call("get_symbol", symbol="app.helpers:normalize", include_source=True)
    assert "def normalize" in out
    plain = call("get_symbol", symbol="app.helpers:normalize", include_source=False)
    assert "def normalize" not in plain.split("```")[0]


def test_who_calls_tool(wired: None) -> None:
    out = call("who_calls", symbol="app.helpers:truncate", depth=1)
    assert "Engine.run" in out


def test_what_it_calls_tool(wired: None) -> None:
    out = call("what_it_calls", symbol="app.core:Engine.run", depth=1)
    assert "truncate" in out


def test_blast_radius_tool_names_tests(wired: None) -> None:
    out = call("blast_radius", target="src/app/core.py")
    assert "tests/test_core.py" in out


def test_related_symbols_tool(wired: None) -> None:
    out = call("related_symbols", symbol="app.helpers:normalize")
    assert "related to" in out


def test_architecture_overview_tool(wired: None) -> None:
    out = call("architecture_overview")
    assert "Architecture overview" in out
    assert "mermaid" in out


def test_file_summary_tool(wired: None) -> None:
    out = call("file_summary", path="src/app/core.py")
    assert "Engine" in out


def test_index_stats_tool(wired: None) -> None:
    out = call("index_stats")
    assert "Index stats" in out


def test_search_code_tool(wired: None) -> None:
    out = call("search_code", query="Collapse whitespace")
    assert "normalize" in out


# ---------------------------------------------------------------------------
# in-process: failure modes must be legible, not exceptions
# ---------------------------------------------------------------------------


def test_unknown_symbol_returns_guidance_not_a_traceback(wired: None) -> None:
    out = call("who_calls", symbol="no_such_symbol_xyz")
    assert "Not found" in out
    assert "find_symbol" in out or "search_code" in out


def test_unknown_file_returns_guidance(wired: None) -> None:
    out = call("file_summary", path="nope/missing.py")
    assert "Not found" in out


def test_missing_index_explains_how_to_fix_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CARTOGRAPH_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("CARTOGRAPH_ROOT", str(tmp_path))
    mcp_server._reset()
    out = call("find_symbol", name="anything")
    assert "No index found" in out
    assert "cartograph index" in out


@pytest.mark.anyio
async def test_every_tool_has_a_substantial_description() -> None:
    """Tool descriptions are the model's routing prompt; a thin one is a bug."""
    tools = await mcp_server.server.list_tools()
    assert len(tools) == len(EXPECTED_TOOLS)
    for tool in tools:
        assert tool.description, f"tool {tool.name} has no description"
        assert len(tool.description) > 80, f"tool {tool.name} description is too thin"


@pytest.mark.anyio
async def test_tool_descriptions_route_between_tools() -> None:
    """Each description should say when to use something *else* instead."""
    tools = {t.name: (t.description or "") for t in await mcp_server.server.list_tools()}
    assert "search_code" in tools["find_symbol"]
    assert "find_symbol" in tools["search_code"] or "identifier" in tools["search_code"]


# ---------------------------------------------------------------------------
# protocol level
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "find_symbol",
    "search_code",
    "get_symbol",
    "who_calls",
    "what_it_calls",
    "blast_radius",
    "related_symbols",
    "file_summary",
    "architecture_overview",
    "index_stats",
}


@pytest.mark.anyio
async def test_stdio_protocol_end_to_end(indexed: IndexResult) -> None:
    """Spawn the real server and speak MCP to it."""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    env = dict(os.environ)
    env["CARTOGRAPH_DB"] = str(indexed.db_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cartograph.mcp_server.server"],
        env=env,
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "cartograph"
        assert init.instructions and "structural" in init.instructions

        tools = await session.list_tools()
        assert {t.name for t in tools.tools} == EXPECTED_TOOLS
        for tool in tools.tools:
            assert tool.description, tool.name
            assert tool.input_schema["type"] == "object"

        resources = await session.list_resources()
        assert {str(r.uri) for r in resources.resources} == {
            "cartograph://architecture",
            "cartograph://stats",
        }

        prompts = await session.list_prompts()
        assert {p.name for p in prompts.prompts} == {"orient"}

        result = await session.call_tool("who_calls", {"symbol": "app.helpers:truncate"})
        assert result.content
        assert "Engine.run" in result.content[0].text  # type: ignore[union-attr]

        arch = await session.read_resource("cartograph://architecture")  # type: ignore[arg-type]
        assert "Architecture overview" in arch.contents[0].text  # type: ignore[union-attr]

        prompt = await session.get_prompt("orient", {"task": "add caching"})
        assert "architecture_overview" in str(prompt.messages[0].content)


@pytest.mark.anyio
async def test_tool_schemas_declare_their_constraints(indexed: IndexResult) -> None:
    """`depth`/`limit` bounds must reach the client, or agents send nonsense."""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    env = dict(os.environ)
    env["CARTOGRAPH_DB"] = str(indexed.db_path)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "cartograph.mcp_server.server"], env=env
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}

    schema = tools["who_calls"].input_schema
    assert "symbol" in schema["required"]
    depth = schema["properties"]["depth"]
    assert depth["maximum"] == 6
    confidence = schema["properties"]["min_confidence"]
    assert confidence["minimum"] == 0.0
    assert confidence["maximum"] == 1.0
