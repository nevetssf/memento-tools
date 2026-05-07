#!/usr/bin/env python3
"""
cellar-mcp.py — MCP server for wine and spirits cellar notes in Obsidian.

Manages tasting notes in Cellar/{Wine,Whiskey,Gin,Vodka}/ with per-producer
files containing multiple bottle entries using inline Dataview fields.
"""

import asyncio
import contextlib
import fcntl
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

sys.path.insert(0, str(Path(__file__).parent))
from config import CELLAR_DIRS, LOCATION_FILE, TEMPLATES_DIR
from journal_fm import get_local_date


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _validate_type(type_: str | None) -> tuple[str, str | None]:
    """Normalize + validate. Returns (lowercased_type, error_message_or_None)."""
    if not type_:
        return "", "Missing required `type`"
    t = type_.lower()
    if t not in VALID_TYPES:
        return t, f"Invalid type '{type_}'. Valid: {', '.join(VALID_TYPES)}"
    return t, None


@contextlib.contextmanager
def _file_lock(path: Path):
    """Exclusive flock via a sidecar `.lock` file.

    Same pattern as chat_signal / priorities. Serializes concurrent writers —
    relevant when Obsidian sync (or another agent) might touch the same file
    during a read-modify-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

app = Server("cellar-db")

VALID_TYPES = list(CELLAR_DIRS.keys())  # wine, whiskey, gin, vodka, tequila, mezcal, rum, port

# Bolded `**Marker:**` body-section names per type, mirroring the
# Templates/<Type> Note.md files. Used by update_bottle to decide whether
# a given field is a body section (use update_body_section) or an inline
# Dataview field (use update_inline_field). Case-insensitive matching
# against this list — preserves the canonical casing of the marker.
BODY_SECTIONS = {
    "wine":    ["Appearance", "Nose", "Palate", "Finish", "Notes", "Food pairings"],
    "whiskey": ["Color", "Nose", "Palate", "With water", "Finish", "Notes"],
    "gin":     ["Nose", "Palate", "Finish", "Notes"],
    "vodka":   ["Nose", "Palate", "Finish", "Notes"],
    "tequila": ["Nose", "Palate", "Finish", "Notes"],
    "mezcal":  ["Color", "Nose", "Palate", "Finish", "Notes"],
    "rum":     ["Color", "Nose", "Palate", "Finish", "Notes"],
    "port":    ["Color", "Nose", "Palate", "Finish", "Notes", "Food pairings"],
}

# Flat set of all body markers across types — used by parse_entries to
# scan a bottle's body for `Marker: value` lines without needing to know
# the bottle's type.
_ALL_BODY_MARKERS = {m for markers in BODY_SECTIONS.values() for m in markers}

# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _read_template(type_: str) -> str | None:
    path = TEMPLATES_DIR / f"{type_.title()} Note.md"
    return path.read_text() if path.exists() else None


def _fill(template: str, values: dict) -> str:
    """Replace {{key}} placeholders; remove any that remain unfilled."""
    for key, val in values.items():
        template = template.replace(f"{{{{{key}}}}}", str(val) if val else "")
    return re.sub(r"\{\{[^}]+\}\}", "", template)


_FILL_SKIP = {"type", "producer", "name", "region", "country", "overwrite", "append"}


def _fill_inline_fields(section: str, fields: dict) -> str:
    """Populate inline `field:: value` lines from the given dict.

    For fields that exist as `field::` in the section, replace the empty
    value. For fields NOT in the template (e.g. `quantity::` on a gin
    bottle, since the gin template doesn't pre-declare it), append a new
    inline line after the last existing `field::` so the value isn't lost.

    Skips structural args (type/producer/name/region/country) that aren't
    bottle metadata. Booleans are normalized to lowercase ("true"/"false").
    """
    for field, val in fields.items():
        if field in _FILL_SKIP:
            continue
        if val is None or str(val) == "":
            continue
        if isinstance(val, bool):
            val = "true" if val else "false"
        pattern = rf"^{re.escape(field)}::[ \t]*$"
        new_line = f"{field}:: {val}"
        section_new, n = re.subn(pattern, new_line, section, count=1, flags=re.MULTILINE)
        if n:
            section = section_new
            continue
        # Field not in template — append after the last existing inline field.
        last_field_end = max(
            (m.end() for m in re.finditer(r'^\w+::.*\n?', section, re.MULTILINE)),
            default=None,
        )
        if last_field_end is not None:
            section = section[:last_field_end] + new_line + "\n" + section[last_field_end:]
    return section


def producer_content(type_: str, producer: str, region: str, country: str) -> str:
    """Build producer file content from template (plain text, no italic)."""
    loc = f"{region}{', ' + country if country else ''}" if region else country or ""
    values = {"producer": producer, "region": region or "", "country": country or "", "location": loc}
    tmpl = _read_template(type_)
    if tmpl:
        # Take everything up to (but not including) the first body --- divider
        parts = tmpl.split("\n---\n")
        producer_tmpl = parts[0] + "\n---\n" + parts[1] if len(parts) >= 2 else tmpl
        return _fill(producer_tmpl, values).rstrip() + "\n"
    # Fallback if template missing
    key = "vintner" if type_ == "wine" else "shipper" if type_ == "port" else "distillery"
    loc_line = f"\n{loc}\n" if loc else ""
    return f"---\n{key}: {producer}\nregion: {region or ''}\ncountry: {country or ''}\ntags: [{type_}]\n---\n\n# {producer}\n{loc_line}"


def entry_content(type_: str, name: str, fields: dict) -> str:
    """Build a bottle entry section from template."""
    tmpl = _read_template(type_)
    if tmpl:
        parts = tmpl.split("\n---\n")
        # Entry section is the third part (index 2)
        entry_tmpl = parts[2] if len(parts) >= 3 else ""
        if entry_tmpl:
            filled = _fill(entry_tmpl, {"name": name})
            return _fill_inline_fields(filled, fields)
    # Fallback: build from known fields
    lines = [f"\n## {name}\n"]
    for field, val in fields.items():
        if field not in ("type", "producer", "name", "region", "country"):
            lines.append(f"{field}:: {val if val is not None else ''}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def producer_path(type: str, producer: str) -> Path:
    return CELLAR_DIRS[type] / f"{producer}.md"


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


def update_inline_field(section: str, field: str, value: str, append: bool = False) -> str:
    """Replace or set an inline `field:: value` line in a section.

    With `append=True`, the new value is joined to any existing non-empty
    value with `, ` (list separator) instead of replacing. If the existing
    value is empty, behaves the same as replace (no stray leading separator).
    """
    pattern = rf'^({re.escape(field)}::[ \t]*)(.*)$'

    def _replace(m: re.Match) -> str:
        prefix, existing = m.group(1), m.group(2).strip()
        if append and existing:
            return f"{prefix}{existing}, {value}"
        return f"{field}:: {value}"

    updated, n = re.subn(pattern, _replace, section, flags=re.MULTILINE)
    if n:
        return updated
    # Field doesn't exist yet — append a new line after the last inline field.
    new_line = f"{field}:: {value}"
    last_field = max((m.end() for m in re.finditer(r'^\w+::.*\n?', section, re.MULTILINE)), default=None)
    if last_field:
        return section[:last_field] + new_line + "\n" + section[last_field:]
    return section + new_line + "\n"


def update_body_section(section: str, key: str, value: str, append: bool = False) -> tuple[str, bool]:
    """Replace or set the value after a plain `Key:` marker.

    Section markers are plain text now (no bold) — `Notes: …`, `Nose: …`,
    etc. — so they render in a regular font in Obsidian. The single colon
    distinguishes them from inline Dataview fields (`field:: value`, double
    colon).

    Returns (new_section, found). If `found` is False, the section is
    unchanged because the marker isn't present — caller should surface
    that as an error rather than silently writing nothing.

    With `append=True`, the new value is joined to any existing non-empty
    value with ` — ` (em dash) instead of replacing. If the existing value
    is empty, behaves the same as replace.
    """
    # `(?!:)` lookahead: don't match `Key::` (which is a Dataview inline field).
    pattern = rf'^({re.escape(key)}:(?!:)[ \t]*)(.*)$'

    def _replace(m: re.Match) -> str:
        prefix, existing = m.group(1), m.group(2).strip()
        if append and existing:
            return f"{prefix}{existing} — {value}"
        return f"{prefix}{value}"

    updated, n = re.subn(pattern, _replace, section, flags=re.MULTILINE)
    return (updated, True) if n else (section, False)


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
        # Inline Dataview fields: `key:: value` (double colon).
        # `[ \t]*` not `\s*` — `\s` matches newlines, so an empty `style::`
        # line would absorb the next line's content.
        for match in re.finditer(r'^(\w+)::[ \t]*(.*)$', section, re.MULTILINE):
            entry[match.group(1)] = match.group(2).strip()
        # Body section markers (plain text, single colon). Enumerate the
        # known markers across all types so we don't accidentally match
        # arbitrary `Word: text` prose lines as section markers.
        for marker in _ALL_BODY_MARKERS:
            body_re = rf'^{re.escape(marker)}:(?!:)[ \t]*(.*)$'
            bm = re.search(body_re, section, re.MULTILINE)
            if bm:
                entry[f"_{marker.lower().replace(' ', '_')}"] = bm.group(1).strip()
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    types_csv = ", ".join(VALID_TYPES)
    type_prop = {"type": "string", "description": f"Category: {types_csv}"}
    producer_prop = {"type": "string", "description": "Producer/vintner/distillery name (used as filename)"}
    name_prop = {"type": "string", "description": "Bottle/expression name"}

    return [
        types.Tool(
            name="get_types",
            description="Return all valid cellar categories.",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="add_producer",
            description="Create a new producer file without adding a bottle. Use when you want to record a producer before tasting any specific bottles.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "region": {"type": "string"},
                    "country": {"type": "string"},
                    "style": {"type": "string", "description": "Whiskey type or gin style"},
                },
                "required": ["type", "producer"]
            }
        ),
        types.Tool(
            name="add_bottle",
            description=(
                "Add a new bottle entry to a producer's note. Creates the producer "
                "file if it doesn't exist. Errors if a bottle with the same name "
                "already exists; pass `overwrite=true` to replace it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "region": {"type": "string"},
                    "country": {"type": "string"},
                    "varietal": {"type": "string", "description": "Wine only — grape variety or blend"},
                    "vintage": {"type": "integer", "description": "Wine/whiskey/port — year"},
                    "age": {"type": "string", "description": "Whiskey/rum/tequila — age statement e.g. '12' or 'NAS'"},
                    "abv": {"type": "number"},
                    "cask": {"type": "string", "description": "Whiskey/rum — cask type e.g. 'ex-bourbon, sherry'"},
                    "botanicals": {"type": "string", "description": "Gin — key botanicals"},
                    "base": {"type": "string", "description": "Vodka — base ingredient e.g. 'potato', 'grain'"},
                    "style": {"type": "string", "description": "Style label — e.g. 'London Dry', 'Contemporary' (gin), 'Single Malt' (whiskey)"},
                    "price": {"type": "number"},
                    "quantity": {"type": "integer"},
                    "in_cellar": {"type": "boolean"},
                    "overwrite": {"type": "boolean", "default": False, "description": "Replace an existing bottle with the same name. Default false → duplicate adds error."},
                },
                "required": ["type", "producer", "name"]
            }
        ),
        types.Tool(
            name="update_bottle",
            description=(
                "Update a specific field in an existing bottle entry. "
                "Default: replace the existing value. With `append=true`, "
                "join the new value to the existing one (` — ` for body "
                "sections like Notes/Nose/Palate; `, ` for inline list "
                "fields like botanicals). Useful for accumulating tasting "
                "notes across sessions without losing prior impressions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "field": {"type": "string", "description": "Field name — inline Dataview (varietal, vintage, abv, botanicals, etc., written `key:: value`) or body marker (Nose, Palate, Finish, Notes, Color, Appearance, Food pairings, With water — written `Marker: value` in plain text)"},
                    "value": {"type": "string"},
                    "append": {
                        "type": "boolean",
                        "default": False,
                        "description": "Append to existing value with a sensible separator. Use for tasting notes / list-shaped fields. Skip for scalar fields (rating, vintage, abv) — those should be replaced."
                    },
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
            name="delete_bottle",
            description=(
                "Remove a single bottle entry from a producer's file. The "
                "producer file itself is preserved with any other bottles "
                "intact. Irreversible — use delete_producer to remove the "
                "entire file instead. The vault is backed up nightly so "
                "accidental deletes can be recovered from restic."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                },
                "required": ["type", "producer", "name"]
            }
        ),
        types.Tool(
            name="delete_producer",
            description=(
                "Delete an entire producer file (and ALL its bottle entries). "
                "Irreversible — use this only when removing a producer "
                "wholesale. The vault is backed up nightly so accidental "
                "deletes can be recovered from restic."
            ),
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
            description="Search cellar notes. Returns matching bottles. Omit `type` to search all categories. All filters combine with AND.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": f"Filter by category ({types_csv}) — omit to search all"},
                    "producer": {"type": "string", "description": "Filter by producer name (partial match)"},
                    "name":     {"type": "string", "description": "Filter by bottle/expression name (partial match)"},
                    "varietal": {"type": "string", "description": "Filter by varietal/grape (wine)"},
                    "vintage":  {"type": "integer", "description": "Filter by vintage year"},
                    "min_rating": {"type": "number", "description": "Minimum rating"},
                    "would_buy_again": {"type": "string", "description": "Filter by yes/no/maybe"},
                },
                "required": []
            }
        ),
        types.Tool(
            name="get_bottle",
            description=(
                "Return one bottle's parsed entry as a dict (inline fields like "
                "varietal/vintage/abv/rating, plus body sections prefixed with `_` "
                "like `_nose`, `_palate`, `_finish`, `_notes`). Use this instead of "
                "get_note when you only need one bottle's data — saves the agent "
                "from re-parsing a producer file's full markdown."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                },
                "required": ["type", "producer", "name"]
            }
        ),
        types.Tool(
            name="recent_tastings",
            description=(
                "Bottles sorted by date_tasted descending. Useful for 'what did I drink "
                "recently?' Only includes bottles that have a `date_tasted` set."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type":  {"type": "string", "description": f"Filter by category ({types_csv}) — omit for all"},
                    "limit": {"type": "integer", "default": 20, "description": "Maximum bottles to return (default 20)"},
                },
                "required": []
            }
        ),
        types.Tool(
            name="untasted_bottles",
            description=(
                "Bottles with no rating set yet. Useful for 'what should I open next?' "
                "By default only includes bottles still in the cellar (in_cellar != false)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": f"Filter by category ({types_csv}) — omit for all"},
                    "in_cellar_only": {"type": "boolean", "default": True, "description": "If true (default), only bottles still in the cellar."},
                },
                "required": []
            }
        ),
        types.Tool(
            name="consume_bottle",
            description=(
                "Decrement a bottle's `quantity` after drinking some. Sets `in_cellar=false` "
                "automatically when the new quantity reaches 0. Default decrement is 1. "
                "Use this for 'I had a glass / finished a bottle' moments."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "decrement": {"type": "integer", "default": 1, "description": "How many to remove (default 1). For 'I finished it', use the current quantity."},
                },
                "required": ["type", "producer", "name"]
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

    if name == "add_producer":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        producer = args["producer"]
        path = producer_path(type_, producer)
        if path.exists():
            return json.dumps({"ok": False, "message": f"Producer '{producer}' already exists", "file": str(path)})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(producer_content(type_, producer, args.get("region", ""), args.get("country", "")))
        return json.dumps({"ok": True, "file": str(path), "producer": producer, "type": type_})

    if name == "add_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        producer = args["producer"]
        bottle_name = args["name"]
        overwrite = bool(args.get("overwrite", False))
        path = producer_path(type_, producer)

        with _file_lock(path):
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(producer_content(type_, producer, args.get("region", ""), args.get("country", "")))

            text = path.read_text()
            # Exact-match dedup (parse_entries gives us each bottle's _name).
            existing_names = [e["_name"] for e in parse_entries(text)]
            if bottle_name in existing_names:
                if not overwrite:
                    return json.dumps({
                        "error": f"Bottle '{bottle_name}' already exists at {path}. Use update_bottle to modify, or pass overwrite=true to replace.",
                        "file": str(path),
                    })
                # Remove the existing entry, then fall through to append the new one.
                bounds = find_entry_bounds(text, bottle_name)
                if bounds:
                    start, end = bounds
                    text = text[:start] + text[end:]
                    path.write_text(text)

            entry = entry_content(type_, bottle_name, args)
            text = path.read_text()
            path.write_text(text.rstrip() + "\n\n---\n" + entry + "---\n")
        return json.dumps({
            "ok": True,
            "file": str(path),
            "producer": producer,
            "name": bottle_name,
            "overwrote": overwrite and bottle_name in existing_names,
        })

    if name == "update_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})

        with _file_lock(path):
            text = path.read_text()
            bounds = find_entry_bounds(text, args["name"])
            if not bounds:
                return json.dumps({"error": f"No entry found matching '{args['name']}'"})

            start, end = bounds
            section = text[start:end]
            field, value = args["field"], args["value"]

            # Case-insensitive match against the type's body markers. If the
            # field matches, route to update_body_section using the canonical
            # marker casing (e.g. "Food pairings", not "Food Pairings" — title()
            # would mangle multi-word markers). Otherwise treat as an inline
            # Dataview field (`field:: value`).
            append = bool(args.get("append", False))
            body_markers = BODY_SECTIONS.get(type_, [])
            marker = next((m for m in body_markers if m.lower() == field.lower()), None)
            if marker:
                section, found = update_body_section(section, marker, value, append=append)
                if not found:
                    return json.dumps({
                        "error": f"Body marker '{marker}:' not found in this bottle entry. "
                                 f"The bottle may have been hand-edited or use a non-template layout.",
                    })
            else:
                section = update_inline_field(section, field, value, append=append)

            path.write_text(text[:start] + section + text[end:])
        return json.dumps({"ok": True, "field": field, "value": value})

    if name == "delete_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})
        with _file_lock(path):
            text = path.read_text()
            bounds = find_entry_bounds(text, args["name"])
            if not bounds:
                return json.dumps({"error": f"No entry found matching '{args['name']}'"})
            start, end = bounds
            path.write_text(text[:start] + text[end:])
        return json.dumps({
            "ok": True,
            "removed_bottle": args["name"],
            "producer": args["producer"],
            "type": type_,
        })

    if name == "delete_producer":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})
        with _file_lock(path):
            path.unlink()
        return json.dumps({
            "ok": True,
            "deleted_producer": args["producer"],
            "type": type_,
            "file": str(path),
        })

    if name == "rate_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        # Validate rating is numeric. A non-numeric value would be written
        # verbatim, then silently break the min_rating filter in
        # search_cellar (float() would raise → entry skipped).
        try:
            rating_val = float(args["rating"])
        except (TypeError, ValueError):
            return json.dumps({"error": f"`rating` must be numeric (got {args.get('rating')!r})"})
        # Render as int when whole, otherwise keep one decimal — matches how
        # ratings are typically written ("88" not "88.0").
        rating_str = str(int(rating_val)) if rating_val == int(rating_val) else f"{rating_val:.1f}"

        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})

        with _file_lock(path):
            text = path.read_text()
            bounds = find_entry_bounds(text, args["name"])
            if not bounds:
                return json.dumps({"error": f"No entry found matching '{args['name']}'"})

            start, end = bounds
            section = text[start:end]
            section = update_inline_field(section, "rating", rating_str)
            section = update_inline_field(section, "date_tasted", args.get("date_tasted") or get_local_date())
            if args.get("would_buy_again"):
                section = update_inline_field(section, "would_buy_again", args["would_buy_again"])

            path.write_text(text[:start] + section + text[end:])
        return json.dumps({"ok": True, "rating": rating_val, "date_tasted": args.get("date_tasted") or get_local_date()})

    if name == "consume_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        decrement = max(1, int(args.get("decrement", 1)))
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})

        with _file_lock(path):
            text = path.read_text()
            bounds = find_entry_bounds(text, args["name"])
            if not bounds:
                return json.dumps({"error": f"No entry found matching '{args['name']}'"})
            start, end = bounds
            section = text[start:end]

            # `[ \t]*` not `\s*` — `\s` matches newlines, which would let
            # an empty `quantity::` line absorb the next line's content.
            qty_match = re.search(r'^quantity::[ \t]*(.*)$', section, re.MULTILINE)
            current_str = qty_match.group(1).strip() if qty_match else ""
            try:
                current = int(current_str)
            except ValueError:
                current = 0

            new_qty = max(0, current - decrement)
            section = update_inline_field(section, "quantity", str(new_qty))
            if new_qty == 0:
                section = update_inline_field(section, "in_cellar", "false")
            path.write_text(text[:start] + section + text[end:])

        return json.dumps({
            "ok": True,
            "name": args["name"],
            "previous_quantity": current,
            "new_quantity": new_qty,
            "in_cellar": new_qty > 0,
        })

    if name == "get_note":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})
        return json.dumps({"producer": args["producer"], "type": type_, "content": path.read_text()})

    if name == "get_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})
        entries = parse_entries(path.read_text())
        name_q = args["name"].strip().lower()
        # Prefer exact match (case-insensitive) over substring; on substring,
        # require unambiguity so the agent doesn't quietly grab the wrong bottle.
        exact = [e for e in entries if e["_name"].lower() == name_q]
        if exact:
            return json.dumps({"type": type_, "producer": args["producer"], "bottle": exact[0]})
        substr = [e for e in entries if name_q in e["_name"].lower()]
        if not substr:
            return json.dumps({"error": f"No bottle matching '{args['name']}'"})
        if len(substr) > 1:
            return json.dumps({
                "error": f"Ambiguous match for '{args['name']}'",
                "candidates": [e["_name"] for e in substr],
            })
        return json.dumps({"type": type_, "producer": args["producer"], "bottle": substr[0]})

    if name == "list_producers":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        dir_ = CELLAR_DIRS[type_]
        if not dir_.exists():
            return json.dumps({"producers": []})
        producers = sorted(p.stem for p in dir_.glob("*.md"))
        return json.dumps({"type": type_, "producers": producers, "count": len(producers)})

    if name == "search_cellar":
        if args.get("type"):
            t, err = _validate_type(args["type"])
            if err: return json.dumps({"error": err})
            types_to_search = [t]
        else:
            types_to_search = VALID_TYPES

        results = []
        name_q = (args.get("name") or "").strip().lower()
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
                    if name_q and name_q not in entry.get("_name", "").lower():
                        continue
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

    if name == "recent_tastings":
        if args.get("type"):
            t, err = _validate_type(args["type"])
            if err: return json.dumps({"error": err})
            types_to_walk = [t]
        else:
            types_to_walk = VALID_TYPES
        limit = int(args.get("limit", 20))

        bottles = []
        for type_ in types_to_walk:
            dir_ = CELLAR_DIRS.get(type_)
            if not dir_ or not dir_.exists():
                continue
            for md_file in dir_.glob("*.md"):
                try:
                    entries = parse_entries(md_file.read_text())
                except Exception:
                    continue
                for entry in entries:
                    date = entry.get("date_tasted", "").strip()
                    if not date:
                        continue
                    bottles.append({"type": type_, "producer": md_file.stem, **entry})

        bottles.sort(key=lambda b: b.get("date_tasted", ""), reverse=True)
        return json.dumps({
            "results": bottles[:limit],
            "returned": min(len(bottles), limit),
            "total_with_dates": len(bottles),
        })

    if name == "untasted_bottles":
        if args.get("type"):
            t, err = _validate_type(args["type"])
            if err: return json.dumps({"error": err})
            types_to_walk = [t]
        else:
            types_to_walk = VALID_TYPES
        in_cellar_only = bool(args.get("in_cellar_only", True))

        results = []
        for type_ in types_to_walk:
            dir_ = CELLAR_DIRS.get(type_)
            if not dir_ or not dir_.exists():
                continue
            for md_file in dir_.glob("*.md"):
                try:
                    entries = parse_entries(md_file.read_text())
                except Exception:
                    continue
                for entry in entries:
                    if entry.get("rating", "").strip():
                        continue
                    if in_cellar_only:
                        # Empty defaults to "in cellar" for new bottles; only
                        # exclude when explicitly false.
                        if entry.get("in_cellar", "").strip().lower() == "false":
                            continue
                    results.append({"type": type_, "producer": md_file.stem, **entry})

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
