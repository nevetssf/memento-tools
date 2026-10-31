#!/usr/bin/env python3
"""Detect, crop, deskew, and clean one notebook page from a photo or scan.

Low-level CLI over notebook_image.py. For filing pages into a notebook use
notebook.py (or the notebook MCP server), which also keeps originals and
the PDF in sync.

Usage:
  notebook-page-clean.py SRC DST [--spread --half left|right]
                         [--rotate 0|90|180|270] [--size WxH]
                         [--seed X,Y]
                         [--corners ULx,ULy URx,URy LRx,LRy LLx,LLy]
                         [--full-frame [--keep-aspect]] [--color]
                         [--no-dewarp] [--bleed N] [--flip]
                         [--auto]

  --rotate   clockwise, applied first (sideways photos; verify output).
  --size     output pixels; default 1050x1500 (3.5x5 in @ 300 dpi).
  --spread --half left|right
             take that page of a two-page spread.
  --seed     a point on the target page (default image centre) for
             single-page shots that include part of the facing page.
  --corners  manual quad when detection fails (coords after rotate).
  --full-frame
             the source is already cropped to the page (Scanner Pro PDF
             page): skip detection, warp the whole frame to --size.
  --keep-aspect
             with --full-frame: fit inside --size and pad white instead of
             stretching (partial cover scans).
  --color    keep colour; skip illumination flattening (covers).
  --no-dewarp
             don't straighten page curl.
  --bleed N  show-through suppression strength (default 1; 0 = off).
  --flip     rotate the finished page 180 degrees.
  --auto     classify the photo (single/spread/sideways) and write every
             page found as DST-<n>.jpg; prints what it decided.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import notebook_image as ni  # noqa: E402


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
    ap.add_argument("--no-dewarp", action="store_true")
    ap.add_argument("--bleed", type=float, default=1.0)
    ap.add_argument("--flip", action="store_true")
    ap.add_argument("--auto", action="store_true")
    args = ap.parse_args()
    if args.spread and not args.half:
        sys.exit("--spread requires --half left|right")
    if args.full_frame and (args.spread or args.corners or args.seed):
        sys.exit("--full-frame can't be combined with --spread/--corners/--seed")
    if args.keep_aspect and not args.full_frame:
        sys.exit("--keep-aspect requires --full-frame")

    out_w, out_h = (int(v) for v in args.size.split("x"))
    img = ni.load_image(args.src)
    base = ni.PageParams(
        rotate=args.rotate,
        half=args.half,
        seed=[int(v) for v in args.seed.split(",")] if args.seed else None,
        corners=[[int(v) for v in c.split(",")] for c in args.corners] if args.corners else None,
        full_frame=args.full_frame, keep_aspect=args.keep_aspect, color=args.color,
        dewarp=not args.no_dewarp, bleed=args.bleed, flip=args.flip,
    )

    if args.auto:
        lay = ni.classify_layout(ni.rotate(img, args.rotate), out_w / out_h)
        print(json.dumps({"layout": lay.kind, "rotate": (args.rotate + lay.rotate) % 360,
                          "halves": lay.halves, "reason": lay.reason}))
        dst = Path(args.dst)
        for n, half in enumerate(lay.halves, 1):
            p = ni.PageParams.from_dict({**base.to_dict(), "rotate": (args.rotate + lay.rotate) % 360,
                                         "half": half, "two_blob": lay.two_blob})
            page, info = ni.process_page(img, p, out_w, out_h)
            path = dst.with_name(f"{dst.stem}-{n}{dst.suffix or '.jpg'}")
            ni.save_image(page, path)
            print(json.dumps({"saved": str(path), "half": half, "flags": info["flags"]}))
        return

    try:
        page, info = ni.process_page(img, base, out_w, out_h)
    except ValueError as e:
        sys.exit(f"{e} — pass --corners")
    ni.save_image(page, args.dst)
    extra = f" quad={info['quad']}" if "quad" in info else f" {info.get('mode', '')}"
    print(f"saved {args.dst} {page.shape[1]}x{page.shape[0]}{extra}")
    for f in info["flags"]:
        print(f"warning: {f}", file=sys.stderr)


if __name__ == "__main__":
    main()
