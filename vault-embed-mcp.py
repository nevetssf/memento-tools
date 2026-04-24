#!/usr/bin/env python3
"""
vault-embed-mcp.py — MCP server for semantic search over the Obsidian vault.

Wraps vault_embed.py with four tools:
  - index_vault       — reconcile the vault → index (add/update/move/delete)
  - semantic_search   — hybrid vector + FTS5 search
  - search_recent     — convenience wrapper for recent journal entries
  - index_status      — stats about the current index
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

sys.path.insert(0, str(Path(__file__).parent))
import vault_embed as ve
from config import VAULT_DIR

app = Server("vault-embed")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="index_vault",
            description=(
                "Reconcile the Obsidian vault with the semantic search index. "
                "Adds new files, updates changed ones, detects moves, deletes orphans. "
                "Safe to call repeatedly; only changed files get re-embedded."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Optional subdirectory under the vault to limit the scan (e.g. 'Journal'). Defaults to the full vault."
                    }
                }
            },
        ),
        types.Tool(
            name="semantic_search",
            description=(
                "Search the Obsidian vault by meaning and keyword (hybrid). "
                "Returns chunks with file path, heading context, content, and metadata."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The natural-language query"},
                    "limit": {"type": "integer", "description": "Max results (default: 10)"},
                    "section": {
                        "type": "string",
                        "description": "Filter by vault section e.g. 'journal', 'people', 'cellar/wine'"
                    },
                    "file_type": {
                        "type": "string",
                        "description": "Filter by 'md' or 'pdf'"
                    },
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["query"]
            },
        ),
        types.Tool(
            name="search_recent",
            description="Search recent journal entries (last N days). Convenience wrapper around semantic_search.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "days": {"type": "integer", "description": "Number of days back (default: 30)"},
                    "limit": {"type": "integer", "description": "Max results (default: 10)"},
                },
                "required": ["query"]
            },
        ),
        types.Tool(
            name="index_status",
            description="Report on the vault search index: file/chunk counts, per-section breakdown, last indexed timestamp, DB size.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = _dispatch(name, arguments)
    except Exception as e:
        result = json.dumps({"error": f"{type(e).__name__}: {e}"})
    return [types.TextContent(type="text", text=result)]


def _dispatch(name: str, args: dict) -> str:
    con = ve.connect()

    if name == "index_vault":
        subpath = args.get("path")
        vault_path = VAULT_DIR / subpath if subpath else VAULT_DIR
        if not vault_path.exists():
            return json.dumps({"error": f"Path not found: {vault_path}"})
        # When scanning a subdir, still pass VAULT_DIR as root so paths are relative to vault
        counts = ve.reconcile(con, VAULT_DIR) if not subpath else _reconcile_subdir(con, vault_path)
        return json.dumps({"ok": True, "counts": counts, "path": str(vault_path)})

    if name == "semantic_search":
        hits = ve.semantic_search(
            con,
            args["query"],
            limit=args.get("limit", 10),
            section=args.get("section"),
            file_type=args.get("file_type"),
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
        )
        return json.dumps({"query": args["query"], "hits": hits, "count": len(hits)})

    if name == "search_recent":
        days = args.get("days", 30)
        date_from = (datetime.now(tz=timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        hits = ve.semantic_search(
            con,
            args["query"],
            limit=args.get("limit", 10),
            section="journal",
            date_from=date_from,
        )
        return json.dumps({"query": args["query"], "days": days, "hits": hits, "count": len(hits)})

    if name == "index_status":
        return json.dumps(ve.index_stats(con))

    return json.dumps({"error": f"Unknown tool: {name}"})


def _reconcile_subdir(con, subdir_path: Path) -> dict:
    """Reconcile a subdirectory: updates matching files, does NOT delete orphans outside the subdir."""
    counts = {"new": 0, "updated": 0, "unchanged": 0, "moved": 0, "skipped": 0, "error": 0}
    for abs_path in subdir_path.rglob("*"):
        if not abs_path.is_file() or abs_path.suffix.lower() not in (".md", ".pdf"):
            continue
        if any(skip in abs_path.parts for skip in (".obsidian", ".trash", "Templates")):
            continue
        result = ve.index_file(con, VAULT_DIR, abs_path)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    return counts


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
