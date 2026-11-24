#!/usr/bin/env python3
"""memento_health.py — scheduled health checks for OpenClaw/Memento and what it depends on.

Runs outside OpenClaw on purpose: Memento can't report its own outage if the gateway or
Signal is down, and a script costs no tokens. Two tiers, each driven by a systemd user
timer:

  fast   (every 15 min)  is it up?   gateway, Signal, failed runs, cron, chat archiving,
                                     display bridge + brokers + voice satellite, DGX, local services
  daily  (07:00 Denver)  is it right? MCP servers, MCP process leak, memory index, vault-embed,
                                     embed parity, backups, Obsidian Sync, disk, failed units,
                                     OpenClaw version / config

Each check returns ok / warn / fail. A check can have a grace period: it reports `warn`
until it has been bad for that long, then `fail`. Alerts go by ntfy (not Signal, which
is being checked): one push when a check starts failing, one when it recovers, and a
daily reminder while it stays failed. `warn` never pushes; it shows in the status note.

State: config.HEALTH_STATE_FILE. Status note: config.HEALTH_NOTE (vault root health.md).
Every fast run also refreshes a heartbeat file on boulder-server; a cron job there pushes
ntfy if it goes stale, so a dead boulder-claw-2 still gets noticed.

Usage: memento_health.py [--tier fast|daily|all] [--dry-run] [--only NAME ...]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

from config import (CHAT_RECONCILE_STATE_FILE, EINK_API_TOKEN, EINK_API_URL,
                    HEALTH_NOTE, HEALTH_STATE_FILE, OPENCLAW_AGENT_DB)

HOME = Path.home()
# systemd gives a minimal PATH; the openclaw CLI is a node script (#!/usr/bin/env node).
os.environ["PATH"] = os.pathsep.join([str(HOME / ".local/node/bin"), str(HOME / ".local/bin"),
                                      str(HOME / "bin"), os.environ.get("PATH", "/usr/bin:/bin")])
OPENCLAW = str(HOME / ".local/bin/openclaw")
RESTIC = str(HOME / "bin/restic")
NTFY = HOME / "bin/ntfy-send.sh"
DGX_SSH = ["ssh", "-i", str(HOME / ".ssh/id_ed25519_boulder_claw"), "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=8", "steven@dgx-spark"]
HEARTBEAT_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "boulder-server"]
SIGNAL_SESSION_PREFIX = "agent:main:signal:direct:uuid:"
VOICE_DEVICES = ["boulder-office-voice"]          # alert if offline; displays are battery gadgets
RESTIC_REPOS = {
    "SF": ("serveme", HOME / ".ssh/id_ed25519_serveme_backup",
           HOME / ".config/restic/serveme.passphrase"),
    "Boulder": ("servemeboulder", HOME / ".ssh/id_ed25519_servemeboulder_backup",
                HOME / ".config/restic/servemeboulder.passphrase"),
}
DENVER = ZoneInfo("America/Denver")


# ---------- framework ---------------------------------------------------------

@dataclass
class Result:
    status: str          # ok | warn | fail
    detail: str = ""


def ok(detail=""):
    return Result("ok", detail)


def warn(detail):
    return Result("warn", detail)


def fail(detail):
    return Result("fail", detail)


@dataclass
class Check:
    name: str
    tier: str
    title: str
    fn: Callable[[dict], Result]
    grace_min: int = 0          # bad for less than this → warn, not fail
    ctx: dict = field(default_factory=dict)


CHECKS: list[Check] = []


def check(name, tier, title, grace_min=0):
    def deco(fn):
        CHECKS.append(Check(name, tier, title, fn, grace_min))
        return fn
    return deco


def run(cmd, timeout=60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def http(url, timeout=8, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                timeout=timeout) as r:
        body = r.read()
        return r.status, body


def http_json(url, timeout=8, token=None):
    return json.loads(http(url, timeout, token)[1])


def openclaw_json(*args, timeout=90):
    p = run([OPENCLAW, *args, "--json"], timeout=timeout, cwd=HOME)
    out = p.stdout
    start = min([i for i in (out.find("{"), out.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise RuntimeError(f"openclaw {' '.join(args)}: no JSON (exit {p.returncode}) "
                           f"{p.stderr.strip()[-200:]}")
    return json.loads(out[start:])


def agent_db():
    return sqlite3.connect(f"file:{OPENCLAW_AGENT_DB}?mode=ro", uri=True, timeout=10)


def unit_prop(unit, *props):
    p = run(["systemctl", "--user", "show", unit, "-p", ",".join(props)])
    return dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)


def ago(ts: float) -> str:
    s = int(time.time() - ts)
    return f"{s // 60} min ago" if s < 7200 else (f"{s // 3600} h ago" if s < 172800
                                                  else f"{s // 86400} days ago")


# ---------- fast tier -------------------------------------------------------------

@check("gateway", "fast", "OpenClaw gateway answers")
def _gateway(ctx):
    if unit_prop("openclaw-gateway.service", "ActiveState")["ActiveState"] != "active":
        return fail("openclaw-gateway.service is not active")
    status, body = http("http://127.0.0.1:18789/health", timeout=5)
    data = json.loads(body)
    return ok("live") if data.get("ok") else fail(f"/health says {data}")


@check("signal", "fast", "Signal channel connected", grace_min=10)
def _signal(ctx):
    d = openclaw_json("channels", "status")
    sig = (d.get("channels") or {}).get("signal")
    if not sig:
        return fail("no Signal channel in channels status")
    if not (sig.get("running") and sig.get("connected")):
        return fail(f"running={sig.get('running')} connected={sig.get('connected')} "
                    f"lastError={sig.get('lastError')}")
    if sig.get("lastError"):
        return warn(f"connected, last error: {sig['lastError']}")
    return ok("running, connected")


@check("run_failures", "fast", "No failed Memento runs (auth, usage limits)")
def _run_failures(ctx):
    since_ms = int((time.time() - 20 * 60) * 1000)
    with agent_db() as con:
        rows = con.execute(
            "select event_json from transcript_events where created_at > ? and "
            "event_json like '%run-failed-before-reply%'", (since_ms,)).fetchall()
    if not rows:
        return ok("none in the last 20 min")
    last = json.loads(rows[-1][0]).get("content", "")[:160]
    return fail(f"{len(rows)} failed run(s) in the last 20 min: {last}")


@check("cron", "fast", "OpenClaw cron jobs healthy (heartbeat ran)")
def _cron(ctx):
    jobs = openclaw_json("cron", "list")["jobs"]
    bad, hb_last = [], None
    for j in jobs:
        st = j.get("state") or {}
        if j.get("declarationKey") == "heartbeat:main":
            hb_last = (st.get("lastRunAtMs") or 0) / 1000
        if j.get("enabled") and (st.get("consecutiveErrors") or 0) > 0:
            bad.append(f"{j.get('displayName') or j.get('name')}: "
                       f"{st.get('consecutiveErrors')} errors ({st.get('lastStatus')})")
    if hb_last is None:
        return fail("no heartbeat job found")
    if time.time() - hb_last > 75 * 60:
        return fail(f"heartbeat last ran {ago(hb_last)}")
    if bad:
        return fail("; ".join(bad))
    return ok(f"{len(jobs)} jobs, heartbeat {ago(hb_last)}")


@check("chat_archive", "fast", "Chat log keeping up with the transcript", grace_min=15)
def _chat_archive(ctx):
    r = unit_prop("chat-reconcile.service", "Result", "ExecMainExitTimestampMonotonic")
    if r.get("Result") not in ("success", None):
        return fail(f"chat-reconcile last result: {r.get('Result')}")
    state = json.loads(Path(CHAT_RECONCILE_STATE_FILE).read_text())
    cursors = state.get("cursors") or {}
    with agent_db() as con:
        row = con.execute(
            "select w.session_id, max(e.seq) from session_windows w join transcript_events e "
            "using(session_id) where w.session_key like ? group by w.session_id "
            "order by max(e.created_at) desc limit 1", (SIGNAL_SESSION_PREFIX + "%",)).fetchone()
    if not row:
        return warn("no Signal DM session found")
    lag = row[1] - cursors.get(row[0], 0)
    return ok("up to date") if lag <= 0 else fail(f"{lag} transcript events not archived")


@check("bridge", "fast", "Display bridge, brokers and voice satellite", grace_min=30)
def _bridge(ctx):
    problems = []
    for host in ("boulder-server", "sf-server"):
        try:
            socket.create_connection((host, 1883), timeout=5).close()
        except OSError as e:
            problems.append(f"broker {host}:1883 unreachable ({e})")
    devices = http_json(f"{EINK_API_URL}/api/devices", token=EINK_API_TOKEN)
    by_id = {d.get("id"): d for d in devices}
    for dev in VOICE_DEVICES:
        if (by_id.get(dev) or {}).get("status") != "online":
            problems.append(f"{dev} is {(by_id.get(dev) or {}).get('status', 'missing')}")
    offline = [d.get("name") for d in devices if d.get("status") != "online"
               and d.get("id") not in VOICE_DEVICES]
    if problems:
        return fail("; ".join(problems))
    note = f"; displays offline: {', '.join(offline)}" if offline else ""
    return ok(f"{len(devices)} devices{note}")


@check("dgx", "fast", "DGX Spark services answer", grace_min=20)
def _dgx(ctx):
    endpoints = {
        "whisper": "http://dgx-spark:8770/v1/models",
        "kokoro": "http://dgx-spark:8771/health",
        "embed": "http://dgx-spark:8790/v1/models",
        "vllm": "http://dgx-spark:8001/health",
    }
    down = []
    for name, url in endpoints.items():
        try:
            http(url, timeout=8)
        except Exception as e:
            down.append(f"{name} ({type(e).__name__})")
    return fail("down: " + ", ".join(down)) if down else ok("whisper, kokoro, embed, vllm")


@check("local_services", "fast", "Local services running")
def _local_services(ctx):
    bad = []
    for unit in ("embed-server.service", "obsidian-sync.service"):
        if unit_prop(unit, "ActiveState")["ActiveState"] != "active":
            bad.append(unit)
    try:
        h = http_json("http://127.0.0.1:8790/health", timeout=5)
        if h.get("status") != "ok":
            bad.append(f"embed-server health {h.get('status')}")
    except Exception as e:
        bad.append(f"embed-server /health ({type(e).__name__})")
    return fail(", ".join(bad)) if bad else ok("embed-server, obsidian-sync")


# ---------- daily tier ---------------------------------------------------------------

@check("mcp", "daily", "All MCP servers answer")
def _mcp(ctx):
    d = openclaw_json("mcp", "probe", timeout=180)
    servers = d.get("servers") or {}
    bad = [f"{n}: {s.get('error') or 'no tools'}" for n, s in servers.items()
           if not s.get("tools")]
    return fail("; ".join(bad)) if bad else ok(f"{len(servers)} servers")


@check("mcp_leak", "daily", "No MCP process leak")
def _mcp_leak(ctx):
    p = run(["pgrep", "-af", r"python3? .*-mcp\.py"])
    counts: dict[str, int] = {}
    for line in p.stdout.splitlines():
        script = next((w for w in line.split() if w.endswith("-mcp.py")), None)
        if script:
            counts[Path(script).name] = counts.get(Path(script).name, 0) + 1
    worst = max(counts.values(), default=0)
    detail = f"max {worst} copies of one server ({len(counts)} servers)"
    if worst > 12:
        return fail(detail + "; likely the stranded-bundle leak")
    return warn(detail) if worst > 6 else ok(detail)


@check("memory_index", "daily", "Memento's memory index active")
def _memory_index(ctx):
    d = openclaw_json("memory", "status", timeout=120)
    st = (d[0] if isinstance(d, list) else d).get("status", {})
    vec = (st.get("vector") or {}).get("index", {}).get("state")
    if vec != "complete":
        return fail(f"vector index state {vec!r} (paused after a model change? "
                    "run `openclaw memory index --force`)")
    if st.get("dirty"):
        return warn(f"vector index complete but dirty; {st.get('files')} files")
    return ok(f"{st.get('files')} files, {st.get('chunks')} chunks, {st.get('model')}")


@check("vault_embed", "daily", "Vault search index current")
def _vault_embed(ctx):
    r = run(["journalctl", "--user", "-u", "vault-embed.service", "--since", "-24h",
             "--no-pager", "-o", "cat"])
    timeouts = r.stdout.count("start operation timed out")
    finished = r.stdout.count("Finished vault-embed.service")
    db = HOME / "obsidian-vault-index/vault-embed.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    pending = con.execute("select count(*) from chunks where metadata is null").fetchone()[0]
    con.close()
    prev = ctx.get("prev_pending")
    ctx["prev_pending"] = pending
    detail = f"{finished} runs finished, {timeouts} timed out in 24 h; {pending} chunks await metadata"
    if finished == 0:
        return fail(detail)
    if pending and prev is not None and pending >= prev:
        return fail(detail + f" (was {prev} yesterday: phase 2 not progressing)")
    return warn(detail) if timeouts else ok(detail)


@check("embed_parity", "daily", "Local and DGX embed servers match")
def _embed_parity(ctx):
    local = http_json("http://127.0.0.1:8790/v1/models")["data"][0]["id"]
    dgx = http_json("http://dgx-spark:8790/v1/models")["data"][0]["id"]
    if local != dgx:
        return fail(f"local {local} vs DGX {dgx}")
    up = (http_json("http://127.0.0.1:8790/health").get("upstream") or {})
    if not up.get("available"):
        return warn(f"both {local}; DGX upstream unavailable ({up.get('last_error')})")
    return ok(local)


@check("backups", "daily", "Backups recent (restic SF + Boulder, config repo)")
def _backups(ctx):
    problems, notes = [], []
    for name, (host, key, pw) in RESTIC_REPOS.items():
        env = dict(os.environ, RESTIC_REPOSITORY=f"sftp:backup@{host}:/Backups/boulder-claw",
                   RESTIC_PASSWORD_FILE=str(pw))
        p = run([RESTIC, "-o", f"sftp.command=ssh -i {key} -o BatchMode=yes "
                 f"-o ConnectTimeout=15 backup@{host} -s sftp", "snapshots", "--latest", "1",
                 "--host", socket.gethostname(), "--json"], timeout=180, env=env)
        try:
            snaps = json.loads(p.stdout)
            t = datetime.fromisoformat(snaps[-1]["time"]).timestamp()
        except Exception:
            problems.append(f"{name}: can't list snapshots ({p.stderr.strip()[-120:]})")
            continue
        (problems if time.time() - t > 26 * 3600 else notes).append(f"{name} {ago(t)}")
    ws = HOME / ".openclaw/workspace"
    last = int(run(["git", "-C", str(ws), "log", "-1", "--format=%ct"]).stdout.strip() or 0)
    ahead = run(["git", "-C", str(ws), "rev-list", "--count", "@{u}..HEAD"]).stdout.strip()
    if time.time() - last > 26 * 3600:
        problems.append(f"openclaw-backup last commit {ago(last)}")
    elif ahead not in ("0", ""):
        problems.append(f"openclaw-backup {ahead} commit(s) not pushed")
    else:
        notes.append(f"config repo {ago(last)}")
    return fail("; ".join(problems)) if problems else ok(", ".join(notes))


@check("obsidian_sync", "daily", "Obsidian Sync active")
def _obsidian_sync(ctx):
    files = list((HOME / ".config/obsidian-headless/sync").glob("*/state.db*"))
    if not files:
        return fail("no Obsidian Sync state found")
    newest = max(f.stat().st_mtime for f in files)
    return warn(f"sync state last written {ago(newest)}") if time.time() - newest > 6 * 3600 \
        else ok(f"sync state written {ago(newest)}")


@check("disk", "daily", "Disk and memory headroom")
def _disk(ctx):
    problems, notes = [], []
    u = shutil.disk_usage("/")
    pct = u.free / u.total * 100
    (problems if pct < 10 else notes).append(f"boulder-claw-2 / {pct:.0f}% free")
    p = run([*DGX_SSH, "df --output=pcent / | tail -1; free -m | awk '/Mem:/{print $7}'"],
            timeout=30)
    try:
        used, avail = p.stdout.split()
        dgx_free = 100 - int(used.rstrip("%"))
        (problems if dgx_free < 10 else notes).append(f"DGX / {dgx_free}% free")
        (problems if int(avail) < 8192 else notes).append(f"DGX {int(avail) // 1024} GB RAM available")
    except ValueError:
        problems.append(f"DGX unreachable over SSH ({p.stderr.strip()[-100:]})")
    return fail("; ".join(problems + notes)) if problems else ok(", ".join(notes))


@check("failed_units", "daily", "No failed systemd units")
def _failed_units(ctx):
    names = []
    for scope in (["--user"], []):
        p = run(["systemctl", *scope, "list-units", "--failed", "--plain", "--no-legend"])
        names += [l.split()[0] for l in p.stdout.splitlines()
                  if l.strip() and not l.split()[0].startswith("app-")]
    return fail(", ".join(names)) if names else ok("none")


@check("openclaw_version", "daily", "OpenClaw version and config")
def _openclaw_version(ctx):
    v = run([OPENCLAW, "--version"], cwd=HOME).stdout.strip().splitlines()[0]
    prev = ctx.get("version")
    ctx["version"] = v
    val = run([OPENCLAW, "config", "validate"], timeout=60, cwd=HOME)
    if val.returncode != 0:
        return fail(f"config validate failed: {(val.stdout + val.stderr).strip()[-200:]}")
    if prev and prev != v:
        return fail(f"upgraded: {prev} → {v}. Run the post-upgrade checklist "
                    "(memento-tools/docs/openclaw-transcripts.md) with Claude Code; "
                    "this clears tomorrow")
    return ok(v)


# ---------- runner -------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(Path(HEALTH_STATE_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {"checks": {}}


def _save_state(state: dict) -> None:
    p = Path(HEALTH_STATE_FILE)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(p)


def notify(title, body, priority="high", tags="rotating_light"):
    if NTFY.exists():
        subprocess.run([str(NTFY), title, priority, tags, body], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def evaluate(chk: Check, rec: dict, now: float) -> tuple[Result, list[tuple]]:
    """Run one check and update its record; return (result, notifications)."""
    ctx = rec.setdefault("ctx", {})
    try:
        res = chk.fn(ctx)
    except Exception as e:
        res = fail(f"check error: {type(e).__name__}: {str(e)[:200]}")
    if res.status == "ok":
        rec.pop("bad_since", None)
    else:
        rec.setdefault("bad_since", now)
        if res.status == "fail" and chk.grace_min and now - rec["bad_since"] < chk.grace_min * 60:
            res = warn(res.detail + f" (fails after {chk.grace_min} min)")
    out = []
    prev = rec.get("status")
    if res.status == "fail" and (prev != "fail" or now - rec.get("alerted_at", 0) > 23.5 * 3600):
        out.append((f"❌ Memento health: {chk.title}", res.detail, "high", "rotating_light"))
        rec["alerted_at"] = now
    elif res.status != "fail" and prev == "fail":
        out.append((f"✅ Recovered: {chk.title}", res.detail or "ok", "default",
                    "white_check_mark"))
        rec.pop("alerted_at", None)
    rec.update(status=res.status, detail=res.detail, last_run=now, title=chk.title,
               tier=chk.tier)
    if res.status == "ok":
        rec["last_ok"] = now
    return res, out


def write_note(state: dict, now: float) -> None:
    order = {"fail": 0, "warn": 1, "ok": 2}
    icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
    recs = sorted(state["checks"].items(),
                  key=lambda kv: (order.get(kv[1].get("status"), 3), kv[1].get("tier", ""), kv[0]))
    counts = {s: sum(1 for _, r in recs if r.get("status") == s) for s in order}
    stamp = lambda t: datetime.fromtimestamp(t, DENVER).strftime("%Y-%m-%d %H:%M %Z") if t else "never"
    lines = [
        "# Memento Health",
        "",
        "_Auto-generated by `memento-tools/memento_health.py` (systemd timers: fast every 15 min, "
        "daily 07:00 Denver). Excluded from indexing via `.embedignore`. Alerts go by ntfy._",
        "",
        f"- **Overall:** {icon['fail'] if counts['fail'] else icon['warn'] if counts['warn'] else icon['ok']} "
        f"{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail",
        f"- **Fast checks last ran:** {stamp(state.get('fast_ran'))}",
        f"- **Daily checks last ran:** {stamp(state.get('daily_ran'))}",
        "",
        "If either time above is more than an hour (fast) or a day (daily) old, the checker "
        "itself has stopped.",
        "",
        "| | Check | Tier | Detail | Last ok |",
        "|---|---|---|---|---|",
    ]
    for name, r in recs:
        detail = (r.get("detail") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {icon.get(r.get('status'), '?')} | {r.get('title', name)} | "
                     f"{r.get('tier')} | {detail} | {stamp(r.get('last_ok'))} |")
    note = Path(HEALTH_NOTE)
    note.parent.mkdir(parents=True, exist_ok=True)
    tmp = note.with_name("." + note.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(note)


def heartbeat() -> None:
    """Dead-man switch: boulder-server's cron alerts if this file goes stale."""
    try:
        run([*HEARTBEAT_SSH, "date +%s > ~/.memento-health-heartbeat"], timeout=20)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tier", choices=["fast", "daily", "all"], default="fast")
    ap.add_argument("--dry-run", action="store_true", help="print, don't notify or save")
    ap.add_argument("--only", nargs="*", help="run only these checks")
    args = ap.parse_args()

    state = _load_state()
    now = time.time()
    selected = [c for c in CHECKS if (args.tier == "all" or c.tier == args.tier)
                and (not args.only or c.name in args.only)]
    pushes = []
    for chk in selected:
        rec = state["checks"].setdefault(chk.name, {})
        if args.dry_run:
            rec = json.loads(json.dumps(rec))
        res, out = evaluate(chk, rec, now)
        pushes += out
        print(f"{res.status:4}  {chk.name:16} {res.detail}")
    if args.dry_run:
        return 0
    known = {c.name for c in CHECKS}
    state["checks"] = {k: v for k, v in state["checks"].items() if k in known}
    for tier in ("fast", "daily"):
        if args.tier in (tier, "all"):
            state[f"{tier}_ran"] = now
    _save_state(state)
    write_note(state, now)
    for title, body, prio, tags in pushes:
        notify(title, body, prio, tags)
    if args.tier in ("fast", "all"):
        heartbeat()
    return 1 if any(state["checks"][c.name]["status"] == "fail" for c in selected) else 0


if __name__ == "__main__":
    sys.exit(main())
