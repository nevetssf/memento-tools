#!/usr/bin/env python3
"""
Photo logger: copies a photo to the Obsidian journal, extracts metadata,
and appends a timestamped entry to the daily journal.

Usage:
    python3 journal-photo-log.py <image_path> [--caption "text"] [--description "text"]

Outputs JSON with the results for the agent to confirm.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from config import JOURNAL_DIR, LOCATION_FILE, SOUL_FILE

JOURNAL_BASE = JOURNAL_DIR
LOCATION_MD = LOCATION_FILE
SOUL_MD = SOUL_FILE

def get_location():
    """Read current location from LOCATION.md, falling back to SOUL.md."""
    try:
        loc = LOCATION_MD.read_text().strip()
        if loc:
            return loc
    except Exception:
        pass
    try:
        text = SOUL_MD.read_text()
        match = re.search(r'\*\*Current location:\*\*\s*(.+)', text)
        if match:
            loc = match.group(1).strip()
            loc = re.split(r'\s*[—–-]{1,2}\s', loc)[0].strip()
            return loc
    except Exception:
        pass
    return "Unknown"

def get_local_time(location):
    """Get local time using localtime.py."""
    script = Path(__file__).parent / "localtime.py"
    try:
        result = subprocess.run(
            ["python3", str(script), location],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    # Fallback
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "timestamp": now.strftime("%H:%M PDT"),
        "abbreviation": "PDT",
        "datetime": now.isoformat(),
    }

def safe_location_name(location):
    """Convert location to filename-safe string."""
    name = re.sub(r'[,]', '', location)
    name = re.sub(r'[^a-zA-Z0-9\s]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name

def extract_metadata(image_path):
    """Extract EXIF metadata from the image file."""
    metadata = {}

    # Try exiftool first
    try:
        result = subprocess.run(
            ["exiftool", "-json", str(image_path)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)[0]
            field_map = {
                "ImageSize": "Dimensions",
                "Make": "Make",
                "Model": "Model",
                "LensModel": "Lens",
                "FocalLength": "FocalLength",
                "FNumber": "Aperture",
                "ExposureTime": "ShutterSpeed",
                "ISO": "ISO",
                "DateTimeOriginal": "DateTaken",
                "GPSPosition": "GPS",
            }
            for exif_key, label in field_map.items():
                if exif_key in data and data[exif_key]:
                    metadata[label] = str(data[exif_key])
            return metadata
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # Fallback to PIL
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        img = Image.open(image_path)
        metadata["Dimensions"] = f"{img.size[0]}x{img.size[1]}"
        exif = img._getexif()
        if exif:
            for k, v in exif.items():
                tag = TAGS.get(k, k)
                if tag == "Make":
                    metadata["Make"] = str(v)
                elif tag == "Model":
                    metadata["Model"] = str(v)
                elif tag == "FocalLength":
                    metadata["FocalLength"] = str(v)
                elif tag == "FNumber":
                    metadata["Aperture"] = str(v)
                elif tag == "ISOSpeedRatings":
                    metadata["ISO"] = str(v)
                elif tag == "ExposureTime":
                    metadata["ShutterSpeed"] = str(v)
                elif tag == "DateTimeOriginal":
                    metadata["DateTaken"] = str(v)
                elif tag == "LensModel":
                    metadata["Lens"] = str(v)
    except Exception:
        pass

    # Fallback to file command
    if not metadata:
        try:
            result = subprocess.run(
                ["file", str(image_path)],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                metadata["FileInfo"] = result.stdout.strip()
        except Exception:
            pass

    return metadata

def get_day_of_week(date_str):
    """Get day of week from date string."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%A")

def build_journal_entry(timestamp, filename, caption, description):
    """Build the markdown journal entry."""
    # Caption for image alt text
    alt_text = caption if caption else (description[:60] + "..." if description and len(description) > 60 else description or filename)

    lines = [f"\n## {timestamp}", f"![{alt_text}](photos/{filename})"]

    if description:
        lines.append(f"*{description}*")

    if caption:
        lines.append(f"\n{caption}")

    return "\n".join(lines)

def ensure_frontmatter(journal_path, date_str, day_of_week):
    """Create journal file with frontmatter if it doesn't exist."""
    if journal_path.exists():
        return
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = f"""---
date: {date_str}
day: {day_of_week}
tags: [photography]
people: []
---
"""
    journal_path.write_text(frontmatter)

def add_photography_tag(journal_path):
    """Add photography tag to existing journal if not present."""
    content = journal_path.read_text()
    if "photography" not in content:
        content = re.sub(
            r'(tags:\s*\[)([^\]]*)',
            lambda m: f"{m.group(1)}{m.group(2)}, photography" if m.group(2).strip() else f"{m.group(1)}photography",
            content,
            count=1
        )
        journal_path.write_text(content)

def main():
    parser = argparse.ArgumentParser(description="Log a photo to Obsidian journal")
    parser.add_argument("image_path", help="Path to the image file")
    parser.add_argument("--caption", default="", help="User's caption/comment")
    parser.add_argument("--description", default="", help="Auto-generated image description")
    parser.add_argument("--date", default="", help="Date YYYY-MM-DD (default: today in Steven's local time)")
    args = parser.parse_args()

    image_path = Path(args.image_path)
    if not image_path.exists():
        print(json.dumps({"error": f"File not found: {image_path}"}))
        sys.exit(1)

    # Get time and location
    location = get_location()
    time_info = get_local_time(location)
    date_str = args.date if args.date else time_info["date"]
    time_str = time_info["time"]
    timestamp = time_info["timestamp"]
    day_of_week = get_day_of_week(date_str)

    # Build filename using current wall-clock time for uniqueness
    loc_safe = safe_location_name(location)
    ext = image_path.suffix.lower()
    now = datetime.now()
    time_part = now.strftime("%H-%M-%S")
    new_filename = f"{date_str}_{time_part}_{loc_safe}{ext}"
    year = date_str[:4]
    photos_dir = JOURNAL_BASE / year / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    # If seconds still collide, add milliseconds
    if (photos_dir / new_filename).exists():
        time_part = now.strftime("%H-%M-%S-") + f"{now.microsecond // 1000:03d}"
        new_filename = f"{date_str}_{time_part}_{loc_safe}{ext}"

    dest_path = photos_dir / new_filename

    # Copy file
    shutil.copy2(str(image_path), str(dest_path))

    # Build and append journal entry
    journal_path = JOURNAL_BASE / year / f"{date_str}.md"
    ensure_frontmatter(journal_path, date_str, day_of_week)
    add_photography_tag(journal_path)

    entry = build_journal_entry(
        timestamp=timestamp,
        filename=new_filename,
        caption=args.caption,
        description=args.description,
    )

    with open(journal_path, "a") as f:
        f.write(entry + "\n")

    result = {
        "status": "ok",
        "timestamp": timestamp,
        "file": str(dest_path),
        "journal": str(journal_path),
        "filename": new_filename,
    }
    print(json.dumps(result))

if __name__ == "__main__":
    main()
