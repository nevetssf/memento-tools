#!/usr/bin/env python3
"""
cellar-mcp.py — MCP server for wine and spirits cellar notes in Obsidian.

Manages tasting notes in Cellar/{Wine,Whiskey,Gin,Vodka}/ with per-producer
files containing multiple bottle entries using inline Dataview fields.
"""

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

sys.path.insert(0, str(Path(__file__).parent))
from config import CELLAR_DIRS, LOCATION_FILE
from journal_fm import get_local_date

app = Server("cellar-db")

VALID_TYPES = list(CELLAR_DIRS.keys())  # wine, whiskey, gin, vodka

# Frontmatter template per type
FM_TEMPLATES = {
    "wine":    lambda producer, region, country: f"---\nvintner: {producer}\nregion: {region or ''}\ncountry: {country or ''}\ntags: [wine]\n---\n\n# {producer}\n\n*{region or ''}{', ' + country if country else ''}*\n",
    "whiskey": lambda producer, region, country: f"---\ndistillery: {producer}\nregion: {region or ''}\ncountry: {country or ''}\ntags: [whiskey]\n---\n\n# {producer}\n\n*{region or ''}{', ' + country if country else ''}*\n",
    "gin":     lambda producer, region, country: f"---\ndistillery: {producer}\nregion: {region or ''}\ncountry: {country or ''}\ntags: [gin]\n---\n\n# {producer}\n\n*{region or ''}{', ' + country if country else ''}*\n",
    "vodka":   lambda producer, region, country: f"---\ndistillery: {producer}\ncountry: {country or ''}\ntags: [vodka]\n---\n\n# {producer}\n\n*{country or ''}*\n",
}

# Inline fields per type
ENTRY_FIELDS = {
    "wine":    ["varietal", "vintage", "alcohol", "price", "rating", "date_tasted", "in_cellar", "quantity", "would_buy_again"],
    "whiskey": ["age", "abv", "cask", "price", "rating", "date_tasted", "in_collection", "quantity", "would_buy_again"],
    "gin":     ["style", "abv", "botanicals", "price", "rating", "date_tasted", "would_buy_again", "best_serve"],
    "vodka":   ["base", "abv", "filtration", "price", "rating", "date_tasted", "would_buy_again", "best_serve"],
}

BODY_SECTIONS = {
    "wine":    ["Appearance", "Nose", "Palate", "Finish", "Food pairings", "Notes"],
    "whiskey": ["Color", "Nose", "Palate", "Finish", "With water", "Notes"],
    "gin":     ["Nose", "Palate", "Finish", "Notes"],
    "vodka":   ["Nose", "Palate", "Finish", "Notes"],
}


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def producer_path(type: str, producer: str) -> Path:
    return CELLAR_DIRS[type] / f"{producer}.md"


def build_entry(name: str, type: str, fields: dict) -> str:
    """Build a new bottle entry section."""
    lines = [f"\n## {name}\n"]
    for field in ENTRY_FIELDS[type]:
        val = fields.get(field, "")
        lines.append(f"{field}:: {val}")
    lines.append("")
    for section in BODY_SECTIONS[type]:
        lines.append(f"**{section}:** ")
        lines.append("")
    lines.append("")
    return "\n".join(lines)


def find_entry_bounds(text: str, name: str) -> tuple[int, int] | None:
    """Return (start, end) byte offsets of the entry section matching name."""
    # Find all section boundaries (--- dividers after frontmatter)
    fm_end = text.find("\n---\n", text.find("\n---\n") + 1)
    if fm_end == -1:
        fm_end = text.find("\n---\n")
    body_start = fm_end + 5 if fm_end != -1 else 0

    body = text[body_start:]
    pattern = re.compile(r'(?:^|\n)---\n', re.MULTILINE)
    dividers = [0] + [m.end() for m in pattern.finditer(body)] + [len(body)]

    for i in range(len(dividers) - 1):
        section = body[dividers[i]:dividers[i + 1]]
        # Match ## heading containing name (case-insensitive)
        if re.search(rf'^##\s+.*{re.escape(name)}', section, re.IGNORECASE | re.MULTILINE):
            return (body_start + dividers[i], body_start + dividers[i + 1])
    return None


def update_inline_field(section: str, field: str, value: str) -> str:
    """Replace or append an inline field in a section."""
    pattern = rf'^{re.escape(field)}::[ \t]*.*'
    new_line = f"{field}:: {value}"
    updated, n = re.subn(pattern, new_line, section, flags=re.MULTILINE)
    if n:
        return updated
    # Append after last inline field line
    last_field = max((m.end() for m in re.finditer(r'^\w+::.*\n?', section, re.MULTILINE)), default=None)
    if last_field:
        return section[:last_field] + new_line + "\n" + section[last_field:]
    return section + new_line + "\n"


def update_body_section(section: str, key: str, value: str) -> str:
    """Replace the value after a **Key:** marker."""
    pattern = rf'(\*\*{re.escape(key)}:\*\*[ \t]*).*'
    updated, n = re.subn(pattern, rf'\g<1>{value}', section, flags=re.MULTILINE)
    return updated if n else section


def parse_entries(text: str) -> list[dict]:
    """Parse all bottle entries from a producer file."""
    fm_end = text.find("\n---\n", text.find("\n---\n") + 1)
    body_start = (fm_end + 5) if fm_end != -1 else 0
    body = text[body_start:]

    entries = []
    sections = re.split(r'\n---\n', body)
    for section in sections:
        section = section.strip()
        if not section:
            continue
        m = re.search(r'^##\s+(.+)', section, re.MULTILINE)
        if not m:
            continue
        entry = {"_name": m.group(1).strip()}
        for match in re.finditer(r'^(\w+)::\s*(.*)', section, re.MULTILINE):
            entry[match.group(1)] = match.group(2).strip()
        for bm in re.finditer(r'\*\*([^*]+):\*\*\s*(.*)', section):
            entry[f"_{bm.group(1).lower().replace(' ', '_')}"] = bm.group(2).strip()
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    type_prop = {"type": "string", "description": "Category: wine, whiskey, gin, or vodka"}
    producer_prop = {"type": "string", "description": "Producer/vintner/distillery name (used as filename)"}
    name_prop = {"type": "string", "description": "Bottle/expression name"}

    return [
        types.Tool(
            name="get_types",
            description="Return all valid cellar categories.",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="add_bottle",
            description="Add a new bottle entry to a producer's note. Creates the producer file if it doesn't exist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "region": {"type": "string"},
                    "country": {"type": "string"},
                    "varietal": {"type": "string", "description": "Wine only — grape variety or blend"},
                    "vintage": {"type": "integer", "description": "Wine/whiskey — year"},
                    "age": {"type": "string", "description": "Whiskey only — age statement e.g. '12' or 'NAS'"},
                    "abv": {"type": "number"},
                    "cask": {"type": "string", "description": "Whiskey — cask type e.g. 'ex-bourbon, sherry'"},
                    "botanicals": {"type": "string", "description": "Gin — key botanicals"},
                    "base": {"type": "string", "description": "Vodka — base ingredient e.g. 'potato', 'grain'"},
                    "style": {"type": "string", "description": "Gin — style e.g. 'London Dry', 'Contemporary'"},
                    "price": {"type": "number"},
                    "quantity": {"type": "integer"},
                    "in_cellar": {"type": "boolean"},
                },
                "required": ["type", "producer", "name"]
            }
        ),
        types.Tool(
            name="update_bottle",
            description="Update a specific field in an existing bottle entry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "field": {"type": "string", "description": "Field name — inline (varietal, vintage, nose, palate, finish, rating, etc.) or body section (Nose, Palate, Finish, etc.)"},
                    "value": {"type": "string"},
                },
                "required": ["type", "producer", "name", "field", "value"]
            }
        ),
        types.Tool(
            name="rate_bottle",
            description="Set the rating, date tasted, and whether to buy again for a bottle.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "rating": {"type": "number", "description": "Rating out of 100"},
                    "would_buy_again": {"type": "string", "description": "yes, no, or maybe"},
                    "date_tasted": {"type": "string", "description": "YYYY-MM-DD (default: today)"},
                },
                "required": ["type", "producer", "name", "rating"]
            }
        ),
        types.Tool(
            name="get_note",
            description="Get the full note for a producer.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                },
                "required": ["type", "producer"]
            }
        ),
        types.Tool(
            name="list_producers",
            description="List all producers in a cellar category.",
            inputSchema={
                "type": "object",
                "properties": {"type": type_prop},
                "required": ["type"]
            }
        ),
        types.Tool(
            name="search_cellar",
            description="Search cellar notes. Returns all bottles of a given type, or filter by producer, varietal, vintage, min rating, etc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Filter by category (wine, whiskey, gin, vodka) — omit to search all"},
                    "producer": {"type": "string", "description": "Filter by producer name (partial match)"},
                    "varietal": {"type": "string", "description": "Filter by varietal/grape (wine)"},
                    "vintage": {"type": "integer", "description": "Filter by vintage year"},
                    "min_rating": {"type": "number", "description": "Minimum rating"},
                    "would_buy_again": {"type": "string", "description": "Filter by yes/no/maybe"},
                },
                "required": []
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
    except Exception as e:
        result = json.dumps({"error": str(e)})
    return [types.TextContent(type="text", text=result)]


def _dispatch(name: str, args: dict) -> str:
    if name == "get_types":
        return json.dumps({"types": VALID_TYPES})

    if name == "add_bottle":
        type_ = args["type"].lower()
        if type_ not in VALID_TYPES:
            return json.dumps({"error": f"Invalid type '{type_}'. Valid: {VALID_TYPES}"})
        producer = args["producer"]
        bottle_name = args["name"]
        path = producer_path(type_, producer)

        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            header = FM_TEMPLATES[type_](producer, args.get("region", ""), args.get("country", ""))
            path.write_text(header)

        entry = build_entry(bottle_name, type_, args)
        text = path.read_text()
        path.write_text(text.rstrip() + "\n\n---\n" + entry + "---\n")
        return json.dumps({"ok": True, "file": str(path), "producer": producer, "name": bottle_name})

    if name == "update_bottle":
        type_ = args["type"].lower()
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})
        text = path.read_text()
        bounds = find_entry_bounds(text, args["name"])
        if not bounds:
            return json.dumps({"error": f"No entry found matching '{args['name']}'"})

        start, end = bounds
        section = text[start:end]
        field, value = args["field"], args["value"]

        # Body section (Nose, Palate, etc.) vs inline field
        if field.title() in BODY_SECTIONS.get(type_, []):
            section = update_body_section(section, field.title(), value)
        else:
            section = update_inline_field(section, field, value)

        path.write_text(text[:start] + section + text[end:])
        return json.dumps({"ok": True, "field": field, "value": value})

    if name == "rate_bottle":
        type_ = args["type"].lower()
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})
        text = path.read_text()
        bounds = find_entry_bounds(text, args["name"])
        if not bounds:
            return json.dumps({"error": f"No entry found matching '{args['name']}'"})

        start, end = bounds
        section = text[start:end]
        section = update_inline_field(section, "rating", str(args["rating"]))
        section = update_inline_field(section, "date_tasted", args.get("date_tasted") or get_local_date())
        if args.get("would_buy_again"):
            section = update_inline_field(section, "would_buy_again", args["would_buy_again"])

        path.write_text(text[:start] + section + text[end:])
        return json.dumps({"ok": True, "rating": args["rating"], "date_tasted": args.get("date_tasted") or get_local_date()})

    if name == "get_note":
        type_ = args["type"].lower()
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})
        return json.dumps({"producer": args["producer"], "type": type_, "content": path.read_text()})

    if name == "list_producers":
        type_ = args["type"].lower()
        if type_ not in VALID_TYPES:
            return json.dumps({"error": f"Invalid type '{type_}'"})
        dir_ = CELLAR_DIRS[type_]
        if not dir_.exists():
            return json.dumps({"producers": []})
        producers = sorted(p.stem for p in dir_.glob("*.md"))
        return json.dumps({"type": type_, "producers": producers, "count": len(producers)})

    if name == "search_cellar":
        types_to_search = [args["type"].lower()] if args.get("type") else VALID_TYPES
        results = []
        for type_ in types_to_search:
            dir_ = CELLAR_DIRS.get(type_)
            if not dir_ or not dir_.exists():
                continue
            for md_file in sorted(dir_.glob("*.md")):
                producer = md_file.stem
                if args.get("producer") and args["producer"].lower() not in producer.lower():
                    continue
                try:
                    entries = parse_entries(md_file.read_text())
                except Exception:
                    continue
                for entry in entries:
                    if args.get("varietal") and args["varietal"].lower() not in entry.get("varietal", "").lower():
                        continue
                    if args.get("vintage") and str(args["vintage"]) != entry.get("vintage", ""):
                        continue
                    if args.get("min_rating"):
                        try:
                            if float(entry.get("rating", 0)) < float(args["min_rating"]):
                                continue
                        except ValueError:
                            continue
                    if args.get("would_buy_again") and entry.get("would_buy_again", "").lower() != args["would_buy_again"].lower():
                        continue
                    results.append({"type": type_, "producer": producer, **entry})

        return json.dumps({"results": results, "count": len(results)})

    return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
