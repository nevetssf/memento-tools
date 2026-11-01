"""Tests for notebook_image.py (page detection) and notebook.py (filing).

Images are synthetic — real notebook photos are private and stay out of the
repo. Each scene is a dark textured table with one or two bright pages that
carry a dot grid and some 'writing'.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
import notebook_image as ni  # noqa: E402
import notebook as nbk  # noqa: E402

PAGE_ASPECT = 1050 / 1500
HAVE_PDF_TOOLS = all(shutil.which(t) for t in ("img2pdf", "pdfimages"))


# ---------------------------------------------------------------------------
# Synthetic scenes
# ---------------------------------------------------------------------------

def _table(h=1600, w=2000, seed=0):
    rng = np.random.default_rng(seed)
    base = rng.normal(55, 12, (h, w)).clip(0, 255).astype(np.uint8)
    base = cv2.GaussianBlur(base, (0, 0), 3)
    return cv2.merge([base, base, (base * 0.9).astype(np.uint8)])


def _paint_page(img, quad, level, marker=None, seed=1):
    """Fill a page quad with paper, dot grid, a few strokes and an optional
    big dark marker block (to tell which page came out)."""
    rng = np.random.default_rng(seed)
    quad = np.asarray(quad, np.int32)
    cv2.fillPoly(img, [quad], (level, level, level))
    x0, y0 = quad[:, 0].min(), quad[:, 1].min()
    x1, y1 = quad[:, 0].max(), quad[:, 1].max()
    inside = np.zeros(img.shape[:2], np.uint8)
    cv2.fillPoly(inside, [quad], 255)
    for y in range(y0 + 40, y1 - 30, 45):
        for x in range(x0 + 40, x1 - 30, 45):
            if inside[y, x]:
                cv2.circle(img, (x, y), 2, (150, 150, 150), -1)
    for _ in range(8):
        y = int(rng.integers(y0 + 80, y1 - 80))
        xa = int(rng.integers(x0 + 60, (x0 + x1) // 2))
        cv2.line(img, (xa, y), (xa + int(rng.integers(80, 250)), y + 5), (30, 30, 30), 4)
    if marker:
        mx = (x0 + x1) // 2
        my = (y0 + y1) // 2
        cv2.rectangle(img, (mx - 60, my - 60), (mx + 60, my + 60), (20, 20, 20), -1)
    return img


def single_page(w=700, h=1000, skew=25):
    img = _table()
    cx, cy = 1000, 800
    quad = [[cx - w // 2 + skew, cy - h // 2], [cx + w // 2, cy - h // 2 + skew],
            [cx + w // 2 - skew, cy + h // 2], [cx - w // 2, cy + h // 2 - skew]]
    return _paint_page(img, quad, 235)


def spread(dip=True, step=True, marker="left", w=650, h=930):
    """Two facing pages; dip=V at the fold, step=pages lit differently."""
    img = _table()
    cx, cy = 1000, 800
    d = 80 if dip else 0
    left = [[cx - w, cy - h // 2], [cx, cy - h // 2 + d], [cx, cy + h // 2 - d], [cx - w, cy + h // 2]]
    right = [[cx, cy - h // 2 + d], [cx + w, cy - h // 2], [cx + w, cy + h // 2], [cx, cy + h // 2 - d]]
    _paint_page(img, left, 205 if step else 235, marker == "left", seed=2)
    _paint_page(img, right, 240, marker == "right", seed=3)
    return img


def partial_facing(keep="left"):
    """A page plus ~25% of its facing page, lit differently."""
    img = _table()
    w, h = 660, 940
    if keep == "left":
        x0 = 600
        main = [[x0, 330], [x0 + w, 330], [x0 + w, 330 + h], [x0, 330 + h]]
        extra = [[x0 + w, 330], [x0 + w + 220, 330], [x0 + w + 220, 330 + h], [x0 + w, 330 + h]]
    else:
        x0 = 820
        main = [[x0, 330], [x0 + w, 330], [x0 + w, 330 + h], [x0, 330 + h]]
        extra = [[x0 - 220, 330], [x0, 330], [x0, 330 + h], [x0 - 220, 330 + h]]
    _paint_page(img, main, 240, True, seed=4)
    _paint_page(img, extra, 205, None, seed=5)
    return img


PALE_SPREAD_QUADS = ([[420, 380], [1000, 430], [1000, 1250], [400, 1290]],
                     [[1000, 430], [1580, 380], [1600, 1290], [1000, 1250]])


def spread_on_pale_table():
    """A spread on a pale grey table that runs off the frame, on a dark
    floor: Otsu splits floor from table+paper, so the table merges with the
    pages unless the threshold is raised."""
    img = _table()
    table = np.array([[0, 0], [1850, 0], [1750, 1450], [0, 1550]], np.int32)
    cv2.fillPoly(img, [table], (160, 160, 160))
    left, right = PALE_SPREAD_QUADS
    _paint_page(img, left, 215, True, seed=2)
    _paint_page(img, right, 240, None, seed=3)
    return img


def wood_table(h=1600, w=1200, seed=0):
    rng = np.random.default_rng(seed)
    xs = np.arange(w)[None, :]
    grain = (np.sin(xs / 9.0 + rng.normal(0, 0.3, (h, 1))) * 12).astype(np.float32)
    img = np.dstack([140 + grain, 175 + grain, 205 + grain]).clip(0, 255).astype(np.uint8)
    return cv2.GaussianBlur(img, (0, 0), 1.5)


COVER_QUAD = [[300, 260], [880, 300], [900, 1300], [270, 1270]]


def dark_cover(table=None, quad=COVER_QUAD, colour=(35, 50, 80), band=True):
    """A dark leather notebook cover (with label and elastic band) on a table."""
    img = wood_table() if table is None else table
    rng = np.random.default_rng(7)
    q = np.asarray(quad, np.int32)
    m = np.zeros(img.shape[:2], np.uint8)
    cv2.fillPoly(m, [q], 255)
    tex = (np.full(img.shape, colour, np.float32)
           + rng.normal(0, 7, img.shape[:2] + (1,))).clip(0, 255).astype(np.uint8)
    img[m > 0] = tex[m > 0]
    c = q.mean(axis=0).astype(int)
    cv2.rectangle(img, (c[0] - 120, c[1] - 260), (c[0] + 60, c[1] - 200), (205, 210, 215), -1)
    if band:
        bx = int((q[1][0] + q[2][0]) / 2 - 60)
        cv2.line(img, (bx, q[1][1] - 5), (bx, q[2][1] + 5), (20, 20, 20), 9)
    return img


def corner_error(quad, truth):
    """Worst corner distance as a fraction of the truth's diagonal (order-free)."""
    truth = np.asarray(truth, np.float32)
    worst = max(min(np.linalg.norm(p - g) for g in truth) for p in quad)
    return worst / np.linalg.norm(np.ptp(truth, axis=0))


def dark_fraction(page):
    return float((page < 60).mean())


def centre_dark(page):
    """Dark share in the middle of a finished page, where the marker sits."""
    h, w = page.shape[:2]
    return dark_fraction(page[int(h * .35):int(h * .65), int(w * .3):int(w * .7)])


# ---------------------------------------------------------------------------
# notebook_image
# ---------------------------------------------------------------------------

class TestGeometry(unittest.TestCase):
    def test_order_corners_any_rotation(self):
        sq = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], np.float32)
        for deg in (0, 30, 44, 45, 46, 60, 89):
            M = cv2.getRotationMatrix2D((50, 50), deg, 1.0)
            pts = cv2.transform(sq.reshape(-1, 1, 2), M).reshape(-1, 2)
            out = ni.order_corners(pts[::-1])          # scrambled input
            self.assertEqual(len({tuple(np.round(p, 3)) for p in out}), 4, deg)
            # clockwise in image coords: positive signed area
            area = 0.5 * sum(out[i][0] * out[(i + 1) % 4][1] - out[(i + 1) % 4][0] * out[i][1]
                             for i in range(4))
            self.assertGreater(area, 0, deg)

    def test_params_roundtrip_drops_defaults(self):
        p = ni.PageParams(rotate=90, half="left", flip=True)
        self.assertEqual(p.to_dict(), {"rotate": 90, "half": "left", "flip": True})
        self.assertEqual(ni.PageParams.from_dict(p.to_dict()), p)
        self.assertEqual(ni.PageParams.from_dict({"bogus": 1}), ni.PageParams())


class TestLayout(unittest.TestCase):
    def test_single_page(self):
        lay = ni.classify_layout(single_page(), PAGE_ASPECT)
        self.assertEqual((lay.kind, lay.rotate, lay.halves), ("single", 0, [None]))

    def test_spread_with_dip(self):
        lay = ni.classify_layout(spread(dip=True, step=False), PAGE_ASPECT)
        self.assertEqual((lay.kind, lay.halves), ("spread", ["left", "right"]))

    def test_flat_spread_found_by_shading(self):
        lay = ni.classify_layout(spread(dip=False, step=True), PAGE_ASPECT)
        self.assertEqual((lay.kind, lay.halves), ("spread", ["left", "right"]))
        self.assertIn("shadow", lay.reason)

    def test_sideways_spread(self):
        img = cv2.rotate(spread(dip=False, step=True), cv2.ROTATE_90_COUNTERCLOCKWISE)
        lay = ni.classify_layout(img, PAGE_ASPECT)
        self.assertEqual(lay.kind, "spread")
        self.assertEqual(lay.rotate, 90)

    def test_partial_facing_page_keeps_main_page(self):
        for keep in ("left", "right"):
            lay = ni.classify_layout(partial_facing(keep), PAGE_ASPECT)
            self.assertEqual((lay.kind, lay.halves), ("single", [keep]), keep)

    def test_no_paper(self):
        self.assertEqual(ni.classify_layout(_table(), PAGE_ASPECT).kind, "none")

    def test_spread_on_pale_table_off_frame(self):
        img = spread_on_pale_table()
        lay = ni.classify_layout(img, PAGE_ASPECT)
        self.assertEqual((lay.kind, lay.rotate, lay.halves), ("spread", 0, ["left", "right"]))
        for half, truth in zip(("left", "right"), PALE_SPREAD_QUADS):
            res = ni.detect_page_quad(img, half=half, page_aspect=PAGE_ASPECT)
            assert res is not None
            self.assertLess(corner_error(res[0], truth), 0.05, half)


class TestCoverDetection(unittest.TestCase):
    ASPECT = 1050 / 1650

    def test_dark_cover_on_wood(self):
        res = ni.detect_object_quad(dark_cover(), self.ASPECT)
        assert res is not None
        self.assertLess(corner_error(res[0], COVER_QUAD), 0.03)

    def test_black_cover_on_white_desk(self):
        desk = np.full((1600, 1200, 3), 232, np.uint8)
        res = ni.detect_object_quad(dark_cover(desk, colour=(28, 28, 30)), self.ASPECT)
        assert res is not None
        self.assertLess(corner_error(res[0], COVER_QUAD), 0.03)

    def test_sideways_cover_turned_upright(self):
        quad = [[220, 420], [1180, 380], [1210, 1000], [200, 1040]]
        img = dark_cover(wood_table(1400, 1400), quad, band=False)
        res = ni.detect_object_quad(img, self.ASPECT)
        assert res is not None
        q, info = res
        self.assertTrue(info.get("sideways"))
        self.assertLess(corner_error(q, quad), 0.03)
        page, pinfo = ni.process_page(img, ni.PageParams(object=True, color=True, bleed=0), 1050, 1650)
        self.assertEqual(page.shape, (1650, 1050, 3))
        self.assertTrue(any("sideways" in f for f in pinfo["flags"]))

    def test_nothing_rectangular(self):
        rng = np.random.default_rng(3)
        noise = rng.integers(0, 255, (1200, 900, 3), dtype=np.uint8)
        self.assertIsNone(ni.detect_object_quad(noise, self.ASPECT))
        with self.assertRaises(ValueError):
            ni.process_page(noise, ni.PageParams(object=True), 1050, 1650)


class TestProcess(unittest.TestCase):
    def test_output_geometry_and_flags(self):
        page, info = ni.process_page(single_page(), ni.PageParams(), 1050, 1500)
        self.assertEqual(page.shape, (1500, 1050))
        self.assertEqual(info["mode"], "detected")
        self.assertIsInstance(info["flags"], list)

    def test_spread_halves_come_from_the_right_side(self):
        img = spread(dip=True, step=False, marker="left")
        left, _ = ni.process_page(img, ni.PageParams(half="left"), 1050, 1500)
        right, _ = ni.process_page(img, ni.PageParams(half="right"), 1050, 1500)
        self.assertGreater(centre_dark(left), centre_dark(right) + 0.05)

    def test_seed_selects_page_under_it(self):
        img = spread(dip=True, step=False, marker="right")
        on_right, info = ni.process_page(img, ni.PageParams(seed=[1350, 800]), 1050, 1500)
        on_left, _ = ni.process_page(img, ni.PageParams(seed=[650, 800]), 1050, 1500)
        self.assertEqual(info["side"], "right")
        self.assertGreater(centre_dark(on_right), centre_dark(on_left) + 0.05)

    def test_flip_is_exact_180(self):
        img = single_page()
        a, _ = ni.process_page(img, ni.PageParams(), 1050, 1500)
        b, _ = ni.process_page(img, ni.PageParams(flip=True), 1050, 1500)
        np.testing.assert_array_equal(a, cv2.rotate(b, cv2.ROTATE_180))

    def test_full_frame_keep_aspect_pads_white(self):
        strip = np.full((900, 300, 3), 120, np.uint8)
        page, _ = ni.process_page(strip, ni.PageParams(full_frame=True, keep_aspect=True,
                                                       color=True), 1050, 1500)
        self.assertEqual(page.shape, (1500, 1050, 3))
        self.assertTrue((page[:, :50] == 255).all())          # padded margins
        self.assertTrue((page[700:800, 500:550] == 120).all())  # content kept

    def test_full_frame_half_of_a_scanned_spread(self):
        scan = np.full((1000, 1400, 3), 240, np.uint8)
        scan[:, :700] = 30
        left, _ = ni.process_page(scan, ni.PageParams(full_frame=True, half="left", color=True), 1050, 1500)
        right, _ = ni.process_page(scan, ni.PageParams(full_frame=True, half="right", color=True), 1050, 1500)
        self.assertGreater(dark_fraction(left), 0.9)
        self.assertLess(dark_fraction(right), 0.1)

    def test_bleed_suppression_keeps_ink_drops_ghosts(self):
        g = np.full((400, 400), 255, np.uint8)
        g[50:60, 50:350] = 20        # ink
        g[200:210, 50:350] = 215     # faint show-through
        g[300:304, 100:104] = 150    # a printed dot
        out = ni.suppress_bleed(g, 1.0)
        self.assertLess(out[55, 200], 40)
        self.assertGreater(out[205, 200], 245)
        self.assertLess(out[302, 102], 230)

    def test_dewarp_straightens_bowed_edges(self):
        h, w = 1500, 1050
        mask = np.zeros((h, w), np.uint8)
        for x in range(w):
            sag = int(30 * np.sin(np.pi * x / (w - 1)))
            mask[20 + sag:h - 20 - sag, x] = 255
        page = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        out, out_mask, info = ni.dewarp_curl(page, mask)
        self.assertEqual(info["dewarp"], "applied")
        top, _ = ni.top_bottom_profiles(out_mask)
        self.assertLess(np.ptp(top[50:-50]), 6)

    def test_dewarp_ignores_a_bite_out_of_one_edge(self):
        # flat page whose bottom edge lost a chunk to a shadowed corner:
        # that is not curl, and correcting it would drag writing off the page
        h, w = 1650, 1050
        mask = np.zeros((h, w), np.uint8)
        mask[10:h - 5] = 255
        mask[h - 130:, 650:900] = 0
        page = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        out, _, info = ni.dewarp_curl(page, mask)
        self.assertEqual(info.get("edge_ignored"), "bottom")
        self.assertNotEqual(info["dewarp"], "applied")
        np.testing.assert_array_equal(out, page)
        flags = ni.quality_flags(np.full((h, w), 255, np.uint8), mask, info, None)
        self.assertTrue(any("bitten" in f for f in flags))


# ---------------------------------------------------------------------------
# notebook.py — index note patching (pure text)
# ---------------------------------------------------------------------------

NOTE = """---
started: 2026-09-30
size: passport (3.5x5in)
status: active
pages: 2
---

# 2026-09-30 Notebook

Intro text.

## Cover

Leica cover.

## Page 001 — title page (received 2026-10-27)

> title

## Page 003 (received 2026-10-27)

three
"""


class TestNotePatching(unittest.TestCase):
    def test_norm_page(self):
        self.assertEqual(nbk.norm_page("007"), "7")
        self.assertEqual(nbk.norm_page(12), "12")
        self.assertEqual(nbk.norm_page("Cover"), "cover")
        self.assertEqual(nbk.norm_page("back"), "back")
        for bad in ("x", "1000", "-1", ""):
            with self.assertRaises(nbk.NotebookError):
                nbk.norm_page(bad)

    def test_insert_in_page_order(self):
        out = nbk.set_section(NOTE, "2", "two", "## Page 002 (received 2026-11-01)")
        heads = [l for l in out.splitlines() if l.startswith("## ")]
        self.assertEqual(heads, ["## Cover", "## Page 001 — title page (received 2026-10-27)",
                                 "## Page 002 (received 2026-11-01)", "## Page 003 (received 2026-10-27)"])
        self.assertIn("Intro text.", out)

    def test_append_after_last(self):
        out = nbk.set_section(NOTE, "10", "ten", "## Page 010 (x)")
        self.assertTrue(out.rstrip().endswith("ten"))
        self.assertIn("three\n\n## Page 010 (x)\n\nten", out)

    def test_replace_keeps_neighbours(self):
        out = nbk.set_section(NOTE, "1", "new title", "## Page 001 — title page (received 2026-10-27)")
        self.assertIn("new title", out)
        self.assertNotIn("> title", out)
        self.assertIn("Leica cover.", out)
        self.assertIn("three", out)

    def test_page_10_does_not_match_page_1(self):
        text = NOTE + "\n## Page 010 (x)\n\nten\n"
        self.assertEqual(nbk._section_re("1").search(text).group(0),
                         "## Page 001 — title page (received 2026-10-27)")

    def test_frontmatter_patch_is_targeted(self):
        out = nbk._patch_frontmatter(NOTE, {"pages": 3, "finished": "2026-12-01"})
        self.assertIn("pages: 3\n", out)
        self.assertIn("finished: 2026-12-01\n---", out)
        self.assertIn("size: passport (3.5x5in)\n", out)
        self.assertEqual(out.split("---\n", 2)[2], NOTE.split("---\n", 2)[2])

    def test_parse_size(self):
        self.assertEqual(nbk._size_record("passport (3.5x5in)")["px"], [1050, 1500])
        self.assertEqual(nbk._size_record("regular")["px"], [1299, 2480])
        self.assertEqual(nbk._size_record("90x140mm")["pagesize"], "90mmx140mm")
        self.assertEqual(nbk._size_record("field-notes")["px"], [1050, 1650])


# ---------------------------------------------------------------------------
# notebook.py — end to end on a temporary vault
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_PDF_TOOLS, "img2pdf / poppler-utils not installed")
class TestNotebookFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "Notebooks"
        self.stage = self.tmp / "staging"
        self.patches = [mock.patch.object(nbk, "NOTEBOOKS_DIR", self.root),
                        mock.patch.object(nbk, "NOTEBOOK_STAGING_DIR", self.stage),
                        mock.patch.object(nbk, "_today", lambda: "2026-11-01")]
        for p in self.patches:
            p.start()
        nbk.new_notebook("Test", "passport", started="2026-10-01", description="A test.")
        self.nb = nbk.find_notebook()
        self.photos = self.tmp / "in"
        self.photos.mkdir()
        cv2.imwrite(str(self.photos / "spread.jpg"), spread(dip=False, step=True, marker="left"))
        cv2.imwrite(str(self.photos / "single.jpg"), single_page())

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def _ingest_file(self, names, assign):
        b = nbk.ingest(self.nb, [str(self.photos / n) for n in names])
        return b, nbk.file_pages(b["id"], [{"candidate": c, "page": p} for c, p in assign])

    def test_spread_shares_one_original(self):
        b, r = self._ingest_file(["spread.jpg"], [("c1", 1), ("c2", 2)])
        self.assertEqual(len(b["candidates"]), 2)
        self.assertEqual(r["status"]["pages"], [1, 2])
        self.assertEqual(r["status"]["originals"], 1)
        self.assertEqual(r["status"]["shared_originals"], [["1", "2"]])
        self.assertIn("each serve two pages (1+2)", r["status"]["originals_note"])
        self.assertEqual(len(list((self.nb / nbk.ORIGINALS).iterdir())), 1)
        self.assertTrue(nbk.pdf_path(self.nb).exists())
        self.assertIn("pages: 2", nbk.index_note(self.nb).read_text())

    def test_replace_prunes_unused_original_only(self):
        self._ingest_file(["spread.jpg"], [("c1", 1), ("c2", 2)])
        # replacing page 2 keeps the spread photo (page 1 still uses it)
        _, r = self._ingest_file(["single.jpg"], [("c1", 2)])
        self.assertEqual(r["replaced"], ["2"])
        self.assertEqual(r["originals_removed"], [])
        self.assertEqual(r["status"]["originals"], 2)
        # replacing page 1 too frees the spread photo
        _, r = self._ingest_file(["single.jpg"], [("c1", 1)])
        self.assertEqual(len(r["originals_removed"]), 1)
        self.assertEqual(r["status"]["originals"], 1)

    def test_gaps_transcription_status_finish(self):
        self._ingest_file(["spread.jpg"], [("c1", 1), ("c2", 4)])
        st = nbk.status(self.nb)
        self.assertEqual(st["gaps"], ["2-3"])
        self.assertEqual(st["untranscribed"], ["1", "4"])
        nbk.transcribe(self.nb, 4, "four")
        nbk.transcribe(self.nb, 1, "one")
        text = nbk.index_note(self.nb).read_text()
        self.assertLess(text.index("## Page 001 (received 2026-11-01)"),
                        text.index("## Page 004 (received 2026-11-01)"))
        self.assertEqual(nbk.status(self.nb)["untranscribed"], [])
        r = nbk.finish(self.nb)
        self.assertEqual(r["status"]["status"], "finished")
        self.assertIn("finished: 2026-11-01", nbk.index_note(self.nb).read_text())

    def test_flip_cover_colour_and_duplicates(self):
        b = nbk.ingest(self.nb, [str(self.photos / "single.jpg")])
        with self.assertRaises(nbk.NotebookError):
            nbk.file_pages(b["id"], [{"candidate": "c1", "page": 1},
                                     {"candidate": "c1", "page": 1}])
        nbk.file_pages(b["id"], [{"candidate": "c1", "page": "cover", "flip": True}])
        man = json.loads((self.nb / nbk.MANIFEST).read_text())
        self.assertEqual(man["pages"]["cover"]["params"].get("flip"), True)
        self.assertEqual(man["pages"]["cover"]["params"].get("color"), True)

    def test_reprocess_and_rebuild(self):
        self._ingest_file(["single.jpg"], [("c1", 1)])
        before = (self.nb / "page-001.jpg").read_bytes()
        rb = nbk.reprocess(self.nb, 1, flip=True)
        self.assertEqual(rb["candidates"][0]["params"].get("flip"), True)
        nbk.file_pages(rb["id"], [{"candidate": "c1", "page": 1}])
        r = nbk.rebuild(self.nb)
        self.assertEqual(r["rebuilt"], ["1"])
        self.assertNotEqual((self.nb / "page-001.jpg").read_bytes(), before)

    def _page_size(self, pid):
        from PIL import Image
        with Image.open(self.nb / nbk.page_filename(pid)) as im:
            return im.size

    def test_set_size_rerenders_everything(self):
        self._ingest_file(["spread.jpg"], [("c1", 1), ("c2", 2)])
        self.assertEqual(self._page_size("1"), (1050, 1500))
        r = nbk.set_size(self.nb, "field-notes")
        self.assertEqual(r["rebuilt"], ["1", "2"])
        self.assertEqual(r["page_px"], [1050, 1650])
        self.assertEqual(self._page_size("2"), (1050, 1650))
        fm = nbk.index_note(self.nb).read_text()
        self.assertIn("size: field-notes (3.5inx5.5in)\n", fm)
        self.assertIn("page_px: 1050x1650\n", fm)
        self.assertEqual(nbk.status(self.nb)["page_px"], [1050, 1650])

    def test_candidates_staged_before_resize_are_rerendered(self):
        b = nbk.ingest(self.nb, [str(self.photos / "single.jpg")])
        nbk.set_size(self.nb, "3.5x5.5in")
        nbk.file_pages(b["id"], [{"candidate": "c1", "page": 1}])
        self.assertEqual(self._page_size("1"), (1050, 1650))

    def test_set_size_refuses_pages_without_originals(self):
        shutil.copyfile(self.photos / "single.jpg", self.nb / "page-005.jpg")
        with self.assertRaises(nbk.NotebookError):
            nbk.set_size(self.nb, "field-notes")
        self.assertIn("page_px: 1050x1500", nbk.index_note(self.nb).read_text())

    def test_close_requires_finish_and_confirm(self):
        self._ingest_file(["single.jpg"], [("c1", 1)])
        with self.assertRaises(nbk.NotebookError):
            nbk.close(self.nb, confirm=True)            # not finished yet
        nbk.finish(self.nb)
        with self.assertRaises(nbk.NotebookError):
            nbk.close(self.nb)                          # no confirm
        self.assertTrue(self.nb.exists())

    def test_close_moves_pdf_and_note_up_and_deletes_folder(self):
        self._ingest_file(["spread.jpg"], [("c1", 1), ("c2", 2)])
        nbk.transcribe(self.nb, 1, "one")
        nbk.finish(self.nb)
        r = nbk.close(self.nb, confirm=True)
        self.assertFalse(self.nb.exists())
        pdf, note = Path(r["pdf"]), Path(r["note"])
        self.assertEqual(pdf.parent, self.root)
        self.assertTrue(pdf.exists())
        text = note.read_text()
        self.assertIn("status: closed\n", text)
        self.assertIn("closed: 2026-11-01", text)
        self.assertIn(f"PDF: [[{pdf.name}]]", text)
        self.assertIn("## Page 001", text)                 # transcriptions kept
        self.assertEqual(r["pages"], [1, 2])
        listed = {n["name"]: n for n in nbk.list_notebooks()}
        self.assertEqual(listed[self.nb.name]["status"], "closed")
        self.assertEqual(listed[self.nb.name]["pages"], 2)

    def test_close_refuses_unexpected_files_and_clashes(self):
        self._ingest_file(["single.jpg"], [("c1", 1)])
        nbk.finish(self.nb)
        stray = self.nb / "notes-to-self.txt"
        stray.write_text("keep me")
        with self.assertRaises(nbk.NotebookError):
            nbk.close(self.nb, confirm=True)
        stray.unlink()
        (self.root / f"{self.nb.name}.pdf").write_bytes(b"x")
        with self.assertRaises(nbk.NotebookError):
            nbk.close(self.nb, confirm=True)
        self.assertTrue((self.nb / "page-001.jpg").exists())

    def test_cover_photo_found_as_object(self):
        cv2.imwrite(str(self.photos / "cover.jpg"), dark_cover())
        b = nbk.ingest(self.nb, [str(self.photos / "cover.jpg")], cover=True)
        self.assertEqual(len(b["candidates"]), 1)
        c = b["candidates"][0]
        self.assertTrue(c["params"].get("object"))
        self.assertTrue(c["params"].get("color"))
        nbk.file_pages(b["id"], [{"candidate": "c1", "page": "cover"}])
        self.assertTrue((self.nb / "page-000-cover.jpg").exists())
        rb = nbk.reprocess(self.nb, "cover", flip=True)
        self.assertTrue(rb["candidates"][0]["params"].get("object"))

    def test_legacy_page_without_original(self):
        shutil.copyfile(self.photos / "single.jpg", self.nb / "page-005.jpg")
        st = nbk.status(self.nb)
        self.assertEqual(st["no_original"], ["5"])
        self.assertEqual(nbk.rebuild(self.nb)["no_original"], ["5"])
        with self.assertRaises(nbk.NotebookError):
            nbk.reprocess(self.nb, 5)


if __name__ == "__main__":
    unittest.main()
