"""
embed-server.py — local CPU embedding server for the vault and OpenClaw memory indexes.

Serves voyage-4-nano (open-weight, Apache 2.0) over a Voyage/OpenAI-compatible
`POST /v1/embeddings`, so `vault_embed.py` and OpenClaw's `voyage` memory provider can
embed without the DGX. Request fields:

  model        "voyage-4-nano" (a "voyage/" or "voyageai/" prefix is accepted)
  input        string or list of strings
  input_type   "query" | "document" | null   (Voyage semantics: selects the retrieval
               prompt; null embeds the raw text)
  output_dimension / dimensions   256 | 512 | 1024 | 2048 (default 1024)

Also `GET /v1/models` and `GET /health`. Binds to loopback by default.

Runs from its own venv (torch is too heavy for the main one):
  uv venv -p 3.12 .venv-embed
  VIRTUAL_ENV=.venv-embed uv pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.0+cpu
  VIRTUAL_ENV=.venv-embed uv pip install -r requirements-embed.txt
  ./.venv-embed/bin/python embed-server.py
"""
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    EMBED_SERVER_HOST, EMBED_SERVER_PORT, EMBED_SERVER_MODEL, EMBED_SERVER_MODEL_REVISION,
)

MODEL_ID = EMBED_SERVER_MODEL.rsplit("/", 1)[-1]          # "voyage-4-nano"
ALLOWED_DIMS = (256, 512, 1024, 2048)
DEFAULT_DIM = 1024
MAX_SEQ_TOKENS = 4096     # vault chunks are ≤ ~1k tokens; caps memory on pathological input
MAX_INPUTS = 1000         # per request
BATCH_SIZE = 16

log = logging.getLogger("embed-server")

_model = None
_model_lock = threading.Lock()   # torch already uses every core; serialize inference


def load_model():
    global _model
    from sentence_transformers import SentenceTransformer
    t = time.time()
    _model = SentenceTransformer(
        EMBED_SERVER_MODEL,
        revision=EMBED_SERVER_MODEL_REVISION,
        trust_remote_code=True,
        device="cpu",
    )
    _model.max_seq_length = MAX_SEQ_TOKENS
    log.info("loaded %s@%s in %.1fs", EMBED_SERVER_MODEL,
             EMBED_SERVER_MODEL_REVISION[:12], time.time() - t)


def encode(texts: list[str], input_type: str | None, dim: int) -> list[list[float]]:
    kwargs = dict(batch_size=BATCH_SIZE, truncate_dim=dim, normalize_embeddings=True,
                  convert_to_numpy=True, show_progress_bar=False)
    with _model_lock:
        if input_type == "query":
            vecs = _model.encode_query(texts, **kwargs)
        elif input_type == "document":
            vecs = _model.encode_document(texts, **kwargs)
        else:
            vecs = _model.encode(texts, **kwargs)
    return vecs.tolist()


class BadRequest(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def parse_request(body: dict) -> tuple[list[str], str | None, int]:
    model = str(body.get("model") or MODEL_ID)
    for prefix in ("voyageai/", "voyage/"):
        model = model.removeprefix(prefix)
    if model != MODEL_ID:
        raise BadRequest(404, f"model {body.get('model')!r} not served here (serving {MODEL_ID!r})")

    inputs = body.get("input")
    if isinstance(inputs, str):
        inputs = [inputs]
    if not isinstance(inputs, list) or not inputs or not all(isinstance(s, str) for s in inputs):
        raise BadRequest(400, "input must be a non-empty string or list of strings")
    if len(inputs) > MAX_INPUTS:
        raise BadRequest(400, f"at most {MAX_INPUTS} inputs per request")

    input_type = body.get("input_type")
    if input_type not in (None, "query", "document"):
        raise BadRequest(400, "input_type must be 'query', 'document', or null")

    dim = body.get("output_dimension") or body.get("dimensions") or DEFAULT_DIM
    if dim not in ALLOWED_DIMS:
        raise BadRequest(400, f"output_dimension must be one of {ALLOWED_DIMS}")
    return inputs, input_type, dim


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "model": MODEL_ID})
        elif self.path.rstrip("/") == "/v1/models":
            self._send(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "owned_by": "voyageai"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/embeddings":
            self._send(404, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise BadRequest(400, "request body must be a JSON object")
            inputs, input_type, dim = parse_request(body)
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid JSON"}})
            return
        except BadRequest as e:
            self._send(e.status, {"error": {"message": str(e)}})
            return

        t = time.time()
        try:
            vectors = encode(inputs, input_type, dim)
        except Exception as e:
            log.exception("encode failed")
            self._send(500, {"error": {"message": f"{type(e).__name__}: {e}"}})
            return
        log.info("embedded %d input(s) type=%s dim=%d in %.2fs",
                 len(inputs), input_type, dim, time.time() - t)
        # Token usage is approximate (4 chars ≈ 1 token), matching vault_embed's estimate.
        tokens = sum(max(1, len(s) // 4) for s in inputs)
        self._send(200, {
            "object": "list",
            "data": [{"object": "embedding", "embedding": v, "index": i}
                     for i, v in enumerate(vectors)],
            "model": MODEL_ID,
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        })

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    load_model()
    server = ThreadingHTTPServer((EMBED_SERVER_HOST, EMBED_SERVER_PORT), Handler)
    log.info("listening on http://%s:%d/v1", EMBED_SERVER_HOST, EMBED_SERVER_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
