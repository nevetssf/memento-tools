#!/usr/bin/env python3
"""
vault-mcp.py — MCP server for Obsidian vault operations.

Wraps: vault-search.py + direct vault file operations
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

sys.path.insert(0, str(Path(__file__).parent))
from config import VAULT_DIR

SCRIPTS_DIR = Path(__file__).parent
VAULT = VAULT_DIR
app = Server("vault-db")


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vsearch = _load("vault-search")

SECTIONS = sorted(vsearch.SECTIONS.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(path: str) -> Path:
    """Resolve a vault-relative or absolute path, enforcing vault root."""
    p = Path(path)
    if not p.is_absolute():
        p = VAULT / p
    p = p.resolve()
    if not str(p).startswith(str(VAULT)):
        raise ValueError(f"Path outside vault: {path}")
    return p


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    path_prop = {"type": "string", "description": "Vault-relative path e.g. 'People/Kasapi, Steven.md'"}
    return [
        types.Tool(
            name="search_vault",
            description="Full-text search across Steven's Obsidian vault. Query is treated as a regex. Returns matching files with excerpts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (regex or plain text). Use 'A|B' for OR."
                    },
                    "section": {
                        "type": "string",
                        "enum": SECTIONS,
                        "description": "Limit search to a vault section (optional)"
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results (default: 10)"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="read_note",
            description="Read the full content of an Obsidian vault note.",
            inputSchema={
                "type": "object",
                "properties": {"path": path_prop},
                "required": ["path"]
            }
        ),
        types.Tool(
            name="write_note",
            description="Write content to a vault note. Fails if the file already exists unless overwrite=true.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": path_prop,
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False, "description": "Set true to replace an existing file"}
                },
                "required": ["path", "content"]
            }
        ),
        types.Tool(
            name="append_note",
            description="Append content to an existing vault note. Fails if the file does not exist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": path_prop,
                    "content": {"type": "string", "description": "Content to append (newline prepended automatically)"}
                },
                "required": ["path", "content"]
            }
        ),
        types.Tool(
            name="list_notes",
            description="List notes in a vault section or subfolder.",
            inputSchema={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": SECTIONS,
                        "description": "Top-level vault section (optional — omit to list all sections)"
                    },
                    "subfolder": {
                        "type": "string",
                        "description": "Subfolder within section e.g. '2026' within Journal (optional)"
                    }
                }
            }
        ),
        types.Tool(
            name="note_exists",
            description="Check whether a vault note exists.",
            inputSchema={
                "type": "object",
                "properties": {"path": path_prop},
                "required": ["path"]
            }
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = _dispatch(name, arguments)
    except BaseException as e:
        result = json.dumps({"error": str(e)})
    return [types.TextContent(type="text", text=result)]


def _dispatch(name: str, args: dict) -> str:
    if name == "search_vault":
        results = vsearch.search(
            query=args["query"],
            section=args.get("section"),
            max_results=args.get("max_results", 10),
        )
        return json.dumps(results, indent=2)

    elif name == "read_note":
        p = _resolve(args["path"])
        if not p.exists():
            return json.dumps({"error": f"Note not found: {args['path']}"})
        return p.read_text(encoding="utf-8", errors="replace")

    elif name == "write_note":
        p = _resolve(args["path"])
        if p.exists() and not args.get("overwrite", False):
            return json.dumps({"error": f"Note already exists: {args['path']} — set overwrite=true to replace"})
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return json.dumps({"status": "ok", "path": str(p.relative_to(VAULT))})

    elif name == "append_note":
        p = _resolve(args["path"])
        if not p.exists():
            return json.dumps({"error": f"Note not found: {args['path']} — use write_note to create it"})
        existing = p.read_text(encoding="utf-8")
        separator = "\n" if existing.endswith("\n") else "\n\n"
        p.write_text(existing + separator + args["content"], encoding="utf-8")
        return json.dumps({"status": "ok", "path": str(p.relative_to(VAULT))})

    elif name == "list_notes":
        if args.get("section"):
            key = args["section"].lower()
            folder_name = vsearch.SECTIONS.get(key, args["section"])
            root = VAULT / folder_name
        else:
            root = VAULT

        if args.get("subfolder"):
            root = root / args["subfolder"]

        if not root.exists():
            return json.dumps({"error": f"Folder not found: {root.relative_to(VAULT)}"})

        notes = sorted(
            str(p.relative_to(VAULT))
            for p in root.rglob("*.md")
        )
        return json.dumps(notes)

    elif name == "note_exists":
        p = _resolve(args["path"])
        return json.dumps({"exists": p.exists(), "path": args["path"]})

    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
