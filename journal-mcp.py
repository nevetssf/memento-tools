#!/usr/bin/env python3
"""
journal-mcp.py — MCP server for journal operations.

Wraps: journal-log.py, journal-header.py, journal-summary.py, priorities.py,
       journal-location.py, journal-photo-log.py, journal-pdf.py
"""

import asyncio
import importlib.util
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))
from config import VAULT_DIR, LOCATION_FILE

app = Server("journal-db")


# ---------------------------------------------------------------------------
# Module loader (handles hyphenated filenames)
# ---------------------------------------------------------------------------

def _load(name: str):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


jlog = _load("journal-log")
jheader = _load("journal-header")
jsummary = _load("journal-summary")
jpriorities = _load("priorities")
jlocation = _load("journal-location")
jweather = _load("journal-weather")
localtime = _load("localtime")
jphoto = _load("journal-photo-log")
jpdf = _load("journal-pdf")

LOCATION_MD = LOCATION_FILE


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run(main_fn, argv: list[str]) -> str:
    """Run a script's main() with argv, capturing stdout/stderr.

    Handles sys.exit(0) (success with output, e.g. priorities.py) and
    sys.exit(1) (error — return stderr JSON message).
    """
    old_argv = sys.argv[:]
    sys.argv = ["script"] + argv
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            main_fn()
        return buf_out.getvalue().strip()
    except SystemExit as e:
        out = buf_out.getvalue().strip()
        if e.code == 0 and out:
            return out
        err = buf_err.getvalue().strip()
        return err if err else json.dumps({"error": "Command failed"})
    finally:
        sys.argv = old_argv


def _date_arg(date: str | None) -> list[str]:
    return ["--date", date] if date else []


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    date_prop = {"type": "string", "description": "Date YYYY-MM-DD (default: today in Steven's local time)"}
    return [
        # --- journal-log ---
        types.Tool(
            name="log_entry",
            description=(
                "Append a timestamped entry to the daily journal. Handles file creation, "
                "chronological insertion, and frontmatter people/tag tagging. "
                "Tag each entry with 1-3 relevant topical tags from Steven's common vocabulary "
                "(work, social, food, travel, family, health, photography, tech, dev, running, nvidia, etc.)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entry": {"type": "string", "description": "Entry text — Steven's exact words, fix typos only, never rephrase"},
                    "time": {"type": "string", "description": "Override timestamp e.g. '14:30 PDT' (default: current local time)"},
                    "date": date_prop,
                    "people": {
                        "type": "array",
                        "description": "People Steven directly interacted with in this entry",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "id": {"type": "integer", "description": "DB id from find_person"}
                            },
                            "required": ["name"]
                        }
                    },
                    "tags": {
                        "type": "array",
                        "description": (
                            "Topical tags for this entry (1-3 recommended). Use Steven's existing vocabulary "
                            "when possible: work, social, food, travel, family, health, photography, tech, "
                            "dev, running, strength-training, nvidia, packing, etc. Tags accumulate across entries in the day."
                        ),
                        "items": {"type": "string"}
                    }
                },
                "required": ["entry"]
            }
        ),
        types.Tool(
            name="init_journal",
            description="Create today's journal file with frontmatter if it doesn't exist. Also sets location from LOCATION.md.",
            inputSchema={
                "type": "object",
                "properties": {"date": date_prop}
            }
        ),
        # --- journal-header ---
        types.Tool(
            name="get_tags",
            description="Get the tags list from the journal frontmatter.",
            inputSchema={
                "type": "object",
                "properties": {"date": date_prop}
            }
        ),
        types.Tool(
            name="add_tag",
            description="Add a tag to the journal frontmatter (idempotent).",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Tag to add e.g. 'running', 'social', 'travel'"},
                    "date": date_prop
                },
                "required": ["tag"]
            }
        ),
        types.Tool(
            name="set_tags",
            description="Replace all journal tags with a new list.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "date": date_prop
                },
                "required": ["tags"]
            }
        ),
        types.Tool(
            name="get_journal_field",
            description="Read a scalar frontmatter field (e.g. 'day', 'date').",
            inputSchema={
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "date": date_prop
                },
                "required": ["field"]
            }
        ),
        types.Tool(
            name="set_journal_field",
            description="Set a scalar frontmatter field.",
            inputSchema={
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                    "date": date_prop
                },
                "required": ["field", "value"]
            }
        ),
        types.Tool(
            name="get_journal_people",
            description="Get the people list from the journal frontmatter.",
            inputSchema={
                "type": "object",
                "properties": {"date": date_prop}
            }
        ),
        types.Tool(
            name="add_journal_person",
            description="Add a person to the journal frontmatter (idempotent, deduplicates by name).",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "person_id": {"type": "integer", "description": "DB id from find_person (include when known)"},
                    "date": date_prop
                },
                "required": ["name"]
            }
        ),
        # --- journal-summary ---
        types.Tool(
            name="get_journal_summary",
            description="Extract journal entries and photos for a date range. Returns structured JSON for the agent to summarize.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of days back from today"},
                    "date": {"type": "string", "description": "Single date YYYY-MM-DD"},
                    "from_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                    "to_date": {"type": "string", "description": "End date YYYY-MM-DD"}
                }
            }
        ),
        # --- priorities ---
        types.Tool(
            name="list_priorities",
            description="List current priorities for the day.",
            inputSchema={
                "type": "object",
                "properties": {"date": date_prop}
            }
        ),
        types.Tool(
            name="set_priorities",
            description="Set the priorities list (replaces existing list, preserves checked state for matching items).",
            inputSchema={
                "type": "object",
                "properties": {
                    "tasks": {"type": "array", "items": {"type": "string"}},
                    "date": date_prop
                },
                "required": ["tasks"]
            }
        ),
        types.Tool(
            name="add_priorities",
            description="Append new priorities without replacing existing ones.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tasks": {"type": "array", "items": {"type": "string"}},
                    "date": date_prop
                },
                "required": ["tasks"]
            }
        ),
        types.Tool(
            name="mark_priority_done",
            description="Mark a priority as done (fuzzy match on task name).",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task name or partial match"},
                    "date": date_prop
                },
                "required": ["task"]
            }
        ),
        # --- journal-location ---
        types.Tool(
            name="set_location",
            description="Update Steven's current location: writes to LOCATION.md (drives timezone), appends to today's journal frontmatter, logs weather, and sends a Signal message.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Location name e.g. 'Boulder, CO'"},
                    "date": date_prop,
                    "include_signal": {"type": "boolean", "description": "Send weather via Signal (default: true)"}
                },
                "required": ["location"]
            }
        ),
        types.Tool(
            name="add_location",
            description="Append a location to the journal frontmatter without changing LOCATION.md. Use when passing through a place that isn't a new home base.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Location name e.g. 'New York, NY'"},
                    "date": date_prop
                },
                "required": ["location"]
            }
        ),
        types.Tool(
            name="get_locations",
            description="Get the location list from the journal frontmatter for a given date.",
            inputSchema={
                "type": "object",
                "properties": {"date": date_prop}
            }
        ),
        # --- journal-weather ---
        types.Tool(
            name="log_weather",
            description="Fetch current weather, log it as a journal entry, and send via Signal. Call when Steven asks about the weather or when context suggests it's useful.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Location override (default: current from LOCATION.md)"},
                    "date": date_prop,
                    "include_signal": {"type": "boolean", "description": "Send via Signal (default: true)"}
                }
            }
        ),
        # --- journal-photo-log ---
        types.Tool(
            name="log_photo",
            description="Copy a photo to the Obsidian journal, extract EXIF metadata, and append a timestamped entry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Absolute path to the image file"},
                    "caption": {
                        "type": "string",
                        "description": (
                            "Only Steven's OWN words about the photo (what he actually typed/said). "
                            "OMIT this parameter entirely if Steven sent the photo with no comment. "
                            "NEVER copy the AI-generated Description here."
                        )
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "The AI-generated image description (text after 'Description:' in the [Image] block). "
                            "Renders in italics as an alt-text-style note. "
                            "Always pass this when the [Image] block is present, regardless of whether Steven commented."
                        )
                    },
                    "date": date_prop
                },
                "required": ["image_path"]
            }
        ),
        # --- journal-pdf ---
        types.Tool(
            name="export_pdf",
            description="Export a journal day as a styled PDF with inline photos.",
            inputSchema={
                "type": "object",
                "properties": {
                    "date": date_prop,
                    "output": {"type": "string", "description": "Output PDF path (default: Journal/YYYY/pdf/YYYY-MM-DD.pdf)"},
                    "include_people": {"type": "boolean", "default": False, "description": "Include people list in PDF"},
                    "include_priorities": {"type": "boolean", "default": False, "description": "Include priorities section in PDF"}
                }
            }
        ),
        # --- localtime ---
        types.Tool(
            name="get_time",
            description="Get the current local time and timezone for a location. Defaults to Steven's current location (LOCATION.md).",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Location name (optional — defaults to current location from LOCATION.md)"}
                }
            }
        ),
        types.Tool(
            name="learn_location",
            description="Save a location alias so future get_time() calls resolve it instantly without geocoding. Use when get_time returns an 'unresolved' error — resolve via a nearby larger city, then save the alias.",
            inputSchema={
                "type": "object",
                "properties": {
                    "alias": {"type": "string", "description": "The unresolved location name to save e.g. 'Wetzlar, Germany'"},
                    "resolve_via": {"type": "string", "description": "A nearby city that resolves correctly e.g. 'Frankfurt'"}
                },
                "required": ["alias", "resolve_via"]
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
    date = args.get("date")

    # --- journal-log ---
    if name == "log_entry":
        argv = ["--entry", args["entry"]]
        if args.get("time"):
            argv += ["--time", args["time"]]
        argv += _date_arg(date)
        if args.get("people"):
            argv.append("--people")
            for p in args["people"]:
                pid = p.get("id")
                argv.append(f"{p['name']}:{pid}" if pid else p["name"])
        if args.get("tags"):
            argv.append("--tags")
            argv.extend(args["tags"])
        return _run(jlog.main, argv)

    elif name == "init_journal":
        result = _run(jlog.main, ["--init"] + _date_arg(date))
        # Log weather when a new file is created or frontmatter was just added
        if "Created" in result or "Added frontmatter" in result:
            d = date or jheader.get_local_date()
            signal_arg = [] if args.get("include_signal", True) else ["--no-signal"]
            _run(jweather.main, signal_arg + _date_arg(d))
        return result

    # --- journal-header ---
    elif name == "get_tags":
        return _run(
            lambda: jheader.cmd_get_tags(date or jheader.get_local_date()),
            []
        )

    elif name == "add_tag":
        d = date or jheader.get_local_date()
        return _run(lambda: jheader.cmd_add_tag(d, args["tag"]), [])

    elif name == "set_tags":
        d = date or jheader.get_local_date()
        return _run(lambda: jheader.cmd_set_tags(d, args["tags"]), [])

    elif name == "get_journal_field":
        d = date or jheader.get_local_date()
        return _run(lambda: jheader.cmd_get(d, args["field"]), [])

    elif name == "set_journal_field":
        d = date or jheader.get_local_date()
        return _run(lambda: jheader.cmd_set(d, args["field"], args["value"]), [])

    elif name == "get_journal_people":
        return _run(
            lambda: jheader.cmd_get_people(date or jheader.get_local_date()),
            []
        )

    elif name == "add_journal_person":
        d = date or jheader.get_local_date()
        pid = args.get("person_id")
        return _run(lambda: jheader.cmd_add_person(d, args["name"], pid), [])

    # --- journal-summary ---
    elif name == "get_journal_summary":
        argv = []
        if args.get("days"):
            argv += ["--days", str(args["days"])]
        elif args.get("date"):
            argv += ["--date", args["date"]]
        elif args.get("from_date") and args.get("to_date"):
            argv += ["--from", args["from_date"], "--to", args["to_date"]]
        return _run(jsummary.main, argv)

    # --- priorities ---
    elif name == "list_priorities":
        return _run(jpriorities.main, ["--list"] + _date_arg(date))

    elif name == "set_priorities":
        return _run(jpriorities.main, ["--set"] + args["tasks"] + _date_arg(date))

    elif name == "add_priorities":
        return _run(jpriorities.main, ["--add"] + args["tasks"] + _date_arg(date))

    elif name == "mark_priority_done":
        return _run(jpriorities.main, ["--done", args["task"]] + _date_arg(date))

    # --- journal-weather ---
    elif name == "log_weather":
        d = date or jheader.get_local_date()
        argv = _date_arg(d)
        if args.get("location"):
            argv += ["--location", args["location"]]
        if not args.get("include_signal", True):
            argv.append("--no-signal")
        return _run(jweather.main, argv)

    # --- journal-location ---
    elif name == "set_location":
        location = args["location"]
        LOCATION_MD.write_text(location + "\n")
        d = date or jlocation.get_local_date()
        _run(lambda: jlocation.add_location(d, location), [])
        signal_arg = [] if args.get("include_signal", True) else ["--no-signal"]
        weather_result = _run(jweather.main, ["--location", location] + signal_arg + _date_arg(d))
        return json.dumps({"location": location, "date": d, "weather": weather_result})

    elif name == "add_location":
        d = date or jlocation.get_local_date()
        return _run(lambda: jlocation.add_location(d, args["location"]), [])

    elif name == "get_locations":
        d = date or jlocation.get_local_date()
        return _run(lambda: jlocation.show_locations(d), [])

    # --- journal-photo-log ---
    elif name == "log_photo":
        argv = [args["image_path"]]
        if args.get("caption"):
            argv += ["--caption", args["caption"]]
        if args.get("description"):
            argv += ["--description", args["description"]]
        argv += _date_arg(date)
        return _run(jphoto.main, argv)

    # --- journal-pdf ---
    elif name == "export_pdf":
        d = date or jheader.get_local_date()
        year = d[:4]
        if args.get("output"):
            output_path = Path(args["output"])
        else:
            pdf_dir = VAULT_DIR / "Journal" / year / "pdf"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            output_path = pdf_dir / f"{d}.pdf"
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        try:
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                jpdf.build_pdf(
                    date_str=d,
                    output_path=output_path,
                    include_people=args.get("include_people", False),
                    include_priorities=args.get("include_priorities", False),
                )
            return json.dumps({"status": "ok", "output": str(output_path)})
        except SystemExit as e:
            err = buf_err.getvalue().strip()
            return err if err else json.dumps({"error": "export_pdf failed"})

    # --- localtime ---
    elif name == "get_time":
        loc = args.get("location") or LOCATION_MD.read_text().strip()
        try:
            return json.dumps(localtime.get_localtime(location=loc))
        except localtime.LocationUnresolved as e:
            return json.dumps({"error": "unresolved", "query": e.query,
                               "suggestion": f"Could not resolve '{e.query}'. Try a nearby larger city, then call learn_location."})
        except (ValueError, KeyError) as e:
            return json.dumps({"error": str(e)})

    elif name == "learn_location":
        try:
            resolved = localtime.get_localtime(location=args["resolve_via"])
            localtime.learn_alias(args["alias"], args["alias"], resolved["timezone"])
            return json.dumps({"status": "ok", "alias": args["alias"], "timezone": resolved["timezone"]})
        except localtime.LocationUnresolved as e:
            return json.dumps({"error": f"Could not resolve '{args['resolve_via']}' either — try a larger city."})
        except (ValueError, KeyError) as e:
            return json.dumps({"error": str(e)})

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
