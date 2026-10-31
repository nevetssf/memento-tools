#!/usr/bin/env python3
"""notebook.py — paper notebooks (Traveler's Notebook) archived in the vault.

Each notebook is a vault folder `Notebooks/<started> <name>/` holding:

  <folder>.md         index note: frontmatter + one `## Page NNN` section per
                      page with the verbatim transcription (searchable twin
                      of the PDF; vault-embed indexes it)
  <folder>.pdf        every page in order, at the notebook's physical size
  page-000-cover.jpg  finished pages: exact page geometry, 300 dpi
  page-NNN.jpg
  page-999-back.jpg
  .originals/         one source image per photo/scan actually in use (a
                      spread photo is shared by both of its pages); hidden
                      from Obsidian
  .notebook.json      manifest: page size, and per page the original + the
                      processing parameters used, so `rebuild` can redo
                      every page when the image pipeline improves

Workflow (the agent drives it; the script does everything deterministic):

  ingest      photos / Scanner Pro PDFs  ->  staged candidate pages (one per
              page found: spreads split, sideways shots rotated, scans
              normalised) with a header crop for reading the page number
  file        assign candidates to page numbers -> pages written, originals
              kept, replaced originals pruned, PDF recompiled
  transcribe  write/replace a page's section in the index note
  reprocess   redo one page from its original with different parameters
              (rotation, manual corners, ...) -> a new candidate to file
  rebuild     redo every page from its originals with recorded parameters
  set-size    change the page size; re-renders every page from its original
  close       (after finish) irreversible close-out: PDF + index note move up to
              Notebooks/, the folder (pages, originals, manifest) is deleted
  status / finish / list / new / compile

All writes to a notebook are serialised with an flock on .notebook.lock.

CLI: notebook.py <command> [...]   (JSON on stdout; run with -h per command)
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from config import NOTEBOOKS_DIR, NOTEBOOK_STAGING_DIR  # noqa: E402
import journal_fm  # noqa: E402
import notebook_image as ni  # noqa: E402

MANIFEST = ".notebook.json"
ORIGINALS = ".originals"
LOCKFILE = ".notebook.lock"
STAGING_TTL_DAYS = 7

# name -> (pagesize string for img2pdf, width_in, height_in)
SIZES = {
    "passport": ("3.5inx5in", 3.5, 5.0),
    "regular": ("110mmx210mm", 110 / 25.4, 210 / 25.4),
    "field-notes": ("3.5inx5.5in", 3.5, 5.5),
}

COVER, BACK = "cover", "back"


class NotebookError(Exception):
    pass


# ---------------------------------------------------------------------------
# Page ids
# ---------------------------------------------------------------------------

def norm_page(page) -> str:
    """'cover' | 'back' | canonical integer string ('7', not '007')."""
    s = str(page).strip().lower()
    if s in ("cover", "front", "front cover", "0", "000"):
        return COVER
    if s in ("back", "back cover", "999"):
        return BACK
    if re.fullmatch(r"\d{1,3}", s):
        n = int(s)
        if 1 <= n <= 998:
            return str(n)
    raise NotebookError(f"bad page id {page!r}: use a number 1-998, 'cover' or 'back'")


def page_filename(pid: str) -> str:
    if pid == COVER:
        return "page-000-cover.jpg"
    if pid == BACK:
        return "page-999-back.jpg"
    return f"page-{int(pid):03d}.jpg"


def page_sort_key(pid: str) -> int:
    return -1 if pid == COVER else 10 ** 6 if pid == BACK else int(pid)


def page_label(pid: str) -> str:
    if pid == COVER:
        return "Cover"
    if pid == BACK:
        return "Back cover"
    return f"Page {int(pid):03d}"


_FILE_RE = re.compile(r"^page-(\d{3})(?:-(cover|back))?\.jpg$")


def pages_on_disk(nb: Path) -> dict[str, Path]:
    out = {}
    for f in nb.glob("page-*.jpg"):
        m = _FILE_RE.match(f.name)
        if not m:
            continue
        if m.group(2) == "cover":
            out[COVER] = f
        elif m.group(2) == "back":
            out[BACK] = f
        elif 1 <= int(m.group(1)) <= 998:
            out[str(int(m.group(1)))] = f
    return out


# ---------------------------------------------------------------------------
# Locations, manifest, locking
# ---------------------------------------------------------------------------

def index_note(nb: Path) -> Path:
    return nb / f"{nb.name}.md"


def pdf_path(nb: Path) -> Path:
    return nb / f"{nb.name}.pdf"


@contextlib.contextmanager
def locked(nb: Path):
    with open(nb / LOCKFILE, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if mode is None and path.exists():
            mode = path.stat().st_mode & 0o777
        os.chmod(tmp, mode if mode is not None else 0o664)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _parse_size(text: str) -> tuple[str, float, float]:
    """Size from a frontmatter value like 'passport (3.5x5in)', 'regular',
    '110x210mm' or '3.5x5in'. Returns (pagesize, width_in, height_in)."""
    t = (text or "").lower()
    for name, spec in SIZES.items():
        if name in t:
            return spec
    m = re.search(r"([\d.]+)\s*x\s*([\d.]+)\s*(mm|in)", t)
    if m:
        w, h, unit = float(m.group(1)), float(m.group(2)), m.group(3)
        div = 25.4 if unit == "mm" else 1.0
        return f"{m.group(1)}{unit}x{m.group(2)}{unit}", w / div, h / div
    raise NotebookError(f"can't tell the page size from {text!r}")


def _size_record(size: str) -> dict:
    pagesize, w_in, h_in = _parse_size(size)
    return {"label": size, "pagesize": pagesize,
            "px": [round(w_in * ni.DPI), round(h_in * ni.DPI)]}


def load_manifest(nb: Path) -> dict:
    """Manifest, created from the index note's frontmatter if missing."""
    mf = nb / MANIFEST
    if mf.exists():
        return json.loads(mf.read_text())
    fm, _ = journal_fm.split_frontmatter(index_note(nb).read_text())
    size = journal_fm.get_field(fm, "size")
    if not size:
        raise NotebookError(f"{nb.name}: no 'size' in the index note frontmatter")
    return {"version": 1, "size": _size_record(size), "pages": {}, "originals": {}}


def save_manifest(nb: Path, man: dict) -> None:
    _atomic_write(nb / MANIFEST, (json.dumps(man, indent=2, sort_keys=True) + "\n").encode())


# ---------------------------------------------------------------------------
# Index note (targeted patches only — never rewrite the whole note)
# ---------------------------------------------------------------------------

def _patch_frontmatter(text: str, fields: dict) -> str:
    if not text.startswith("---\n"):
        raise NotebookError("index note has no frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise NotebookError("index note frontmatter is not closed")
    fm = text[4:end]
    for k, v in fields.items():
        fm = journal_fm.set_field(fm, k, str(v)).rstrip("\n")
    return "---\n" + fm + text[end:]


def _section_re(pid: str) -> re.Pattern:
    if pid == COVER:
        return re.compile(r"^## Cover\b.*$", re.M)
    if pid == BACK:
        return re.compile(r"^## Back cover\b.*$", re.M)
    return re.compile(rf"^## Page 0*{int(pid)}(?!\d).*$", re.M)


_ANY_PAGE_RE = re.compile(r"^## (Cover|Back cover|Page (\d+))\b.*$", re.M)


def _sections(text: str) -> list[tuple[str, int, int]]:
    """(pid, start, end) of each page section; end = next '## ' or EOF."""
    heads = [(m.start(), m) for m in _ANY_PAGE_RE.finditer(text)]
    nxt = [m.start() for m in re.finditer(r"^## ", text, re.M)]
    out = []
    for start, m in heads:
        pid = COVER if m.group(1) == "Cover" else BACK if m.group(1) == "Back cover" \
            else str(int(m.group(2)))
        later = [n for n in nxt if n > start]
        out.append((pid, start, later[0] if later else len(text)))
    return out


def set_section(text: str, pid: str, body: str, heading: str) -> str:
    """Insert or replace one page section, keeping page order."""
    block = f"{heading}\n\n{body.strip()}\n\n"
    for spid, start, end in _sections(text):
        if spid == pid:
            return text[:start] + block + text[end:]
    key = page_sort_key(pid)
    for spid, start, _ in _sections(text):
        if page_sort_key(spid) > key:
            return text[:start] + block + text[start:]
    if not text.endswith("\n"):
        text += "\n"
    if not text.endswith("\n\n"):
        text += "\n"
    return text + block


def transcribed_pages(text: str) -> set[str]:
    return {pid for pid, _, _ in _sections(text)}


def _today() -> str:
    try:
        return journal_fm.get_local_date()
    except Exception:
        return date.today().isoformat()


# ---------------------------------------------------------------------------
# Notebooks
# ---------------------------------------------------------------------------

def _fm_of(nb: Path) -> str:
    fm, _ = journal_fm.split_frontmatter(index_note(nb).read_text())
    return fm


def all_notebooks(root: Path | None = None) -> list[Path]:
    root = root or NOTEBOOKS_DIR
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and index_note(d).exists())


def find_notebook(name: str | None = None, root: Path | None = None) -> Path:
    """Folder name (exact or case-insensitive substring), a path, or None for
    the only active notebook."""
    if name and Path(name).is_dir() and index_note(Path(name)).exists():
        return Path(name)
    books = all_notebooks(root)
    if name:
        exact = [b for b in books if b.name == name]
        hits = exact or [b for b in books if name.lower() in b.name.lower()]
        if len(hits) == 1:
            return hits[0]
        raise NotebookError(f"{'no' if not hits else 'several'} notebooks match {name!r}: "
                            f"{[b.name for b in (hits or books)]}")
    active = [b for b in books if (journal_fm.get_field(_fm_of(b), "status") or "") == "active"]
    if len(active) == 1:
        return active[0]
    raise NotebookError("say which notebook: " +
                        (f"active ones are {[b.name for b in active]}" if active
                         else "none is active"))


def list_notebooks(root: Path | None = None) -> list[dict]:
    root = root or NOTEBOOKS_DIR
    out = []
    for b in all_notebooks(root):
        fm = _fm_of(b)
        out.append({"name": b.name, "path": str(b),
                    "status": journal_fm.get_field(fm, "status"),
                    "size": journal_fm.get_field(fm, "size"),
                    "pages": len([p for p in pages_on_disk(b) if p not in (COVER, BACK)])})
    # closed-out notebooks: an index note + PDF directly in the root
    if root.is_dir():
        for note in sorted(root.glob("*.md")):
            fm, _ = journal_fm.split_frontmatter(note.read_text())
            if (journal_fm.get_field(fm, "status") or "") == "closed":
                pdf = note.with_suffix(".pdf")
                out.append({"name": note.stem, "path": str(pdf if pdf.exists() else note),
                            "status": "closed", "size": journal_fm.get_field(fm, "size"),
                            "pages": int(journal_fm.get_field(fm, "pages") or 0)})
    return out


def new_notebook(name: str, size: str, started: str | None = None,
                 description: str | None = None, root: Path | None = None) -> dict:
    root = root or NOTEBOOKS_DIR
    started = started or _today()
    datetime.strptime(started, "%Y-%m-%d")
    rec = _size_record(size)
    nb = root / f"{started} {name}"
    if nb.exists():
        raise NotebookError(f"{nb} already exists")
    nb.mkdir(parents=True)
    w, h = rec["px"]
    label = size if "(" in size else f"{size} ({rec['pagesize']})"
    note = (f"---\nstarted: {started}\nsize: {label}\npage_px: {w}x{h}\n"
            f"status: active\npages: 0\n---\n\n# {nb.name}\n\n"
            f"{(description or '').strip()}\n").replace("\n\n\n", "\n\n")
    index_note(nb).write_text(note)
    rec["label"] = label
    save_manifest(nb, {"version": 1, "size": rec, "pages": {}, "originals": {}})
    return {"notebook": nb.name, "path": str(nb), "size": label, "page_px": [w, h]}


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def compile_pdf(nb: Path, man: dict | None = None) -> dict:
    man = man or load_manifest(nb)
    pages = pages_on_disk(nb)
    order = sorted(pages, key=page_sort_key)
    if not order:
        return {"pdf": None, "pages": 0}
    out = pdf_path(nb)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "out.pdf"
        subprocess.run(["img2pdf", "--pagesize", man["size"]["pagesize"],
                        *[str(pages[p]) for p in order], "-o", str(tmp)],
                       check=True, capture_output=True)
        _atomic_write(out, tmp.read_bytes())
    return {"pdf": str(out), "pages": len(order)}


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _staging_root() -> Path:
    NOTEBOOK_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    return NOTEBOOK_STAGING_DIR


def _prune_staging():
    cutoff = time.time() - STAGING_TTL_DAYS * 86400
    for d in _staging_root().iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def _load_batch(batch_id: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"[\w-]+", batch_id or ""):
        raise NotebookError(f"bad batch id {batch_id!r}")
    d = _staging_root() / batch_id
    if not (d / "batch.json").exists():
        raise NotebookError(f"no staged batch {batch_id!r} (batches expire after "
                            f"{STAGING_TTL_DAYS} days)")
    return d, json.loads((d / "batch.json").read_text())


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _split_pdf(pdf: Path, out_dir: Path) -> list[Path]:
    """Embedded page scans, losslessly (pdfimages -j); pdftoppm if the PDF
    has no JPEG images."""
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdfimages", "-j", str(pdf), str(out_dir / "p")],
                   check=True, capture_output=True)
    imgs = sorted(out_dir.glob("p-*.jpg"))
    if imgs:
        for f in out_dir.glob("p-*"):
            if f.suffix != ".jpg":
                f.unlink()
        return imgs
    for f in out_dir.glob("p-*"):
        f.unlink()
    subprocess.run(["pdftoppm", "-r", "300", "-jpeg", str(pdf), str(out_dir / "r")],
                   check=True, capture_output=True)
    return sorted(out_dir.glob("r-*.jpg"))


def _render_candidate(bdir: Path, cid: str, original: Path, params: ni.PageParams,
                      px: tuple[int, int]) -> dict:
    img = ni.load_image(original)
    page, info = ni.process_page(img, params, *px)
    image = bdir / f"{cid}.jpg"
    header = bdir / f"{cid}-header.jpg"
    ni.save_image(page, image)
    ni.save_image(ni.header_strip(page), header)
    return {"id": cid, "image": str(image), "header": str(header),
            "params": params.to_dict(), "flags": info["flags"],
            "notes": {k: info[k] for k in ("mode", "spine", "crease_trim", "dewarp")
                      if k in info}}


def ingest(nb: Path, sources: list[str], rotate: int = 0, layout: str = "auto",
           cover: bool = False) -> dict:
    """Stage every page found in the given photos/PDFs as a candidate."""
    if layout not in ("auto", "single", "spread"):
        raise NotebookError("layout must be auto, single or spread")
    man = load_manifest(nb)
    px = tuple(man["size"]["px"])
    aspect = px[0] / px[1]
    _prune_staging()
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex()
    bdir = _staging_root() / batch_id
    (bdir / "originals").mkdir(parents=True)
    cands, problems = [], []
    n = 0

    def add(original: Path, params: ni.PageParams, source: str, layout_note: str):
        nonlocal n
        n += 1
        cid = f"c{n}"
        try:
            c = _render_candidate(bdir, cid, original, params, px)
        except ValueError as e:
            problems.append({"source": source, "error": str(e)})
            return
        c.update({"source": source, "original": original.name, "layout": layout_note})
        cands.append(c)

    for src in sources:
        sp = Path(src).expanduser()
        if not sp.exists():
            problems.append({"source": src, "error": "file not found"})
            continue
        if sp.suffix.lower() == ".pdf":
            scans = _split_pdf(sp, bdir / "pdf" / sp.stem)
            for k, scan in enumerate(scans, 1):
                staged = bdir / "originals" / f"{_sha(scan)[:16]}.jpg"
                shutil.copyfile(scan, staged)
                img = ni.rotate(ni.load_image(staged), rotate)
                h, w = img.shape[:2]
                is_spread = layout == "spread" or (
                    layout == "auto" and abs((w / h) / (2 * aspect) - 1) < 0.2)
                halves = ["left", "right"] if is_spread else [None]
                # a scan far from page-shaped is a partial capture (e.g. just
                # a cover label): pad it rather than stretch it
                odd = not is_spread and abs(math.log((w / h) / aspect)) > 0.3
                for half in halves:
                    params = ni.PageParams(rotate=rotate, full_frame=True, half=half,
                                           color=cover, keep_aspect=cover or odd,
                                           bleed=0 if cover else 1.0)
                    note = ("scan, two pages side by side" if is_spread else
                            "scan not page-shaped; padded, not stretched" if odd else
                            "scan of one page")
                    add(staged, params, f"{sp.name} page {k}", note)
                    if odd and cands and cands[-1]["original"] == staged.name:
                        cands[-1]["flags"].append(
                            "scan isn't page-shaped (partial capture?); padded with white")
            continue
        staged = bdir / "originals" / f"{_sha(sp)[:16]}{sp.suffix.lower() or '.jpg'}"
        shutil.copyfile(sp, staged)
        img = ni.load_image(staged)
        lay = ni.classify_layout(ni.rotate(img, rotate), aspect)
        if layout == "single":
            halves = lay.halves if (lay.kind == "single" and lay.halves) else [None]
        elif layout == "spread":
            halves = ["left", "right"]
        else:
            halves = lay.halves
        if not halves:
            if cover:
                # a cover often isn't bright paper: keep the whole frame
                add(staged, ni.PageParams(rotate=rotate, full_frame=True, keep_aspect=True,
                                          color=True, bleed=0), sp.name, "cover, whole frame")
            else:
                problems.append({"source": sp.name, "error": f"no page found ({lay.reason})"})
            continue
        for half in halves:
            params = ni.PageParams(rotate=(rotate + lay.rotate) % 360, half=half,
                                   two_blob=lay.two_blob and layout != "single",
                                   color=cover, bleed=0 if cover else 1.0)
            add(staged, params, sp.name, f"{lay.kind}: {lay.reason}")

    batch = {"id": batch_id, "notebook": str(nb), "created": datetime.now().isoformat(timespec="seconds"),
             "candidates": cands, "problems": problems}
    (bdir / "batch.json").write_text(json.dumps(batch, indent=2))
    return {**batch, "review": "View every candidate image. Read the page number "
            "(the header image is the top of the page, enlarged), check the crop "
            "(page corners intact, nothing clipped, no neighbouring page), then "
            "file them. Use flip for a page that is upside down."}


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------

def _gc_originals(nb: Path, man: dict) -> list[str]:
    used = {rec.get("original") for rec in man["pages"].values()}
    removed = []
    odir = nb / ORIGINALS
    for name in list(man["originals"]):
        if name not in used:
            man["originals"].pop(name)
            with contextlib.suppress(FileNotFoundError):
                (odir / name).unlink()
            removed.append(name)
    if odir.is_dir():
        for f in odir.iterdir():
            if f.name not in man["originals"]:
                f.unlink()
                removed.append(f.name)
    return removed


def _update_counts(nb: Path) -> None:
    pages = [p for p in pages_on_disk(nb) if p not in (COVER, BACK)]
    note = index_note(nb)
    note.write_text(_patch_frontmatter(note.read_text(), {"pages": len(pages)}))


def file_pages(batch_id: str, assignments: list[dict], received: str | None = None) -> dict:
    """assignments: [{"candidate": "c1", "page": 12, "flip": false}, ...]"""
    bdir, batch = _load_batch(batch_id)
    nb = Path(batch["notebook"])
    cands = {c["id"]: c for c in batch["candidates"]}
    received = received or _today()
    seen = set()
    for a in assignments:
        if a.get("candidate") not in cands:
            raise NotebookError(f"no candidate {a.get('candidate')!r} in batch {batch_id}")
        pid = norm_page(a.get("page"))
        if pid in seen:
            raise NotebookError(f"page {pid} assigned twice")
        seen.add(pid)
    filed, replaced = [], []
    with locked(nb):
        man = load_manifest(nb)
        px = tuple(man["size"]["px"])
        odir = nb / ORIGINALS
        odir.mkdir(exist_ok=True)
        existing = pages_on_disk(nb)
        for a in assignments:
            c = cands[a["candidate"]]
            pid = norm_page(a["page"])
            params = ni.PageParams.from_dict(c["params"])
            src_img = Path(c["image"])
            # staged before a size change? render again at the current size
            with Image.open(src_img) as im:
                rerender = tuple(im.size) != px
            if a.get("flip"):
                params.flip = not params.flip
                rerender = True
            if pid in (COVER, BACK) and not params.color:
                params.color, params.bleed = True, 0.0     # covers stay in colour
                rerender = True
            if rerender:
                c = {**c, **_render_candidate(bdir, c["id"], bdir / "originals" / c["original"],
                                              params, px)}
                src_img = Path(c["image"])
            orig_name = c["original"]
            if not (odir / orig_name).exists():
                shutil.copyfile(bdir / "originals" / orig_name, odir / orig_name)
            man["originals"].setdefault(orig_name, {"received": received, "source": c["source"]})
            dest = nb / page_filename(pid)
            if pid in existing:
                replaced.append(pid)
            _atomic_write(dest, src_img.read_bytes())
            man["pages"][pid] = {"original": orig_name, "params": params.to_dict(),
                                 "received": received, "source": c["source"]}
            filed.append({"page": pid, "file": dest.name, "flags": c["flags"]})
        removed = _gc_originals(nb, man)
        save_manifest(nb, man)
        pdf = compile_pdf(nb, man)
        _update_counts(nb)
    st = status(nb)
    return {"notebook": nb.name, "filed": filed, "replaced": replaced,
            "originals_removed": removed, "pdf": pdf, "status": st}


def transcribe(nb: Path, page, text: str, heading_note: str | None = None,
               received: str | None = None) -> dict:
    """Write or replace a page's transcription section in the index note."""
    pid = norm_page(page)
    if not text or not text.strip():
        raise NotebookError("empty transcription")
    with locked(nb):
        note = index_note(nb)
        cur = note.read_text()
        m = _section_re(pid).search(cur)
        if heading_note:
            heading = f"## {page_label(pid)} ({heading_note})"
        elif m:
            heading = m.group(0)
        else:
            man = load_manifest(nb)
            got = man["pages"].get(pid, {}).get("received") or received or _today()
            heading = f"## {page_label(pid)} (received {got})"
        note.write_text(set_section(cur, pid, text, heading))
    return {"notebook": nb.name, "page": pid, "heading": heading, "replaced": bool(m)}


def reprocess(nb: Path, page, **overrides) -> dict:
    """Re-run one filed page from its original with changed parameters and
    stage the result as a candidate (file it to replace the page)."""
    pid = norm_page(page)
    man = load_manifest(nb)
    rec = man["pages"].get(pid)
    if not rec:
        raise NotebookError(f"page {pid} has no recorded original; retake the photo and ingest it")
    orig = nb / ORIGINALS / rec["original"]
    params = ni.PageParams.from_dict({**rec["params"],
                                      **{k: v for k, v in overrides.items() if v is not None}})
    if overrides.get("corners"):
        params.half, params.two_blob, params.seed, params.full_frame = None, False, None, False
    _prune_staging()
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex()
    bdir = _staging_root() / batch_id
    (bdir / "originals").mkdir(parents=True)
    shutil.copyfile(orig, bdir / "originals" / orig.name)
    try:
        c = _render_candidate(bdir, "c1", bdir / "originals" / orig.name, params,
                              tuple(man["size"]["px"]))
    except ValueError as e:
        raise NotebookError(str(e)) from e
    c.update({"source": rec.get("source", ""), "original": orig.name,
              "layout": f"reprocess of {page_label(pid)}"})
    batch = {"id": batch_id, "notebook": str(nb), "created": datetime.now().isoformat(timespec="seconds"),
             "candidates": [c], "problems": [], "for_page": pid}
    (bdir / "batch.json").write_text(json.dumps(batch, indent=2))
    return {**batch, "original_image": str(orig),
            "review": f"View the candidate; if it's right, file c1 as page {pid}."}


def _rebuild_locked(nb: Path, man: dict, want: set | None) -> dict:
    """Rebuild body; caller holds the notebook lock."""
    done, skipped, flagged = [], [], {}
    px = tuple(man["size"]["px"])
    on_disk = pages_on_disk(nb)
    for pid in sorted(set(on_disk) | set(man["pages"]), key=page_sort_key):
        if want is not None and pid not in want:
            continue
        rec = man["pages"].get(pid)
        if not rec or not (nb / ORIGINALS / rec["original"]).exists():
            skipped.append(pid)
            continue
        img = ni.load_image(nb / ORIGINALS / rec["original"])
        try:
            page, info = ni.process_page(img, ni.PageParams.from_dict(rec["params"]), *px)
        except ValueError as e:
            flagged[pid] = [str(e)]
            continue
        with tempfile.NamedTemporaryFile(prefix=".rebuild-", suffix=".jpg", dir=nb, delete=False) as t:
            tmp = Path(t.name)
        try:
            ni.save_image(page, tmp)
            _atomic_write(nb / page_filename(pid), tmp.read_bytes())
        finally:
            tmp.unlink(missing_ok=True)
        done.append(pid)
        if info["flags"]:
            flagged[pid] = info["flags"]
    pdf = compile_pdf(nb, man)
    return {"notebook": nb.name, "rebuilt": done, "no_original": skipped,
            "flags": flagged, "pdf": pdf}


def rebuild(nb: Path, pages: list | None = None) -> dict:
    """Redo pages from their originals with their recorded parameters."""
    want = {norm_page(p) for p in pages} if pages else None
    with locked(nb):
        return _rebuild_locked(nb, load_manifest(nb), want)


def set_size(nb: Path, size: str) -> dict:
    """Change the notebook's physical page size: update the manifest and the
    index note's size/page_px, re-render every page from its original at the
    new geometry, recompile the PDF. Refuses if any page has no stored
    original (it could not be re-rendered and would keep the old size)."""
    rec = _size_record(size)
    w, h = rec["px"]
    label = size if "(" in size else f"{size} ({rec['pagesize']})"
    rec["label"] = label
    with locked(nb):
        man = load_manifest(nb)
        missing = sorted((p for p in pages_on_disk(nb)
                          if p not in man["pages"]
                          or not (nb / ORIGINALS / man["pages"][p]["original"]).exists()),
                         key=page_sort_key)
        if missing:
            raise NotebookError(f"pages without a stored original can't be resized: {missing}; "
                                "retake and file them first")
        old = man["size"]
        man["size"] = rec
        save_manifest(nb, man)
        note = index_note(nb)
        note.write_text(_patch_frontmatter(note.read_text(),
                                           {"size": label, "page_px": f"{w}x{h}"}))
        result = _rebuild_locked(nb, man, None)
    return {**result, "old_size": old.get("label"), "new_size": label, "page_px": [w, h]}


def status(nb: Path) -> dict:
    man = load_manifest(nb)
    fm = _fm_of(nb)
    on_disk = pages_on_disk(nb)
    nums = sorted(int(p) for p in on_disk if p not in (COVER, BACK))
    gaps = []
    if nums:
        have = set(nums)
        missing = [n for n in range(1, nums[-1] + 1) if n not in have]
        # compress into ranges
        for n in missing:
            if gaps and gaps[-1][1] == n - 1:
                gaps[-1][1] = n
            else:
                gaps.append([n, n])
    text = index_note(nb).read_text()
    written = transcribed_pages(text)
    return {
        "notebook": nb.name,
        "status": journal_fm.get_field(fm, "status"),
        "size": man["size"]["label"],
        "page_px": man["size"]["px"],
        "cover": COVER in on_disk,
        "back": BACK in on_disk,
        "pages": nums,
        "highest_page": nums[-1] if nums else None,
        "gaps": [f"{a}" if a == b else f"{a}-{b}" for a, b in gaps],
        "untranscribed": sorted((p for p in on_disk if p not in written), key=page_sort_key),
        "no_original": sorted((p for p in on_disk if p not in man["pages"]), key=page_sort_key),
        "originals": len(man["originals"]),
        "pdf": str(pdf_path(nb)) if pdf_path(nb).exists() else None,
    }


def finish(nb: Path, finished: str | None = None) -> dict:
    finished = finished or _today()
    with locked(nb):
        man = load_manifest(nb)
        pdf = compile_pdf(nb, man)
        note = index_note(nb)
        note.write_text(_patch_frontmatter(note.read_text(),
                                           {"status": "finished", "finished": finished}))
        _update_counts(nb)
    return {"notebook": nb.name, "finished": finished, "pdf": pdf, "status": status(nb)}


def _expected_entry(name: str, nb: Path) -> bool:
    """Files close() may delete: everything this tool creates in a notebook."""
    return (name in (MANIFEST, ORIGINALS, LOCKFILE, index_note(nb).name, pdf_path(nb).name)
            or bool(_FILE_RE.match(name))
            or name.startswith(".rebuild-")
            or name.startswith(f".{pdf_path(nb).name}.")      # _atomic_write temp
            or name.startswith(f".{index_note(nb).name}."))


def close(nb: Path, confirm: bool = False, closed: str | None = None) -> dict:
    """Close out a FINISHED notebook — irreversible: final PDF compile, PDF and
    index note move up to the notebooks root (note links the PDF, status:
    closed), and the folder with its page images, originals and manifest is
    deleted. Pages can no longer be fixed or rebuilt afterwards."""
    if not confirm:
        raise NotebookError("closing deletes the page images and originals for good; "
                            "pass confirm to proceed")
    root = nb.parent
    fm = _fm_of(nb)
    if (journal_fm.get_field(fm, "status") or "") != "finished":
        raise NotebookError(f"{nb.name} isn't finished; finish it first (closing is a separate, later step)")
    unexpected = sorted(e.name for e in nb.iterdir() if not _expected_entry(e.name, nb))
    if unexpected:
        raise NotebookError(f"{nb.name} holds files this tool didn't create: {unexpected}; "
                            "move them out first")
    dest_pdf, dest_note = root / pdf_path(nb).name, root / index_note(nb).name
    clash = [str(p.name) for p in (dest_pdf, dest_note) if p.exists()]
    if clash:
        raise NotebookError(f"already in {root.name}/: {clash}")
    closed = closed or _today()
    with locked(nb):
        man = load_manifest(nb)
        pdf = compile_pdf(nb, man)
        if not pdf["pdf"]:
            raise NotebookError("no pages to compile; nothing to close out")
        pages = status(nb)
        note = index_note(nb)
        text = _patch_frontmatter(note.read_text(), {"status": "closed", "closed": closed})
        link = f"PDF: [[{dest_pdf.name}]]"
        if link not in text:
            text, n = re.subn(r"^(# .*)$", lambda m: f"{m.group(1)}\n\n{link}", text, count=1,
                              flags=re.M)
            if not n:
                text = text.rstrip("\n") + f"\n\n{link}\n"
        note.write_text(text)
        os.replace(pdf_path(nb), dest_pdf)
        os.replace(note, dest_note)
    shutil.rmtree(nb)
    return {"notebook": nb.name, "closed": closed, "pdf": str(dest_pdf), "note": str(dest_note),
            "pages": pages["pages"], "cover": pages["cover"], "back": pages["back"],
            "deleted": str(nb)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Paper notebook archive in the vault")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")
    p = sub.add_parser("new")
    p.add_argument("name")
    p.add_argument("--size", required=True, help="passport | regular | WxHmm | WxHin")
    p.add_argument("--started")
    p.add_argument("--description")

    p = sub.add_parser("ingest")
    p.add_argument("sources", nargs="+")
    p.add_argument("--notebook")
    p.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    p.add_argument("--layout", default="auto", choices=["auto", "single", "spread"])
    p.add_argument("--cover", action="store_true")

    p = sub.add_parser("file")
    p.add_argument("batch")
    p.add_argument("assign", nargs="+", help="CANDIDATE=PAGE, e.g. c1=12 c2=13 c3=cover; "
                                             "append :flip to rotate 180 (c2=13:flip)")
    p.add_argument("--received")

    p = sub.add_parser("transcribe")
    p.add_argument("page")
    p.add_argument("--notebook")
    p.add_argument("--text-file", required=True, help="file with the transcription ('-' = stdin)")
    p.add_argument("--heading-note")

    p = sub.add_parser("reprocess")
    p.add_argument("page")
    p.add_argument("--notebook")
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270])
    p.add_argument("--half", choices=["left", "right"])
    p.add_argument("--corners", nargs=4, metavar="X,Y")
    p.add_argument("--seed")
    p.add_argument("--flip", action="store_true", default=None)
    p.add_argument("--bleed", type=float)
    p.add_argument("--no-dewarp", action="store_true")
    p.add_argument("--color", action="store_true", default=None)

    p = sub.add_parser("set-size")
    p.add_argument("size", help="passport | regular | field-notes | WxHmm | WxHin")
    p.add_argument("--notebook")

    p = sub.add_parser("close", help="close out a finished notebook (irreversible)")
    p.add_argument("--notebook", required=True)
    p.add_argument("--confirm", action="store_true")

    for name in ("status", "compile", "finish"):
        p = sub.add_parser(name)
        p.add_argument("--notebook")
    p = sub.add_parser("rebuild")
    p.add_argument("--notebook")
    p.add_argument("--pages", nargs="*")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            _out(list_notebooks())
        elif a.cmd == "new":
            _out(new_notebook(a.name, a.size, a.started, a.description))
        elif a.cmd == "ingest":
            _out(ingest(find_notebook(a.notebook), a.sources, a.rotate, a.layout, a.cover))
        elif a.cmd == "file":
            assigns = []
            for s in a.assign:
                cand, _, rest = s.partition("=")
                page, _, opt = rest.partition(":")
                assigns.append({"candidate": cand, "page": page, "flip": opt == "flip"})
            _out(file_pages(a.batch, assigns, a.received))
        elif a.cmd == "transcribe":
            text = sys.stdin.read() if a.text_file == "-" else Path(a.text_file).read_text()
            _out(transcribe(find_notebook(a.notebook), a.page, text, a.heading_note))
        elif a.cmd == "reprocess":
            ov = {"rotate": a.rotate, "half": a.half, "flip": a.flip, "bleed": a.bleed,
                  "color": a.color,
                  "seed": [int(v) for v in a.seed.split(",")] if a.seed else None,
                  "corners": [[int(v) for v in c.split(",")] for c in a.corners] if a.corners else None}
            if a.no_dewarp:
                ov["dewarp"] = False
            _out(reprocess(find_notebook(a.notebook), a.page, **ov))
        elif a.cmd == "close":
            _out(close(find_notebook(a.notebook), a.confirm))
        elif a.cmd == "set-size":
            _out(set_size(find_notebook(a.notebook), a.size))
        elif a.cmd == "status":
            _out(status(find_notebook(a.notebook)))
        elif a.cmd == "compile":
            nb = find_notebook(a.notebook)
            with locked(nb):
                _out(compile_pdf(nb))
        elif a.cmd == "rebuild":
            _out(rebuild(find_notebook(a.notebook), a.pages))
        elif a.cmd == "finish":
            _out(finish(find_notebook(a.notebook)))
    except NotebookError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
