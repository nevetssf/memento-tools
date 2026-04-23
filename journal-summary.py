#!/usr/bin/env python3
"""
Journal summary: extracts journal entries and a representative photo for each day.

Usage:
    python3 journal-summary.py --days 2        # last 2 days
    python3 journal-summary.py --from 2026-03-26 --to 2026-03-28
    python3 journal-summary.py --date 2026-03-28   # single day

Outputs JSON with entries and photo paths for the agent to summarize.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import JOURNAL_DIR

JOURNAL_BASE = JOURNAL_DIR


def parse_frontmatter(text):
    """Extract frontmatter fields."""
    fm = {}
    match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
    if not match:
        return fm
    for line in match.group(1).splitlines():
        if ':' in line:
            key, val = line.split(':', 1)
            fm[key.strip()] = val.strip()
    return fm


def parse_entries(text):
    """Split journal into timestamped entries."""
    entries = []
    # Split on ## HH:MM TZ headers
    parts = re.split(r'^(## \d{1,2}:\d{2} [A-Z]{2,4})', text, flags=re.MULTILINE)

    i = 1
    while i < len(parts) - 1:
        header = parts[i].strip()
        body = parts[i + 1].strip()
        time_match = re.match(r'## (\d{1,2}:\d{2} [A-Z]{2,4})', header)
        timestamp = time_match.group(1) if time_match else header

        # Find photos in this entry
        photos = re.findall(r'!\[([^\]]*)\]\(photos/([^)]+)\)', body)

        # Clean body for summary (remove image links and details blocks)
        clean = re.sub(r'!\[[^\]]*\]\([^)]+\)\n?', '', body)
        clean = re.sub(r'<details>.*?</details>', '', clean, flags=re.DOTALL)
        clean = re.sub(r'\*([^*]+)\*', r'\1', clean)  # remove italics markers
        clean = clean.strip()

        entry = {
            "time": timestamp,
            "text": clean,
        }
        if photos:
            entry["photos"] = [{"alt": p[0], "filename": p[1]} for p in photos]

        entries.append(entry)
        i += 2

    return entries


def get_best_photo(entries, year_dir):
    """Pick the best representative photo for the day."""
    all_photos = []
    for entry in entries:
        for photo in entry.get("photos", []):
            photo_path = year_dir / "photos" / photo["filename"]
            if photo_path.exists():
                all_photos.append({
                    "filename": photo["filename"],
                    "alt": photo["alt"],
                    "path": str(photo_path),
                    "time": entry["time"],
                })

    if not all_photos:
        return None

    # Prefer photos with captions (non-empty alt text that isn't just a filename)
    captioned = [p for p in all_photos if p["alt"] and not p["alt"].endswith(('.jpg', '.png', '.jpeg'))]
    if captioned:
        # Pick one from the middle of the day for variety
        return captioned[len(captioned) // 2]

    return all_photos[len(all_photos) // 2]


def process_day(date_str):
    """Process a single day's journal."""
    year = date_str[:4]
    year_dir = JOURNAL_BASE / year
    journal_path = year_dir / f"{date_str}.md"

    if not journal_path.exists():
        return None

    text = journal_path.read_text()
    fm = parse_frontmatter(text)
    entries = parse_entries(text)
    best_photo = get_best_photo(entries, year_dir)

    # Build day summary data
    day_data = {
        "date": date_str,
        "day_of_week": fm.get("day", ""),
        "tags": fm.get("tags", ""),
        "entry_count": len(entries),
        "entries": entries,
    }
    if best_photo:
        day_data["photo"] = best_photo

    return day_data


def main():
    parser = argparse.ArgumentParser(description="Extract journal data for summary")
    parser.add_argument("--days", type=int, help="Number of days back from today")
    parser.add_argument("--from", dest="from_date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--date", help="Single date (YYYY-MM-DD)")
    args = parser.parse_args()

    today = datetime.now().date()

    if args.date:
        dates = [args.date]
    elif args.days:
        dates = [(today - timedelta(days=i)).isoformat() for i in range(args.days - 1, -1, -1)]
    elif args.from_date and args.to_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        end = datetime.strptime(args.to_date, "%Y-%m-%d").date()
        dates = []
        d = start
        while d <= end:
            dates.append(d.isoformat())
            d += timedelta(days=1)
    else:
        dates = [today.isoformat()]

    days = []
    for date_str in dates:
        day_data = process_day(date_str)
        if day_data:
            days.append(day_data)

    result = {
        "period": f"{dates[0]} to {dates[-1]}" if len(dates) > 1 else dates[0],
        "days_with_entries": len(days),
        "days": days,
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
