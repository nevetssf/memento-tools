#!/usr/bin/env python3
"""
people.py — People & relationship manager for people.db

Usage: python3 people.py <command> [args]

Commands:
  find <name>               Fuzzy search by name
  show <name|id>            Full details + relationships
  add-person <name>         Add a new person
  update-person <name|id>   Update person fields
  delete-person <name|id>   Hard-delete person + relationships
  relatives <name|id>       List relationships (optional --type filter)
  relate                    Add a relationship
  update-relationship       Change relationship type between two people
  delete-relationship       Remove relationship between two people
  between                   List all relationships between two specific people
  check                     Integrity report
  repair                    Auto-fix safe issues, flag the rest
  rebuild-inferred [name]   Clear and regenerate inferred family relationships
  graph [name]              Generate Mermaid relationship graph (markdown)
  links [name]              Update Obsidian notes with wiki-linked relationships
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).parent))
from config import DB_PATH as _DB_PATH, VAULT_DIR as _VAULT_DIR, PEOPLE_DIR as _PEOPLE_DIR

DB_PATH = str(_DB_PATH)
VAULT_PATH = str(_VAULT_DIR)
PEOPLE_DIR = str(_PEOPLE_DIR)

# Explicit relationship types (stored in DB). Family relationships beyond 'parent'
# are inferred from the parent/child graph — see Relationship Inference Engine below.
VALID_TYPES = {
    "spouse", "parent", "friend", "acquaintance",
    "coworker", "manager", "report", "neighbor",
    "boyfriend", "girlfriend",
    "ex-boyfriend", "ex-girlfriend", "ex-spouse",
    "accountant", "financial advisor", "client", "pet", "owner",
    "godfather", "godmother", "godson", "goddaughter",
    "courtesy-cousin",
    "courtesy-aunt", "courtesy-uncle",
    "courtesy-niece", "courtesy-nephew",
    "artist", "celebrity",
}

# Family types that are inferred, not stored explicitly
INFERRED_FAMILY_TYPES = {
    "child", "brother", "sister", "sibling", "cousin",
    "uncle", "aunt", "nephew", "niece",
    "grandfather", "grandmother", "grandparent",
    "grandson", "granddaughter", "grandchild",
    "father", "mother", "son", "daughter",
}

# Types where having the same type in both directions is valid (symmetric)
SYMMETRIC_TYPES = {
    "spouse", "ex-spouse", "friend", "acquaintance", "coworker", "neighbor", "courtesy-cousin",
    "artist", "celebrity",
    # Romantic types are symmetric for same-sex couples (get_reciprocal returns same type for same gender)
    "boyfriend", "girlfriend", "ex-boyfriend", "ex-girlfriend",
}

RECIPROCALS = {
    "spouse": "spouse", "ex-spouse": "ex-spouse", "friend": "friend", "acquaintance": "acquaintance", "coworker": "coworker",
    "neighbor": "neighbor",
    # godparent reciprocals handled by get_reciprocal() (gender-dependent)
    "manager": "report", "report": "manager",
    "accountant": "client", "client": "accountant",
    "financial advisor": "client",
    "pet": "owner", "owner": "pet",
    "courtesy-cousin": "courtesy-cousin",
    "artist": "artist", "celebrity": "celebrity",
    # gender-dependent types are handled by get_reciprocal()
}


def get_reciprocal(con, person_id, rel_type):
    """Return the reciprocal type for rel_type, using person_id's gender for gender-dependent types."""
    gender_dependent = {
        "boyfriend":    ("M", "boyfriend",    "girlfriend"),
        "girlfriend":   ("M", "boyfriend",    "girlfriend"),
        "ex-boyfriend": ("M", "ex-boyfriend", "ex-girlfriend"),
        "ex-girlfriend":("M", "ex-boyfriend", "ex-girlfriend"),
        "godfather":    ("M", "godson",       "goddaughter"),
        "godmother":    ("M", "godson",       "goddaughter"),
        "godson":            ("M", "godfather",      "godmother"),
        "goddaughter":       ("M", "godfather",      "godmother"),
        "courtesy-aunt":     ("M", "courtesy-nephew", "courtesy-niece"),
        "courtesy-uncle":    ("M", "courtesy-nephew", "courtesy-niece"),
        "courtesy-niece":    ("M", "courtesy-uncle",  "courtesy-aunt"),
        "courtesy-nephew":   ("M", "courtesy-uncle",  "courtesy-aunt"),
    }
    if rel_type in gender_dependent:
        male_gender, male_recip, female_recip = gender_dependent[rel_type]
        row = con.execute("SELECT gender FROM people WHERE id=?", (person_id,)).fetchone()
        gender = row["gender"] if row else None
        return male_recip if gender == male_gender else female_recip
    return RECIPROCALS.get(rel_type)


# Gender-aware type aliases for relatives --type filter
TYPE_ALIASES = {
    "husband":      ("spouse", "M"),
    "wife":         ("spouse", "F"),
    "ex-husband":   ("ex-spouse", "M"),
    "ex-wife":      ("ex-spouse", "F"),
}

# Mapping from user-friendly filter terms to patterns that match inferred relationship names.
# Each key maps to a list of substrings — if any substring appears in the inferred type, it matches.
# Optional gender filter as second element.
INFERRED_TYPE_FILTERS = {
    "child":        (["son", "daughter", "child"], None),
    "children":     (["son", "daughter", "child"], None),
    "son":          (["son", "grandson", "child"], "M"),
    "daughter":     (["daughter", "granddaughter", "child"], "F"),
    "sibling":      (["brother", "sister", "sibling"], None),
    "brother":      (["brother", "sibling"], "M"),
    "sister":       (["sister", "sibling"], "F"),
    "cousin":       (["cousin"], None),
    "uncle":        (["uncle"], None),
    "aunt":         (["aunt"], None),
    "nephew":       (["nephew"], None),
    "niece":        (["niece"], None),
    "grandparent":  (["grandfather", "grandmother", "grandparent"], None),
    "grandfather":  (["grandfather", "grandparent"], None),
    "grandmother":  (["grandmother", "grandparent"], None),
    "grandchild":   (["grandson", "granddaughter", "grandchild"], None),
    "grandson":     (["grandson", "grandchild"], None),
    "granddaughter":(["granddaughter", "grandchild"], None),
    "father":       (["father"], None),
    "mother":       (["mother"], None),
    "parent":       (["father", "mother", "parent"], None),
    "parents":      (["father", "mother", "parent"], None),
}


def connect(db_path=None):
    con = sqlite3.connect(db_path or DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def out(data, pretty=False):
    if pretty:
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    for k, v in item.items():
                        if v is not None:
                            print(f"  {k}: {v}")
                    print()
                else:
                    print(f"  {item}")
        elif isinstance(data, dict):
            for k, v in data.items():
                if v is not None:
                    print(f"{k}: {v}")
        else:
            print(data)
    else:
        print(json.dumps(data, default=str))


def err(msg, code=1):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(code)


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def resolve_person(con, name_or_id, label="person"):
    """Resolve name (fuzzy) or numeric ID to a single person row. Exits on ambiguity."""
    if str(name_or_id).isdigit():
        row = con.execute("SELECT * FROM people WHERE id=?", (int(name_or_id),)).fetchone()
        if not row:
            err(f"{label} id {name_or_id} not found")
        return row
    rows = con.execute(
        "SELECT * FROM people WHERE name LIKE ? COLLATE NOCASE ORDER BY name",
        (f"%{name_or_id}%",)
    ).fetchall()
    if not rows:
        err(f"No {label} found matching '{name_or_id}'")
    if len(rows) > 1:
        matches = [{"id": r["id"], "name": r["name"]} for r in rows]
        err(f"Ambiguous {label} '{name_or_id}' — matches: {json.dumps(matches)}")
    return rows[0]


def person_to_dict(row):
    return {k: row[k] for k in row.keys() if row[k] is not None}


# ---------------------------------------------------------------------------
# Relationship Inference Engine
# ---------------------------------------------------------------------------

def ensure_inferred_column(con):
    """Add 'inferred' column to relationships table if it doesn't exist."""
    cols = [row[1] for row in con.execute("PRAGMA table_info(relationships)").fetchall()]
    if "inferred" not in cols:
        con.execute("ALTER TABLE relationships ADD COLUMN inferred BOOLEAN NOT NULL DEFAULT 0")
        con.commit()


def build_parent_graph(con):
    """Build a dict mapping person_id -> set of parent_ids (from 'parent' relationships)."""
    rows = con.execute(
        "SELECT person_id, relative_id FROM relationships WHERE relative_is = 'parent'"
    ).fetchall()
    graph = {}
    for r in rows:
        graph.setdefault(r["person_id"], set()).add(r["relative_id"])
    return graph


def find_ancestors(person_id, parent_graph):
    """BFS upward through parent graph. Returns dict {ancestor_id: generation} (gen 0 = self)."""
    ancestors = {person_id: 0}
    queue = deque([(person_id, 0)])
    while queue:
        pid, gen = queue.popleft()
        for parent_id in parent_graph.get(pid, set()):
            if parent_id not in ancestors:
                ancestors[parent_id] = gen + 1
                queue.append((parent_id, gen + 1))
    return ancestors


def find_common_ancestors(ancestors_a, ancestors_b):
    """Find common ancestors and return list of (ancestor_id, gen_a, gen_b)."""
    common = set(ancestors_a.keys()) & set(ancestors_b.keys())
    results = []
    for aid in common:
        gen_a = ancestors_a[aid]
        gen_b = ancestors_b[aid]
        if gen_a > 0 and gen_b > 0:  # exclude self
            results.append((aid, gen_a, gen_b))
    return results


def name_relationship(gen_a, gen_b, gender=None):
    """
    Name a relationship given generations from person A and B to their common ancestor.
    Uses standard genealogy formula:
      - Direct line (one gen is 0): handled separately
      - gen_a == gen_b == 1: sibling
      - gen_a == 1, gen_b > 1: uncle/aunt (B is A's parent's sibling's descendant line)
      - gen_a > 1, gen_b == 1: nephew/niece
      - Otherwise: cousin_degree = min(gen_a, gen_b) - 1, removed = abs(gen_a - gen_b)
    """
    if gen_a == gen_b == 1:
        # Siblings
        if gender == "M":
            return "brother"
        elif gender == "F":
            return "sister"
        return "sibling"

    if gen_a == 1 and gen_b == 2:
        # B is A's parent's child's child... no — B's parent is A's parent → B is A's sibling's child
        # Actually: gen_a=1 means A is 1 gen from common ancestor (A's parent)
        # gen_b=2 means B is 2 gens from that ancestor (grandchild of ancestor = child of A's sibling)
        # So B is A's nephew/niece
        if gender == "M":
            return "nephew"
        elif gender == "F":
            return "niece"
        return "nephew/niece"

    if gen_a == 2 and gen_b == 1:
        # B is A's uncle/aunt
        if gender == "M":
            return "uncle"
        elif gender == "F":
            return "aunt"
        return "uncle/aunt"

    # General uncle/aunt and nephew/niece for deeper generations
    if gen_a == 1 and gen_b > 2:
        # B is gen_b-1 generations below A's sibling
        greats = gen_b - 2
        prefix = "great-" * greats
        if gender == "M":
            return f"{prefix}nephew"
        elif gender == "F":
            return f"{prefix}niece"
        return f"{prefix}nephew/niece"

    if gen_b == 1 and gen_a > 2:
        # B is gen_a-1 generations above A through sibling line = great-uncle/aunt
        greats = gen_a - 2
        prefix = "great-" * greats
        if gender == "M":
            return f"{prefix}uncle"
        elif gender == "F":
            return f"{prefix}aunt"
        return f"{prefix}uncle/aunt"

    # Direct ancestor/descendant lines
    if gen_a == 0 or gen_b == 0:
        return None  # handled by find_direct_line

    # Cousin formula: degree = min(gen_a, gen_b) - 1, removed = abs(gen_a - gen_b)
    degree = min(gen_a, gen_b) - 1
    removed = abs(gen_a - gen_b)

    if degree == 0:
        return None  # shouldn't happen given guards above

    ordinals = {1: "1st", 2: "2nd", 3: "3rd"}
    degree_str = ordinals.get(degree, f"{degree}th")

    if removed == 0:
        return f"{degree_str} cousin"
    else:
        times = "once" if removed == 1 else "twice" if removed == 2 else f"{removed}x"
        return f"{degree_str} cousin {times} removed"


def find_direct_line(person_id, target_id, parent_graph):
    """
    Check if target is a direct ancestor or descendant of person.
    Returns (direction, generations) or None.
    direction: "ancestor" means target is person's ancestor, "descendant" means target is person's descendant.
    """
    # Check if target is an ancestor of person
    ancestors = find_ancestors(person_id, parent_graph)
    if target_id in ancestors and ancestors[target_id] > 0:
        return ("ancestor", ancestors[target_id])

    # Check if target is a descendant (person is target's ancestor)
    target_ancestors = find_ancestors(target_id, parent_graph)
    if person_id in target_ancestors and target_ancestors[person_id] > 0:
        return ("descendant", target_ancestors[person_id])

    return None


def name_direct_line(direction, generations, gender=None):
    """Name a direct-line relationship (grandparent, great-grandchild, etc.)."""
    if direction == "ancestor":
        if generations == 1:
            if gender == "M":
                return "father"
            elif gender == "F":
                return "mother"
            return "parent"
        elif generations == 2:
            if gender == "M":
                return "grandfather"
            elif gender == "F":
                return "grandmother"
            return "grandparent"
        else:
            greats = generations - 2
            prefix = "great-" * greats
            if gender == "M":
                return f"{prefix}grandfather"
            elif gender == "F":
                return f"{prefix}grandmother"
            return f"{prefix}grandparent"
    else:  # descendant
        if generations == 1:
            if gender == "M":
                return "son"
            elif gender == "F":
                return "daughter"
            return "child"
        elif generations == 2:
            if gender == "M":
                return "grandson"
            elif gender == "F":
                return "granddaughter"
            return "grandchild"
        else:
            greats = generations - 2
            prefix = "great-" * greats
            if gender == "M":
                return f"{prefix}grandson"
            elif gender == "F":
                return f"{prefix}granddaughter"
            return f"{prefix}grandchild"


def infer_relationship(con, person_id, target_id, parent_graph=None):
    """
    Infer the relationship between person and target by traversing the parent/child graph.
    Returns a relationship name string, or None if no connection found.
    The name describes what target IS to person (e.g. "1st cousin" means target is person's 1st cousin).
    """
    if parent_graph is None:
        parent_graph = build_parent_graph(con)

    # Check direct line first
    direct = find_direct_line(person_id, target_id, parent_graph)
    if direct:
        target_gender = con.execute("SELECT gender FROM people WHERE id=?", (target_id,)).fetchone()
        gender = target_gender["gender"] if target_gender else None
        return name_direct_line(direct[0], direct[1], gender)

    # Find common ancestors
    ancestors_a = find_ancestors(person_id, parent_graph)
    ancestors_b = find_ancestors(target_id, parent_graph)
    common = find_common_ancestors(ancestors_a, ancestors_b)

    if not common:
        return None

    # Pick the closest common ancestor pair (minimize total generations)
    common.sort(key=lambda x: x[1] + x[2])
    _, gen_a, gen_b = common[0]

    target_gender = con.execute("SELECT gender FROM people WHERE id=?", (target_id,)).fetchone()
    gender = target_gender["gender"] if target_gender else None

    return name_relationship(gen_a, gen_b, gender)


def infer_all_relatives(con, person_id, parent_graph=None):
    """
    Find all people connected to person through the parent/child graph
    and infer their relationship names.
    Returns list of dicts: [{id, name, gender, inferred_type, gen_a, gen_b}, ...]
    """
    if parent_graph is None:
        parent_graph = build_parent_graph(con)

    # Build a bidirectional graph (parent + child edges) for BFS
    bidi_graph = {}
    for pid, parents in parent_graph.items():
        for parent_id in parents:
            bidi_graph.setdefault(pid, set()).add(parent_id)
            bidi_graph.setdefault(parent_id, set()).add(pid)

    # BFS to find all reachable people
    visited = {person_id}
    queue = deque([person_id])
    while queue:
        current = queue.popleft()
        for neighbor in bidi_graph.get(current, set()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    visited.discard(person_id)
    if not visited:
        return []

    # For each reachable person, infer the relationship
    ancestors_a = find_ancestors(person_id, parent_graph)
    results = []
    for target_id in visited:
        rel_name = infer_relationship(con, person_id, target_id, parent_graph)
        if rel_name:
            row = con.execute("SELECT id, name, gender, pronouns FROM people WHERE id=?", (target_id,)).fetchone()
            if row:
                results.append({
                    "relative_is": rel_name,
                    "id": row["id"],
                    "name": row["name"],
                    "gender": row["gender"],
                    "pronouns": row["pronouns"],
                    "inferred": True
                })

    results.sort(key=lambda r: (r["relative_is"], r["name"]))
    return results


def cache_inferred_relationships(con, person_id, inferred_rels):
    """
    Write inferred relationships to the DB (inferred=1).
    Clears old inferred rows for this person first.
    Skips relationships that already exist as explicit (inferred=0).
    """
    ensure_inferred_column(con)

    # Clear old inferred rows for this person
    con.execute(
        "DELETE FROM relationships WHERE person_id=? AND inferred=1",
        (person_id,)
    )

    for rel in inferred_rels:
        # Skip if an explicit relationship already exists
        existing = con.execute(
            "SELECT id, inferred FROM relationships WHERE person_id=? AND relative_id=? AND relative_is=?",
            (person_id, rel["id"], rel["relative_is"])
        ).fetchone()
        if existing:
            continue

        # Also skip if there's any explicit relationship between these two people
        # (the explicit one takes precedence — e.g. explicit "cousin" beats inferred "1st cousin")
        explicit = con.execute(
            "SELECT id FROM relationships WHERE person_id=? AND relative_id=? AND inferred=0",
            (person_id, rel["id"])
        ).fetchone()
        if explicit:
            continue

        try:
            # Disable the auto-reciprocal trigger by inserting directly
            # We'll handle the reciprocal ourselves
            con.execute(
                "INSERT OR IGNORE INTO relationships (person_id, relative_id, relative_is, inferred, created_at) "
                "VALUES (?,?,?,1,?)",
                (person_id, rel["id"], rel["relative_is"], now_iso())
            )
        except sqlite3.IntegrityError:
            pass

    con.commit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_find(args):
    con = connect(args.db)
    rows = con.execute(
        "SELECT id, name, gender, pronouns, birthdate, location, profession, notes "
        "FROM people WHERE name LIKE ? COLLATE NOCASE ORDER BY name",
        (f"%{args.name}%",)
    ).fetchall()
    if not rows:
        err(f"No people found matching '{args.name}'")
    out([person_to_dict(r) for r in rows], args.pretty)


def cmd_show(args):
    con = connect(args.db)
    person = resolve_person(con, args.name_or_id)
    result = person_to_dict(person)
    rels = con.execute(
        "SELECT r.relative_is, r.relative_qualifier, p.id, p.name, p.gender, p.pronouns "
        "FROM relationships r JOIN people p ON p.id=r.relative_id "
        "WHERE r.person_id=? ORDER BY r.relative_is, p.name",
        (person["id"],)
    ).fetchall()
    result["relationships"] = [dict(r) for r in rels]
    out(result, args.pretty)


PERSON_FIELDS = [
    "name", "gender", "pronouns", "birthdate", "deathdate", "age", "location",
    "profession", "business", "phone", "address", "aliases", "biography",
    "notes", "health", "sport", "highschool", "university", "linkedin",
    "github", "personal_website", "professional_site", "ieee", "acm_profile",
    "researchgate", "patents", "background_profile", "reputation_profile",
    "obsidian_file", "favorite_color", "interests", "desired_university",
]


def add_person_args(parser):
    for field in PERSON_FIELDS:
        parser.add_argument(f"--{field.replace('_', '-')}")


def cmd_add_person(args):
    con = connect(args.db)
    existing = con.execute(
        "SELECT id FROM people WHERE name=?", (args.name,)
    ).fetchone()
    if existing:
        err(f"Person '{args.name}' already exists with id {existing['id']}")
    now = now_iso()
    fields = {"name": args.name, "date_created": now, "date_updated": now}
    for field in PERSON_FIELDS:
        val = getattr(args, field, None)
        if val is not None:
            fields[field] = val
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    con.execute(f"INSERT INTO people ({cols}) VALUES ({placeholders})", list(fields.values()))
    con.commit()
    person = con.execute("SELECT * FROM people WHERE name=?", (args.name,)).fetchone()
    out({"ok": True, "id": person["id"], "name": person["name"]}, args.pretty)


def cmd_update_person(args):
    con = connect(args.db)
    person = resolve_person(con, args.name_or_id)
    updates = {"date_updated": now_iso()}
    for field in PERSON_FIELDS:
        val = getattr(args, field, None)
        if val is not None:
            updates[field] = val
    if len(updates) == 1:
        err("No fields to update — specify at least one field flag")
    set_clause = ", ".join(f"{k}=?" for k in updates)
    con.execute(
        f"UPDATE people SET {set_clause} WHERE id=?",
        list(updates.values()) + [person["id"]]
    )
    con.commit()
    out({"ok": True, "id": person["id"], "name": person["name"], "updated": list(updates.keys())}, args.pretty)


def cmd_delete_person(args):
    con = connect(args.db)
    person = resolve_person(con, args.name_or_id)
    pid = person["id"]
    rel_count = con.execute(
        "SELECT COUNT(*) FROM relationships WHERE person_id=? OR relative_id=?", (pid, pid)
    ).fetchone()[0]
    if not args.force:
        out({
            "dry_run": True,
            "would_delete": {"person": person["name"], "id": pid, "relationships": rel_count},
            "hint": "Pass --force to execute"
        }, args.pretty)
        return
    con.execute("DELETE FROM relationships WHERE person_id=? OR relative_id=?", (pid, pid))
    con.execute("DELETE FROM people WHERE id=?", (pid,))
    con.commit()
    out({"ok": True, "deleted": person["name"], "id": pid, "relationships_removed": rel_count}, args.pretty)


def _match_inferred_type(rel_type, filter_key, gender_filter):
    """Check if an inferred relationship type matches a filter key."""
    rel_lower = rel_type.lower()

    # Check INFERRED_TYPE_FILTERS for smart matching
    if filter_key in INFERRED_TYPE_FILTERS:
        patterns, _ = INFERRED_TYPE_FILTERS[filter_key]
        return any(p in rel_lower for p in patterns)

    # Fallback: substring match (e.g. "1st cousin" contains "cousin")
    return filter_key in rel_lower or rel_lower == filter_key


def cmd_relatives(args):
    con = connect(args.db)
    person = resolve_person(con, args.name_or_id)

    # Parse type filter
    requested_type = args.type.lower() if args.type else None
    gender_filter = None
    db_type_filter = None  # for querying explicit DB rows

    inferred_only = False  # True when the filter targets only inferred types
    if requested_type:
        if requested_type in TYPE_ALIASES:
            db_type, gender_filter = TYPE_ALIASES[requested_type]
            db_type_filter = (db_type,) if isinstance(db_type, str) else db_type
        elif requested_type in INFERRED_TYPE_FILTERS:
            _, gender_filter = INFERRED_TYPE_FILTERS[requested_type]
            # "parent" is stored explicitly; other family types are inferred-only
            if requested_type in ("parent", "parents", "father", "mother"):
                db_type_filter = ("parent",)
            else:
                inferred_only = True
        else:
            db_type_filter = (requested_type,)

    # Check if inference is requested (default: true)
    do_infer = getattr(args, "infer", True)

    if do_infer:
        ensure_inferred_column(con)

    # Query explicit relationships from DB (skip if filter targets inferred-only types)
    results = []
    if not inferred_only:
        query = (
            "SELECT r.relative_is, r.relative_qualifier, p.id, p.name, p.gender, p.pronouns "
            "FROM relationships r JOIN people p ON p.id=r.relative_id "
            "WHERE r.person_id=?"
        )
        params = [person["id"]]

        if not do_infer:
            query += " AND (r.inferred IS NULL OR r.inferred=0)"

        if db_type_filter:
            placeholders = ",".join("?" * len(db_type_filter))
            query += f" AND r.relative_is IN ({placeholders})"
            params.extend(db_type_filter)

        if gender_filter:
            query += " AND p.gender=?"
            params.append(gender_filter)

        query += " ORDER BY r.relative_is, p.name"
        rows = con.execute(query, params).fetchall()
        results = [dict(r) for r in rows]

    # Supplement with inferred relationships
    if do_infer:
        parent_graph = build_parent_graph(con)
        inferred = infer_all_relatives(con, person["id"], parent_graph)

        # Cache them
        cache_inferred_relationships(con, person["id"], inferred)

        # Filter inferred results by type if needed
        if requested_type:
            filtered = []
            for rel in inferred:
                if not _match_inferred_type(rel["relative_is"], requested_type, gender_filter):
                    continue
                if gender_filter and rel.get("gender") != gender_filter:
                    continue
                filtered.append(rel)
            inferred = filtered

        # Merge: avoid duplicates (prefer explicit over inferred)
        existing_ids = {r["id"] for r in results}
        for rel in inferred:
            if rel["id"] not in existing_ids:
                results.append(rel)

        results.sort(key=lambda r: (r["relative_is"], r.get("name", "")))

    out(results, args.pretty)


def cmd_relate(args):
    con = connect(args.db)
    person = resolve_person(con, args.person, "person")
    relative = resolve_person(con, args.relative, "relative")
    rel_type = args.relative_is.lower()
    if rel_type in INFERRED_FAMILY_TYPES:
        err(f"'{rel_type}' is inferred from the family tree — use 'parent' to define family links")
    if rel_type not in VALID_TYPES:
        err(f"Unknown relationship type '{rel_type}'. Valid: {', '.join(sorted(VALID_TYPES))}")
    qualifier = getattr(args, "qualifier", None)
    try:
        con.execute(
            "INSERT INTO relationships (person_id, relative_id, relative_is, notes, relative_qualifier, created_at) VALUES (?,?,?,?,?,?)",
            (person["id"], relative["id"], rel_type, args.notes, qualifier, now_iso())
        )
        con.commit()
    except sqlite3.IntegrityError:
        err(f"Relationship already exists: {person['name']} → {relative['name']} ({rel_type})")
    result = {
        "ok": True,
        "person": person["name"],
        "relative": relative["name"],
        "relative_is": rel_type,
        "note": "Reciprocal created automatically by trigger"
    }
    if qualifier:
        result["qualifier"] = qualifier
    out(result, args.pretty)


def cmd_update_relationship(args):
    con = connect(args.db)
    person = resolve_person(con, args.person, "person")
    relative = resolve_person(con, args.relative, "relative")
    from_type = args.from_type.lower()
    to_type = args.to_type.lower()
    if to_type not in VALID_TYPES:
        err(f"Unknown relationship type '{to_type}'. Valid: {', '.join(sorted(VALID_TYPES))}")
    # Check the existing relationship exists
    existing = con.execute(
        "SELECT id FROM relationships WHERE person_id=? AND relative_id=? AND relative_is=?",
        (person["id"], relative["id"], from_type)
    ).fetchone()
    if not existing:
        err(f"No '{from_type}' relationship found between {person['name']} and {relative['name']}")
    # Delete both directions of old relationship, re-insert new one (trigger handles reciprocal)
    con.execute(
        "DELETE FROM relationships WHERE (person_id=? AND relative_id=?) OR (person_id=? AND relative_id=?)",
        (person["id"], relative["id"], relative["id"], person["id"])
    )
    con.execute(
        "INSERT INTO relationships (person_id, relative_id, relative_is) VALUES (?,?,?)",
        (person["id"], relative["id"], to_type)
    )
    con.commit()
    out({
        "ok": True,
        "person": person["name"],
        "relative": relative["name"],
        "from": from_type,
        "to": to_type,
        "note": "Reciprocal updated automatically by trigger"
    }, args.pretty)


def cmd_delete_relationship(args):
    con = connect(args.db)
    person = resolve_person(con, args.person, "person")
    relative = resolve_person(con, args.relative, "relative")
    pid, rid = person["id"], relative["id"]

    if args.type:
        rel_type = args.type.lower()
        # Resolve gender-aware aliases
        if rel_type in TYPE_ALIASES:
            resolved = TYPE_ALIASES[rel_type][0]
            rel_type = resolved if isinstance(resolved, str) else resolved[0]
        if rel_type not in VALID_TYPES:
            err(f"Unknown relationship type: {args.type!r}")
        reciprocal = get_reciprocal(con, pid, rel_type)
        # Delete A→B direction and its reciprocal B→A
        pairs = [(pid, rid, rel_type)]
        if reciprocal and reciprocal != rel_type:
            pairs.append((rid, pid, reciprocal))
        else:
            pairs.append((rid, pid, rel_type))
        count = 0
        for p, r, t in pairs:
            count += con.execute(
                "DELETE FROM relationships WHERE person_id=? AND relative_id=? AND relative_is=?",
                (p, r, t)
            ).rowcount
        if count == 0:
            err(f"No '{rel_type}' relationship found between {person['name']} and {relative['name']}")
    else:
        # No --type: list what exists so agent/user can pick
        rows = con.execute(
            "SELECT relative_is FROM relationships WHERE (person_id=? AND relative_id=?) OR (person_id=? AND relative_id=?)",
            (pid, rid, rid, pid)
        ).fetchall()
        if not rows:
            err(f"No relationship found between {person['name']} and {relative['name']}")
        types = sorted({r["relative_is"] for r in rows})
        if len(types) == 1:
            # Only one type — delete it
            rel_type = types[0]
            count = con.execute(
                "DELETE FROM relationships WHERE (person_id=? AND relative_id=?) OR (person_id=? AND relative_id=?)",
                (pid, rid, rid, pid)
            ).rowcount
        else:
            err(
                f"Multiple relationships exist between {person['name']} and {relative['name']}: "
                f"{types}. Use --type to specify which one to delete."
            )

    con.commit()
    out({"ok": True, "person": person["name"], "relative": relative["name"],
         "rows_removed": count}, args.pretty)


def cmd_between(args):
    con = connect(args.db)
    a = resolve_person(con, args.person_a, "person_a")
    b = resolve_person(con, args.person_b, "person_b")
    rows = con.execute(
        "SELECT relative_is FROM relationships WHERE person_id=? AND relative_id=?",
        (a["id"], b["id"])
    ).fetchall()
    # Express from A's perspective only (avoid duplicate symmetric rows)
    types = [r["relative_is"] for r in rows]
    out({
        "person_a": {"id": a["id"], "name": a["name"]},
        "person_b": {"id": b["id"], "name": b["name"]},
        # reads as: "B is A's <type>"
        "b_is_a_s": types,
        "count": len(types),
    }, args.pretty)


def cmd_check(args):
    con = connect(args.db)
    errors = []
    warnings = []

    # 1. Missing reciprocals (skip inferred rows — they don't require manual reciprocals)
    rows = con.execute("""
        SELECT r.id, p1.name AS person, r.relative_is, p2.name AS relative
        FROM relationships r
        JOIN people p1 ON p1.id=r.person_id
        JOIN people p2 ON p2.id=r.relative_id
        WHERE r.inferred=0
          AND NOT EXISTS (
            SELECT 1 FROM relationships r2
            WHERE r2.person_id=r.relative_id AND r2.relative_id=r.person_id
        )
    """).fetchall()
    for r in rows:
        errors.append({
            "issue": "missing_reciprocal",
            "id": r["id"],
            "person": r["person"],
            "relative_is": r["relative_is"],
            "relative": r["relative"]
        })

    # 2. Conflicting directions: asymmetric type stored in both directions
    # e.g. A->B parent AND B->A parent (the classic bug)
    # Valid to have multiple *different* types between the same pair (friend + manager)
    # Skip inferred rows (computed from family graph) and symmetric types (both directions expected)
    sym_placeholders = ",".join(f"'{t}'" for t in SYMMETRIC_TYPES)
    rows = con.execute(f"""
        SELECT p1.name AS person, p2.name AS relative, r1.relative_is AS type
        FROM relationships r1
        JOIN relationships r2 ON r2.person_id=r1.relative_id AND r2.relative_id=r1.person_id
                              AND r2.relative_is=r1.relative_is
        JOIN people p1 ON p1.id=r1.person_id
        JOIN people p2 ON p2.id=r1.relative_id
        WHERE r1.person_id < r1.relative_id
          AND r1.inferred=0
          AND r1.relative_is NOT IN ({sym_placeholders})
    """).fetchall()
    for r in rows:
        errors.append({
            "issue": "conflicting_direction",
            "person": r["person"],
            "relative": r["relative"],
            "type": r["type"],
            "detail": f"Both sides have relative_is='{r['type']}' — one direction is wrong"
        })

    # 3. Unknown relationship types (skip inferred rows — they use computed family type names)
    placeholders = ",".join(f"'{t}'" for t in VALID_TYPES)
    rows = con.execute(
        f"SELECT id, person_id, relative_id, relative_is FROM relationships "
        f"WHERE relative_is NOT IN ({placeholders}) AND inferred=0"
    ).fetchall()
    for r in rows:
        errors.append({
            "issue": "unknown_type",
            "id": r["id"],
            "relative_is": r["relative_is"]
        })

    # 4. Orphaned references
    rows = con.execute("""
        SELECT r.id, 'person_id' AS col, r.person_id AS bad_id
        FROM relationships r WHERE NOT EXISTS (SELECT 1 FROM people WHERE id=r.person_id)
        UNION ALL
        SELECT r.id, 'relative_id', r.relative_id
        FROM relationships r WHERE NOT EXISTS (SELECT 1 FROM people WHERE id=r.relative_id)
    """).fetchall()
    for r in rows:
        errors.append({
            "issue": "orphaned_reference",
            "relationship_id": r["id"],
            "column": r["col"],
            "bad_id": r["bad_id"]
        })

    # 5. Self-relationships
    rows = con.execute(
        "SELECT r.id, p.name FROM relationships r JOIN people p ON p.id=r.person_id "
        "WHERE r.person_id=r.relative_id"
    ).fetchall()
    for r in rows:
        errors.append({"issue": "self_relationship", "id": r["id"], "name": r["name"]})

    # 6. Missing gender (warning)
    rows = con.execute(
        "SELECT id, name FROM people WHERE gender IS NULL OR gender NOT IN ('M','F','NB')"
    ).fetchall()
    for r in rows:
        warnings.append({"issue": "missing_or_unknown_gender", "id": r["id"], "name": r["name"]})

    result = {
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "errors": len(errors),
            "warnings": len(warnings),
            "ok": len(errors) == 0
        }
    }
    out(result, args.pretty)
    if errors:
        sys.exit(1)


def cmd_repair(args):
    con = connect(args.db)
    fixed = []
    flagged = []

    # Fix 1: Insert missing reciprocals
    missing = con.execute("""
        SELECT r.person_id, r.relative_id, r.relative_is, r.notes, r.created_at,
               p1.name AS person_name, p2.name AS relative_name
        FROM relationships r
        JOIN people p1 ON p1.id=r.person_id
        JOIN people p2 ON p2.id=r.relative_id
        WHERE NOT EXISTS (
            SELECT 1 FROM relationships r2
            WHERE r2.person_id=r.relative_id AND r2.relative_id=r.person_id
        )
    """).fetchall()
    for r in missing:
        recip = get_reciprocal(con, r["person_id"], r["relative_is"])
        if recip:
            try:
                con.execute(
                    "INSERT OR IGNORE INTO relationships (person_id, relative_id, relative_is, notes, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (r["relative_id"], r["person_id"], recip, r["notes"], r["created_at"])
                )
                fixed.append({
                    "action": "inserted_reciprocal",
                    "person": r["relative_name"],
                    "relative": r["person_name"],
                    "relative_is": recip
                })
            except sqlite3.IntegrityError:
                pass
        else:
            flagged.append({
                "issue": "missing_reciprocal_unknown_type",
                "person": r["person_name"],
                "relative_is": r["relative_is"]
            })

    # Fix 2: Remove self-relationships
    self_rels = con.execute(
        "SELECT r.id, p.name FROM relationships r JOIN people p ON p.id=r.person_id "
        "WHERE r.person_id=r.relative_id"
    ).fetchall()
    for r in self_rels:
        con.execute("DELETE FROM relationships WHERE id=?", (r["id"],))
        fixed.append({"action": "deleted_self_relationship", "id": r["id"], "name": r["name"]})

    con.commit()

    # Flag only: asymmetric type in both directions (needs manual fix — can't auto-determine correct side)
    conflicts = con.execute("""
        SELECT p1.name AS person, p2.name AS relative, r1.relative_is AS type
        FROM relationships r1
        JOIN relationships r2 ON r2.person_id=r1.relative_id AND r2.relative_id=r1.person_id
                              AND r2.relative_is=r1.relative_is
        JOIN people p1 ON p1.id=r1.person_id
        JOIN people p2 ON p2.id=r1.relative_id
        WHERE r1.person_id < r1.relative_id
    """).fetchall()
    for r in conflicts:
        if r["type"] not in SYMMETRIC_TYPES:
            flagged.append({
                "issue": "conflicting_direction_needs_manual_fix",
                "person": r["person"],
                "relative": r["relative"],
                "type": r["type"]
            })

    # Flag only: unknown types
    placeholders = ",".join(f"'{t}'" for t in VALID_TYPES)
    unknown = con.execute(
        f"SELECT id, relative_is FROM relationships WHERE relative_is NOT IN ({placeholders})"
    ).fetchall()
    for r in unknown:
        flagged.append({"issue": "unknown_type_needs_manual_fix", "id": r["id"], "relative_is": r["relative_is"]})

    # Flag only: missing gender
    no_gender = con.execute(
        "SELECT id, name FROM people WHERE gender IS NULL OR gender NOT IN ('M','F','NB')"
    ).fetchall()
    for r in no_gender:
        flagged.append({"issue": "missing_gender", "id": r["id"], "name": r["name"]})

    out({"fixed": fixed, "flagged": flagged, "summary": {"fixed": len(fixed), "flagged": len(flagged)}}, args.pretty)


def cmd_rebuild_inferred(args):
    con = connect(args.db)
    ensure_inferred_column(con)

    # Clear all inferred rows
    deleted = con.execute("DELETE FROM relationships WHERE inferred=1").rowcount
    con.commit()

    # Optionally rebuild for a single person
    if args.name_or_id:
        person = resolve_person(con, args.name_or_id)
        people_ids = [person["id"]]
    else:
        people_ids = [r["id"] for r in con.execute("SELECT id FROM people").fetchall()]

    parent_graph = build_parent_graph(con)
    total = 0
    for pid in people_ids:
        inferred = infer_all_relatives(con, pid, parent_graph)
        cache_inferred_relationships(con, pid, inferred)
        total += len(inferred)

    out({
        "ok": True,
        "cleared": deleted,
        "rebuilt_for": len(people_ids),
        "inferred_rows": total,
    }, args.pretty)


def cmd_graph(args):
    """Generate a Mermaid relationship graph as a markdown file."""
    con = connect(args.db)
    output_path = args.output or str(_VAULT_DIR / "Family Tree.md")

    # Determine root person(s)
    if args.name_or_id:
        root = resolve_person(con, args.name_or_id)
        root_ids = {root["id"]}
        title = f"Family Tree — {root['name']}"
    else:
        root_ids = None
        title = "Relationship Graph"

    # Gather all relationships
    query = """
        SELECT r.person_id, r.relative_id, r.relative_is, r.inferred, r.relative_qualifier,
               p1.name AS person_name, p2.name AS relative_name
        FROM relationships r
        JOIN people p1 ON p1.id = r.person_id
        JOIN people p2 ON p2.id = r.relative_id
    """
    filters = []
    if args.type == "family":
        filters.append("(r.relative_is = 'parent' OR r.inferred = 1)")
    elif args.type == "explicit":
        filters.append("r.inferred = 0")
    elif args.type:
        filters.append(f"r.relative_is = '{args.type}'")

    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY r.person_id, r.relative_id"

    rows = con.execute(query).fetchall()

    # If rooted, BFS to depth limit
    if root_ids is not None:
        depth = args.depth or 2
        # Build adjacency from relationship rows
        adj = {}
        for r in rows:
            adj.setdefault(r["person_id"], set()).add(r["relative_id"])
            adj.setdefault(r["relative_id"], set()).add(r["person_id"])

        # BFS from root
        visited = set(root_ids)
        frontier = set(root_ids)
        for _ in range(depth):
            next_frontier = set()
            for pid in frontier:
                for neighbor in adj.get(pid, set()):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.add(neighbor)
            frontier = next_frontier
            if not frontier:
                break

        # Filter rows to only include edges where both ends are visited
        rows = [r for r in rows if r["person_id"] in visited and r["relative_id"] in visited]

    # Deduplicate edges: for symmetric relationships, keep only one direction (lower id first)
    # For directional ones (parent), keep the natural direction
    seen_edges = set()
    edges = []
    directional_types = {"parent", "manager", "report",
                         "accountant", "client", "pet", "owner",
                         "godfather", "godmother", "godson", "goddaughter"}

    for r in rows:
        pid, rid = r["person_id"], r["relative_id"]
        rel_type = r["relative_is"]

        # For inferred child/descendant types, skip — we already have the parent direction
        if r["inferred"] and any(t in rel_type for t in ["son", "daughter", "child",
                                                          "nephew", "niece",
                                                          "grandson", "granddaughter"]):
            continue

        # For symmetric types, normalize edge key
        if rel_type not in directional_types and not r["inferred"]:
            edge_key = (min(pid, rid), max(pid, rid), rel_type)
        else:
            edge_key = (pid, rid, rel_type)

        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            edges.append(r)

    # Build node ID map (sanitize names for Mermaid)
    node_ids = {}
    node_labels = {}
    all_people_ids = set()
    for r in edges:
        all_people_ids.add(r["person_id"])
        all_people_ids.add(r["relative_id"])

    for pid in sorted(all_people_ids):
        row = con.execute("SELECT id, name FROM people WHERE id=?", (pid,)).fetchone()
        if row:
            node_ids[pid] = f"p{pid}"
            node_labels[pid] = row["name"]

    # Generate Mermaid
    lines = ["```mermaid", "graph TD"]

    # Node declarations
    for pid in sorted(node_ids.keys()):
        label = node_labels[pid].replace('"', "'")
        lines.append(f'    {node_ids[pid]}["{label}"]')

    lines.append("")

    # Edge declarations
    for r in edges:
        pid, rid = r["person_id"], r["relative_id"]
        if pid not in node_ids or rid not in node_ids:
            continue
        rel_type = r["relative_is"]
        qualifier = r["relative_qualifier"]
        label = f"{qualifier} {rel_type}" if qualifier else rel_type

        # Directional types get arrows, symmetric get plain lines
        if rel_type in directional_types or r["inferred"]:
            lines.append(f'    {node_ids[pid]} -->|"{label}"| {node_ids[rid]}')
        else:
            lines.append(f'    {node_ids[pid]} ---|"{label}"| {node_ids[rid]}')

    lines.append("```")

    # Write markdown file
    md_content = f"# {title}\n\n" + "\n".join(lines) + "\n"

    with open(output_path, "w") as f:
        f.write(md_content)

    out({
        "ok": True,
        "output": output_path,
        "nodes": len(node_ids),
        "edges": len(edges),
        "title": title,
    }, args.pretty)


def name_to_obsidian_path(name):
    """Convert 'Firstname Lastname' to 'People/Lastname, Firstname.md'.
    Handles single names, multi-part first names, and placeholder names."""
    parts = name.split()
    if len(parts) == 1:
        return f"People/{parts[0]}.md"
    lastname = parts[-1]
    firstname = " ".join(parts[:-1])
    return f"People/{lastname}, {firstname}.md"


def cmd_links(args):
    """Update Obsidian people notes with wiki-linked relationship sections."""
    con = connect(args.db)

    # Determine which people to process
    if args.name_or_id:
        person = resolve_person(con, args.name_or_id)
        people = [person]
    else:
        people = con.execute("SELECT * FROM people ORDER BY name").fetchall()

    parent_graph = build_parent_graph(con)
    created = 0
    updated = 0
    skipped = 0

    for person in people:
        pid = person["id"]
        name = person["name"]

        # Skip placeholder people (names starting with "Unknown")
        if name.startswith("Unknown"):
            skipped += 1
            continue

        # Determine obsidian file path
        obs_rel = person["obsidian_file"] or name_to_obsidian_path(name)
        obs_abs = os.path.join(VAULT_PATH, obs_rel)

        # Gather all relationships for this person
        explicit = con.execute(
            "SELECT r.relative_is, r.relative_qualifier, r.inferred, p.name "
            "FROM relationships r JOIN people p ON p.id = r.relative_id "
            "WHERE r.person_id = ? ORDER BY r.relative_is, p.name",
            (pid,)
        ).fetchall()

        # Also run inference to get family relationships
        inferred = infer_all_relatives(con, pid, parent_graph)

        # Merge: inferred first (more specific names like "father" vs "parent"),
        # then explicit non-family types. Deduplicate by person name.
        seen_names = set()  # track which people we've already linked
        rels = []

        for r in inferred:
            if r["name"].startswith("Unknown"):
                continue
            seen_names.add(r["name"])
            rels.append((r["name"], r["relative_is"]))

        for r in explicit:
            rel_name = r["name"]
            if rel_name.startswith("Unknown"):
                continue
            if rel_name in seen_names:
                continue  # already have a (likely more specific) inferred link
            seen_names.add(rel_name)
            qualifier = r["relative_qualifier"]
            label = f"{qualifier} {r['relative_is']}" if qualifier else r["relative_is"]
            rels.append((rel_name, label))

        if not rels:
            skipped += 1
            continue

        # Build the linked relationships section
        section_lines = ["## Linked Relationships", ""]
        for rel_name, rel_type in sorted(rels, key=lambda x: (x[1], x[0])):
            wiki = name_to_obsidian_path(rel_name).replace("People/", "").replace(".md", "")
            section_lines.append(f"- **{rel_type}:** [[{wiki}]]")
        section_lines.append("")
        section_text = "\n".join(section_lines)

        # Create or update the file
        if os.path.exists(obs_abs):
            with open(obs_abs, "r") as f:
                content = f.read()

            # Replace existing section or append before the --- footer
            pattern = r"## Linked Relationships\n.*?(?=\n## |\n---|\Z)"
            if re.search(pattern, content, re.DOTALL):
                content = re.sub(pattern, section_text.rstrip(), content, flags=re.DOTALL)
            elif "\n---\n" in content:
                content = content.replace("\n---\n", f"\n{section_text}\n---\n", 1)
            else:
                content = content.rstrip() + "\n\n" + section_text

            with open(obs_abs, "w") as f:
                f.write(content)
            updated += 1
        else:
            # Create stub note
            os.makedirs(os.path.dirname(obs_abs), exist_ok=True)
            stub = f"# {name}\n\n{section_text}\n---\n*Created: {datetime.now().strftime('%Y-%m-%d')}*\n"
            with open(obs_abs, "w") as f:
                f.write(stub)
            created += 1

        # Update obsidian_file in DB if not set
        if not person["obsidian_file"]:
            con.execute("UPDATE people SET obsidian_file=? WHERE id=?", (obs_rel, pid))

    con.commit()

    out({
        "ok": True,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }, args.pretty)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="people.py",
        description="People & relationship manager for people.db"
    )
    # Shared parent parser for flags inherited by all subcommands
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--pretty", action="store_true", help="Human-readable output")
    shared.add_argument("--db", default=None, help=f"Override DB path (default: {DB_PATH})")

    sub = parser.add_subparsers(dest="command", required=True)

    # find
    p = sub.add_parser("find", parents=[shared], help="Fuzzy search by name")
    p.add_argument("name")

    # show
    p = sub.add_parser("show", parents=[shared], help="Full details + relationships")
    p.add_argument("name_or_id")

    # add-person
    p = sub.add_parser("add-person", parents=[shared], help="Add a new person")
    p.add_argument("name")
    add_person_args(p)

    # update-person
    p = sub.add_parser("update-person", parents=[shared], help="Update person fields")
    p.add_argument("name_or_id")
    add_person_args(p)

    # delete-person
    p = sub.add_parser("delete-person", parents=[shared], help="Hard-delete person + all relationships")
    p.add_argument("name_or_id")
    p.add_argument("--force", action="store_true", help="Actually delete (without this, dry-run only)")

    # relatives
    p = sub.add_parser("relatives", parents=[shared], help="List relationships for a person")
    p.add_argument("name_or_id")
    p.add_argument("--type", help="Filter by relationship type (supports aliases: son, daughter, mother, etc.)")
    p.add_argument("--infer", action=argparse.BooleanOptionalAction, default=True,
                   help="Include inferred relationships from family tree traversal (default: true)")

    # relate
    p = sub.add_parser("relate", parents=[shared], help="Add a relationship")
    p.add_argument("--person", required=True, help="The person (name or id)")
    p.add_argument("--relative", required=True, help="The relative (name or id)")
    p.add_argument("--relative-is", required=True, dest="relative_is", help="Type: 'child' means relative IS person's child")
    p.add_argument("--qualifier", help="Qualifier for the relationship (e.g. adoptive, step, foster)")
    p.add_argument("--notes")

    # update-relationship
    p = sub.add_parser("update-relationship", parents=[shared], help="Change relationship type between two people")
    p.add_argument("--person", required=True)
    p.add_argument("--relative", required=True)
    p.add_argument("--from", required=True, dest="from_type", help="Current relationship type")
    p.add_argument("--to", required=True, dest="to_type", help="New relationship type")

    # delete-relationship
    p = sub.add_parser("delete-relationship", parents=[shared], help="Remove a relationship between two people")
    p.add_argument("--person", required=True)
    p.add_argument("--relative", required=True)
    p.add_argument("--type", help="Relationship type to delete; required if multiple types exist between the pair")

    # between
    p = sub.add_parser("between", parents=[shared], help="List all relationships between two people")
    p.add_argument("person_a")
    p.add_argument("person_b")

    # check
    sub.add_parser("check", parents=[shared], help="Integrity report")

    # repair
    sub.add_parser("repair", parents=[shared], help="Auto-fix safe issues, flag the rest")

    # graph
    p = sub.add_parser("graph", parents=[shared], help="Generate Mermaid relationship graph")
    p.add_argument("name_or_id", nargs="?", default=None,
                   help="Root person to center graph on (default: all people)")
    p.add_argument("--depth", type=int, default=2,
                   help="How many hops from root (default: 2)")
    p.add_argument("--type", help="Filter: 'family', 'explicit', or a specific type")
    p.add_argument("--output", "-o", help="Output file path (default: obsidian-vault/Family Tree.md)")

    # links
    p = sub.add_parser("links", parents=[shared],
                       help="Update Obsidian people notes with wiki-linked relationships")
    p.add_argument("name_or_id", nargs="?", default=None,
                   help="Update links for a specific person only (default: everyone)")

    # rebuild-inferred
    p = sub.add_parser("rebuild-inferred", parents=[shared],
                       help="Clear and regenerate all inferred family relationships")
    p.add_argument("name_or_id", nargs="?", default=None,
                   help="Rebuild for a specific person only (default: everyone)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "find": cmd_find,
        "show": cmd_show,
        "add-person": cmd_add_person,
        "update-person": cmd_update_person,
        "delete-person": cmd_delete_person,
        "relatives": cmd_relatives,
        "relate": cmd_relate,
        "update-relationship": cmd_update_relationship,
        "delete-relationship": cmd_delete_relationship,
        "between": cmd_between,
        "check": cmd_check,
        "repair": cmd_repair,
        "graph": cmd_graph,
        "links": cmd_links,
        "rebuild-inferred": cmd_rebuild_inferred,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
