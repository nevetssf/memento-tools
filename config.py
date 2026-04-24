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

# Cellar — wine and spirits collection (default: $VAULT_DIR/Cellar)
CELLAR_DIR = Path(os.environ.get("MEMENTO_CELLAR_DIR", str(VAULT_DIR / "Cellar")))
CELLAR_DIRS = {
    "wine":    CELLAR_DIR / "Wine",
    "whiskey": CELLAR_DIR / "Whiskey",
    "gin":     CELLAR_DIR / "Gin",
    "vodka":   CELLAR_DIR / "Vodka",
}

# Signal target for notifications — set MEMENTO_SIGNAL_TARGET in your environment
# e.g. export MEMENTO_SIGNAL_TARGET=uuid:your-signal-uuid
SIGNAL_TARGET = os.environ.get("MEMENTO_SIGNAL_TARGET", "")
