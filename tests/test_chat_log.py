#!/usr/bin/env python3
"""Tests for chat_log.py (channel-tagged sections, migration) and chat_reconcile.py
(reading OpenClaw's agent DB). Run: python3 -m unittest tests.test_chat_log
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))
import chat_log as cl
import chat_reconcile as cr

DEN = ZoneInfo("America/Denver")


def at(h, m, day=24):
    return datetime(2026, 11, day, h, m, tzinfo=DEN)


class TempVault(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name) / "vault"
        self.chats = self.vault / "Chats"
        self.chats.mkdir(parents=True)
        self._patches = [patch.object(cl, "CHAT_ROOT", self.chats),
                         patch.object(cl, "VAULT_DIR", self.vault)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def day(self, d=24):
        return (self.chats / "2026" / "11" / f"2026-11-{d:02d}.md").read_text()


class TestSections(TempVault):
    def test_exchange_shares_one_heading_with_channel(self):
        cl.save_message("Steven", "hi", timestamp=at(8, 1))
        cl.save_message("Memento", "hello", timestamp=at(8, 3))
        text = self.day()
        self.assertEqual(text.count("## "), 1)
        self.assertIn("## 08:01 MST · signal\n**Steven:** hi\n\n**Memento:** hello", text)
        self.assertIn("channels: [signal]", text)

    def test_voice_heading_device_trigger_and_frontmatter(self):
        cl.save_message("Steven", "hi", timestamp=at(8, 1))
        cl.save_message("Steven", "time?", timestamp=at(9, 0), channel="voice",
                        device="boulder-office-voice", trigger="wake word")
        cl.save_message("Memento", "Nine.", timestamp=at(9, 0), channel="voice",
                        device="boulder-office-voice", trigger="wake word")
        text = self.day()
        self.assertIn("## 09:00 MST · voice · boulder-office-voice (wake word)\n"
                      "**Steven:** time?\n\n**Memento:** Nine.", text)
        self.assertIn("channels: [signal, voice]", text)
        self.assertIn("devices: [boulder-office-voice]\ntags: [chat]", text)

    def test_reply_on_other_channel_or_late_gets_own_heading(self):
        cl.save_message("Steven", "q", timestamp=at(8, 0))
        cl.save_message("Memento", "late", timestamp=at(8, 45))
        cl.save_message("Memento", "voice", timestamp=at(8, 46), channel="voice", device="d")
        self.assertEqual(self.day().count("## "), 3)

    def test_parse_returns_channel_fields(self):
        cl.save_message("Steven", "q", timestamp=at(8, 0), channel="voice",
                        device="boulder-desk-lcd", trigger="push-to-talk")
        cl.save_message("Steven", "s", timestamp=at(8, 5))
        msgs = cl.get_by_date("2026-11-24")
        self.assertEqual(msgs[0]["channel"], "voice")
        self.assertEqual(msgs[0]["device"], "boulder-desk-lcd")
        self.assertEqual(msgs[0]["trigger"], "push-to-talk")
        self.assertEqual(msgs[1]["channel"], "signal")
        self.assertNotIn("device", msgs[1])
        self.assertEqual(len(cl.search("s", channel="signal")), 1)

    def test_label_roundtrip(self):
        label = cl.section_label("voice", "boulder-epaper-mini", "push-to-talk")
        self.assertEqual(cl.parse_label(label), {"channel": "voice",
                         "device": "boulder-epaper-mini", "trigger": "push-to-talk"})
        self.assertEqual(cl.parse_label("signal")["device"], None)


class TestMigration(TempVault):
    def test_moves_and_tags_legacy_files(self):
        old = self.chats / "Signal" / "2026" / "10" / "2026-10-30.md"
        old.parent.mkdir(parents=True)
        (old.parent / "photos").mkdir()
        (old.parent / "photos" / "a.jpg").write_bytes(b"x")
        old.write_text("---\ndate: 2026-10-30\nchannel: signal\ntags: [chat]\n---\n\n"
                       "## 07:04 PDT\n**Steven:** ![[Chats/Signal/2026/10/photos/a.jpg]]\n")
        grp = self.chats / "Signal" / "Groups" / "G" / "2026" / "10" / "2026-10-30.md"
        grp.parent.mkdir(parents=True)
        grp.write_text("---\ndate: 2026-10-30\nchannel: signal\n---\n\n## 07:04 PDT\n**Ray:** yo\n")

        self.assertEqual(cl.migrate_from_signal(dry_run=True)["moved"], 3)
        self.assertTrue(old.exists())
        result = cl.migrate_from_signal()
        self.assertEqual((result["moved"], result["conflicts"]), (3, []))
        new = (self.chats / "2026" / "10" / "2026-10-30.md").read_text()
        self.assertIn("channels: [signal]", new)
        self.assertIn("## 07:04 PDT · signal\n", new)
        self.assertIn("![[Chats/2026/10/photos/a.jpg]]", new)
        self.assertTrue((self.chats / "2026" / "10" / "photos" / "a.jpg").exists())
        self.assertIn("· signal", (self.chats / "Groups" / "G" / "2026" / "10" /
                                   "2026-10-30.md").read_text())
        self.assertFalse((self.chats / "Signal").exists())
        self.assertEqual(cl.migrate_from_signal()["moved"], 0)


# ---------- reconciler -------------------------------------------------------

SIGNAL_KEY = "agent:main:signal:direct:uuid:x"


def _ev(seq, obj, ts_ms):
    return (seq, json.dumps(obj), ts_ms)


def _user(text, ts, transport=True):
    oc = {"senderIsOwner": True}
    if transport:
        oc.update(senderName="Steven Kasapi", transport={"channel": "signal"})
    return {"type": "message", "timestamp": ts,
            "message": {"role": "user", "content": text, "idempotencyKey": "k",
                        "__openclaw": oc}}


def _asst(text, ts):
    return {"type": "message", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class TestReconcile(TempVault):
    def setUp(self):
        super().setUp()
        self.db = Path(self._tmp.name) / "agent.sqlite"
        con = sqlite3.connect(self.db)
        con.executescript("""
            create table session_windows (session_id text, session_key text,
                created_at integer, display_name text);
            create table transcript_events (session_id text, seq integer, event_json text,
                created_at integer, event_zstd blob, event_utf8_bytes integer,
                navigation_json text);
        """)
        con.execute("insert into session_windows values ('s1', ?, 0, null)", (SIGNAL_KEY,))
        con.execute("insert into session_windows values ('hb', 'agent:main:main', 0, null)")
        voice = ("[voice · Office at boulder] what time is it\n\n(Spoken to you through the "
                 "voice satellite Office at boulder, after its wake word. No screen.)")
        ptt = ("[voice · boulder-desk-lcd at boulder] show it\n\n(Spoken to you by "
               "push-to-talk on display device boulder-desk-lcd at boulder.)")
        events = [
            _ev(1, _user("archived already", "2026-11-24T14:00:00Z"), 1),
            _ev(2, _user("Morning!", "2026-11-24T15:00:00Z"), 100),
            _ev(3, _asst("Morning Steven.", "2026-11-24T15:00:05Z"), 101),
            _ev(4, _user(voice, "2026-11-24T16:00:00Z", transport=False), 102),
            _ev(5, _asst("It's nine.", "2026-11-24T16:00:04Z"), 103),
            _ev(6, _user("Automation repair request from the scheduler", "2026-11-24T16:30:00Z",
                         transport=False), 104),
            _ev(7, _asst("NO_REPLY", "2026-11-24T16:30:02Z"), 105),
            _ev(8, _asst("should not appear", "2026-11-24T16:30:03Z"), 106),
            _ev(9, _user(ptt, "2026-11-24T17:00:00Z", transport=False), 107),
            _ev(10, _user('read <file name="lease---58e3e08f-dc53-4f9d-9e68-f191f8913ae0.pdf" '
                          'mime="application/pdf">LONG TEXT</file>', "2026-11-24T18:00:00Z"), 108),
        ]
        con.executemany("insert into transcript_events (session_id, seq, event_json, created_at)"
                        " values ('s1', ?, ?, ?)", events)
        con.execute("insert into transcript_events (session_id, seq, event_json, created_at) "
                    "values ('hb', 1, ?, 100)",
                    (json.dumps(_user("[OpenClaw heartbeat poll]", "2026-11-24T15:30:00Z",
                                      transport=False)),))
        con.commit()
        con.close()
        self.state = Path(self._tmp.name) / "state.json"
        self._rp = [patch.object(cr, "OPENCLAW_AGENT_DB", self.db),
                    patch.object(cr, "STATE_FILE", self.state),
                    patch.object(cr, "ALERT_FILE", self.state.with_suffix(".alert")),
                    patch.object(cr, "_local_zone", lambda: DEN),
                    patch.object(cr, "_notify", lambda *a, **k: None)]
        for p in self._rp:
            p.start()

    def tearDown(self):
        for p in self._rp:
            p.stop()
        super().tearDown()

    def test_first_run_requires_cutoff(self):
        with self.assertRaises(cr.ReconcileError):
            cr.reconcile()

    def test_backfill_tags_channels_and_skips_noise(self):
        res = cr.reconcile(init_cutoff="1970-01-01T00:00:00.050Z")
        text = self.day()
        self.assertNotIn("archived already", text)
        self.assertIn("## 08:00 MST · signal\n**Steven:** Morning!\n\n**Memento:** Morning Steven.",
                      text)
        self.assertIn("## 09:00 MST · voice · boulder-office-voice (wake word)\n"
                      "**Steven:** what time is it\n\n**Memento:** It's nine.", text)
        self.assertIn("voice · boulder-desk-lcd (push-to-talk)\n**Steven:** show it", text)
        self.assertNotIn("Spoken to you", text)
        self.assertNotIn("scheduler", text)
        self.assertNotIn("should not appear", text)
        self.assertNotIn("heartbeat", text)
        self.assertIn("**Steven:** read [attached file: lease.pdf]", text)
        self.assertNotIn("LONG TEXT", text)
        self.assertEqual(res["messages_written"], 6)
        self.assertEqual(cr.reconcile()["messages_written"], 0)

    def test_schema_change_fails_loudly(self):
        con = sqlite3.connect(self.db)
        con.execute("alter table transcript_events rename column event_json to body")
        con.commit()
        con.close()
        with self.assertRaisesRegex(cr.ReconcileError, "schema changed"):
            cr.reconcile(init_cutoff="1970-01-01T00:00:00Z")

    def test_main_exits_nonzero_and_alerts_once(self):
        sent = []
        with patch.object(cr, "_notify", lambda *a, **k: sent.append(a[0])), \
             patch.object(sys, "argv", ["chat_reconcile.py"]):
            self.assertEqual(cr.main(), 1)
            self.assertEqual(cr.main(), 1)
            self.assertEqual(sent, ["chat-reconcile failing"])
            cr.reconcile(init_cutoff="1970-01-01T00:00:00Z")
            self.assertEqual(cr.main(), 0)
            self.assertEqual(sent, ["chat-reconcile failing", "chat-reconcile recovered"])


if __name__ == "__main__":
    unittest.main()
