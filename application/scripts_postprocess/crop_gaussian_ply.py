#!/usr/bin/env python3
"""
Crop a 3D Gaussian Splatting point_cloud.ply (binary or ascii) by position.

Typical uses:
  • Remove ceiling: drop Gaussians above a horizontal plane (e.g. --max-y …).
  • Remove outdoor floaters: keep only an axis-aligned box (--min/max x z or all axes).
  • Tighter footprint: --footprint-xz with polygon vertices in the x–z plane (y is vertical).

Coordinates are exactly those in the PLY (same as COLMAP / 3DGS training space).

Examples:
  python scripts/crop_gaussian_ply.py in.ply out.ply --list-bounds
  python scripts/crop_gaussian_ply.py in.ply out.ply --max-y 2.8 --dry-run
  python scripts/crop_gaussian_ply.py in.ply out.ply \\
      --min-x -4 --max-x 5 --min-z -2 --max-z 6 --max-y 3.0
  python scripts/crop_gaussian_ply.py in.ply out.ply \\
      --footprint-xz room.txt --max-y 3.0

footprint.txt: one "x z" pair per line (# comments allowed). Winding order does not matter.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
from plyfile import PlyData, PlyElement


def _load_footprint_xz(path: Path) -> tuple[np.ndarray, np.ndarray]:
    xs: list[float] = []
    zs: list[float] = []
    for line in path.read_text().splitlines():
        line = re.sub(r"#.*$", "", line).strip()
        if not line:
            continue
        parts = re.split(r"[,\s]+", line)
        if len(parts) < 2:
            raise ValueError(f"Bad line in {path!s}: {line!r} (need two numbers: x z)")
        xs.append(float(parts[0]))
        zs.append(float(parts[1]))
    if len(xs) < 3:
        raise ValueError(f"Footprint needs at least 3 vertices, got {len(xs)} in {path}")
    return np.asarray(xs, dtype=np.float64), np.asarray(zs, dtype=np.float64)


def points_in_polygon_xz(x: np.ndarray, z: np.ndarray, px: np.ndarray, pz: np.ndarray) -> np.ndarray:
    """
    Even-odd rule: ray cast along +x from each (x,z). Vectorized over points, loop over edges.
    px,pz: polygon closed implicitly (edge from i to i+1, last to first).
    """
    x = np.asarray(x, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    n = int(px.shape[0])
    if n < 3:
        raise ValueError("polygon must have >= 3 vertices")
    inside = np.zeros(x.shape[0], dtype=bool)
    for i in range(n):
        j = (i + 1) % n
        xi, zi = float(px[i]), float(pz[i])
        xj, zj = float(px[j]), float(pz[j])
        denom = zj - zi
        if denom == 0.0:
            denom = 1e-30
        intersect = ((zi > z) != (zj > z)) & (x < (xj - xi) * (z - zi) / denom + xi)
        inside ^= intersect
    return inside


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_ply", type=Path)
    ap.add_argument("output_ply", type=Path, nargs="?", help="Required unless --list-bounds")
    ap.add_argument("--list-bounds", action="store_true", help="Print axis-aligned min/max and exit")
    ap.add_argument("--dry-run", action="store_true", help="Print keep/remove counts, do not write")
    ap.add_argument("--min-x", type=float, default=None)
    ap.add_argument("--max-x", type=float, default=None)
    ap.add_argument("--min-y", type=float, default=None)
    ap.add_argument("--max-y", type=float, default=None, help="Ceiling clip: remove points with y > this")
    ap.add_argument("--min-z", type=float, default=None)
    ap.add_argument("--max-z", type=float, default=None)
    ap.add_argument(
        "--footprint-xz",
        type=Path,
        default=None,
        help="Text file: polygon in x–z plane; keep (default) or drop via --polygon-outside",
    )
    ap.add_argument(
        "--polygon-outside",
        action="store_true",
        help="With --footprint-xz, KEEP points outside the polygon instead of inside",
    )
    args = ap.parse_args()

    if not args.input_ply.is_file():
        sys.stderr.write(f"Not a file: {args.input_ply}\n")
        sys.exit(1)

    ply = PlyData.read(str(args.input_ply))
    if "vertex" not in ply:
        sys.stderr.write("PLY has no 'vertex' element.\n")
        sys.exit(1)
    v = ply["vertex"]
    data = v.data
    n0 = len(data)
    if n0 == 0:
        sys.stderr.write("Empty PLY.\n")
        sys.exit(1)

    x = np.asarray(data["x"])
    y = np.asarray(data["y"])
    z = np.asarray(data["z"])

    print(f"Read {n0} Gaussians from {args.input_ply}")

    if args.list_bounds:
        print(
            f"x: [{x.min():.6f}, {x.max():.6f}]\n"
            f"y: [{y.min():.6f}, {y.max():.6f}]\n"
            f"z: [{z.min():.6f}, {z.max():.6f}]"
        )
        return

    if args.output_ply is None:
        sys.stderr.write("output_ply required unless --list-bounds\n")
        sys.exit(2)

    mask = np.ones(n0, dtype=bool)
    if args.min_x is not None:
        mask &= x >= args.min_x
    if args.max_x is not None:
        mask &= x <= args.max_x
    if args.min_y is not None:
        mask &= y >= args.min_y
    if args.max_y is not None:
        mask &= y <= args.max_y
    if args.min_z is not None:
        mask &= z >= args.min_z
    if args.max_z is not None:
        mask &= z <= args.max_z

    if args.footprint_xz is not None:
        px, pz = _load_footprint_xz(args.footprint_xz)
        in_poly = points_in_polygon_xz(x, z, px, pz)
        if args.polygon_outside:
            mask &= ~in_poly
        else:
            mask &= in_poly

    kept = int(mask.sum())
    removed = n0 - kept
    print(f"Keep {kept} / {n0}  (remove {removed})")

    if args.dry_run:
        return

    if kept == 0:
        sys.stderr.write("Nothing left after crop; not writing empty PLY.\n")
        sys.exit(3)

    out = data[mask]
    el = PlyElement.describe(out, "vertex")
    args.output_ply.parent.mkdir(parents=True, exist_ok=True)
    PlyData([el]).write(str(args.output_ply))
    print(f"Wrote {args.output_ply}")


if __name__ == "__main__":
    main()
