#!/usr/bin/env python3
"""chat_reconcile.py — sync OpenClaw transcripts → the chat log (Chats/ in the vault).

Since OpenClaw 2026.10.9 the session index and every transcript live in the agent
database (config.OPENCLAW_AGENT_DB, ~/.openclaw/agents/main/agent/openclaw-agent.sqlite),
not in sessions.json + <uuid>.jsonl files. This reconciler opens that database
read-only, walks the conversation sessions' transcript events past a per-session
cursor (last `seq` written), and appends Steven's and Memento's turns to the chat
log via chat_log.save_message, tagging every exchange with its channel:

  signal      user event has __openclaw.transport.channel == "signal"
  voice       user text starts "[voice · <device> …]" (the memento-voice-agent
              wrapper); the device and trigger (push-to-talk / wake word) are parsed
              from it and the wrapper's instruction note is dropped
  control-ui  transport channel "webchat"/"control-ui"
  <other>     any other transport channel, by name

User events with no channel (heartbeat polls, scheduler/cron prompts, test runs, and
events imported from pre-2026.10 JSONL, which predate the archive cut-off anyway) are
skipped, and so are the assistant replies that follow them. Attachments inlined as
<file …>…</file> are reduced to "[attached file: name]". The schema this depends on is
OpenClaw-internal and undocumented: see docs/openclaw-transcripts.md.

State (config.CHAT_RECONCILE_STATE_FILE): {"version": 2, "cursors": {session_id: seq}};
the last alerted error sits beside it in chat_reconcile_state.alert. The first run needs --init-cutoff ISO_TS
(everything at or before it is treated as already archived) so a missing state file
can never re-append the whole history.

Spoken announcements (the `speak` tool, which isn't in the transcript) come from the
display bridge's per-device `spoke` events and are logged as
`voice · <device> (agent-initiated <reason>)` or `(requested)`.

Failures exit 1 and push one ntfy notification per distinct error (and one when it
recovers), so this can't fail silently again.

Run via systemd timer (every 60s) or manually: chat_reconcile.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

import chat_log as cl
import localtime
from config import (LOCATION_FILE, CHAT_RECONCILE_STATE_FILE, OPENCLAW_AGENT_DB,
                    EINK_API_URL, EINK_API_TOKEN)

STATE_FILE = CHAT_RECONCILE_STATE_FILE
STATE_VERSION = 2
NTFY = Path.home() / "bin/ntfy-send.sh"
SCHEMA_DOC = "memento-tools/docs/openclaw-transcripts.md"

# Sessions whose turns are conversation with Steven (or his groups). agent:main:main
# is included only for channel-tagged messages (Control UI chat); its heartbeat polls
# carry no transport and are skipped by classification.
SESSION_KEY_PATTERNS = ("agent:main:signal:%", "agent:main:main")

# Columns this script reads; checked up front so a schema change fails loudly.
REQUIRED_SCHEMA = {
    "session_windows": {"session_id", "session_key", "created_at"},
    "transcript_events": {"session_id", "seq", "event_json", "event_zstd", "created_at"},
}

CHANNEL_ALIASES = {"webchat": "control-ui", "web": "control-ui", "control-ui": "control-ui"}
# Device labels the voice wrapper used before it named the device itself.
DEVICE_ALIASES = {"Office": "boulder-office-voice", "memento-voice": "boulder-office-voice"}
# Voice devices with no button: always "wake word" (the wrapper labelled their first
# turns push-to-talk before it had a wake-word note).
WAKE_WORD_ONLY_DEVICES = {"boulder-office-voice"}
# The bridge can be down briefly (deploys, restarts); only alert after this long.
BRIDGE_GRACE_S = 15 * 60
SKIP_ASSISTANT_TEXT = {"NO_REPLY", "HEARTBEAT_OK"}

_META_BLOCK_RE = re.compile(
    r"^[A-Z][A-Za-z ]+\(untrusted metadata\):\s*\n```json\s*\n.*?\n```\s*\n+",
    re.DOTALL | re.MULTILINE,
)
# "[voice · <where>] <text>\n\n(Spoken to you …)"
_VOICE_RE = re.compile(r"^\[voice · (?P<where>[^\]\n]+)\][ \t]*(?P<body>.*)$", re.DOTALL)
_FILE_BLOCK_RE = re.compile(r'<file name="([^"]*)"[^>]*>.*?</file>', re.DOTALL)
_VOICE_NOTE_RE = re.compile(r"\n\n\(Spoken to you .*\)\s*$", re.DOTALL)


class ReconcileError(Exception):
    pass


# ---------- state ------------------------------------------------------------

def _load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        st = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ReconcileError(f"unreadable state file {STATE_FILE}: {e}")
    if st.get("version") != STATE_VERSION:
        return None  # pre-2026.10 offsets state: needs --init-cutoff
    return st


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


# ---------- database ---------------------------------------------------------

def _connect() -> sqlite3.Connection:
    if not OPENCLAW_AGENT_DB.exists():
        raise ReconcileError(f"OpenClaw agent DB not found at {OPENCLAW_AGENT_DB}")
    try:
        con = sqlite3.connect(f"file:{OPENCLAW_AGENT_DB}?mode=ro", uri=True, timeout=10)
        for table, cols in REQUIRED_SCHEMA.items():
            have = {r[1] for r in con.execute(f'pragma table_info("{table}")')}
            missing = cols - have
            if missing:
                raise ReconcileError(
                    f"OpenClaw schema changed: {table} lacks {sorted(missing)} "
                    f"(see {SCHEMA_DOC})")
    except sqlite3.Error as e:
        raise ReconcileError(f"cannot read OpenClaw agent DB: {e}")
    return con


def _sessions(con) -> list[tuple[str, str]]:
    where = " or ".join("session_key like ?" for _ in SESSION_KEY_PATTERNS)
    return list(con.execute(
        f"select session_id, session_key from session_windows where {where} "
        "order by created_at", SESSION_KEY_PATTERNS))


def _decode(event_json, event_zstd) -> dict | None:
    """Event body: plain JSON, or zstd-compressed (large events) via the zstd CLI."""
    raw = event_json
    if raw is None and event_zstd is not None:
        try:
            raw = subprocess.run(["zstd", "-dcq"], input=event_zstd, capture_output=True,
                                 check=True, timeout=30).stdout.decode("utf-8")
        except (OSError, subprocess.SubprocessError) as e:
            raise ReconcileError(f"cannot decompress transcript event (zstd CLI): {e}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _events(con, session_id: str, after_seq: int):
    for seq, ej, ez in con.execute(
            "select seq, event_json, event_zstd from transcript_events "
            "where session_id = ? and seq > ? order by seq", (session_id, after_seq)):
        yield seq, _decode(ej, ez)


def _cursor_at(con, session_id: str, cutoff_ms: int) -> int:
    row = con.execute("select max(seq) from transcript_events where session_id = ? "
                      "and created_at <= ?", (session_id, cutoff_ms)).fetchone()
    return row[0] or 0


# ---------- parsing ----------------------------------------------------------

def _text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    pieces = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "text":
            t = (part.get("text") or "").strip()
            if t:
                pieces.append(t)
    return "\n\n".join(pieces)


def _strip_metadata(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _META_BLOCK_RE.sub("", text, count=1)
    return text.strip()


def _collapse_files(text: str) -> str:
    """Replace inlined attachments '<file name="x---<uuid>.pdf" …>…</file>' with a note."""
    def note(m):
        name = re.sub(r"---[0-9a-f-]{36}(?=\.\w+$|$)", "", m.group(1))
        return f"[attached file: {name}]"
    return _FILE_BLOCK_RE.sub(note, text).strip()


def parse_voice(text: str) -> dict | None:
    """'[voice · boulder-epaper-mini at boulder] hi\\n\\n(Spoken … push-to-talk …)' →
    {'device', 'trigger', 'text'}; None if not a voice turn."""
    m = _VOICE_RE.match(text)
    if not m:
        return None
    body = m.group("body")
    note = _VOICE_NOTE_RE.search(body)
    trigger = None
    if note:
        trigger = "wake word" if "wake word" in note.group(0) else "push-to-talk"
        body = body[:note.start()]
    where = m.group("where").strip()
    device = re.split(r" in | at ", where, maxsplit=1)[0].strip()
    device = DEVICE_ALIASES.get(device, device)
    if device in WAKE_WORD_ONLY_DEVICES:
        trigger = "wake word"
    return {"device": device, "trigger": trigger, "text": body.strip()}


def classify_user(msg: dict, session_key: str) -> dict | None:
    """Where a user message came from: {'channel','device','trigger','text','sender'}
    or None when it isn't Steven talking (heartbeat, cron, test run)."""
    text = _text(msg.get("content"))
    if not text or text.startswith("A new session was started via /new or /reset"):
        return None
    oc = msg.get("__openclaw") or {}
    sender = (oc.get("senderName") or "Steven").split()[0]
    voice = parse_voice(text)
    if voice:
        return {"channel": "voice", "device": voice["device"], "trigger": voice["trigger"],
                "text": voice["text"], "sender": "Steven"}
    transport = oc.get("transport") or {}
    channel = transport.get("channel") if isinstance(transport, dict) else None
    if not channel:
        return None
    channel = CHANNEL_ALIASES.get(channel, channel)
    text = _collapse_files(_strip_metadata(text))
    if not text:
        return None
    return {"channel": channel, "device": None, "trigger": None, "text": text,
            "sender": sender}


def _group_name(con, session_key: str) -> str | None:
    if ":group:" not in session_key:
        return None
    row = con.execute("select display_name from session_windows where session_key = ? "
                      "and display_name is not null limit 1", (session_key,)).fetchone()
    return (row[0] if row else None) or "Memento Group"


# ---------- reconcile --------------------------------------------------------

def _local_zone() -> ZoneInfo:
    location = LOCATION_FILE.read_text().strip() if Path(LOCATION_FILE).exists() else None
    return ZoneInfo(localtime.get_localtime(location=location)["timezone"])


def _when(obj: dict, tz: ZoneInfo) -> datetime | None:
    ts = obj.get("timestamp")
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
    msg_ts = (obj.get("message") or {}).get("timestamp")
    if isinstance(msg_ts, (int, float)):
        return datetime.fromtimestamp(msg_ts / 1000, tz)
    return None


def _reconcile_session(con, session_id, session_key, state, tz, dry_run) -> tuple[int, int]:
    cursors = state["cursors"]
    after = cursors.get(session_id, 0)
    group = _group_name(con, session_key)
    current = None  # where the exchange in progress came from
    written = skipped = 0
    for seq, obj in _events(con, session_id, after):
        if obj is None:
            skipped += 1
            cursors[session_id] = seq
            continue
        out = None
        typ = obj.get("type")
        msg = obj.get("message") or {}
        if typ == "message" and msg.get("role") == "user":
            current = classify_user(msg, session_key)
            out = current
        elif typ == "message" and msg.get("role") == "assistant" and current:
            text = _text(msg.get("content"))
            if text and text not in SKIP_ASSISTANT_TEXT:
                out = {**current, "sender": "Memento", "text": text}
        elif typ == "custom_message" and current and obj.get("display"):
            text = obj.get("content") if isinstance(obj.get("content"), str) else ""
            if text.strip():
                out = {**current, "sender": "system", "text": text.strip()}
        if out:
            when = _when(obj, tz)
            if when is None:
                skipped += 1
            else:
                if not dry_run:
                    cl.save_message(sender=out["sender"], text=out["text"], timestamp=when,
                                    group=group, channel=out["channel"],
                                    device=out["device"], trigger=out["trigger"])
                written += 1
        cursors[session_id] = seq
    return written, skipped


# ---------- spoken output (bridge `spoke` events) ---------------------------

def _bridge_get(path: str):
    req = urllib.request.Request(f"{EINK_API_URL}{path}",
                                 headers={"Authorization": f"Bearer {EINK_API_TOKEN}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def spoke_trigger(reason: str | None) -> str:
    reason = (reason or "requested").strip()
    return "requested" if reason == "requested" else f"agent-initiated {reason}"


def _reconcile_spoken(state: dict, tz: ZoneInfo, dry_run: bool) -> int:
    """Archive what Memento said aloud through `speak` (bridge `spoke` events, ok only).

    Tool calls aren't in OpenClaw's transcript, so this is the only record of spoken
    announcements. Refused attempts are skipped: Memento sends those on Signal, which
    the transcript captures. The bridge keeps events in memory (its seq resets on
    restart), so the cursor is the last event timestamp per device; a device seen for
    the first time starts from now. Raises ReconcileError after BRIDGE_GRACE_S of the
    bridge being unreachable.
    """
    if not EINK_API_TOKEN:
        raise ReconcileError("EINK_API_TOKEN not set (chat-reconcile.service reads "
                             "~/.openclaw/.env); spoken announcements aren't archived")
    cursors = state.setdefault("spoke_ts", {})
    try:
        devices = _bridge_get("/api/devices")
        batches = []
        for d in devices:
            dev_id = d.get("id")
            if not dev_id:
                continue
            if dev_id not in cursors:
                cursors[dev_id] = time.time()
                continue
            batches.append((d, _bridge_get(f"/api/devices/{dev_id}/events?since=0&limit=200")))
    except (OSError, ValueError) as e:
        since = state.setdefault("bridge_fail_since", time.time())
        if time.time() - since > BRIDGE_GRACE_S:
            raise ReconcileError(f"display bridge unreachable for "
                                 f"{int((time.time() - since) / 60)} min ({EINK_API_URL}): {e}")
        return 0
    state.pop("bridge_fail_since", None)

    written = 0
    for d, events in batches:
        dev_id = d["id"]
        name = d.get("name") or dev_id
        label = DEVICE_ALIASES.get(name, name)
        last = cursors[dev_id]
        for e in sorted(events, key=lambda e: e.get("ts", 0)):
            ts = e.get("ts") or 0
            data = e.get("data") or {}
            if e.get("type") != "spoke" or ts <= cursors[dev_id]:
                continue
            last = max(last, ts)
            text = (data.get("text") or "").strip()
            if not data.get("ok") or not text:
                continue
            if not dry_run:
                cl.save_message(sender="Memento", text=text,
                                timestamp=datetime.fromtimestamp(ts, tz), channel="voice",
                                device=label, trigger=spoke_trigger(data.get("reason")))
            written += 1
        cursors[dev_id] = last
    return written


def reconcile(init_cutoff: str | None = None, dry_run: bool = False) -> dict:
    con = _connect()
    state = _load_state()
    sessions = _sessions(con)
    if state is None:
        if not init_cutoff:
            raise ReconcileError(
                f"no v{STATE_VERSION} state at {STATE_FILE}; run once with "
                "--init-cutoff <ISO timestamp of the last archived message>")
        cutoff_ms = int(datetime.fromisoformat(init_cutoff.replace("Z", "+00:00"))
                        .timestamp() * 1000)
        state = {"version": STATE_VERSION,
                 "cursors": {sid: _cursor_at(con, sid, cutoff_ms) for sid, _ in sessions}}
    tz = _local_zone()
    total_w = total_s = 0
    try:
        for sid, key in sessions:
            w, s = _reconcile_session(con, sid, key, state, tz, dry_run)
            total_w += w
            total_s += s
        spoken = _reconcile_spoken(state, tz, dry_run)
    finally:
        if not dry_run:
            _save_state(state)
    return {"messages_written": total_w, "events_skipped": total_s,
            "spoken_written": spoken, "sessions": len(sessions), "dry_run": dry_run}


def _notify(title: str, body: str, priority: str = "high", tags: str = "warning") -> None:
    if NTFY.exists():
        subprocess.run([str(NTFY), title, priority, tags, body], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


ALERT_FILE = STATE_FILE.with_suffix(".alert")


def _set_alert(error: str | None) -> None:
    """Notify once per distinct error (remembered in ALERT_FILE), and once on recovery."""
    prev = ALERT_FILE.read_text() if ALERT_FILE.exists() else None
    if error == prev:
        return
    if error:
        _notify("chat-reconcile failing", f"{error}\n\nChats are not being archived.")
        ALERT_FILE.write_text(error)
    else:
        _notify("chat-reconcile recovered", "Chat archiving is working again.",
                priority="default", tags="white_check_mark")
        ALERT_FILE.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--init-cutoff", help="first run only: ISO timestamp of the last "
                    "message already in the chat log")
    ap.add_argument("--dry-run", action="store_true", help="count, don't write")
    args = ap.parse_args()
    try:
        result = reconcile(init_cutoff=args.init_cutoff, dry_run=args.dry_run)
    except Exception as e:
        err = str(e) if isinstance(e, ReconcileError) else f"{type(e).__name__}: {e}"
        print(json.dumps({"error": err}), file=sys.stderr)
        if not args.dry_run:
            _set_alert(err)
        return 1
    if not args.dry_run:
        _set_alert(None)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
