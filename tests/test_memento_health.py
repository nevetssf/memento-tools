#!/usr/bin/env python3
"""Tests for memento_health.py's alerting logic. Run: python3 -m unittest tests.test_memento_health"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import memento_health as mh


def make(results, grace=0):
    it = iter(results)
    return mh.Check("x", "fast", "Thing works", lambda ctx: next(it), grace_min=grace)


class TestEvaluate(unittest.TestCase):
    def test_alert_once_then_recover(self):
        chk = make([mh.fail("down"), mh.fail("down"), mh.ok("up")])
        rec = {}
        _, n1 = mh.evaluate(chk, rec, 1000)
        _, n2 = mh.evaluate(chk, rec, 1900)
        res, n3 = mh.evaluate(chk, rec, 2800)
        self.assertEqual([t for t, *_ in n1], ["❌ Memento health: Thing works"])
        self.assertEqual(n2, [])
        self.assertEqual([t for t, *_ in n3], ["✅ Recovered: Thing works"])
        self.assertEqual((res.status, rec["last_ok"]), ("ok", 2800))
        self.assertNotIn("bad_since", rec)

    def test_grace_period_warns_first(self):
        chk = make([mh.fail("slow"), mh.fail("slow")], grace=30)
        rec = {}
        r1, n1 = mh.evaluate(chk, rec, 0)
        r2, n2 = mh.evaluate(chk, rec, 31 * 60)
        self.assertEqual((r1.status, n1), ("warn", []))
        self.assertIn("fails after 30 min", r1.detail)
        self.assertEqual(r2.status, "fail")
        self.assertEqual(len(n2), 1)

    def test_daily_reminder_while_failing(self):
        chk = make([mh.fail("down")] * 3)
        rec = {}
        mh.evaluate(chk, rec, 0)
        _, n = mh.evaluate(chk, rec, 3600)
        self.assertEqual(n, [])
        _, n = mh.evaluate(chk, rec, 24 * 3600)
        self.assertEqual(len(n), 1)

    def test_exception_becomes_fail_and_warn_never_pushes(self):
        def boom(ctx):
            raise ConnectionError("refused")
        res, n = mh.evaluate(mh.Check("b", "fast", "B", boom), {}, 0)
        self.assertEqual(res.status, "fail")
        self.assertIn("ConnectionError", res.detail)
        _, n = mh.evaluate(make([mh.warn("meh")]), {}, 0)
        self.assertEqual(n, [])


class TestNote(unittest.TestCase):
    def test_note_lists_failures_first(self):
        with tempfile.TemporaryDirectory() as d, \
             patch.object(mh, "HEALTH_NOTE", Path(d) / "health.md"):
            state = {"fast_ran": 1795540000, "checks": {
                "a": {"status": "ok", "title": "A ok", "tier": "fast", "detail": "fine",
                      "last_ok": 1795540000},
                "b": {"status": "fail", "title": "B broken", "tier": "daily", "detail": "x|y"}}}
            mh.write_note(state, 1795540000)
            text = (Path(d) / "health.md").read_text()
        self.assertLess(text.index("B broken"), text.index("A ok"))
        self.assertIn("1 ok, 0 warn, 1 fail", text)
        self.assertIn("x\\|y", text)


if __name__ == "__main__":
    unittest.main()
