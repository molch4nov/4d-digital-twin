#!/usr/bin/env python3
"""
Build a grayscale PNG for COLMAP --ImageReader.camera_mask_path:
  255 = use pixels for feature extraction
  0   = masked (ignored)

For equirectangular 360° the rig / helmet / tripod sits at the nadir (bottom of
the panorama). Mask that region so SIFT does not lock onto the operator and
create floaters / fog inside the reconstruction.

Modes:
  strip   — full-width band along the bottom (recommended; covers rig corners)
  ellipse — centered ellipse (legacy; often misses side rig parts)
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def build_mask(w: int, h: int, mode: str, bottom_frac: float, width_frac: float) -> Image.Image:
    mask = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(mask)
    eh = max(8, int(h * bottom_frac))
    y0 = max(0, h - eh)
    y1 = h

    if mode == "strip":
        draw.rectangle([0, y0, w, y1], fill=0)
    elif mode == "ellipse":
        ew = max(8, int(w * width_frac))
        cx = w // 2
        x0 = cx - ew // 2
        x1 = cx + ew // 2
        draw.ellipse([x0, y0, x1, y1], fill=0)
    else:
        raise ValueError(f"unknown mode: {mode}")
    return mask


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--ref-image",
        required=True,
        type=Path,
        help="Any frame from the video (defines width/height for the mask).",
    )
    ap.add_argument("-o", "--output", required=True, type=Path)
    ap.add_argument(
        "--mode",
        choices=("strip", "ellipse"),
        default="strip",
        help="strip = full-width bottom band; ellipse = centered oval (legacy).",
    )
    ap.add_argument(
        "--bottom-frac",
        type=float,
        default=0.28,
        help="Height of masked region as fraction of image height (from bottom).",
    )
    ap.add_argument(
        "--width-frac",
        type=float,
        default=0.58,
        help="[ellipse only] Horizontal extent as fraction of image width.",
    )
    ap.add_argument(
        "--preview",
        type=Path,
        default=None,
        help="Optional RGB image path: reference frame with masked region tinted red.",
    )
    args = ap.parse_args()

    if not (0 < args.bottom_frac < 1):
        raise SystemExit("--bottom-frac must be between 0 and 1")
    if args.mode == "ellipse" and not (0 < args.width_frac < 1):
        raise SystemExit("--width-frac must be between 0 and 1")

    ref = Image.open(args.ref_image)
    w, h = ref.size
    mask = build_mask(w, h, args.mode, args.bottom_frac, args.width_frac)

    if args.preview is not None:
        base = ref.convert("RGB")
        tint = Image.new("RGB", (w, h), (235, 65, 55))
        inv = mask.point(lambda p: 255 if p < 128 else 0)
        blended = Image.composite(tint, base, inv)
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        blended.save(args.preview, quality=92)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    mask.save(args.output)
    print(f"mask: mode={args.mode} size={w}x{h} bottom_frac={args.bottom_frac} -> {args.output}")


if __name__ == "__main__":
    main()
