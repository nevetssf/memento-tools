#!/usr/bin/env python3
"""
Tests for people.py

Run with: python3 -m pytest tests/ -v
      or: python3 -m unittest discover tests/ -v
"""

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import people as P

# ---------------------------------------------------------------------------
# Schema — mirrors steven.db exactly
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    gender TEXT,
    pronouns TEXT,
    birthdate TEXT, deathdate TEXT, age INTEGER,
    location TEXT, profession TEXT, business TEXT,
    phone TEXT, address TEXT, aliases TEXT,
    biography TEXT, notes TEXT, health TEXT,
    sport TEXT, highschool TEXT, university TEXT,
    linkedin TEXT, github TEXT, personal_website TEXT,
    professional_site TEXT, ieee TEXT, acm_profile TEXT,
    researchgate TEXT, patents TEXT, background_profile TEXT,
    reputation_profile TEXT, obsidian_file TEXT,
    date_created TEXT, date_updated TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    favorite_color TEXT, interests TEXT, desired_university TEXT
);

CREATE TABLE relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES people(id),
    relative_id INTEGER NOT NULL REFERENCES people(id),
    relative_is TEXT NOT NULL,
    notes TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    inferred BOOLEAN NOT NULL DEFAULT 0,
    relative_qualifier TEXT,
    UNIQUE(person_id, relative_id, relative_is)
);
"""

# Trigger: auto-reciprocals for non-family types only.
# Family relationships (child, sibling, cousin, etc.) are inferred from the parent graph.
TRIGGER = """
CREATE TRIGGER auto_reciprocal_relationship
AFTER INSERT ON relationships
BEGIN
    INSERT OR IGNORE INTO relationships (person_id, relative_id, relative_is, notes, created_at)
    SELECT NEW.relative_id, NEW.person_id, inv, NEW.notes, NEW.created_at
    FROM (SELECT CASE NEW.relative_is
        WHEN 'spouse'            THEN 'spouse'
        WHEN 'friend'            THEN 'friend'
        WHEN 'coworker'          THEN 'coworker'
        WHEN 'neighbor'          THEN 'neighbor'
        WHEN 'step-parent'       THEN 'step-child'
        WHEN 'step-child'        THEN 'step-parent'
        WHEN 'manager'           THEN 'report'
        WHEN 'report'            THEN 'manager'
        WHEN 'accountant'        THEN 'client'
        WHEN 'client'            THEN 'accountant'
        WHEN 'financial advisor' THEN 'client'
        WHEN 'pet'               THEN 'owner'
        WHEN 'owner'             THEN 'pet'
        WHEN 'boyfriend' THEN
            CASE (SELECT gender FROM people WHERE id = NEW.person_id)
                WHEN 'M' THEN 'boyfriend' WHEN 'F' THEN 'girlfriend' ELSE 'boyfriend' END
        WHEN 'girlfriend' THEN
            CASE (SELECT gender FROM people WHERE id = NEW.person_id)
                WHEN 'M' THEN 'boyfriend' WHEN 'F' THEN 'girlfriend' ELSE 'girlfriend' END
        WHEN 'ex-boyfriend' THEN
            CASE (SELECT gender FROM people WHERE id = NEW.person_id)
                WHEN 'M' THEN 'ex-boyfriend' WHEN 'F' THEN 'ex-girlfriend' ELSE 'ex-boyfriend' END
        WHEN 'ex-girlfriend' THEN
            CASE (SELECT gender FROM people WHERE id = NEW.person_id)
                WHEN 'M' THEN 'ex-boyfriend' WHEN 'F' THEN 'ex-girlfriend' ELSE 'ex-girlfriend' END
        WHEN 'godfather' THEN
            CASE (SELECT gender FROM people WHERE id = NEW.person_id)
                WHEN 'M' THEN 'godson' WHEN 'F' THEN 'goddaughter' ELSE 'godson' END
        WHEN 'godmother' THEN
            CASE (SELECT gender FROM people WHERE id = NEW.person_id)
                WHEN 'M' THEN 'godson' WHEN 'F' THEN 'goddaughter' ELSE 'goddaughter' END
        WHEN 'godson' THEN
            CASE (SELECT gender FROM people WHERE id = NEW.person_id)
                WHEN 'M' THEN 'godfather' WHEN 'F' THEN 'godmother' ELSE 'godfather' END
        WHEN 'goddaughter' THEN
            CASE (SELECT gender FROM people WHERE id = NEW.person_id)
                WHEN 'M' THEN 'godfather' WHEN 'F' THEN 'godmother' ELSE 'godmother' END
        ELSE NULL
    END AS inv)
    WHERE inv IS NOT NULL;
END;
"""


# ---------------------------------------------------------------------------
# Test base class
# ---------------------------------------------------------------------------

class PeopleTestCase(unittest.TestCase):
    """
    Base class: spins up a fresh temp-file DB for each test so commands
    that call connect(args.db) get the pre-seeded schema and trigger.
    """

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = self._tmp.name
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.executescript(TRIGGER)
        con.close()

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    # -- helpers -------------------------------------------------------------

    def con(self):
        """Open a raw connection to the test DB (row_factory set)."""
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def add_person(self, name, gender=None):
        with self.con() as c:
            c.execute(
                "INSERT INTO people (name, gender) VALUES (?,?)", (name, gender)
            )
            return c.execute("SELECT id FROM people WHERE name=?", (name,)).fetchone()[0]

    def add_rel(self, person_id, relative_id, rel_type, notes=None):
        """Insert one relationship row; trigger creates the reciprocal."""
        with self.con() as c:
            c.execute(
                "INSERT INTO relationships (person_id, relative_id, relative_is, notes) VALUES (?,?,?,?)",
                (person_id, relative_id, rel_type, notes),
            )

    def add_rel_no_trigger(self, person_id, relative_id, rel_type, notes=None):
        """Insert a relationship row WITHOUT firing the trigger (for testing missing-reciprocal detection)."""
        with self.con() as c:
            c.execute("DROP TRIGGER IF EXISTS auto_reciprocal_relationship")
            c.execute(
                "INSERT INTO relationships (person_id, relative_id, relative_is, notes) VALUES (?,?,?,?)",
                (person_id, relative_id, rel_type, notes),
            )
        # Restore trigger
        with self.con() as c:
            c.executescript(TRIGGER)

    def rels_between(self, id_a, id_b):
        """Return set of (person_id, relative_id, relative_is) tuples for A↔B."""
        with self.con() as c:
            rows = c.execute(
                "SELECT person_id, relative_id, relative_is FROM relationships "
                "WHERE (person_id=? AND relative_id=?) OR (person_id=? AND relative_id=?)",
                (id_a, id_b, id_b, id_a),
            ).fetchall()
        return {(r[0], r[1], r[2]) for r in rows}

    def rel_type_from(self, person_id, relative_id):
        """Return relative_is value for the specific directed row, or None."""
        with self.con() as c:
            row = c.execute(
                "SELECT relative_is FROM relationships WHERE person_id=? AND relative_id=?",
                (person_id, relative_id),
            ).fetchone()
        return row[0] if row else None

    def args(self, **kwargs):
        """Build a SimpleNamespace that mimics parsed argparse args."""
        defaults = {"db": self.db, "pretty": False}
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def run_cmd(self, fn, capture_stdout=True, **kwargs):
        """
        Call a cmd_* function, capture stdout, return parsed JSON.
        Raises SystemExit if the command calls err().
        """
        ns = self.args(**kwargs)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            fn(ns)
        output = buf.getvalue().strip()
        return json.loads(output) if output else None

    def run_cmd_expect_error(self, fn, **kwargs):
        """Assert the command exits non-zero and return the error message."""
        ns = self.args(**kwargs)
        buf = io.StringIO()
        with patch("sys.stderr", buf), self.assertRaises(SystemExit):
            fn(ns)
        return json.loads(buf.getvalue())["error"]


# ===========================================================================
# get_reciprocal
# ===========================================================================

class TestGetReciprocal(PeopleTestCase):

    def test_simple_types(self):
        c = self.con()
        self.assertEqual(P.get_reciprocal(c, 0, "spouse"), "spouse")
        self.assertEqual(P.get_reciprocal(c, 0, "friend"), "friend")
        self.assertEqual(P.get_reciprocal(c, 0, "manager"), "report")
        self.assertEqual(P.get_reciprocal(c, 0, "report"), "manager")
        self.assertEqual(P.get_reciprocal(c, 0, "pet"), "owner")
        self.assertEqual(P.get_reciprocal(c, 0, "owner"), "pet")
        self.assertEqual(P.get_reciprocal(c, 0, "accountant"), "client")
        self.assertEqual(P.get_reciprocal(c, 0, "step-parent"), "step-child")

    def test_boyfriend_male_person(self):
        pid = self.add_person("Bob", "M")
        c = self.con()
        self.assertEqual(P.get_reciprocal(c, pid, "boyfriend"), "boyfriend")
        self.assertEqual(P.get_reciprocal(c, pid, "girlfriend"), "boyfriend")

    def test_boyfriend_female_person(self):
        pid = self.add_person("Alice", "F")
        c = self.con()
        self.assertEqual(P.get_reciprocal(c, pid, "girlfriend"), "girlfriend")
        self.assertEqual(P.get_reciprocal(c, pid, "boyfriend"), "girlfriend")

    def test_ex_boyfriend_male_person(self):
        pid = self.add_person("Bob", "M")
        c = self.con()
        self.assertEqual(P.get_reciprocal(c, pid, "ex-boyfriend"), "ex-boyfriend")
        self.assertEqual(P.get_reciprocal(c, pid, "ex-girlfriend"), "ex-boyfriend")

    def test_unknown_type_returns_none(self):
        c = self.con()
        self.assertIsNone(P.get_reciprocal(c, 0, "wizard"))


# ===========================================================================
# Trigger reciprocals
# ===========================================================================

class TestTrigger(PeopleTestCase):
    """Trigger now only handles non-family reciprocals (no parent↔child, no sibling types)."""

    def test_parent_no_child_reciprocal(self):
        """Parent inserts should NOT create child reciprocals (child is inferred)."""
        mum = self.add_person("Mum", "F")
        kid = self.add_person("Kid", "M")
        self.add_rel(kid, mum, "parent")
        # No child reciprocal should exist
        self.assertIsNone(self.rel_type_from(mum, kid))

    def test_spouse_symmetric(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "spouse")
        self.assertEqual(self.rel_type_from(b, a), "spouse")

    def test_manager_report(self):
        boss = self.add_person("Boss", "M")
        emp = self.add_person("Employee", "F")
        self.add_rel(boss, emp, "report")
        self.assertEqual(self.rel_type_from(emp, boss), "manager")

    def test_boyfriend_male_gets_girlfriend_reciprocal(self):
        man = self.add_person("Man", "M")
        woman = self.add_person("Woman", "F")
        self.add_rel(man, woman, "girlfriend")
        self.assertEqual(self.rel_type_from(woman, man), "boyfriend")

    def test_pet_owner(self):
        owner = self.add_person("Jerry", "M")
        pet = self.add_person("Fido", None)
        self.add_rel(owner, pet, "pet")
        self.assertEqual(self.rel_type_from(pet, owner), "owner")

    def test_no_duplicate_on_symmetric(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "friend")
        with self.con() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM relationships WHERE "
                "(person_id=? AND relative_id=?) OR (person_id=? AND relative_id=?)",
                (a, b, b, a),
            ).fetchone()[0]
        self.assertEqual(count, 2)


# ===========================================================================
# cmd_relate
# ===========================================================================

class TestRelate(PeopleTestCase):

    def test_basic_relate(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        result = self.run_cmd(P.cmd_relate, person="Alice", relative="Bob", relative_is="friend", notes=None)
        self.assertTrue(result["ok"])
        self.assertEqual(self.rel_type_from(a, b), "friend")
        self.assertEqual(self.rel_type_from(b, a), "friend")  # trigger

    def test_relate_invalid_type(self):
        self.add_person("Alice", "F")
        self.add_person("Bob", "M")
        msg = self.run_cmd_expect_error(P.cmd_relate, person="Alice", relative="Bob", relative_is="wizard", notes=None)
        self.assertIn("wizard", msg)

    def test_relate_duplicate_rejected(self):
        self.add_person("Alice", "F")
        self.add_person("Bob", "M")
        self.run_cmd(P.cmd_relate, person="Alice", relative="Bob", relative_is="friend", notes=None)
        msg = self.run_cmd_expect_error(P.cmd_relate, person="Alice", relative="Bob", relative_is="friend", notes=None)
        self.assertIn("already exists", msg)

    def test_relate_multiple_types_allowed(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.run_cmd(P.cmd_relate, person="Alice", relative="Bob", relative_is="friend", notes=None)
        self.run_cmd(P.cmd_relate, person="Alice", relative="Bob", relative_is="coworker", notes=None)
        rels = self.rels_between(a, b)
        types = {r[2] for r in rels}
        self.assertIn("friend", types)
        self.assertIn("coworker", types)

    def test_relate_rejects_inferred_family_types(self):
        self.add_person("Alice", "F")
        self.add_person("Bob", "M")
        for t in ("child", "brother", "sister", "cousin", "uncle", "aunt", "nephew", "niece"):
            msg = self.run_cmd_expect_error(P.cmd_relate, person="Alice", relative="Bob", relative_is=t, notes=None)
            self.assertIn("inferred", msg)

    def test_relate_parent_allowed(self):
        kid = self.add_person("Kid", "M")
        mum = self.add_person("Mum", "F")
        result = self.run_cmd(P.cmd_relate, person="Kid", relative="Mum", relative_is="parent", notes=None)
        self.assertTrue(result["ok"])
        self.assertEqual(self.rel_type_from(kid, mum), "parent")


# ===========================================================================
# cmd_between
# ===========================================================================

class TestBetween(PeopleTestCase):

    def test_between_no_relationship(self):
        self.add_person("Alice", "F")
        self.add_person("Bob", "M")
        result = self.run_cmd(P.cmd_between, person_a="Alice", person_b="Bob")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["b_is_a_s"], [])

    def test_between_single(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "friend")
        result = self.run_cmd(P.cmd_between, person_a="Alice", person_b="Bob")
        self.assertEqual(result["b_is_a_s"], ["friend"])

    def test_between_multiple(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "friend")
        self.add_rel(a, b, "coworker")
        result = self.run_cmd(P.cmd_between, person_a="Alice", person_b="Bob")
        self.assertCountEqual(result["b_is_a_s"], ["friend", "coworker"])

    def test_between_directional(self):
        # between A B shows from A's perspective only
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(b, a, "parent")  # "Alice is Bob's parent"
        result = self.run_cmd(P.cmd_between, person_a="Bob", person_b="Alice")
        self.assertEqual(result["b_is_a_s"], ["parent"])


# ===========================================================================
# cmd_delete_relationship
# ===========================================================================

class TestDeleteRelationship(PeopleTestCase):

    def test_delete_single_no_type_flag(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "friend")
        self.run_cmd(P.cmd_delete_relationship, person="Alice", relative="Bob", type=None)
        self.assertEqual(len(self.rels_between(a, b)), 0)

    def test_delete_with_type_flag(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "friend")
        self.add_rel(a, b, "coworker")
        self.run_cmd(P.cmd_delete_relationship, person="Alice", relative="Bob", type="friend")
        remaining = {r[2] for r in self.rels_between(a, b)}
        self.assertNotIn("friend", remaining)
        self.assertIn("coworker", remaining)

    def test_delete_multiple_requires_type(self):
        self.add_person("Alice", "F")
        self.add_person("Bob", "M")
        a = P.resolve_person(self.con(), "Alice")["id"]
        b = P.resolve_person(self.con(), "Bob")["id"]
        self.add_rel(a, b, "friend")
        self.add_rel(a, b, "coworker")
        msg = self.run_cmd_expect_error(P.cmd_delete_relationship, person="Alice", relative="Bob", type=None)
        self.assertIn("Multiple", msg)

    def test_delete_removes_both_directions(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "friend")
        self.run_cmd(P.cmd_delete_relationship, person="Alice", relative="Bob", type="friend")
        # Both the A→B and B→A rows should be gone
        self.assertEqual(len(self.rels_between(a, b)), 0)

    def test_delete_nonexistent_errors(self):
        self.add_person("Alice", "F")
        self.add_person("Bob", "M")
        msg = self.run_cmd_expect_error(P.cmd_delete_relationship, person="Alice", relative="Bob", type="friend")
        self.assertIn("No", msg)


# ===========================================================================
# cmd_check
# ===========================================================================

class TestCheck(PeopleTestCase):

    def _check(self):
        """Run check and return the result dict (suppress sys.exit on errors)."""
        ns = self.args()
        buf = io.StringIO()
        try:
            with patch("sys.stdout", buf):
                P.cmd_check(ns)
        except SystemExit:
            pass
        return json.loads(buf.getvalue())

    def test_clean_db(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "friend")
        result = self._check()
        self.assertTrue(result["summary"]["ok"])

    def test_detects_missing_reciprocal(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel_no_trigger(a, b, "friend")
        result = self._check()
        issues = [e["issue"] for e in result["errors"]]
        self.assertIn("missing_reciprocal", issues)

    def test_detects_conflicting_direction(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        # Insert both directions with 'parent' (impossible in reality) — bypass trigger
        self.add_rel_no_trigger(a, b, "parent")
        self.add_rel_no_trigger(b, a, "parent")
        result = self._check()
        issues = [e["issue"] for e in result["errors"]]
        self.assertIn("conflicting_direction", issues)

    def test_symmetric_type_not_flagged_as_conflict(self):
        # 'friend' in both directions is valid — should NOT be flagged
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        # Use trigger: inserting friend auto-creates the symmetric reciprocal
        self.add_rel(a, b, "friend")
        result = self._check()
        conflicts = [e for e in result["errors"] if e["issue"] == "conflicting_direction"]
        self.assertEqual(len(conflicts), 0)

    def test_multiple_different_types_not_flagged(self):
        # friend + coworker between same pair is valid
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "friend")
        self.add_rel(a, b, "coworker")
        result = self._check()
        self.assertTrue(result["summary"]["ok"])

    def test_detects_self_relationship(self):
        a = self.add_person("Alice", "F")
        self.add_rel_no_trigger(a, a, "friend")
        result = self._check()
        issues = [e["issue"] for e in result["errors"]]
        self.assertIn("self_relationship", issues)

    def test_warns_missing_gender(self):
        self.add_person("Alice", None)  # no gender
        result = self._check()
        issues = [w["issue"] for w in result["warnings"]]
        self.assertIn("missing_or_unknown_gender", issues)

    def test_gender_set_no_warning(self):
        self.add_person("Alice", "F")
        result = self._check()
        self.assertEqual(result["summary"]["warnings"], 0)


# ===========================================================================
# cmd_repair
# ===========================================================================

class TestRepair(PeopleTestCase):

    def _repair(self):
        ns = self.args()
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            P.cmd_repair(ns)
        return json.loads(buf.getvalue())

    def test_repair_inserts_missing_reciprocal(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel_no_trigger(a, b, "friend")
        result = self._repair()
        self.assertEqual(result["summary"]["fixed"], 1)
        self.assertEqual(self.rel_type_from(b, a), "friend")

    def test_repair_removes_self_relationship(self):
        a = self.add_person("Alice", "F")
        with self.con() as c:
            c.execute(
                "INSERT INTO relationships (person_id, relative_id, relative_is) VALUES (?,?,?)",
                (a, a, "friend"),
            )
        result = self._repair()
        fixed_actions = [f["action"] for f in result["fixed"]]
        self.assertIn("deleted_self_relationship", fixed_actions)

    def test_repair_flags_conflict(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel_no_trigger(a, b, "parent")
        self.add_rel_no_trigger(b, a, "parent")
        result = self._repair()
        flagged_issues = [f["issue"] for f in result["flagged"]]
        self.assertIn("conflicting_direction_needs_manual_fix", flagged_issues)

    def test_repair_parent_no_reciprocal_needed(self):
        """Parent relationships don't need reciprocals (child is inferred)."""
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel_no_trigger(a, b, "parent")  # "Bob is Alice's parent"
        result = self._repair()
        # No child reciprocal should be created (child is inferred)
        self.assertIsNone(self.rel_type_from(b, a))


# ===========================================================================
# cmd_add_person / cmd_update_person / cmd_delete_person
# ===========================================================================

class TestPersonCRUD(PeopleTestCase):

    def test_add_person(self):
        result = self.run_cmd(
            P.cmd_add_person,
            name="Jane Doe",
            gender="F",
            location="Boulder, CO",
            profession="Engineer",
            pronouns=None, birthdate=None, deathdate=None, age=None,
            business=None, phone=None, address=None, aliases=None,
            biography=None, notes=None, health=None, sport=None,
            highschool=None, university=None, linkedin=None, github=None,
            personal_website=None, professional_site=None, ieee=None,
            acm_profile=None, researchgate=None, patents=None,
            background_profile=None, reputation_profile=None, obsidian_file=None,
        )
        self.assertTrue(result["ok"])
        with self.con() as c:
            row = c.execute("SELECT * FROM people WHERE name='Jane Doe'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["gender"], "F")

    def test_add_person_duplicate_rejected(self):
        self.add_person("Alice", "F")
        msg = self.run_cmd_expect_error(
            P.cmd_add_person,
            name="Alice",
            gender="F",
            **{f: None for f in P.PERSON_FIELDS if f not in ("gender", "name")},
        )
        self.assertIn("already exists", msg)

    def test_update_person(self):
        self.add_person("Alice", "F")
        result = self.run_cmd(
            P.cmd_update_person,
            name_or_id="Alice",
            location="New York",
            **{f: None for f in P.PERSON_FIELDS if f != "location"},
        )
        self.assertTrue(result["ok"])
        with self.con() as c:
            row = c.execute("SELECT location FROM people WHERE name='Alice'").fetchone()
        self.assertEqual(row[0], "New York")

    def test_delete_person_dry_run(self):
        a = self.add_person("Alice", "F")
        result = self.run_cmd(P.cmd_delete_person, name_or_id="Alice", force=False)
        self.assertTrue(result["dry_run"])
        with self.con() as c:
            row = c.execute("SELECT id FROM people WHERE id=?", (a,)).fetchone()
        self.assertIsNotNone(row)  # still exists

    def test_delete_person_force(self):
        a = self.add_person("Alice", "F")
        b = self.add_person("Bob", "M")
        self.add_rel(a, b, "friend")
        result = self.run_cmd(P.cmd_delete_person, name_or_id="Alice", force=True)
        self.assertTrue(result["ok"])
        with self.con() as c:
            row = c.execute("SELECT id FROM people WHERE id=?", (a,)).fetchone()
            rels = c.execute("SELECT id FROM relationships WHERE person_id=? OR relative_id=?", (a, a)).fetchall()
        self.assertIsNone(row)
        self.assertEqual(len(rels), 0)


# ===========================================================================
# cmd_relatives (with type aliases)
# ===========================================================================

class TestRelatives(PeopleTestCase):

    def setUp(self):
        super().setUp()
        self.steven = self.add_person("Steven", "M")
        self.harper = self.add_person("Harper", "F")
        self.sterling = self.add_person("Sterling", "F")
        self.bob = self.add_person("Bob", "M")
        # Use parent links (the only explicit family type)
        self.add_rel(self.harper, self.steven, "parent")     # Steven is Harper's parent
        self.add_rel(self.sterling, self.steven, "parent")   # Steven is Sterling's parent
        self.add_rel(self.steven, self.bob, "friend")

    def test_relatives_all(self):
        ns = self.args(name_or_id="Steven", type=None, infer=True)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            P.cmd_relatives(ns)
        result = json.loads(buf.getvalue())
        names = {r["name"] for r in result}
        self.assertIn("Harper", names)
        self.assertIn("Sterling", names)
        self.assertIn("Bob", names)

    def test_relatives_filter_children(self):
        ns = self.args(name_or_id="Steven", type="children", infer=True)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            P.cmd_relatives(ns)
        result = json.loads(buf.getvalue())
        names = {r["name"] for r in result}
        self.assertIn("Harper", names)
        self.assertIn("Sterling", names)
        self.assertNotIn("Bob", names)

    def test_relatives_alias_daughter(self):
        ns = self.args(name_or_id="Steven", type="daughter", infer=True)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            P.cmd_relatives(ns)
        result = json.loads(buf.getvalue())
        names = {r["name"] for r in result}
        self.assertIn("Harper", names)
        self.assertIn("Sterling", names)

    def test_relatives_alias_son_returns_empty(self):
        ns = self.args(name_or_id="Steven", type="son", infer=True)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            P.cmd_relatives(ns)
        result = json.loads(buf.getvalue())
        self.assertEqual(result, [])

    def test_relatives_no_infer(self):
        """With --no-infer, only explicit DB relationships are returned."""
        ns = self.args(name_or_id="Steven", type=None, infer=False)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            P.cmd_relatives(ns)
        result = json.loads(buf.getvalue())
        names = {r["name"] for r in result}
        self.assertIn("Bob", names)       # explicit friend
        self.assertNotIn("Harper", names)  # child is inferred, not explicit


# ===========================================================================
# resolve_person
# ===========================================================================

class TestResolvePerson(PeopleTestCase):

    def test_resolve_by_exact_name(self):
        self.add_person("Alice Smith", "F")
        c = self.con()
        row = P.resolve_person(c, "Alice Smith")
        self.assertEqual(row["name"], "Alice Smith")

    def test_resolve_by_id(self):
        pid = self.add_person("Alice", "F")
        c = self.con()
        row = P.resolve_person(c, str(pid))
        self.assertEqual(row["id"], pid)

    def test_resolve_fuzzy(self):
        self.add_person("Alice Smith", "F")
        c = self.con()
        row = P.resolve_person(c, "Alice")
        self.assertEqual(row["name"], "Alice Smith")

    def test_resolve_ambiguous_exits(self):
        self.add_person("Alice Smith", "F")
        self.add_person("Alice Jones", "F")
        c = self.con()
        with self.assertRaises(SystemExit):
            P.resolve_person(c, "Alice")

    def test_resolve_not_found_exits(self):
        c = self.con()
        with self.assertRaises(SystemExit):
            P.resolve_person(c, "Nobody")


# ===========================================================================
# Relationship Inference Engine
# ===========================================================================

class TestNameRelationship(unittest.TestCase):
    """Unit tests for the name_relationship() function."""

    def test_siblings(self):
        self.assertEqual(P.name_relationship(1, 1, "M"), "brother")
        self.assertEqual(P.name_relationship(1, 1, "F"), "sister")
        self.assertEqual(P.name_relationship(1, 1, None), "sibling")

    def test_uncle_aunt(self):
        # gen_a=2, gen_b=1: target is person's uncle/aunt
        self.assertEqual(P.name_relationship(2, 1, "M"), "uncle")
        self.assertEqual(P.name_relationship(2, 1, "F"), "aunt")

    def test_nephew_niece(self):
        # gen_a=1, gen_b=2: target is person's nephew/niece
        self.assertEqual(P.name_relationship(1, 2, "M"), "nephew")
        self.assertEqual(P.name_relationship(1, 2, "F"), "niece")

    def test_first_cousin(self):
        # gen_a=2, gen_b=2: 1st cousin
        self.assertEqual(P.name_relationship(2, 2), "1st cousin")

    def test_second_cousin(self):
        # gen_a=3, gen_b=3: 2nd cousin
        self.assertEqual(P.name_relationship(3, 3), "2nd cousin")

    def test_third_cousin(self):
        self.assertEqual(P.name_relationship(4, 4), "3rd cousin")

    def test_first_cousin_once_removed(self):
        # gen_a=2, gen_b=3: 1st cousin once removed
        self.assertEqual(P.name_relationship(2, 3), "1st cousin once removed")
        # Symmetric: gen_a=3, gen_b=2
        self.assertEqual(P.name_relationship(3, 2), "1st cousin once removed")

    def test_first_cousin_twice_removed(self):
        self.assertEqual(P.name_relationship(2, 4), "1st cousin twice removed")

    def test_great_uncle(self):
        # gen_a=3, gen_b=1: great-uncle
        self.assertEqual(P.name_relationship(3, 1, "M"), "great-uncle")
        self.assertEqual(P.name_relationship(3, 1, "F"), "great-aunt")

    def test_great_nephew(self):
        # gen_a=1, gen_b=3: great-nephew
        self.assertEqual(P.name_relationship(1, 3, "M"), "great-nephew")
        self.assertEqual(P.name_relationship(1, 3, "F"), "great-niece")

    def test_great_great_uncle(self):
        self.assertEqual(P.name_relationship(4, 1, "M"), "great-great-uncle")

    def test_second_cousin_once_removed(self):
        # gen_a=3, gen_b=4: 2nd cousin once removed
        self.assertEqual(P.name_relationship(3, 4), "2nd cousin once removed")


class TestNameDirectLine(unittest.TestCase):
    """Unit tests for name_direct_line()."""

    def test_parent(self):
        self.assertEqual(P.name_direct_line("ancestor", 1, "M"), "father")
        self.assertEqual(P.name_direct_line("ancestor", 1, "F"), "mother")

    def test_grandparent(self):
        self.assertEqual(P.name_direct_line("ancestor", 2, "M"), "grandfather")
        self.assertEqual(P.name_direct_line("ancestor", 2, "F"), "grandmother")

    def test_great_grandparent(self):
        self.assertEqual(P.name_direct_line("ancestor", 3, "M"), "great-grandfather")
        self.assertEqual(P.name_direct_line("ancestor", 3, "F"), "great-grandmother")

    def test_great_great_grandparent(self):
        self.assertEqual(P.name_direct_line("ancestor", 4, "M"), "great-great-grandfather")

    def test_child(self):
        self.assertEqual(P.name_direct_line("descendant", 1, "M"), "son")
        self.assertEqual(P.name_direct_line("descendant", 1, "F"), "daughter")

    def test_grandchild(self):
        self.assertEqual(P.name_direct_line("descendant", 2, "M"), "grandson")
        self.assertEqual(P.name_direct_line("descendant", 2, "F"), "granddaughter")

    def test_great_grandchild(self):
        self.assertEqual(P.name_direct_line("descendant", 3, "F"), "great-granddaughter")


class TestInferenceEngine(PeopleTestCase):
    """Integration tests for the full inference pipeline using a family tree."""

    def _build_family(self):
        """
        Build a 3-generation family:
            Grandpa(M) + Grandma(F)
              ├── Dad(M) + Mom(F) → Child1(M), Child2(F)
              └── Uncle(M) → Cousin(F)
        """
        gpa = self.add_person("Grandpa", "M")
        gma = self.add_person("Grandma", "F")
        dad = self.add_person("Dad", "M")
        mom = self.add_person("Mom", "F")
        uncle = self.add_person("Uncle", "M")
        child1 = self.add_person("Child1", "M")
        child2 = self.add_person("Child2", "F")
        cousin = self.add_person("Cousin", "F")

        # Parent links only
        self.add_rel(dad, gpa, "parent")
        self.add_rel(dad, gma, "parent")
        self.add_rel(uncle, gpa, "parent")
        self.add_rel(uncle, gma, "parent")
        self.add_rel(child1, dad, "parent")
        self.add_rel(child1, mom, "parent")
        self.add_rel(child2, dad, "parent")
        self.add_rel(child2, mom, "parent")
        self.add_rel(cousin, uncle, "parent")

        return {
            "gpa": gpa, "gma": gma, "dad": dad, "mom": mom,
            "uncle": uncle, "child1": child1, "child2": child2, "cousin": cousin,
        }

    def test_build_parent_graph(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        self.assertIn(f["gpa"], pg[f["dad"]])
        self.assertIn(f["gma"], pg[f["dad"]])
        self.assertIn(f["dad"], pg[f["child1"]])

    def test_find_ancestors(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        anc = P.find_ancestors(f["child1"], pg)
        self.assertEqual(anc[f["child1"]], 0)
        self.assertEqual(anc[f["dad"]], 1)
        self.assertEqual(anc[f["mom"]], 1)
        self.assertEqual(anc[f["gpa"]], 2)
        self.assertEqual(anc[f["gma"]], 2)

    def test_infer_sibling(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        rel = P.infer_relationship(con, f["child1"], f["child2"], pg)
        self.assertEqual(rel, "sister")  # Child2 is F

    def test_infer_parent_child(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        # Child1 → Dad: Dad is Child1's father
        rel = P.infer_relationship(con, f["child1"], f["dad"], pg)
        self.assertEqual(rel, "father")
        # Dad → Child1: Child1 is Dad's son
        rel = P.infer_relationship(con, f["dad"], f["child1"], pg)
        self.assertEqual(rel, "son")

    def test_infer_grandparent(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        rel = P.infer_relationship(con, f["child1"], f["gpa"], pg)
        self.assertEqual(rel, "grandfather")
        rel = P.infer_relationship(con, f["child1"], f["gma"], pg)
        self.assertEqual(rel, "grandmother")

    def test_infer_uncle(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        rel = P.infer_relationship(con, f["child1"], f["uncle"], pg)
        self.assertEqual(rel, "uncle")

    def test_infer_cousin(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        rel = P.infer_relationship(con, f["child1"], f["cousin"], pg)
        self.assertEqual(rel, "1st cousin")

    def test_infer_nephew(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        # Uncle → Child1: Child1 is Uncle's nephew
        rel = P.infer_relationship(con, f["uncle"], f["child1"], pg)
        self.assertEqual(rel, "nephew")

    def test_infer_no_relation(self):
        f = self._build_family()
        stranger = self.add_person("Stranger", "M")
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        rel = P.infer_relationship(con, f["child1"], stranger, pg)
        self.assertIsNone(rel)

    def test_infer_all_relatives(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        rels = P.infer_all_relatives(con, f["child1"], pg)
        names = {r["name"]: r["relative_is"] for r in rels}
        self.assertEqual(names["Child2"], "sister")
        self.assertEqual(names["Dad"], "father")
        self.assertEqual(names["Mom"], "mother")
        self.assertEqual(names["Grandpa"], "grandfather")
        self.assertEqual(names["Grandma"], "grandmother")
        self.assertEqual(names["Uncle"], "uncle")
        self.assertEqual(names["Cousin"], "1st cousin")

    def test_infer_grandchild(self):
        f = self._build_family()
        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        rel = P.infer_relationship(con, f["gpa"], f["child1"], pg)
        self.assertEqual(rel, "grandson")

    def test_four_generations(self):
        """Great-grandparent inference across 4 generations."""
        ggpa = self.add_person("GGrandpa", "M")
        gpa = self.add_person("Grandpa", "M")
        dad = self.add_person("Dad", "M")
        kid = self.add_person("Kid", "F")
        self.add_rel(gpa, ggpa, "parent")
        self.add_rel(dad, gpa, "parent")
        self.add_rel(kid, dad, "parent")

        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        self.assertEqual(P.infer_relationship(con, kid, ggpa, pg), "great-grandfather")
        self.assertEqual(P.infer_relationship(con, ggpa, kid, pg), "great-granddaughter")

    def test_second_cousin(self):
        """Second cousins share great-grandparents (gen 3 each)."""
        ggpa = self.add_person("GGrandpa", "M")
        gpa1 = self.add_person("Grandpa1", "M")
        gpa2 = self.add_person("Grandpa2", "M")
        dad1 = self.add_person("Dad1", "M")
        dad2 = self.add_person("Dad2", "M")
        kid1 = self.add_person("Kid1", "M")
        kid2 = self.add_person("Kid2", "F")
        self.add_rel(gpa1, ggpa, "parent")
        self.add_rel(gpa2, ggpa, "parent")
        self.add_rel(dad1, gpa1, "parent")
        self.add_rel(dad2, gpa2, "parent")
        self.add_rel(kid1, dad1, "parent")
        self.add_rel(kid2, dad2, "parent")

        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        self.assertEqual(P.infer_relationship(con, kid1, kid2, pg), "2nd cousin")

    def test_cousin_once_removed(self):
        """First cousin once removed: one generation off."""
        gpa = self.add_person("Grandpa", "M")
        dad = self.add_person("Dad", "M")
        uncle = self.add_person("Uncle", "M")
        kid = self.add_person("Kid", "M")
        cousin = self.add_person("Cousin", "F")
        cousin_kid = self.add_person("CousinKid", "M")
        self.add_rel(dad, gpa, "parent")
        self.add_rel(uncle, gpa, "parent")
        self.add_rel(kid, dad, "parent")
        self.add_rel(cousin, uncle, "parent")
        self.add_rel(cousin_kid, cousin, "parent")

        con = P.connect(self.db)
        pg = P.build_parent_graph(con)
        # Kid and CousinKid: Kid is gen 2 from Grandpa, CousinKid is gen 3
        self.assertEqual(P.infer_relationship(con, kid, cousin_kid, pg), "1st cousin once removed")


class TestMatchInferredType(unittest.TestCase):
    """Tests for the _match_inferred_type filter function."""

    def test_cousin_matches_1st_cousin(self):
        self.assertTrue(P._match_inferred_type("1st cousin", "cousin", None))

    def test_cousin_matches_2nd_cousin_twice_removed(self):
        self.assertTrue(P._match_inferred_type("2nd cousin twice removed", "cousin", None))

    def test_grandparent_matches_grandfather(self):
        self.assertTrue(P._match_inferred_type("grandfather", "grandparent", None))

    def test_grandparent_matches_grandmother(self):
        self.assertTrue(P._match_inferred_type("grandmother", "grandparent", None))

    def test_grandparent_matches_great_grandmother(self):
        self.assertTrue(P._match_inferred_type("great-grandmother", "grandparent", None))

    def test_children_matches_son(self):
        self.assertTrue(P._match_inferred_type("son", "children", None))

    def test_children_matches_daughter(self):
        self.assertTrue(P._match_inferred_type("daughter", "children", None))

    def test_sibling_matches_brother(self):
        self.assertTrue(P._match_inferred_type("brother", "sibling", None))

    def test_sibling_matches_sister(self):
        self.assertTrue(P._match_inferred_type("sister", "sibling", None))

    def test_uncle_does_not_match_cousin(self):
        self.assertFalse(P._match_inferred_type("uncle", "cousin", None))

    def test_friend_does_not_match_sibling(self):
        self.assertFalse(P._match_inferred_type("friend", "sibling", None))


if __name__ == "__main__":
    unittest.main()
