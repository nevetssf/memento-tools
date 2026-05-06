#!/home/steven/memento-tools/.venv/bin/python3
"""Cron entry: roll incomplete priorities forward + seed template defaults.

Runs at 2 AM local time (configured in user crontab as 09:00 UTC for PDT).
Imports priorities.py directly — no subprocess, uses the venv interpreter
via the shebang above.

Behavior:
- "Today" is resolved via LOCATION.md → localtime (so it's correct even
  when Steven is travelling and the cron's 09:00 UTC trigger lands on a
  different local day than the system clock thinks).
- If today's journal doesn't exist yet, this is a no-op (the journal is
  initialized later by init_journal / log_entry; rollover will catch up
  on the next 2 AM tick or via a manual rollover_priorities call).
- Yesterday is `today - 1 day` in local-zone terms.
- Always seeds template defaults from Templates/Priorities.md (with-template
  is implicit at this entry point).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import priorities  # noqa: E402


def main() -> int:
    today = priorities.get_today_date()
    yesterday = (
        datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=1)
    ).isoformat()

    src = priorities.journal_path(yesterday)
    dst = priorities.journal_path(today)

    if not dst.exists():
        # Today's journal hasn't been created yet; nothing to merge into.
        # Cron will retry tomorrow; manual `rollover_priorities` MCP call
        # can also do it later once the journal exists.
        print(json.dumps({
            "skipped": True,
            "reason": f"no journal yet at {dst}",
            "from": yesterday,
            "to": today,
        }))
        return 0

    incomplete = [label for done, label in priorities.read_priorities(src) if not done]
    template_defaults = priorities.read_template_defaults()

    with priorities._file_lock(dst):
        items = priorities.read_priorities(dst)
        existing = {label for _, label in items}
        added_from_yesterday = []
        added_from_template = []
        for label in incomplete:
            if label not in existing:
                items.append((False, label))
                existing.add(label)
                added_from_yesterday.append(label)
        for label in template_defaults:
            if label not in existing:
                items.append((False, label))
                existing.add(label)
                added_from_template.append(label)
        priorities._write_priorities(dst, items)

    print(json.dumps({
        "from": yesterday,
        "to": today,
        "added_from_yesterday": added_from_yesterday,
        "added_from_template": added_from_template,
        "total_after": len(items),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
