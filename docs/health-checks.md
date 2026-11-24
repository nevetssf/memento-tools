# Health checks: what's monitored, thresholds, and what to do

`memento_health.py` checks OpenClaw/Memento and everything it depends on, so breakage is
found by the monitor, not by Steve trying to talk to Memento. This is the reference for
reviewing the checks and the runbook for when one fails.

**Last reviewed: 2026-11-24** (also `LAST_REVIEWED` in the script, shown in `health.md`).
Review every few months: read the table, compare with what has broken since, then add,
retune or drop checks.

## How it runs

| Piece | Where | Schedule |
|---|---|---|
| Fast tier ("is it up?") | `memento-health-fast.timer` → `memento_health.py --tier fast` on boulder-claw-2 | every 15 min |
| Daily tier ("is it right?") | `memento-health-daily.timer` → `--tier daily` | 07:00 America/Denver |
| Status note | `~/obsidian-vault/health.md` (embedignored) | rewritten every run |
| State | `~/memento-data/health_state.json` | per check: status, since, last ok |
| Dead-man watcher | boulder-server: crontab → `~/bin/memento-deadman.sh` | every 15 min |
| Weekly "monitor alive" push | sent by the daily tier | every 7 days |

- **Alerts** go by **ntfy**, never Signal (Signal is one of the things checked):
  - **❌ on a new failure**, high priority;
  - **✅ on recovery**;
  - a **daily reminder** while a check stays failed.
  - `warn` never pushes; it only shows in `health.md`.
- **Grace periods:** a check with one reports `warn` until it has been bad for that long, then `fail`. That keeps restarts and blips quiet.
- **Errors inside a check** (exception, timeout) count as `fail`, so a broken check can't hide a problem.
- **Dead-man switch:** every fast run writes `date +%s` to `~/.memento-health-heartbeat` on boulder-server over SSH. If it's more than 45 min old, boulder-server pushes "boulder-claw-2 not checking in", once, and again on recovery. It also writes `~/.memento-deadman-lastrun` there, so you can confirm cron fires.
- **Weekly "🩺 Memento monitor alive"** (low priority) proves the ntfy path still reaches Steve's phone. **No push for over a week means ntfy or the daily tier is broken.**
- **Manual run:** `cd ~/memento-tools; set -a; . ~/.openclaw/.env; set +a; ./.venv/bin/python3 memento_health.py --tier all --dry-run` prints every result without saving state or pushing. `--only NAME …` runs a subset.

## Fast tier (every 15 min)

| Check | What it verifies | Fails when | Grace | When it fails |
|---|---|---|---|---|
| `gateway` | `openclaw-gateway.service` active and `GET 127.0.0.1:18789/health` says ok | unit inactive or health not ok | none | `systemctl --user status openclaw-gateway`, `journalctl --user -u openclaw-gateway -n 50`. Restart only when Memento is idle. |
| `signal` | `openclaw channels status --json`: Signal running + connected | not running/connected | 10 min | Check the gateway log (`/tmp/openclaw/openclaw-<UTC date>.log`, subsystem `channels/signal`). signal-cli runs under the gateway. For registration problems, ask Claude Code; it keeps notes on the bot number and re-registration. |
| `run_failures` | no `run-failed-before-reply` events in the transcript in the last 20 min | any | none | The detail has OpenClaw's message: OAuth expired → re-auth the Claude subscription (ask Claude Code); usage limit → wait or switch model. |
| `cron` | heartbeat job ran within 75 min; no enabled job with consecutive errors | either | none | `openclaw cron list` shows each job's last status; the gateway log has the run's error. |
| `chat_archive` | chat-reconcile's last run succeeded and its cursor matches the Signal DM session's newest event | lag > 0 | 15 min | `journalctl --user -u chat-reconcile -n 20`. A schema error means OpenClaw changed: see `docs/openclaw-transcripts.md`. |
| `bridge` | both brokers (boulder-server, sf-server :1883) accept TCP; `boulder-office-voice` online in the bridge. Displays offline are noted, not failed (battery gadgets). | broker down or satellite offline | 30 min | `ssh boulder-server docker ps` (eink-bridge, eink-mosquitto, memento-voice-bridge). Satellite: power, or the voice bridge's logs. |
| `dgx` | Whisper `:8770/v1/models`, Kokoro `:8771/health`, embed `:8790/v1/models`, vLLM `:8001/health` answer | any down | 20 min | `ssh -i ~/.ssh/id_ed25519_boulder_claw steven@dgx-spark docker ps`; watch for vLLM crash loops (`RestartCount`). |
| `local_services` | `embed-server` and `obsidian-sync` active; embed-server `/health` ok | any | none | `systemctl --user status <unit>`. |

## Daily tier (07:00 Denver)

| Check | What it verifies | Fails when (warns when) | When it fails |
|---|---|---|---|
| `mcp` | `openclaw mcp probe --json`: every MCP server lists tools | any server without tools | `openclaw mcp probe`; run the server by hand with `~/bin/memento-mcp.sh <script>` to see its error. |
| `mcp_leak` | copies of each `*-mcp.py` process | > 12 (warn > 6) | The stranded-bundle leak: check `mcp.sessionIdleTtlMs` is still set. Restarting the gateway when idle clears it. |
| `memory_index` | `openclaw memory status --json`: vector index `complete` | not complete (warn: dirty) | `openclaw memory index --force` (needed after an embedding-model change). |
| `vault_embed` | vault-embed finished at least once in 24 h; phase-2 backlog (chunks with no metadata) shrinking day over day | no finished run, or backlog not shrinking (warn: any timeout) | `journalctl --user -u vault-embed`; is the DGX LLM up and is `MEMENTO_CHAT_MODEL_NAME` in `vault-embed.service` still served? ("model-swap trap"). Runs cap phase 2 at 150 chunks (`VAULT_EMBED_P2_LIMIT`). |
| `embed_parity` | local and DGX embed servers serve the same model | differ (warn: DGX upstream unavailable) | Rebuild the DGX copy (`dgx-spark:~/embed-server/`) from current `embed-server.py`/`config.py`. |
| `backups` | newest restic snapshot under 26 h in both repos; `~/.openclaw/workspace` committed within 26 h and pushed | any | `journalctl --user -u restic-backup-sf` / `-boulder` / `openclaw-backup`. Repo integrity is the separate weekly `restic-check.timer`. |
| `signal_traffic` | last Signal message from Steve (transcript) and Signal ERRORs in the gateway log (a clean daemon exit on restart is ignored) | > 5 errors in 24 h or silence > 5 days (warn: any error, silence > 48 h) | Silence may just mean Steve hasn't written. Errors: read the log lines named in the detail. |
| `voice_turns` | last wake-word turn from the Office satellite, only while LOCATION.md says Boulder | none for 7 days (warn: 3 days) | Say the wake word and ask something. If nothing happens: voice bridge logs on boulder-server, device power/Wi-Fi. |
| `obsidian_sync` | Obsidian Sync state written recently | (warn only: > 6 h) | `journalctl --user -u obsidian-sync`; `obsidian-sync-restart.timer` restarts it every few hours. |
| `disk` | boulder-claw-2 `/` and DGX `/` ≥ 10 % free; DGX ≥ 8 GB RAM available | below either | Clean up; for DGX memory see the Sep 2026 OOM notes (model reloads under embed load). |
| `failed_units` | no failed systemd units (user + system), ignoring `app-*` desktop scopes | any | `systemctl [--user] status <unit>`. |
| `openclaw_version` | `openclaw config validate` passes; version unchanged since yesterday | validate fails, or version changed (one day) | After an upgrade: run the post-upgrade checklist with Claude Code (`docs/openclaw-transcripts.md`, "After an OpenClaw upgrade"; `openclaw doctor` warnings; MCP probe). Clears the next day. |

## Not covered (known gaps, accepted for now)

- **Model auth before it fails:** an expired login shows up after the next failed run (`run_failures`, or `cron` via the 30-min heartbeat), so up to about 45 minutes late.
- **Outbound Signal delivery:** no free end-to-end test. A send failure shows only as an ERROR in `signal_traffic`.
- **Quality:** checks prove services answer, not that answers are good (e.g. Whisper accuracy, reply sanity).
- **NAS hardware:** serveme/servemeboulder health beyond the weekly restic check.

## Changing the checks

- Each check is a function decorated with `@check(name, tier, title, grace_min)` in `memento_health.py`, returning `ok()`, `warn()` or `fail()` with a short detail.
- Add a row to this file in the same commit, and bump `LAST_REVIEWED` when you review.
- Checks removed from the script disappear from the state and note on the next run.
