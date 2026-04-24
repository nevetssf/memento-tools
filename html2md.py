#!/usr/bin/env python3
"""
html2md.py — Convert HTML/HTM files in the vault to sidecar Markdown files.

Mirrors pdf2md.py: each Foo.html gets a Foo.md alongside it with frontmatter
recording the source hash, so re-runs are idempotent and never overwrite
user edits unless --force is passed.

Extraction: BeautifulSoup strips non-content elements (script/style/nav/etc.),
then markdownify converts what remains to clean Markdown.

Usage:
    html2md.py                          # convert every HTML in the vault
    html2md.py path/to/file.html [...]  # convert specific files
    html2md.py --force                  # re-convert even if hash matches
    html2md.py --dry-run                # preview without writing
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import VAULT_DIR


# ---------------------------------------------------------------------------
# Hashing & frontmatter parsing (same convention as pdf2md.py)
# ---------------------------------------------------------------------------

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def stored_hash(md_path: Path) -> str | None:
    if not md_path.exists():
        return None
    try:
        text = md_path.read_text(errors="replace")
        if not text.startswith("---"):
            return None
        end = text.find("\n---", 4)
        if end == -1:
            return None
        for line in text[4:end].splitlines():
            m = re.match(r'^source_html_hash:\s*(.+)', line)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

STRIP_TAGS = ("script", "style", "noscript", "iframe", "nav", "footer",
              "header", "aside", "form", "button", "svg")


def extract(html_text: str) -> tuple[str, dict]:
    """Returns (markdown_content, html_metadata)."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html_text, "html.parser")

    meta = {}
    if soup.title and soup.title.string:
        meta["title"] = soup.title.string.strip()
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        meta["description"] = desc["content"].strip()
    author = soup.find("meta", attrs={"name": "author"})
    if author and author.get("content"):
        meta["author"] = author["content"].strip()

    # Remove non-content elements
    for tag in soup(STRIP_TAGS):
        tag.decompose()

    # Drop comments
    from bs4 import Comment
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        c.extract()

    body = soup.body or soup
    md = markdownify(str(body), heading_style="ATX", strip=["a"]) if False else \
         markdownify(str(body), heading_style="ATX")

    # Collapse runs of blank lines
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md, meta


# ---------------------------------------------------------------------------
# MD assembly
# ---------------------------------------------------------------------------

def _yaml_safe(v):
    s = str(v).replace("\n", " ").strip()
    if any(c in s for c in ":#'\"|>@`") or s.startswith(("- ", "[", "{")):
        return f'"{s.replace(chr(34), chr(92) + chr(34))}"'
    return s


def build_md(source_html: Path, source_html_hash: str, content: str,
             html_meta: dict, method: str) -> str:
    title = html_meta.get("title") or source_html.stem
    fm_lines = ["---", f"title: {_yaml_safe(title)}"]
    if html_meta.get("author"):
        fm_lines.append(f"author: {_yaml_safe(html_meta['author'])}")
    if html_meta.get("description"):
        fm_lines.append(f"description: {_yaml_safe(html_meta['description'])}")
    fm_lines += [
        f"source_html: {_yaml_safe(source_html.name)}",
        f"source_html_hash: {source_html_hash}",
        f"converted_at: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"conversion_method: {method}",
        "tags: [html-extract]",
        "---",
        "",
    ]
    return "\n".join(fm_lines) + content.rstrip() + "\n"


# ---------------------------------------------------------------------------
# Single-file conversion
# ---------------------------------------------------------------------------

def convert_html(html_path: Path, *, force: bool = False) -> dict:
    md_path = html_path.with_suffix(".md")
    current_hash = file_hash(html_path)
    prev_hash = stored_hash(md_path)

    if prev_hash == current_hash and not force:
        return {"status": "unchanged", "path": str(html_path)}

    if prev_hash is not None and prev_hash != current_hash and not force:
        return {
            "status": "stale",
            "path": str(html_path),
            "md_path": str(md_path),
            "warning": "HTML hash differs from MD's recorded hash; pass --force to overwrite.",
        }

    try:
        html_text = html_path.read_text(errors="replace")
        text, meta = extract(html_text)
        method = "markdownify"
    except Exception as e:
        return {"status": "error", "path": str(html_path),
                "error": f"{type(e).__name__}: {e}"}

    md_content = build_md(html_path, current_hash, text, meta, method)
    md_path.write_text(md_content)
    return {
        "status": "converted",
        "path": str(html_path),
        "md_path": str(md_path),
        "method": method,
        "chars": len(text),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("paths", nargs="*",
                        help="Specific HTML files to convert (default: every HTML in the vault)")
    parser.add_argument("--force", action="store_true",
                        help="Re-convert even if hash matches; overwrites existing MD")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without writing any MD")
    args = parser.parse_args()

    if args.paths:
        files = [Path(p).resolve() for p in args.paths
                 if Path(p).suffix.lower() in (".html", ".htm") and Path(p).exists()]
    else:
        files = sorted(p for p in VAULT_DIR.rglob("*")
                       if p.is_file() and p.suffix.lower() in (".html", ".htm")
                       and not any(s in p.parts for s in (".obsidian", ".trash", "Templates")))

    if not files:
        print("No HTML files found.")
        return

    iterator = files
    if sys.stdout.isatty():
        try:
            from tqdm import tqdm
            iterator = tqdm(files, unit="html", desc="Converting")
        except ImportError:
            pass

    summary = {}
    for html in iterator:
        try:
            if args.dry_run:
                md = html.with_suffix(".md")
                ph = stored_hash(md)
                ch = file_hash(html)
                if ph == ch:
                    r = {"status": "unchanged", "path": str(html)}
                elif ph is None:
                    r = {"status": "would-convert", "path": str(html)}
                else:
                    r = {"status": "would-update (--force needed)", "path": str(html)}
            else:
                r = convert_html(html, force=args.force)
            summary[r["status"]] = summary.get(r["status"], 0) + 1
            if not sys.stdout.isatty():
                print(json.dumps(r))
            elif r["status"] in ("stale", "error"):
                print(f"\n  {r['status']}: {html}: {r.get('warning') or r.get('error')}")
        except Exception as e:
            summary["error"] = summary.get("error", 0) + 1
            print(f"\n  error: {html}: {type(e).__name__}: {e}", file=sys.stderr)

    if sys.stdout.isatty():
        print()
        print("Summary:", summary)


if __name__ == "__main__":
    main()
