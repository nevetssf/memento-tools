#!/usr/bin/env python3
"""
chat-signal-mcp.py — MCP server for the chat-signal-db.

Persistent record of Steven's Signal exchanges with the agent (Memento), stored
under VAULT_DIR/Chats/Signal/YYYY/MM/. One Markdown file per day, photos in a
per-month directory.

Distinct from journal-db: journal entries are Steven's logged life events;
chats are the full transcript of his exchanges with the agent.

Auth: set MEMENTO_VAULT_DIR if vault is non-default; no other secrets needed.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

import chat_signal as cs

app = Server("chat-signal-db")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    photo_arg = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "List of source filesystem paths (e.g. '/tmp/photo.jpg') and/or "
            "pre-formed Obsidian wikilinks like '![[Chats/Signal/.../foo.jpg]]'. "
            "Source paths are auto-moved into the month's photos dir; wikilinks "
            "are passed through."
        ),
    }
    timestamp_arg = {
        "type": "string",
        "description": (
            "ISO 8601 datetime (e.g. '2026-05-01T14:32:45-06:00'). "
            "Defaults to current local time per LOCATION.md."
        ),
    }
    agent_arg = {
        "type": "string",
        "description": (
            "Agent identity ('main' is default and untagged in the speaker label; "
            "non-default values render as 'Memento (work):')."
        ),
        "default": "main",
    }
    date_arg = {
        "type": "string",
        "description": "ISO date 'YYYY-MM-DD'. Defaults to today.",
    }

    return [
        # ----- save -----------------------------------------------------
        types.Tool(
            name="chat_save_message",
            description=(
                "Append a single chat message (Steven, Memento, or system) to today's "
                "or `timestamp`'s chat file. Empty `text` is allowed only when `photos` "
                "is non-empty."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sender": {
                        "type": "string",
                        "enum": ["Steven", "Memento", "system"],
                        "description": "Who sent the message.",
                    },
                    "text": {"type": "string"},
                    "timestamp": timestamp_arg,
                    "photos": photo_arg,
                    "agent": agent_arg,
                },
                "required": ["sender", "text"],
            },
        ),
        types.Tool(
            name="chat_save_exchange",
            description=(
                "Convenience: append a paired Steven → Memento exchange in one call. "
                "Two `chat_save_message` calls underneath."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "steven_text": {"type": "string"},
                    "memento_text": {"type": "string"},
                    "timestamp": timestamp_arg,
                    "steven_photos": photo_arg,
                    "memento_photos": photo_arg,
                    "agent": agent_arg,
                },
                "required": ["steven_text", "memento_text"],
            },
        ),
        types.Tool(
            name="chat_save_attachment",
            description=(
                "Move an attachment (image / audio / video / generic file) from a source "
                "filesystem path into the chat's per-month directory. Auto-detects kind "
                "from the file extension if not supplied. Returns the Obsidian wikilink "
                "(plus resolved kind and final path), ready to embed in a message via "
                "`chat_save_message(photos=[<wikilink>])`. Filename pattern: "
                "YYYY-MM-DD-HHMMSS-<slug>.ext (slug optional). Per-kind subdirs: "
                "image→photos/, audio→audio/, video→video/, file→attachments/."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Absolute or relative filesystem path of the file.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["image", "audio", "video", "file"],
                        "description": (
                            "Override the auto-detected kind. Usually unnecessary — the "
                            "extension determines kind."
                        ),
                    },
                    "date": date_arg,
                    "slug": {
                        "type": "string",
                        "description": "Optional human-readable slug to suffix the filename.",
                    },
                },
                "required": ["source_path"],
            },
        ),
        types.Tool(
            name="chat_save_photo",
            description=(
                "Backwards-compatible alias for `chat_save_attachment` with `kind='image'`. "
                "Returns just the wikilink string (legacy contract). Prefer "
                "`chat_save_attachment` for new callers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_path": {"type": "string"},
                    "date": date_arg,
                    "slug": {"type": "string"},
                },
                "required": ["source_path"],
            },
        ),
        # ----- retrieve --------------------------------------------------
        types.Tool(
            name="chat_get_today",
            description="Return today's chat messages as a list.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Last N messages of the day. Default: all.",
                    }
                },
            },
        ),
        types.Tool(
            name="chat_get_recent",
            description=(
                "Return the most recent N messages, walking backwards across day-files. "
                "Useful for short-context recall."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "since": {
                        "type": "string",
                        "description": "ISO date 'YYYY-MM-DD'; messages strictly older are excluded.",
                    },
                },
                "required": ["limit"],
            },
        ),
        types.Tool(
            name="chat_get_by_date",
            description="Return all messages for a specific date.",
            inputSchema={
                "type": "object",
                "properties": {
                    "date": date_arg,
                    "limit": {"type": "integer"},
                },
                "required": ["date"],
            },
        ),
        types.Tool(
            name="chat_search",
            description=(
                "Case-insensitive substring search across chat files. Optionally "
                "filter by date range and/or sender."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "since": {"type": "string", "description": "ISO date inclusive."},
                    "until": {"type": "string", "description": "ISO date inclusive."},
                    "sender": {
                        "type": "string",
                        "enum": ["Steven", "Memento", "system"],
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="chat_get_dates",
            description="List dates that have chat files. Optional year/month filter.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer"},
                    "month": {"type": "integer"},
                },
            },
        ),
        # ----- utility ---------------------------------------------------
        types.Tool(
            name="chat_get_path",
            description="Return the absolute filesystem path of a day's chat file.",
            inputSchema={
                "type": "object",
                "properties": {"date": date_arg},
            },
        ),
        types.Tool(
            name="chat_get_summary",
            description=(
                "Counts and stats over a date range: exchange_count, day_count, "
                "photo_count, top_senders."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "ISO date inclusive."},
                    "date_to": {"type": "string", "description": "ISO date inclusive."},
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = _dispatch(name, arguments or {})
    except FileNotFoundError as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
    except ValueError as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
    except Exception as e:  # pragma: no cover  — surface unexpected errors clearly
        return [
            types.TextContent(
                type="text",
                text=json.dumps({"error": f"{type(e).__name__}: {e}"}),
            )
        ]
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


def _dispatch(name: str, args: dict):
    if name == "chat_save_message":
        return cs.save_message(
            sender=args["sender"],
            text=args.get("text", ""),
            timestamp=args.get("timestamp"),
            photos=args.get("photos") or None,
            agent=args.get("agent", cs.DEFAULT_AGENT),
        )
    if name == "chat_save_exchange":
        return cs.save_exchange(
            steven_text=args["steven_text"],
            memento_text=args["memento_text"],
            timestamp=args.get("timestamp"),
            steven_photos=args.get("steven_photos") or None,
            memento_photos=args.get("memento_photos") or None,
            agent=args.get("agent", cs.DEFAULT_AGENT),
        )
    if name == "chat_save_attachment":
        return cs.save_attachment(
            source_path=args["source_path"],
            kind=args.get("kind"),
            date=args.get("date"),
            slug=args.get("slug"),
        )
    if name == "chat_save_photo":
        link = cs.save_photo(
            source_path=args["source_path"],
            date=args.get("date"),
            slug=args.get("slug"),
        )
        return {"wikilink": link}

    if name == "chat_get_today":
        return cs.get_today(limit=args.get("limit"))
    if name == "chat_get_recent":
        return cs.get_recent(limit=args["limit"], since=args.get("since"))
    if name == "chat_get_by_date":
        return cs.get_by_date(date=args["date"], limit=args.get("limit"))
    if name == "chat_search":
        return cs.search(
            query=args["query"],
            since=args.get("since"),
            until=args.get("until"),
            sender=args.get("sender"),
        )
    if name == "chat_get_dates":
        return cs.get_dates(year=args.get("year"), month=args.get("month"))

    if name == "chat_get_path":
        return {"path": cs.get_path(date=args.get("date"))}
    if name == "chat_get_summary":
        return cs.get_summary(
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
        )

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
