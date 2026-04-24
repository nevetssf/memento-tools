#!/usr/bin/env python3
"""
pdf2md.py — Convert PDFs in the vault to sidecar Markdown files.

Each Foo.pdf gets a Foo.md alongside it with frontmatter recording the
source hash, so re-runs are idempotent and never overwrite user edits
unless --force is passed.

Default extraction: pymupdf4llm (fast, structure-preserving).
Fallback for image-only PDFs: qwen2-vl-7b via LM Studio (slower).

Usage:
    pdf2md.py                         # convert every PDF in the vault
    pdf2md.py path/to/file.pdf [...]  # convert specific files
    pdf2md.py --force                 # re-convert even if hash matches
    pdf2md.py --no-vision-fallback    # skip vision OCR on image PDFs
"""
import argparse
import base64
import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import VAULT_DIR, CHAT_MODEL_URL

VISION_MODEL_NAME = "qwen2-vl-7b-instruct"
TEXT_THRESHOLD_CHARS = 100   # PDFs with less text per page than this trigger fallback


# ---------------------------------------------------------------------------
# Hashing & frontmatter parsing
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
            m = re.match(r'^source_pdf_hash:\s*(.+)', line)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Extraction backends
# ---------------------------------------------------------------------------

def extract_pymupdf4llm(pdf_path: Path) -> tuple[str, dict]:
    """Structure-preserving extraction via pymupdf4llm."""
    import pymupdf4llm
    import fitz
    text = pymupdf4llm.to_markdown(str(pdf_path))
    doc = fitz.open(str(pdf_path))
    meta = {k: v for k, v in (doc.metadata or {}).items() if v}
    meta["page_count"] = doc.page_count
    doc.close()
    return text, meta


def extract_vision(pdf_path: Path) -> tuple[str, dict]:
    """Fallback for image-only PDFs: render each page to PNG, OCR via vision LLM."""
    import fitz
    doc = fitz.open(str(pdf_path))
    meta = {k: v for k, v in (doc.metadata or {}).items() if v}
    meta["page_count"] = doc.page_count

    pages = []
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=150)
        png_bytes = pix.tobytes("png")
        b64 = base64.b64encode(png_bytes).decode()

        body = {
            "model": VISION_MODEL_NAME,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text":
                        "Extract the full text content of this page in clean markdown. "
                        "Preserve paragraph breaks, headings, lists, and tables. "
                        "Do not add commentary or summarize."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                ]
            }],
            "max_tokens": 4000,
            "temperature": 0.0,
        }
        req = urllib.request.Request(
            f"{CHAT_MODEL_URL}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            page_text = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            page_text = f"<!-- vision OCR failed: {e} -->"

        pages.append(f"<!-- page: {i} -->\n\n{page_text}")
    doc.close()
    return "\n\n".join(pages), meta


def is_text_sparse(text: str, page_count: int) -> bool:
    """Heuristic: less than ~100 chars per page → probably image-based."""
    return len(text.strip()) < (max(page_count, 1) * TEXT_THRESHOLD_CHARS)


# ---------------------------------------------------------------------------
# MD assembly
# ---------------------------------------------------------------------------

def _yaml_safe(v):
    """Quote frontmatter values that contain YAML-significant characters."""
    s = str(v).replace("\n", " ").strip()
    if any(c in s for c in ":#'\"|>@`") or s.startswith(("- ", "[", "{")):
        return f'"{s.replace(chr(34), chr(92) + chr(34))}"'
    return s


def build_md(source_pdf: Path, source_pdf_hash: str, content: str,
             pdf_meta: dict, method: str) -> str:
    title = pdf_meta.get("title") or source_pdf.stem
    fm_lines = [
        "---",
        f"title: {_yaml_safe(title)}",
    ]
    if pdf_meta.get("author"):
        fm_lines.append(f"author: {_yaml_safe(pdf_meta['author'])}")
    fm_lines += [
        f"source_pdf: {_yaml_safe(source_pdf.name)}",
        f"source_pdf_hash: {source_pdf_hash}",
        f"converted_at: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"conversion_method: {method}",
        f"page_count: {pdf_meta.get('page_count', 0)}",
        "tags: [pdf-extract]",
        "---",
        "",
    ]
    return "\n".join(fm_lines) + content.rstrip() + "\n"


# ---------------------------------------------------------------------------
# Single-file conversion
# ---------------------------------------------------------------------------

def convert_pdf(pdf_path: Path, *, force: bool = False,
                vision_fallback: bool = True) -> dict:
    md_path = pdf_path.with_suffix(".md")
    current_hash = file_hash(pdf_path)
    prev_hash = stored_hash(md_path)

    # Skip if already up-to-date
    if prev_hash == current_hash and not force:
        return {"status": "unchanged", "path": str(pdf_path)}

    # PDF changed since last conversion — refuse without --force
    if prev_hash is not None and prev_hash != current_hash and not force:
        return {
            "status": "stale",
            "path": str(pdf_path),
            "md_path": str(md_path),
            "warning": "PDF hash differs from MD's recorded hash; pass --force to overwrite.",
        }

    # Extract via pymupdf4llm first
    try:
        text, pdf_meta = extract_pymupdf4llm(pdf_path)
        method = "pymupdf4llm"
    except Exception as e:
        return {"status": "error", "path": str(pdf_path),
                "error": f"pymupdf4llm: {type(e).__name__}: {e}"}

    # Image-PDF fallback
    if vision_fallback and is_text_sparse(text, pdf_meta.get("page_count", 1)):
        try:
            text2, meta2 = extract_vision(pdf_path)
            text = text2
            pdf_meta = {**pdf_meta, **meta2}
            method = "qwen2-vl-7b-vision"
        except Exception as e:
            method = f"pymupdf4llm-sparse (vision fallback failed: {e})"

    md_content = build_md(pdf_path, current_hash, text, pdf_meta, method)
    md_path.write_text(md_content)
    return {
        "status": "converted",
        "path": str(pdf_path),
        "md_path": str(md_path),
        "method": method,
        "pages": pdf_meta.get("page_count", 0),
        "chars": len(text),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("paths", nargs="*",
                        help="Specific PDFs to convert (default: every PDF in the vault)")
    parser.add_argument("--force", action="store_true",
                        help="Re-convert even if hash matches; overwrites existing MD")
    parser.add_argument("--no-vision-fallback", action="store_true",
                        help="Don't use vision OCR for image-based PDFs (extracts will be empty)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without writing any MD")
    args = parser.parse_args()

    if args.paths:
        pdfs = [Path(p).resolve() for p in args.paths
                if Path(p).suffix.lower() == ".pdf" and Path(p).exists()]
    else:
        pdfs = sorted(VAULT_DIR.rglob("*.pdf"))
        # Skip standard non-content directories
        pdfs = [p for p in pdfs
                if not any(s in p.parts for s in (".obsidian", ".trash", "Templates"))]

    if not pdfs:
        print("No PDFs found.")
        return

    # Progress bar
    iterator = pdfs
    if sys.stdout.isatty():
        try:
            from tqdm import tqdm
            iterator = tqdm(pdfs, unit="pdf", desc="Converting")
        except ImportError:
            pass

    summary = {}
    for pdf in iterator:
        try:
            if args.dry_run:
                md = pdf.with_suffix(".md")
                ph = stored_hash(md)
                ch = file_hash(pdf)
                if ph == ch:
                    r = {"status": "unchanged", "path": str(pdf)}
                elif ph is None:
                    r = {"status": "would-convert", "path": str(pdf)}
                else:
                    r = {"status": "would-update (--force needed)", "path": str(pdf)}
            else:
                r = convert_pdf(pdf, force=args.force,
                                vision_fallback=not args.no_vision_fallback)
            summary[r["status"]] = summary.get(r["status"], 0) + 1
            if not sys.stdout.isatty():
                print(json.dumps(r))
            elif r["status"] in ("stale", "error"):
                print(f"\n  {r['status']}: {pdf}: {r.get('warning') or r.get('error')}")
        except Exception as e:
            summary["error"] = summary.get("error", 0) + 1
            print(f"\n  error: {pdf}: {type(e).__name__}: {e}", file=sys.stderr)

    if sys.stdout.isatty():
        print()
        print("Summary:", summary)


if __name__ == "__main__":
    main()
