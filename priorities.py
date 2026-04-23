#!/usr/bin/env python3
"""
priorities.py — Manage daily priorities in the Obsidian journal.

Usage:
  python3 priorities.py --set "Task 1" "Task 2" "Task 3"
  python3 priorities.py --done "Task 1"
  python3 priorities.py --list
  python3 priorities.py --date 2026-03-28 --list

The Priorities section is placed after the frontmatter, before the first
timestamped journal entry. Items are Markdown checkboxes: - [ ] / - [x].

Options:
  --date YYYY-MM-DD   Target date (defaults to today in local time)
  --set TASK...       Create/replace priorities list (preserves checked state)
  --done TASK         Mark a priority done (fuzzy/case-insensitive match)
  --list              Show current priorities

Output: JSON on stdout.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import JOURNAL_DIR, LOCATION_FILE, SOUL_FILE
from localtime import get_localtime  # noqa: E402

JOURNAL_BASE = JOURNAL_DIR
LOCATION_MD = LOCATION_FILE
SOUL_MD = SOUL_FILE


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def get_today_date():
    """Get today's date in Steven's local timezone via LOCATION.md."""
    location = "San Francisco"
    try:
        loc = LOCATION_MD.read_text().strip()
        if loc:
            location = loc
    except Exception:
        try:
            text = SOUL_MD.read_text()
            m = re.search(r'\*\*Current location:\*\*\s*(.+)', text)
            if m:
                loc = m.group(1).strip()
                loc = re.split(r'\s*[—–-]\s', loc)[0].strip()
                if loc:
                    location = loc
        except Exception:
            pass

    try:
        return get_localtime(location=location)["date"]
    except Exception:
        from datetime import date
        return date.today().isoformat()


def journal_path(date_str):
    year = date_str[:4]
    return JOURNAL_BASE / year / f"{date_str}.md"


# ---------------------------------------------------------------------------
# Priorities section parsing / building
# ---------------------------------------------------------------------------

# Matches the whole ## Priorities block (header + all checkbox lines)
SECTION_RE = re.compile(
    r'(## Priorities\n(?:- \[[ x]\] [^\n]*\n?)*)',
    re.MULTILINE,
)

ITEM_RE = re.compile(r'^- \[([ x])\] (.+)$')


def parse_items(section_text):
    """Return list of (checked: bool, label: str) from a Priorities block."""
    items = []
    for line in section_text.splitlines():
        m = ITEM_RE.match(line)
        if m:
            items.append((m.group(1) == 'x', m.group(2)))
    return items


def build_section(items):
    """Render a Priorities block from list of (checked, label)."""
    lines = ["## Priorities"]
    for checked, label in items:
        mark = 'x' if checked else ' '
        lines.append(f"- [{mark}] {label}")
    return "\n".join(lines) + "\n"


def insert_after_frontmatter(text, section):
    """Insert section after the closing --- of frontmatter."""
    fm = re.match(r'^---\n.*?\n---\n', text, re.DOTALL)
    if fm:
        pos = fm.end()
        return text[:pos] + "\n" + section + "\n" + text[pos:]
    return section + "\n" + text


def items_to_json(items):
    return [{"done": checked, "task": label} for checked, label in items]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_set(path, new_labels):
    if not path.exists():
        _err(f"No journal found at {path}")

    text = path.read_text()
    existing = SECTION_RE.search(text)

    if existing:
        old_state = {label: checked for checked, label in parse_items(existing.group(1))}
        merged = [(old_state.get(label, False), label) for label in new_labels]
        new_section = build_section(merged)
        new_text = SECTION_RE.sub(new_section, text, count=1)
    else:
        new_section = build_section([(False, label) for label in new_labels])
        new_text = insert_after_frontmatter(text, new_section)

    path.write_text(new_text)
    items = parse_items(SECTION_RE.search(new_text).group(1))
    _out({"date": path.stem, "priorities": items_to_json(items)})


def cmd_add(path, new_labels):
    if not path.exists():
        _err(f"No journal found at {path}")

    text = path.read_text()
    existing = SECTION_RE.search(text)

    if existing:
        items = parse_items(existing.group(1))
        existing_labels = {label for _, label in items}
        for label in new_labels:
            if label not in existing_labels:
                items.append((False, label))
        new_section = build_section(items)
        new_text = SECTION_RE.sub(new_section, text, count=1)
    else:
        items = [(False, label) for label in new_labels]
        new_section = build_section(items)
        new_text = insert_after_frontmatter(text, new_section)

    path.write_text(new_text)
    items = parse_items(SECTION_RE.search(new_text).group(1))
    _out({"date": path.stem, "priorities": items_to_json(items)})


def cmd_done(path, query):
    if not path.exists():
        _err(f"No journal found at {path}")

    text = path.read_text()
    existing = SECTION_RE.search(text)
    if not existing:
        _err("No priorities section found")

    items = parse_items(existing.group(1))
    q = query.lower()
    matches = [(i, label) for i, (_, label) in enumerate(items) if q in label.lower()]

    if not matches:
        _err(f"No priority matching '{query}'")
    if len(matches) > 1:
        _err(f"Ambiguous match for '{query}': {[m[1] for m in matches]}")

    idx, label = matches[0]
    items[idx] = (True, label)
    new_section = build_section(items)
    new_text = SECTION_RE.sub(new_section, text, count=1)
    path.write_text(new_text)
    _out({"date": path.stem, "checked_off": label, "priorities": items_to_json(items)})


def cmd_list(path):
    if not path.exists():
        _out({"date": path.stem, "priorities": [], "note": "No journal for this date"})
        return

    text = path.read_text()
    existing = SECTION_RE.search(text)
    items = parse_items(existing.group(1)) if existing else []
    _out({"date": path.stem, "priorities": items_to_json(items)})


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _out(data):
    print(json.dumps(data, indent=2))
    sys.exit(0)


def _err(msg):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Manage daily priorities in the journal")
    parser.add_argument("--date", help="Date (YYYY-MM-DD); defaults to today in local time")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--set", nargs="+", metavar="TASK", help="Set priorities (replaces list, preserves checked state)")
    group.add_argument("--add", nargs="+", metavar="TASK", help="Add priorities (appends without replacing existing)")
    group.add_argument("--done", metavar="TASK", help="Mark a priority done (fuzzy match)")
    group.add_argument("--list", action="store_true", help="List current priorities")
    args = parser.parse_args()

    date_str = args.date if args.date else get_today_date()
    path = journal_path(date_str)

    if args.set:
        cmd_set(path, args.set)
    elif args.add:
        cmd_add(path, args.add)
    elif args.done:
        cmd_done(path, args.done)
    else:
        cmd_list(path)


if __name__ == "__main__":
    main()
