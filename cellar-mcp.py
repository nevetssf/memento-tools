#!/usr/bin/env python3
"""cellar-mcp.py — MCP server backed by cellar.db (SQLite).

Replaces the previous YAML-per-producer cellar.

Tools (grouped):
  Producers:  add_producer, find_producer, show_producer, list_producers,
              update_producer, delete_producer
  Bottles:    add_bottle, find_bottle, show_bottle, update_bottle,
              delete_bottle, consume_bottle, untasted_bottles
  Tastings:   add_tasting, update_tasting, delete_tasting, recent_tastings,
              rate_bottle (convenience: creates a tasting row)
  Search:     search_cellar, get_types
  Vault:      read_producer_note, append_producer_note,
              read_bottle_note,   append_bottle_note
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

import cellar as cdb  # noqa: E402

app = Server("cellar-db")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    type_prop = {"type": "string", "description": "Drink type (gin, whiskey, wine, ...)"}
    status_prop = {
        "type": "string",
        "enum": sorted(cdb.VALID_STATUSES),
        "description": "Cellar lifecycle status",
    }
    rating_prop = {"type": "integer", "minimum": 0, "maximum": 100,
                   "description": "Rating 0-100"}
    name_or_id_prop = {"type": "string", "description": "Name (case-insensitive) or numeric id"}

    return [
        # ----- producers -----
        types.Tool(
            name="add_producer",
            description="Add a producer (distillery, winery, etc.). Idempotent — returns existing id if name matches.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "region": {"type": "string"},
                    "country": {"type": "string"},
                    "website": {"type": "string"},
                    "notes": {"type": "string", "description": "Short note. Long prose goes in the linked vault note."},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="find_producer",
            description="Fuzzy substring search by producer name.",
            inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        types.Tool(
            name="show_producer",
            description="Producer details + all their bottles.",
            inputSchema={"type": "object", "properties": {"name_or_id": name_or_id_prop}, "required": ["name_or_id"]},
        ),
        types.Tool(
            name="list_producers",
            description="List producers, optionally filtered to those who have at least one bottle of `type`.",
            inputSchema={"type": "object", "properties": {"type": type_prop}},
        ),
        types.Tool(
            name="update_producer",
            description="Update producer fields. Pass any combination of name/region/country/website/notes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": name_or_id_prop,
                    "name": {"type": "string"}, "region": {"type": "string"},
                    "country": {"type": "string"}, "website": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["name_or_id"],
            },
        ),
        types.Tool(
            name="delete_producer",
            description="Delete a producer. CASCADE drops their bottles + tastings. Vault notes are NOT deleted.",
            inputSchema={"type": "object", "properties": {"name_or_id": name_or_id_prop}, "required": ["name_or_id"]},
        ),
        types.Tool(
            name="merge_producers",
            description=(
                "Merge two producer records. All of `source`'s bottles get re-attributed to "
                "`target` (their producer_id flips, vault notes move to the new producer "
                "folder). Bottle-name collisions are auto-merged: tastings move to target's "
                "matching bottle, source's bottle is dropped. Source producer's note prose "
                "is appended to target's note under a '(merged from <name>)' header. The "
                "source producer row + note file are deleted. "
                "Use when Steven says 'Producer X is actually Producer Y' or for cleaning "
                "up post-migration redundant producers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {**name_or_id_prop, "description": "The producer being absorbed (will be deleted)."},
                    "target": {**name_or_id_prop, "description": "The producer that survives."},
                    "append_notes": {"type": "boolean", "default": True,
                                     "description": "Carry source's note prose into target's note. Default true."}
                },
                "required": ["source", "target"],
            },
        ),
        # ----- bottles -----
        types.Tool(
            name="add_bottle",
            description="Add a bottle under a producer. Set producer (name or id), name, type, plus any structured fields.",
            inputSchema={
                "type": "object",
                "properties": {
                    "producer": name_or_id_prop, "name": {"type": "string"}, "type": type_prop,
                    "expression": {"type": "string"}, "style": {"type": "string"},
                    "varietal": {"type": "string"}, "vintage": {"type": "integer"},
                    "age": {"type": "string"}, "abv": {"type": "string"},
                    "cask_type": {"type": "string"}, "botanicals": {"type": "string"},
                    "terroir": {"type": "string", "description": "Soil/climate/region characteristics; mostly relevant for wine."},
                    "price": {"type": "string"}, "acquired_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "status": status_prop, "quantity": {"type": "integer", "minimum": 0},
                    "would_buy_again": {"type": "boolean"},
                    "rating": {**rating_prop, "description": "Bottle-level rating 0-100. Distinct from per-tasting ratings (which live on the tastings row)."},
                    "notes": {"type": "string"},
                },
                "required": ["producer", "name", "type"],
            },
        ),
        types.Tool(
            name="find_bottle",
            description="Fuzzy substring search across bottle.name AND producer.name. Optional type/status filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "type": type_prop, "status": status_prop,
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="show_bottle",
            description="Bottle details + producer + all tastings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": name_or_id_prop,
                    "producer": {"type": "string", "description": "Required only if bottle name is ambiguous across producers."},
                },
                "required": ["name_or_id"],
            },
        ),
        types.Tool(
            name="update_bottle",
            description="Update structured bottle fields. Long-form prose belongs in the linked vault note (see append_bottle_note).",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": name_or_id_prop,
                    "name": {"type": "string"}, "type": type_prop,
                    "expression": {"type": "string"}, "style": {"type": "string"},
                    "varietal": {"type": "string"}, "vintage": {"type": "integer"},
                    "age": {"type": "string"}, "abv": {"type": "string"},
                    "cask_type": {"type": "string"}, "botanicals": {"type": "string"},
                    "terroir": {"type": "string"},
                    "price": {"type": "string"}, "acquired_date": {"type": "string"},
                    "status": status_prop, "quantity": {"type": "integer"},
                    "would_buy_again": {"type": "boolean"},
                    "rating": rating_prop,
                    "notes": {"type": "string"},
                },
                "required": ["name_or_id"],
            },
        ),
        types.Tool(
            name="delete_bottle",
            description="Delete a bottle (cascades tastings). Vault note not removed.",
            inputSchema={"type": "object", "properties": {"name_or_id": name_or_id_prop}, "required": ["name_or_id"]},
        ),
        types.Tool(
            name="consume_bottle",
            description="Decrement quantity by 1; auto-flip status to 'consumed' when it reaches 0.",
            inputSchema={"type": "object", "properties": {"name_or_id": name_or_id_prop}, "required": ["name_or_id"]},
        ),
        types.Tool(
            name="untasted_bottles",
            description="In-cellar bottles with zero tasting rows. Optional type filter.",
            inputSchema={"type": "object", "properties": {"type": type_prop}},
        ),
        # ----- tastings -----
        types.Tool(
            name="add_tasting",
            description="Append a tasting record to a bottle. All fields optional except `bottle`.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bottle": name_or_id_prop, "producer": {"type": "string"},
                    "tasted_at": {"type": "string", "description": "YYYY-MM-DD"},
                    "rating": rating_prop,
                    "nose": {"type": "string"}, "palate": {"type": "string"},
                    "finish": {"type": "string"}, "color": {"type": "string"},
                    "food_pairings": {"type": "string"}, "location": {"type": "string"},
                    "notes": {"type": "string"},
                    "obsidian_file": {"type": "string", "description": "Optional vault path for a long-form essay version of this tasting."},
                },
                "required": ["bottle"],
            },
        ),
        types.Tool(
            name="update_tasting",
            description="Update fields of an existing tasting row.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tasting_id": {"type": "integer"},
                    "tasted_at": {"type": "string"}, "rating": rating_prop,
                    "nose": {"type": "string"}, "palate": {"type": "string"},
                    "finish": {"type": "string"}, "color": {"type": "string"},
                    "food_pairings": {"type": "string"}, "location": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["tasting_id"],
            },
        ),
        types.Tool(
            name="delete_tasting",
            description="Delete a tasting row.",
            inputSchema={"type": "object", "properties": {"tasting_id": {"type": "integer"}}, "required": ["tasting_id"]},
        ),
        types.Tool(
            name="recent_tastings",
            description="Most recent tastings across the whole cellar.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                    "since": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                },
            },
        ),
        types.Tool(
            name="rate_bottle",
            description=(
                "Convenience: create a tasting row with rating + would_buy_again + optional notes. "
                "For a richer tasting (nose/palate/etc.) use `add_tasting` directly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "bottle": name_or_id_prop, "producer": {"type": "string"},
                    "rating": rating_prop,
                    "would_buy_again": {"type": "boolean"},
                    "tasted_at": {"type": "string", "description": "YYYY-MM-DD; defaults to today"},
                    "notes": {"type": "string"},
                },
                "required": ["bottle", "rating"],
            },
        ),
        # ----- search -----
        types.Tool(
            name="search_cellar",
            description="Combined search across producers + bottles by name substring. Optional type/status filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "type": type_prop, "status": status_prop,
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_types",
            description="List drink types currently in the cellar plus the recommended vocabulary.",
            inputSchema={"type": "object"},
        ),
        # ----- vault notes -----
        types.Tool(
            name="read_producer_note",
            description="Read the producer's long-form vault note (the file at producers.obsidian_file).",
            inputSchema={"type": "object", "properties": {"name_or_id": name_or_id_prop}, "required": ["name_or_id"]},
        ),
        types.Tool(
            name="append_producer_note",
            description="Append text to the producer's vault note. Creates the file (with frontmatter) on first use.",
            inputSchema={
                "type": "object",
                "properties": {"name_or_id": name_or_id_prop, "text": {"type": "string"}},
                "required": ["name_or_id", "text"],
            },
        ),
        types.Tool(
            name="read_bottle_note",
            description="Read the bottle's long-form vault note.",
            inputSchema={
                "type": "object",
                "properties": {"name_or_id": name_or_id_prop, "producer": {"type": "string"}},
                "required": ["name_or_id"],
            },
        ),
        types.Tool(
            name="append_bottle_note",
            description="Append text to the bottle's vault note. Creates the file (with frontmatter) on first use.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": name_or_id_prop, "producer": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["name_or_id", "text"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _row(r):
    return dict(r) if r is not None else None


def _rows(rs):
    return [dict(r) for r in rs]


def _name_or_id(v):
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return v


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    args = arguments or {}
    try:
        result = _dispatch(name, args)
    except (KeyError, ValueError, TypeError) as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
    except Exception as e:
        return [types.TextContent(
            type="text",
            text=json.dumps({"error": f"{type(e).__name__}: {e}"}),
        )]
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


def _dispatch(name: str, args: dict):
    con = cdb.connect()
    try:
        # producers
        if name == "add_producer":
            pid = cdb.add_producer(con, args["name"], **{
                k: args.get(k) for k in ("region", "country", "website", "notes")
            })
            return _row(cdb.get_producer(con, pid))
        if name == "find_producer":
            return _rows(cdb.find_producers(con, args["query"]))
        if name == "show_producer":
            return cdb.show_producer(con, _name_or_id(args["name_or_id"]))
        if name == "list_producers":
            return _rows(cdb.list_producers(con, type_=args.get("type")))
        if name == "update_producer":
            n = cdb.update_producer(con, _name_or_id(args["name_or_id"]),
                                    **{k: args[k] for k in args if k in {
                                        "name", "region", "country", "website", "notes"}})
            return {"updated_rows": n}
        if name == "delete_producer":
            n = cdb.delete_producer(con, _name_or_id(args["name_or_id"]))
            return {"deleted_rows": n}
        if name == "merge_producers":
            with cdb.transaction(con):
                return cdb.merge_producers(
                    con,
                    _name_or_id(args["source"]),
                    _name_or_id(args["target"]),
                    append_notes=args.get("append_notes", True),
                )

        # bottles
        if name == "add_bottle":
            type_ = args.pop("type")
            producer = args.pop("producer")
            bname = args.pop("name")
            bid = cdb.add_bottle(con, _name_or_id(producer), bname, type_, **args)
            return _row(cdb.get_bottle(con, bid))
        if name == "find_bottle":
            return _rows(cdb.find_bottles(con, args["query"],
                                          type_=args.get("type"),
                                          status=args.get("status")))
        if name == "show_bottle":
            return cdb.show_bottle(con, _name_or_id(args["name_or_id"]),
                                   producer=args.get("producer"))
        if name == "update_bottle":
            n = cdb.update_bottle(con, _name_or_id(args["name_or_id"]),
                                  **{k: args[k] for k in args if k != "name_or_id"})
            return {"updated_rows": n}
        if name == "delete_bottle":
            n = cdb.delete_bottle(con, _name_or_id(args["name_or_id"]))
            return {"deleted_rows": n}
        if name == "consume_bottle":
            return cdb.consume_bottle(con, _name_or_id(args["name_or_id"]))
        if name == "untasted_bottles":
            return _rows(cdb.untasted_bottles(con, type_=args.get("type")))

        # tastings
        if name == "add_tasting":
            bottle = args.pop("bottle")
            producer = args.pop("producer", None)
            b = cdb.get_bottle(con, _name_or_id(bottle), producer=producer)
            if not b:
                raise KeyError(f"No bottle matching {bottle!r}")
            tid = cdb.add_tasting(con, b["id"], **args)
            return {"tasting_id": tid}
        if name == "update_tasting":
            n = cdb.update_tasting(con, args["tasting_id"],
                                   **{k: args[k] for k in args if k != "tasting_id"})
            return {"updated_rows": n}
        if name == "delete_tasting":
            return {"deleted_rows": cdb.delete_tasting(con, args["tasting_id"])}
        if name == "recent_tastings":
            return _rows(cdb.recent_tastings(con, limit=args.get("limit", 20),
                                             since=args.get("since")))
        if name == "rate_bottle":
            from datetime import date
            bottle = args["bottle"]
            producer = args.get("producer")
            b = cdb.get_bottle(con, _name_or_id(bottle), producer=producer)
            if not b:
                raise KeyError(f"No bottle matching {bottle!r}")
            tid = cdb.add_tasting(
                con, b["id"],
                rating=args["rating"],
                tasted_at=args.get("tasted_at") or date.today().isoformat(),
                notes=args.get("notes"),
            )
            if "would_buy_again" in args:
                cdb.update_bottle(con, b["id"], would_buy_again=args["would_buy_again"])
            return {"tasting_id": tid, "bottle_id": b["id"]}

        # search
        if name == "search_cellar":
            return cdb.search_cellar(con, args["query"],
                                     type_=args.get("type"), status=args.get("status"))
        if name == "get_types":
            return cdb.get_types(con)

        # vault notes
        if name == "read_producer_note":
            return cdb.read_producer_note(con, _name_or_id(args["name_or_id"]))
        if name == "append_producer_note":
            return cdb.append_producer_note(con, _name_or_id(args["name_or_id"]), args["text"])
        if name == "read_bottle_note":
            return cdb.read_bottle_note(con, _name_or_id(args["name_or_id"]),
                                        producer=args.get("producer"))
        if name == "append_bottle_note":
            return cdb.append_bottle_note(con, _name_or_id(args["name_or_id"]),
                                          args["text"], producer=args.get("producer"))

        raise ValueError(f"Unknown tool: {name}")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
