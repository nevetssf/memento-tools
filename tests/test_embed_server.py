"""Request validation for embed-server.py (no model load; runs in the main venv)."""
import importlib.util
import json
import sys
import unittest
from unittest import mock
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


es = _load("embed_server", "embed-server.py")


class TestParseRequest(unittest.TestCase):
    def test_single_string_defaults(self):
        inputs, input_type, dim = es.parse_request({"model": "voyage-4-nano", "input": "hi"})
        self.assertEqual((inputs, input_type, dim), (["hi"], None, 1024))

    def test_provider_prefixes_accepted(self):
        for model in ("voyage/voyage-4-nano", "voyageai/voyage-4-nano"):
            es.parse_request({"model": model, "input": ["a"]})

    def test_query_and_dimension_aliases(self):
        self.assertEqual(
            es.parse_request({"input": ["a"], "input_type": "query", "output_dimension": 512})[1:],
            ("query", 512))
        self.assertEqual(es.parse_request({"input": ["a"], "dimensions": 256})[2], 256)

    def test_wrong_model_is_404(self):
        with self.assertRaises(es.BadRequest) as cm:
            es.parse_request({"model": "text-embedding-qwen3-embedding-4b", "input": "a"})
        self.assertEqual(cm.exception.status, 404)

    def test_invalid_requests_are_400(self):
        bad = [
            {"input": []},
            {"input": [1, 2]},
            {"input": "a", "input_type": "passage"},
            {"input": "a", "output_dimension": 300},
            {"input": ["a"] * (es.MAX_INPUTS + 1)},
        ]
        for body in bad:
            with self.subTest(body=str(body)[:60]):
                with self.assertRaises(es.BadRequest) as cm:
                    es.parse_request(body)
                self.assertEqual(cm.exception.status, 400)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(n, dim=4, model=None):
    return _Resp({"model": model or es.MODEL_ID,
                  "data": [{"index": i, "embedding": [0.5] * dim} for i in range(n)]})


class TestUpstreamFailover(unittest.TestCase):
    def setUp(self):
        self.up = es.Upstream("http://dgx:8790/v1", timeout=5, cooldown=60)
        self.local = mock.patch.object(es, "encode",
                                       side_effect=lambda x, t, d: [[0.1] * d for _ in x])
        self.local_mock = self.local.start()
        self.addCleanup(self.local.stop)

    def _run(self, inputs, input_type, urlopen):
        with mock.patch.object(es.urllib.request, "urlopen", urlopen):
            return es.embed_with_failover(inputs, input_type, 4, upstream=self.up)

    def test_documents_go_upstream(self):
        vecs, where = self._run(["a", "b"], "document", mock.Mock(return_value=_ok(2)))
        self.assertEqual(where, "upstream")
        self.assertEqual(vecs, [[0.5] * 4] * 2)
        self.local_mock.assert_not_called()

    def test_untyped_goes_upstream(self):
        self.assertEqual(self._run(["a"], None, mock.Mock(return_value=_ok(1)))[1], "upstream")

    def test_queries_always_local(self):
        urlopen = mock.Mock(return_value=_ok(1))
        vecs, where = self._run(["q"], "query", urlopen)
        self.assertEqual(where, "local")
        urlopen.assert_not_called()

    def test_unreachable_falls_back_and_cools_down(self):
        down = mock.Mock(side_effect=es.urllib.error.URLError("No route to host"))
        self.assertEqual(self._run(["a"], "document", down)[1], "local")
        self.assertFalse(self.up.status()["available"])
        # within the cooldown the upstream isn't even tried
        again = mock.Mock(return_value=_ok(1))
        self.assertEqual(self._run(["a"], "document", again)[1], "local")
        again.assert_not_called()

    def test_retries_after_cooldown(self):
        self._run(["a"], "document", mock.Mock(side_effect=TimeoutError("timed out")))
        self.up.skip_until = 0
        self.assertEqual(self._run(["a"], "document", mock.Mock(return_value=_ok(1)))[1],
                         "upstream")

    def test_malformed_responses_fall_back(self):
        bad = {
            "http 500": mock.Mock(side_effect=es.urllib.error.HTTPError(
                "u", 500, "boom", {}, None)),
            "wrong count": mock.Mock(return_value=_ok(1)),
            "wrong dims": mock.Mock(return_value=_ok(2, dim=3)),
            "wrong model": mock.Mock(return_value=_ok(2, model="qwen3-embedding")),
        }
        for name, urlopen in bad.items():
            with self.subTest(name):
                self.up.skip_until = 0
                vecs, where = self._run(["a", "b"], "document", urlopen)
                self.assertEqual(where, "local")
                self.assertEqual(vecs, [[0.1] * 4] * 2)

    def test_no_upstream_configured_is_local(self):
        self.up = es.Upstream("", timeout=5, cooldown=60)
        urlopen = mock.Mock(return_value=_ok(1))
        self.assertEqual(self._run(["a"], "document", urlopen)[1], "local")
        urlopen.assert_not_called()
        self.assertEqual(self.up.status(), {"configured": False})


if __name__ == "__main__":
    unittest.main()
