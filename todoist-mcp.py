#!/usr/bin/env python3
"""
todoist-mcp.py — MCP server for Todoist task management.

Wraps the Todoist REST API v2 with tools for CRUD on tasks, listing projects/labels,
and convenience wrappers like today/overdue.

Auth: set MEMENTO_TODOIST_TOKEN in the environment (or openclaw.json env section).
Get a token at https://todoist.com/app/settings/integrations/developer
"""

import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

sys.path.insert(0, str(Path(__file__).parent))
from config import TODOIST_TOKEN

API_BASE = "https://api.todoist.com/api/v1"

app = Server("todoist")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _request(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict | list | None:
    if not TODOIST_TOKEN:
        raise RuntimeError("MEMENTO_TODOIST_TOKEN not set — configure in openclaw.json env section")
    url = f"{API_BASE}{path}"
    if params:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if q:
            url += f"?{q}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {TODOIST_TOKEN}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Todoist API {e.code}: {body_text}") from e


def _get(path: str, **params) -> list | dict:
    """GET, auto-unwrapping {'results': [...]} list responses."""
    r = _request("GET", path, params=params or None)
    if isinstance(r, dict) and "results" in r and len(r) <= 3:
        return r["results"]
    return r


def _post(path: str, body: dict | None = None) -> dict | None:
    return _request("POST", path, body=body)


def _delete(path: str) -> None:
    _request("DELETE", path)


# ---------------------------------------------------------------------------
# Projects / labels cache (name → id lookups)
# ---------------------------------------------------------------------------

def _projects() -> list[dict]:
    return _get("/projects") or []


def _labels() -> list[dict]:
    return _get("/labels") or []


def _project_id(name_or_id: str) -> str | None:
    if not name_or_id:
        return None
    if str(name_or_id).isdigit():
        return str(name_or_id)
    name = name_or_id.lower()
    for p in _projects():
        if p["name"].lower() == name:
            return p["id"]
    return None


# ---------------------------------------------------------------------------
# Task shaping
# ---------------------------------------------------------------------------

def _task_summary(t: dict) -> dict:
    """Return a compact, human-friendly view of a Todoist task (v1 API shape)."""
    due = t.get("due") or {}
    return {
        "id": t["id"],
        "content": t["content"],
        "description": t.get("description") or None,
        "project_id": t.get("project_id"),
        "priority": t.get("priority"),  # 1=low..4=urgent
        "labels": t.get("labels") or [],
        "due": due.get("string"),
        "due_date": due.get("date"),
        "is_completed": t.get("checked", False) or t.get("is_completed", False),
        "url": t.get("url") or f"https://todoist.com/app/task/{t['id']}",
    }


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_tasks",
            description=(
                "List active tasks with optional filters. "
                "Filter by project name/id, label, or Todoist filter expression (e.g. 'today', 'overdue', 'p1 & @work')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name or id"},
                    "label": {"type": "string", "description": "Label name"},
                    "filter": {"type": "string", "description": "Todoist filter query e.g. 'today', 'overdue', '7 days'"},
                    "limit": {"type": "integer", "description": "Max results (default: 50)"},
                },
            },
        ),
        types.Tool(
            name="add_task",
            description="Create a new task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Task title"},
                    "description": {"type": "string", "description": "Additional notes"},
                    "project": {"type": "string", "description": "Project name or id (default: Inbox)"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "priority": {"type": "integer", "description": "1 (low) to 4 (urgent)"},
                    "due_string": {"type": "string", "description": "Natural-language due date e.g. 'tomorrow 9am', 'next monday'"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD (alternative to due_string)"},
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="complete_task",
            description="Mark a task as complete (closes it).",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="update_task",
            description="Update an existing task's content, priority, labels, or due date.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "content": {"type": "string"},
                    "description": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "priority": {"type": "integer", "description": "1-4"},
                    "due_string": {"type": "string"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="delete_task",
            description="Delete a task permanently.",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="list_projects",
            description="List all Todoist projects.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_labels",
            description="List all Todoist labels.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_today",
            description="Return tasks due today (shortcut for list_tasks with filter='today').",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_overdue",
            description="Return overdue tasks.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = _dispatch(name, arguments)
    except Exception as e:
        result = json.dumps({"error": f"{type(e).__name__}: {e}"})
    return [types.TextContent(type="text", text=result)]


def _dispatch(name: str, args: dict) -> str:
    if name == "list_tasks":
        params = {}
        if args.get("project"):
            pid = _project_id(args["project"])
            if pid:
                params["project_id"] = pid
        if args.get("label"):
            params["label"] = args["label"]
        if args.get("filter"):
            params["filter"] = args["filter"]
        tasks = _get("/tasks", **params) or []
        limit = args.get("limit", 50)
        return json.dumps({"tasks": [_task_summary(t) for t in tasks[:limit]], "count": len(tasks)})

    if name == "add_task":
        body = {"content": args["content"]}
        if args.get("description"):
            body["description"] = args["description"]
        if args.get("project"):
            pid = _project_id(args["project"])
            if pid:
                body["project_id"] = pid
        if args.get("labels"):
            body["labels"] = args["labels"]
        if args.get("priority"):
            body["priority"] = int(args["priority"])
        if args.get("due_string"):
            body["due_string"] = args["due_string"]
        elif args.get("due_date"):
            body["due_date"] = args["due_date"]
        t = _post("/tasks", body)
        return json.dumps({"ok": True, "task": _task_summary(t)})

    if name == "complete_task":
        _post(f"/tasks/{args['task_id']}/close")
        return json.dumps({"ok": True, "task_id": args["task_id"], "status": "completed"})

    if name == "update_task":
        body = {}
        for k in ("content", "description", "labels", "priority", "due_string", "due_date"):
            if k in args and args[k] is not None:
                body[k] = int(args[k]) if k == "priority" else args[k]
        if not body:
            return json.dumps({"error": "No update fields provided"})
        t = _post(f"/tasks/{args['task_id']}", body)
        return json.dumps({"ok": True, "task": _task_summary(t) if t else None})

    if name == "delete_task":
        _delete(f"/tasks/{args['task_id']}")
        return json.dumps({"ok": True, "task_id": args["task_id"], "status": "deleted"})

    if name == "list_projects":
        projs = _projects()
        return json.dumps({"projects": [{"id": p["id"], "name": p["name"]} for p in projs]})

    if name == "list_labels":
        labels = _labels()
        return json.dumps({"labels": [{"id": l["id"], "name": l["name"]} for l in labels]})

    if name == "get_today":
        tasks = _get("/tasks", filter="today") or []
        return json.dumps({"tasks": [_task_summary(t) for t in tasks], "count": len(tasks)})

    if name == "get_overdue":
        tasks = _get("/tasks", filter="overdue") or []
        return json.dumps({"tasks": [_task_summary(t) for t in tasks], "count": len(tasks)})

    return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
