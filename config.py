"""
config.py — Central configuration for memento-tools.

All deployment-specific paths are resolved from environment variables.
Override any of these in your shell to adapt to a different deployment:

  export MEMENTO_VAULT_DIR=/path/to/your/obsidian/vault
  export MEMENTO_DATA_DIR=/path/to/your/memento-data
  export MEMENTO_DB_PATH=/path/to/your/people.db
  export MEMENTO_CELLAR_DB_PATH=/path/to/your/cellar.db
  export MEMENTO_LOCATION_FILE=/path/to/LOCATION.md
  export MEMENTO_SOUL_FILE=/path/to/SOUL.md
  export MEMENTO_SIGNAL_TARGET=uuid:your-signal-uuid
"""
import os
from pathlib import Path

# Obsidian vault root
VAULT_DIR = Path(os.environ.get("MEMENTO_VAULT_DIR", str(Path.home() / "obsidian-vault")))

# Journal directory (default: $VAULT_DIR/Journal)
JOURNAL_DIR = Path(os.environ.get("MEMENTO_JOURNAL_DIR", str(VAULT_DIR / "Journal")))

# SQLite databases live outside the vault so Obsidian Sync never copies a
# live (possibly mid-write) DB or its -wal/-shm files between devices.
DATA_DIR = Path(os.environ.get("MEMENTO_DATA_DIR", str(Path.home() / "memento-data")))

# People SQLite database (default: $DATA_DIR/people.db)
DB_PATH = Path(os.environ.get("MEMENTO_DB_PATH", str(DATA_DIR / "people.db")))

# Cellar SQLite database (default: $DATA_DIR/cellar.db)
CELLAR_DB_PATH = Path(os.environ.get("MEMENTO_CELLAR_DB_PATH", str(DATA_DIR / "cellar.db")))

# Face recognition DB + contact sheets (biometric data — never in the vault or git)
FACES_DB_PATH = Path(os.environ.get("MEMENTO_FACES_DB_PATH", str(DATA_DIR / "faces.db")))
FACES_REVIEW_DIR = Path(os.environ.get("MEMENTO_FACES_REVIEW_DIR", str(DATA_DIR / "faces-review")))

# Small runtime state files
LOCALTIME_ALIASES_FILE = Path(os.environ.get("MEMENTO_LOCALTIME_ALIASES_FILE",
    str(DATA_DIR / "localtime-aliases.json")))
CHAT_RECONCILE_STATE_FILE = Path(os.environ.get("MEMENTO_CHAT_RECONCILE_STATE_FILE",
    str(DATA_DIR / "chat_reconcile_state.json")))

# People notes directory in Obsidian
PEOPLE_DIR = Path(os.environ.get("MEMENTO_PEOPLE_DIR", str(VAULT_DIR / "People")))

# Agent workspace files (configurable for non-OpenClaw deployments)
LOCATION_FILE = Path(os.environ.get("MEMENTO_LOCATION_FILE",
    str(Path.home() / ".openclaw/workspace/LOCATION.md")))
SOUL_FILE = Path(os.environ.get("MEMENTO_SOUL_FILE",
    str(Path.home() / ".openclaw/workspace/SOUL.md")))
# OpenClaw's session index — chat_reconcile.py reads it to find agent transcripts
OPENCLAW_SESSIONS_JSON = Path(os.environ.get("MEMENTO_OPENCLAW_SESSIONS_JSON",
    str(Path.home() / ".openclaw/agents/main/sessions/sessions.json")))

# Obsidian templates directory
TEMPLATES_DIR = Path(os.environ.get("MEMENTO_TEMPLATES_DIR", str(VAULT_DIR / "Templates")))

# Cellar — wine and spirits collection (default: $VAULT_DIR/Cellar)
# Paper notebooks (notebook.py / notebook-mcp.py). Staging holds candidate
# pages awaiting review; it lives outside the vault so it never syncs.
NOTEBOOKS_DIR = Path(os.environ.get("MEMENTO_NOTEBOOKS_DIR", str(VAULT_DIR / "Notebooks")))
NOTEBOOK_STAGING_DIR = Path(os.environ.get("MEMENTO_NOTEBOOK_STAGING_DIR",
                                           str(Path.home() / ".cache" / "memento" / "notebook-staging")))

CELLAR_DIR = Path(os.environ.get("MEMENTO_CELLAR_DIR", str(VAULT_DIR / "Cellar")))
CELLAR_DIRS = {
    "wine":    CELLAR_DIR / "Wine",
    "whiskey": CELLAR_DIR / "Whiskey",
    "gin":     CELLAR_DIR / "Gin",
    "vodka":   CELLAR_DIR / "Vodka",
    "tequila": CELLAR_DIR / "Tequila",
    "mezcal":  CELLAR_DIR / "Mezcal",
    "rum":     CELLAR_DIR / "Rum",
    "port":    CELLAR_DIR / "Port",
}

# Signal target for notifications — set MEMENTO_SIGNAL_TARGET in your environment
# e.g. export MEMENTO_SIGNAL_TARGET=uuid:your-signal-uuid
SIGNAL_TARGET = os.environ.get("MEMENTO_SIGNAL_TARGET", "")

# Vector embedding index for the vault
EMBED_DB_PATH = Path(os.environ.get("MEMENTO_EMBED_DB_PATH",
    str(Path.home() / "obsidian-vault-index" / "vault-embed.db")))

# Local embedding server (embed-server.py) — serves voyage-4-nano on CPU so indexing
# doesn't depend on the DGX being reachable. Voyage/OpenAI-compatible /v1/embeddings.
EMBED_SERVER_HOST = os.environ.get("MEMENTO_EMBED_SERVER_HOST", "127.0.0.1")
EMBED_SERVER_PORT = int(os.environ.get("MEMENTO_EMBED_SERVER_PORT", "8790"))
EMBED_SERVER_MODEL = os.environ.get("MEMENTO_EMBED_SERVER_MODEL", "voyageai/voyage-4-nano")
# Pinned HF revision: the model ships trust_remote_code modeling code, so never float it.
EMBED_SERVER_MODEL_REVISION = os.environ.get("MEMENTO_EMBED_SERVER_MODEL_REVISION",
    "67fabc9bef010dabc5f6024aa1b1b6b93410426f")
# "cpu" here; "cuda" for the GPU copy of the same server on the DGX.
EMBED_SERVER_DEVICE = os.environ.get("MEMENTO_EMBED_SERVER_DEVICE", "cpu")
# Optional upstream embed-server (same model + revision, e.g. the DGX GPU copy). When set,
# document/untyped batches are forwarded there and embedded locally only if it fails;
# queries are always embedded locally (fast, no network dependency). Empty = local only.
EMBED_UPSTREAM_URL = os.environ.get("MEMENTO_EMBED_UPSTREAM_URL", "")
EMBED_UPSTREAM_TIMEOUT = float(os.environ.get("MEMENTO_EMBED_UPSTREAM_TIMEOUT", "30"))
# After an upstream failure, skip it for this many seconds before trying again.
EMBED_UPSTREAM_COOLDOWN = float(os.environ.get("MEMENTO_EMBED_UPSTREAM_COOLDOWN", "60"))

# Embedding model endpoint (OpenAI-compatible /v1/embeddings). Defaults to the local
# embed-server. To go back to the DGX: http://dgx-spark:1234/v1,
# text-embedding-qwen3-embedding-4b, 2560 dims — then `vault_embed.py --reembed`.
EMBED_MODEL_URL = os.environ.get("MEMENTO_EMBED_MODEL_URL",
    f"http://{EMBED_SERVER_HOST}:{EMBED_SERVER_PORT}/v1")
EMBED_MODEL_NAME = os.environ.get("MEMENTO_EMBED_MODEL_NAME", "voyage-4-nano")
EMBED_DIMS = int(os.environ.get("MEMENTO_EMBED_DIMS", "1024"))
# Max texts per /embeddings request. The CPU embed-server serializes inference, so a
# big request blocks every other client (OpenClaw memory search times out at 120 s);
# small requests let queries interleave and keep each one well under the timeout.
EMBED_REQUEST_BATCH = int(os.environ.get("MEMENTO_EMBED_REQUEST_BATCH", "8"))

# Chat model endpoint for chunking / metadata extraction / reranking
CHAT_MODEL_URL = os.environ.get("MEMENTO_CHAT_MODEL_URL", "http://dgx-spark:1234/v1")
CHAT_MODEL_NAME = os.environ.get("MEMENTO_CHAT_MODEL_NAME", "qwen3.6-35b-a3b@q8_k_xl")
# Optional bearer token. LM Studio doesn't require auth; vLLM does.
CHAT_MODEL_API_KEY = os.environ.get("MEMENTO_CHAT_MODEL_API_KEY", "")

# Todoist API token — get yours at https://todoist.com/app/settings/integrations/developer
TODOIST_TOKEN = os.environ.get("MEMENTO_TODOIST_TOKEN", "")

# Anthropic API — used by pdf2md.py --backend claude.
# Optional: set ANTHROPIC_API_KEY in your shell, or MEMENTO_ANTHROPIC_API_KEY for an
# isolated key. The default model is Haiku 4.5 (cheap, very good for OCR/extraction).
ANTHROPIC_API_KEY = os.environ.get("MEMENTO_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("MEMENTO_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
