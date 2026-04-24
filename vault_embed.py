"""
vault_embed.py — Obsidian vault semantic search index.

Builds and queries a local SQLite index of vault contents with:
  - Qwen3-Embedding-4B (2560-dim vectors via sqlite-vec)
  - SQLite FTS5 for keyword fallback
  - Hybrid chunking (heading-based + LLM distillation for long sections)
  - LLM metadata extraction (people, topics, action items)
  - PDF support via pymupdf
  - Incremental indexing with move detection
"""
import hashlib
import json
import re
import sqlite3
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    VAULT_DIR, EMBED_DB_PATH, EMBED_MODEL_URL, EMBED_MODEL_NAME, EMBED_DIMS,
    CHAT_MODEL_URL, CHAT_MODEL_NAME,
)
from journal_fm import split_frontmatter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CHUNK_TOKENS = 500      # target chunk size
MIN_CHUNK_TOKENS = 30       # drop chunks smaller than this (usually noise)
DISTILL_THRESHOLD = 1000    # sections over this go through LLM distillation
CHUNK_OVERLAP_TOKENS = 50   # overlap when splitting long sections

# Rough token counting (4 chars ≈ 1 token for English prose)
def count_tokens(text: str) -> int:
    return max(1, len(text) // 4)

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    file_type    TEXT NOT NULL,
    section      TEXT,
    title        TEXT,
    frontmatter  TEXT,
    date         TEXT,
    tags         TEXT,
    content_hash TEXT NOT NULL,
    modified_at  TEXT,
    indexed_at   TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    heading     TEXT,
    content     TEXT NOT NULL,
    token_count INTEGER,
    page_num    INTEGER,
    metadata    TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0(
    chunk_id  INTEGER PRIMARY KEY,
    embedding FLOAT[{EMBED_DIMS}]
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    heading,
    content='chunks',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content, heading)
    VALUES (new.id, new.content, new.heading);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content, heading)
    VALUES ('delete', old.id, old.content, old.heading);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content, heading)
    VALUES ('delete', old.id, old.content, old.heading);
    INSERT INTO chunks_fts(rowid, content, heading)
    VALUES (new.id, new.content, new.heading);
END;

CREATE INDEX IF NOT EXISTS idx_chunks_file   ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_files_section ON files(section);
CREATE INDEX IF NOT EXISTS idx_files_date    ON files(date);
CREATE INDEX IF NOT EXISTS idx_files_hash    ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_type    ON files(file_type);
"""


def connect():
    EMBED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(EMBED_DB_PATH))
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(SCHEMA)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


# ---------------------------------------------------------------------------
# LM Studio HTTP clients
# ---------------------------------------------------------------------------

def _post_json(url: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def embed(text: str) -> list[float]:
    """Embed a single text. Returns 2560-dim vector."""
    data = _post_json(
        f"{EMBED_MODEL_URL}/embeddings",
        {"model": EMBED_MODEL_NAME, "input": text},
    )
    return data["data"][0]["embedding"]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts in one request."""
    data = _post_json(
        f"{EMBED_MODEL_URL}/embeddings",
        {"model": EMBED_MODEL_NAME, "input": texts},
        timeout=300,
    )
    return [item["embedding"] for item in data["data"]]


def chat(prompt: str, system: str = "", temperature: float = 0.3, max_tokens: int = 500) -> str:
    """Call the chat model. Returns just the text response."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    data = _post_json(
        f"{CHAT_MODEL_URL}/chat/completions",
        {
            "model": CHAT_MODEL_NAME,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=300,
    )
    return data["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Content fingerprinting
# ---------------------------------------------------------------------------

def content_hash(text: str) -> str:
    """SHA256 of normalized content. Ignores whitespace drift for dedup."""
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Section classification
# ---------------------------------------------------------------------------

def section_from_path(rel_path: Path) -> str:
    parts = rel_path.parts
    if len(parts) <= 1:
        return "root"
    if len(parts) >= 2 and parts[0] == "Cellar":
        return f"cellar/{parts[1].lower()}"
    return parts[0].lower()


# ---------------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------------

def chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split markdown body into (heading_path, content) chunks.

    Rules:
      - Short files (<MAX_CHUNK_TOKENS): one chunk
      - Otherwise split on H2, then H3 within long H2 sections
      - Long leaf sections get word-window split with overlap
    """
    if count_tokens(text) <= MAX_CHUNK_TOKENS:
        return [("", text.strip())] if text.strip() else []

    chunks = []
    current_h1 = ""
    current_h2 = ""
    current_h3 = ""
    buf = []
    buf_heading = ""

    def flush():
        if not buf:
            return
        content = "\n".join(buf).strip()
        if content:
            chunks.append((buf_heading, content))
        buf.clear()

    for line in text.splitlines():
        h1 = re.match(r"^#\s+(.+)", line)
        h2 = re.match(r"^##\s+(.+)", line)
        h3 = re.match(r"^###\s+(.+)", line)
        if h1 or h2 or h3:
            flush()
            if h1:
                current_h1 = h1.group(1).strip()
                current_h2 = ""
                current_h3 = ""
                buf_heading = f"# {current_h1}"
            elif h2:
                current_h2 = h2.group(1).strip()
                current_h3 = ""
                parts = [current_h1, current_h2] if current_h1 else [current_h2]
                buf_heading = " > ".join(f"{'#' * (i + 1)} {p}" for i, p in enumerate(parts))
            else:
                current_h3 = h3.group(1).strip()
                parts = [x for x in [current_h1, current_h2, current_h3] if x]
                buf_heading = " > ".join(f"{'#' * (i + 1)} {p}" for i, p in enumerate(parts))
            buf.append(line)
        else:
            buf.append(line)
    flush()

    # Further split any chunk over MAX_CHUNK_TOKENS
    expanded = []
    for heading, content in chunks:
        if count_tokens(content) <= MAX_CHUNK_TOKENS:
            expanded.append((heading, content))
            continue
        # Window-split by paragraphs with overlap
        paragraphs = [p for p in re.split(r"\n\n+", content) if p.strip()]
        window: list[str] = []
        window_tokens = 0
        for p in paragraphs:
            pt = count_tokens(p)
            if window and window_tokens + pt > MAX_CHUNK_TOKENS:
                expanded.append((heading, "\n\n".join(window).strip()))
                # keep last paragraph as overlap seed
                overlap = window[-1] if count_tokens(window[-1]) < CHUNK_OVERLAP_TOKENS * 4 else ""
                window = [overlap] if overlap else []
                window_tokens = count_tokens(overlap) if overlap else 0
            window.append(p)
            window_tokens += pt
        if window:
            expanded.append((heading, "\n\n".join(window).strip()))

    return [(h, c) for h, c in expanded if count_tokens(c) >= MIN_CHUNK_TOKENS]


# ---------------------------------------------------------------------------
# PDF chunking
# ---------------------------------------------------------------------------

def chunk_pdf(path: Path) -> tuple[list[tuple[str, str, int]], dict]:
    """Returns ((heading, content, page_num) chunks, pdf_metadata)."""
    import fitz  # pymupdf
    doc = fitz.open(str(path))
    meta = {k: v for k, v in doc.metadata.items() if v}
    chunks = []
    title = meta.get("title") or path.stem
    for i, page in enumerate(doc, start=1):
        text = page.get_text().strip()
        if not text:
            continue
        # If page is short, use as-is; if long, split further
        if count_tokens(text) <= MAX_CHUNK_TOKENS:
            heading = f"{title} > Page {i}"
            chunks.append((heading, text, i))
        else:
            for j, para_window in enumerate(_paragraph_windows(text, MAX_CHUNK_TOKENS, CHUNK_OVERLAP_TOKENS)):
                heading = f"{title} > Page {i}" + (f" ({j + 1})" if j else "")
                chunks.append((heading, para_window, i))
    doc.close()
    return [(h, c, p) for h, c, p in chunks if count_tokens(c) >= MIN_CHUNK_TOKENS], meta


def _paragraph_windows(text: str, max_tokens: int, overlap_tokens: int):
    paragraphs = [p for p in re.split(r"\n\n+|\n(?=[A-Z])", text) if p.strip()]
    window: list[str] = []
    window_tokens = 0
    for p in paragraphs:
        pt = count_tokens(p)
        if window and window_tokens + pt > max_tokens:
            yield "\n\n".join(window).strip()
            overlap = window[-1] if count_tokens(window[-1]) < overlap_tokens * 4 else ""
            window = [overlap] if overlap else []
            window_tokens = count_tokens(overlap) if overlap else 0
        window.append(p)
        window_tokens += pt
    if window:
        yield "\n\n".join(window).strip()


# ---------------------------------------------------------------------------
# LLM distillation (for long sections)
# ---------------------------------------------------------------------------

DISTILL_SYSTEM = (
    "You summarize long sections of personal notes into self-contained thoughts. "
    "Each thought should preserve specific facts, names, dates, and quotes. "
    "Return 1-3 thoughts, each 2-4 sentences, separated by '---'. No preamble."
)


def distill(long_text: str) -> list[str]:
    """LLM-distill a very long section into 1-3 standalone chunks."""
    try:
        out = chat(long_text, system=DISTILL_SYSTEM, temperature=0.2, max_tokens=600)
        parts = [p.strip() for p in out.split("---") if p.strip()]
        return parts or [long_text[:2000]]
    except Exception:
        # Fallback: truncate
        return [long_text[:2000]]


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

METADATA_SYSTEM = (
    "Extract structured metadata from a personal note chunk. "
    "Return JSON with keys: people (list of proper names mentioned), "
    "topics (list of key topics/themes, 1-5 items), "
    "action_items (list of any todos or follow-ups), "
    "dates_mentioned (list of dates in YYYY-MM-DD form). "
    "Use empty arrays for missing fields. Return only the JSON, no preamble."
)


def extract_metadata(chunk_text: str) -> dict:
    try:
        out = chat(chunk_text, system=METADATA_SYSTEM, temperature=0.0, max_tokens=300)
        # Strip markdown fences if present
        out = re.sub(r"^```(?:json)?\n?|\n?```$", "", out.strip(), flags=re.MULTILINE)
        return json.loads(out)
    except Exception:
        return {"people": [], "topics": [], "action_items": [], "dates_mentioned": []}


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

def parse_frontmatter_kv(fm_text: str) -> dict:
    """Lightweight frontmatter → dict parse (sufficient for index metadata)."""
    fm = {}
    for line in fm_text.splitlines():
        m = re.match(r"^(\w+):\s*(.*)", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip() for v in val[1:-1].split(",") if v.strip()]
            fm[key] = val
    return fm


def extract_date(fm: dict, path: Path) -> str | None:
    # Journal files: filename YYYY-MM-DD.md
    if re.match(r"^\d{4}-\d{2}-\d{2}$", path.stem):
        return path.stem
    # From frontmatter
    for key in ("date", "date_tasted", "created"):
        v = fm.get(key)
        if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v):
            return v[:10]
    return None


def extract_tags(fm: dict) -> str:
    tags = fm.get("tags", [])
    if isinstance(tags, list):
        return ",".join(tags)
    if isinstance(tags, str):
        return tags
    return ""


# ---------------------------------------------------------------------------
# Indexing one file
# ---------------------------------------------------------------------------

def _first_h1(text: str) -> str:
    m = re.search(r"^#\s+(.+)", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def index_file(con: sqlite3.Connection, vault_path: Path, abs_path: Path) -> dict:
    """Index a single file. Returns {'status': 'new|updated|moved|unchanged|skipped', 'chunks': N}."""
    rel_path = abs_path.relative_to(vault_path)
    file_type = abs_path.suffix.lower().lstrip(".")
    if file_type not in ("md", "pdf"):
        return {"status": "skipped", "reason": "unsupported type"}

    raw_bytes = abs_path.read_bytes()
    hash_ = hashlib.sha256(raw_bytes).hexdigest()
    mtime = datetime.fromtimestamp(abs_path.stat().st_mtime, tz=timezone.utc).isoformat()
    now = datetime.now(tz=timezone.utc).isoformat()

    # --- Move detection: same hash, different path ---
    existing_by_hash = con.execute(
        "SELECT id, path FROM files WHERE content_hash=?", (hash_,)
    ).fetchone()
    if existing_by_hash and existing_by_hash["path"] != str(rel_path):
        old_path = existing_by_hash["path"]
        if not (vault_path / old_path).exists():
            con.execute(
                "UPDATE files SET path=?, modified_at=?, indexed_at=? WHERE id=?",
                (str(rel_path), mtime, now, existing_by_hash["id"]),
            )
            con.commit()
            return {"status": "moved", "from": old_path, "to": str(rel_path)}

    # --- Check existing by path ---
    existing = con.execute(
        "SELECT id, content_hash FROM files WHERE path=?", (str(rel_path),)
    ).fetchone()
    if existing and existing["content_hash"] == hash_:
        return {"status": "unchanged"}

    # --- Parse and chunk ---
    if file_type == "md":
        text = raw_bytes.decode("utf-8", errors="replace")
        fm_text, body = split_frontmatter(text)
        fm = parse_frontmatter_kv(fm_text) if fm_text else {}
        title = _first_h1(body) or abs_path.stem
        frontmatter_json = json.dumps(fm)
        date = extract_date(fm, rel_path)
        tags = extract_tags(fm)
        raw_chunks = chunk_markdown(body)
        chunk_tuples = [(h, c, None) for h, c in raw_chunks]
    else:  # pdf
        chunk_tuples, pdf_meta = chunk_pdf(abs_path)
        title = pdf_meta.get("title") or abs_path.stem
        frontmatter_json = json.dumps(pdf_meta)
        date = None
        tags = ""

    section = section_from_path(rel_path)

    # --- LLM distillation for any chunk over DISTILL_THRESHOLD ---
    distilled: list[tuple[str, str, int | None]] = []
    for heading, content, page_num in chunk_tuples:
        if count_tokens(content) > DISTILL_THRESHOLD:
            for thought in distill(content):
                distilled.append((heading, thought, page_num))
        else:
            distilled.append((heading, content, page_num))

    # --- Upsert file row ---
    if existing:
        file_id = existing["id"]
        con.execute(
            """UPDATE files SET file_type=?, section=?, title=?, frontmatter=?,
               date=?, tags=?, content_hash=?, modified_at=?, indexed_at=? WHERE id=?""",
            (file_type, section, title, frontmatter_json, date, tags, hash_, mtime, now, file_id),
        )
        # Delete old chunks+embeddings (FTS5 trigger handles itself)
        con.execute(
            "DELETE FROM embeddings WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id=?)",
            (file_id,),
        )
        con.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        status = "updated"
    else:
        cur = con.execute(
            """INSERT INTO files (path, file_type, section, title, frontmatter,
               date, tags, content_hash, modified_at, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (str(rel_path), file_type, section, title, frontmatter_json,
             date, tags, hash_, mtime, now),
        )
        file_id = cur.lastrowid
        status = "new"

    if not distilled:
        con.commit()
        return {"status": status, "chunks": 0}

    # --- Embed in batch ---
    texts_to_embed = [content for _, content, _ in distilled]
    try:
        vectors = embed_batch(texts_to_embed)
    except Exception as e:
        con.rollback()
        return {"status": "error", "error": str(e), "path": str(rel_path)}

    # --- Insert chunks + embeddings ---
    for idx, ((heading, content, page_num), vector) in enumerate(zip(distilled, vectors)):
        meta = extract_metadata(content)
        cur = con.execute(
            """INSERT INTO chunks (file_id, chunk_index, heading, content, token_count, page_num, metadata)
               VALUES (?,?,?,?,?,?,?)""",
            (file_id, idx, heading, content, count_tokens(content), page_num, json.dumps(meta)),
        )
        chunk_id = cur.lastrowid
        con.execute(
            "INSERT INTO embeddings (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, json.dumps(vector)),
        )

    con.commit()
    return {"status": status, "chunks": len(distilled)}


# ---------------------------------------------------------------------------
# Full vault reconciliation
# ---------------------------------------------------------------------------

def reconcile(con: sqlite3.Connection, vault_path: Path = None,
              skip_dirs: tuple[str, ...] = (".obsidian", ".trash", "Templates")) -> dict:
    """Walk the vault, add/update/delete/move as needed. Returns counts by status."""
    vault_path = vault_path or VAULT_DIR
    counts = {"new": 0, "updated": 0, "unchanged": 0, "moved": 0, "deleted": 0, "skipped": 0, "error": 0}
    seen_paths: set[str] = set()

    for abs_path in vault_path.rglob("*"):
        if not abs_path.is_file():
            continue
        if any(skip in abs_path.parts for skip in skip_dirs):
            continue
        if abs_path.suffix.lower() not in (".md", ".pdf"):
            continue
        rel = str(abs_path.relative_to(vault_path))
        seen_paths.add(rel)
        result = index_file(con, vault_path, abs_path)
        counts[result["status"]] = counts.get(result["status"], 0) + 1

    # Delete orphans — files in DB but no longer in vault
    rows = con.execute("SELECT id, path FROM files").fetchall()
    for row in rows:
        if row["path"] not in seen_paths:
            con.execute(
                "DELETE FROM embeddings WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id=?)",
                (row["id"],),
            )
            con.execute("DELETE FROM files WHERE id=?", (row["id"],))
            counts["deleted"] += 1
    con.commit()
    return counts


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def semantic_search(con: sqlite3.Connection, query: str, *,
                    limit: int = 10, section: str = None,
                    file_type: str = None, date_from: str = None,
                    date_to: str = None) -> list[dict]:
    """Hybrid search: vector KNN merged with FTS5 keyword results."""
    qvec = embed(query)

    # --- Vector search ---
    vec_rows = con.execute(
        """SELECT e.chunk_id, e.distance
           FROM embeddings e
           WHERE e.embedding MATCH ? AND k = ?
           ORDER BY e.distance""",
        (json.dumps(qvec), limit * 3),
    ).fetchall()
    vec_ranks = {r["chunk_id"]: (i + 1, r["distance"]) for i, r in enumerate(vec_rows)}

    # --- FTS5 keyword search ---
    fts_ranks: dict[int, int] = {}
    try:
        fts_rows = con.execute(
            "SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit * 3),
        ).fetchall()
        for i, r in enumerate(fts_rows):
            fts_ranks[r["rowid"]] = i + 1
    except sqlite3.OperationalError:
        pass  # invalid FTS5 query — fall back to vector only

    # --- Reciprocal Rank Fusion ---
    k_rrf = 60
    candidates: dict[int, float] = {}
    for cid, (rank, _) in vec_ranks.items():
        candidates[cid] = candidates.get(cid, 0) + 1 / (k_rrf + rank)
    for cid, rank in fts_ranks.items():
        candidates[cid] = candidates.get(cid, 0) + 1 / (k_rrf + rank)

    sorted_ids = sorted(candidates.keys(), key=lambda x: -candidates[x])[:limit * 2]

    # --- Fetch rows with file metadata ---
    if not sorted_ids:
        return []
    placeholders = ",".join("?" * len(sorted_ids))
    rows = con.execute(
        f"""SELECT c.id, c.heading, c.content, c.token_count, c.page_num, c.metadata,
                   f.path, f.file_type, f.section, f.title, f.date, f.tags
            FROM chunks c JOIN files f ON f.id=c.file_id
            WHERE c.id IN ({placeholders})""",
        sorted_ids,
    ).fetchall()

    row_map = {r["id"]: r for r in rows}
    results = []
    for cid in sorted_ids:
        r = row_map.get(cid)
        if not r:
            continue
        # Apply filters
        if section and r["section"] != section:
            continue
        if file_type and r["file_type"] != file_type:
            continue
        if date_from and (not r["date"] or r["date"] < date_from):
            continue
        if date_to and (not r["date"] or r["date"] > date_to):
            continue
        results.append({
            "chunk_id": cid,
            "path": r["path"],
            "file_type": r["file_type"],
            "section": r["section"],
            "title": r["title"],
            "date": r["date"],
            "tags": r["tags"],
            "heading": r["heading"],
            "content": r["content"],
            "page_num": r["page_num"],
            "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            "score": candidates[cid],
            "vector_rank": vec_ranks.get(cid, (None, None))[0],
            "fts_rank": fts_ranks.get(cid),
        })
        if len(results) >= limit:
            break
    return results


def index_stats(con: sqlite3.Connection) -> dict:
    files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    embeddings = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    by_section = dict(con.execute("SELECT section, COUNT(*) FROM files GROUP BY section").fetchall())
    last_indexed = con.execute("SELECT MAX(indexed_at) FROM files").fetchone()[0]
    db_size = EMBED_DB_PATH.stat().st_size if EMBED_DB_PATH.exists() else 0
    return {
        "files": files,
        "chunks": chunks,
        "embeddings": embeddings,
        "by_section": by_section,
        "last_indexed": last_indexed,
        "db_size_mb": round(db_size / 1024 / 1024, 1),
        "embed_model": EMBED_MODEL_NAME,
        "embed_dims": EMBED_DIMS,
    }
