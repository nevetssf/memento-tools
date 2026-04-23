#!/usr/bin/env python3
"""
Rollover incomplete priorities from yesterday to today.
Run via cron at 2am local time.
"""
import subprocess
import sys
import re
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).parent))
from config import JOURNAL_DIR

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)

PRIORITIES_SCRIPT = Path(__file__).parent / "priorities.py"

def get_incomplete_priorities(journal_path: Path) -> list[str]:
    """Parse a journal file for unchecked priorities."""
    if not journal_path.exists():
        return []

    content = journal_path.read_text()
    incomplete = []

    # Look for unchecked priorities: - [ ] Task name
    pattern = re.compile(r'^-\s+\[\s+\]\s+(.+)$', re.MULTILINE)
    for match in pattern.finditer(content):
        task = match.group(1).strip()
        incomplete.append(task)

    return incomplete


def get_existing_priorities(date_str: str) -> list[str]:
    """Get current priorities for a given date via the priorities script."""
    cmd = ["python3", str(PRIORITIES_SCRIPT), "--date", date_str, "--list"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    import json
    try:
        data = json.loads(result.stdout)
        return [p["task"] for p in data.get("priorities", []) if not p.get("done")]
    except json.JSONDecodeError:
        return []

def main():
    # Find yesterday's journal
    year = YESTERDAY.strftime("%Y")
    journal_file = JOURNAL_DIR / year / f"{YESTERDAY}.md"

    if not journal_file.exists():
        print(f"No journal found for {YESTERDAY}, skipping rollover.")
        sys.exit(0)

    incomplete = get_incomplete_priorities(journal_file)

    if not incomplete:
        print(f"No incomplete priorities for {YESTERDAY}, nothing to roll over.")
        sys.exit(0)

    today_str = TODAY.strftime("%Y-%m-%d")

    # Get existing incomplete priorities for today
    existing = get_existing_priorities(today_str)

    # Merge: existing + incomplete from yesterday (deduplicated, preserving existing order)
    all_tasks = existing[:]
    for task in incomplete:
        if task not in all_tasks:
            all_tasks.append(task)

    print(f"Rollover {len(incomplete)} incomplete priorities to {today_str}:")
    for task in incomplete:
        print(f"  - {task}")
    if existing:
        print(f"Preserved existing incomplete priorities: {existing}")

    # Build the command
    cmd = ["python3", str(PRIORITIES_SCRIPT), "--date", today_str, "--set"] + all_tasks
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error running priorities script: {result.stderr}")
        sys.exit(1)
    else:
        print(f"Done.")

if __name__ == "__main__":
    main()
