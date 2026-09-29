"""Index identity tracking and --reembed for vault_embed.py (no embedding server needed)."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vault_embed as ve


def fake_vectors(texts, input_type="document"):
    return [[0.5] * ve.EMBED_DIMS for _ in texts]


class IdentityTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        patches = [
            mock.patch.object(ve, "EMBED_DB_PATH", tmp / "vault-embed.db"),
            mock.patch.object(ve, "LOCK_FILE", tmp / "index.lock"),
            mock.patch.object(ve, "PROGRESS_FILE", tmp / "index-progress.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.con = ve.connect()
        self.addCleanup(self.con.close)
        self.addCleanup(self.tmp.cleanup)

    def make_old_model_index(self, n_chunks=5, old_dims=8):
        """Simulate an index built by a different model with a different width."""
        self.con.execute("DROP TABLE embeddings")
        self.con.execute("CREATE VIRTUAL TABLE embeddings USING vec0("
                         f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{old_dims}])")
        self.con.execute("INSERT INTO files (path, file_type, content_hash) VALUES ('a.md','md','h')")
        for i in range(n_chunks):
            cur = self.con.execute(
                "INSERT INTO chunks (file_id, chunk_index, content, metadata) VALUES (1, ?, ?, ?)",
                (i, f"chunk {i}", '{"topics": ["kept"]}'))
            self.con.execute("INSERT INTO embeddings (chunk_id, embedding) VALUES (?, ?)",
                             (cur.lastrowid, str([0.1] * old_dims)))
        self.con.commit()

    def vector_count(self):
        return self.con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]


class TestIdentity(IdentityTestBase):
    def test_fresh_index_adopts_configured_model(self):
        self.assertEqual(ve.index_identity(self.con), ve.CURRENT_IDENTITY)
        ve.check_index_identity(self.con)  # no raise

    def test_width_mismatch_is_rejected(self):
        self.make_old_model_index()
        self.assertIsNone(ve.index_identity(self.con))
        with self.assertRaisesRegex(RuntimeError, "--reembed"):
            ve.check_index_identity(self.con)

    def test_recorded_other_model_is_rejected_even_at_same_width(self):
        ve._meta_set(self.con, "embed_identity", f"some-other-model@{ve.EMBED_DIMS}")
        with self.assertRaisesRegex(RuntimeError, "some-other-model"):
            ve.check_index_identity(self.con)


class TestReembed(IdentityTestBase):
    def test_reembed_switches_model_and_keeps_chunks(self):
        self.make_old_model_index(n_chunks=5)
        with mock.patch.object(ve, "embed_batch", side_effect=fake_vectors):
            r = ve.reembed(self.con, batch_size=2)
        self.assertEqual(r["embedded"], 5)
        self.assertEqual(ve._table_dims(self.con), ve.EMBED_DIMS)
        self.assertEqual(self.vector_count(), 5)
        self.assertEqual(ve.index_identity(self.con), ve.CURRENT_IDENTITY)
        self.assertIsNone(ve._meta_get(self.con, "reembed_target"))
        metas = {r[0] for r in self.con.execute("SELECT metadata FROM chunks")}
        self.assertEqual(metas, {'{"topics": ["kept"]}'})
        self.assertFalse(ve.LOCK_FILE.exists())

    def test_interrupted_reembed_resumes_without_starting_over(self):
        self.make_old_model_index(n_chunks=5)
        calls = {"n": 0}

        def flaky(texts, input_type="document"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("embed server went away")
            return fake_vectors(texts)

        with mock.patch.object(ve, "embed_batch", side_effect=flaky):
            with self.assertRaises(OSError):
                ve.reembed(self.con, batch_size=2)
        self.assertEqual(self.vector_count(), 2)
        self.assertFalse(ve.LOCK_FILE.exists())
        # Partial index: search may use it, indexing may not.
        with self.assertRaises(RuntimeError):
            ve.check_index_identity(self.con)
        ve.check_index_identity(self.con, allow_partial=True)

        seen = []
        with mock.patch.object(ve, "embed_batch",
                               side_effect=lambda t, input_type="document": seen.extend(t) or fake_vectors(t)):
            r = ve.reembed(self.con, batch_size=2)
        self.assertEqual(r["embedded"], 3)            # only the missing chunks
        self.assertEqual(seen, ["chunk 2", "chunk 3", "chunk 4"])
        self.assertEqual(self.vector_count(), 5)
        self.assertEqual(ve.index_identity(self.con), ve.CURRENT_IDENTITY)

    def test_reembed_on_current_index_only_fills_gaps(self):
        with mock.patch.object(ve, "embed_batch", side_effect=fake_vectors) as m:
            r = ve.reembed(self.con)
        self.assertEqual(r["embedded"], 0)
        m.assert_not_called()

    def test_reembed_respects_lock(self):
        ve.LOCK_FILE.write_text(str(__import__("os").getpid()))
        with self.assertRaisesRegex(RuntimeError, "in progress"):
            ve.reembed(self.con)


class TestEnrichWhenChatModelDown(IdentityTestBase):
    def add_pending_chunks(self, n):
        self.con.execute("INSERT INTO files (path, file_type, content_hash) VALUES ('b.md','md','h2')")
        for i in range(n):
            self.con.execute("INSERT INTO chunks (file_id, chunk_index, content) VALUES (1, ?, ?)",
                             (i, f"pending {i}"))
        self.con.commit()

    def null_count(self):
        return self.con.execute("SELECT COUNT(*) FROM chunks WHERE metadata IS NULL").fetchone()[0]

    def test_unreachable_model_skips_without_stamping(self):
        self.add_pending_chunks(5)
        with mock.patch.object(ve, "chat_model_unreachable", return_value="chat model unreachable at x"), \
             mock.patch.object(ve, "chat") as chat:
            r = ve.enrich_metadata(self.con)
        chat.assert_not_called()
        self.assertEqual(r["skipped"], "chat model unreachable at x")
        self.assertEqual((r["processed"], r["remaining"]), (0, 5))
        self.assertEqual(self.null_count(), 5)

    def test_outage_mid_pass_stops_and_leaves_rest_pending(self):
        self.add_pending_chunks(5)
        replies = iter(['[{"people":[],"topics":["a"],"action_items":[],"dates_mentioned":[]},'
                        '{"people":[],"topics":["b"],"action_items":[],"dates_mentioned":[]}]'])

        def chat(prompt, **kw):
            try:
                return next(replies)
            except StopIteration:
                raise ve.ChatUnreachable("chat model unreachable at x: timed out")

        with mock.patch.object(ve, "chat_model_unreachable", return_value=None), \
             mock.patch.object(ve, "chat", side_effect=chat):
            r = ve.enrich_metadata(self.con, batch_size=2)
        self.assertEqual(r["processed"], 2)
        self.assertEqual(r["remaining"], 3)
        self.assertIn("timed out", r["skipped"])
        self.assertEqual(self.null_count(), 3)
        self.assertFalse(ve.LOCK_FILE.exists())

    def test_model_errors_still_fall_back_to_empty_metadata(self):
        """A reachable model that answers badly keeps the old behaviour (not an outage)."""
        self.add_pending_chunks(2)
        with mock.patch.object(ve, "chat_model_unreachable", return_value=None), \
             mock.patch.object(ve, "chat", return_value="not json"):
            r = ve.enrich_metadata(self.con, batch_size=2)
        self.assertNotIn("skipped", r)
        self.assertEqual(self.null_count(), 0)

    def test_chat_converts_network_errors_only(self):
        import urllib.error
        with mock.patch.object(ve, "_post_json", side_effect=urllib.error.URLError("No route to host")):
            with self.assertRaises(ve.ChatUnreachable):
                ve.chat("x")
        http_err = urllib.error.HTTPError("u", 404, "model not found", {}, None)
        with mock.patch.object(ve, "_post_json", side_effect=http_err):
            with self.assertRaises(urllib.error.HTTPError):
                ve.chat("x")


class TestEmbedRequests(unittest.TestCase):
    def test_query_and_document_input_types(self):
        sent = []

        def fake_post(url, body, timeout=120, api_key=""):
            sent.append(body)
            return {"data": [{"embedding": [0.0]} for _ in
                             (body["input"] if isinstance(body["input"], list) else [body["input"]])]}

        with mock.patch.object(ve, "_post_json", side_effect=fake_post):
            ve.embed("find my notes")
            ve.embed_batch(["doc one", "doc two"])
        self.assertEqual(sent[0]["input_type"], "query")
        self.assertEqual(sent[1]["input_type"], "document")
        self.assertEqual(sent[0]["model"], ve.EMBED_MODEL_NAME)


if __name__ == "__main__":
    unittest.main()
