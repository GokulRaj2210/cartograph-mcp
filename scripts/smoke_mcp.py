#!/usr/bin/env python
"""Start the MCP server for real and drive one round trip over stdio.

Unit tests can import the tool functions and still miss the failures that
actually break an integration: a bad entry point, a schema the SDK refuses to
generate, or -- the classic -- something printing to stdout and corrupting the
JSON-RPC stream. So CI speaks the protocol.

Usage: python scripts/smoke_mcp.py [repo_root]
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

from cartograph.indexer.pipeline import index_repo

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


async def main(root: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "smoke.db"
        result = index_repo(root, db_path=db)
        print(f"indexed {result.stats.files_indexed} files, {result.stats.symbols} symbols")
        if result.failures:
            print(f"::error::{len(result.failures)} file(s) failed to parse")
            for path, reason in result.failures[:5]:
                print(f"  {path}: {reason}")
            return 1

        env = {**os.environ, "CARTOGRAPH_DB": str(db)}
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "cartograph.mcp_server.server"], env=env
        )

        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected: {init.server_info.name} {init.server_info.version}")

            tools = {t.name for t in (await session.list_tools()).tools}
            if tools != EXPECTED_TOOLS:
                print(
                    f"::error::tool mismatch; missing={EXPECTED_TOOLS - tools} "
                    f"extra={tools - EXPECTED_TOOLS}"
                )
                return 1
            print(f"tools ok: {len(tools)}")

            resources = {str(r.uri) for r in (await session.list_resources()).resources}
            print(f"resources ok: {sorted(resources)}")

            prompts = {p.name for p in (await session.list_prompts()).prompts}
            print(f"prompts ok: {sorted(prompts)}")

            # A real query against a real graph.
            out = await session.call_tool("architecture_overview", {"include_diagram": False})
            text = out.content[0].text  # type: ignore[union-attr]
            if "Architecture overview" not in text:
                print(f"::error::unexpected tool output: {text[:200]}")
                return 1
            print("round trip ok")

            # Errors must come back as readable text, not as a transport failure.
            miss = await session.call_tool("who_calls", {"symbol": "definitely_absent_zzz"})
            if "Not found" not in miss.content[0].text:  # type: ignore[union-attr]
                print("::error::a missing symbol did not produce a graceful message")
                return 1
            print("error handling ok")

    print("MCP smoke test passed")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    raise SystemExit(asyncio.run(main(target.resolve())))
