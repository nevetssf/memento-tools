#!/usr/bin/env python3
"""
journal-header.py — Read and write Obsidian journal frontmatter safely.

AGENTS: ALWAYS use this script to modify journal frontmatter. Never edit the
YAML header directly — hand-written YAML causes malformed tags, wrong types,
and nested-list bugs.

Usage:
  python3 journal-header.py --get-tags
  python3 journal-header.py --add-tag "running"
  python3 journal-header.py --set-tags "social" "work" "running"
  python3 journal-header.py --get day
  python3 journal-header.py --set day Monday
  python3 journal-header.py --date 2026-03-28 --add-tag "travel"
  python3 journal-header.py --get-people
  python3 journal-header.py --add-person "Name" --person-id 42
  python3 journal-header.py --migrate-people   # one-time migration of old block format

Tags are always written as an inline YAML list: tags: [social, running]
People are always written as parallel inline lists:
  people: [Name1, Name2]
  people_ids: [1, 2]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from journal_fm import (
    get_local_date, get_journal_path,
    split_frontmatter, reassemble,
    parse_tags, replace_tags,
    parse_people, replace_people,
    get_field, set_field,
    JOURNAL_DIR,
)


def read_file(path: Path) -> str:
    if not path.exists():
        print(f"Error: journal file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_get_tags(datestr: str):
    path = get_journal_path(datestr)
    fm_text, _ = split_frontmatter(read_file(path))
    print(json.dumps(parse_tags(fm_text)))


def cmd_add_tag(datestr: str, tag: str):
    path = get_journal_path(datestr)
    text = read_file(path)
    fm_text, body = split_frontmatter(text)
    tags = parse_tags(fm_text)
    if tag not in tags:
        tags.append(tag)
    path.write_text(reassemble(replace_tags(fm_text, tags), body))
    print(json.dumps({"date": datestr, "tags": tags}))


def cmd_set_tags(datestr: str, tags: list[str]):
    path = get_journal_path(datestr)
    text = read_file(path)
    fm_text, body = split_frontmatter(text)
    path.write_text(reassemble(replace_tags(fm_text, tags), body))
    print(json.dumps({"date": datestr, "tags": tags}))


def cmd_get(datestr: str, key: str):
    path = get_journal_path(datestr)
    fm_text, _ = split_frontmatter(read_file(path))
    val = get_field(fm_text, key)
    if val is None:
        print(f"Field '{key}' not found.", file=sys.stderr)
        sys.exit(1)
    print(val)


def cmd_set(datestr: str, key: str, value: str):
    path = get_journal_path(datestr)
    text = read_file(path)
    fm_text, body = split_frontmatter(text)
    path.write_text(reassemble(set_field(fm_text, key, value), body))
    print(json.dumps({"date": datestr, key: value}))


def cmd_get_people(datestr: str):
    path = get_journal_path(datestr)
    fm_text, _ = split_frontmatter(read_file(path))
    print(json.dumps(parse_people(fm_text)))


def cmd_add_person(datestr: str, name: str, person_id: int | None):
    path = get_journal_path(datestr)
    text = read_file(path)
    fm_text, body = split_frontmatter(text)
    people = parse_people(fm_text)
    if not any(p["name"] == name for p in people):
        entry: dict = {"name": name}
        if person_id is not None:
            entry["id"] = person_id
        people.append(entry)
    path.write_text(reassemble(replace_people(fm_text, people), body))
    print(json.dumps({"date": datestr, "people": people}))


def cmd_migrate_people():
    """Migrate all journal entries to Name (id) people format."""
    migrated = []
    skipped = []
    for md_file in sorted(JOURNAL_DIR.rglob("*.md")):
        text = md_file.read_text()
        fm_text, body = split_frontmatter(text)
        if not fm_text or "people:" not in fm_text:
            skipped.append(md_file.name)
            continue
        people = parse_people(fm_text)
        new_fm = replace_people(fm_text, people)
        if new_fm != fm_text:
            md_file.write_text(reassemble(new_fm, body))
            migrated.append(md_file.name)
        else:
            skipped.append(md_file.name)
    print(json.dumps({"migrated": migrated, "skipped_count": len(skipped)}))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Read/write Obsidian journal frontmatter. Always use this instead of editing YAML directly."
    )
    parser.add_argument("--date", help="Date YYYY-MM-DD (default: today in Steven's local time)")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--get-tags", action="store_true", help="Print tags as JSON array")
    group.add_argument("--add-tag", metavar="TAG", help="Add a tag (idempotent)")
    group.add_argument("--set-tags", nargs="+", metavar="TAG", help="Replace all tags")
    group.add_argument("--get", metavar="FIELD", help="Print a scalar frontmatter field")
    group.add_argument("--set", nargs=2, metavar=("FIELD", "VALUE"), help="Set a scalar frontmatter field")
    group.add_argument("--get-people", action="store_true", help="Print people as JSON array")
    group.add_argument("--add-person", metavar="NAME", help="Add a person (use --person-id for their DB id)")
    group.add_argument("--migrate-people", action="store_true",
                       help="One-time migration: convert old block-dict people to parallel inline lists")

    parser.add_argument("--person-id", type=int, help="People DB id (used with --add-person)")

    args = parser.parse_args()
    datestr = args.date or get_local_date()

    if args.get_tags:
        cmd_get_tags(datestr)
    elif args.add_tag:
        cmd_add_tag(datestr, args.add_tag)
    elif args.set_tags:
        cmd_set_tags(datestr, args.set_tags)
    elif args.get:
        cmd_get(datestr, args.get)
    elif args.set:
        cmd_set(datestr, args.set[0], args.set[1])
    elif args.get_people:
        cmd_get_people(datestr)
    elif args.add_person:
        cmd_add_person(datestr, args.add_person, args.person_id)
    elif args.migrate_people:
        cmd_migrate_people()
