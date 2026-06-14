#!/usr/bin/env python3
"""
Journal entry logger: appends a timestamped entry to the daily journal.

Usage:
    python3 journal-log.py "--entry" "Your entry text" [--time "HH:MM TZ"]

If --time is omitted, it uses the current local time.
"""

import argparse
import fcntl
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from journal_fm import get_current_location, get_journal_path, JOURNAL_DIR
from localtime import get_localtime


def get_time_info() -> dict:
    return get_localtime(location=get_current_location())


def day_of_week(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


def ensure_frontmatter(journal_path: Path, date_str: str):
    """Create the file with frontmatter, or prepend frontmatter to an existing file that lacks it."""
    template = f"---\ndate: {date_str}\nday: {day_of_week(date_str)}\ntags: []\npeople: []\n---\n"
    if not journal_path.exists():
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(template)
    else:
        existing = journal_path.read_text()
        if not existing.startswith("---"):
            sep = "\n" if existing and not existing.startswith("\n") else ""
            journal_path.write_text(template + sep + existing)


def normalize_timestamp(ts: str) -> str:
    """Accept loose time strings and return canonical `HH:MM TZ` (24-hour, zero-padded).

    Handles:
      "1:39 PM PDT"  → "13:39 PDT"
      "12:00 AM PDT" → "00:00 PDT"
      "12:30 PM PDT" → "12:30 PDT"
      "7:14 PDT"     → "07:14 PDT"   (just zero-pad)
      "13:39 PDT"    → "13:39 PDT"   (already canonical)
      ""             → ""             (empty stays empty — caller will derive from now)

    The agent has been observed passing 12-hour format with "AM"/"PM" suffixes
    despite the SKILL.md spec; rather than rejecting, normalize and move on so
    chronological insertion still works.
    """
    if not ts or not ts.strip():
        return ts
    s = ts.strip()
    # 12-hour with AM/PM suffix
    m = re.match(r'^(\d{1,2}):(\d{2})\s*([AaPp])[Mm]\s*(.*)$', s)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3).lower() == 'p':
            hour += 12
        rest = m.group(4).strip()
        return f"{hour:02d}:{m.group(2)}" + (f" {rest}" if rest else "")
    # Already 24-hour-ish; just zero-pad the hour for consistency
    m = re.match(r'^(\d{1,2}):(\d{2})\s*(.*)$', s)
    if m:
        rest = m.group(3).strip()
        return f"{int(m.group(1)):02d}:{m.group(2)}" + (f" {rest}" if rest else "")
    return s


def parse_time_minutes(timestamp: str) -> int | None:
    m = re.match(r'(\d{1,2}):(\d{2})', timestamp)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def insert_chronologically(text: str, new_entry: str, new_minutes: int) -> str:
    fm_end = 0
    if text.startswith("---\n"):
        close = text.find("\n---\n", 4)
        if close != -1:
            fm_end = close + 5

    body = text[fm_end:]
    entry_positions = []
    for m in re.finditer(r'^## (\d{1,2}:\d{2}\b[^\n]*)', body, re.MULTILINE):
        minutes = parse_time_minutes(m.group(1))
        if minutes is not None:
            entry_positions.append((m.start(), minutes))

    insert_at = next((pos for pos, mins in entry_positions if mins > new_minutes), None)

    if insert_at is not None:
        before = body[:insert_at].rstrip("\n")
        after = body[insert_at:]
        body = before + "\n\n" + new_entry + "\n\n" + after
    else:
        body = body.rstrip("\n") + "\n\n" + new_entry + "\n"

    return text[:fm_end] + body


def main():
    parser = argparse.ArgumentParser(description="Log a journal entry")
    parser.add_argument("--entry", help="Entry text")
    parser.add_argument("--time", default="", help="Time in HH:MM TZ format (e.g., 17:05 PDT)")
    parser.add_argument("--date", default="", help="Date YYYY-MM-DD (default: today in Steven's local time)")
    parser.add_argument("--init", action="store_true", help="Create today's journal file if it doesn't exist (no entry needed)")
    parser.add_argument("--people", nargs="+", metavar="NAME:ID", help="Add people to frontmatter (e.g., 'Harry Chen:169' 'Jane Doe:42')")
    parser.add_argument("--tags", nargs="+", metavar="TAG", help="Add tags to frontmatter (e.g., 'work' 'nvidia')")
    args = parser.parse_args()

    if not args.init and not args.entry:
        parser.error("--entry is required unless using --init")

    time_info = get_time_info()
    date_str = args.date or time_info["date"]
    if args.time:
        timestamp = normalize_timestamp(args.time)
        if timestamp != args.time.strip():
            # Surface in the gateway log so we can spot agent regressions —
            # the script silently fixes it, but the agent should be
            # passing canonical 24-hour HH:MM TZ per the SKILL.
            print(
                f"journal-log: normalized non-canonical time "
                f"{args.time!r} -> {timestamp!r} "
                f"(agent should pass 24-hour HH:MM TZ, e.g. '13:39 PDT')",
                file=sys.stderr,
            )
    else:
        timestamp = time_info["timestamp"]

    journal_path = get_journal_path(date_str)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    existed = journal_path.exists()
    if not existed:
        journal_path.touch()

    # Exclusive flock for the read-modify-write of the journal body. Blocks
    # across processes — important because cron jobs (priorities-rollover,
    # journal-header subprocesses) can race with this write.
    entry_index = 0
    with open(journal_path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        content = f.read()
        had_frontmatter = content.startswith("---")
        if not had_frontmatter:
            template = f"---\ndate: {date_str}\nday: {day_of_week(date_str)}\ntags: []\npeople: []\n---\n"
            sep = "\n" if content and not content.startswith("\n") else ""
            content = template + sep + content

        if not args.init:
            entry_text = f"## {timestamp}\n{args.entry}"
            new_minutes = parse_time_minutes(timestamp)
            if new_minutes is not None:
                content = insert_chronologically(content, entry_text, new_minutes)
            else:
                content = content.rstrip("\n") + "\n\n" + entry_text + "\n"
            entry_index = len(re.findall(r'^## \d{1,2}:\d{2}\b', content, re.MULTILINE))

        f.seek(0)
        f.truncate()
        f.write(content)

    if not had_frontmatter:
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "journal-location.py"), "--date", date_str, "--init"],
            capture_output=True, text=True, timeout=10
        )

    if args.init:
        if not existed:
            print(f"Created {journal_path}")
        elif not had_frontmatter:
            print(f"Added frontmatter: {journal_path}")
        else:
            print(f"Already exists: {journal_path}")
        return

    header_script = Path(__file__).parent / "journal-header.py"

    if args.people:
        for person_spec in args.people:
            if ":" in person_spec:
                name, pid = person_spec.rsplit(":", 1)
                cmd = [sys.executable, str(header_script), "--date", date_str, "--add-person", name, "--person-id", pid]
            else:
                cmd = [sys.executable, str(header_script), "--date", date_str, "--add-person", person_spec]
            subprocess.run(cmd, capture_output=True, text=True, timeout=10)

    if args.tags:
        for tag in args.tags:
            subprocess.run(
                [sys.executable, str(header_script), "--date", date_str, "--add-tag", tag],
                capture_output=True, text=True, timeout=10
            )

    char_count = len(args.entry)
    print(f"Logged — {timestamp} · {journal_path.name} · entry #{entry_index} · {char_count} chars")


if __name__ == "__main__":
    main()
