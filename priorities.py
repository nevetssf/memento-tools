#!/usr/bin/env python3
"""
priorities.py — Manage daily priorities in the Obsidian journal.

Storage: a `## Priorities` checkbox section at the top of each day's journal
file (`Journal/YYYY/YYYY-MM-DD.md`), placed immediately after the YAML
frontmatter and before the first timestamped entry.

The default-priority *template* lives in the vault at
`Templates/Priorities.md` (Steven maintains it manually). Same checkbox
format. Rollover and apply_template seed today's list from there.

CLI:
  priorities.py --list                              # today's priorities
  priorities.py --set "Task 1" "Task 2" ...         # replace list
  priorities.py --add "Task 3" ...                  # append (deduped)
  priorities.py --done "lunch"                      # check off (fuzzy)
  priorities.py --unmark "lunch"                    # uncheck (fuzzy)
  priorities.py --remove "lunch"                    # delete a priority
  priorities.py --apply-template                    # merge template defaults in
  priorities.py --rollover                          # pull incomplete from yesterday
  priorities.py --rollover --from 2026-04-30        # pull from a specific day
  priorities.py --summary                           # last 7 days completion stats
  priorities.py --summary --from A --to B           # range completion stats
  priorities.py --date YYYY-MM-DD --list            # any of the above for a specific day

Output: JSON on stdout (errors to stderr).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import re
import sys
from collections import Counter
from datetime import date as _date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import JOURNAL_DIR, LOCATION_FILE, VAULT_DIR
from localtime import get_localtime  # noqa: E402

JOURNAL_BASE = JOURNAL_DIR
LOCATION_MD = LOCATION_FILE
TEMPLATE_PATH = VAULT_DIR / "Templates" / "Priorities.md"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def get_today_date() -> str:
    """Today in Steven's local zone, via LOCATION.md → localtime resolver."""
    location = ""
    if LOCATION_MD.exists():
        location = LOCATION_MD.read_text().strip()
    try:
        return get_localtime(location=location or None)["date"]
    except Exception:
        return _date.today().isoformat()


def journal_path(date_str: str) -> Path:
    year = date_str[:4]
    return JOURNAL_BASE / year / f"{date_str}.md"


def _parse_iso_date(s: str) -> _date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# File locking — serialize concurrent writers (Obsidian sync, multiple agents)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _file_lock(path: Path):
    """Exclusive flock on a sidecar `.lock` file next to the target.

    Same pattern as chat_signal.py: lock applies even if the file doesn't
    exist yet, since we lock the sidecar.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Priorities section parsing / building
# ---------------------------------------------------------------------------

# Whole `## Priorities` block (header + all checkbox lines)
SECTION_RE = re.compile(
    r'(## Priorities\n(?:- \[[ x]\] [^\n]*\n?)*)',
    re.MULTILINE,
)

ITEM_RE = re.compile(r'^- \[([ x])\] (.+)$')
# Bare bullet lines used in some templates: `- task name`
BARE_BULLET_RE = re.compile(r'^- (?!\[)(.+)$')


def parse_items(section_text: str) -> list[tuple[bool, str]]:
    """Return list of (checked, label) from a `## Priorities` block."""
    items = []
    for line in section_text.splitlines():
        m = ITEM_RE.match(line)
        if m:
            items.append((m.group(1) == 'x', m.group(2).strip()))
    return items


def build_section(items: list[tuple[bool, str]]) -> str:
    """Render a `## Priorities` block from list of (checked, label)."""
    lines = ["## Priorities"]
    for checked, label in items:
        mark = 'x' if checked else ' '
        lines.append(f"- [{mark}] {label}")
    return "\n".join(lines) + "\n"


def insert_after_frontmatter(text: str, section: str) -> str:
    fm = re.match(r'^---\n.*?\n---\n', text, re.DOTALL)
    if fm:
        pos = fm.end()
        return text[:pos] + "\n" + section + "\n" + text[pos:]
    return section + "\n" + text


def items_to_json(items: list[tuple[bool, str]]) -> list[dict]:
    return [{"done": checked, "task": label} for checked, label in items]


def read_priorities(path: Path) -> list[tuple[bool, str]]:
    """Read priorities from a journal file. Empty list if no section/file."""
    if not path.exists():
        return []
    m = SECTION_RE.search(path.read_text())
    return parse_items(m.group(1)) if m else []


# ---------------------------------------------------------------------------
# Template defaults — Templates/Priorities.md in the vault
# ---------------------------------------------------------------------------

def read_template_defaults() -> list[str]:
    """Read default-priority labels from `Templates/Priorities.md`.

    Accepts both checkbox lines (`- [ ] task`) and bare bullets (`- task`).
    Skips blank lines and any line that doesn't start with `-`. Existing
    check state in the template is *ignored* — defaults are always seeded
    unchecked.

    Returns [] if the template file is missing or empty (so callers can
    treat absence of template as "no defaults" without crashing).
    """
    if not TEMPLATE_PATH.exists():
        return []
    labels: list[str] = []
    for raw in TEMPLATE_PATH.read_text().splitlines():
        line = raw.rstrip()
        m = ITEM_RE.match(line)
        if m:
            labels.append(m.group(2).strip())
            continue
        m = BARE_BULLET_RE.match(line)
        if m:
            labels.append(m.group(1).strip())
    return labels


# ---------------------------------------------------------------------------
# Match helpers (used by --done / --unmark / --remove)
# ---------------------------------------------------------------------------

def _resolve_match(items: list[tuple[bool, str]], query: str) -> int:
    """Return index of the single matching item or raise ValueError.

    Match precedence (case-insensitive):
      1. Exact match on label → wins outright (even if other items contain
         the same string as substring).
      2. Otherwise, single substring match → use it.
      3. Multiple substring matches with no exact → ambiguous; raise.
      4. No match → raise.
    """
    q = query.strip().lower()
    exact = [i for i, (_, label) in enumerate(items) if label.lower() == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(f"Multiple priorities exactly named {query!r}")
    fuzzy = [(i, label) for i, (_, label) in enumerate(items) if q in label.lower()]
    if not fuzzy:
        raise ValueError(f"No priority matching {query!r}")
    if len(fuzzy) > 1:
        raise ValueError(f"Ambiguous match for {query!r}: {[m[1] for m in fuzzy]}")
    return fuzzy[0][0]


# ---------------------------------------------------------------------------
# Write helpers — every writer uses the same flock + read-modify-write
# ---------------------------------------------------------------------------

def _write_priorities(path: Path, items: list[tuple[bool, str]]) -> None:
    """Replace (or insert) the `## Priorities` section atomically. Caller holds the lock."""
    section = build_section(items)
    if path.exists():
        text = path.read_text()
        if SECTION_RE.search(text):
            new_text = SECTION_RE.sub(section, text, count=1)
        else:
            new_text = insert_after_frontmatter(text, section)
        path.write_text(new_text)
    else:
        # No journal file yet — refuse writes for non-empty priority lists.
        # The caller (e.g. cmd_apply_template) decides whether to error
        # or to no-op when the journal is absent.
        raise FileNotFoundError(str(path))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_set(path: Path, new_labels: list[str]) -> None:
    if not path.exists():
        _err(f"No journal found at {path}")
    with _file_lock(path):
        old = {label: checked for checked, label in read_priorities(path)}
        merged = [(old.get(label, False), label) for label in new_labels]
        _write_priorities(path, merged)
    _out({"date": path.stem, "priorities": items_to_json(merged)})


def cmd_add(path: Path, new_labels: list[str]) -> None:
    if not path.exists():
        _err(f"No journal found at {path}")
    with _file_lock(path):
        items = read_priorities(path)
        existing_labels = {label for _, label in items}
        for label in new_labels:
            if label not in existing_labels:
                items.append((False, label))
        _write_priorities(path, items)
    _out({"date": path.stem, "priorities": items_to_json(items)})


def cmd_done(path: Path, query: str) -> None:
    if not path.exists():
        _err(f"No journal found at {path}")
    with _file_lock(path):
        items = read_priorities(path)
        if not items:
            _err("No priorities section found")
        try:
            idx = _resolve_match(items, query)
        except ValueError as e:
            _err(str(e))
        items[idx] = (True, items[idx][1])
        _write_priorities(path, items)
    _out({"date": path.stem, "checked_off": items[idx][1], "priorities": items_to_json(items)})


def cmd_unmark(path: Path, query: str) -> None:
    if not path.exists():
        _err(f"No journal found at {path}")
    with _file_lock(path):
        items = read_priorities(path)
        if not items:
            _err("No priorities section found")
        try:
            idx = _resolve_match(items, query)
        except ValueError as e:
            _err(str(e))
        items[idx] = (False, items[idx][1])
        _write_priorities(path, items)
    _out({"date": path.stem, "unchecked": items[idx][1], "priorities": items_to_json(items)})


def cmd_remove(path: Path, query: str) -> None:
    if not path.exists():
        _err(f"No journal found at {path}")
    with _file_lock(path):
        items = read_priorities(path)
        if not items:
            _err("No priorities section found")
        try:
            idx = _resolve_match(items, query)
        except ValueError as e:
            _err(str(e))
        removed_label = items[idx][1]
        items = items[:idx] + items[idx + 1:]
        _write_priorities(path, items)
    _out({"date": path.stem, "removed": removed_label, "priorities": items_to_json(items)})


def cmd_list(path: Path) -> None:
    if not path.exists():
        _out({"date": path.stem, "priorities": [], "note": "No journal for this date"})
        return
    items = read_priorities(path)
    _out({"date": path.stem, "priorities": items_to_json(items)})


def cmd_apply_template(path: Path) -> None:
    """Merge `Templates/Priorities.md` defaults into the day's list.

    Defaults are added unchecked and de-duped against existing labels.
    Existing priorities (and their check states) are preserved. If the
    template file is missing or empty, this is a no-op (returns the
    current list).
    """
    defaults = read_template_defaults()
    if not path.exists():
        _err(f"No journal found at {path}")
    with _file_lock(path):
        items = read_priorities(path)
        existing = {label for _, label in items}
        added = []
        for label in defaults:
            if label not in existing:
                items.append((False, label))
                added.append(label)
        _write_priorities(path, items)
    _out({
        "date": path.stem,
        "applied_template": str(TEMPLATE_PATH),
        "added": added,
        "priorities": items_to_json(items),
    })


def cmd_rollover(from_date: str, to_date: str, with_template: bool) -> None:
    """Pull incomplete priorities from `from_date` into `to_date`.

    With `with_template=True`, also seeds defaults from the template.
    Existing priorities at `to_date` are preserved (incl. check states);
    new items are appended unchecked, deduped by label.

    If `from_date`'s journal is missing, that side is silently empty.
    `to_date`'s journal must exist (we don't auto-create).
    """
    src_path = journal_path(from_date)
    dst_path = journal_path(to_date)

    incomplete_yesterday = [label for done, label in read_priorities(src_path) if not done]
    template_defaults = read_template_defaults() if with_template else []

    if not dst_path.exists():
        _err(f"No journal found at {dst_path} (target date {to_date}). "
             "Create it before rolling over.")

    with _file_lock(dst_path):
        items = read_priorities(dst_path)
        existing = {label for _, label in items}
        added_from_yesterday = []
        added_from_template = []
        for label in incomplete_yesterday:
            if label not in existing:
                items.append((False, label))
                existing.add(label)
                added_from_yesterday.append(label)
        for label in template_defaults:
            if label not in existing:
                items.append((False, label))
                existing.add(label)
                added_from_template.append(label)
        _write_priorities(dst_path, items)
    _out({
        "from": from_date,
        "to": to_date,
        "added_from_yesterday": added_from_yesterday,
        "added_from_template": added_from_template,
        "priorities": items_to_json(items),
    })


def cmd_summary(from_date: str, to_date: str) -> None:
    """Aggregate completion stats over an inclusive date range."""
    d_from = _parse_iso_date(from_date)
    d_to = _parse_iso_date(to_date)
    if d_from > d_to:
        d_from, d_to = d_to, d_from

    total = done = 0
    days_with_entries = 0
    daily = []
    open_tasks: Counter = Counter()

    cur = d_from
    while cur <= d_to:
        ds = cur.isoformat()
        items = read_priorities(journal_path(ds))
        if items:
            days_with_entries += 1
            day_done = sum(1 for c, _ in items if c)
            day_total = len(items)
            total += day_total
            done += day_done
            daily.append({
                "date": ds,
                "total": day_total,
                "done": day_done,
                "open": day_total - day_done,
            })
            for c, label in items:
                if not c:
                    open_tasks[label] += 1
        cur += timedelta(days=1)

    _out({
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "days_with_entries": days_with_entries,
        "total_priorities": total,
        "completed": done,
        "open": total - done,
        "completion_rate": round(done / total, 3) if total else None,
        "frequently_open": [
            {"task": label, "open_days": n}
            for label, n in open_tasks.most_common(10)
            if n >= 2
        ],
        "daily": daily,
    })


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _out(data) -> None:
    print(json.dumps(data, indent=2))
    sys.exit(0)


def _err(msg: str) -> None:
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Manage daily priorities in the journal")
    parser.add_argument("--date", help="Single-day target (YYYY-MM-DD); default: today in local time")
    parser.add_argument("--from", dest="from_date", help="Range start (YYYY-MM-DD) for --rollover / --summary")
    parser.add_argument("--to", dest="to_date", help="Range end (YYYY-MM-DD) for --rollover / --summary")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List current priorities")
    group.add_argument("--set", nargs="+", metavar="TASK", help="Replace list (preserves checked state for matching labels)")
    group.add_argument("--add", nargs="+", metavar="TASK", help="Append priorities (deduped)")
    group.add_argument("--done", metavar="TASK", help="Mark a priority done (exact match preferred, then fuzzy)")
    group.add_argument("--unmark", metavar="TASK", help="Mark a priority undone (exact match preferred, then fuzzy)")
    group.add_argument("--remove", metavar="TASK", help="Remove a priority from the list (fuzzy)")
    group.add_argument("--apply-template", action="store_true",
                       help="Merge defaults from Templates/Priorities.md into the day's list")
    group.add_argument("--rollover", action="store_true",
                       help="Roll incomplete priorities forward (default: yesterday → today). "
                            "Also seeds template defaults.")
    group.add_argument("--summary", action="store_true",
                       help="Completion stats over a range. Default: last 7 days through today.")
    args = parser.parse_args()

    today = get_today_date()

    if args.list:
        cmd_list(journal_path(args.date or today))
    elif args.set:
        cmd_set(journal_path(args.date or today), args.set)
    elif args.add:
        cmd_add(journal_path(args.date or today), args.add)
    elif args.done:
        cmd_done(journal_path(args.date or today), args.done)
    elif args.unmark:
        cmd_unmark(journal_path(args.date or today), args.unmark)
    elif args.remove:
        cmd_remove(journal_path(args.date or today), args.remove)
    elif args.apply_template:
        cmd_apply_template(journal_path(args.date or today))
    elif args.rollover:
        to_d = args.to_date or args.date or today
        try:
            from_d = args.from_date or (
                _parse_iso_date(to_d) - timedelta(days=1)
            ).isoformat()
        except ValueError as e:
            _err(f"Bad --to/--date {to_d!r}: {e}")
        cmd_rollover(from_d, to_d, with_template=True)
    else:  # summary
        to_d = args.to_date or args.date or today
        try:
            from_d = args.from_date or (
                _parse_iso_date(to_d) - timedelta(days=6)
            ).isoformat()  # last 7 days inclusive
        except ValueError as e:
            _err(f"Bad --to/--date {to_d!r}: {e}")
        cmd_summary(from_d, to_d)


if __name__ == "__main__":
    main()
