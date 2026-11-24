"""Chat log: persistent record of Steven's conversation with the agent, all channels.

Stores chats as Markdown under CHAT_DIR (VAULT_DIR/Chats)/YYYY/MM/YYYY-MM-DD.md, with
attachments under CHAT_DIR/YYYY/MM/photos/ (audio/, video/, attachments/). Group chats
go under CHAT_DIR/Groups/<group>/YYYY/MM/.

Steven talks to Memento in one shared session over several channels (Signal, voice
devices, the Control UI), so one day file holds all of them. Each exchange is one
`##` section whose heading names its channel, and for voice the device and trigger:

    ## 14:02 MST · voice · boulder-office-voice (wake word)
    **Steven:** What's on my calendar?
    **Memento:** Just the 3 pm call.

The frontmatter lists the day's `channels` and `devices`.

Distinct from the daily journal: journal entries are Steven's logged life events;
chats are the full transcript of his exchanges with the agent (Memento).

Public API used by chat-log-mcp.py:
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

from config import VAULT_DIR, LOCATION_FILE, CHAT_DIR
import localtime

# ---------- Constants -----------------------------------------------------

CHAT_ROOT = Path(CHAT_DIR)
DEFAULT_AGENT = "main"
DEFAULT_CHANNEL = "signal"
VALID_SENDERS = {"Steven", "Memento", "system"}
# A Memento/system message joins the open section (the exchange it answers) when the
# section has the same channel label and started at most this many minutes earlier.
REPLY_WINDOW_MIN = 30

# Speaker label, optionally with `(agent)` suffix. Captures: name, agent, rest.
_SPEAKER_RE = re.compile(r"^\*\*([A-Za-z][A-Za-z _-]*?)(?:\s+\(([^)]+)\))?:\*\*\s*(.*)$")
# "## HH:MM TZ · channel · device (trigger)". Captures: time, tz, label.
_HEADING_RE = re.compile(
    r"^##\s+(\d{2}:\d{2})(?:\s+([A-Z]{3,5}))?(?:\s+·\s+(.+?))?[ \t]*$", re.MULTILINE)
_LABEL_CLEAN_RE = re.compile(r"[^\w .@-]+")
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
    channel: str | None = None
    device: str | None = None
    trigger: str | None = None

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
        for k in ("channel", "device", "trigger"):
            if getattr(self, k):
                d[k] = getattr(self, k)
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


def _chat_dir(d: _date, group: str | None = None) -> Path:
    root = CHAT_ROOT / "Groups" / _group_dirname(group) if group else CHAT_ROOT
    return root / f"{d.year:04d}" / f"{d.month:02d}"


def _group_dirname(group: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "-", group).strip(" .-") or "Unnamed Group"


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


def _chat_file(d: _date, group: str | None = None) -> Path:
    return _chat_dir(d, group) / f"{d.isoformat()}.md"


def get_path(date=None) -> str:
    return str(_chat_file(_to_date(date)))


def _ensure_file(path: Path, d: _date, group: str | None = None) -> None:
    # File-size check (not just existence) because _file_lock touches the
    # file before locking, so an empty file may exist with no frontmatter.
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"date: {d.isoformat()}\n"
        "channels: []\n"
        + (f'group: "{group}"\n' if group else "")
        + f"tags: [{'chat, group-chat' if group else 'chat'}]\n"
        "---\n"
    )
    path.write_text(frontmatter)


@contextlib.contextmanager
def _file_lock(file_path: Path):
    """Exclusive flock on the chat file itself. Touches the file if missing
    so the caller (_ensure_file) can populate frontmatter inside the lock."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if not file_path.exists():
        file_path.touch()
    with open(file_path, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


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

def _speaker_label(sender: str, agent: str, group: str | None = None) -> str:
    if sender not in VALID_SENDERS and not group:
        return f"unknown:{sender}"
    if sender == "Memento" and agent and agent != DEFAULT_AGENT:
        return f"Memento ({agent})"
    return sender


def _render_message(msg: Message, group: str | None = None) -> str:
    speaker = _speaker_label(msg.sender, msg.agent, group)
    if msg.photos and msg.text:
        body = [f"**{speaker}:**", *msg.photos, msg.text]
    elif msg.photos:
        body = [f"**{speaker}:**", *msg.photos]
    else:
        body = [f"**{speaker}:** {msg.text}"]
    return "\n".join(body)


def _clean_label(value) -> str:
    return _LABEL_CLEAN_RE.sub("", str(value or "")).strip()[:60]


def section_label(channel: str | None, device: str | None = None,
                  trigger: str | None = None) -> str:
    """'signal' / 'voice · boulder-office-voice (wake word)' — the heading's tail."""
    parts = [_clean_label(channel) or DEFAULT_CHANNEL]
    dev = _clean_label(device)
    if dev:
        trig = _clean_label(trigger)
        parts.append(f"{dev} ({trig})" if trig else dev)
    return " · ".join(parts)


def parse_label(label: str | None) -> dict:
    """Inverse of section_label: {'channel', 'device', 'trigger'} (None when absent)."""
    out = {"channel": None, "device": None, "trigger": None}
    if not label:
        return out
    parts = [p.strip() for p in label.split(" · ")]
    out["channel"] = parts[0] or None
    if len(parts) > 1:
        m = re.match(r"^(.*?)(?:\s+\(([^)]*)\))?$", parts[1])
        out["device"] = (m.group(1) or "").strip() or None
        out["trigger"] = (m.group(2) or "").strip() or None
    return out


def _render_heading(when: datetime, label: str | None = None) -> str:
    tz = when.strftime("%Z")
    head = f"## {when.strftime('%H:%M')}"
    if tz:
        head = f"{head} {tz}"
    if label:
        head = f"{head} · {label}"
    return head


def _fm_list_add(text: str, key: str, value: str) -> str:
    """Add `value` to the frontmatter flow list `key: [a, b]` (targeted, not a rewrite).

    Creates the line (before `tags:`, else at the end of the block) when missing.
    """
    value = _clean_label(value)
    m = re.match(r"^---\n(.*?\n)---\n", text, re.DOTALL)
    if not m or not value:
        return text
    fm = m.group(1)
    lm = re.search(rf"^{re.escape(key)}:[ \t]*\[(.*)\][ \t]*$", fm, re.MULTILINE)
    if lm:
        items = [x.strip() for x in lm.group(1).split(",") if x.strip()]
        if value in items:
            return text
        items.append(value)
        fm = fm[:lm.start()] + f"{key}: [{', '.join(items)}]" + fm[lm.end():]
    else:
        line = f"{key}: [{value}]\n"
        tm = re.search(r"^tags:", fm, re.MULTILINE)
        fm = fm[:tm.start()] + line + fm[tm.start():] if tm else fm + line
    return "---\n" + fm + "---\n" + text[m.end():]


# ---------- Save tools ----------------------------------------------------

def save_message(
    sender: str,
    text: str,
    timestamp=None,
    photos=None,
    agent: str = DEFAULT_AGENT,
    group: str | None = None,
    channel: str = DEFAULT_CHANNEL,
    device: str | None = None,
    trigger: str | None = None,
) -> dict:
    """Append a single message to today's (or timestamp's) chat file.

    `channel` (signal / voice / control-ui …), and for voice `device` and `trigger`,
    go in the section heading. A message from Steven (or any non-agent sender) opens
    a new section unless it lands in the same minute and channel as the last one; a
    Memento/system message joins the last section when it is on the same channel and
    started within REPLY_WINDOW_MIN minutes, so an exchange stays under one heading.

    With `group`, writes to Chats/Groups/<group>/... and accepts any sender name.
    """
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

    label = section_label(channel, device, trigger)
    msg = Message(
        timestamp=_render_heading(when).removeprefix("## "),
        sender=sender,
        text=text or "",
        photos=photo_links,
        agent=agent,
    )

    file_path = _chat_file(d, group)
    new_heading = _render_heading(when, label).strip()
    rendered_msg = _render_message(msg, group)

    with _file_lock(file_path):
        _ensure_file(file_path, d, group)
        existing = file_path.read_text()

        last = None
        for hm in _HEADING_RE.finditer(existing):
            last = hm

        join = False
        if last is not None:
            if last.group(0).strip() == new_heading:
                join = True
            elif sender in ("Memento", "system") and (last.group(3) or "").strip() == label:
                hh, mm = map(int, last.group(1).split(":"))
                gap = (when.hour * 60 + when.minute) - (hh * 60 + mm)
                join = 0 <= gap <= REPLY_WINDOW_MIN

        if join:
            new_content = existing.rstrip() + "\n\n" + rendered_msg + "\n"
        else:
            new_content = existing.rstrip() + "\n\n" + new_heading + "\n" + rendered_msg + "\n"

        new_content = _fm_list_add(new_content, "channels", _clean_label(channel) or DEFAULT_CHANNEL)
        if device:
            new_content = _fm_list_add(new_content, "devices", device)
        file_path.write_text(new_content)

    return {
        "path": str(file_path),
        "timestamp": msg.timestamp,
        "speaker": _speaker_label(sender, agent, group),
        "section": label,
    }


def save_exchange(
    steven_text: str,
    memento_text: str,
    timestamp=None,
    steven_photos=None,
    memento_photos=None,
    agent: str = DEFAULT_AGENT,
    channel: str = DEFAULT_CHANNEL,
    device: str | None = None,
    trigger: str | None = None,
) -> dict:
    where = {"channel": channel, "device": device, "trigger": trigger}
    s = save_message(
        sender="Steven",
        text=steven_text,
        timestamp=timestamp,
        photos=steven_photos,
        agent=agent,
        **where,
    )
    m = save_message(
        sender="Memento",
        text=memento_text,
        timestamp=timestamp,
        photos=memento_photos,
        agent=agent,
        **where,
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
    current_where = parse_label(None)
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
            current_where = parse_label(m.group(3))
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
                **current_where,
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


def search(query: str, since=None, until=None, sender=None, channel=None) -> list[dict]:
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
            if channel and (m.channel or DEFAULT_CHANNEL) != channel:
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


# ---------- One-time migration from Chats/Signal/ --------------------------

_LEGACY_HEADING_RE = re.compile(r"^(##\s+\d{2}:\d{2}(?:\s+[A-Z]{3,5})?)[ \t]*$", re.MULTILINE)


def migrate_from_signal(dry_run: bool = False) -> dict:
    """Move the pre-2026-11 layout Chats/Signal/... into Chats/... (idempotent).

    Day files: headings gain ' · signal', `channel: signal` becomes
    `channels: [signal]`, and wikilinks into Chats/Signal/ are repointed.
    Attachment dirs move as they are. Refuses to overwrite an existing target.
    """
    legacy = CHAT_ROOT / "Signal"
    moved, rewritten, conflicts = 0, 0, []
    if not legacy.exists():
        return {"moved": 0, "rewritten": 0, "conflicts": [], "note": "nothing to migrate"}

    files = sorted(p for p in legacy.rglob("*") if p.is_file())
    for src in files:
        dest = CHAT_ROOT / src.relative_to(legacy)
        if dest.exists():
            conflicts.append(str(dest))
    if conflicts:
        return {"moved": 0, "rewritten": 0, "conflicts": conflicts}

    vault = Path(VAULT_DIR)
    old_prefix = str(legacy.relative_to(vault)) + "/"
    new_prefix = str(CHAT_ROOT.relative_to(vault)) + "/"
    for src in files:
        dest = CHAT_ROOT / src.relative_to(legacy)
        if src.suffix == ".md" and _is_iso_date(src.stem):
            text = src.read_text()
            new = _LEGACY_HEADING_RE.sub(r"\1 · signal", text)
            new = re.sub(r"^channel:[ \t]*signal[ \t]*$", "channels: [signal]", new,
                         count=1, flags=re.MULTILINE)
            new = new.replace("[[" + old_prefix, "[[" + new_prefix)
            if new != text:
                rewritten += 1
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new)
                src.unlink()
        elif not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        moved += 1

    if not dry_run:
        for d in sorted((p for p in legacy.rglob("*") if p.is_dir()), reverse=True):
            with contextlib.suppress(OSError):
                d.rmdir()
        with contextlib.suppress(OSError):
            legacy.rmdir()
    return {"moved": moved, "rewritten": rewritten, "conflicts": [], "dry_run": dry_run}


# ---------- CLI -----------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="chat-log CLI for testing")
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

    p_migrate = sub.add_parser("migrate-from-signal",
                               help="one-time move of Chats/Signal/ into Chats/")
    p_migrate.add_argument("--dry-run", action="store_true")

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
    elif args.cmd == "migrate-from-signal":
        print(json.dumps(migrate_from_signal(dry_run=args.dry_run), indent=2))
    elif args.cmd == "dates":
        print(json.dumps(get_dates(year=args.year, month=args.month), indent=2))
