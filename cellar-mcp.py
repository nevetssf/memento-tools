#!/usr/bin/env python3
"""
cellar-mcp.py — MCP server for wine and spirits cellar notes in Obsidian.

Storage model (current):
- One Markdown file per producer at `Cellar/<Type>/<Producer>.md`.
- YAML frontmatter holds producer-level metadata + a `bottles:` list. Each
  bottle is a dict (name, varietal, vintage, abv, rating, date_tasted,
  nose, palate, finish, etc.). All structured data lives in YAML.
- Body holds: `# Producer` H1, optional prose (history/notes about the
  producer), then one `## Bottle Name` section per bottle with a single
  `Notes: <free-form text>` line. Notes is the only per-bottle data
  that lives outside YAML — kept in body so it can be long-form prose.

Categories (per CELLAR_DIRS in config.py):
  wine, whiskey, gin, vodka, tequila, mezcal, rum, port

Concurrency: every read-modify-write holds an exclusive flock on a
sidecar `.lock` file (mirror of chat_signal / priorities pattern).
"""

import asyncio
import contextlib
import fcntl
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

sys.path.insert(0, str(Path(__file__).parent))
from config import CELLAR_DIRS
from journal_fm import get_local_date


app = Server("cellar-db")

VALID_TYPES = list(CELLAR_DIRS.keys())  # wine, whiskey, gin, vodka, tequila, mezcal, rum, port

# Per-type field hints — drives tool descriptions, doesn't restrict the
# schema. Bottles can have arbitrary additional keys.
TYPE_FIELD_HINTS = {
    "wine":    "varietal, vintage, abv, price, quantity, in_cellar, rating, date_tasted, would_buy_again, appearance, nose, palate, finish, food_pairings",
    "whiskey": "vintage, age, cask, abv, style, price, quantity, in_cellar, rating, date_tasted, would_buy_again, color, nose, palate, with_water, finish",
    "gin":     "style, abv, botanicals, price, quantity, in_cellar, rating, date_tasted, would_buy_again, best_serve, nose, palate, finish",
    "vodka":   "base, abv, price, quantity, in_cellar, rating, date_tasted, would_buy_again, nose, palate, finish",
    "tequila": "age, abv, price, quantity, in_cellar, rating, date_tasted, would_buy_again, nose, palate, finish",
    "mezcal":  "age, abv, price, quantity, in_cellar, rating, date_tasted, would_buy_again, color, nose, palate, finish",
    "rum":     "age, cask, abv, price, quantity, in_cellar, rating, date_tasted, would_buy_again, color, nose, palate, finish",
    "port":    "vintage, abv, price, quantity, in_cellar, rating, date_tasted, would_buy_again, color, nose, palate, finish, food_pairings",
}

# Numeric / boolean coercion when add_bottle / update_bottle accepts JSON
NUMERIC_FIELDS = {"abv", "price", "rating", "vintage", "quantity"}
BOOL_FIELDS = {"in_cellar"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _validate_type(type_: str | None) -> tuple[str, str | None]:
    if not type_:
        return "", "Missing required `type`"
    t = type_.lower()
    if t not in VALID_TYPES:
        return t, f"Invalid type '{type_}'. Valid: {', '.join(VALID_TYPES)}"
    return t, None


@contextlib.contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def producer_path(type_: str, producer: str) -> Path:
    return CELLAR_DIRS[type_] / f"{producer}.md"


def _coerce_value(field: str, val):
    """Coerce string args from MCP into typed YAML values."""
    if val is None:
        return None
    if isinstance(val, bool) or isinstance(val, (int, float)):
        return val
    s = str(val).strip()
    if s == "":
        return None
    if field in NUMERIC_FIELDS:
        try:
            f = float(s)
            return int(f) if f == int(f) else f
        except ValueError:
            return s
    if field in BOOL_FIELDS:
        v = s.lower()
        if v in ("true", "yes"):
            return True
        if v in ("false", "no"):
            return False
    return s


# ---------------------------------------------------------------------------
# File read / write
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def read_producer(path: Path) -> tuple[dict, str]:
    """Returns (frontmatter_dict, body_text). body excludes the frontmatter block."""
    text = path.read_text()
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    body = text[m.end():]
    return fm, body


def write_producer(path: Path, fm: dict, body: str) -> None:
    """Write producer file: YAML frontmatter + body. Body should not include the
    frontmatter delimiters; this function adds them."""
    yaml_out = yaml.dump(fm, default_flow_style=False, allow_unicode=True,
                          sort_keys=False, width=120)
    body_norm = body.lstrip("\n")
    if not body_norm.endswith("\n"):
        body_norm = body_norm + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml_out}---\n\n{body_norm}")


def _producer_skeleton(type_: str, producer: str, region: str = "", country: str = "") -> tuple[dict, str]:
    fm = {"type": type_, "producer": producer}
    if region:  fm["region"]  = region
    if country: fm["country"] = country
    fm["tags"] = [type_]
    fm["bottles"] = []
    body = f"# {producer}\n"
    return fm, body


# ---------------------------------------------------------------------------
# Bottle helpers (operate on parsed fm + body)
# ---------------------------------------------------------------------------

def find_bottle_in_fm(fm: dict, name: str) -> tuple[int, dict] | tuple[None, None]:
    """Return (index, bottle) for the matching bottle, or (None, None).
    Case-insensitive exact match preferred; otherwise unique substring match.
    Raises ValueError if substring match is ambiguous."""
    bottles = fm.get("bottles") or []
    q = name.strip().lower()
    exact = [(i, b) for i, b in enumerate(bottles) if str(b.get("name", "")).lower() == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(f"Multiple bottles named exactly {name!r}")
    fuzzy = [(i, b) for i, b in enumerate(bottles) if q in str(b.get("name", "")).lower()]
    if not fuzzy:
        return None, None
    if len(fuzzy) > 1:
        names = [str(b.get("name", "")) for _, b in fuzzy]
        raise ValueError(f"Ambiguous match for {name!r}; candidates: {names}")
    return fuzzy[0]


def _body_bottle_section_re(name: str) -> re.Pattern:
    """Match `## <name>\\nNotes: <value>\\n` and any blank lines after."""
    return re.compile(
        rf"(^##\s+{re.escape(name)}\s*\n)(Notes:[ \t]*([^\n]*)\n)?",
        re.MULTILINE,
    )


def get_bottle_notes(body: str, name: str) -> str | None:
    """Return the Notes value for a bottle from body, or None if section missing."""
    m = _body_bottle_section_re(name).search(body)
    if not m:
        return None
    return (m.group(3) or "").strip()


def set_bottle_notes(body: str, name: str, value: str, append: bool = False) -> tuple[str, bool]:
    """Set / replace / append the Notes line for a bottle. Returns (new_body, found)."""
    pattern = _body_bottle_section_re(name)
    m = pattern.search(body)
    if not m:
        return body, False
    heading_line = m.group(1)
    existing_notes = (m.group(3) or "").strip()
    if append and existing_notes:
        new_value = f"{existing_notes} — {value}"
    else:
        new_value = value
    new_block = f"{heading_line}Notes: {new_value}\n"
    return body[:m.start()] + new_block + body[m.end():], True


def add_bottle_section_to_body(body: str, name: str, notes: str = "") -> str:
    """Append a new `## name\\nNotes: <notes>\\n` block to body."""
    body = body.rstrip() + "\n\n"
    body += f"## {name}\nNotes: {notes}\n"
    return body


def remove_bottle_section_from_body(body: str, name: str) -> str:
    """Remove the `## name\\nNotes: ...\\n` block (and trailing blank lines) from body."""
    pattern = re.compile(
        rf"^##\s+{re.escape(name)}\s*\n(?:Notes:[ \t]*[^\n]*\n)?(?:\n)*",
        re.MULTILINE,
    )
    return pattern.sub("", body, count=1)


def _bottle_with_notes(bottle: dict, body: str) -> dict:
    """Return a flat dict combining a bottle's YAML fields + its Notes from body."""
    out = dict(bottle)
    notes = get_bottle_notes(body, bottle.get("name", ""))
    if notes is not None:
        out["notes"] = notes
    return out


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
            description=(
                "Create a new producer file with no bottles. Use when you want to "
                "record a producer before tasting any specific bottles. add_bottle "
                "auto-creates a producer file if it doesn't exist, so you usually "
                "don't need this."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "region": {"type": "string"},
                    "country": {"type": "string"},
                },
                "required": ["type", "producer"]
            }
        ),
        types.Tool(
            name="add_bottle",
            description=(
                "Add a new bottle entry under a producer. Creates the producer "
                "file if it doesn't exist. Errors if a bottle with the same name "
                "already exists; pass `overwrite=true` to replace.\n\n"
                "All structured fields (varietal, vintage, abv, rating, etc.) "
                "live in the producer file's YAML frontmatter under the bottle's "
                "entry. The Notes (free-form tasting notes) live in the body. "
                "Pass tasting fields here as needed; the schema is open — type "
                "field hints below are guidance, not restrictions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "region": {"type": "string"},
                    "country": {"type": "string"},
                    "varietal": {"type": "string", "description": "Wine — grape variety or blend"},
                    "vintage": {"type": "integer", "description": "Wine/whiskey/port — year"},
                    "age": {"type": "string", "description": "Whiskey/rum/tequila — age statement e.g. '12' or 'NAS'"},
                    "abv": {"type": "number"},
                    "cask": {"type": "string", "description": "Whiskey/rum — cask type"},
                    "botanicals": {"type": "string", "description": "Gin"},
                    "base": {"type": "string", "description": "Vodka — base ingredient"},
                    "style": {"type": "string", "description": "Style label e.g. 'London Dry', 'Single Malt'"},
                    "price": {"type": "number"},
                    "quantity": {"type": "integer"},
                    "in_cellar": {"type": "boolean"},
                    "notes": {"type": "string", "description": "Initial tasting notes (goes into the body, not YAML)"},
                    "overwrite": {"type": "boolean", "default": False, "description": "Replace an existing bottle with the same name."},
                },
                "required": ["type", "producer", "name"]
            }
        ),
        types.Tool(
            name="update_bottle",
            description=(
                "Update a single field on an existing bottle. `field='notes'` "
                "edits the body Notes line; any other field edits the YAML "
                "entry (creating the key if it didn't exist). `append=true` "
                "joins with ` — ` for notes / `, ` for other strings instead "
                "of replacing — useful for accumulating tasting notes across "
                "sessions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "field": {"type": "string", "description": "Field name (e.g. 'rating', 'nose', 'palate', 'notes', 'quantity'). 'notes' edits body; everything else edits YAML."},
                    "value": {"type": "string"},
                    "append": {"type": "boolean", "default": False, "description": "Append rather than replace."},
                },
                "required": ["type", "producer", "name", "field", "value"]
            }
        ),
        types.Tool(
            name="rate_bottle",
            description="Set the rating, date_tasted, and would_buy_again for a bottle. Always replaces (these are scalar fields).",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "rating": {"type": "number", "description": "Rating out of 100"},
                    "would_buy_again": {"type": "string", "description": "yes / no / maybe"},
                    "date_tasted": {"type": "string", "description": "YYYY-MM-DD (default: today)"},
                },
                "required": ["type", "producer", "name", "rating"]
            }
        ),
        types.Tool(
            name="consume_bottle",
            description=(
                "Decrement a bottle's `quantity` after drinking some. Sets "
                "`in_cellar=false` automatically when quantity reaches 0."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": type_prop,
                    "producer": producer_prop,
                    "name": name_prop,
                    "decrement": {"type": "integer", "default": 1, "description": "How many to remove (default 1)."},
                },
                "required": ["type", "producer", "name"]
            }
        ),
        types.Tool(
            name="delete_bottle",
            description="Remove a single bottle from a producer file (YAML entry + body section). Producer file is preserved with any other bottles intact.",
            inputSchema={
                "type": "object",
                "properties": {"type": type_prop, "producer": producer_prop, "name": name_prop},
                "required": ["type", "producer", "name"]
            }
        ),
        types.Tool(
            name="delete_producer",
            description="Delete an entire producer file (and ALL its bottles).",
            inputSchema={
                "type": "object",
                "properties": {"type": type_prop, "producer": producer_prop},
                "required": ["type", "producer"]
            }
        ),
        types.Tool(
            name="get_note",
            description="Get the full producer file content as Markdown.",
            inputSchema={
                "type": "object",
                "properties": {"type": type_prop, "producer": producer_prop},
                "required": ["type", "producer"]
            }
        ),
        types.Tool(
            name="get_bottle",
            description="Return one bottle's data as a flat dict (YAML fields + Notes from body).",
            inputSchema={
                "type": "object",
                "properties": {"type": type_prop, "producer": producer_prop, "name": name_prop},
                "required": ["type", "producer", "name"]
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
            description="Search across the cellar. Filters combine with AND. Omit `type` to search all categories.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": f"Filter by category ({types_csv}) — omit for all"},
                    "producer": {"type": "string", "description": "Filter by producer (partial match)"},
                    "name":     {"type": "string", "description": "Filter by bottle name (partial match)"},
                    "varietal": {"type": "string", "description": "Filter by varietal (partial match)"},
                    "vintage":  {"type": "integer", "description": "Exact vintage year"},
                    "min_rating": {"type": "number", "description": "Minimum rating"},
                    "would_buy_again": {"type": "string", "description": "yes / no / maybe"},
                },
                "required": []
            }
        ),
        types.Tool(
            name="recent_tastings",
            description="Bottles sorted by date_tasted descending. Only includes bottles with a date_tasted set.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type":  {"type": "string", "description": f"Filter by category ({types_csv}) — omit for all"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": []
            }
        ),
        types.Tool(
            name="untasted_bottles",
            description="Bottles with no rating set yet. Default: only bottles still in the cellar.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": f"Filter by category ({types_csv}) — omit for all"},
                    "in_cellar_only": {"type": "boolean", "default": True},
                },
                "required": []
            }
        ),
    ]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = _dispatch(name, arguments)
    except ValueError as e:
        result = json.dumps({"error": str(e)})
    except Exception as e:
        result = json.dumps({"error": f"{type(e).__name__}: {e}"})
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
        with _file_lock(path):
            fm, body = _producer_skeleton(type_, producer, args.get("region", ""), args.get("country", ""))
            write_producer(path, fm, body)
        return json.dumps({"ok": True, "file": str(path), "producer": producer, "type": type_})

    if name == "add_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        producer = args["producer"]
        bottle_name = args["name"]
        overwrite = bool(args.get("overwrite", False))
        path = producer_path(type_, producer)

        # Build the new bottle dict from supplied args
        SKIP = {"type", "producer", "name", "overwrite", "notes"}
        new_bottle = {"name": bottle_name}
        for k, v in args.items():
            if k in SKIP:
                continue
            coerced = _coerce_value(k, v)
            if coerced is not None:
                new_bottle[k] = coerced
        notes_init = args.get("notes", "")

        with _file_lock(path):
            if not path.exists():
                fm, body = _producer_skeleton(type_, producer, args.get("region", ""), args.get("country", ""))
            else:
                fm, body = read_producer(path)
                fm.setdefault("bottles", [])

            bottles = fm["bottles"]
            existing_idx = next((i for i, b in enumerate(bottles)
                                  if str(b.get("name", "")) == bottle_name), None)
            if existing_idx is not None and not overwrite:
                return json.dumps({
                    "error": f"Bottle '{bottle_name}' already exists at {path}. "
                             "Use update_bottle to modify, or pass overwrite=true to replace.",
                    "file": str(path),
                })
            if existing_idx is not None:
                bottles[existing_idx] = new_bottle
                # Body section already exists — update Notes if provided
                if notes_init:
                    body, _ = set_bottle_notes(body, bottle_name, notes_init)
            else:
                bottles.append(new_bottle)
                body = add_bottle_section_to_body(body, bottle_name, notes_init)

            write_producer(path, fm, body)

        return json.dumps({"ok": True, "file": str(path), "producer": producer,
                            "name": bottle_name, "overwrote": overwrite and existing_idx is not None})

    if name == "update_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})

        field = args["field"]
        value = args["value"]
        append = bool(args.get("append", False))

        with _file_lock(path):
            fm, body = read_producer(path)
            try:
                idx, bottle = find_bottle_in_fm(fm, args["name"])
            except ValueError as e:
                return json.dumps({"error": str(e)})
            if idx is None:
                return json.dumps({"error": f"No bottle found matching '{args['name']}'"})

            actual_name = bottle["name"]

            if field.lower() == "notes":
                # Edit body Notes
                body, found = set_bottle_notes(body, actual_name, value, append=append)
                if not found:
                    # Section missing in body even though YAML has the bottle —
                    # add it back rather than silently fail.
                    body = add_bottle_section_to_body(body, actual_name, value)
            else:
                # Edit YAML field
                if append and field in bottle and bottle[field]:
                    existing = str(bottle[field])
                    bottle[field] = f"{existing}, {value}"
                else:
                    bottle[field] = _coerce_value(field, value)

            write_producer(path, fm, body)

        return json.dumps({"ok": True, "name": actual_name, "field": field, "value": value})

    if name == "rate_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        try:
            rating_val = float(args["rating"])
        except (TypeError, ValueError):
            return json.dumps({"error": f"`rating` must be numeric (got {args.get('rating')!r})"})
        rating_norm = int(rating_val) if rating_val == int(rating_val) else rating_val
        date_tasted = args.get("date_tasted") or get_local_date()

        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})

        with _file_lock(path):
            fm, body = read_producer(path)
            try:
                idx, bottle = find_bottle_in_fm(fm, args["name"])
            except ValueError as e:
                return json.dumps({"error": str(e)})
            if idx is None:
                return json.dumps({"error": f"No bottle found matching '{args['name']}'"})

            bottle["rating"] = rating_norm
            bottle["date_tasted"] = date_tasted
            if args.get("would_buy_again"):
                bottle["would_buy_again"] = args["would_buy_again"]
            write_producer(path, fm, body)

        return json.dumps({"ok": True, "name": bottle["name"], "rating": rating_val, "date_tasted": date_tasted})

    if name == "consume_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        decrement = max(1, int(args.get("decrement", 1)))
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})

        with _file_lock(path):
            fm, body = read_producer(path)
            try:
                idx, bottle = find_bottle_in_fm(fm, args["name"])
            except ValueError as e:
                return json.dumps({"error": str(e)})
            if idx is None:
                return json.dumps({"error": f"No bottle found matching '{args['name']}'"})

            current = bottle.get("quantity", 0)
            try:
                current = int(current)
            except (TypeError, ValueError):
                current = 0
            new_qty = max(0, current - decrement)
            bottle["quantity"] = new_qty
            if new_qty == 0:
                bottle["in_cellar"] = False
            write_producer(path, fm, body)

        return json.dumps({"ok": True, "name": bottle["name"], "previous_quantity": current,
                            "new_quantity": new_qty, "in_cellar": new_qty > 0})

    if name == "delete_bottle":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})

        with _file_lock(path):
            fm, body = read_producer(path)
            try:
                idx, bottle = find_bottle_in_fm(fm, args["name"])
            except ValueError as e:
                return json.dumps({"error": str(e)})
            if idx is None:
                return json.dumps({"error": f"No bottle found matching '{args['name']}'"})
            removed_name = bottle["name"]
            del fm["bottles"][idx]
            body = remove_bottle_section_from_body(body, removed_name)
            write_producer(path, fm, body)

        return json.dumps({"ok": True, "removed_bottle": removed_name, "producer": args["producer"], "type": type_})

    if name == "delete_producer":
        type_, err = _validate_type(args.get("type"))
        if err: return json.dumps({"error": err})
        path = producer_path(type_, args["producer"])
        if not path.exists():
            return json.dumps({"error": f"No note found for '{args['producer']}'"})
        with _file_lock(path):
            path.unlink()
        return json.dumps({"ok": True, "deleted_producer": args["producer"], "type": type_, "file": str(path)})

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
        fm, body = read_producer(path)
        try:
            idx, bottle = find_bottle_in_fm(fm, args["name"])
        except ValueError as e:
            # Ambiguous match — surface candidates
            candidates = [b.get("name", "") for b in (fm.get("bottles") or [])
                           if args["name"].lower() in str(b.get("name", "")).lower()]
            return json.dumps({"error": str(e), "candidates": candidates})
        if idx is None:
            return json.dumps({"error": f"No bottle matching '{args['name']}'"})
        return json.dumps({"type": type_, "producer": args["producer"],
                            "bottle": _bottle_with_notes(bottle, body)})

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

        producer_q = (args.get("producer") or "").strip().lower()
        name_q     = (args.get("name") or "").strip().lower()
        varietal_q = (args.get("varietal") or "").strip().lower()
        vintage_q  = args.get("vintage")
        min_rating = args.get("min_rating")
        wba_q      = (args.get("would_buy_again") or "").strip().lower()

        results = []
        for type_ in types_to_search:
            dir_ = CELLAR_DIRS.get(type_)
            if not dir_ or not dir_.exists():
                continue
            for md_file in sorted(dir_.glob("*.md")):
                if producer_q and producer_q not in md_file.stem.lower():
                    continue
                try:
                    fm, body = read_producer(md_file)
                except Exception:
                    continue
                producer = md_file.stem
                for bottle in fm.get("bottles") or []:
                    bn = str(bottle.get("name", ""))
                    if name_q and name_q not in bn.lower():
                        continue
                    if varietal_q and varietal_q not in str(bottle.get("varietal", "")).lower():
                        continue
                    if vintage_q is not None and str(vintage_q) != str(bottle.get("vintage", "")):
                        continue
                    if min_rating is not None:
                        try:
                            if float(bottle.get("rating", 0)) < float(min_rating):
                                continue
                        except (TypeError, ValueError):
                            continue
                    if wba_q and str(bottle.get("would_buy_again", "")).lower() != wba_q:
                        continue
                    results.append({"type": type_, "producer": producer, **_bottle_with_notes(bottle, body)})
        return json.dumps({"results": results, "count": len(results)})

    if name == "recent_tastings":
        if args.get("type"):
            t, err = _validate_type(args["type"])
            if err: return json.dumps({"error": err})
            types_to_walk = [t]
        else:
            types_to_walk = VALID_TYPES
        limit = int(args.get("limit", 20))

        bottles_acc = []
        for type_ in types_to_walk:
            dir_ = CELLAR_DIRS.get(type_)
            if not dir_ or not dir_.exists():
                continue
            for md_file in dir_.glob("*.md"):
                try:
                    fm, body = read_producer(md_file)
                except Exception:
                    continue
                for bottle in fm.get("bottles") or []:
                    if not str(bottle.get("date_tasted", "")).strip():
                        continue
                    bottles_acc.append({"type": type_, "producer": md_file.stem, **_bottle_with_notes(bottle, body)})

        bottles_acc.sort(key=lambda b: str(b.get("date_tasted", "")), reverse=True)
        return json.dumps({
            "results": bottles_acc[:limit],
            "returned": min(len(bottles_acc), limit),
            "total_with_dates": len(bottles_acc),
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
                    fm, body = read_producer(md_file)
                except Exception:
                    continue
                for bottle in fm.get("bottles") or []:
                    if str(bottle.get("rating", "")).strip():
                        continue
                    if in_cellar_only:
                        ic = bottle.get("in_cellar", True)
                        if ic is False or str(ic).lower() == "false":
                            continue
                    results.append({"type": type_, "producer": md_file.stem, **_bottle_with_notes(bottle, body)})
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
