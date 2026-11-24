# OpenClaw transcripts: where they live and how chat_reconcile.py reads them

`chat_reconcile.py` depends on OpenClaw's **internal, undocumented** storage. This file
records what that storage looked like when the reader was written, so the next upgrade
can be checked against it. Observed on **OpenClaw 2026.10.9 (8b18b55)**, 2026-11-24.

## What changed in 2026.10.9

Before: `~/.openclaw/agents/main/sessions/sessions.json` (session index) plus one
`<session-id>.jsonl` transcript per session. The old reconciler tailed those files.

Since the 2026-11-01 upgrade both live in one SQLite database:

```
~/.openclaw/agents/main/agent/openclaw-agent.sqlite   (+ -wal, -shm)
```

`sessions/` still holds old `*.jsonl`, `*.jsonl.reset.*`, `*.deleted.*.zst` and
`*.trajectory*.json` files, but **no live session writes a .jsonl any more**. The
migration copied the old JSONL events into the database
(`agents/main/session-sqlite-import-archive/`).

The database is OpenClaw's live store. Open it **read-only**
(`file:…?mode=ro`, URI mode) and never write to it. There's no `sqlite3` CLI on
boulder-claw-2; use Python's `sqlite3`. A plain query needs the `vec0` extension
only if you touch the vector tables, so don't.

## Tables chat_reconcile.py uses

`REQUIRED_SCHEMA` in `chat_reconcile.py` checks these columns on every run and fails
loudly if one is missing.

### `session_windows`: one row per transcript (a session key can have several)

| column | meaning |
|---|---|
| `session_id` | transcript id (a UUID; the old `.jsonl` basename) |
| `session_key` | routing key, e.g. `agent:main:signal:direct:uuid:<steven>` |
| `created_at` | ms since epoch |
| `previous_session_id`, `reason` | chain after reset/rollover/compaction (`initial`, `reset`, `rollover`, `fork`, `rewind`, `switch`, `recovery`, `compaction`) |
| `channel`, `chat_type`, `display_name` | `signal` / `direct`·`group` / group name (often null) |

`session_nodes` maps each `session_key` to its `current_session_id`, and its
`entry_json` holds what `sessions.json` used to hold (tokens, model, delivery…).
The reconciler doesn't need it. It walks every window whose key matches
`agent:main:signal:%` or is `agent:main:main`.

### `transcript_events`: the transcript itself

| column | meaning |
|---|---|
| `session_id` | → `session_windows.session_id` |
| `seq` | 1-based, increasing per session; **the reconciler's cursor** |
| `event_json` | the event as JSON text, or NULL when compressed |
| `event_zstd` | zstd-compressed JSON for large events (attachments, long replies); decoded with the `zstd` CLI |
| `created_at` | ms since epoch |
| `event_utf8_bytes`, `navigation_json` | unused |

Related but unused: `session_transcript_active_events` (which events are in the live
context after compaction; compaction does **not** delete rows from
`transcript_events`), `transcript_event_identities`, the `session_transcript_fts*`
full-text index, and `session_transcript_archives` (zstd blobs of reset/deleted
transcripts).

## Event JSON shapes

```jsonc
{"type": "session", "version": 3, "id": "<session_id>", "timestamp": "…Z", "cwd": "…"}   // seq 1

{"type": "message", "id": "…", "parentId": "…", "timestamp": "2026-11-24T15:00:00.000Z",
 "message": {"role": "user",                    // or "assistant"
             "content": "text" | [{"type": "text", "text": "…"}],
             "timestamp": 1795…,                // ms
             "idempotencyKey": "…",             // runs since 2026.10.9
             "__openclaw": {"senderIsOwner": true,
                            "senderName": "Steven Kasapi", "senderId": "…",
                            "transport": {"channel": "signal", "conversationRef": "conv_…",
                                          "messageId": "…", "replyToId": "…"},
                            "media": [{"path": "…/media/inbound/…", "contentType": "image/jpeg"}]}}}

{"type": "custom_message", "customType": "run-failed-before-reply",
 "content": "Your request couldn't be completed: …", "display": true, "details": {…}}
```

Assistant `content` (claude-cli runs) contains only `text` parts; tool calls aren't in
the transcript.

## How each channel is recognised

Steven's DM session (`agent:main:signal:direct:uuid:…`) is shared by every channel.
The memento-voice-agent wrapper writes into it, and `session.dmScope` is
per-channel-peer.

| channel | user event looks like |
|---|---|
| `signal` | `__openclaw.transport.channel == "signal"`. Text is what he typed. Images arrive as `[Image attachment …]` / `[User sent media without caption]`, voice notes as `[Audio]\nTranscript:\n…`, documents with the extracted text inlined as `<file name="…" mime="…">…</file>` (collapsed to `[attached file: name]`). |
| `voice` | no transport. Text is `[voice · <where>] <words>\n\n(Spoken to you …)` from `~/bin/memento-voice-agent`. `<where>` is `<device name> at <site>`. The note says "after its wake word" (WAKE_NOTE) or "push-to-talk" (VOICE_NOTE). The satellite reports its room ("Office") as its name, which `DEVICE_ALIASES` maps to `boulder-office-voice`. |
| `control-ui` | transport channel `webchat` (mapped). None seen yet since the upgrade; adjust `CHANNEL_ALIASES` if it shows up as something else. |
| skipped | no transport and no voice prefix: heartbeat polls (`[OpenClaw heartbeat poll]`, all of `agent:main:main` so far), scheduler "Automation repair request…" prompts, cron/test runs, and events imported from pre-upgrade JSONL (no `idempotencyKey`). Assistant replies after a skipped user event are skipped too. Assistant `NO_REPLY` / `HEARTBEAT_OK` are dropped. |

## Cut-off and state

The old archive (`Chats/Signal/…`, now `Chats/…`) ends at seq 34 of session
`70f2202b-…` (2026-11-01T20:23:51Z, "You can kill the flight monitor"). The first run
was `chat_reconcile.py --init-cutoff 2026-11-01T20:23:51.176Z`, which sets each
session's cursor to its last event at or before then. State:
`~/memento-data/chat_reconcile_state.json` = `{"version": 2, "cursors": {session_id: seq}}`.
The last alerted error is in `chat_reconcile_state.alert`. Without v2 state the script
refuses to run rather than re-append history.

## After an OpenClaw upgrade

1. `systemctl --user status chat-reconcile` and `journalctl --user -u chat-reconcile -n 20`:
   a schema change shows as `OpenClaw schema changed: …` (exit 1, plus an ntfy push).
2. Even if it's green, check a new Signal message and a voice turn reach today's
   `Chats/` file. A silent shape change (say, transport renamed) would skip messages
   without erroring.
3. Quick look at the shapes:

```python
import sqlite3, json
c = sqlite3.connect("file:/home/steven/.openclaw/agents/main/agent/openclaw-agent.sqlite?mode=ro", uri=True)
sid = c.execute("select current_session_id from session_nodes where session_key like 'agent:main:signal:direct:uuid:%'").fetchone()[0]
for seq, ej in c.execute("select seq, event_json from transcript_events where session_id=? order by seq desc limit 4", (sid,)):
    print(seq, (ej or "<zstd>")[:300])
```

4. If sessions moved again, check `openclaw sessions --help`. As of 2026.10.9 it
   lists, compacts, archives and exports trajectories, but has no transcript export,
   which is why we read the database.
