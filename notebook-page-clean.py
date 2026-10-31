#!/usr/bin/env python3
"""Detect, crop, deskew, and clean notebook pages from phone photos.

Used by the notebook-pages skill. Pipeline:
  1. Segment the paper (bright, unsaturated) and take the largest blob —
     for an open notebook this is usually BOTH facing pages merged.
  2. Find the spine as the dip in the blob's top profile / bump in the
     bottom profile (facing pages meet in a V at the fold). If there is
     no dip, the blob is a single page.
  3. Split the mask at the spine line and fit a quad to the requested
     page's own mask half (so each page gets its own plane — facing
     pages are never coplanar).
  4. Warp that quad straight to the exact page geometry.
  5. Flatten illumination, autocontrast, save with 300 DPI.

Usage:
  notebook-page-clean.py SRC DST [--spread --half left|right]
                         [--rotate 0|90|180|270] [--size WxH]
                         [--seed X,Y]
                         [--corners ULx,ULy URx,URy LRx,LRy LLx,LLy]

  --rotate   applied first (sideways/upside-down photos; verify output).
  --size     output pixels; default 1050x1500 (3.5x5 in @ 300 dpi).
  --seed     a point on the target page (default image center after
             rotation) for single-page shots that include the facing page.
  --corners  manual override when detection fails (coords after rotate).
  --full-frame
             the source is already cropped to the page (e.g. a Scanner Pro
             PDF page split out with pdfimages/pdftoppm): skip detection and
             warp the whole frame to --size, fixing the scanner's aspect.
  --keep-aspect
             with --full-frame: don't stretch; fit inside --size and pad with
             white (for a partial cover scan that isn't page-shaped).
  --color    keep colour and skip illumination flattening (covers).
"""
import argparse
import sys

import cv2
import numpy as np

MASK_SCALE_PX = 1200  # working width for segmentation


def order_corners(pts):
    """Return corners ordered ul, ur, lr, ll."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def paper_mask(img):
    """Binary mask of bright/unsaturated paper at reduced scale.
    Returns (mask, scale). Aggressive opening breaks thin bridges to
    bright background (tables, cloth)."""
    h, w = img.shape[:2]
    scale = float(MASK_SCALE_PX) / max(h, w)
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]
    _, bright = cv2.threshold(val, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.bitwise_and(bright, (sat < 90).astype(np.uint8) * 255)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    # strong open: disconnect the page from bright background it touches
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((15, 15), np.uint8))
    return mask, scale


def largest_blob_mask(mask, min_area_frac=0.10):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    blob = max(contours, key=cv2.contourArea)
    if cv2.contourArea(blob) < min_area_frac * mask.shape[0] * mask.shape[1]:
        return None
    out = np.zeros_like(mask)
    cv2.drawContours(out, [blob], -1, 255, cv2.FILLED)
    return out


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


def side_mask(mask, spine, side):
    """Zero out the mask on the other side of the spine line."""
    h, w = mask.shape
    (xt, yt), (xb, yb) = spine
    # extend the spine segment to full image height
    if yb == yt:
        x_top_ext, x_bot_ext = xt, xb
    else:
        slope = (xb - xt) / float(yb - yt)
        x_top_ext = xt + slope * (0 - yt)
        x_bot_ext = xb + slope * (h - 1 - yb)
    out = mask.copy()
    if side == "left":
        poly = np.array([[x_top_ext, 0], [w - 1, 0], [w - 1, h - 1], [x_bot_ext, h - 1]])
    else:
        poly = np.array([[0, 0], [x_top_ext, 0], [x_bot_ext, h - 1], [0, h - 1]])
    cv2.fillPoly(out, [poly.astype(np.int32)], 0)
    return out


def quad_from_mask(mask):
    """Fit a quad to the largest contour of a single-page mask."""
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
    for eps in (0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(c, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            best = approx.reshape(4, 2).astype(np.float32)
            break
    if best is None:
        best = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
    # sanity: the quad must actually hug the contour
    q_area = cv2.contourArea(best.astype(np.int32))
    if q_area <= 0 or not (0.75 <= q_area / c_area <= 1.35):
        best = cv2.boxPoints(cv2.minAreaRect(c)).astype(np.float32)
        q_area = cv2.contourArea(best.astype(np.int32))
        if q_area <= 0 or not (0.75 <= q_area / c_area <= 1.45):
            return None
    return order_corners(best)


def detect_page_quad(img, spread, half, seed):
    """Full-res quad of the requested page, or None."""
    mask, scale = paper_mask(img)
    blob = largest_blob_mask(mask)
    if blob is None:
        return None
    side = None
    spine = find_spine(blob)
    if spine is None:
        if spread:
            # no dip found; cut at the blob's horizontal middle
            top, _ = top_bottom_profiles(blob)
            valid = np.where(top >= 0)[0]
            cx = int((valid[0] + valid[-1]) / 2)
            spine = ((cx, 0), (cx, blob.shape[0] - 1))
            side = half
            target = blob if half is None else side_mask(blob, spine, half)
        else:
            target = blob  # single page, whole blob
    else:
        if spread:
            side = half
        else:
            sx, sy = seed
            sxs, sys_ = sx * scale, sy * scale
            (xt, yt), (xb, yb) = spine
            # which side of the spine line is the seed on?
            cross = (xb - xt) * (sys_ - yt) - (yb - yt) * (sxs - xt)
            # image y points down, so cross < 0 means the seed is right of
            # a top→bottom spine
            side = "right" if cross < 0 else "left"
        target = side_mask(blob, spine, side)
    quad = quad_from_mask(target)
    if quad is None:
        return None
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
    return quad / scale, side


def trim_edge_to_crease(img, quad, edge, out_w, out_h):
    """If the fold crease (dark groove, continuous top to bottom) sits inside
    the given OUTPUT edge's band, pull that edge in to it. Trim only — a clean
    page edge has no groove and is left untouched."""
    dst_rect = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
                        dtype=np.float32)
    M = cv2.getPerspectiveTransform(quad, dst_rect)
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
        return quad                             # no clear crease: keep as-is
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
    return np.array([ul, ur, lr, ll], dtype=np.float32)


def refine_inner_edge(img, quad, side, out_w, out_h):
    """Trim the spine side to the fold crease. side None (single-page shot
    with no detected spine): leave the quad alone — the facing page's curl
    makes edge statistics unreliable there; use --corners if it matters."""
    if side == "right":
        return trim_edge_to_crease(img, quad, "left", out_w, out_h)
    if side == "left":
        return trim_edge_to_crease(img, quad, "right", out_w, out_h)
    return quad


def flatten_illumination(gray):
    bg = cv2.GaussianBlur(gray, (0, 0), 60)
    norm = cv2.divide(gray, bg, scale=255)
    lo, hi = np.percentile(norm, (0.5, 99.9))
    if hi <= lo:
        return norm
    out = np.clip((norm.astype(np.float32) - lo) * 255.0 / (hi - lo), 0, 255)
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
    canvas = np.full((out_h, out_w, 3), 255, np.uint8)
    x0, y0 = (out_w - nw) // 2, (out_h - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = fitted
    return canvas


def save_with_dpi(img, path, dpi=300):
    ok = cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        sys.exit(f"failed to write {path}")
    from PIL import Image
    im = Image.open(path)
    im.save(path, dpi=(dpi, dpi), quality=88)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--spread", action="store_true")
    ap.add_argument("--half", choices=["left", "right"])
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    ap.add_argument("--size", default="1050x1500")
    ap.add_argument("--seed")
    ap.add_argument("--corners", nargs=4, metavar="X,Y")
    ap.add_argument("--full-frame", action="store_true")
    ap.add_argument("--keep-aspect", action="store_true")
    ap.add_argument("--color", action="store_true")
    args = ap.parse_args()
    if args.spread and not args.half:
        sys.exit("--spread requires --half left|right")
    if args.full_frame and (args.spread or args.corners or args.seed):
        sys.exit("--full-frame can't be combined with --spread/--corners/--seed")
    if args.keep_aspect and not args.full_frame:
        sys.exit("--keep-aspect requires --full-frame")

    out_w, out_h = (int(v) for v in args.size.split("x"))

    from PIL import Image, ImageOps
    pil = ImageOps.exif_transpose(Image.open(args.src)).convert("RGB")
    img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    if args.rotate:
        rot = {90: cv2.ROTATE_90_COUNTERCLOCKWISE,
               180: cv2.ROTATE_180,
               270: cv2.ROTATE_90_CLOCKWISE}[args.rotate]
        img = cv2.rotate(img, rot)

    if args.full_frame:
        page = fit_full_frame(img, out_w, out_h, args.keep_aspect)
        out = page if args.color else flatten_illumination(cv2.cvtColor(page, cv2.COLOR_BGR2GRAY))
        save_with_dpi(out, args.dst)
        print(f"saved {args.dst} {out.shape[1]}x{out.shape[0]} full-frame"
              f"{' keep-aspect' if args.keep_aspect else ''}")
        return

    side = None
    if args.corners:
        quad = order_corners([tuple(int(v) for v in c.split(",")) for c in args.corners])
    else:
        if args.seed:
            seed = tuple(int(v) for v in args.seed.split(","))
        else:
            seed = (img.shape[1] // 2, img.shape[0] // 2)
        res = detect_page_quad(img, args.spread, args.half, seed)
        if res is None:
            sys.exit("page detection failed — pass --corners")
        quad, side = res

    # small outward pad so content at the very page edge survives the crop
    centroid = quad.mean(axis=0)
    quad = centroid + (quad - centroid) * 1.02
    if not args.corners:
        quad = refine_inner_edge(img, quad, side, out_w, out_h)

    dst_rect = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
                        dtype=np.float32)
    Mf = cv2.getPerspectiveTransform(quad, dst_rect)
    page = cv2.warpPerspective(img, Mf, (out_w, out_h), flags=cv2.INTER_CUBIC)

    out = page if args.color else flatten_illumination(cv2.cvtColor(page, cv2.COLOR_BGR2GRAY))
    save_with_dpi(out, args.dst)
    print(f"saved {args.dst} {out.shape[1]}x{out.shape[0]} quad={quad.astype(int).tolist()}")


if __name__ == "__main__":
    main()
