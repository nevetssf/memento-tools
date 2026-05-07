#!/usr/bin/env python3
"""cellar_migrate.py — one-shot migration: nested-YAML files → cellar.db.

Walks ~/obsidian-vault/Cellar/<Type>/<Producer>.md, parses each file's
YAML frontmatter (producer metadata + bottles[] list), and writes:
    - one row per producer in cellar.db
    - one row per bottle in cellar.db (under its producer)
    - one row per tasting in cellar.db (extracted from the YAML's
      palate/nose/finish/food_pairings/rating/date_tasted/etc. fields)
    - new vault notes at Cellar/Producer/<Producer>.md and
      Cellar/<Type>/<Producer>/<Bottle>.md, seeded with any
      pre-existing body prose from the original file

Originals are archived to Archive/Cellar-<timestamp>-pre-db/<...>.

Usage:
    cellar_migrate.py --dry-run                  # to scratch dir + scratch db, report only
    cellar_migrate.py                            # the real thing
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# YAML — pyyaml lives in the venv
sys.path.insert(0, str(Path(__file__).parent))

import yaml  # noqa: E402

import cellar as cdb  # noqa: E402
from config import VAULT_DIR  # noqa: E402

# ---------------------------------------------------------------------------
# Field classification — which YAML keys are bottle-shaped vs tasting-shaped
# ---------------------------------------------------------------------------

# Bottle-shaped fields go on bottles table.
BOTTLE_FIELD_MAP = {
    "expression":    "expression",
    "style":         "style",
    "varietal":      "varietal",
    "vintage":       "vintage",
    "age":           "age",
    "abv":           "abv",
    "cask":          "cask_type",
    "cask_type":     "cask_type",
    "botanicals":    "botanicals",
    "price":         "price",
    "quantity":      "quantity",
    "in_cellar":     None,  # special — drives status
    "would_buy_again": "would_buy_again",
    "acquired_date": "acquired_date",
    "best_serve":    None,  # gin-only flavor; fold into notes
    "base":          "style",  # vodka uses 'base' as style-ish
}

# Tasting-shaped fields move to a single tasting row per bottle.
TASTING_FIELD_MAP = {
    "rating":        "rating",
    "date_tasted":   "tasted_at",
    "tasted_at":     "tasted_at",
    "nose":          "nose",
    "palate":        "palate",
    "finish":        "finish",
    "color":         "color",
    "appearance":    "color",
    "with_water":    None,   # whiskey-specific; fold into notes
    "food_pairings": "food_pairings",
    "location":      "location",
}


# ---------------------------------------------------------------------------
# Frontmatter + body parsing
# ---------------------------------------------------------------------------

FM_RE = re.compile(r'^---\n(.*?)\n---\n?(.*)', re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"bad YAML frontmatter: {e}")
    return fm, m.group(2)


# Per-bottle body prose pattern: ## <bottle name>\n... up to the next ## or end.
SECTION_RE = re.compile(r'^## (?P<title>.+?)\n(?P<body>.*?)(?=\n## |\Z)',
                        re.MULTILINE | re.DOTALL)


def extract_per_bottle_prose(body: str) -> dict[str, str]:
    """Return {bottle_name: prose} where prose has 'Notes:' stripped and is non-trivial."""
    out: dict[str, str] = {}
    for m in SECTION_RE.finditer(body):
        title = m.group("title").strip()
        section_body = m.group("body").strip()
        # Strip leading "Notes:" prefix
        section_body = re.sub(r'^Notes:\s*', '', section_body, count=1)
        if section_body and section_body.strip() not in ("", "Notes:"):
            out[title] = section_body
    return out


def extract_producer_prose(body: str) -> str:
    """Lines BEFORE the first '## ' heading — typically the producer-level prose."""
    parts = body.split("\n## ", 1)
    head = parts[0]
    # Drop leading H1 if it's just the producer name
    head = re.sub(r'^#\s.+?\n', '', head, count=1)
    return head.strip()


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _coerce_bool(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"yes", "y", "true", "1"}:
            return 1
        if s in {"no", "n", "false", "0"}:
            return 0
    return None


def _coerce_str(v) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _coerce_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def _coerce_iso_date(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # Already ISO?
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        pass
    return None  # accept only ISO; non-ISO becomes None


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def derive_status(bottle_yaml: dict) -> str:
    """Map legacy in_cellar (yes/no) + quantity to a status enum value."""
    in_cellar = _coerce_bool(bottle_yaml.get("in_cellar"))
    qty = _coerce_int(bottle_yaml.get("quantity"))
    if in_cellar == 0:
        return "consumed"
    if qty == 0:
        return "consumed"
    return "in-cellar"


def split_bottle_into_tasting(bottle_yaml: dict) -> tuple[dict, dict, list[str]]:
    """Split a bottle YAML record into (bottle_fields, tasting_fields, leftover_into_notes).

    Returns:
      bottle_fields  — dict ready to pass to cellar.add_bottle (kwargs)
      tasting_fields — dict ready to pass to cellar.add_tasting (kwargs);
                       empty {} if no tasting-shaped fields were present
      leftover_lines — strings to fold into bottle.notes (e.g. "best_serve: ...")
    """
    bottle_fields: dict = {}
    tasting_fields: dict = {}
    leftover: list[str] = []

    for key, val in bottle_yaml.items():
        if key == "name":
            continue  # handled by caller
        if key in BOTTLE_FIELD_MAP:
            target = BOTTLE_FIELD_MAP[key]
            if target is None:
                # Special handling for in_cellar/best_serve etc.
                if key == "best_serve" and val:
                    leftover.append(f"best_serve: {val}")
                continue
            if target == "vintage":
                bottle_fields[target] = _coerce_int(val)
            elif target == "quantity":
                qi = _coerce_int(val)
                if qi is not None:
                    bottle_fields[target] = qi
            elif target == "would_buy_again":
                wb = _coerce_bool(val)
                if wb is not None:
                    bottle_fields[target] = wb
            elif target == "acquired_date":
                bottle_fields[target] = _coerce_iso_date(val)
            else:
                bottle_fields[target] = _coerce_str(val)
            continue
        if key in TASTING_FIELD_MAP:
            target = TASTING_FIELD_MAP[key]
            if target is None:
                if key == "with_water" and val:
                    leftover.append(f"with water: {val}")
                continue
            if target == "rating":
                ri = _coerce_int(val)
                if ri is not None:
                    tasting_fields[target] = ri
            elif target == "tasted_at":
                d = _coerce_iso_date(val)
                if d:
                    tasting_fields[target] = d
            else:
                cs = _coerce_str(val)
                if cs:
                    tasting_fields[target] = cs
            continue
        # Unknown key — fold into bottle.notes for safety
        if val is not None:
            leftover.append(f"{key}: {val}")

    bottle_fields["status"] = derive_status(bottle_yaml)
    return bottle_fields, tasting_fields, leftover


def migrate(con, *, vault_root: Path, dry_run: bool) -> dict:
    """Walk Cellar/, populate the DB, write new vault notes. Returns summary stats."""
    cellar_root = vault_root / "Cellar"
    files = sorted(p for p in cellar_root.glob("*/*.md") if p.is_file())

    archive_root = vault_root / f"Archive/Cellar-{datetime.now():%Y%m%d-%H%M%S}-pre-db"

    counts = {
        "files": 0, "producers": 0, "bottles": 0, "tastings": 0,
        "producer_notes": 0, "bottle_notes": 0,
        "skipped_empty": 0, "errors": 0,
    }
    errors: list[str] = []

    with cdb.transaction(con):
        for path in files:
            counts["files"] += 1
            rel = path.relative_to(vault_root)
            try:
                fm, body = split_frontmatter(path.read_text())
            except ValueError as e:
                errors.append(f"{rel}: {e}")
                counts["errors"] += 1
                continue

            producer_name = _coerce_str(fm.get("producer"))
            bottle_type = _coerce_str(fm.get("type"))
            if not producer_name:
                # Some "Champagne Notes.md" style files have no producer; fall back to filename
                producer_name = path.stem
            if not bottle_type:
                # Fall back to parent directory name (e.g. "Wine")
                bottle_type = path.parent.name.lower()

            producer_id = cdb.add_producer(
                con, producer_name,
                region=_coerce_str(fm.get("region")),
                country=_coerce_str(fm.get("country")),
                website=_coerce_str(fm.get("website")),
            )
            counts["producers"] += 1 if cdb.get_producer(con, producer_id)[
                "created_at"] == cdb.get_producer(con, producer_id)["updated_at"] else 0

            # Producer-level prose → producer note
            producer_prose = extract_producer_prose(body)
            per_bottle_prose = extract_per_bottle_prose(body)

            if producer_prose:
                cdb.append_producer_note(con, producer_id, producer_prose)
                counts["producer_notes"] += 1

            for bottle_yaml in fm.get("bottles") or []:
                if not isinstance(bottle_yaml, dict):
                    continue
                bname = _coerce_str(bottle_yaml.get("name"))
                if not bname:
                    counts["skipped_empty"] += 1
                    continue

                bottle_fields, tasting_fields, leftover = split_bottle_into_tasting(bottle_yaml)
                if leftover:
                    notes_blob = "\n".join(leftover)
                    bottle_fields["notes"] = (
                        (bottle_fields.get("notes") or "") + "\n" + notes_blob
                    ).strip()

                try:
                    bid = cdb.add_bottle(con, producer_id, bname, bottle_type, **bottle_fields)
                except Exception as e:
                    errors.append(f"{rel} → bottle {bname!r}: {e}")
                    counts["errors"] += 1
                    continue
                counts["bottles"] += 1

                # Per-bottle prose → bottle note
                prose = per_bottle_prose.get(bname) or ""
                # Also try matching by expression if name didn't match
                if not prose and bottle_yaml.get("expression"):
                    prose = per_bottle_prose.get(_coerce_str(bottle_yaml["expression"]) or "") or ""
                if prose:
                    cdb.append_bottle_note(con, bid, prose)
                    counts["bottle_notes"] += 1

                if tasting_fields:
                    cdb.add_tasting(con, bid, **tasting_fields)
                    counts["tastings"] += 1

            # Archive original (only on real run)
            if not dry_run:
                dest = archive_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(dest))

    return {
        "counts": counts,
        "errors": errors,
        "archive_root": str(archive_root) if not dry_run else None,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Migrate to a scratch DB and scratch vault copy; print results only")
    parser.add_argument("--scratch", default="/tmp/cellar-migrate-scratch",
                        help="Scratch root for --dry-run (default: /tmp/cellar-migrate-scratch)")
    args = parser.parse_args()

    if args.dry_run:
        scratch = Path(args.scratch)
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True)

        # Copy real Cellar tree into scratch so the migration's "archive originals"
        # step (which we skip in dry-run) doesn't matter, and so vault-note writes
        # land in scratch instead of polluting the real vault.
        scratch_vault = scratch / "vault"
        (scratch_vault / "Cellar").mkdir(parents=True)
        # Cellar tree
        for p in (Path(VAULT_DIR) / "Cellar").iterdir():
            if p.is_dir():
                shutil.copytree(p, scratch_vault / "Cellar" / p.name)

        scratch_db = scratch / "cellar.db"
        # We need cellar.py's helpers to operate on scratch_vault and scratch_db.
        # Monkey-patch the module-level VAULT_DIR + DB_PATH for this run.
        cdb.VAULT_DIR = str(scratch_vault)
        cdb.DB_PATH = scratch_db
        cdb.CELLAR_ROOT = scratch_vault / "Cellar"

        con = cdb.connect(scratch_db)
        result = migrate(con, vault_root=scratch_vault, dry_run=True)
        con.close()

        result["scratch"] = str(scratch)
        print(json.dumps(result, indent=2))
        return 0

    # Real run
    con = cdb.connect()
    result = migrate(con, vault_root=Path(VAULT_DIR), dry_run=False)
    con.close()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
