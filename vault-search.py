#!/usr/bin/env python3
"""
vault-search.py — Full-text search across Steven's Obsidian vault.

Usage:
  python3 vault-search.py "query"
  python3 vault-search.py "query" --section Notes
  python3 vault-search.py "query" --max 5
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import VAULT_DIR

VAULT = VAULT_DIR

SECTIONS = {
    "journal": "Journal",
    "notes": "Notes",
    "people": "People",
    "clippings": "Clippings",
    "travel": "Travel",
    "medical": "Medical",
    "whiskeys": "Whiskeys",
    "wines": "Wines",
    "ideas": "Ideas",
    "reference": "Reference",
    "photography": "Photography",
    "website": "Website",
}


def extract_title(path: Path, content: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def extract_excerpt(content: str, pattern: re.Pattern, context: int = 2) -> str:
    lines = content.splitlines()
    matches = []
    for i, line in enumerate(lines):
        if pattern.search(line):
            start = max(0, i - context)
            end = min(len(lines), i + context + 1)
            snippet = "\n".join(lines[start:end]).strip()
            matches.append(snippet)
            if len(matches) >= 2:
                break
    return "\n…\n".join(matches) if matches else ""


def search(query: str, section: str | None, max_results: int) -> list[dict]:
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    search_root = VAULT
    if section:
        key = section.lower()
        folder = SECTIONS.get(key, section)
        search_root = VAULT / folder
        if not search_root.exists():
            return [{"error": f"Section '{section}' not found at {search_root}"}]

    results = []
    for md_file in sorted(search_root.rglob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not pattern.search(content):
            continue

        rel = md_file.relative_to(VAULT)
        section_name = rel.parts[0] if len(rel.parts) > 1 else "vault"
        results.append({
            "file": str(rel),
            "section": section_name,
            "title": extract_title(md_file, content),
            "excerpt": extract_excerpt(content, pattern),
        })
        if len(results) >= max_results:
            break

    return results


def main():
    parser = argparse.ArgumentParser(description="Search Obsidian vault")
    parser.add_argument("query", help="Search query (regex or plain text)")
    parser.add_argument("--section", help="Limit to a vault section (e.g. Notes, Journal, People)")
    parser.add_argument("--max", type=int, default=10, help="Max results (default: 10)")
    args = parser.parse_args()

    results = search(args.query, args.section, args.max)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
