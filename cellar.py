"""cellar.py — SQLite-backed cellar database.

Mirrors the people-db pattern: structured data is canonical in SQLite at
~/obsidian-vault/cellar.db, long-form prose lives in linked Obsidian
notes whose paths are stored on the row's `obsidian_file` field.

Three tables: producers, bottles, tastings. Each row optionally points
at a vault note. Notes are created lazily — only on first
read_*_note / append_*_note that actually writes content.

Tables and key relationships:
    producers (id, name UNIQUE, region, country, ...)
    bottles   (producer_id → producers.id, name, type, ...,
               UNIQUE(producer_id, name))
    tastings  (bottle_id → bottles.id, tasted_at, rating 0-100, nose,
               palate, finish, ...)
"""

from __future__ import annotations

import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import VAULT_DIR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = Path(VAULT_DIR) / "cellar.db"
CELLAR_ROOT = Path(VAULT_DIR) / "Cellar"

# Recommended type vocabulary. The DB doesn't restrict via CHECK so new
# categories (e.g. "amaro", "brandy") can be added freely; this list is
# used by get_types() and for migration normalization.
KNOWN_TYPES = [
    "gin", "mezcal", "port", "rum", "tequila", "vodka", "whiskey", "wine",
]

VALID_STATUSES = {"wishlist", "acquired", "in-cellar", "consumed", "gifted"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS producers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    region        TEXT,
    country       TEXT,
    website       TEXT,
    notes         TEXT,
    obsidian_file TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bottles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    producer_id     INTEGER NOT NULL REFERENCES producers(id) ON DELETE CASCADE,
    name            TEXT NOT NULL COLLATE NOCASE,
    type            TEXT NOT NULL,
    expression      TEXT,
    style           TEXT,
    varietal        TEXT,
    vintage         INTEGER,
    age             TEXT,
    abv             TEXT,
    cask_type       TEXT,
    botanicals      TEXT,
    price           TEXT,
    acquired_date   TEXT,
    status          TEXT NOT NULL DEFAULT 'in-cellar'
                       CHECK (status IN ('wishlist','acquired','in-cellar','consumed','gifted')),
    quantity        INTEGER NOT NULL DEFAULT 1,
    would_buy_again INTEGER,
    notes           TEXT,
    obsidian_file   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(producer_id, name)
);
CREATE INDEX IF NOT EXISTS idx_bottles_producer ON bottles(producer_id);
CREATE INDEX IF NOT EXISTS idx_bottles_type     ON bottles(type);
CREATE INDEX IF NOT EXISTS idx_bottles_status   ON bottles(status);

CREATE TABLE IF NOT EXISTS tastings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bottle_id     INTEGER NOT NULL REFERENCES bottles(id) ON DELETE CASCADE,
    tasted_at     TEXT,
    rating        INTEGER CHECK (rating IS NULL OR (rating >= 0 AND rating <= 100)),
    nose          TEXT,
    palate        TEXT,
    finish        TEXT,
    color         TEXT,
    food_pairings TEXT,
    location      TEXT,
    notes         TEXT,
    obsidian_file TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tastings_bottle ON tastings(bottle_id);
CREATE INDEX IF NOT EXISTS idx_tastings_date   ON tastings(tasted_at);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the cellar DB; auto-create schema on first use."""
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), isolation_level=None)  # autocommit; we manage tx explicitly
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA_SQL)
    return con


@contextmanager
def transaction(con: sqlite3.Connection):
    con.execute("BEGIN")
    try:
        yield
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

# Filesystem-unsafe characters; replace with underscore. Keep spaces, ', etc.
_UNSAFE_FS_RE = re.compile(r'[/\x00:<>"\\|?*]')


def safe_name(s: str) -> str:
    """Make a string safe to use as a filename component."""
    s = _UNSAFE_FS_RE.sub("_", s)
    # Strip leading/trailing dots and whitespace (Obsidian doesn't love either)
    return s.strip(" ._") or "untitled"


def producer_note_path(producer_name: str) -> str:
    """Canonical vault path for a producer's long-form note."""
    return f"Cellar/Producer/{safe_name(producer_name)}.md"


def bottle_note_path(type_: str, producer_name: str, bottle_name: str) -> str:
    """Canonical vault path for a bottle's long-form note."""
    type_part = safe_name(type_).capitalize()
    return (
        f"Cellar/{type_part}/"
        f"{safe_name(producer_name)}/{safe_name(bottle_name)}.md"
    )


def absolute(vault_relative: str) -> Path:
    """Resolve a vault-relative path to an absolute Path."""
    return Path(VAULT_DIR) / vault_relative


# ---------------------------------------------------------------------------
# Producer CRUD
# ---------------------------------------------------------------------------

def get_producer(con: sqlite3.Connection, name_or_id: str | int) -> sqlite3.Row | None:
    """Look up a producer by id (int) or by case-insensitive name."""
    if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
        return con.execute("SELECT * FROM producers WHERE id=?", (int(name_or_id),)).fetchone()
    return con.execute("SELECT * FROM producers WHERE name=? COLLATE NOCASE", (name_or_id,)).fetchone()


def find_producers(con: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    """Fuzzy substring search on producer name."""
    pattern = f"%{query}%"
    return con.execute(
        "SELECT * FROM producers WHERE name LIKE ? COLLATE NOCASE ORDER BY name",
        (pattern,),
    ).fetchall()


def list_producers(con: sqlite3.Connection, type_: str | None = None) -> list[sqlite3.Row]:
    if type_:
        # producers that have at least one bottle of the given type
        return con.execute(
            """
            SELECT DISTINCT p.* FROM producers p
              JOIN bottles b ON b.producer_id = p.id
              WHERE b.type = ? COLLATE NOCASE
              ORDER BY p.name
            """,
            (type_,),
        ).fetchall()
    return con.execute("SELECT * FROM producers ORDER BY name").fetchall()


def add_producer(
    con: sqlite3.Connection,
    name: str,
    *,
    region: str | None = None,
    country: str | None = None,
    website: str | None = None,
    notes: str | None = None,
    obsidian_file: str | None = None,
) -> int:
    """Insert a new producer; returns id. Idempotent: returns existing id if name matches."""
    existing = get_producer(con, name)
    if existing:
        return existing["id"]
    if obsidian_file is None:
        obsidian_file = producer_note_path(name)
    con.execute(
        """
        INSERT INTO producers (name, region, country, website, notes, obsidian_file)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, region, country, website, notes, obsidian_file),
    )
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_producer(con: sqlite3.Connection, name_or_id: str | int, **fields) -> int:
    """Update arbitrary producer fields. Returns rowcount affected."""
    p = get_producer(con, name_or_id)
    if not p:
        raise KeyError(f"No producer matching {name_or_id!r}")
    cols = [k for k in fields if k in {
        "name", "region", "country", "website", "notes", "obsidian_file",
    }]
    if not cols:
        return 0
    sets = ", ".join(f"{c}=?" for c in cols) + ", updated_at=datetime('now')"
    vals = [fields[c] for c in cols] + [p["id"]]
    return con.execute(f"UPDATE producers SET {sets} WHERE id=?", vals).rowcount


def delete_producer(con: sqlite3.Connection, name_or_id: str | int) -> int:
    """Delete a producer (CASCADE deletes bottles + tastings)."""
    p = get_producer(con, name_or_id)
    if not p:
        return 0
    return con.execute("DELETE FROM producers WHERE id=?", (p["id"],)).rowcount


# ---------------------------------------------------------------------------
# Bottle CRUD
# ---------------------------------------------------------------------------

BOTTLE_FIELDS = {
    "name", "type", "expression", "style", "varietal", "vintage",
    "age", "abv", "cask_type", "botanicals", "price",
    "acquired_date", "status", "quantity", "would_buy_again",
    "notes", "obsidian_file",
}


def get_bottle(con: sqlite3.Connection, name_or_id: str | int,
               producer: str | int | None = None) -> sqlite3.Row | None:
    """Look up a bottle by id, or by name (optionally constrained by producer)."""
    if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
        return con.execute("SELECT * FROM bottles WHERE id=?", (int(name_or_id),)).fetchone()
    if producer is not None:
        p = get_producer(con, producer)
        if not p:
            return None
        return con.execute(
            "SELECT * FROM bottles WHERE producer_id=? AND name=? COLLATE NOCASE",
            (p["id"], name_or_id),
        ).fetchone()
    rows = con.execute(
        "SELECT * FROM bottles WHERE name=? COLLATE NOCASE", (name_or_id,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise ValueError(
            f"Bottle name {name_or_id!r} is ambiguous across {len(rows)} producers; "
            "pass `producer` to disambiguate."
        )
    return None


def find_bottles(con: sqlite3.Connection, query: str,
                 type_: str | None = None,
                 status: str | None = None) -> list[sqlite3.Row]:
    """Fuzzy substring on bottle.name OR producer.name."""
    pattern = f"%{query}%"
    sql = """
        SELECT b.*, p.name AS producer_name
          FROM bottles b
          JOIN producers p ON b.producer_id = p.id
          WHERE (b.name LIKE ? COLLATE NOCASE OR p.name LIKE ? COLLATE NOCASE)
    """
    params: list = [pattern, pattern]
    if type_:
        sql += " AND b.type = ? COLLATE NOCASE"
        params.append(type_)
    if status:
        sql += " AND b.status = ?"
        params.append(status)
    sql += " ORDER BY p.name, b.name"
    return con.execute(sql, params).fetchall()


def add_bottle(con: sqlite3.Connection, producer: str | int, name: str, type_: str,
               **fields) -> int:
    """Insert a bottle under the given producer. Returns id."""
    p = get_producer(con, producer)
    if not p:
        raise KeyError(f"No producer matching {producer!r} — add the producer first.")

    # Defaults
    fields.setdefault("status", "in-cellar")
    fields.setdefault("quantity", 1)
    if fields["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    if "obsidian_file" not in fields or not fields["obsidian_file"]:
        fields["obsidian_file"] = bottle_note_path(type_, p["name"], name)

    # Coerce booleans to 0/1
    if "would_buy_again" in fields and fields["would_buy_again"] is not None:
        fields["would_buy_again"] = 1 if fields["would_buy_again"] else 0

    # Build INSERT
    cols = ["producer_id", "name", "type"] + [k for k in fields if k in BOTTLE_FIELDS - {"name", "type"}]
    vals = [p["id"], name, type_] + [fields[c] for c in cols[3:]]
    placeholders = ",".join("?" * len(cols))
    con.execute(f"INSERT INTO bottles ({','.join(cols)}) VALUES ({placeholders})", vals)
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_bottle(con: sqlite3.Connection, name_or_id: str | int, **fields) -> int:
    """Update arbitrary bottle fields."""
    b = get_bottle(con, name_or_id) if isinstance(name_or_id, (int, str)) else name_or_id
    if not b:
        raise KeyError(f"No bottle matching {name_or_id!r}")
    if "status" in fields and fields["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    if "would_buy_again" in fields and fields["would_buy_again"] is not None:
        fields["would_buy_again"] = 1 if fields["would_buy_again"] else 0
    cols = [k for k in fields if k in BOTTLE_FIELDS]
    if not cols:
        return 0
    sets = ", ".join(f"{c}=?" for c in cols) + ", updated_at=datetime('now')"
    vals = [fields[c] for c in cols] + [b["id"]]
    return con.execute(f"UPDATE bottles SET {sets} WHERE id=?", vals).rowcount


def delete_bottle(con: sqlite3.Connection, name_or_id: str | int) -> int:
    b = get_bottle(con, name_or_id) if isinstance(name_or_id, (int, str)) else name_or_id
    if not b:
        return 0
    return con.execute("DELETE FROM bottles WHERE id=?", (b["id"],)).rowcount


def consume_bottle(con: sqlite3.Connection, name_or_id: str | int) -> dict:
    """Decrement quantity by 1; flip status to 'consumed' when it reaches 0."""
    b = get_bottle(con, name_or_id)
    if not b:
        raise KeyError(f"No bottle matching {name_or_id!r}")
    new_qty = max(0, (b["quantity"] or 0) - 1)
    new_status = "consumed" if new_qty == 0 else b["status"]
    con.execute(
        "UPDATE bottles SET quantity=?, status=?, updated_at=datetime('now') WHERE id=?",
        (new_qty, new_status, b["id"]),
    )
    return {"id": b["id"], "quantity": new_qty, "status": new_status}


def untasted_bottles(con: sqlite3.Connection, type_: str | None = None) -> list[sqlite3.Row]:
    """Bottles with status in-cellar/acquired and no tasting rows."""
    sql = """
        SELECT b.*, p.name AS producer_name
          FROM bottles b
          JOIN producers p ON b.producer_id = p.id
          WHERE b.status IN ('in-cellar','acquired')
            AND NOT EXISTS (SELECT 1 FROM tastings t WHERE t.bottle_id = b.id)
    """
    params: list = []
    if type_:
        sql += " AND b.type = ? COLLATE NOCASE"
        params.append(type_)
    sql += " ORDER BY p.name, b.name"
    return con.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Tasting CRUD
# ---------------------------------------------------------------------------

TASTING_FIELDS = {
    "tasted_at", "rating", "nose", "palate", "finish", "color",
    "food_pairings", "location", "notes", "obsidian_file",
}


def add_tasting(con: sqlite3.Connection, bottle: str | int, **fields) -> int:
    """Append a new tasting row."""
    b = get_bottle(con, bottle) if isinstance(bottle, (int, str)) else bottle
    if not b:
        raise KeyError(f"No bottle matching {bottle!r}")
    if "rating" in fields and fields["rating"] is not None:
        r = int(fields["rating"])
        if not (0 <= r <= 100):
            raise ValueError("rating must be 0-100 (or null)")
        fields["rating"] = r
    if "tasted_at" in fields and fields["tasted_at"]:
        # Validate ISO format if provided
        try:
            datetime.strptime(fields["tasted_at"], "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"tasted_at must be YYYY-MM-DD: {e}")

    cols = ["bottle_id"] + [k for k in fields if k in TASTING_FIELDS]
    vals = [b["id"]] + [fields[c] for c in cols[1:]]
    placeholders = ",".join("?" * len(cols))
    con.execute(f"INSERT INTO tastings ({','.join(cols)}) VALUES ({placeholders})", vals)
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_tasting(con: sqlite3.Connection, tasting_id: int, **fields) -> int:
    if "rating" in fields and fields["rating"] is not None:
        r = int(fields["rating"])
        if not (0 <= r <= 100):
            raise ValueError("rating must be 0-100")
        fields["rating"] = r
    cols = [k for k in fields if k in TASTING_FIELDS]
    if not cols:
        return 0
    sets = ", ".join(f"{c}=?" for c in cols)
    vals = [fields[c] for c in cols] + [tasting_id]
    return con.execute(f"UPDATE tastings SET {sets} WHERE id=?", vals).rowcount


def delete_tasting(con: sqlite3.Connection, tasting_id: int) -> int:
    return con.execute("DELETE FROM tastings WHERE id=?", (tasting_id,)).rowcount


def tastings_for_bottle(con: sqlite3.Connection, bottle_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM tastings WHERE bottle_id=? ORDER BY tasted_at DESC, id DESC",
        (bottle_id,),
    ).fetchall()


def recent_tastings(con: sqlite3.Connection, limit: int = 20,
                    since: str | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT t.*, b.name AS bottle_name, b.type AS bottle_type,
               p.name AS producer_name
          FROM tastings t
          JOIN bottles b ON t.bottle_id = b.id
          JOIN producers p ON b.producer_id = p.id
    """
    params: list = []
    if since:
        sql += " WHERE t.tasted_at IS NOT NULL AND t.tasted_at >= ?"
        params.append(since)
    sql += " ORDER BY COALESCE(t.tasted_at, t.created_at) DESC, t.id DESC LIMIT ?"
    params.append(int(limit))
    return con.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_cellar(con: sqlite3.Connection, query: str,
                  type_: str | None = None,
                  status: str | None = None) -> dict:
    """Combined search: producers + bottles matching the query."""
    return {
        "producers": [dict(r) for r in find_producers(con, query)],
        "bottles": [dict(r) for r in find_bottles(con, query, type_=type_, status=status)],
    }


def get_types(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("SELECT DISTINCT type FROM bottles ORDER BY type").fetchall()
    db_types = [r["type"] for r in rows]
    # Union with the recommended list so callers see new ones too
    return sorted(set(db_types) | set(KNOWN_TYPES))


# ---------------------------------------------------------------------------
# Vault note helpers (lazy create/append/read)
# ---------------------------------------------------------------------------

def _frontmatter_for(kind: str, **meta) -> str:
    """Build a minimal frontmatter block linking back to the DB."""
    lines = ["---", f"kind: {kind}"]
    for k, v in meta.items():
        if v is not None:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n"


def read_note(rel_path: str) -> str:
    p = absolute(rel_path)
    if not p.exists():
        return ""
    return p.read_text()


def append_note(rel_path: str, text: str, frontmatter: str = "") -> None:
    """Append text to a vault note, creating the file (with frontmatter) if absent."""
    p = absolute(rel_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists() and frontmatter:
        p.write_text(frontmatter)
    with p.open("a") as f:
        if not text.endswith("\n"):
            text += "\n"
        f.write(text)


def write_note(rel_path: str, content: str) -> None:
    """Replace a vault note's content (or create it)."""
    p = absolute(rel_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def append_producer_note(con: sqlite3.Connection, producer: str | int, text: str) -> dict:
    p = get_producer(con, producer)
    if not p:
        raise KeyError(f"No producer matching {producer!r}")
    rel = p["obsidian_file"] or producer_note_path(p["name"])
    fm = _frontmatter_for("producer", db_id=p["id"], name=p["name"])
    append_note(rel, text, frontmatter=fm)
    if not p["obsidian_file"]:
        update_producer(con, p["id"], obsidian_file=rel)
    return {"path": rel, "appended_chars": len(text)}


def read_producer_note(con: sqlite3.Connection, producer: str | int) -> dict:
    p = get_producer(con, producer)
    if not p:
        raise KeyError(f"No producer matching {producer!r}")
    rel = p["obsidian_file"] or producer_note_path(p["name"])
    return {"path": rel, "content": read_note(rel)}


def append_bottle_note(con: sqlite3.Connection, bottle: str | int, text: str,
                       producer: str | int | None = None) -> dict:
    b = get_bottle(con, bottle, producer=producer)
    if not b:
        raise KeyError(f"No bottle matching {bottle!r}")
    rel = b["obsidian_file"]
    if not rel:
        prow = get_producer(con, b["producer_id"])
        rel = bottle_note_path(b["type"], prow["name"], b["name"])
    fm = _frontmatter_for(
        "bottle", db_id=b["id"], producer_id=b["producer_id"],
        name=b["name"], type=b["type"],
    )
    append_note(rel, text, frontmatter=fm)
    if not b["obsidian_file"]:
        update_bottle(con, b["id"], obsidian_file=rel)
    return {"path": rel, "appended_chars": len(text)}


def read_bottle_note(con: sqlite3.Connection, bottle: str | int,
                     producer: str | int | None = None) -> dict:
    b = get_bottle(con, bottle, producer=producer)
    if not b:
        raise KeyError(f"No bottle matching {bottle!r}")
    rel = b["obsidian_file"]
    if not rel:
        prow = get_producer(con, b["producer_id"])
        rel = bottle_note_path(b["type"], prow["name"], b["name"])
    return {"path": rel, "content": read_note(rel)}


# ---------------------------------------------------------------------------
# Show helpers (for show_producer / show_bottle MCP tools)
# ---------------------------------------------------------------------------

def merge_producers(con: sqlite3.Connection, source: str | int, target: str | int,
                    *, append_notes: bool = True) -> dict:
    """Merge `source` producer into `target`. After this:
        - all of source's bottles point at target
        - bottle name collisions are merged (tastings combined into target's bottle,
          source bottle deleted; source's vault note file deleted, target's kept)
        - bottles' vault notes are renamed/moved to the new producer folder
        - source producer's note prose (if any) is appended to target's note
        - source producer row + its note file are deleted
    """
    src = get_producer(con, source)
    tgt = get_producer(con, target)
    if not src:
        raise KeyError(f"No producer matching source {source!r}")
    if not tgt:
        raise KeyError(f"No producer matching target {target!r}")
    if src["id"] == tgt["id"]:
        raise ValueError("source and target are the same producer")

    moved: list[dict] = []
    merged: list[dict] = []

    src_bottles = con.execute(
        "SELECT * FROM bottles WHERE producer_id=?", (src["id"],),
    ).fetchall()

    for sb in src_bottles:
        # Does target have a bottle with this name (case-insensitive)?
        existing = con.execute(
            "SELECT id, obsidian_file FROM bottles WHERE producer_id=? AND name=? COLLATE NOCASE",
            (tgt["id"], sb["name"]),
        ).fetchone()

        if existing:
            # Bottle-name collision: move tastings, delete source bottle.
            con.execute(
                "UPDATE tastings SET bottle_id=? WHERE bottle_id=?",
                (existing["id"], sb["id"]),
            )
            if sb["obsidian_file"]:
                old_abs = absolute(sb["obsidian_file"])
                if old_abs.exists():
                    old_abs.unlink()
            con.execute("DELETE FROM bottles WHERE id=?", (sb["id"],))
            merged.append({
                "bottle_name": sb["name"],
                "merged_into_id": existing["id"],
            })
            continue

        # Normal re-attribution
        new_path = bottle_note_path(sb["type"], tgt["name"], sb["name"])
        con.execute(
            """UPDATE bottles
                  SET producer_id=?, obsidian_file=?, updated_at=datetime('now')
                WHERE id=?""",
            (tgt["id"], new_path, sb["id"]),
        )
        if sb["obsidian_file"]:
            old_abs = absolute(sb["obsidian_file"])
            new_abs = absolute(new_path)
            if old_abs.exists() and old_abs != new_abs:
                new_abs.parent.mkdir(parents=True, exist_ok=True)
                import shutil as _shutil
                _shutil.move(str(old_abs), str(new_abs))
        moved.append({"bottle_name": sb["name"], "bottle_id": sb["id"]})

    # Carry over source's producer-note prose (minus frontmatter), if any.
    if append_notes and src["obsidian_file"]:
        old_note_abs = absolute(src["obsidian_file"])
        if old_note_abs.exists():
            content = old_note_abs.read_text()
            body = re.sub(r"^---\n.*?\n---\n", "", content, count=1, flags=re.DOTALL).strip()
            # Strip the auto-generated `# <Producer>` H1 if it's the only thing left
            body = re.sub(rf"^#\s+{re.escape(src['name'])}\s*\n?", "", body, count=1).strip()
            if body:
                append_producer_note(
                    con, tgt["id"],
                    f"\n## (merged from {src['name']})\n\n{body}\n",
                )
            old_note_abs.unlink()

    # Delete source producer row
    con.execute("DELETE FROM producers WHERE id=?", (src["id"],))

    return {
        "merged_into": tgt["name"],
        "removed_producer": src["name"],
        "bottles_re_attributed": len(moved),
        "bottles_merged_by_name": len(merged),
        "moved": moved,
        "merged": merged,
    }


def show_producer(con: sqlite3.Connection, name_or_id: str | int) -> dict | None:
    p = get_producer(con, name_or_id)
    if not p:
        return None
    bottles = con.execute(
        "SELECT * FROM bottles WHERE producer_id=? ORDER BY type, name",
        (p["id"],),
    ).fetchall()
    return {
        "producer": dict(p),
        "bottles": [dict(b) for b in bottles],
    }


def show_bottle(con: sqlite3.Connection, name_or_id: str | int,
                producer: str | int | None = None) -> dict | None:
    b = get_bottle(con, name_or_id, producer=producer)
    if not b:
        return None
    p = get_producer(con, b["producer_id"])
    tastings = tastings_for_bottle(con, b["id"])
    return {
        "bottle": dict(b),
        "producer": dict(p) if p else None,
        "tastings": [dict(t) for t in tastings],
    }
