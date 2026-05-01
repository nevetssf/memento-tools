#!/usr/bin/env python3
"""chat_reconcile.py — sync OpenClaw agent session JSONL → chat-signal-db.

OpenClaw writes every agent turn (user input + assistant reply) to per-session
JSONL files at ~/.openclaw/agents/main/sessions/<uuid>.jsonl. A separate index
at ~/.openclaw/agents/main/sessions/sessions.json maps each session-key to its
file plus channel metadata.

This reconciler walks that index, picks Signal sessions, tails each session
file from the last-known byte offset, extracts user (Steven) and assistant
(Memento) text turns, and appends them to the chat-signal-db Markdown
transcript via chat_signal.save_message.

State (per-file byte offset) lives at ~/memento-tools/.chat_reconcile_state.json
so successive runs only process new lines.

Why an offset-based reconciler instead of an OpenClaw hook?
The Signal channel-extension's outbound delivery path bypasses OpenClaw's
internal `message:sent` hook. The session JSONL is the only complete record
of agent replies, so we read from it rather than from the hook bus.

Run via systemd timer (every 60s) or manually: chat_reconcile.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

import chat_signal as cs
import localtime
from config import LOCATION_FILE

SESSIONS_JSON = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
STATE_FILE = SCRIPTS_DIR / ".chat_reconcile_state.json"

# Inbound user messages from a channel are wrapped with one or more
# "<Heading> (untrusted metadata):\n```json\n{...}\n```" blocks before the
# real text. Strip them. Each block is followed by a blank line.
_META_BLOCK_RE = re.compile(
    r"^[A-Z][A-Za-z ]+\(untrusted metadata\):\s*\n```json\s*\n.*?\n```\s*\n+",
    re.DOTALL | re.MULTILINE,
)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def _signal_sessions(sessions: dict):
    """Yield (sessionKey, sessionFile) for sessions that route via Signal."""
    for key, val in sessions.items():
        if ":signal:" not in key:
            continue
        if not isinstance(val, dict):
            continue
        sf = val.get("sessionFile")
        if sf and Path(sf).exists():
            yield key, sf


def _local_zone() -> ZoneInfo:
    """Steven's current local zone, per LOCATION.md."""
    location = LOCATION_FILE.read_text().strip() if Path(LOCATION_FILE).exists() else None
    info = localtime.get_localtime(location=location)
    return ZoneInfo(info["timezone"])


def _to_local_dt(utc_iso: str, tz: ZoneInfo) -> datetime:
    """Convert a JSONL UTC timestamp string to a ZoneInfo-aware datetime.

    Pass the resulting datetime to chat_signal.save_message *as a datetime*,
    not as an ISO string — ISO strings round-trip through fromisoformat and
    end up with a fixed-offset tzinfo that strftime('%Z') renders as
    'UTC-07:00' instead of 'PDT'.
    """
    s = utc_iso.replace("Z", "+00:00") if utc_iso.endswith("Z") else utc_iso
    return datetime.fromisoformat(s).astimezone(tz)


def _strip_metadata(text: str) -> str:
    """Remove leading 'X (untrusted metadata)' JSON blocks from a user message."""
    prev = None
    out = text
    while prev != out:
        prev = out
        out = _META_BLOCK_RE.sub("", out, count=1)
    return out.strip()


def _extract_text(content) -> str:
    """Concatenate text parts from a message's content list. Skip thinking/tool parts."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    pieces = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            t = (part.get("text") or "").strip()
            if t:
                pieces.append(t)
    return "\n\n".join(pieces)


def _parse_message(obj: dict):
    """Return (utc_iso_ts, sender, text) for a JSONL line, or None to skip."""
    if obj.get("type") != "message":
        return None
    msg = obj.get("message", {})
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None

    text = _extract_text(msg.get("content"))
    if not text:
        return None

    # Skip the synthetic post-/reset kickoff (a "user" message OpenClaw
    # injects to prompt the session-startup sequence). It's not really
    # something Steven typed.
    if role == "user" and text.startswith("A new session was started via /new or /reset"):
        return None

    ts = obj.get("timestamp")
    if not ts:
        return None

    if role == "user":
        text = _strip_metadata(text)
        if not text:
            return None
        sender = "Steven"
    else:
        sender = "Memento"

    return ts, sender, text


def _reconcile_file(session_file: str, state: dict, tz: ZoneInfo) -> tuple[int, int]:
    """Process new lines in one session file. Returns (written, skipped)."""
    last_offset = state.get(session_file, 0)
    path = Path(session_file)
    size = path.stat().st_size
    if last_offset > size:
        # File was truncated/rotated; restart from the top.
        last_offset = 0
    if last_offset == size:
        return 0, 0

    written = 0
    skipped = 0
    new_offset = last_offset

    with open(path, "rb") as f:
        f.seek(last_offset)
        for raw in f:
            new_offset += len(raw)
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue

            parsed = _parse_message(obj)
            if not parsed:
                continue

            utc_iso, sender, text = parsed
            local_dt = _to_local_dt(utc_iso, tz)
            try:
                cs.save_message(sender=sender, text=text, timestamp=local_dt)
                written += 1
            except Exception as e:
                print(f"[reconcile] save failed for {sender}: {e}", file=sys.stderr)
                skipped += 1

    state[session_file] = new_offset
    return written, skipped


def reconcile() -> dict:
    """Run one reconciliation pass over all Signal sessions."""
    if not SESSIONS_JSON.exists():
        return {"error": f"no sessions index at {SESSIONS_JSON}"}

    try:
        sessions = json.loads(SESSIONS_JSON.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"unreadable sessions index: {e}"}

    state = _load_state()
    tz = _local_zone()

    total_written = 0
    total_skipped = 0
    files = 0
    for _key, sf in _signal_sessions(sessions):
        files += 1
        w, s = _reconcile_file(sf, state, tz)
        total_written += w
        total_skipped += s

    _save_state(state)
    return {
        "messages_written": total_written,
        "lines_skipped": total_skipped,
        "files_processed": files,
        "state_path": str(STATE_FILE),
    }


if __name__ == "__main__":
    result = reconcile()
    print(json.dumps(result, indent=2))
