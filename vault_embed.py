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
import os
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


def chunk_text(text: str, title: str = "") -> list[tuple[str, str]]:
    """Split plain text into (heading, content) chunks by paragraph windows."""
    text = text.strip()
    if not text:
        return []
    if count_tokens(text) <= MAX_CHUNK_TOKENS:
        return [(title, text)] if text else []
    return [(title, w) for w in _paragraph_windows(text, MAX_CHUNK_TOKENS, CHUNK_OVERLAP_TOKENS)
            if count_tokens(w) >= MIN_CHUNK_TOKENS]


def chunk_html(html: str) -> tuple[list[tuple[str, str]], dict]:
    """Extract text from HTML and chunk by heading structure. Returns (chunks, metadata)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # Metadata: title, description
    meta = {}
    if soup.title and soup.title.string:
        meta["title"] = soup.title.string.strip()
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        meta["description"] = desc["content"].strip()
    author = soup.find("meta", attrs={"name": "author"})
    if author and author.get("content"):
        meta["author"] = author["content"].strip()

    # Strip non-content elements
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    # Convert headings to markdown-style so chunk_markdown can handle them
    body = soup.body or soup
    lines = []
    for el in body.descendants:
        if el.name in ("h1", "h2", "h3", "h4"):
            level = int(el.name[1])
            text = el.get_text(strip=True)
            if text:
                lines.append(f"{'#' * level} {text}")
        elif el.name in ("p", "li", "pre", "blockquote"):
            text = el.get_text(" ", strip=True)
            if text:
                lines.append(text)

    pseudo_md = "\n\n".join(lines)
    return chunk_markdown(pseudo_md), meta


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

EMPTY_METADATA = {"people": [], "topics": [], "action_items": [], "dates_mentioned": []}

METADATA_BATCH_SYSTEM = (
    "For each numbered note chunk below, extract structured metadata. "
    "Return a JSON array (one object per chunk, in order) with keys: "
    "people, topics, action_items, dates_mentioned. "
    "Use empty arrays for missing fields. Return only the JSON array, no preamble or markdown fences."
)


def extract_metadata(chunk_text: str) -> dict:
    try:
        out = chat(chunk_text, system=METADATA_SYSTEM, temperature=0.0, max_tokens=300)
        out = re.sub(r"^```(?:json)?\n?|\n?```$", "", out.strip(), flags=re.MULTILINE)
        return json.loads(out)
    except Exception:
        return dict(EMPTY_METADATA)


def extract_metadata_batch(chunk_texts: list[str], max_batch_size: int = 8) -> list[dict]:
    """Extract metadata for multiple chunks in one LLM call. Falls back to per-chunk on failure."""
    if not chunk_texts:
        return []
    results: list[dict] = []
    # Process in batches of max_batch_size
    for i in range(0, len(chunk_texts), max_batch_size):
        batch = chunk_texts[i:i + max_batch_size]
        prompt = "\n\n".join(f"=== CHUNK {j + 1} ===\n{text}" for j, text in enumerate(batch))
        try:
            out = chat(prompt, system=METADATA_BATCH_SYSTEM, temperature=0.0,
                       max_tokens=300 * len(batch))
            out = re.sub(r"^```(?:json)?\n?|\n?```$", "", out.strip(), flags=re.MULTILINE)
            parsed = json.loads(out)
            if isinstance(parsed, list) and len(parsed) == len(batch):
                results.extend(parsed)
                continue
        except Exception:
            pass
        # Fallback: one-by-one
        for text in batch:
            results.append(extract_metadata(text))
    return results


def metadata_from_frontmatter(fm: dict, section: str) -> dict | None:
    """For files with rich frontmatter (journal, cellar, people), derive metadata
    without an LLM call. Returns None if the section is not eligible for this shortcut."""
    section = (section or "").lower()
    if not section.startswith(("journal", "cellar/", "people")):
        return None

    # Normalize list fields
    def as_list(v):
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return []

    # People from frontmatter. Journal uses "people: [Name (id), ...]"; cellar/people use different keys.
    people_raw = as_list(fm.get("people"))
    people = [re.sub(r"\s*\(\d+\)\s*$", "", p).strip() for p in people_raw]

    tags = as_list(fm.get("tags"))

    # Dates
    dates = []
    for key in ("date", "date_tasted", "created"):
        v = fm.get(key)
        if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v):
            dates.append(v[:10])

    # Section-specific topic hints
    topics = list(tags)  # tags become topics
    if section.startswith("cellar/"):
        # e.g. cellar/wine -> wine, whiskey, etc.
        topics.append(section.split("/", 1)[1])
        for key in ("vintner", "distillery", "shipper", "region", "country"):
            v = fm.get(key)
            if isinstance(v, str) and v.strip():
                topics.append(v.strip())
    elif section == "people":
        # People notes: frontmatter may have name, role, location, etc.
        for key in ("name", "role", "company", "location"):
            v = fm.get(key)
            if isinstance(v, str) and v.strip():
                topics.append(v.strip())

    # Dedupe topics preserving order
    seen = set()
    topics_unique = []
    for t in topics:
        if t and t not in seen:
            seen.add(t)
            topics_unique.append(t)

    return {
        "people": people,
        "topics": topics_unique[:8],
        "action_items": [],
        "dates_mentioned": dates,
    }


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
    ext = abs_path.suffix.lower().lstrip(".")
    # Normalize extensions to a canonical file_type
    file_type = {"markdown": "md", "htm": "html"}.get(ext, ext)
    if file_type not in ("md", "pdf", "html", "txt"):
        return {"status": "skipped", "reason": f"unsupported type: {ext}"}

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
    elif file_type == "pdf":
        chunk_tuples_raw, pdf_meta = chunk_pdf(abs_path)
        title = pdf_meta.get("title") or abs_path.stem
        frontmatter_json = json.dumps(pdf_meta)
        date = None
        tags = ""
        chunk_tuples = chunk_tuples_raw  # already (heading, content, page_num)
    elif file_type == "html":
        html = raw_bytes.decode("utf-8", errors="replace")
        raw_chunks, html_meta = chunk_html(html)
        title = html_meta.get("title") or abs_path.stem
        frontmatter_json = json.dumps(html_meta)
        date = None
        tags = ""
        chunk_tuples = [(h, c, None) for h, c in raw_chunks]
    else:  # txt
        text = raw_bytes.decode("utf-8", errors="replace")
        title = abs_path.stem
        frontmatter_json = json.dumps({})
        date = None
        tags = ""
        raw_chunks = chunk_text(text, title)
        chunk_tuples = [(h, c, None) for h, c in raw_chunks]

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

    # --- Metadata: shortcut for structured files, batch LLM for the rest ---
    if file_type == "md":
        fm_shortcut = metadata_from_frontmatter(fm, section)
    else:
        fm_shortcut = None

    if fm_shortcut is not None:
        # All chunks in this file share the same frontmatter-derived metadata
        metas = [fm_shortcut] * len(distilled)
    else:
        metas = extract_metadata_batch(texts_to_embed)

    # --- Insert chunks + embeddings ---
    for idx, ((heading, content, page_num), vector, meta) in enumerate(zip(distilled, vectors, metas)):
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
# Progress tracking & locking
# ---------------------------------------------------------------------------

PROGRESS_FILE = EMBED_DB_PATH.parent / "index-progress.json"
LOCK_FILE = EMBED_DB_PATH.parent / "index.lock"


def _write_progress(state: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(PROGRESS_FILE)


def read_progress() -> dict | None:
    """Return the current or last-run progress state, or None if never run."""
    if not PROGRESS_FILE.exists():
        return None
    try:
        state = json.loads(PROGRESS_FILE.read_text())
    except Exception:
        return None
    # Detect stale 'running' state: lock gone or PID dead
    if state.get("status") == "running":
        pid = state.get("pid")
        if pid and not _pid_alive(pid):
            state["status"] = "failed"
            state["error"] = "Process died without finishing"
    return state


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def _acquire_lock() -> bool:
    """Returns True if lock acquired; False if another indexing run is active."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if _pid_alive(pid):
                return False
        except (ValueError, OSError):
            pass
        # stale — clear and take it
        LOCK_FILE.unlink(missing_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Full vault reconciliation
# ---------------------------------------------------------------------------

def reconcile(con: sqlite3.Connection, vault_path: Path = None,
              skip_dirs: tuple[str, ...] = (".obsidian", ".trash", "Templates"),
              *, report_progress: bool = True) -> dict:
    """Walk the vault, add/update/delete/move as needed. Returns counts by status.

    Writes progress to PROGRESS_FILE every file so callers can poll the state.
    Refuses to run if another indexing process holds the lock.
    """
    vault_path = vault_path or VAULT_DIR

    if report_progress and not _acquire_lock():
        raise RuntimeError("Another indexing run is in progress (lock file exists)")

    # First pass: enumerate files so we know the total for progress
    files_to_scan: list[Path] = []
    for abs_path in vault_path.rglob("*"):
        if not abs_path.is_file():
            continue
        if any(skip in abs_path.parts for skip in skip_dirs):
            continue
        if abs_path.suffix.lower() not in (".md", ".markdown", ".pdf", ".html", ".htm", ".txt"):
            continue
        files_to_scan.append(abs_path)

    counts = {"new": 0, "updated": 0, "unchanged": 0, "moved": 0, "deleted": 0, "skipped": 0, "error": 0}
    seen_paths: set[str] = set()
    started_at = datetime.now(tz=timezone.utc).isoformat()

    def emit_progress(**extra):
        if not report_progress:
            return
        state = {
            "status": "running",
            "pid": os.getpid(),
            "started_at": started_at,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            "scope": str(vault_path),
            "total_files": len(files_to_scan),
            "counts": counts,
            **extra,
        }
        _write_progress(state)

    try:
        emit_progress(processed_files=0, current_file=None, phase="indexing")

        for i, abs_path in enumerate(files_to_scan, start=1):
            rel = str(abs_path.relative_to(vault_path))
            seen_paths.add(rel)
            # Emit BEFORE processing so current_file shows what's being worked on live
            emit_progress(processed_files=i - 1, current_file=rel, phase="indexing")
            try:
                result = index_file(con, vault_path, abs_path)
                counts[result["status"]] = counts.get(result["status"], 0) + 1
            except Exception as e:
                counts["error"] += 1
                emit_progress(processed_files=i, current_file=rel,
                              phase="indexing", last_error=f"{rel}: {e}")
                continue
            # Emit after each file so counts update live
            emit_progress(processed_files=i, current_file=rel, phase="indexing")

        # Delete orphans — files in DB but no longer in vault (only when scanning whole vault)
        if vault_path == VAULT_DIR:
            emit_progress(processed_files=len(files_to_scan), phase="deleting_orphans")
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

        if report_progress:
            _write_progress({
                "status": "completed",
                "pid": os.getpid(),
                "started_at": started_at,
                "finished_at": datetime.now(tz=timezone.utc).isoformat(),
                "scope": str(vault_path),
                "total_files": len(files_to_scan),
                "processed_files": len(files_to_scan),
                "counts": counts,
            })
        return counts

    except Exception as e:
        if report_progress:
            _write_progress({
                "status": "failed",
                "pid": os.getpid(),
                "started_at": started_at,
                "finished_at": datetime.now(tz=timezone.utc).isoformat(),
                "scope": str(vault_path),
                "total_files": len(files_to_scan),
                "counts": counts,
                "error": f"{type(e).__name__}: {e}",
            })
        raise
    finally:
        if report_progress:
            _release_lock()


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


# ---------------------------------------------------------------------------
# CLI entry point (used by background indexing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--reconcile", action="store_true", help="Run full vault reconciliation")
    parser.add_argument("--path", help="Subpath under vault to scan (default: full vault)")
    args = parser.parse_args()

    if args.reconcile:
        scope = VAULT_DIR / args.path if args.path else VAULT_DIR
        con = connect()
        try:
            counts = reconcile(con, scope)
            print(json.dumps({"ok": True, "counts": counts}))
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
