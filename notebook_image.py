"""Image pipeline for notebook page photos and scans.

Shared by `notebook.py` (the notebook manager / MCP backend) and the
`notebook-page-clean.py` CLI. Everything here is pure image processing: no
vault paths, no state.

Pipeline for a photo:
  1. classify_layout(): segment the paper, decide single page vs two-page
     spread (fold dip in the paper's top profile, two side-by-side blobs, or
     the blob's aspect against the notebook's known page aspect), and whether
     the shot is sideways.
  2. detect_page_quad(): fit a quad to the requested page's own mask half
     (facing pages are never coplanar), extend the inner edge across the
     fold, then trim it back to the crease when the groove is visible.
  3. process_page(): warp to exact page geometry, straighten page curl
     (dewarp), flatten illumination, optionally suppress bleed-through, and
     report quality flags for the reviewer.

Scans that are already cropped to the page (Scanner Pro PDF pages) skip
detection: `full_frame=True` maps the whole frame to the page geometry.
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field

import cv2
import numpy as np

MASK_SCALE_PX = 1200          # working size for segmentation

# columns/rows outside the paper are all-NaN by design in the shading profiles
warnings.filterwarnings("ignore", message="All-NaN slice encountered")
warnings.filterwarnings("ignore", message="Mean of empty slice")
DPI = 300

# Clockwise rotation by `rotate` degrees, applied before anything else.
_ROTATIONS = {90: cv2.ROTATE_90_CLOCKWISE,
              180: cv2.ROTATE_180,
              270: cv2.ROTATE_90_COUNTERCLOCKWISE}


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_image(path) -> np.ndarray:
    """Load as BGR uint8 with EXIF orientation applied."""
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        pil = ImageOps.exif_transpose(im).convert("RGB")
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def rotate(img: np.ndarray, degrees: int) -> np.ndarray:
    """Rotate clockwise by 0/90/180/270 degrees."""
    degrees %= 360
    if degrees == 0:
        return img
    if degrees not in _ROTATIONS:
        raise ValueError(f"rotation must be 0/90/180/270, got {degrees}")
    return cv2.rotate(img, _ROTATIONS[degrees])


def save_image(img: np.ndarray, path, dpi: int = DPI, quality: int = 88) -> None:
    """Single JPEG encode with DPI metadata (gray or BGR input)."""
    from PIL import Image
    if img.ndim == 2:
        pil = Image.fromarray(img, mode="L")
    else:
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    pil.save(str(path), "JPEG", quality=quality, dpi=(dpi, dpi))


def header_strip(img: np.ndarray, frac: float = 0.16, scale: float = 2.0) -> np.ndarray:
    """Top strip of a page, enlarged — where the handwritten page number lives."""
    h = img.shape[0]
    strip = img[: max(1, int(h * frac))]
    return cv2.resize(strip, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def order_corners(pts) -> np.ndarray:
    """Return corners ordered ul, ur, lr, ll.

    Sorts by angle around the centroid (robust at any rotation, unlike the
    x+y / y-x trick, which can pick the same point twice near 45 degrees),
    then starts from the corner nearest the image origin."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])   # y down: clockwise
    pts = pts[np.argsort(ang)]
    start = int(np.argmin(pts.sum(axis=1)))
    return np.roll(pts, -start, axis=0).astype(np.float32)


def _dst_rect(out_w: int, out_h: int) -> np.ndarray:
    return np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
                    dtype=np.float32)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def paper_mask(img):
    """Binary mask of bright/unsaturated paper at reduced scale.
    Returns (mask, scale). Aggressive opening breaks thin bridges to
    bright background (tables, cloth)."""
    h, w = img.shape[:2]
    scale = float(MASK_SCALE_PX) / max(h, w)
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]
    t, bright = cv2.threshold(val, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if t < 90:
        # Otsu split a uniformly dark scene (no paper) at its own noise
        return np.zeros_like(val), scale
    mask = cv2.bitwise_and(bright, (sat < 90).astype(np.uint8) * 255)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    # strong open: disconnect the page from bright background it touches
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))
    return mask, scale


def page_blobs(mask, min_area_frac=0.10, second_frac=0.40):
    """Filled masks of the paper blobs, largest first: the largest, plus a
    second one when it is comparable in size (two pages separated by a
    shadowed gutter segment as two blobs)."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    total = mask.shape[0] * mask.shape[1]
    a0 = cv2.contourArea(contours[0])
    if a0 < min_area_frac * total:
        return []
    keep = [contours[0]]
    if len(contours) > 1 and cv2.contourArea(contours[1]) >= second_frac * a0:
        keep.append(contours[1])
    out = []
    for c in keep:
        m = np.zeros_like(mask)
        cv2.drawContours(m, [c], -1, 255, cv2.FILLED)
        out.append(m)
    return out


def largest_blob_mask(mask, min_area_frac=0.10):
    blobs = page_blobs(mask, min_area_frac)
    return blobs[0] if blobs else None


def _blob_bbox(blob):
    ys, xs = np.nonzero(blob)
    return xs.min(), ys.min(), xs.max(), ys.max()


def _blob_centroid(blob):
    m = cv2.moments(blob, binaryImage=True)
    return m["m10"] / m["m00"], m["m01"] / m["m00"]


def top_bottom_profiles(mask):
    """Per-column first/last nonzero rows; -1 where column is empty."""
    h, w = mask.shape
    nz = mask > 0
    any_col = nz.any(axis=0)
    top = np.where(any_col, nz.argmax(axis=0), -1)
    bot = np.where(any_col, h - 1 - nz[::-1].argmax(axis=0), -1)
    return top, bot


def find_spine(mask):
    """Spine line of a two-page blob: the V-dip in the top profile and the
    bump in the bottom profile, searched over the middle of the blob.
    Returns ((xt, yt), (xb, yb)) in mask coords, or None if no clear dip
    (single page)."""
    h, w = mask.shape
    top, bot = top_bottom_profiles(mask)
    valid = np.where(top >= 0)[0]
    if valid.size < 20:
        return None
    x_lo, x_hi = valid[0], valid[-1]
    span = x_hi - x_lo
    mid_lo, mid_hi = x_lo + int(span * 0.30), x_lo + int(span * 0.70)
    mid = slice(mid_lo, mid_hi + 1)

    t_mid = top[mid].astype(np.float32)
    b_mid = bot[mid].astype(np.float32)
    if t_mid.size == 0:
        return None
    xt = mid_lo + int(np.argmax(t_mid))     # deepest top dip
    xb = mid_lo + int(np.argmin(b_mid))     # highest bottom bump

    # Reference edge height away from the middle (outer page tops)
    outer = np.r_[top[x_lo:mid_lo], top[mid_hi:x_hi]]
    outer = outer[outer >= 0]
    if outer.size == 0:
        return None
    dip_depth = top[xt] - np.median(outer)
    if dip_depth < 0.03 * h:                # no real dip: single page
        return None
    return (xt, int(top[xt])), (xb, int(bot[xb]))


def small_gray(img, scale):
    """Grayscale at the segmentation scale."""
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


def _paper_level_profile(gray, blob, axis):
    """Per-column (axis=0) or per-row (axis=1) paper brightness inside the
    blob: the 80th percentile, so sparse handwriting doesn't register but a
    shadow running the whole length does. NaN where the blob is thin."""
    g = gray.astype(np.float32)
    g[blob == 0] = np.nan
    if axis == 1:
        g = g.T
    counts = np.sum(~np.isnan(g), axis=0)
    with np.errstate(all="ignore"):
        prof = np.nanpercentile(g, 80, axis=0)
    prof[counts < 0.3 * max(1, counts.max())] = np.nan
    return prof


def _fold_candidates(prof, w, window, min_strength=12.0):
    """Candidate folds in a paper-brightness profile, strongest first. Two
    signatures: a brightness STEP (the pages face the light at different
    angles — the common case) or a narrow dark TROUGH (gutter shadow).
    Returns [(index, strength), ...]."""
    if np.isnan(prof).all():
        return []
    filled = np.where(np.isnan(prof), np.nanmedian(prof), prof).astype(np.float32)
    if filled.size >= 3:
        filled = cv2.medianBlur(filled.reshape(1, -1), 3).ravel()
    lo, hi = int(w * window[0]), int(w * window[1])
    if hi - lo < 5:
        return []
    s = max(2, int(0.012 * w))
    score = np.zeros_like(filled)
    # step: sharp change between short spans either side
    d = np.zeros_like(filled)
    d[s:-s] = np.abs(filled[2 * s:] - filled[:-2 * s])
    gradient = float(np.percentile(d[lo:hi], 75)) + 1.0
    step_ok = d >= 3.0 * gradient
    score = np.where(step_ok, d, 0)
    # trough: narrow dip below both sides
    k = max(3, int(0.05 * w))
    for j in range(lo, hi):
        left = filled[max(0, j - k):max(1, j - s)]
        right = filled[min(w - 1, j + s):min(w, j + k)]
        if left.size and right.size:
            depth = min(left.mean(), right.mean()) - filled[j]
            if depth > score[j]:
                score[j] = depth
    cands = []
    sc = score.copy()
    sc[:lo] = 0
    sc[hi:] = 0
    sep = max(3, int(0.05 * w))
    while True:
        j = int(np.argmax(sc))
        if sc[j] < min_strength:
            break
        cands.append((j, float(sc[j])))
        sc[max(0, j - sep):j + sep + 1] = 0      # suppress neighbours
    return cands


def _follow_fold(gray, blob, x0, y0, x1, y1, fx, w):
    """Fit the fold line through horizontal slabs (it may lean in
    perspective). Returns (a, b) with x = a*y + b in mask coords."""
    rad = max(4, int(0.06 * w))
    pts = []
    for sidx in range(6):
        ya = y0 + int((y1 - y0) * sidx / 6)
        yb = y0 + int((y1 - y0) * (sidx + 1) / 6)
        sub_blob = np.zeros_like(blob)
        sub_blob[ya:yb] = blob[ya:yb]
        with np.errstate(all="ignore"):
            p = _paper_level_profile(gray, sub_blob, axis=0)[x0:x1 + 1]
        c = _fold_candidates(p, w, (max(0.0, (fx - rad) / w), min(1.0, (fx + rad) / w)), 8.0)
        if c:
            pts.append(((ya + yb) / 2.0, float(x0 + c[0][0])))
    if len(pts) >= 4:
        ys = np.array([q[0] for q in pts])
        xs = np.array([q[1] for q in pts])
        a, b = np.polyfit(ys, xs, 1)
        if abs(a) <= 0.25:                      # steeper isn't a fold
            return float(a), float(b)
    return 0.0, float(x0 + fx)


def find_fold_line(gray, blob, page_aspect=None, window=(0.12, 0.88)):
    """Fold between two pages found from shading: works when the open
    notebook lies flat and the paper outline has no V-dip. Looks for a
    vertical fold; pass rotated inputs for a horizontal one.

    With `page_aspect`, a candidate is only accepted if the larger side it
    leaves is page-shaped — this rejects steps at the edge of bright clutter
    that merged into the paper blob.

    Returns (spine, position, strength) — spine as ((xt, yt), (xb, yb)) in
    mask coords spanning the blob's height, position = fold x as a fraction
    of the blob's width, strength = gray levels of the step/trough — or None."""
    x0, y0, x1, y1 = _blob_bbox(blob)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    if w < 40 or h < 40:
        return None
    with np.errstate(all="ignore"):
        prof = _paper_level_profile(gray, blob, axis=0)[x0:x1 + 1]
    for fx, strength in _fold_candidates(prof, w, window):
        if page_aspect:
            big = max(fx, w - fx) / float(h)
            if abs(math.log(big / page_aspect)) > 0.25:
                continue
        a, b = _follow_fold(gray, blob, x0, y0, x1, y1, fx, w)
        spine = ((int(round(a * y0 + b)), int(y0)), (int(round(a * y1 + b)), int(y1)))
        return spine, fx / float(w), strength
    return None


def locate_spine(gray, blob, page_aspect=None):
    """Spine of an open notebook in the blob: the V-dip in the outline if
    there is one, else a fold found from shading.
    Returns (spine, how, strength) or (None, None, 0)."""
    spine = find_spine(blob)
    if spine is not None:
        return spine, "dip", float("inf")
    fold = find_fold_line(gray, blob, page_aspect)
    if fold is not None:
        return fold[0], "fold", fold[2]
    return None, None, 0.0


def _spine_poly(mask_shape, spine, side):
    """Polygon covering the OTHER side of the (extended) spine line."""
    h, w = mask_shape
    (xt, yt), (xb, yb) = spine
    if yb == yt:
        x_top_ext, x_bot_ext = xt, xb
    else:
        slope = (xb - xt) / float(yb - yt)
        x_top_ext = xt + slope * (0 - yt)
        x_bot_ext = xb + slope * (h - 1 - yb)
    if side == "left":
        poly = np.array([[x_top_ext, 0], [w - 1, 0], [w - 1, h - 1], [x_bot_ext, h - 1]])
    else:
        poly = np.array([[0, 0], [x_top_ext, 0], [x_bot_ext, h - 1], [0, h - 1]])
    return poly.astype(np.int32)


def side_mask(mask, spine, side):
    """Zero out the mask on the other side of the spine line."""
    out = mask.copy()
    cv2.fillPoly(out, [_spine_poly(mask.shape, spine, side)], 0)
    return out


def _side_areas(blob, spine):
    left = int(np.count_nonzero(side_mask(blob, spine, "left")))
    right = int(np.count_nonzero(side_mask(blob, spine, "right")))
    return left, right


def _line_intersection(l1, l2):
    """Intersect two lines given as (vx, vy, x0, y0)."""
    vx1, vy1, x1, y1 = l1
    vx2, vy2, x2, y2 = l2
    den = vx1 * vy2 - vy1 * vx2
    if abs(den) < 1e-9:
        return None
    t = ((x2 - x1) * vy2 - (y2 - y1) * vx2) / den
    return np.array([x1 + t * vx1, y1 + t * vy1], dtype=np.float32)


def refine_quad_edges(contour, quad, band_frac=0.04, trim=0.18):
    """Re-fit each side of a quad as a least-squares line through the
    contour points along the middle of that side, then intersect adjacent
    lines for the corners. Rounded page corners and slightly bowed edges
    otherwise pull approxPolyDP's vertices inside the paper, clipping writing
    near the edge. Returns the refined quad, or the input if any side has
    too few points or a corner moves implausibly far."""
    pts = contour.reshape(-1, 2).astype(np.float32)
    q = order_corners(quad)
    lines = []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        ab = b - a
        L = float(np.hypot(*ab))
        if L < 10:
            return quad
        u = ab / L
        n = np.array([-u[1], u[0]], dtype=np.float32)
        rel = pts - a
        along = rel @ u
        across = np.abs(rel @ n)
        sel = (along > trim * L) & (along < (1 - trim) * L) & (across < band_frac * L)
        if sel.sum() < 10:
            return quad
        vx, vy, x0, y0 = cv2.fitLine(pts[sel], cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append((vx, vy, x0, y0))
    out = []
    for i in range(4):
        p = _line_intersection(lines[(i - 1) % 4], lines[i])
        if p is None:
            return quad
        out.append(p)
    out = np.array(out, dtype=np.float32)
    span = float(np.hypot(*(q[2] - q[0])))
    if np.max(np.hypot(*(out - q).T)) > 0.06 * span:
        return quad
    return out


def quad_from_mask(mask):
    """Fit a quad to the largest contour of a single-page mask.
    Returns (quad, used_fallback) or None."""
    # kill slivers and satellite blobs that survived the first opening
    # (bright table edges touching the page along a thin bridge)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((25, 25), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    c_area = cv2.contourArea(c)
    if c_area < 0.02 * mask.shape[0] * mask.shape[1]:
        return None
    peri = cv2.arcLength(c, True)
    best = None
    fallback = False
    for eps in (0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(c, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            best = approx.reshape(4, 2).astype(np.float32)
            break
    if best is None:
        best = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
        fallback = True
    # sanity: the quad must actually hug the contour
    q_area = cv2.contourArea(best.astype(np.int32))
    if q_area <= 0 or not (0.75 <= q_area / c_area <= 1.35):
        best = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
        fallback = True
        q_area = cv2.contourArea(best.astype(np.int32))
        if q_area <= 0 or not (0.75 <= q_area / c_area <= 1.45):
            return None
    if not fallback:
        best = refine_quad_edges(c, best)
    return order_corners(best), fallback


# ---------------------------------------------------------------------------
# Layout classification
# ---------------------------------------------------------------------------

@dataclass
class Layout:
    """What a photo contains, in the orientation given by `rotate`.

    kind:   "single" | "spread" | "none"
    halves: pages to extract — [None] for a lone page, ["left", "right"] for
            a spread, or ["left"] / ["right"] for a single page whose facing
            page is partly in frame.
    two_blob: the two pages segmented as separate blobs (no shared outline).
    """
    kind: str
    rotate: int = 0
    halves: list = field(default_factory=list)
    two_blob: bool = False
    reason: str = ""


def _classify_upright(img, page_aspect):
    """Classify assuming the photo is upright. Returns a Layout, or a Layout
    with kind 'sideways-*' when the evidence says it's rotated 90 degrees."""
    mask, scale = paper_mask(img)
    blobs = page_blobs(mask)
    if not blobs:
        return Layout("none", reason="no paper found")

    if len(blobs) == 2:
        (x0, y0), (x1, y1) = _blob_centroid(blobs[0]), _blob_centroid(blobs[1])
        if abs(x1 - x0) > abs(y1 - y0):
            return Layout("spread", halves=["left", "right"], two_blob=True,
                          reason="two side-by-side paper blobs")
        # stacked vertically: a sideways spread
        return Layout("sideways-spread", reason="two stacked paper blobs")

    blob = blobs[0]
    gray = small_gray(img, scale)
    spine, how, strength = locate_spine(gray, blob, page_aspect)
    # a sideways spread has its fold running across the long axis; when only
    # shading evidence exists in both directions, the stronger one wins
    rblob = cv2.rotate(blob, cv2.ROTATE_90_CLOCKWISE)
    rgray = cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
    rspine, rhow, rstrength = locate_spine(rgray, rblob, page_aspect)
    if rspine is not None and rstrength > strength:
        return Layout("sideways-spread", reason=("fold dip" if rhow == "dip" else "fold shadow") + " across the long axis")
    if spine is not None:
        la, ra = _side_areas(blob, spine)
        small, big = min(la, ra), max(la, ra)
        what = "fold dip" if how == "dip" else "fold shadow"
        if big and small / big >= 0.6:
            return Layout("spread", halves=["left", "right"],
                          reason=f"{what}; both pages fully in frame")
        side = "left" if la >= ra else "right"
        return Layout("single", halves=[side],
                      reason=f"{what}; facing page only partly in frame, keeping {side}")


    # no dip: decide by the blob's aspect against the known page aspect
    x0, y0, x1, y1 = _blob_bbox(blob)
    a = (x1 - x0 + 1) / float(y1 - y0 + 1)
    p = page_aspect
    hyps = [("single", p), ("spread", 2 * p), ("sideways-single", 1 / p),
            ("sideways-spread", 1 / (2 * p))]          # listed in prior order
    dists = [(abs(math.log(a / h_a)), i, name) for i, (name, h_a) in enumerate(hyps)]
    best_d = min(d for d, _, _ in dists)
    # within 0.05 of the best fit, prefer the earlier (more common) hypothesis
    name = min((i, n) for d, i, n in dists if d <= best_d + 0.05)[1]
    reason = f"no fold dip; paper aspect {a:.2f} vs page {p:.2f}"
    if name == "single":
        return Layout("single", halves=[None], reason=reason)
    if name == "spread":
        return Layout("spread", halves=["left", "right"], reason=reason + " (split at centre)")
    return Layout(name, reason=reason)


def classify_layout(img, page_aspect: float) -> Layout:
    """Decide what the photo contains. `page_aspect` = page width / height.

    Sideways shots are rotated 90 degrees clockwise; whether that should
    have been 270 can't be told from the outline, so the reviewer may need
    to flip the result 180."""
    lay = _classify_upright(img, page_aspect)
    if not lay.kind.startswith("sideways"):
        return lay
    rot = _classify_upright(rotate(img, 90), page_aspect)
    rot.rotate = 90
    if rot.kind.startswith("sideways") or rot.kind == "none":
        # inconsistent evidence: fall back to the upright reading
        want_spread = lay.kind == "sideways-spread"
        return Layout("spread" if want_spread else "single",
                      halves=["left", "right"] if want_spread else [None],
                      reason=lay.reason + "; rotation check inconclusive")
    rot.reason = f"sideways ({lay.reason}); rotated 90: {rot.reason}"
    return rot


# ---------------------------------------------------------------------------
# Page quad detection
# ---------------------------------------------------------------------------

def detect_page_quad(img, half=None, seed=None, two_blob=False, page_aspect=None):
    """Full-res quad of one page. Returns (quad, side, info) or None.

    half:  "left"/"right" — take that page of a spread (or of a single page
           with the facing page in frame). None — a lone page, or choose the
           page under `seed` when a fold is visible.
    seed:  (x, y) point on the wanted page; default image centre."""
    mask, scale = paper_mask(img)
    blobs = page_blobs(mask)
    if not blobs:
        return None
    info = {}
    side = None
    if two_blob and len(blobs) == 2:
        cx = [_blob_centroid(b)[0] for b in blobs]
        order = np.argsort(cx)                       # left blob first
        target = blobs[order[0] if half == "left" else order[1]]
        side = half
    else:
        blob = blobs[0]
        spine, how, _ = locate_spine(small_gray(img, scale), blob, page_aspect)
        if spine is None:
            if half:
                # no dip found; cut at the blob's horizontal middle
                top, _ = top_bottom_profiles(blob)
                valid = np.where(top >= 0)[0]
                cx = int((valid[0] + valid[-1]) / 2)
                spine = ((cx, 0), (cx, blob.shape[0] - 1))
                side = half
                target = side_mask(blob, spine, half)
                info["spine"] = "centre-cut"
            else:
                target = blob  # single page, whole blob
        else:
            info["spine"] = how
            if half:
                side = half
            else:
                if seed is None:
                    seed = (img.shape[1] / 2.0, img.shape[0] / 2.0)
                sx, sy = seed[0] * scale, seed[1] * scale
                (xt, yt), (xb, yb) = spine
                # which side of the top->bottom spine line is the seed on?
                # (image y points down, so cross < 0 means right of the line)
                cross = (xb - xt) * (sy - yt) - (yb - yt) * (sx - xt)
                side = "right" if cross < 0 else "left"
            target = side_mask(blob, spine, side)
    res = quad_from_mask(target)
    if res is None:
        return None
    quad, fallback = res
    info["quad_fallback"] = fallback
    if side is not None:
        # writing often crosses the fold: extend the inner (spine) edge
        # a touch into the facing page so gutter-hugging strokes survive
        ul, ur, lr, ll = quad
        if side == "right":
            ul = ul - 0.025 * (ur - ul)
            ll = ll - 0.025 * (lr - ll)
        else:
            ur = ur + 0.025 * (ur - ul)
            lr = lr + 0.025 * (lr - ll)
        quad = np.array([ul, ur, lr, ll], dtype=np.float32)
    return quad / scale, side, info


def trim_edge_to_crease(img, quad, edge, out_w, out_h):
    """If the fold crease (dark groove, continuous top to bottom) sits inside
    the given OUTPUT edge's band, pull that edge in to it. Trim only — a clean
    page edge has no groove and is left untouched. Returns (quad, trimmed)."""
    M = cv2.getPerspectiveTransform(quad, _dst_rect(out_w, out_h))
    gray = cv2.cvtColor(cv2.warpPerspective(img, M, (out_w, out_h)), cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    band_w = int(out_w * 0.16)
    x0 = 0 if edge == "left" else out_w - band_w
    band = gray[:, x0:x0 + band_w].astype(np.float32)
    page_ref = float(np.median(gray[:, out_w // 4: 3 * out_w // 4]))

    # 5 horizontal slabs; column means kill handwriting, keep the crease.
    # Strict darkness gate: only a clearly shadowed groove triggers a trim,
    # so harsh-light folds and clean margins are left untouched.
    pts = []
    for i in range(5):
        y0, y1 = int(out_h * i / 5), int(out_h * (i + 1) / 5)
        prof = band[y0:y1].mean(axis=0)
        c = int(np.argmin(prof))
        if prof[c] < 0.85 * page_ref:
            pts.append(((y0 + y1) / 2.0, float(c)))
    if len(pts) < 3:
        return quad, False                      # no clear crease: keep as-is
    ys = np.array([p[0] for p in pts])
    xs = np.array([p[1] for p in pts])
    a, b = np.polyfit(ys, xs, 1)                # x = a*y + b within the band
    # cut slightly OUTSIDE the groove: writing that hugs the gutter often
    # crosses the crease center, and a faint shadow line at the edge is
    # cheaper than clipped strokes
    back = 0.01 * out_w * (-1 if edge == "left" else 1)
    x_top, x_bot = b + x0 + back, a * (out_h - 1) + b + x0 + back
    inner_out = np.array([[[x_top, 0]], [[x_bot, out_h - 1]]], dtype=np.float32)
    inner_orig = cv2.perspectiveTransform(inner_out, np.linalg.inv(M)).reshape(2, 2)
    ul, ur, lr, ll = quad
    if edge == "left":
        ul, ll = inner_orig[0], inner_orig[1]
    else:
        ur, lr = inner_orig[0], inner_orig[1]
    return np.array([ul, ur, lr, ll], dtype=np.float32), True


def refine_inner_edge(img, quad, side, out_w, out_h):
    """Trim the spine side to the fold crease. side None (single-page shot
    with no detected spine): leave the quad alone."""
    if side == "right":
        return trim_edge_to_crease(img, quad, "left", out_w, out_h)
    if side == "left":
        return trim_edge_to_crease(img, quad, "right", out_w, out_h)
    return quad, False


# ---------------------------------------------------------------------------
# Dewarp (page curl)
# ---------------------------------------------------------------------------

def _edge_curves(mask):
    """Per-column top/bottom paper edge of a warped page mask, or (None, None)
    where a column doesn't show both edges near the frame edges."""
    h, w = mask.shape
    top, bot = top_bottom_profiles(mask)
    valid = (top >= 0) & (top < 0.12 * h) & (bot > 0.88 * h)
    return top.astype(np.float64), bot.astype(np.float64), valid


def _robust_curve(xs, ys, deg=3):
    """Polynomial fit with one round of outlier rejection."""
    coef = np.polyfit(xs, ys, deg)
    resid = ys - np.polyval(coef, xs)
    sd = resid.std() or 1.0
    keep = np.abs(resid) < 2.5 * sd
    if keep.sum() >= deg + 5:
        coef = np.polyfit(xs[keep], ys[keep], deg)
    return coef


def dewarp_curl(page, page_mask, min_dev_frac=0.003, max_dev_frac=0.06, min_valid=0.6):
    """Straighten page curl along the top/bottom edges.

    `page` and `page_mask` are already warped to output geometry. The page's
    top and bottom edges should be straight lines; where they bow (the page
    bending into the gutter), each column is shifted vertically so both
    edges become their chords. Only the curvature is removed — the framing
    the quad chose is kept. Returns (page, page_mask, info)."""
    h, w = page_mask.shape
    top, bot, valid = _edge_curves(page_mask)
    if valid.mean() < min_valid:
        return page, page_mask, {"dewarp": "skipped: page edges not visible"}
    xs = np.nonzero(valid)[0].astype(np.float64)
    all_x = np.arange(w, dtype=np.float64)
    devs = []
    for prof in (top, bot):
        coef = _robust_curve(xs, prof[valid])
        curve = np.polyval(coef, all_x)
        chord = curve[0] + (curve[-1] - curve[0]) * all_x / (w - 1)
        devs.append(curve - chord)
    dev_t, dev_b = devs
    peak = float(max(np.abs(dev_t).max(), np.abs(dev_b).max()))
    if peak < min_dev_frac * h:
        return page, page_mask, {"dewarp": "not needed", "curl_px": round(peak, 1)}
    if peak > max_dev_frac * h:
        return page, page_mask, {"dewarp": f"skipped: implausible curl {peak:.0f}px"}
    yy = np.arange(h, dtype=np.float32)[:, None] / (h - 1)
    map_y = (np.arange(h, dtype=np.float32)[:, None]
             + (dev_t[None, :] * (1 - yy) + dev_b[None, :] * yy)).astype(np.float32)
    map_x = np.repeat(np.arange(w, dtype=np.float32)[None, :], h, axis=0)
    out = cv2.remap(page, map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    out_mask = cv2.remap(page_mask, map_x, map_y, cv2.INTER_NEAREST,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return out, out_mask, {"dewarp": "applied", "curl_px": round(peak, 1)}


# ---------------------------------------------------------------------------
# Tone
# ---------------------------------------------------------------------------

def flatten_illumination(gray):
    bg = cv2.GaussianBlur(gray, (0, 0), 60)
    norm = cv2.divide(gray, bg, scale=255)
    lo, hi = np.percentile(norm, (0.5, 99.9))
    if hi <= lo:
        return norm
    out = np.clip((norm.astype(np.float32) - lo) * 255.0 / (hi - lo), 0, 255)
    return out.astype(np.uint8)


def suppress_bleed(gray, strength: float = 1.0):
    """Push faint marks (show-through from the other side of the page) to
    white while keeping ink and the printed dot grid.

    Works on a flattened page (paper ~255). Pixels lighter than the white
    point are clipped to white; darker ones are stretched so ink keeps its
    contrast. The white point sits between the paper level and the darkest
    marks' level and moves toward the ink as `strength` grows (0 = off)."""
    if strength <= 0:
        return gray
    g = gray.astype(np.float32)
    paper = float(np.percentile(g, 60))
    ink = float(np.percentile(g, 0.5))
    white = paper - min(0.9, 0.18 * strength) * (paper - ink)
    lo = ink
    if white <= lo + 10:
        return gray
    out = np.clip((g - lo) * 255.0 / (white - lo), 0, 255)
    return out.astype(np.uint8)


def fit_full_frame(img, out_w, out_h, keep_aspect):
    """Map an already-cropped page scan onto the exact output geometry.
    Stretch by default (corrects a scanner's aspect error); with keep_aspect,
    fit inside and centre on a white canvas."""
    if not keep_aspect:
        return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]
    s = min(out_w / w, out_h / h)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
    fitted = cv2.resize(img, (nw, nh), interpolation=interp)
    canvas = np.full((out_h, out_w) + img.shape[2:], 255, np.uint8)
    x0, y0 = (out_w - nw) // 2, (out_h - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = fitted
    return canvas


# ---------------------------------------------------------------------------
# Object (cover) detection — any colour, not just bright paper
# ---------------------------------------------------------------------------

OBJECT_SCALE_PX = 640


def _mask_grabcut(small):
    """Foreground from GrabCut seeded with a centred box (the subject is
    assumed not to touch the outer 4% of the frame)."""
    h, w = small.shape[:2]
    m = max(4, int(0.04 * min(h, w)))
    mask = np.zeros((h, w), np.uint8)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(small, mask, (m, m, w - 2 * m, h - 2 * m), bgd, fgd, 4, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None
    return np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)


def _mask_center_cluster(small, k=3):
    """Colour clusters (Lab k-means); keep the cluster owning the centre."""
    h, w = small.shape[:2]
    lab = cv2.cvtColor(cv2.GaussianBlur(small, (0, 0), 2), cv2.COLOR_BGR2LAB)
    data = lab.reshape(-1, 3).astype(np.float32)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    labels0 = np.zeros((data.shape[0], 1), np.int32)
    _, labels, _ = cv2.kmeans(data, k, labels0, crit, 2, cv2.KMEANS_PP_CENTERS)
    labels = labels.reshape(h, w)
    c = labels[int(h * .35):int(h * .65), int(w * .35):int(w * .65)]
    centre = int(np.bincount(c.ravel(), minlength=k).argmax())
    return np.where(labels == centre, 255, 0).astype(np.uint8)


def _mask_edges(small):
    """Region enclosed by strong edges, filled."""
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 7, 40, 7)
    med = float(np.median(gray))
    edges = cv2.Canny(gray, int(max(0, 0.66 * med)), int(min(255, 1.33 * med)))
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mask = np.zeros_like(gray)
    for c in contours:
        cv2.drawContours(mask, [cv2.convexHull(c)], -1, 255, cv2.FILLED)
    return mask


def _score_object_mask(mask, page_aspect):
    """Best quad in a candidate mask and how plausible it is as the cover.
    Returns (score, quad, detail) or None."""
    h, w = mask.shape
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(c)
    frac = area / float(h * w)
    if not 0.08 <= frac <= 0.97:
        return None
    filled = np.zeros_like(mask)
    cv2.drawContours(filled, [c], -1, 255, cv2.FILLED)
    res = quad_from_mask(filled)
    if res is None:
        return None
    quad, fallback = res
    q_area = cv2.contourArea(quad.astype(np.float32))
    rect_fit = min(area, q_area) / max(area, q_area)
    ul, ur, lr, ll = quad
    qw = (np.linalg.norm(ur - ul) + np.linalg.norm(lr - ll)) / 2
    qh = (np.linalg.norm(ll - ul) + np.linalg.norm(lr - ur)) / 2
    a = qw / max(qh, 1e-6)
    # portrait or sideways both acceptable; the cover may overhang the pages
    adiff = min(abs(math.log(a / page_aspect)), abs(math.log(a * page_aspect)))
    aspect_score = math.exp(-adiff / 0.2)
    # touching the frame border usually means a wrong (background) region
    border = 0.9 if (quad[:, 0].min() < 2 or quad[:, 1].min() < 2 or
                     quad[:, 0].max() > w - 3 or quad[:, 1].max() > h - 3) else 1.0
    score = rect_fit ** 3 * aspect_score * border * (0.8 if fallback else 1.0)
    return score, quad, {"rect_fit": round(rect_fit, 3), "aspect": round(a, 3),
                         "area": round(frac, 3)}


def detect_object_quad(img, page_aspect: float, min_score: float = 0.45):
    """Quad of the notebook (e.g. a dark leather cover) as an object of any
    colour — for covers, where the paper detector finds nothing or only a
    pale label. Tries GrabCut, colour clustering and edges; keeps the most
    rectangular, page-shaped result. Returns (quad, info) in full-res
    coordinates, or None."""
    h, w = img.shape[:2]
    scale = float(OBJECT_SCALE_PX) / max(h, w)
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    best = None
    for name, fn in (("grabcut", _mask_grabcut), ("colour", _mask_center_cluster),
                     ("edges", _mask_edges)):
        mask = fn(small)
        if mask is None:
            continue
        sc = _score_object_mask(mask, page_aspect)
        if sc and (best is None or sc[0] > best[0]):
            best = (sc[0], sc[1], {**sc[2], "method": name})
    if best is None or best[0] < min_score:
        return None
    score, quad, detail = best
    quad = order_corners(quad / scale)
    # a sideways cover: start the corner order one step later so the warp
    # turns it upright (90 degrees; the reviewer may still need a flip)
    ul, ur, lr, ll = quad
    qw = (np.linalg.norm(ur - ul) + np.linalg.norm(lr - ll)) / 2
    qh = (np.linalg.norm(ll - ul) + np.linalg.norm(lr - ur)) / 2
    if (qw > qh) != (page_aspect > 1):
        quad = np.roll(quad, 1, axis=0)
        detail["sideways"] = True
    detail["score"] = round(score, 3)
    return quad.astype(np.float32), detail


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------

def edge_ink(gray, pmask, frac=0.012, dark=110):
    """For each edge of a cleaned page: the share of the outermost strip that
    is paper, and the share of that paper that is ink. Paper reaching the
    edge means the crop cut into the page; ink there means content may be
    clipped. Background margin (outside the paper mask) is ignored."""
    h, w = gray.shape
    t = max(2, int(min(h, w) * frac))
    sl = {"top": (slice(0, t), slice(None)), "bottom": (slice(h - t, h), slice(None)),
          "left": (slice(None), slice(0, t)), "right": (slice(None), slice(w - t, w))}
    out = {}
    for k, s in sl.items():
        paper = pmask[s] > 0
        pf = float(paper.mean())
        ink = float((gray[s][paper] < dark).mean()) if paper.any() else 0.0
        out[k] = (round(pf, 3), round(ink, 4))
    return out


def quality_flags(gray, pmask, info, side):
    """Human-readable warnings for the reviewer."""
    flags = []
    if info.get("quad_fallback"):
        flags.append("page outline was fitted as a plain rectangle; check the corners")
    if info.get("mode") == "object":
        if info.get("score", 1) < 0.65:
            flags.append(f"cover outline is uncertain (score {info['score']}); check the corners")
        if info.get("sideways"):
            flags.append("cover was sideways and turned 90 degrees; it may need a flip")
    if info.get("spine") == "centre-cut":
        flags.append("no fold found; spread was split at the centre")
    inner = {"left": "right", "right": "left"}.get(side or "")
    if pmask is not None:
        for edge, (paper, ink) in edge_ink(gray, pmask).items():
            if edge != inner and paper > 0.5 and ink > 0.02:
                flags.append(f"writing reaches the {edge} edge; content may be clipped")
    if str(info.get("dewarp", "")).startswith("skipped: implausible"):
        flags.append("page curl looked implausible and was not corrected")
    return flags


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------

@dataclass
class PageParams:
    """Everything needed to reproduce one page from its original."""
    rotate: int = 0                 # clockwise, applied to the original first
    half: str | None = None         # "left" / "right" / None
    two_blob: bool = False
    seed: list | None = None        # [x, y] after rotation
    corners: list | None = None     # [[x, y] x4] after rotation (manual quad)
    full_frame: bool = False
    keep_aspect: bool = False
    color: bool = False
    dewarp: bool = True
    bleed: float = 1.0               # show-through suppression (0 = off)
    flip: bool = False              # rotate the finished page 180
    object: bool = False            # detect the notebook as an object of any colour (covers)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PageParams":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        """Only non-default values, so manifests stay readable."""
        default = PageParams()
        return {k: getattr(self, k) for k in self.__dataclass_fields__
                if getattr(self, k) != getattr(default, k)}


def process_page(img: np.ndarray, params: PageParams, out_w: int, out_h: int):
    """Produce one finished page from an original image.
    Returns (page, info) — page is gray uint8 (BGR when params.color);
    info carries the quad, steps taken and reviewer flags.
    Raises ValueError when no page can be found."""
    img = rotate(img, params.rotate)
    info: dict = {}
    side = None
    pmask = None

    if params.full_frame:
        if params.half:
            # a scan of two facing pages: take one half of the frame
            mid = img.shape[1] // 2
            img = img[:, :mid] if params.half == "left" else img[:, mid:]
        page = fit_full_frame(img, out_w, out_h, params.keep_aspect)
        info["mode"] = "full-frame" + (" keep-aspect" if params.keep_aspect else "")
    else:
        if params.corners:
            quad = order_corners(params.corners)
            info["mode"] = "manual corners"
        elif params.object:
            res = detect_object_quad(img, out_w / out_h)
            if res is None:
                raise ValueError("couldn't find the notebook in the photo; pass manual corners")
            quad, det = res
            info.update(det)
            info["mode"] = "object"
            centroid = quad.mean(axis=0)
            quad = centroid + (quad - centroid) * 1.01
        else:
            res = detect_page_quad(img, half=params.half, seed=params.seed,
                                   two_blob=params.two_blob, page_aspect=out_w / out_h)
            if res is None:
                raise ValueError("page detection failed; pass manual corners")
            quad, side, det = res
            info.update(det)
            info["mode"] = "detected"
            # small outward pad so content at the very page edge survives
            centroid = quad.mean(axis=0)
            quad = centroid + (quad - centroid) * 1.02
            quad, trimmed = refine_inner_edge(img, quad, side, out_w, out_h)
            info["crease_trim"] = trimmed
        info["quad"] = np.round(quad).astype(int).tolist()
        M = cv2.getPerspectiveTransform(quad.astype(np.float32), _dst_rect(out_w, out_h))
        page = cv2.warpPerspective(img, M, (out_w, out_h), flags=cv2.INTER_CUBIC)

        mask, scale = paper_mask(img)
        Ms = cv2.getPerspectiveTransform((quad * scale).astype(np.float32),
                                         _dst_rect(out_w, out_h))
        pmask = cv2.warpPerspective(mask, Ms, (out_w, out_h), flags=cv2.INTER_NEAREST)
        if params.object:
            pmask = None                    # paper mask means nothing for a cover
        elif params.dewarp and not params.corners:
            page, pmask, dinfo = dewarp_curl(page, pmask)
            info.update(dinfo)

    if params.color:
        out = page
        gray_for_checks = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
    else:
        out = flatten_illumination(cv2.cvtColor(page, cv2.COLOR_BGR2GRAY))
        if params.bleed:
            out = suppress_bleed(out, params.bleed)
        gray_for_checks = out
    if params.flip:
        out = rotate(out, 180)
    info["side"] = side
    info["flags"] = [] if params.full_frame else quality_flags(gray_for_checks, pmask, info, side)
    return out, info
