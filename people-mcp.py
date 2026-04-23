#!/usr/bin/env python3
"""
people-mcp.py — MCP server wrapping people.py for OpenClaw.

Exposes all people.py commands as MCP tools.
"""

import argparse
import asyncio
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import people as p

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

app = Server("people-db")

RELATIONSHIP_TYPES = sorted(p.VALID_TYPES)


def _capture(fn):
    """Run fn(), capturing stdout. On SystemExit, return the stderr JSON error from p.err()."""
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            fn()
        return buf_out.getvalue().strip()
    except SystemExit:
        err = buf_err.getvalue().strip()
        return err if err else json.dumps({"error": "Command failed"})


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="find_person",
            description="Fuzzy search for people by name. Returns matching records with id, name, profession, location.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name or partial name to search for"}
                },
                "required": ["name"]
            }
        ),
        types.Tool(
            name="show_person",
            description="Full profile for a person including all relationships. Use name or numeric id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": {"type": "string", "description": "Person name (fuzzy) or numeric id"}
                },
                "required": ["name_or_id"]
            }
        ),
        types.Tool(
            name="get_relatives",
            description="List relationships for a person. Infers family relationships (siblings, cousins, etc.) from the parent graph. Optionally filter by type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": {"type": "string", "description": "Person name or id"},
                    "type": {
                        "type": "string",
                        "description": "Filter by relationship type (e.g. cousin, grandmother, friend, parent, sibling). Optional."
                    },
                    "infer": {
                        "type": "boolean",
                        "description": "Include inferred family relationships (default: true)",
                        "default": True
                    }
                },
                "required": ["name_or_id"]
            }
        ),
        types.Tool(
            name="add_person",
            description="Add a new person to the database.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "gender": {"type": "string", "enum": ["M", "F", "NB"]},
                    "profession": {"type": "string"},
                    "location": {"type": "string"},
                    "birthdate": {"type": "string", "description": "YYYY-MM-DD"},
                    "deathdate": {"type": "string", "description": "YYYY-MM-DD"},
                    "notes": {"type": "string"},
                    "phone": {"type": "string"},
                    "university": {"type": "string"},
                    "linkedin": {"type": "string"},
                    "biography": {"type": "string"},
                    "interests": {"type": "string"},
                    "obsidian_file": {"type": "string"},
                },
                "required": ["name"]
            }
        ),
        types.Tool(
            name="update_person",
            description="Update fields on an existing person record.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": {"type": "string", "description": "Person name or id"},
                    "name": {"type": "string", "description": "New name"},
                    "gender": {"type": "string", "enum": ["M", "F", "NB"]},
                    "profession": {"type": "string"},
                    "location": {"type": "string"},
                    "birthdate": {"type": "string"},
                    "deathdate": {"type": "string"},
                    "notes": {"type": "string"},
                    "phone": {"type": "string"},
                    "university": {"type": "string"},
                    "linkedin": {"type": "string"},
                    "biography": {"type": "string"},
                    "interests": {"type": "string"},
                    "obsidian_file": {"type": "string"},
                },
                "required": ["name_or_id"]
            }
        ),
        types.Tool(
            name="relate",
            description="Add a relationship between two people. Only use 'parent' for family links — siblings, cousins, etc. are inferred automatically. For step-parents use qualifier='marriage'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person": {"type": "string", "description": "Person name or id"},
                    "relative": {"type": "string", "description": "Relative name or id"},
                    "relative_is": {
                        "type": "string",
                        "enum": RELATIONSHIP_TYPES,
                        "description": "Relationship type. The relative IS this to the person (e.g. 'parent' means relative is person's parent)."
                    },
                    "qualifier": {
                        "type": "string",
                        "description": "Optional qualifier: adoptive, marriage, foster"
                    },
                    "notes": {"type": "string"}
                },
                "required": ["person", "relative", "relative_is"]
            }
        ),
        types.Tool(
            name="update_relationship",
            description="Change the type of an existing relationship between two people.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person": {"type": "string"},
                    "relative": {"type": "string"},
                    "from_type": {"type": "string", "enum": RELATIONSHIP_TYPES, "description": "Current type"},
                    "to_type": {"type": "string", "enum": RELATIONSHIP_TYPES, "description": "New type"}
                },
                "required": ["person", "relative", "from_type", "to_type"]
            }
        ),
        types.Tool(
            name="delete_relationship",
            description="Remove a relationship between two people.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person": {"type": "string"},
                    "relative": {"type": "string"},
                    "type": {"type": "string", "enum": RELATIONSHIP_TYPES, "description": "Relationship type to delete"}
                },
                "required": ["person", "relative", "type"]
            }
        ),
        types.Tool(
            name="between",
            description="List all relationships between two specific people.",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_a": {"type": "string"},
                    "person_b": {"type": "string"}
                },
                "required": ["person_a", "person_b"]
            }
        ),
        types.Tool(
            name="delete_person",
            description="Delete a person and all their relationships. Pass force=true to actually delete (default is dry-run).",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": {"type": "string"},
                    "force": {"type": "boolean", "default": False}
                },
                "required": ["name_or_id"]
            }
        ),
        types.Tool(
            name="rebuild_inferred",
            description="Clear and regenerate all inferred family relationships from the parent graph. Run after bulk relationship changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": {"type": "string", "description": "Rebuild for one person only (optional — default: everyone)"}
                }
            }
        ),
        types.Tool(
            name="graph_relationships",
            description="Generate a Mermaid family tree graph and write it to the Obsidian vault. Run after relationship changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": {"type": "string", "description": "Root person (default: Steven)"},
                    "depth": {"type": "integer", "default": 2},
                    "type": {"type": "string", "description": "Filter: 'family', 'explicit', or a specific type"}
                }
            }
        ),
        types.Tool(
            name="update_obsidian_links",
            description="Update Obsidian people notes with wiki-linked relationship sections. Run after relationship changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_or_id": {"type": "string", "description": "Update one person only (optional — default: everyone)"}
                }
            }
        ),
        types.Tool(
            name="check_integrity",
            description="Run a database integrity check. Returns errors (missing reciprocals, unknown types, etc.) and warnings.",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="repair_db",
            description="Auto-fix safe database issues (missing reciprocals, self-relationships). Flags issues that need manual review.",
            inputSchema={"type": "object", "properties": {}}
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
    if name == "find_person":
        con = p.connect()
        try:
            rows = con.execute(
                "SELECT id, name, gender, pronouns, birthdate, location, profession, notes "
                "FROM people WHERE name LIKE ? COLLATE NOCASE ORDER BY name",
                (f"%{args['name']}%",)
            ).fetchall()
            return json.dumps([p.person_to_dict(r) for r in rows], default=str)
        except SystemExit:
            return json.dumps({"error": f"No people found matching '{args['name']}'"})
        finally:
            con.close()

    elif name == "show_person":
        con = p.connect()
        buf_err = io.StringIO()
        try:
            with redirect_stderr(buf_err):
                person = p.resolve_person(con, args["name_or_id"])
            result = p.person_to_dict(person)
            rels = con.execute(
                "SELECT r.relative_is, r.relative_qualifier, p2.id, p2.name, p2.gender, p2.pronouns "
                "FROM relationships r JOIN people p2 ON p2.id=r.relative_id "
                "WHERE r.person_id=? ORDER BY r.relative_is, p2.name",
                (person["id"],)
            ).fetchall()
            result["relationships"] = [dict(r) for r in rels]
            return json.dumps(result, default=str)
        except SystemExit:
            err = buf_err.getvalue().strip()
            return err if err else json.dumps({"error": f"Person not found: {args['name_or_id']}"})
        finally:
            con.close()

    elif name == "get_relatives":
        a = argparse.Namespace(
            name_or_id=args["name_or_id"],
            type=args.get("type"),
            infer=args.get("infer", True),
            db=None, pretty=False
        )
        return _capture(lambda: p.cmd_relatives(a))

    elif name == "add_person":
        field_map = {f: args.get(f) for f in p.PERSON_FIELDS if f != "name"}
        a = argparse.Namespace(name=args["name"], db=None, pretty=False, **field_map)
        return _capture(lambda: p.cmd_add_person(a))

    elif name == "update_person":
        field_map = {f: args.get(f) for f in p.PERSON_FIELDS}
        a = argparse.Namespace(name_or_id=args["name_or_id"], db=None, pretty=False, **field_map)
        return _capture(lambda: p.cmd_update_person(a))

    elif name == "relate":
        a = argparse.Namespace(
            person=args["person"],
            relative=args["relative"],
            relative_is=args["relative_is"],
            qualifier=args.get("qualifier"),
            notes=args.get("notes"),
            db=None, pretty=False
        )
        return _capture(lambda: p.cmd_relate(a))

    elif name == "update_relationship":
        a = argparse.Namespace(
            person=args["person"],
            relative=args["relative"],
            from_type=args["from_type"],
            to_type=args["to_type"],
            db=None, pretty=False
        )
        return _capture(lambda: p.cmd_update_relationship(a))

    elif name == "delete_relationship":
        a = argparse.Namespace(
            person=args["person"],
            relative=args["relative"],
            type=args["type"],
            db=None, pretty=False
        )
        return _capture(lambda: p.cmd_delete_relationship(a))

    elif name == "between":
        a = argparse.Namespace(
            person_a=args["person_a"],
            person_b=args["person_b"],
            db=None, pretty=False
        )
        return _capture(lambda: p.cmd_between(a))

    elif name == "delete_person":
        a = argparse.Namespace(
            name_or_id=args["name_or_id"],
            force=args.get("force", False),
            db=None, pretty=False
        )
        return _capture(lambda: p.cmd_delete_person(a))

    elif name == "rebuild_inferred":
        a = argparse.Namespace(name_or_id=args.get("name_or_id"), db=None, pretty=False)
        return _capture(lambda: p.cmd_rebuild_inferred(a))

    elif name == "graph_relationships":
        a = argparse.Namespace(
            name_or_id=args.get("name_or_id", "Steven"),
            depth=args.get("depth", 2),
            type=args.get("type", "family"),
            output=None,
            db=None, pretty=False
        )
        return _capture(lambda: p.cmd_graph(a))

    elif name == "update_obsidian_links":
        a = argparse.Namespace(name_or_id=args.get("name_or_id"), db=None, pretty=False)
        return _capture(lambda: p.cmd_links(a))

    elif name == "check_integrity":
        a = argparse.Namespace(db=None, pretty=False)
        return _capture(lambda: p.cmd_check(a))

    elif name == "repair_db":
        a = argparse.Namespace(db=None, pretty=False)
        return _capture(lambda: p.cmd_repair(a))

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
