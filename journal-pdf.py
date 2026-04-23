#!/usr/bin/env python3
"""
Convert a day's journal entry into a styled PDF.

Usage:
    journal-pdf.py --date 2026-03-28
    journal-pdf.py --date 2026-03-28 --output /tmp/journal-2026-03-28.pdf
    journal-pdf.py                          # today

Uses weasyprint from a dedicated venv.
"""

import argparse
import base64
import io
import os
import re
import sys
import textwrap
from datetime import datetime
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
""")


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


def build_pdf(date_str, output_path, include_people=False, include_priorities=False):
    """Build PDF for a given date."""
    year = date_str[:4]
    year_dir = JOURNAL_BASE / year
    journal_path = year_dir / f"{date_str}.md"

    if not journal_path.exists():
        print(f"No journal entry found for {date_str}", file=sys.stderr)
        sys.exit(1)

    raw = journal_path.read_text()
    fm, body = parse_frontmatter(raw)

    # Build header
    day_name = fm.get("day", "")
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    title = date_obj.strftime("%B %-d, %Y")
    if day_name:
        title = f"{day_name}, {title}"

    parts = [f"<h1>{title}</h1>"]

    # Location as a subtitle under the title
    location = fm.get("location", "")
    if not location:
        photo_match = re.search(r'!\[[^\]]*\]\(photos/\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(.+?)\.\w+\)', body)
        if photo_match:
            location = photo_match.group(1).replace("_", " ")
    if isinstance(location, list):
        location = ", ".join(str(loc) for loc in location)
    if location:
        parts.append(f'<h2 class="location">{location}</h2>')

    # People — only if flag is set
    if include_people:
        people = fm.get("people", [])
        if isinstance(people, list) and people:
            names = [p["name"] if isinstance(p, dict) else str(p) for p in people]
            parts.append(f'<div class="people">With: {", ".join(names)}</div>')

    # Strip priorities section from body unless requested
    if not include_priorities:
        body = re.sub(r'^## Priorities\n(?:- \[[ x]\] .+\n?)+', '', body, flags=re.MULTILINE)

    # Remove tags section (already shown via frontmatter styling, not needed in body)
    # Convert body
    body_html = journal_to_html(body, year_dir)
    parts.append(body_html)

    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body>{"".join(parts)}</body></html>"""

    doc = weasyprint.HTML(string=full_html)
    doc.write_pdf(str(output_path), stylesheets=[weasyprint.CSS(string=CSS)])

    print(output_path)


def main():
    parser = argparse.ArgumentParser(description="Convert a journal day to PDF")
    parser.add_argument("--date", help="Date as YYYY-MM-DD (default: today)")
    parser.add_argument("--output", "-o", help="Output PDF path")
    parser.add_argument("--people", action="store_true", help="Include people list")
    parser.add_argument("--priorities", action="store_true", help="Include priorities section")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"Invalid date format: {date_str}. Use YYYY-MM-DD.", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output = args.output
    else:
        year = date_str[:4]
        pdf_dir = JOURNAL_BASE / year / "pdf"
        pdf_dir.mkdir(exist_ok=True)
        output = str(pdf_dir / f"{date_str}.pdf")
    build_pdf(date_str, output, include_people=args.people, include_priorities=args.priorities)


if __name__ == "__main__":
    main()
