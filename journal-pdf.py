#!/usr/bin/env python3
"""
Convert one or more journal entries into a styled PDF.

Usage:
    journal-pdf.py --dates 2026-03-28
    journal-pdf.py --dates 2026-03-28..2026-04-02         # inclusive range
    journal-pdf.py --dates 2026-03-28,2026-04-01          # explicit list
    journal-pdf.py --dates 2026-03-28..2026-03-31,2026-04-05   # mixed
    journal-pdf.py                                         # today
    journal-pdf.py --dates ... --output /tmp/foo.pdf

`--date` is accepted as an alias of `--dates` for backward compatibility.

Multi-day output renders one PDF with each day flowing into the next
(no forced page break). A thin separator and a day-header introduce
each day's entry. Per-day people / location / priorities are inline.

Uses weasyprint from a dedicated venv.
"""

import argparse
import base64
import io
import os
import re
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import JOURNAL_DIR

# Ensure we can import from the venv
VENV_SITE = Path.home() / ".local/share/journal-pdf-venv/lib"
venv_dirs = sorted(VENV_SITE.glob("python*/site-packages"))
if venv_dirs:
    sys.path.insert(0, str(venv_dirs[-1]))

import markdown
import weasyprint
import yaml
from PIL import Image

JOURNAL_BASE = JOURNAL_DIR
DEFAULT_OUTPUT_DIR = None  # default: same directory as the journal .md file

CSS = textwrap.dedent("""\
    @page {
        size: letter;
        margin: 0.75in 1in;
        @bottom-center {
            content: counter(page);
            font-size: 9pt;
            color: #888;
        }
    }
    body {
        font-family: 'Georgia', 'Times New Roman', serif;
        font-size: 11pt;
        line-height: 1.6;
        color: #222;
    }
    h1 {
        font-size: 22pt;
        margin-bottom: 2pt;
        color: #333;
        border-bottom: 2px solid #ccc;
        padding-bottom: 6pt;
    }
    h2.location {
        font-size: 14pt;
        font-weight: normal;
        color: #555;
        margin-top: 0;
        margin-bottom: 18pt;
        border-left: none;
        padding-left: 0;
    }
    .tags {
        font-size: 9pt;
        color: #888;
        margin-bottom: 18pt;
    }
    .tags span {
        background: #f0f0f0;
        padding: 2pt 6pt;
        border-radius: 3pt;
        margin-right: 4pt;
    }
    h2 {
        font-size: 13pt;
        color: #555;
        margin-top: 18pt;
        margin-bottom: 6pt;
        border-left: 3px solid #4a90d9;
        padding-left: 8pt;
    }
    p { margin: 6pt 0; }
    ul, ol { margin: 6pt 0 6pt 18pt; }
    img {
        border-radius: 4pt;
        box-shadow: 0 1px 4px rgba(0,0,0,0.15);
    }
    img.landscape {
        max-width: 100%;
        max-height: 2.8in;
        display: block;
        margin: 6pt auto;
    }
    img.portrait {
        max-height: 2.8in;
        max-width: 45%;
        float: left;
        margin: 0 10pt 6pt 0;
    }
    .figure-landscape {
        margin: 8pt 0;
        text-align: center;
    }
    .figure-portrait {
        margin: 8pt 0;
        overflow: hidden;
    }
    .figure-portrait .caption {
        text-align: left;
        padding-top: 4pt;
    }
    .caption {
        font-style: italic;
        font-size: 9pt;
        color: #666;
        text-align: center;
        margin-top: 2pt;
        margin-bottom: 8pt;
    }
    .people {
        font-size: 9pt;
        color: #666;
        margin-bottom: 8pt;
    }
    /* Multi-day rendering: thin separator before each non-first day,
       slightly lighter day header than the top H1 title. */
    hr.day-sep {
        border: none;
        border-top: 1px solid #ccc;
        margin: 24pt 0 12pt;
    }
    h2.day-header {
        font-size: 16pt;
        font-weight: normal;
        color: #333;
        margin-top: 0;
        margin-bottom: 4pt;
        border-left: none;
        padding-left: 0;
        border-bottom: 1px solid #ddd;
        padding-bottom: 4pt;
    }
""")


# ---------------------------------------------------------------------------
# Date-spec parsing
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_dates(spec):
    """Parse a `--dates` spec into a sorted, deduped list of YYYY-MM-DD strings.

    Accepts a string with three forms, mixed freely via comma:
      - `2026-05-03`                            single day
      - `2026-05-01..2026-05-07`                inclusive range
      - `2026-05-01,2026-05-03,2026-05-05`      explicit list
      - `2026-05-01..2026-05-03,2026-05-07`     mix of ranges and singletons

    A reversed range (`end..start`) is silently swapped. Whitespace around
    tokens is tolerated. Empty/None spec raises ValueError.
    """
    if not spec or not spec.strip():
        raise ValueError("empty dates spec")

    out = set()
    for raw_token in spec.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if ".." in token:
            start_s, end_s = (s.strip() for s in token.split("..", 1))
            if not _DATE_RE.match(start_s) or not _DATE_RE.match(end_s):
                raise ValueError(f"bad range token: {token!r}")
            d1 = datetime.strptime(start_s, "%Y-%m-%d").date()
            d2 = datetime.strptime(end_s, "%Y-%m-%d").date()
            if d1 > d2:
                d1, d2 = d2, d1
            cur = d1
            while cur <= d2:
                out.add(cur.isoformat())
                cur += timedelta(days=1)
        else:
            if not _DATE_RE.match(token):
                raise ValueError(f"bad date token: {token!r}")
            datetime.strptime(token, "%Y-%m-%d")  # raises if invalid date
            out.add(token)
    return sorted(out)


def parse_frontmatter(text):
    """Extract YAML frontmatter and body."""
    match = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not match:
        return {}, text
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, match.group(2)


MAX_IMG_WIDTH = 1200  # px — good for print at ~4in wide
JPEG_QUALITY = 72     # good balance: visually clean, ~70-80% smaller than originals


def compress_photo(filepath):
    """Resize and compress a photo, return (data_uri, is_portrait)."""
    try:
        img = Image.open(filepath)
        is_portrait = img.height > img.width
        img.thumbnail((MAX_IMG_WIDTH, MAX_IMG_WIDTH), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}", is_portrait
    except Exception:
        return filepath.as_uri(), False


def journal_to_html(text, year_dir):
    """Convert journal markdown body to HTML, resolving photo paths."""
    # First pass: replace markdown images with HTML, tagging orientation
    def replace_img(m):
        alt = m.group(1)
        filename = m.group(2)
        path = year_dir / "photos" / filename
        if path.exists():
            uri, is_portrait = compress_photo(path)
            orient = "portrait" if is_portrait else "landscape"
            return f'<img src="{uri}" alt="{alt}" class="{orient}">'
        return ""

    text = re.sub(r'!\[([^\]]*)\]\(photos/([^)]+)\)', replace_img, text)

    # Convert italic descriptions after images into caption divs
    text = re.sub(
        r'^\*([^*]+)\*$',
        r'<div class="caption">\1</div>',
        text,
        flags=re.MULTILINE,
    )

    # Wrap portrait image + following caption into a side-by-side figure
    text = re.sub(
        r'(<img [^>]*class="portrait"[^>]*>)\s*\n?\s*(<div class="caption">.*?</div>)',
        r'<div class="figure-portrait">\1\2</div>',
        text,
        flags=re.DOTALL,
    )

    # Wrap landscape image + following caption into a stacked figure
    text = re.sub(
        r'(<img [^>]*class="landscape"[^>]*>)\s*\n?\s*(<div class="caption">.*?</div>)',
        r'<div class="figure-landscape">\1\2</div>',
        text,
        flags=re.DOTALL,
    )

    html = markdown.markdown(text, extensions=["extra", "sane_lists"])
    return html


def _is_contiguous(dates):
    """True if `dates` (sorted ISO strings) covers a contiguous day range."""
    if len(dates) <= 1:
        return True
    cur = datetime.strptime(dates[0], "%Y-%m-%d").date()
    end = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    expected = []
    while cur <= end:
        expected.append(cur.isoformat())
        cur += timedelta(days=1)
    return dates == expected


def _multi_day_title(dates):
    """Render a top-level title for a multi-day PDF."""
    first = datetime.strptime(dates[0], "%Y-%m-%d")
    last = datetime.strptime(dates[-1], "%Y-%m-%d")
    if _is_contiguous(dates):
        if first.year == last.year:
            return f"{first.strftime('%B %-d')} – {last.strftime('%B %-d, %Y')}"
        return f"{first.strftime('%B %-d, %Y')} – {last.strftime('%B %-d, %Y')}"
    # Non-contiguous list
    return f"{len(dates)} days, {first.strftime('%B %-d, %Y')} – {last.strftime('%B %-d, %Y')}"


def default_output_path(dates):
    """Default PDF path under `Journal/<year>/pdf/`. Year is the first day's year."""
    year = dates[0][:4]
    pdf_dir = JOURNAL_BASE / year / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    if len(dates) == 1:
        return pdf_dir / f"{dates[0]}.pdf"
    if _is_contiguous(dates):
        return pdf_dir / f"{dates[0]}_to_{dates[-1]}.pdf"
    return pdf_dir / f"{dates[0]}_plus_{len(dates) - 1}.pdf"


def _render_day_section(date_str, fm, body, year_dir, *, level, include_people, include_priorities):
    """Render one day's HTML fragment. `level` is 1 (single-day) or 2 (multi-day)."""
    parts = []
    day_name = fm.get("day", "")
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    day_title = date_obj.strftime("%B %-d, %Y")
    if day_name:
        day_title = f"{day_name}, {day_title}"

    if level == 1:
        parts.append(f"<h1>{day_title}</h1>")
    else:
        parts.append(f'<h2 class="day-header">{day_title}</h2>')

    # Location subtitle (per day)
    location = fm.get("location", "")
    if not location:
        photo_match = re.search(
            r'!\[[^\]]*\]\(photos/\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(.+?)\.\w+\)',
            body,
        )
        if photo_match:
            location = photo_match.group(1).replace("_", " ")
    if isinstance(location, list):
        location = ", ".join(str(loc) for loc in location)
    if location:
        parts.append(f'<h2 class="location">{location}</h2>')

    # People — per-day inline
    if include_people:
        people = fm.get("people", [])
        if isinstance(people, list) and people:
            names = [p["name"] if isinstance(p, dict) else str(p) for p in people]
            parts.append(f'<div class="people">With: {", ".join(names)}</div>')

    # Strip priorities unless requested
    if not include_priorities:
        body = re.sub(r'^## Priorities\n(?:- \[[ x]\] .+\n?)+', '', body, flags=re.MULTILINE)

    parts.append(journal_to_html(body, year_dir))
    return "".join(parts)


def build_pdf(dates, output_path, include_people=False, include_priorities=False):
    """Build a PDF covering one or more journal dates.

    `dates` is a list of YYYY-MM-DD strings (sorted ascending) — typically
    the result of `parse_dates(spec)`. A bare string is also accepted for
    backward compatibility with the single-day signature.

    Days that have no journal file are skipped with a stderr warning. If
    no dates resolve to a real file, exits non-zero.
    """
    if isinstance(dates, str):
        dates = [dates]
    if not dates:
        print("No dates given", file=sys.stderr)
        sys.exit(1)

    rendered = []
    for d in dates:
        year = d[:4]
        year_dir = JOURNAL_BASE / year
        path = year_dir / f"{d}.md"
        if path.exists():
            rendered.append((d, year_dir, path))
        else:
            print(f"warning: no journal file for {d}, skipping", file=sys.stderr)

    if not rendered:
        print("No journal entries found for the specified dates", file=sys.stderr)
        sys.exit(1)

    parts = []

    # Top title only when rendering >1 day. Single-day keeps the existing
    # H1 layout untouched (the per-day section emits its own H1).
    multi = len(rendered) > 1
    if multi:
        parts.append(f"<h1>{_multi_day_title([d for d, _, _ in rendered])}</h1>")

    for i, (date_str, year_dir, journal_path) in enumerate(rendered):
        if multi and i > 0:
            parts.append('<hr class="day-sep">')
        raw = journal_path.read_text()
        fm, body = parse_frontmatter(raw)
        parts.append(_render_day_section(
            date_str, fm, body, year_dir,
            level=2 if multi else 1,
            include_people=include_people,
            include_priorities=include_priorities,
        ))

    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body>{"".join(parts)}</body></html>"""

    doc = weasyprint.HTML(string=full_html)
    doc.write_pdf(str(output_path), stylesheets=[weasyprint.CSS(string=CSS)])

    print(output_path)


def main():
    parser = argparse.ArgumentParser(description="Convert one or more journal days to PDF")
    parser.add_argument(
        "--dates",
        help="Date spec: 'YYYY-MM-DD', 'A..B' (range), 'A,B,C' (list), or any "
             "comma-mix of those. Default: today.",
    )
    parser.add_argument("--date", help="Alias of --dates (single day, kept for back-compat)")
    parser.add_argument("--output", "-o", help="Output PDF path")
    parser.add_argument("--people", action="store_true", help="Include people list per day")
    parser.add_argument("--priorities", action="store_true", help="Include priorities section per day")
    args = parser.parse_args()

    spec = args.dates or args.date or datetime.now().strftime("%Y-%m-%d")
    try:
        dates = parse_dates(spec)
    except ValueError as e:
        print(f"Invalid date spec {spec!r}: {e}. Use YYYY-MM-DD, A..B, or A,B,C.", file=sys.stderr)
        sys.exit(1)

    output = args.output or default_output_path(dates)
    build_pdf(dates, output, include_people=args.people, include_priorities=args.priorities)


if __name__ == "__main__":
    main()
