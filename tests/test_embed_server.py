"""Request validation for embed-server.py (no model load; runs in the main venv)."""
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
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


if __name__ == "__main__":
    unittest.main()
