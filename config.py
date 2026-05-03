"""
config.py — Central configuration for memento-tools.

All deployment-specific paths are resolved from environment variables.
Override any of these in your shell to adapt to a different deployment:

  export MEMENTO_VAULT_DIR=/path/to/your/obsidian/vault
  export MEMENTO_DB_PATH=/path/to/your/people.db
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

# People SQLite database (default: $VAULT_DIR/people.db)
DB_PATH = Path(os.environ.get("MEMENTO_DB_PATH", str(VAULT_DIR / "people.db")))

# People notes directory in Obsidian
PEOPLE_DIR = Path(os.environ.get("MEMENTO_PEOPLE_DIR", str(VAULT_DIR / "People")))

# Agent workspace files (configurable for non-OpenClaw deployments)
LOCATION_FILE = Path(os.environ.get("MEMENTO_LOCATION_FILE",
    str(Path.home() / ".openclaw/workspace/LOCATION.md")))
SOUL_FILE = Path(os.environ.get("MEMENTO_SOUL_FILE",
    str(Path.home() / ".openclaw/workspace/SOUL.md")))

# Obsidian templates directory
TEMPLATES_DIR = Path(os.environ.get("MEMENTO_TEMPLATES_DIR", str(VAULT_DIR / "Templates")))

# Cellar — wine and spirits collection (default: $VAULT_DIR/Cellar)
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

# Embedding model endpoint (OpenAI-compatible /v1/embeddings)
EMBED_MODEL_URL = os.environ.get("MEMENTO_EMBED_MODEL_URL", "http://192.168.6.19:1234/v1")
EMBED_MODEL_NAME = os.environ.get("MEMENTO_EMBED_MODEL_NAME", "text-embedding-qwen3-embedding-4b")
EMBED_DIMS = int(os.environ.get("MEMENTO_EMBED_DIMS", "2560"))

# Chat model endpoint for chunking / metadata extraction / reranking
CHAT_MODEL_URL = os.environ.get("MEMENTO_CHAT_MODEL_URL", "http://192.168.6.19:1234/v1")
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
