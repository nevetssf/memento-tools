#!/usr/bin/env python3
"""notebook-mcp.py — MCP server for paper notebooks archived in the vault.

Thin wrapper over notebook.py (all logic lives there). Tools:

  notebook_list        notebooks with status, size, page count
  notebook_new         start a notebook (asks nothing; caller supplies size)
  notebook_ingest      photos / Scanner Pro PDFs -> staged candidate pages
  notebook_file        assign candidates to page numbers (writes pages,
                       keeps originals, recompiles the PDF)
  notebook_transcribe  write/replace a page's section in the index note
  notebook_reprocess   redo one page from its original with new parameters
  notebook_status      pages, gaps, untranscribed pages
  notebook_rebuild     redo every page from its originals
  notebook_finish      mark finished, final compile

Never prints to stdout (that is the MCP channel).
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

import notebook as nbk  # noqa: E402

app = Server("notebook")

_NB = {"type": "string",
       "description": "Notebook folder name (or unique part of it). Omit for the only active notebook."}
_PAGE = {"type": ["integer", "string"], "description": "Page number 1-998, 'cover' or 'back'."}


def _tool(name, description, props, required=()):
    return types.Tool(name=name, description=description,
                      inputSchema={"type": "object", "properties": props,
                                   "required": list(required)})


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        _tool("notebook_list", "List notebooks: name, status (active/finished), size, page count.", {}),
        _tool("notebook_new",
              "Start a new notebook folder + index note. Ask Steven for the page size first.",
              {"name": {"type": "string", "description": "e.g. 'Notebook' or a description from the first page"},
               "size": {"type": "string",
                        "description": "'passport' (3.5x5in), 'regular' (110x210mm), or 'WxHmm' / 'WxHin'"},
               "started": {"type": "string", "description": "YYYY-MM-DD from the first page; default today"},
               "description": {"type": "string"}},
              ["name", "size"]),
        _tool("notebook_ingest",
              "Process photos and/or Scanner Pro PDFs into candidate pages for review. Detects "
              "single pages vs two-page spreads (also sideways), crops, deskews, flattens curl, "
              "cleans. Returns a batch id and per candidate: image path, header image (top of "
              "the page enlarged, to read the page number), params and warning flags. VIEW EVERY "
              "CANDIDATE before filing.",
              {"sources": {"type": "array", "items": {"type": "string"},
                           "description": "Absolute paths of images/PDFs as received"},
               "notebook": _NB,
               "rotate": {"type": "integer", "enum": [0, 90, 180, 270],
                          "description": "Clockwise rotation to apply first (only if auto-detection got it wrong)"},
               "layout": {"type": "string", "enum": ["auto", "single", "spread"],
                          "description": "Override single/spread detection"},
               "cover": {"type": "boolean", "description": "These sources are the cover (keep colour)"}},
              ["sources"]),
        _tool("notebook_file",
              "File reviewed candidates as pages. Replaces existing pages with the same number "
              "(their unused originals are deleted), recompiles the PDF, updates the page count. "
              "Use flip for a candidate that is upside down.",
              {"batch": {"type": "string"},
               "assignments": {"type": "array", "items": {
                   "type": "object",
                   "properties": {"candidate": {"type": "string"}, "page": _PAGE,
                                  "flip": {"type": "boolean"}},
                   "required": ["candidate", "page"]}},
               "received": {"type": "string", "description": "YYYY-MM-DD; default today"}},
              ["batch", "assignments"]),
        _tool("notebook_transcribe",
              "Write (or replace) the verbatim transcription of a page in the notebook's index "
              "note, in page order. Mark unclear words [?]; describe sketches in brackets.",
              {"page": _PAGE, "text": {"type": "string"}, "notebook": _NB,
               "heading_note": {"type": "string",
                                "description": "Replaces the parenthetical in the heading, e.g. "
                                               "'received 2026-10-31; replaced 2026-11-02'"}},
              ["page", "text"]),
        _tool("notebook_reprocess",
              "Redo one filed page from its stored original with changed parameters; returns a "
              "new candidate to review and file.",
              {"page": _PAGE, "notebook": _NB,
               "rotate": {"type": "integer", "enum": [0, 90, 180, 270]},
               "half": {"type": "string", "enum": ["left", "right"]},
               "corners": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                           "description": "Manual quad [[x,y]x4] in original-image pixels after rotate"},
               "seed": {"type": "array", "items": {"type": "number"},
                        "description": "[x, y] point on the wanted page"},
               "flip": {"type": "boolean"},
               "bleed": {"type": "number", "description": "Show-through suppression 0-3 (default 1)"},
               "dewarp": {"type": "boolean"},
               "color": {"type": "boolean"}},
              ["page"]),
        _tool("notebook_status",
              "Pages present, gaps, pages missing a transcription, pages without a stored original.",
              {"notebook": _NB}),
        _tool("notebook_rebuild",
              "Redo pages from their stored originals with their recorded parameters, then "
              "recompile the PDF. Use after the image pipeline improves.",
              {"notebook": _NB, "pages": {"type": "array", "items": _PAGE}}),
        _tool("notebook_finish", "Mark the notebook finished and do a final compile.",
              {"notebook": _NB, "finished": {"type": "string", "description": "YYYY-MM-DD; default today"}}),
    ]


def _dispatch(name: str, a: dict):
    if name == "notebook_list":
        return nbk.list_notebooks()
    if name == "notebook_new":
        return nbk.new_notebook(a["name"], a["size"], a.get("started"), a.get("description"))
    if name == "notebook_ingest":
        return nbk.ingest(nbk.find_notebook(a.get("notebook")), a["sources"],
                          int(a.get("rotate") or 0), a.get("layout") or "auto",
                          bool(a.get("cover")))
    if name == "notebook_file":
        return nbk.file_pages(a["batch"], a["assignments"], a.get("received"))
    if name == "notebook_transcribe":
        return nbk.transcribe(nbk.find_notebook(a.get("notebook")), a["page"], a["text"],
                              a.get("heading_note"))
    if name == "notebook_reprocess":
        keys = ("rotate", "half", "corners", "seed", "flip", "bleed", "dewarp", "color")
        return nbk.reprocess(nbk.find_notebook(a.get("notebook")), a["page"],
                             **{k: a[k] for k in keys if k in a})
    if name == "notebook_status":
        return nbk.status(nbk.find_notebook(a.get("notebook")))
    if name == "notebook_rebuild":
        return nbk.rebuild(nbk.find_notebook(a.get("notebook")), a.get("pages"))
    if name == "notebook_finish":
        return nbk.finish(nbk.find_notebook(a.get("notebook")), a.get("finished"))
    raise ValueError(f"unknown tool {name}")


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    args = arguments or {}
    try:
        # image processing takes seconds per page; keep the event loop free
        result = await asyncio.to_thread(_dispatch, name, args)
    except (nbk.NotebookError, KeyError, ValueError, TypeError) as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
    except Exception as e:
        return [types.TextContent(type="text",
                                  text=json.dumps({"error": f"{type(e).__name__}: {e}"}))]
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def _main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
