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
import subprocess
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
                "Safe to call repeatedly; only changed files get re-embedded. "
                "For a full-vault scan, pass background=true and poll index_progress to check status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Optional subdirectory under the vault to limit the scan (e.g. 'Journal'). Defaults to the full vault."
                    },
                    "filter": {
                        "type": "string",
                        "enum": ["all", "text", "pdf"],
                        "description": "What to index: 'all' (default; text files first then PDFs), 'text' (.md/.html/.txt only), 'pdf' (PDFs only). When filtered, orphan deletion only applies within that file type."
                    },
                    "background": {
                        "type": "boolean",
                        "description": "Run in background (default: false). When true, returns immediately; poll index_progress to track."
                    }
                }
            },
        ),
        types.Tool(
            name="index_progress",
            description=(
                "Report the status of the current or most recent indexing run. "
                "Returns phase (indexing/deleting_orphans/completed/failed), processed/total file counts, "
                "current file, counts by status, and timestamps."
            ),
            inputSchema={"type": "object", "properties": {}},
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

        # Reject if another run is in progress
        existing = ve.read_progress()
        if existing and existing.get("status") == "running":
            return json.dumps({"ok": False, "error": "An indexing run is already in progress", "progress": existing})

        file_filter = args.get("filter", "all")
        if file_filter not in ("all", "text", "pdf"):
            return json.dumps({"error": f"Invalid filter: {file_filter}"})

        if args.get("background"):
            # Spawn detached subprocess
            script = Path(__file__).parent / "vault_embed.py"
            cmd = [sys.executable, str(script), "--reconcile"]
            if subpath:
                cmd += ["--path", subpath]
            if file_filter == "text":
                cmd.append("--text-only")
            elif file_filter == "pdf":
                cmd.append("--pdf-only")
            log = Path(str(ve.EMBED_DB_PATH.parent / "index.log"))
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a") as f:
                proc = subprocess.Popen(
                    cmd, stdout=f, stderr=f,
                    start_new_session=True,
                )
            return json.dumps({
                "ok": True,
                "background": True,
                "pid": proc.pid,
                "scope": str(vault_path),
                "filter": file_filter,
                "note": "Poll index_progress to check status.",
            })

        # Synchronous
        if subpath:
            counts = _reconcile_subdir(con, vault_path, file_filter=file_filter)
        else:
            counts = ve.reconcile(con, VAULT_DIR, file_filter=file_filter)
        return json.dumps({"ok": True, "background": False, "counts": counts,
                           "path": str(vault_path), "filter": file_filter})

    if name == "index_progress":
        state = ve.read_progress()
        if not state:
            return json.dumps({"status": "never_run"})
        return json.dumps(state)

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


def _reconcile_subdir(con, subdir_path: Path, file_filter: str = "all") -> dict:
    """Reconcile a subdirectory with progress reporting; does NOT delete orphans outside the subdir."""
    return ve.reconcile(con, subdir_path, file_filter=file_filter)


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
