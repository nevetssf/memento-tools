#!/usr/bin/env python3
"""
Tests for priorities.py and priorities-rollover.py

Run with: python3 -m pytest tests/ -v
      or: python3 -m unittest tests/test_priorities.py -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import importlib.util

sys.path.insert(0, str(Path(__file__).parent.parent))
import priorities as P

# priorities-rollover.py has a hyphen so can't be imported normally
_spec = importlib.util.spec_from_file_location(
    "priorities_rollover",
    Path(__file__).parent.parent / "priorities-rollover.py",
)
PR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PR)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FRONTMATTER = "---\ndate: 2026-03-29\nday: Sunday\ntags: []\npeople: []\n---\n"
ENTRY = "\n## 09:00 PDT\nWent for a walk.\n"


def make_journal(tmp_path, date_str="2026-03-29", content=None):
    year = date_str[:4]
    d = tmp_path / year
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{date_str}.md"
    if content is None:
        content = FRONTMATTER + ENTRY
    p.write_text(content)
    return p


def run_cmd_set(path, labels):
    """Call cmd_set and capture the JSON output, suppressing sys.exit."""
    with patch("sys.stdout") as mock_stdout, patch("sys.exit"):
        output = []
        mock_stdout.write = lambda s: output.append(s)
        try:
            P.cmd_set(path, labels)
        except SystemExit:
            pass
    return path.read_text()


def call_set(path, labels):
    """Run cmd_set and return (new_text, json_output)."""
    captured = []
    original_out = P._out
    def fake_out(data):
        captured.append(data)
        raise SystemExit(0)
    with patch.object(P, "_out", fake_out):
        try:
            P.cmd_set(path, labels)
        except SystemExit:
            pass
    return path.read_text(), captured[0] if captured else None


def call_done(path, query):
    """Run cmd_done and return (new_text, json_output_or_error)."""
    captured = []
    errors = []
    def fake_out(data):
        captured.append(data)
        raise SystemExit(0)
    def fake_err(msg):
        errors.append(msg)
        raise SystemExit(1)
    with patch.object(P, "_out", fake_out), patch.object(P, "_err", fake_err):
        try:
            P.cmd_done(path, query)
        except SystemExit:
            pass
    return path.read_text(), captured[0] if captured else None, errors[0] if errors else None


def call_list(path):
    """Run cmd_list and return json_output."""
    captured = []
    def fake_out(data):
        captured.append(data)
        raise SystemExit(0)
    with patch.object(P, "_out", fake_out):
        try:
            P.cmd_list(path)
        except SystemExit:
            pass
    return captured[0] if captured else None


# ---------------------------------------------------------------------------
# parse_items
# ---------------------------------------------------------------------------

class TestParseItems(unittest.TestCase):

    def test_unchecked(self):
        section = "## Priorities\n- [ ] Task A\n- [ ] Task B\n"
        items = P.parse_items(section)
        self.assertEqual(items, [(False, "Task A"), (False, "Task B")])

    def test_checked(self):
        section = "## Priorities\n- [x] Done\n- [ ] Pending\n"
        items = P.parse_items(section)
        self.assertEqual(items, [(True, "Done"), (False, "Pending")])

    def test_empty_section(self):
        section = "## Priorities\n"
        self.assertEqual(P.parse_items(section), [])

    def test_ignores_non_checkbox_lines(self):
        section = "## Priorities\n- [ ] Real task\nsome random line\n- [x] Done\n"
        items = P.parse_items(section)
        self.assertEqual(items, [(False, "Real task"), (True, "Done")])

    def test_label_with_special_chars(self):
        section = "## Priorities\n- [ ] Buy milk & eggs (store)\n"
        items = P.parse_items(section)
        self.assertEqual(items, [(False, "Buy milk & eggs (store)")])


# ---------------------------------------------------------------------------
# build_section
# ---------------------------------------------------------------------------

class TestBuildSection(unittest.TestCase):

    def test_basic(self):
        items = [(False, "Task A"), (True, "Task B")]
        result = P.build_section(items)
        self.assertEqual(result, "## Priorities\n- [ ] Task A\n- [x] Task B\n")

    def test_empty(self):
        result = P.build_section([])
        self.assertEqual(result, "## Priorities\n")

    def test_roundtrip(self):
        items = [(False, "Alpha"), (True, "Beta"), (False, "Gamma")]
        section = P.build_section(items)
        parsed = P.parse_items(section)
        self.assertEqual(parsed, items)


# ---------------------------------------------------------------------------
# insert_after_frontmatter
# ---------------------------------------------------------------------------

class TestInsertAfterFrontmatter(unittest.TestCase):

    def test_with_frontmatter(self):
        text = FRONTMATTER + ENTRY
        section = "## Priorities\n- [ ] Task\n"
        result = P.insert_after_frontmatter(text, section)
        self.assertIn("## Priorities", result)
        # Priorities comes before first entry
        self.assertLess(result.index("## Priorities"), result.index("## 09:00 PDT"))
        # Frontmatter is preserved intact
        self.assertTrue(result.startswith(FRONTMATTER))

    def test_without_frontmatter(self):
        text = "## 09:00 PDT\nsome entry\n"
        section = "## Priorities\n- [ ] Task\n"
        result = P.insert_after_frontmatter(text, section)
        self.assertTrue(result.startswith("## Priorities"))

    def test_original_content_preserved(self):
        text = FRONTMATTER + ENTRY
        section = "## Priorities\n- [ ] Task\n"
        result = P.insert_after_frontmatter(text, section)
        self.assertIn("Went for a walk.", result)


# ---------------------------------------------------------------------------
# cmd_set
# ---------------------------------------------------------------------------

class TestCmdSet(unittest.TestCase):

    def test_creates_section_in_new_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            text, out = call_set(path, ["Task A", "Task B"])
            self.assertIn("## Priorities", text)
            self.assertIn("- [ ] Task A", text)
            self.assertIn("- [ ] Task B", text)

    def test_section_placed_before_first_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            text, _ = call_set(path, ["Task A"])
            self.assertLess(text.index("## Priorities"), text.index("## 09:00 PDT"))

    def test_replaces_existing_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            call_set(path, ["Old Task"])
            text, _ = call_set(path, ["New Task"])
            self.assertNotIn("Old Task", text)
            self.assertIn("New Task", text)
            self.assertEqual(text.count("## Priorities"), 1)

    def test_preserves_checked_state_on_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            call_set(path, ["Task A", "Task B"])
            call_done(path, "Task A")
            # Re-set with same tasks — Task A should stay checked
            text, out = call_set(path, ["Task A", "Task B"])
            self.assertIn("- [x] Task A", text)
            self.assertIn("- [ ] Task B", text)

    def test_new_tasks_start_unchecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            call_set(path, ["Task A"])
            call_done(path, "Task A")
            text, _ = call_set(path, ["Task A", "Brand New Task"])
            self.assertIn("- [x] Task A", text)
            self.assertIn("- [ ] Brand New Task", text)

    def test_errors_on_missing_journal(self):
        path = Path("/tmp/nonexistent/2026-01-01.md")
        errors = []
        with patch.object(P, "_err", lambda msg: errors.append(msg) or (_ for _ in ()).throw(SystemExit(1))):
            try:
                P.cmd_set(path, ["Task"])
            except SystemExit:
                pass
        self.assertTrue(len(errors) > 0)

    def test_json_output_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            _, out = call_set(path, ["Alpha", "Beta"])
            self.assertIn("priorities", out)
            self.assertEqual(len(out["priorities"]), 2)
            self.assertEqual(out["priorities"][0]["task"], "Alpha")
            self.assertFalse(out["priorities"][0]["done"])


# ---------------------------------------------------------------------------
# cmd_done
# ---------------------------------------------------------------------------

class TestCmdDone(unittest.TestCase):

    def _setup(self, tmp, tasks=None):
        path = make_journal(Path(tmp))
        call_set(path, tasks or ["Walk the dog", "Write report", "Call mom"])
        return path

    def test_exact_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._setup(tmp)
            text, out, err = call_done(path, "Walk the dog")
            self.assertIn("- [x] Walk the dog", text)
            self.assertIsNone(err)
            self.assertEqual(out["checked_off"], "Walk the dog")

    def test_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._setup(tmp)
            text, out, err = call_done(path, "walk the dog")
            self.assertIn("- [x] Walk the dog", text)
            self.assertIsNone(err)

    def test_substring_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._setup(tmp)
            text, out, err = call_done(path, "report")
            self.assertIn("- [x] Write report", text)
            self.assertIsNone(err)

    def test_no_match_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._setup(tmp)
            _, out, err = call_done(path, "nonexistent task")
            self.assertIsNotNone(err)
            self.assertIn("No priority matching", err)

    def test_ambiguous_match_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._setup(tmp, ["Write report", "Write email"])
            _, out, err = call_done(path, "write")
            self.assertIsNotNone(err)
            self.assertIn("Ambiguous", err)

    def test_already_done_marks_again(self):
        """Marking an already-checked item is idempotent."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._setup(tmp)
            call_done(path, "Walk the dog")
            text, out, err = call_done(path, "Walk the dog")
            self.assertIn("- [x] Walk the dog", text)
            self.assertIsNone(err)

    def test_other_items_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._setup(tmp)
            call_done(path, "Walk the dog")
            text = path.read_text()
            self.assertIn("- [ ] Write report", text)
            self.assertIn("- [ ] Call mom", text)

    def test_errors_on_no_priorities_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            _, out, err = call_done(path, "anything")
            self.assertIsNotNone(err)
            self.assertIn("No priorities section", err)

    def test_errors_on_missing_journal(self):
        path = Path("/tmp/nonexistent/2026-01-01.md")
        errors = []
        with patch.object(P, "_err", lambda msg: errors.append(msg) or (_ for _ in ()).throw(SystemExit(1))):
            try:
                P.cmd_done(path, "task")
            except SystemExit:
                pass
        self.assertTrue(len(errors) > 0)


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------

class TestCmdList(unittest.TestCase):

    def test_lists_priorities(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            call_set(path, ["Alpha", "Beta"])
            out = call_list(path)
            self.assertEqual(len(out["priorities"]), 2)
            self.assertEqual(out["priorities"][0]["task"], "Alpha")

    def test_returns_empty_for_no_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            out = call_list(path)
            self.assertEqual(out["priorities"], [])

    def test_returns_empty_for_missing_file(self):
        path = Path("/tmp/nonexistent/2099-01-01.md")
        out = call_list(path)
        self.assertEqual(out["priorities"], [])
        self.assertIn("note", out)

    def test_reflects_checked_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            call_set(path, ["Task A", "Task B"])
            call_done(path, "Task A")
            out = call_list(path)
            states = {p["task"]: p["done"] for p in out["priorities"]}
            self.assertTrue(states["Task A"])
            self.assertFalse(states["Task B"])

    def test_date_in_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp), "2026-03-29")
            out = call_list(path)
            self.assertEqual(out["date"], "2026-03-29")


# ---------------------------------------------------------------------------
# Content integrity — journal body must be preserved
# ---------------------------------------------------------------------------

class TestContentIntegrity(unittest.TestCase):

    def test_set_preserves_journal_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            call_set(path, ["Task A"])
            self.assertIn("Went for a walk.", path.read_text())

    def test_done_preserves_journal_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            call_set(path, ["Task A"])
            call_done(path, "Task A")
            self.assertIn("Went for a walk.", path.read_text())

    def test_set_preserves_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            call_set(path, ["Task A"])
            text = path.read_text()
            self.assertIn("date: 2026-03-29", text)
            self.assertIn("day: Sunday", text)

    def test_section_not_duplicated_on_multiple_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make_journal(Path(tmp))
            call_set(path, ["Task A"])
            call_set(path, ["Task B"])
            call_set(path, ["Task C"])
            self.assertEqual(path.read_text().count("## Priorities"), 1)


# ---------------------------------------------------------------------------
# priorities-rollover: get_incomplete_priorities
# ---------------------------------------------------------------------------

class TestGetIncompletePriorities(unittest.TestCase):

    def _write(self, tmp_path, content):
        p = tmp_path / "test.md"
        p.write_text(content)
        return p

    def test_finds_unchecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(Path(tmp), "## Priorities\n- [ ] Task A\n- [x] Done\n- [ ] Task B\n")
            result = PR.get_incomplete_priorities(p)
            self.assertEqual(result, ["Task A", "Task B"])

    def test_ignores_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(Path(tmp), "## Priorities\n- [x] All done\n")
            self.assertEqual(PR.get_incomplete_priorities(p), [])

    def test_missing_file_returns_empty(self):
        result = PR.get_incomplete_priorities(Path("/tmp/nonexistent.md"))
        self.assertEqual(result, [])

    def test_no_priorities_section_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(Path(tmp), FRONTMATTER + ENTRY)
            self.assertEqual(PR.get_incomplete_priorities(p), [])

    def test_strips_label_whitespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(Path(tmp), "- [ ]   Task with spaces   \n")
            result = PR.get_incomplete_priorities(p)
            self.assertEqual(result, ["Task with spaces"])


# ---------------------------------------------------------------------------
# priorities-rollover: deduplication logic
# ---------------------------------------------------------------------------

class TestRolloverDeduplication(unittest.TestCase):

    def _merge(self, existing, incomplete):
        """Reproduce the deduplication logic from priorities-rollover.main()."""
        all_tasks = existing[:]
        for task in incomplete:
            if task not in all_tasks:
                all_tasks.append(task)
        return all_tasks

    def test_no_overlap(self):
        result = self._merge(["Today task"], ["Yesterday task"])
        self.assertEqual(result, ["Today task", "Yesterday task"])

    def test_deduplicates_overlap(self):
        result = self._merge(["Shared task", "Today only"], ["Shared task", "Yesterday only"])
        self.assertEqual(result, ["Shared task", "Today only", "Yesterday only"])

    def test_existing_order_preserved(self):
        result = self._merge(["B", "A"], ["C"])
        self.assertEqual(result[0], "B")
        self.assertEqual(result[1], "A")

    def test_empty_existing(self):
        result = self._merge([], ["Task A", "Task B"])
        self.assertEqual(result, ["Task A", "Task B"])

    def test_empty_incomplete(self):
        result = self._merge(["Task A"], [])
        self.assertEqual(result, ["Task A"])

    def test_both_empty(self):
        self.assertEqual(self._merge([], []), [])


# ---------------------------------------------------------------------------
# priorities-rollover: get_existing_priorities
# ---------------------------------------------------------------------------

class TestGetExistingPriorities(unittest.TestCase):

    def test_returns_incomplete_tasks_only(self):
        mock_output = json.dumps({"priorities": [
            {"task": "Done task", "done": True},
            {"task": "Pending task", "done": False},
        ]})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)
            result = PR.get_existing_priorities("2026-03-29")
        self.assertEqual(result, ["Pending task"])

    def test_returns_empty_on_script_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = PR.get_existing_priorities("2026-03-29")
        self.assertEqual(result, [])

    def test_returns_empty_on_invalid_json(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json")
            result = PR.get_existing_priorities("2026-03-29")
        self.assertEqual(result, [])

    def test_returns_empty_on_no_priorities_key(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps({"date": "2026-03-29"}))
            result = PR.get_existing_priorities("2026-03-29")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
