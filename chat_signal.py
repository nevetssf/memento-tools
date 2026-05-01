"""Chat-Signal database: persistent record of Signal exchanges with the agent.

Stores chats as Markdown under VAULT_DIR/Chats/Signal/YYYY/MM/YYYY-MM-DD.md,
with photos under VAULT_DIR/Chats/Signal/YYYY/MM/photos/.

Distinct from the daily journal: journal entries are Steven's logged life events;
chats are the full transcript of his exchanges with the agent (Memento).

Public API used by chat-signal-mcp.py:
  save_message(sender, text, ...)
  save_exchange(steven_text, memento_text, ...)
  save_photo(source_path, ...)
  get_today(limit=None)
  get_recent(limit, since=None)
  get_by_date(date, limit=None)
  search(query, since=None, until=None, sender=None)
  get_dates(year=None, month=None)
  get_path(date=None)
  get_summary(date_from=None, date_to=None)
"""

from __future__ import annotations

import contextlib
import fcntl
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as _date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import VAULT_DIR, LOCATION_FILE
import localtime

# ---------- Constants -----------------------------------------------------

CHAT_ROOT = Path(VAULT_DIR) / "Chats" / "Signal"
DEFAULT_AGENT = "main"
VALID_SENDERS = {"Steven", "Memento", "system"}

# Speaker label, optionally with `(agent)` suffix. Captures: name, agent, rest.
_SPEAKER_RE = re.compile(r"^\*\*([A-Za-z][A-Za-z _-]*?)(?:\s+\(([^)]+)\))?:\*\*\s*(.*)$")
_HEADING_RE = re.compile(r"^##\s+(\d{2}:\d{2})(?:\s+([A-Z]{3,5}))?", re.MULTILINE)
_PHOTO_RE = re.compile(r"!\[\[[^\]]+\]\]")
_PHOTO_FULL_RE = re.compile(r"^!\[\[[^\]]+\]\]$")


# ---------- Data class ----------------------------------------------------

@dataclass
class Message:
    timestamp: str
    sender: str
    text: str
    photos: list[str] = field(default_factory=list)
    agent: str = DEFAULT_AGENT
    date: str | None = None

    def to_dict(self) -> dict:
        d = {
            "timestamp": self.timestamp,
            "sender": self.sender,
            "text": self.text,
            "photos": self.photos,
            "agent": self.agent,
        }
        if self.date:
            d["date"] = self.date
        return d


# ---------- Path helpers --------------------------------------------------

def _local_now() -> datetime:
    """Tz-aware datetime in Steven's current local zone (per LOCATION.md)."""
    location = LOCATION_FILE.read_text().strip() if Path(LOCATION_FILE).exists() else None
    info = localtime.get_localtime(location=location)
    return datetime.now(tz=ZoneInfo(info["timezone"]))


def _to_date(value) -> _date:
    if value is None:
        return _local_now().date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    if isinstance(value, str):
        return _date.fromisoformat(value)
    raise TypeError(f"Cannot interpret {value!r} as a date")


def _chat_dir(d: _date) -> Path:
    return CHAT_ROOT / f"{d.year:04d}" / f"{d.month:02d}"


def _photo_dir(d: _date) -> Path:
    """Backwards-compatible: photos directory (still used for image attachments)."""
    return _chat_dir(d) / "photos"


def _attachment_dir(d: _date, kind: str) -> Path:
    """Per-kind attachments directory: photos / audio / video / attachments."""
    subdir = {
        "image": "photos",
        "audio": "audio",
        "video": "video",
        "file": "attachments",
    }.get(kind, "attachments")
    return _chat_dir(d) / subdir


def _chat_file(d: _date) -> Path:
    return _chat_dir(d) / f"{d.isoformat()}.md"


def get_path(date=None) -> str:
    return str(_chat_file(_to_date(date)))


def _ensure_file(path: Path, d: _date) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"date: {d.isoformat()}\n"
        "channel: signal\n"
        "tags: [chat]\n"
        "---\n"
    )
    path.write_text(frontmatter)


@contextlib.contextmanager
def _file_lock(file_path: Path):
    """Exclusive flock via a sidecar `.lock` file.

    Serializes concurrent writers to the same chat-file. The chat hook can fire
    quickly for back-to-back messages (e.g. a media + caption pair from Signal)
    and we don't want one writer's read/modify/write to clobber another's.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    with open(lock_path, "w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


# ---------- Slug + attachment handling ------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text):
    if not text:
        return None
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s or None


# Map common file extensions to attachment kinds. Used when caller doesn't
# supply `kind=` explicitly. Anything not listed falls through to "file".
_EXT_KIND = {
    # images
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".heic": "image", ".heif": "image", ".bmp": "image",
    ".tiff": "image", ".tif": "image", ".svg": "image",
    # audio
    ".mp3": "audio", ".m4a": "audio", ".aac": "audio", ".ogg": "audio",
    ".opus": "audio", ".wav": "audio", ".flac": "audio", ".oga": "audio",
    # video
    ".mp4": "video", ".mov": "video", ".webm": "video", ".avi": "video",
    ".mkv": "video", ".m4v": "video", ".3gp": "video",
}


def _kind_from_ext(suffix: str) -> str:
    return _EXT_KIND.get(suffix.lower(), "file")


def save_attachment(
    source_path: str,
    kind: str | None = None,
    date=None,
    slug=None,
) -> dict:
    """Move an attachment into the month's per-kind directory.

    Args:
      source_path: filesystem path of the source file.
      kind: 'image' / 'audio' / 'video' / 'file'. Auto-detected from the file
            extension when not supplied.
      date: ISO date / datetime / None — controls which month-dir is used.
            Defaults to today (Steven's local zone).
      slug: optional human-readable suffix appended to the filename.

    Returns: {'wikilink': '![[...]]', 'kind': <resolved>, 'path': <abs path>}.
    The wikilink is suitable for embedding in a chat message via
    `chat_save_message(photos=[<wikilink>])` (the `photos` arg accepts any
    attachment wikilink, not just images).
    """
    src = Path(source_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"Attachment source not found: {src}")

    d = _to_date(date)
    suffix = src.suffix.lower() or ".bin"
    resolved_kind = kind or _kind_from_ext(suffix)
    if resolved_kind not in {"image", "audio", "video", "file"}:
        resolved_kind = "file"

    dest_dir = _attachment_dir(d, resolved_kind)
    dest_dir.mkdir(parents=True, exist_ok=True)

    now = _local_now()
    slug_part = _slugify(slug)
    base = f"{d.isoformat()}-{now.strftime('%H%M%S')}"
    filename = f"{base}-{slug_part}{suffix}" if slug_part else f"{base}{suffix}"

    dest = dest_dir / filename
    counter = 1
    while dest.exists():
        stem = f"{base}-{slug_part}-{counter}" if slug_part else f"{base}-{counter}"
        dest = dest_dir / f"{stem}{suffix}"
        counter += 1

    shutil.move(str(src), str(dest))

    rel = dest.relative_to(Path(VAULT_DIR))
    return {
        "wikilink": f"![[{rel}]]",
        "kind": resolved_kind,
        "path": str(dest),
    }


def save_photo(source_path: str, date=None, slug=None) -> str:
    """Backwards-compatible wrapper around save_attachment(kind='image').

    Returns just the wikilink string (legacy contract). Prefer
    save_attachment for new code; it returns a dict with kind metadata.
    """
    result = save_attachment(source_path, kind="image", date=date, slug=slug)
    return result["wikilink"]


def _normalize_photo(photo: str, d: _date) -> str:
    """Coerce an attachment arg (source path OR wikilink string) to a wikilink.

    Despite the name (kept for compat with the existing `photos` parameter),
    this works for any attachment kind — the wikilink format is identical
    whether the file is an image, audio, video, or generic file.
    """
    s = photo.strip()
    if s.startswith("![[") and s.endswith("]]"):
        return s
    if s.startswith("[[") and s.endswith("]]"):
        return f"!{s}"
    # Source path → save and return wikilink
    return save_attachment(s, date=d)["wikilink"]


# ---------- Markdown rendering --------------------------------------------

def _speaker_label(sender: str, agent: str) -> str:
    if sender not in VALID_SENDERS:
        return f"unknown:{sender}"
    if sender == "Memento" and agent and agent != DEFAULT_AGENT:
        return f"Memento ({agent})"
    return sender


def _render_message(msg: Message) -> str:
    speaker = _speaker_label(msg.sender, msg.agent)
    if msg.photos and msg.text:
        body = [f"**{speaker}:**", *msg.photos, msg.text]
    elif msg.photos:
        body = [f"**{speaker}:**", *msg.photos]
    else:
        body = [f"**{speaker}:** {msg.text}"]
    return "\n".join(body)


def _render_heading(when: datetime) -> str:
    tz = when.strftime("%Z")
    head = f"## {when.strftime('%H:%M')}"
    if tz:
        head = f"{head} {tz}"
    return head


# ---------- Save tools ----------------------------------------------------

def save_message(
    sender: str,
    text: str,
    timestamp=None,
    photos=None,
    agent: str = DEFAULT_AGENT,
) -> dict:
    """Append a single message to today's (or timestamp's) chat file."""
    if not text and not photos:
        raise ValueError("save_message: text and photos cannot both be empty")

    if isinstance(timestamp, str):
        when = datetime.fromisoformat(timestamp)
    elif isinstance(timestamp, datetime):
        when = timestamp
    else:
        when = _local_now()
    d = when.date()

    photo_links = [_normalize_photo(p, d) for p in (photos or [])]

    msg = Message(
        timestamp=_render_heading(when).removeprefix("## "),
        sender=sender,
        text=text or "",
        photos=photo_links,
        agent=agent,
    )

    file_path = _chat_file(d)
    new_heading = _render_heading(when).strip()
    rendered_msg = _render_message(msg)

    with _file_lock(file_path):
        _ensure_file(file_path, d)
        existing = file_path.read_text()

        last_heading_text = None
        for hm in _HEADING_RE.finditer(existing):
            line_end = existing.find("\n", hm.end())
            if line_end == -1:
                line_end = len(existing)
            last_heading_text = existing[hm.start():line_end].strip()

        if last_heading_text == new_heading:
            new_content = existing.rstrip() + "\n\n" + rendered_msg + "\n"
        else:
            new_content = existing.rstrip() + "\n\n" + new_heading + "\n" + rendered_msg + "\n"

        file_path.write_text(new_content)

    return {
        "path": str(file_path),
        "timestamp": msg.timestamp,
        "speaker": _speaker_label(sender, agent),
    }


def save_exchange(
    steven_text: str,
    memento_text: str,
    timestamp=None,
    steven_photos=None,
    memento_photos=None,
    agent: str = DEFAULT_AGENT,
) -> dict:
    s = save_message(
        sender="Steven",
        text=steven_text,
        timestamp=timestamp,
        photos=steven_photos,
        agent=agent,
    )
    m = save_message(
        sender="Memento",
        text=memento_text,
        timestamp=timestamp,
        photos=memento_photos,
        agent=agent,
    )
    return {"path": s["path"], "messages": [s, m]}


# ---------- Parsing -------------------------------------------------------

def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
    return text


def _is_iso_date(s: str) -> bool:
    try:
        _date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _parse_chat_file(path: Path) -> list[Message]:
    if not path.exists():
        return []
    body = _strip_frontmatter(path.read_text())
    file_date = _date.fromisoformat(path.stem) if _is_iso_date(path.stem) else None

    messages = []
    current_heading = None
    current = None

    def _flush():
        nonlocal current
        if current is not None:
            current.text = current.text.strip()
            messages.append(current)
            current = None

    for raw in body.splitlines():
        line = raw.rstrip()

        m = _HEADING_RE.match(line)
        if m:
            _flush()
            current_heading = (m.group(1), m.group(2) or "")
            continue

        m = _SPEAKER_RE.match(line)
        if m:
            _flush()
            sender_raw = m.group(1).strip()
            agent_tag = m.group(2)
            rest = m.group(3)

            ts = " ".join(filter(None, current_heading)) if current_heading else ""
            current = Message(
                timestamp=ts.strip(),
                sender=sender_raw,
                text="",
                agent=(agent_tag or DEFAULT_AGENT).strip(),
                date=file_date.isoformat() if file_date else None,
            )
            inline_photos = _PHOTO_RE.findall(rest)
            text_remainder = _PHOTO_RE.sub("", rest).strip()
            for p in inline_photos:
                current.photos.append(p)
            if text_remainder:
                current.text = text_remainder
            continue

        if current is None:
            continue

        if not line.strip():
            continue

        if _PHOTO_FULL_RE.match(line.strip()):
            current.photos.append(line.strip())
        else:
            current.text = (current.text + ("\n" if current.text else "") + line)

    _flush()
    return messages


# ---------- Retrieve ------------------------------------------------------

def get_by_date(date, limit=None) -> list[dict]:
    d = _to_date(date)
    msgs = _parse_chat_file(_chat_file(d))
    if limit is not None:
        msgs = msgs[-limit:]
    return [m.to_dict() for m in msgs]


def get_today(limit=None) -> list[dict]:
    return get_by_date(_local_now().date(), limit=limit)


def _all_chat_files() -> list[Path]:
    if not CHAT_ROOT.exists():
        return []
    return sorted(CHAT_ROOT.rglob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))


def get_dates(year=None, month=None) -> list[str]:
    out = []
    for path in _all_chat_files():
        if not _is_iso_date(path.stem):
            continue
        d = _date.fromisoformat(path.stem)
        if year is not None and d.year != year:
            continue
        if month is not None and d.month != month:
            continue
        out.append(d.isoformat())
    return out


def get_recent(limit: int, since=None) -> list[dict]:
    if limit <= 0:
        return []
    since_date = _to_date(since) if since else None

    collected = []
    for path in reversed(_all_chat_files()):
        if not _is_iso_date(path.stem):
            continue
        d = _date.fromisoformat(path.stem)
        if since_date and d < since_date:
            break
        msgs = _parse_chat_file(path)
        for m in reversed(msgs):
            collected.append(m)
            if len(collected) >= limit:
                break
        if len(collected) >= limit:
            break
    collected.reverse()
    return [m.to_dict() for m in collected]


def search(query: str, since=None, until=None, sender=None) -> list[dict]:
    if not query:
        return []
    q = query.lower()
    since_d = _to_date(since) if since else None
    until_d = _to_date(until) if until else None

    results = []
    for path in _all_chat_files():
        if not _is_iso_date(path.stem):
            continue
        d = _date.fromisoformat(path.stem)
        if since_d and d < since_d:
            continue
        if until_d and d > until_d:
            continue
        for m in _parse_chat_file(path):
            if sender and m.sender != sender:
                continue
            if q in m.text.lower():
                results.append(m)
    return [m.to_dict() for m in results]


def get_summary(date_from=None, date_to=None) -> dict:
    f = _to_date(date_from) if date_from else None
    t = _to_date(date_to) if date_to else None

    exchanges = 0
    photos = 0
    days = set()
    senders = Counter()

    for path in _all_chat_files():
        if not _is_iso_date(path.stem):
            continue
        d = _date.fromisoformat(path.stem)
        if f and d < f:
            continue
        if t and d > t:
            continue
        msgs = _parse_chat_file(path)
        if msgs:
            days.add(d)
        for m in msgs:
            exchanges += 1
            photos += len(m.photos)
            senders[m.sender] += 1

    return {
        "exchange_count": exchanges,
        "day_count": len(days),
        "photo_count": photos,
        "top_senders": senders.most_common(),
        "range": {
            "from": f.isoformat() if f else None,
            "to": t.isoformat() if t else None,
        },
    }


# ---------- CLI -----------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="chat-signal-db CLI for testing")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_save = sub.add_parser("save")
    p_save.add_argument("sender")
    p_save.add_argument("text")
    p_save.add_argument("--photo", action="append", default=[])

    p_today = sub.add_parser("today")
    p_recent = sub.add_parser("recent")
    p_recent.add_argument("--limit", type=int, default=20)

    p_search = sub.add_parser("search")
    p_search.add_argument("query")

    p_summary = sub.add_parser("summary")

    p_dates = sub.add_parser("dates")
    p_dates.add_argument("--year", type=int)
    p_dates.add_argument("--month", type=int)

    args = parser.parse_args()

    if args.cmd == "save":
        print(json.dumps(save_message(args.sender, args.text, photos=args.photo), indent=2))
    elif args.cmd == "today":
        print(json.dumps(get_today(), indent=2))
    elif args.cmd == "recent":
        print(json.dumps(get_recent(args.limit), indent=2))
    elif args.cmd == "search":
        print(json.dumps(search(args.query), indent=2))
    elif args.cmd == "summary":
        print(json.dumps(get_summary(), indent=2))
    elif args.cmd == "dates":
        print(json.dumps(get_dates(year=args.year, month=args.month), indent=2))
