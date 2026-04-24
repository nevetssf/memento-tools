# memento-tools

A personal collection of MCP servers and command-line utilities that sit between an
[Obsidian](https://obsidian.md) vault and an AI agent. Built originally for use with
[OpenClaw](https://openclaw.ai), but the MCP servers are framework-agnostic — anything
that speaks the [Model Context Protocol](https://modelcontextprotocol.io) can use them.

## What it does

- **Journal** — log timestamped entries to a daily Obsidian note, with people-tagging
  and topical tag suggestions wired into the agent's workflow.
- **People & relationships** — a SQLite-backed database of people, with an inference
  engine for family relationships (siblings, cousins, in-laws, etc.) that reads
  parent/child links.
- **Cellar** — wine and spirits tasting notes (wine, whiskey, gin, vodka, tequila,
  mezcal, rum, port) with templates per category, inline Dataview-style fields, and
  per-producer files.
- **Vault semantic search** — local vector index of the entire vault using
  [`sqlite-vec`](https://github.com/asg017/sqlite-vec), embedded by
  [Qwen3-Embedding-4B](https://huggingface.co/Qwen/Qwen3-Embedding-4B) running locally,
  with FTS5 keyword fallback. Two-phase indexing (embed first, enrich metadata later).
- **Vault search** — keyword / structural search across the vault.
- **Cellar inventory** — list / search / rate bottles.
- **Todoist** — list, create, complete tasks across projects and labels.
- **Format converters** — turn PDFs and HTML files into sidecar `.md` files so they
  show up cleanly in Obsidian and embed well.

## Architecture

```
~/memento-tools/
├── config.py                    Env-var-driven paths and model endpoints
├── requirements.txt             Locked Python deps
├── setup.sh                     One-step venv bootstrap
├── .venv/                       Project-local Python environment
│
│  MCP servers (registered in your agent's mcp config)
├── journal-mcp.py               log_entry, init_journal, get/add/set tags,
│                                add_journal_person, get_journal_summary,
│                                set_location, log_weather, log_photo, ...
├── people-mcp.py                find_person, show_person, add_person,
│                                relate, get_relatives, between, etc.
├── cellar-mcp.py                add_producer, add_bottle, update_bottle,
│                                rate_bottle, search_cellar, get_types
├── vault-mcp.py                 search_vault, read_note, append_note, write_note
├── vault-embed-mcp.py           index_vault, semantic_search, search_recent,
│                                index_progress, enrich_metadata, index_status
├── todoist-mcp.py               list/add/complete/update/delete tasks
│
│  Underlying scripts (also runnable from the CLI)
├── people.py                    Relationship CRUD + inference engine
├── journal_fm.py                Frontmatter helpers (split / parse / replace)
├── journal-log.py               Append a timestamped entry, manage frontmatter
├── journal-header.py            Read/write tags, scalar fields, people
├── journal-location.py          Manage location field in frontmatter
├── journal-summary.py           Extract a date range for AI summarization
├── journal-weather.py           Fetch weather, log to journal, optional Signal
├── journal-photo-log.py         Copy a photo into Journal/YYYY/photos/, log it
├── journal-pdf.py               Render a journal day as a styled PDF
├── priorities.py                Daily priorities tracker
├── priorities-rollover.py       Move yesterday's open items to today
├── localtime.py                 Local time/date for Steven's current location
├── vault-search.py              Full-text search across vault sections
├── vault_embed.py               Vector indexer / semantic search core
├── pdf2md.py                    PDF → sidecar Markdown (pymupdf4llm + vision fallback)
├── html2md.py                   HTML/HTM → sidecar Markdown (markdownify)
├── morning-report.py            Daily greeting via Signal
├── check-important-email.py     Surface high-priority email
└── tests/
    ├── test_people.py
    └── test_priorities.py
```

## Setup

Requires Python 3.12+ and an Obsidian vault.

```bash
git clone https://github.com/nevetssf/memento-tools.git ~/memento-tools
cd ~/memento-tools
./setup.sh                       # creates .venv and installs requirements.txt
```

## Configuration

Everything deployment-specific is read from environment variables, with sensible
defaults for Steven's setup. Override what you need:

| Variable | Default | Purpose |
|---|---|---|
| `MEMENTO_VAULT_DIR` | `~/obsidian-vault` | Root of your Obsidian vault |
| `MEMENTO_JOURNAL_DIR` | `$VAULT/Journal` | Journal subdirectory |
| `MEMENTO_DB_PATH` | `$VAULT/people.db` | People SQLite database |
| `MEMENTO_PEOPLE_DIR` | `$VAULT/People` | Folder for per-person Obsidian notes |
| `MEMENTO_CELLAR_DIR` | `$VAULT/Cellar` | Wine/spirits notes |
| `MEMENTO_TEMPLATES_DIR` | `$VAULT/Templates` | Note templates (cellar uses these) |
| `MEMENTO_LOCATION_FILE` | `~/.openclaw/workspace/LOCATION.md` | Current city/state |
| `MEMENTO_SOUL_FILE` | `~/.openclaw/workspace/SOUL.md` | Agent persona file |
| `MEMENTO_EMBED_DB_PATH` | `~/obsidian-vault-index/vault-embed.db` | Vector index |
| `MEMENTO_EMBED_MODEL_URL` | `http://192.168.6.19:1234/v1` | Embedding endpoint (LM Studio / vLLM) |
| `MEMENTO_EMBED_MODEL_NAME` | `text-embedding-qwen3-embedding-4b` | Embedding model |
| `MEMENTO_EMBED_DIMS` | `2560` | Vector dimensions |
| `MEMENTO_CHAT_MODEL_URL` | `http://192.168.6.19:1234/v1` | Chat LLM endpoint |
| `MEMENTO_CHAT_MODEL_NAME` | `qwen3.6-35b-a3b@q8_k_xl` | Chat LLM (used for metadata extraction & distillation) |
| `MEMENTO_SIGNAL_TARGET` | (empty) | Signal UUID for notifications (e.g. `uuid:...`) |
| `MEMENTO_TODOIST_TOKEN` | (empty) | Todoist API token (https://todoist.com/app/settings/integrations/developer) |
| `MEMENTO_EMAIL_ACCOUNTS` | (empty) | Comma-list of `email:flags` for `check-important-email.py` |

For OpenClaw, set these in `openclaw.json` under `env`. For other agents, export them
in the shell that launches the MCP servers.

### MCP server registration (OpenClaw example)

```json
{
  "mcp": {
    "servers": {
      "people-db":   {"command": "/path/to/.venv/bin/python3", "args": ["/path/to/memento-tools/people-mcp.py"]},
      "journal-db":  {"command": "/path/to/.venv/bin/python3", "args": ["/path/to/memento-tools/journal-mcp.py"]},
      "vault-db":    {"command": "/path/to/.venv/bin/python3", "args": ["/path/to/memento-tools/vault-mcp.py"]},
      "cellar-db":   {"command": "/path/to/.venv/bin/python3", "args": ["/path/to/memento-tools/cellar-mcp.py"]},
      "vault-embed": {"command": "/path/to/.venv/bin/python3", "args": ["/path/to/memento-tools/vault-embed-mcp.py"]},
      "todoist":     {"command": "/path/to/.venv/bin/python3", "args": ["/path/to/memento-tools/todoist-mcp.py"]}
    }
  }
}
```

## Vault semantic search

Two-phase indexing: chunk + embed first (search becomes useful immediately), then
extract LLM metadata in a second pass.

```bash
# Default: phase 1 then phase 2 (full pipeline, single command)
./.venv/bin/python3 vault_embed.py --reconcile

# Phase 1 only (fast); enrich later
./.venv/bin/python3 vault_embed.py --reconcile --no-metadata
./.venv/bin/python3 vault_embed.py --enrich-metadata

# Filter by file type
./.venv/bin/python3 vault_embed.py --reconcile --text-only   # skip PDFs
./.venv/bin/python3 vault_embed.py --reconcile --pdf-only    # only PDFs

# Subdirectory
./.venv/bin/python3 vault_embed.py --reconcile --path Cellar
```

What you get:
- Vector search via `sqlite-vec` (2560-dim Qwen3-Embedding vectors)
- Keyword search via SQLite FTS5 — automatically merged with vector results via
  Reciprocal Rank Fusion
- LLM-extracted metadata per chunk: `people`, `topics`, `action_items`, `dates_mentioned`
- Frontmatter shortcut: Journal/People/Cellar files derive metadata directly from
  frontmatter and skip the LLM call entirely (~60× faster for those)
- Move detection via content hash — renames don't waste re-embedding
- Resumable; idempotent; safe to interrupt

### `.embedignore`

A `.gitignore`-style file at the vault root excludes paths from the index. Example:

```gitignore
# Patterns without '/' glob-match against any path component (matches at any depth):
Reference*

# Index sidecar .md instead of the source files:
*.pdf
*.html
*.htm

# Negate to re-include a specific file:
!path/to/important.pdf
```

The defaults always-skipped: `.obsidian/`, `.trash/`, `Templates/`, `Reference`.

### Frontmatter opt-out

Add `embed: false` to a note's frontmatter to skip it. Existing index entries for that
file get deleted automatically.

## PDF → Markdown

```bash
./.venv/bin/python3 pdf2md.py            # convert every PDF in the vault
./.venv/bin/python3 pdf2md.py path.pdf   # specific files
./.venv/bin/python3 pdf2md.py --force    # re-convert even if hash matches
./.venv/bin/python3 pdf2md.py --dry-run  # preview
```

- Uses [`pymupdf4llm`](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/) for fast
  structure-preserving extraction (preserves headings, lists, tables)
- Falls back to a vision LLM (default: `qwen2-vl-7b-instruct`) for image-only PDFs
  (scans, photos of documents)
- Writes a sidecar `Foo.md` next to `Foo.pdf` with frontmatter recording
  `source_pdf_hash` so re-runs are idempotent
- Refuses to overwrite existing `.md` unless `--force` is passed (your edits are safe)

## HTML → Markdown

```bash
./.venv/bin/python3 html2md.py
```

Same shape as `pdf2md.py`. Uses BeautifulSoup to strip non-content
(`script`/`style`/`nav`/`footer`/etc.), then [`markdownify`](https://pypi.org/project/markdownify/)
for the conversion. Useful for old web clippings that Obsidian can't render usefully.

## Cellar templates

Each spirit category (Wine, Whiskey, Gin, Vodka, Tequila, Mezcal, Rum, Port) has a
matching template in `$VAULT/Templates/{Type} Note.md`. The `cellar-mcp.py` reads
these at runtime — change a template, and new entries reflect the change.

Per-producer files live in `$VAULT/Cellar/{Type}/{Producer}.md` with multiple bottle
entries per file, each with inline Dataview-style fields:

```markdown
---
vintner: Orin Swift
region: Napa Valley, CA
country: USA
tags: [wine]
---

# Orin Swift

*Napa Valley, CA, USA*

---

## Abstract — Grenache / Petite Syrah / Syrah

varietal:: Grenache, Petite Syrah, Syrah
date_tasted:: 2012-01-14

Tried at Swirl. Sweet, raspberries and strawberries. Good for quaffing.
```

## Journal entries

Entries are appended chronologically to `Journal/YYYY/YYYY-MM-DD.md`. The frontmatter
maintains:
- `tags` — topical tags (work, social, food, travel, etc.)
- `people` — direct interactions, formatted as `[Name (id), ...]` with people-DB ids
  in italics-like inline notation
- `location` — current city/state (auto-set on file creation)

The `log_entry` MCP tool handles file creation, chronological insertion, frontmatter
updates, and weather logging on first call of the day.

## People relationships

`people.py` is a CRUD tool plus a relationship inference engine. Family relationships
beyond direct parent/child are *computed* from the parent graph at query time
(siblings = same parents; cousins = same grandparents; etc.) and cached in the
`relationships` table with `inferred=1`. Explicit relationships (`spouse`, `friend`,
`coworker`, etc.) are stored directly with `inferred=0`.

```bash
./.venv/bin/python3 people.py find "Joe"
./.venv/bin/python3 people.py show "Joe Greco" --pretty
./.venv/bin/python3 people.py relate --person "A" --relative "B" --relative-is parent
./.venv/bin/python3 people.py rebuild-inferred
./.venv/bin/python3 people.py graph "Steven" --depth 3      # Mermaid family tree
./.venv/bin/python3 people.py links                          # Update Obsidian wiki-links
```

## Tests

```bash
./.venv/bin/python3 -m unittest discover tests
```

Smoke tests for `people.py` (relationship inference) and `priorities.py`.

## Design choices worth knowing

- **No raw file overwrites** — every script that touches files reads-then-modifies
  with targeted regex replacements, never full parse-and-rebuild. This was a hard-won
  lesson; an earlier "parse the whole frontmatter, modify dict, write back" approach
  silently corrupted other fields.
- **Frontmatter shortcut** — files in Journal/People/Cellar already have structured
  metadata in frontmatter. The vault indexer reads that directly instead of asking
  the LLM, which is ~60× faster.
- **Idempotency everywhere** — `index_vault`, `pdf2md`, `html2md`, `add_producer`,
  `enrich_metadata` are all safe to re-run. They detect already-done work and skip.
- **Stdin-isatty progress bars** — CLI invocations get a `tqdm` bar; subprocess /
  systemd / MCP background runs don't. Same code path, different UX.
- **Hash-based dedup and move detection** — files renamed without content changes
  are recognized as moves and don't trigger re-embedding.

## License

Personal use. No warranty. Patches welcome via GitHub issues.
