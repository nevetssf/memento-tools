#!/usr/bin/env python3
"""
journal-location.py — Manage location property in Obsidian journal frontmatter.

Usage:
  python3 journal-location.py                       # Show today's journal location(s)
  python3 journal-location.py --add "San Francisco" # Add location to today's journal
  python3 journal-location.py --init                # Set initial location on today's journal
  python3 journal-location.py --date YYYY-MM-DD ... # Operate on a specific date

Date is always derived from Steven's current local time (via localtime.py),
NOT UTC — so journal dates match his timezone.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from journal_fm import (
    get_current_location, get_local_date, get_journal_path,
    split_frontmatter, reassemble,
    parse_location, replace_location,
)


def get_locations(path: Path) -> list[str]:
    fm_text, _ = split_frontmatter(path.read_text())
    return parse_location(fm_text)


def add_location(datestr: str, location: str):
    path = get_journal_path(datestr)
    if not path.exists():
        print(f"No journal for {datestr}.")
        return
    text = path.read_text()
    fm_text, body = split_frontmatter(text)
    locations = parse_location(fm_text)
    if location not in locations:
        locations.append(location)
    path.write_text(reassemble(replace_location(fm_text, locations), body))
    print(f"Location(s) for {datestr}: {locations}")


def init_location(datestr: str):
    path = get_journal_path(datestr)
    if not path.exists():
        print(f"No journal for {datestr}.")
        return
    locations = get_locations(path)
    if locations:
        print(f"Already has location(s): {locations}")
        return
    add_location(datestr, get_current_location())


def show_locations(datestr: str):
    path = get_journal_path(datestr)
    if not path.exists():
        print(f"No journal for {datestr}.")
        return
    locations = get_locations(path)
    print(f"Locations: {locations}" if locations else "No location set.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage location in Obsidian journal frontmatter")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format (default: today in local time)")
    parser.add_argument("--add", help="Add a location")
    parser.add_argument("--init", action="store_true", help="Set initial location if none exists")
    parser.add_argument("--list", action="store_true", help="Show locations for the date")
    args = parser.parse_args()

    datestr = args.date or get_local_date()

    if args.add:
        add_location(datestr, args.add)
    elif args.init:
        init_location(datestr)
    elif args.list:
        show_locations(datestr)
    else:
        show_locations(datestr)
