#!/usr/bin/env python3
"""
journal_fm.py — Shared frontmatter helpers for journal scripts.

Each field operation targets only its own field via regex replacement.
No full-frontmatter parse/rebuild — prevents cross-contamination between fields.

People format (Obsidian-friendly inline list with IDs as suffix):
  people: [Raymond Suke Flournoy (2), Joe Greco (177)]

All legacy formats (block-of-dicts, parallel people_ids list) are parsed
on read and always written in the current format.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import JOURNAL_DIR, LOCATION_FILE as LOCATION_MD, SOUL_FILE as SOUL_MD
from localtime import get_localtime


# ---------------------------------------------------------------------------
# File / path helpers
# ---------------------------------------------------------------------------

def get_current_location() -> str:
    try:
        loc = LOCATION_MD.read_text().strip()
        if loc:
            return loc
    except Exception:
        pass
    try:
        text = SOUL_MD.read_text()
        m = re.search(r'\*\*Current location:\*\*\s*(.+)', text)
        if m:
            loc = re.split(r'\s*[—–-]\s', m.group(1).strip())[0].strip()
            if loc:
                return loc
    except Exception:
        pass
    return "San Francisco"


def get_local_date() -> str:
    return get_localtime(location=get_current_location())["date"]


def get_journal_path(datestr: str | None = None) -> Path:
    if datestr is None:
        datestr = get_local_date()
    return JOURNAL_DIR / datestr[:4] / f"{datestr}.md"


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (fm_text, body). fm_text is raw text between the --- fences."""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---\n", 4)
    if end == -1:
        # Handle closing fence at EOF with no trailing newline
        if text.endswith("\n---"):
            end = len(text) - 4
        else:
            return "", text
    return text[4:end], text[end + 5:].lstrip("\n")


def reassemble(fm_text: str, body: str) -> str:
    return f"---\n{fm_text}\n---\n{body}"


# ---------------------------------------------------------------------------
# Tags — always inline: tags: [foo, bar]
# ---------------------------------------------------------------------------

def parse_tags(fm_text: str) -> list[str]:
    m = re.search(r'^tags:\s*\[([^\]]*)\]', fm_text, re.MULTILINE)
    if m:
        return [t.strip().strip('"\'') for t in m.group(1).split(',') if t.strip()]
    m = re.search(r'^tags:\s*\n((?:[ \t]+-[^\n]*\n?)+)', fm_text, re.MULTILINE)
    if m:
        tags = []
        for line in m.group(1).splitlines():
            stripped = line.strip()
            if stripped.startswith('-'):
                val = re.sub(r'^(-\s*)+', '', stripped).strip()
                if val:
                    tags.append(val)
        return tags
    return []


def serialize_tags(tags: list[str]) -> str:
    return f"tags: [{', '.join(tags)}]"


def replace_tags(fm_text: str, tags: list[str]) -> str:
    new_line = serialize_tags(tags)
    replaced, n = re.subn(r'^tags:[ \t]*\[[^\]]*\]', new_line, fm_text, flags=re.MULTILINE)
    if n:
        return replaced
    replaced, n = re.subn(
        r'^tags:\s*\n(?:[ \t]+-[ \t]+[^\n]*\n?)+',
        new_line + "\n",
        fm_text, flags=re.MULTILINE,
    )
    if n:
        return replaced
    return fm_text.rstrip("\n") + f"\n{new_line}\n"


# ---------------------------------------------------------------------------
# People — single inline list with IDs as "(N)" suffix:
#   people: [Raymond Suke Flournoy (2), Joe Greco (177)]
#
# All legacy formats (block-of-dicts, parallel people_ids list) are parsed
# on read and always written in the new format.
# ---------------------------------------------------------------------------

def parse_people(fm_text: str) -> list[dict]:
    """Return list of {name, id?} dicts. Handles all legacy formats transparently."""
    # All inline formats: people: [...]
    m = re.search(r'^people:\s*\[([^\]]*)\]', fm_text, re.MULTILINE)
    if m:
        raw = m.group(1).strip()
        if not raw:
            return []
        # Legacy parallel people_ids list
        ids: list[int | None] = []
        m_ids = re.search(r'^people_ids:\s*\[([^\]]*)\]', fm_text, re.MULTILINE)
        if m_ids:
            for tok in m_ids.group(1).split(','):
                tok = tok.strip()
                ids.append(int(tok) if tok.isdigit() else None)
        result = []
        for i, token in enumerate(raw.split(',')):
            token = token.strip().strip('"\'')
            if not token:
                continue
            # Current format: "Name (123)" — match last (digits) group
            m_id = re.match(r'^(.+?)\s+\((\d+)\)$', token)
            if m_id:
                result.append({"name": m_id.group(1), "id": int(m_id.group(2))})
            else:
                entry: dict = {"name": token}
                if i < len(ids) and ids[i] is not None:
                    entry["id"] = ids[i]
                result.append(entry)
        return result

    # Legacy block-of-dicts format:
    #   people:
    #     - name: Foo
    #       id: 1
    if re.search(r'^people:\s*\[\s*\]', fm_text, re.MULTILINE):
        return []
    m = re.search(r'^people:\s*\n((?:[ \t]+-[^\n]*\n?(?:[ \t]+[^-\n][^\n]*\n?)*)+)', fm_text, re.MULTILINE)
    if not m:
        return []
    people: list[dict] = []
    current: dict | None = None
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("- name:"):
            if current is not None:
                people.append(current)
            current = {"name": stripped[len("- name:"):].strip()}
        elif stripped.startswith("id:") and current is not None:
            try:
                current["id"] = int(stripped[len("id:"):].strip())
            except ValueError:
                pass
    if current is not None:
        people.append(current)
    return people


def serialize_people(people: list[dict]) -> str:
    if not people:
        return "people: []"
    parts = [f"{p['name']} ({p['id']})" if "id" in p else p["name"] for p in people]
    return f"people: [{', '.join(parts)}]"


def replace_people(fm_text: str, people: list[dict]) -> str:
    """Replace people in raw frontmatter. Removes legacy people_ids field."""
    new_block = serialize_people(people)

    # Remove legacy parallel people_ids field
    fm_text = re.sub(r'^people_ids:[ \t]*\[[^\]]*\]\n?', '', fm_text, flags=re.MULTILINE)

    # Replace inline people: [...]
    replaced, n = re.subn(r'^people:[ \t]*\[[^\]]*\]', new_block, fm_text, flags=re.MULTILINE)
    if n:
        return replaced

    # Replace block form (legacy dict format)
    replaced, n = re.subn(
        r'^people:\s*\n(?:[ \t]+[^\n]+\n?)+',
        new_block + "\n",
        fm_text, flags=re.MULTILINE,
    )
    if n:
        return replaced

    replaced, n = re.subn(r'^people:[ \t]*$', new_block, fm_text, flags=re.MULTILINE)
    if n:
        return replaced

    return fm_text.rstrip("\n") + f"\n{new_block}\n"


# ---------------------------------------------------------------------------
# Location — block list:
#   location:
#     - City, ST
# ---------------------------------------------------------------------------

def parse_location(fm_text: str) -> list[str]:
    if re.search(r'^location:\s*\[\s*\]', fm_text, re.MULTILINE):
        return []
    m = re.search(r'^location:\s*\n((?:[ \t]+-[^\n]*\n?)+)', fm_text, re.MULTILINE)
    if m:
        return [line.strip()[2:].strip() for line in m.group(1).splitlines() if line.strip().startswith("- ")]
    m = re.search(r'^location:\s*(.+)', fm_text, re.MULTILINE)
    if m:
        return [m.group(1).strip()]
    return []


def serialize_location(locations: list[str]) -> str:
    if not locations:
        return "location: []"
    return "location:\n" + "\n".join(f"  - {loc}" for loc in locations)


def replace_location(fm_text: str, locations: list[str]) -> str:
    new_block = serialize_location(locations)
    replaced, n = re.subn(r'^location:\s*\[\s*\]', new_block, fm_text, flags=re.MULTILINE)
    if n:
        return replaced
    replaced, n = re.subn(
        r'^location:\s*\n(?:[ \t]+-[^\n]*\n?)+',
        new_block + "\n",
        fm_text, flags=re.MULTILINE,
    )
    if n:
        return replaced
    replaced, n = re.subn(r'^location:[ \t]*.*', new_block, fm_text, flags=re.MULTILINE)
    if n:
        return replaced
    return fm_text.rstrip("\n") + f"\n{new_block}\n"


# ---------------------------------------------------------------------------
# Generic scalar fields (date, day, etc.)
# ---------------------------------------------------------------------------

def get_field(fm_text: str, key: str) -> str | None:
    m = re.search(rf'^{re.escape(key)}:\s*(.+)', fm_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def set_field(fm_text: str, key: str, value: str) -> str:
    new_line = f"{key}: {value}"
    replaced, n = re.subn(rf'^{re.escape(key)}:[ \t]*.*', new_line, fm_text, flags=re.MULTILINE)
    if n:
        return replaced
    return fm_text.rstrip("\n") + f"\n{new_line}\n"
