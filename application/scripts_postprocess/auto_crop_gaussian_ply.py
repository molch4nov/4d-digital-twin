#!/usr/bin/env python3
"""
Fully automatic cropping for 3DGS point_cloud.ply — no hand-drawn polygons.

Two strategies (pick with --mode):

1) percentile  —  Clip x/z by percentiles. On y (COLMAP: y-down, roof = low y):
                  --p-low-y cuts ceiling, --p-high-y loosely limits floor (default 98.5%).

2) cameras     —  Load registered camera centers from a COLMAP scene
                  (sparse/0/images.bin or images.txt). Build a 2D convex
                  hull in the x–z plane around the trajectory, expand it by
                  --margin, and keep points inside + a vertical band around
                  camera heights. Follows an L-shaped path better than a box
                  only when you pass a decent --margin.

Surface snap (--mode cameras only) — auto-detects floor / ceiling from the
Gaussian density histogram, so you don't have to guess y offsets:

  --floor-snap         Detect floor drop-off; clip sub-floor junk without
                       going deeper than the actual floor surface.
                       Tune with --floor-snap-margin (default 0.15 m) and
                       --floor-snap-search (default 2.5 m).

  --ceil-snap          Detect ceiling density peak; fully remove the ceiling.
                       Tune with --ceil-snap-margin (default 0.10 m) and
                       --ceil-snap-search (default 2.5 m).

  --xz-percentile 99   After hull crop, clip x and z to the central P%% of
                       surviving splats to remove scattered side floaters.

  --interior           All-in-one preset for indoor scans: enables
                       --floor-snap, --ceil-snap, --xz-percentile 99.0,
                       min-opacity 0.05, max-scale-percentile 99.0, auto
                       voxel-defog, cameras-max-dist 15 m.

Quick indoor example (recommended):
  python scripts/auto_crop_gaussian_ply.py in.ply out.ply --mode cameras --interior

Manual example:
  python scripts/auto_crop_gaussian_ply.py in.ply out.ply --mode cameras \\
      --scene /path/to/colmap_scene_root \\
      --margin 1.75 --floor-snap --ceil-snap --xz-percentile 99.0 --defog --dry-run

--scene is the folder that contains sparse/0/ (same as train.py -s).

If training produced cameras.json next to point_cloud/, it is found automatically.
You can also pass it explicitly:
  --cameras-json /path/to/output/cameras.json  (3DGS format: \"position\": [x,y,z])

Internal fog / floaters (after crop, needs opacity + scale_* in PLY):
  --defog              Preset: low opacity, huge scales, sparse voxels
  --min-opacity 0.08   Drop Gaussians with sigmoid(opacity) below threshold
  --max-scale 0.12     Drop if max(exp(scale_i)) above limit (metres)
  --max-scale-percentile 99.5
  --defog-voxel 0.04 --defog-voxel-min-count 3
  --defog-cameras-max-dist 12   With --scene / cameras.json: drop faint
                                 splats far from all camera centers
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from plyfile import PlyData, PlyElement

# Repo root = parent of scripts/
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _cross(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))


def convex_hull_2d(points: np.ndarray) -> np.ndarray:
    """Monotone chain; points is (N,2). Returns hull vertices in CCW order without duplicate closing point."""
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) <= 1:
        return pts
    pts = pts[np.lexsort((pts[:, 1], pts[:, 0]))]
    lower: list[np.ndarray] = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[np.ndarray] = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return np.stack(hull, axis=0)


def dilate_polygon_radial(poly: np.ndarray, margin: float) -> tuple[np.ndarray, np.ndarray]:
    """Expand each vertex away from centroid by margin (2D)."""
    if margin <= 0:
        return poly[:, 0], poly[:, 1]
    c = poly.mean(axis=0)
    out = poly.copy()
    for i in range(len(out)):
        d = out[i] - c
        n = np.linalg.norm(d)
        if n < 1e-9:
            continue
        out[i] = out[i] + margin * d / n
    return out[:, 0], out[:, 1]


def points_in_polygon_xz(x: np.ndarray, z: np.ndarray, px: np.ndarray, pz: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    n = int(px.shape[0])
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


def colmap_camera_centers(scene_root: Path) -> np.ndarray:
    from scene.colmap_loader import read_extrinsics_binary, read_extrinsics_text, qvec2rotmat

    sparse = scene_root / "sparse" / "0"
    bin_path = sparse / "images.bin"
    txt_path = sparse / "images.txt"
    if bin_path.is_file():
        images = read_extrinsics_binary(str(bin_path))
    elif txt_path.is_file():
        images = read_extrinsics_text(str(txt_path))
    else:
        raise FileNotFoundError(f"No {bin_path} or {txt_path}")

    centers = []
    for im in images.values():
        R = qvec2rotmat(im.qvec)
        t = im.tvec.reshape(3)
        c = -R.T @ t
        centers.append(c)
    return np.stack(centers, axis=0)


def json_camera_centers(path: Path) -> np.ndarray:
    data = json.loads(path.read_text())
    pos = []
    for entry in data:
        if "position" in entry:
            pos.append(entry["position"])
    if len(pos) == 0:
        raise ValueError(f"No 'position' fields in {path}")
    return np.asarray(pos, dtype=np.float64)


def _sigmoid_opacity(logit: np.ndarray) -> np.ndarray:
    logit = np.asarray(logit, dtype=np.float64)
    return 1.0 / (1.0 + np.exp(-np.clip(logit, -30.0, 30.0)))


def _max_axis_scales(data) -> np.ndarray | None:
    names = data.dtype.names
    if names is None or not all(f"scale_{i}" in names for i in range(3)):
        return None
    scales = np.exp(np.stack([np.asarray(data[f"scale_{i}"], dtype=np.float64) for i in range(3)], axis=1))
    return scales.max(axis=1)


def _opacities_from_data(data) -> np.ndarray | None:
    if data.dtype.names is None or "opacity" not in data.dtype.names:
        return None
    return _sigmoid_opacity(np.asarray(data["opacity"], dtype=np.float64))


def _auto_voxel_size(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> float:
    """Heuristic cell size from scene extent (metres)."""
    pts = np.column_stack([x, y, z])
    extent = float(np.max(pts.max(axis=0) - pts.min(axis=0)))
    return max(extent / 120.0, 0.02)


def _voxel_keys(x: np.ndarray, y: np.ndarray, z: np.ndarray, voxel: float) -> tuple[np.ndarray, np.ndarray]:
    origin = np.array([x.min(), y.min(), z.min()], dtype=np.float64)
    vox = np.floor(np.column_stack([x, y, z]) - origin) / voxel
    vox = vox.astype(np.int64)
    # Pack 3D indices into one key (scene fits in ~few km at 2cm voxels).
    key = vox[:, 0] + vox[:, 1] * 1_000_003 + vox[:, 2] * 1_000_003 * 1_000_003
    return key, origin


def filter_internal_fog(
    data,
    *,
    min_opacity: float | None = None,
    max_scale: float | None = None,
    max_scale_percentile: float | None = None,
    voxel_size: float | None = None,
    voxel_min_count: int = 3,
    voxel_max_opacity: float = 0.22,
    use_voxel: bool = True,
    camera_centers: np.ndarray | None = None,
    cameras_max_dist: float | None = None,
    cameras_max_opacity: float = 0.35,
) -> tuple[np.ndarray, list[str]]:
    """
    Return boolean keep-mask (length N) and log lines for each filter stage.
    Applied only to the current subset (e.g. after spatial crop).
    """
    n = len(data)
    keep = np.ones(n, dtype=bool)
    logs: list[str] = []

    op = _opacities_from_data(data)
    scales = _max_axis_scales(data)
    x = np.asarray(data["x"], dtype=np.float64)
    y = np.asarray(data["y"], dtype=np.float64)
    z = np.asarray(data["z"], dtype=np.float64)

    if min_opacity is not None:
        if op is None:
            logs.append("  [fog] min-opacity: skip (no opacity field)")
        else:
            m = op >= min_opacity
            removed = int((keep & ~m).sum())
            keep &= m
            logs.append(f"  [fog] min-opacity>={min_opacity:.4f}: remove {removed}")

    if max_scale is not None:
        if scales is None:
            logs.append("  [fog] max-scale: skip (no scale_* fields)")
        else:
            m = scales <= max_scale
            removed = int((keep & ~m).sum())
            keep &= m
            logs.append(f"  [fog] max-scale<={max_scale:.4f} m: remove {removed}")

    if max_scale_percentile is not None and scales is not None:
        thr = float(np.percentile(scales[keep], max_scale_percentile))
        m = scales <= thr
        removed = int((keep & ~m).sum())
        keep &= m
        logs.append(
            f"  [fog] max-scale p{max_scale_percentile:.1f}<={thr:.4f} m: remove {removed}"
        )

    if use_voxel and voxel_size is not None and voxel_size > 0 and op is not None:
        sub = np.where(keep)[0]
        if len(sub) > 0:
            key, _ = _voxel_keys(x[sub], y[sub], z[sub], voxel_size)
            _, inverse, counts = np.unique(key, return_inverse=True, return_counts=True)
            voxel_count = counts[inverse]
            op_sub = op[sub]
            drop_local = (voxel_count < voxel_min_count) & (op_sub < voxel_max_opacity)
            removed = int(drop_local.sum())
            keep[sub[drop_local]] = False
            logs.append(
                f"  [fog] sparse voxel (size={voxel_size:.3f} m, "
                f"count<{voxel_min_count}, opacity<{voxel_max_opacity:.2f}): remove {removed}"
            )

    if camera_centers is not None and cameras_max_dist is not None and op is not None:
        pts = np.column_stack([x, y, z])
        # (N, C) distances -> min over cameras
        d2 = np.sum((pts[:, None, :] - camera_centers[None, :, :]) ** 2, axis=2)
        min_d = np.sqrt(d2.min(axis=1))
        drop = (min_d > cameras_max_dist) & (op < cameras_max_opacity)
        removed = int((keep & drop).sum())
        keep &= ~drop
        logs.append(
            f"  [fog] far from cameras (>{cameras_max_dist:.1f} m, "
            f"opacity<{cameras_max_opacity:.2f}): remove {removed}"
        )

    return keep, logs


def detect_surface_y(
    y: np.ndarray,
    y_ref: float,
    direction: str = "floor",
    search_range: float = 2.5,
    n_bins: int = 200,
    threshold_frac: float = 0.08,
) -> float | None:
    """
    Detect a horizontal room surface from the Gaussian y-distribution.
    COLMAP coordinate system: y points DOWN (ceiling = small y, floor = large y).

    direction='floor'
        Searches [y_ref, y_ref + search_range] (below cameras).
        Returns the OUTER DROP-OFF edge — last dense bin before noise.
        Caller adds a small +margin to keep the floor surface but cut sub-floor junk.

    direction='ceil'
        Searches [y_ref - search_range, y_ref] (above cameras).
        Returns the PEAK position — the ceiling surface itself.
        Caller adds a +margin toward cameras to fully remove the ceiling.

    Returns None if fewer than 60 points are found in the search band or the
    histogram peak is too low to be reliable.
    """
    if direction == "floor":
        lo, hi = float(y_ref), float(y_ref) + search_range
    else:
        lo, hi = float(y_ref) - search_range, float(y_ref)

    sub = y[(y >= lo) & (y <= hi)]
    if len(sub) < 60:
        return None

    counts, edges = np.histogram(sub, bins=n_bins, range=(lo, hi))
    w = max(3, n_bins // 20)
    kernel = np.ones(w, dtype=np.float64) / w
    smoothed = np.convolve(counts.astype(np.float64), kernel, mode="same")

    peak_val = float(smoothed.max())
    if peak_val < 20.0:
        return None
    threshold = threshold_frac * peak_val

    if direction == "floor":
        above = np.where(smoothed > threshold)[0]
        if len(above) == 0:
            return None
        last_idx = int(above[-1])
        return float((edges[last_idx] + edges[last_idx + 1]) / 2.0)
    else:
        peak_idx = int(smoothed.argmax())
        return float((edges[peak_idx] + edges[peak_idx + 1]) / 2.0)


def resolve_camera_centers(
    ply_path: Path,
    scene: Path | None,
    cameras_json: Path | None,
    no_json_fallback: bool,
) -> tuple[np.ndarray | None, str | None]:
    cam_path = cameras_json
    if cam_path is None and not no_json_fallback:
        cam_path = default_cameras_json_near(ply_path)
    if cam_path is not None and cam_path.is_file():
        centers = json_camera_centers(cam_path)
        return centers, str(cam_path)
    if scene is not None:
        centers = colmap_camera_centers(scene)
        return centers, str(scene / "sparse" / "0")
    return None, None


def default_cameras_json_near(ply_path: Path) -> Path | None:
    """output/cameras.json if ply_path is .../output/point_cloud/iteration_XXX/point_cloud.ply"""
    p = ply_path.resolve()
    parts = p.parts
    if "point_cloud" in parts:
        i = parts.index("point_cloud")
        candidate = Path(*parts[:i]) / "cameras.json"
        if candidate.is_file():
            return candidate
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_ply", type=Path)
    ap.add_argument("output_ply", type=Path)
    ap.add_argument("--mode", choices=("percentile", "cameras"), default="percentile")
    ap.add_argument("--dry-run", action="store_true", help="Only print stats; do not write output")
    ap.add_argument("--list-bounds", action="store_true", help="Print xyz min/max and exit")

    ap.add_argument("--p-low", type=float, default=5.0, help="[percentile] low %% on x and z (0–100)")
    ap.add_argument("--p-high", type=float, default=94.0, help="[percentile] high %% on x and z (0–100)")
    ap.add_argument(
        "--p-low-y",
        type=float,
        default=10.0,
        help="[percentile] cut roof: remove lowest %% on y (COLMAP y-down, ceiling = min y)",
    )
    ap.add_argument(
        "--p-high-y",
        type=float,
        default=98.5,
        help="[percentile] cut floor: remove highest %% on y (default loose 98.5)",
    )
    ap.add_argument(
        "--axes",
        choices=("xyz", "xz"),
        default="xyz",
        help="[percentile] xyz = clip x,y,z; xz = clip x,z + y band (p-low-y / p-high-y)",
    )

    ap.add_argument("--scene", type=Path, default=None, help="[cameras] COLMAP scene root (contains sparse/0/)")
    ap.add_argument("--cameras-json", type=Path, default=None, help="[cameras] 3DGS cameras.json")
    ap.add_argument("--margin", type=float, default=1.75, help="[cameras] expand hull in x–z (metres)")
    ap.add_argument(
        "--y-roof-cut",
        type=float,
        default=1.2,
        help="[cameras] raise min-y bound by this many metres above lowest camera (cut ceiling)",
    )
    ap.add_argument(
        "--y-floor-extra",
        type=float,
        default=3.0,
        help="[cameras] metres below highest camera y to keep floor",
    )
    ap.add_argument(
        "--y-padding",
        type=float,
        default=None,
        help=argparse.SUPPRESS,
    )
    ap.add_argument(
        "--y-extra-above",
        type=float,
        default=None,
        help=argparse.SUPPRESS,
    )
    ap.add_argument("--no-json-fallback", action="store_true", help="Do not look for cameras.json beside output/")

    ap.add_argument("--use-bbox", action="store_true", help="[cameras] Use AABB instead of convex hull in XZ")

    # Surface snap: automatic floor / ceiling detection from Gaussian density.
    ap.add_argument(
        "--floor-snap",
        action="store_true",
        help="[cameras] Auto-detect floor Y from Gaussian density drop-off; "
             "clip sub-floor junk without going too deep",
    )
    ap.add_argument(
        "--floor-snap-margin",
        type=float,
        default=0.15,
        metavar="METRES",
        help="[floor-snap] Keep this many metres past the detected floor surface (default 0.15)",
    )
    ap.add_argument(
        "--floor-snap-search",
        type=float,
        default=2.5,
        metavar="METRES",
        help="[floor-snap] Search range below cameras for floor detection (default 2.5 m)",
    )
    ap.add_argument(
        "--ceil-snap",
        action="store_true",
        help="[cameras] Auto-detect ceiling Y from density peak; fully slice off ceiling surface",
    )
    ap.add_argument(
        "--ceil-snap-margin",
        type=float,
        default=0.10,
        metavar="METRES",
        help="[ceil-snap] Shift cut inward (toward room) by this many metres (default 0.10)",
    )
    ap.add_argument(
        "--ceil-snap-search",
        type=float,
        default=2.5,
        metavar="METRES",
        help="[ceil-snap] Search range above cameras for ceiling detection (default 2.5 m)",
    )
    ap.add_argument(
        "--xz-percentile",
        type=float,
        default=None,
        metavar="P",
        help="After hull/bbox crop keep only the central P%% of x and z "
             "(clips scattered side floaters; e.g. --xz-percentile 99.0)",
    )
    ap.add_argument(
        "--interior",
        action="store_true",
        help="Preset for indoor scans: enables --floor-snap, --ceil-snap, "
             "--xz-percentile 99.0, and stronger defog defaults",
    )

    ap.add_argument(
        "--defog",
        action="store_true",
        help="Preset: min-opacity + large scales + sparse-voxel fog removal",
    )
    ap.add_argument(
        "--min-opacity",
        type=float,
        default=None,
        help="Drop Gaussians with sigmoid(opacity) below this (0–1)",
    )
    ap.add_argument(
        "--max-scale",
        type=float,
        default=None,
        help="Drop if max(exp(scale_i)) exceeds this size in metres",
    )
    ap.add_argument(
        "--max-scale-percentile",
        type=float,
        default=None,
        metavar="P",
        help="Drop scales above percentile P (0–100) among survivors",
    )
    ap.add_argument(
        "--defog-voxel",
        type=float,
        default=None,
        metavar="METRES",
        help="Voxel size for sparse fog cull (default: auto from scene extent)",
    )
    ap.add_argument("--defog-voxel-min-count", type=int, default=3, help="Min Gaussians per voxel to keep faint splats")
    ap.add_argument(
        "--defog-voxel-max-opacity",
        type=float,
        default=0.22,
        help="In sparse voxels, remove splats below this opacity",
    )
    ap.add_argument("--no-defog-voxel", action="store_true", help="Disable voxel-based internal fog filter")
    ap.add_argument(
        "--defog-cameras-max-dist",
        type=float,
        default=None,
        metavar="METRES",
        help="With cameras: remove faint splats farther than this from all camera centers",
    )
    ap.add_argument(
        "--defog-cameras-max-opacity",
        type=float,
        default=0.35,
        help="Max opacity for --defog-cameras-max-dist cull",
    )
    args = ap.parse_args()

    # Legacy flags (old logic was inverted for COLMAP y-down).
    if args.y_extra_above is not None:
        args.y_floor_extra = args.y_extra_above
    if args.y_padding is not None:
        args.y_roof_cut = args.y_padding

    if args.defog:
        if args.min_opacity is None:
            args.min_opacity = 0.06
        if args.max_scale is None and args.max_scale_percentile is None:
            args.max_scale_percentile = 99.5
        if args.defog_voxel is None and not args.no_defog_voxel:
            args.defog_voxel = -1.0  # sentinel: auto
        if args.defog_cameras_max_dist is None:
            args.defog_cameras_max_dist = 12.0  # used only if cameras.json / --scene found

    if args.interior:
        # Surface snap
        args.floor_snap = True
        args.ceil_snap = True
        # Extra side trim
        if args.xz_percentile is None:
            args.xz_percentile = 99.0
        # Defog: aggressive settings for interior fog / backscatter
        if args.min_opacity is None:
            args.min_opacity = 0.05
        if args.max_scale is None and args.max_scale_percentile is None:
            args.max_scale_percentile = 99.0
        if args.defog_voxel is None and not args.no_defog_voxel:
            args.defog_voxel = -1.0
        if args.defog_cameras_max_dist is None:
            args.defog_cameras_max_dist = 15.0

    if not args.input_ply.is_file():
        sys.stderr.write(f"Not a file: {args.input_ply}\n")
        sys.exit(1)
    if args.output_ply.is_dir():
        sys.stderr.write(
            f"output_ply is a directory, not a file: {args.output_ply}\n"
            f"Example:\n"
            f"  python {Path(__file__).name} in.ply "
            f"{args.output_ply / 'point_cloud_cropped.ply'}\n"
        )
        sys.exit(1)
    if args.output_ply.suffix.lower() != ".ply":
        sys.stderr.write(f"Warning: output path has no .ply extension: {args.output_ply}\n")

    ply = PlyData.read(str(args.input_ply))
    v = ply["vertex"]
    data = v.data
    n0 = len(data)
    x = np.asarray(data["x"], dtype=np.float64)
    y = np.asarray(data["y"], dtype=np.float64)
    z = np.asarray(data["z"], dtype=np.float64)

    print(f"Read {n0} Gaussians from {args.input_ply}")
    if args.list_bounds:
        print(
            f"x: [{x.min():.6f}, {x.max():.6f}]\n"
            f"y: [{y.min():.6f}, {y.max():.6f}]\n"
            f"z: [{z.min():.6f}, {z.max():.6f}]"
        )
        return

    mask = np.ones(n0, dtype=bool)

    if args.mode == "percentile":
        pl, ph = args.p_low, args.p_high
        ply, phy = args.p_low_y, args.p_high_y
        if not (0 <= pl < ph <= 100):
            sys.stderr.write("Need 0 <= p-low < p-high <= 100\n")
            sys.exit(2)
        if not (0 <= ply < phy <= 100):
            sys.stderr.write("Need 0 <= p-low-y < p-high-y <= 100\n")
            sys.exit(2)
        for arr, name in ((x, "x"), (z, "z")):
            lo, hi = np.percentile(arr, [pl, ph])
            mask &= (arr >= lo) & (arr <= hi)
            print(f"  {name}: keep [{lo:.6f}, {hi:.6f}] ({pl}%–{ph}%)")
        lo_y = float(np.percentile(y, ply))
        hi_y = float(np.percentile(y, phy))
        mask &= (y >= lo_y) & (y <= hi_y)
        print(
            f"  y: keep [{lo_y:.6f}, {hi_y:.6f}] "
            f"(roof: cut lowest {ply}% y, floor: cut above {phy}% y; COLMAP y-down)"
        )
        if args.axes == "xz":
            print("  (axes=xz: same y band applied)")

    else:
        cam_path = args.cameras_json
        if cam_path is None and not args.no_json_fallback:
            cam_path = default_cameras_json_near(args.input_ply)
        if cam_path is not None and cam_path.is_file():
            centers = json_camera_centers(cam_path)
            print(f"  Using {len(centers)} cameras from {cam_path}")
        elif args.scene is not None:
            centers = colmap_camera_centers(args.scene)
            print(f"  Using {len(centers)} COLMAP cameras from {args.scene / 'sparse' / '0'}")
        else:
            sys.stderr.write(
                "cameras mode needs --scene PATH (COLMAP) or --cameras-json, "
                "or place cameras.json next to training output.\n"
            )
            sys.exit(2)

        cx, cy, cz = centers[:, 0], centers[:, 1], centers[:, 2]
        xz = np.column_stack([cx, cz])
        if len(xz) < 3 or args.use_bbox:
            xmin, xmax = cx.min() - args.margin, cx.max() + args.margin
            zmin, zmax = cz.min() - args.margin, cz.max() + args.margin
            mask &= x >= xmin
            mask &= x <= xmax
            mask &= z >= zmin
            mask &= z <= zmax
            print(f"  Few cameras: using AABB xz + margin; x[{xmin:.4f},{xmax:.4f}] z[{zmin:.4f},{zmax:.4f}]")
        else:
            hull = convex_hull_2d(xz)
            px, pz = dilate_polygon_radial(hull, args.margin)
            mask &= points_in_polygon_xz(x, z, px, pz)
            print(f"  Convex hull in xz: {len(hull)} vertices, margin={args.margin} m")

        # COLMAP y-down: min camera y ≈ ceiling side, max y ≈ head / floor side.
        y_lo = float(cy.min() + args.y_roof_cut)
        y_hi = float(cy.max() + args.y_floor_extra)

        # Optional surface snap (overrides y_roof_cut / y_floor_extra).
        if args.ceil_snap:
            # detect_surface_y returns the ceiling PEAK; shift toward room to cut it off.
            surf = detect_surface_y(
                y[mask], float(cy.min()),
                direction="ceil", search_range=args.ceil_snap_search,
            )
            if surf is not None:
                y_lo = surf + args.ceil_snap_margin
                print(f"  ceil-snap: ceiling peak at y={surf:.4f} → y_lo={y_lo:.4f} (ceiling removed)")
            else:
                print(f"  ceil-snap: no clear peak found, fallback y_lo={y_lo:.4f}")

        if args.floor_snap:
            # detect_surface_y returns the last-dense bin in the floor zone.
            surf = detect_surface_y(
                y[mask], float(cy.max()),
                direction="floor", search_range=args.floor_snap_search,
            )
            if surf is not None:
                y_hi = surf + args.floor_snap_margin
                print(f"  floor-snap: floor drop-off at y={surf:.4f} → y_hi={y_hi:.4f} (sub-floor removed)")
            else:
                print(f"  floor-snap: no clear drop-off found, fallback y_hi={y_hi:.4f}")

        mask &= y >= y_lo
        mask &= y <= y_hi
        print(
            f"  y band from cameras: [{y_lo:.6f}, {y_hi:.6f}] "
            f"(roof-cut +{args.y_roof_cut} m above min cam y, "
            f"floor +{args.y_floor_extra} m below max cam y)"
        )

    # Optional side trim: clip x and z to central P% of surviving splats.
    if args.xz_percentile is not None and 0.0 < args.xz_percentile < 100.0:
        p_margin = (100.0 - args.xz_percentile) / 2.0
        sub_x = x[mask]
        sub_z = z[mask]
        xl, xh = np.percentile(sub_x, [p_margin, 100.0 - p_margin])
        zl, zh = np.percentile(sub_z, [p_margin, 100.0 - p_margin])
        before_xz = int(mask.sum())
        mask &= (x >= xl) & (x <= xh) & (z >= zl) & (z <= zh)
        print(
            f"  xz-percentile p{args.xz_percentile}: "
            f"x[{xl:.4f},{xh:.4f}] z[{zl:.4f},{zh:.4f}]  "
            f"remove {before_xz - int(mask.sum())} side splats"
        )

    after_crop = int(mask.sum())
    print(f"After crop: keep {after_crop} / {n0}  (remove {n0 - after_crop})")

    fog_enabled = (
        args.min_opacity is not None
        or args.max_scale is not None
        or args.max_scale_percentile is not None
        or (args.defog_voxel is not None and not args.no_defog_voxel)
        or args.defog_cameras_max_dist is not None
    )
    if fog_enabled:
        sub_idx = np.where(mask)[0]
        sub_data = data[sub_idx]
        voxel_size = args.defog_voxel
        if voxel_size is not None and voxel_size < 0:
            voxel_size = _auto_voxel_size(
                np.asarray(sub_data["x"], dtype=np.float64),
                np.asarray(sub_data["y"], dtype=np.float64),
                np.asarray(sub_data["z"], dtype=np.float64),
            )
            print(f"  [fog] auto voxel size: {voxel_size:.4f} m")

        cam_centers = None
        if args.defog_cameras_max_dist is not None:
            cam_centers, cam_src = resolve_camera_centers(
                args.input_ply, args.scene, args.cameras_json, args.no_json_fallback
            )
            if cam_centers is None:
                print("  [fog] cameras-max-dist: skip (no --scene / cameras.json)")
            else:
                print(f"  [fog] camera centers: {len(cam_centers)} from {cam_src}")

        fog_keep, fog_logs = filter_internal_fog(
            sub_data,
            min_opacity=args.min_opacity,
            max_scale=args.max_scale,
            max_scale_percentile=args.max_scale_percentile,
            voxel_size=voxel_size if not args.no_defog_voxel else None,
            voxel_min_count=args.defog_voxel_min_count,
            voxel_max_opacity=args.defog_voxel_max_opacity,
            use_voxel=not args.no_defog_voxel,
            camera_centers=cam_centers,
            cameras_max_dist=args.defog_cameras_max_dist if cam_centers is not None else None,
            cameras_max_opacity=args.defog_cameras_max_opacity,
        )
        for line in fog_logs:
            print(line)
        mask[sub_idx] &= fog_keep
        fog_removed = after_crop - int(mask.sum())
        print(f"After defog: remove {fog_removed} more")

    kept = int(mask.sum())
    print(f"Keep {kept} / {n0}  (remove {n0 - kept} total)")
    if args.dry_run:
        return
    if kept == 0:
        sys.stderr.write("Nothing left after crop.\n")
        sys.exit(3)

    out = data[mask]
    args.output_ply.parent.mkdir(parents=True, exist_ok=True)
    PlyData([PlyElement.describe(out, "vertex")]).write(str(args.output_ply))
    print(f"Wrote {args.output_ply}")


if __name__ == "__main__":
    main()
