#!/usr/bin/env python3
"""cellar_advent_split.py — parse Advent calendar bottles into real producers.

The original cellar migration treated each "Whiskeys Advent 20XX" calendar as
a single producer. This script re-attributes each bottle to its actual
distillery, while leaving the Whiskeys Advent producer rows + their vault
notes intact (per Steven: "leave the note alone — keep it as a memory of what
we tasted").

Pipeline:
  1) Read all bottles whose producer.name LIKE 'Whiskeys Advent%'.
  2) Send the bottle names to the chat model in one batch, prompt for JSON
     {"day": N, "producer": "...", "bottle": "..."}.
  3) Print proposals as a table.
  4) With --apply, perform DB updates:
       - add real producer if missing
       - move bottle (producer_id, name)
       - rename + relocate vault note
       - add tasting record with location="Whiskeys Advent 20XX", notes="Day N"
  5) After moves, append a "Day N → [[link]]" cross-reference list to each
     Whiskeys Advent producer note.

Usage:
    cellar_advent_split.py            # dry-run, parse + print proposed actions
    cellar_advent_split.py --apply    # actually move
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cellar as cdb  # noqa: E402
import vault_embed    # noqa: E402  for chat()


PROMPT = """\
You will receive a list of whisky bottle names from advent calendars. Each
starts with "Day N — ". Some are just "Day N" with no info — for those, return
producer=null and bottle=null.

For each, extract:
  - day:      the integer N
  - producer: the distillery / brand (e.g. "Jack Daniel's", "Crown Royal", "Buffalo Trace")
  - bottle:   the specific expression / line (e.g. "Gentleman Jack", "Old No. 7"). May be null
              if the entry is just the brand name with no specific expression (e.g. "Crown Royal" alone).

Producer parsing rules:
  - "Jack Daniel's Gentleman Jack" → producer="Jack Daniel's", bottle="Gentleman Jack"
  - "Crown Royal" alone → producer="Crown Royal", bottle=null
  - "Elijah Craig Small Batch Straight Bourbon Whiskey" → producer="Elijah Craig", bottle="Small Batch"
  - "Powers Irish Whiskey" → producer="Powers", bottle="Irish Whiskey"
  - "Johnnie Walker Red Label" → producer="Johnnie Walker", bottle="Red Label"
  - "Bulleit Bourbon" → producer="Bulleit", bottle="Bourbon"
  - "Bulleit Rye" → producer="Bulleit", bottle="Rye"
  - "Knob Creek Bourbon (100 Proof)" → producer="Knob Creek", bottle="Bourbon 100 Proof"
  - "Maker's Mark Bourbon" → producer="Maker's Mark", bottle="Bourbon"
  - "Still Austin Straight Rye Whiskey" → producer="Still Austin", bottle="Straight Rye"
  - "Laws Four Grain Straight Bourbon" → producer="Laws", bottle="Four Grain Straight Bourbon"
  - "Colorado Straight Bourbon Whiskey" → producer="Colorado Straight Bourbon Whiskey", bottle=null  (unknown brand, treat the whole thing as producer)

Strip filler suffixes from the bottle field:
  - Trailing "Whiskey", "Whisky", "Bourbon" alone is okay if it's the only descriptor
  - But "Straight Bourbon Whiskey" → keep "Straight Bourbon" if there's a more specific word

Return STRICT JSON: an array of objects, in the same order as the input. Example:
[
  {"day": 1, "producer": "Jack Daniel's", "bottle": "Gentleman Jack"},
  {"day": 4, "producer": "Crown Royal", "bottle": null}
]

Input:
"""


def parse_via_llm(names: list[str]) -> list[dict]:
    body = "\n".join(f"- {n}" for n in names)
    raw = vault_embed.chat(prompt=PROMPT + body, temperature=0, max_tokens=4096)
    # The model may wrap in code fences; strip them.
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)
    return json.loads(raw)


def collect_advent_bottles(con) -> list[dict]:
    rows = con.execute("""
        SELECT b.id, b.name AS bottle_name, b.type, b.obsidian_file,
               p.id AS producer_id, p.name AS producer_name
          FROM bottles b
          JOIN producers p ON b.producer_id = p.id
         WHERE p.name LIKE 'Whiskeys Advent%'
         ORDER BY p.name,
                  CAST(SUBSTR(b.name, 5,
                       CASE WHEN INSTR(b.name, ' — ') > 0
                            THEN INSTR(b.name, ' — ') - 5 ELSE LENGTH(b.name) END
                  ) AS INTEGER)
    """).fetchall()
    return [dict(r) for r in rows]


def extract_day_int(name: str) -> int | None:
    m = re.match(r"Day\s+(\d+)", name)
    return int(m.group(1)) if m else None


def apply_changes(con, advent_bottles: list[dict], parses: list[dict]) -> dict:
    """Perform the DB + filesystem moves. advent_bottles and parses are zip-aligned.

    When the (real_producer, target_bottle_name) pair already exists in the
    bottles table, we MERGE rather than create a duplicate:
      - delete the Advent bottle row (and its vault note, if any)
      - add a tasting on the EXISTING real-producer bottle with
        location=<advent calendar>, notes=Day N — preserves the calendar
        context as a tasting record without violating UNIQUE(producer_id,name).
    """
    counts = {
        "moved": 0, "kept": 0, "merged": 0,
        "tastings_added": 0, "new_producers": 0, "note_renames": 0,
    }
    actions = []

    for bottle, parse in zip(advent_bottles, parses):
        day = parse.get("day")
        new_producer_name = parse.get("producer")
        new_bottle_name = parse.get("bottle")

        if not new_producer_name or new_producer_name == bottle["producer_name"]:
            counts["kept"] += 1
            continue

        # Add real producer (idempotent)
        existing = cdb.get_producer(con, new_producer_name)
        if existing:
            new_producer_id = existing["id"]
        else:
            new_producer_id = cdb.add_producer(con, new_producer_name)
            counts["new_producers"] += 1

        target_bottle_name = new_bottle_name or new_producer_name

        # Detect collision with an existing bottle under the new producer.
        collision = con.execute(
            "SELECT id, obsidian_file FROM bottles WHERE producer_id=? AND name=? COLLATE NOCASE",
            (new_producer_id, target_bottle_name),
        ).fetchone()

        if collision and collision["id"] != bottle["id"]:
            # Merge: keep the existing bottle, delete the Advent one, record the
            # calendar tasting on the existing bottle.
            cdb.add_tasting(
                con, collision["id"],
                location=bottle["producer_name"],
                notes=f"Day {day}" if day is not None else None,
            )
            counts["tastings_added"] += 1

            # Delete the Advent bottle's vault note file (won't delete the
            # surviving real-producer bottle's note, which is what we want).
            if bottle["obsidian_file"]:
                old_abs = cdb.absolute(bottle["obsidian_file"])
                if old_abs.exists():
                    old_abs.unlink()

            con.execute("DELETE FROM bottles WHERE id=?", (bottle["id"],))
            counts["merged"] += 1
            actions.append({
                "bottle_id": bottle["id"],
                "from": f"{bottle['producer_name']} / {bottle['bottle_name']}",
                "to":   f"{new_producer_name} / {target_bottle_name}",
                "day":  day,
                "merged_into": collision["id"],
            })
            continue

        # Normal path: re-attribute and rename
        new_path = cdb.bottle_note_path(bottle["type"], new_producer_name, target_bottle_name)
        old_path_rel = bottle["obsidian_file"]

        con.execute(
            """UPDATE bottles
                  SET producer_id = ?, name = ?, obsidian_file = ?,
                      updated_at = datetime('now')
                WHERE id = ?""",
            (new_producer_id, target_bottle_name, new_path, bottle["id"]),
        )
        counts["moved"] += 1

        if old_path_rel:
            old_abs = cdb.absolute(old_path_rel)
            new_abs = cdb.absolute(new_path)
            if old_abs.exists() and old_abs != new_abs:
                new_abs.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_abs), str(new_abs))
                counts["note_renames"] += 1

        cdb.add_tasting(
            con, bottle["id"],
            location=bottle["producer_name"],
            notes=f"Day {day}" if day is not None else None,
        )
        counts["tastings_added"] += 1

        actions.append({
            "bottle_id": bottle["id"],
            "from": f"{bottle['producer_name']} / {bottle['bottle_name']}",
            "to":   f"{new_producer_name} / {target_bottle_name}",
            "day":  day,
        })

    return {"counts": counts, "actions": actions}


def append_advent_xref(con, advent_producer_name: str, actions: list[dict]) -> None:
    """Append a Day → bottle-link list to the Whiskeys Advent producer note."""
    relevant = [a for a in actions if a["from"].startswith(advent_producer_name + " /")]
    if not relevant:
        return
    relevant.sort(key=lambda a: a["day"] or 0)
    lines = ["", "## Day-by-day", ""]
    for a in relevant:
        new_p, _, new_b = a["to"].partition(" / ")
        # Wikilink to the bottle's new vault path
        # The bottle's note path: Cellar/<Type>/<Producer>/<Bottle>.md — assume whiskey here.
        # We store just the basename target — Obsidian resolves [[Bottle]] but not uniquely
        # when there are duplicates; safer to use the full path.
        link = f"[[Cellar/Whiskey/{new_p}/{new_b}|{new_p} {new_b}]]"
        lines.append(f"- **Day {a['day']}** — {link}")
    cdb.append_producer_note(con, advent_producer_name, "\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Apply changes to cellar.db + vault notes (default: dry-run)")
    parser.add_argument("--cached", help="Path to a pre-parsed JSON file to skip the LLM call (debug)")
    args = parser.parse_args()

    con = cdb.connect()
    advent_bottles = collect_advent_bottles(con)
    if not advent_bottles:
        print(json.dumps({"note": "no Advent bottles to process"}))
        return 0

    names = [b["bottle_name"] for b in advent_bottles]

    if args.cached and Path(args.cached).exists():
        parses = json.loads(Path(args.cached).read_text())
        print(f"# parses loaded from cache: {args.cached}", file=sys.stderr)
    else:
        parses = parse_via_llm(names)
        # Cache to /tmp so we can re-apply without re-querying the model
        Path("/tmp/cellar_advent_parses.json").write_text(json.dumps(parses, indent=2))
        print("# parses cached at /tmp/cellar_advent_parses.json", file=sys.stderr)

    if len(parses) != len(advent_bottles):
        print(f"ERROR: model returned {len(parses)} parses but {len(advent_bottles)} bottles expected",
              file=sys.stderr)
        return 1

    # Build the proposal table
    proposed = []
    for b, p in zip(advent_bottles, parses):
        proposed.append({
            "id": b["id"],
            "calendar": b["producer_name"],
            "from_name": b["bottle_name"],
            "parsed_producer": p.get("producer"),
            "parsed_bottle": p.get("bottle"),
            "action": "KEEP" if (not p.get("producer") or p["producer"] == b["producer_name"])
                      else "MOVE",
        })

    if not args.apply:
        print(json.dumps({"proposed": proposed,
                          "summary": {
                              "total":   len(proposed),
                              "to_move": sum(1 for x in proposed if x["action"] == "MOVE"),
                              "keep":    sum(1 for x in proposed if x["action"] == "KEEP"),
                          }}, indent=2))
        return 0

    # APPLY mode
    with cdb.transaction(con):
        result = apply_changes(con, advent_bottles, parses)
        # Cross-reference list per Advent calendar (still in the same tx)
        advent_names = sorted({b["producer_name"] for b in advent_bottles})
        for advent in advent_names:
            append_advent_xref(con, advent, result["actions"])

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
