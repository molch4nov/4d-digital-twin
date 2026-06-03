"""Build panorama_index.json linking ERP frames to COLMAP camera positions."""

from __future__ import annotations

import importlib.util
import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import numpy as np

_FRAME_RE = re.compile(r"frame_(\d+)_v(\d+)", re.I)


@lru_cache(maxsize=1)
def _colmap_loader():
    path = Path("/root/work/gaussian-splatting/scene/colmap_loader.py")
    if not path.is_file():
        raise FileNotFoundError(f"colmap_loader.py not found: {path}")
    spec = importlib.util.spec_from_file_location("colmap_loader_standalone", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.read_extrinsics_binary, mod.read_extrinsics_text, mod.qvec2rotmat


def _load_colmap_images(sparse_dir: Path) -> dict:
    read_extrinsics_binary, read_extrinsics_text, _ = _colmap_loader()

    bin_path = sparse_dir / "images.bin"
    txt_path = sparse_dir / "images.txt"
    if bin_path.is_file():
        images = read_extrinsics_binary(str(bin_path))
    elif txt_path.is_file():
        images = read_extrinsics_text(str(txt_path))
    else:
        raise FileNotFoundError(f"No images.bin/txt in {sparse_dir}")
    return images


def _camera_center(qvec, tvec, qvec2rotmat) -> np.ndarray:
    R = qvec2rotmat(np.asarray(qvec, dtype=np.float64))
    t = np.asarray(tvec, dtype=np.float64).reshape(3)
    return (-R.T @ t).astype(np.float64)


def _camera_forward(qvec, qvec2rotmat) -> np.ndarray:
    R = qvec2rotmat(np.asarray(qvec, dtype=np.float64))
    return (R.T @ np.array([0.0, 0.0, 1.0], dtype=np.float64)).astype(np.float64)


def build_panorama_index(
    task_id: str,
    colmap_dir: Path,
    *,
    result_path: str | None = None,
    viewer_type: str = "mesh",
) -> Path:
    """Write {task_dir}/panorama_index.json. Returns path to manifest."""
    colmap_dir = Path(colmap_dir)
    task_dir = colmap_dir.parent
    erp_dir = colmap_dir / "_erp_frames"
    if not erp_dir.is_dir():
        raise FileNotFoundError(f"ERP folder missing: {erp_dir}")

    sparse = colmap_dir / "sparse" / "0"
    if not sparse.is_dir():
        for d in sorted((colmap_dir / "sparse").glob("*")):
            if (d / "images.bin").is_file():
                sparse = d
                break
    _, _, qvec2rotmat = _colmap_loader()
    images = _load_colmap_images(sparse)

    by_erp: dict[int, list[np.ndarray]] = defaultdict(list)
    by_erp_forward: dict[int, list[np.ndarray]] = defaultdict(list)
    for im in images.values():
        name = im.name
        m = _FRAME_RE.search(name)
        if not m:
            continue
        erp_id = int(m.group(1))
        view_id = int(m.group(2))
        c = _camera_center(im.qvec, im.tvec, qvec2rotmat)
        by_erp[erp_id].append(c)
        # ERP center ≈ pinhole view v0 (yaw=0 in ffmpeg v360).
        if view_id == 0:
            by_erp_forward[erp_id].append(_camera_forward(im.qvec, qvec2rotmat))

    frames = []
    for erp_id in sorted(by_erp.keys()):
        erp_file = erp_dir / f"erp_{erp_id:06d}.jpg"
        if not erp_file.is_file():
            continue
        centers = np.stack(by_erp[erp_id], axis=0)
        pos = centers.mean(axis=0)
        frame = {
            "id": erp_id,
            "x": float(pos[0]),
            "y": float(pos[1]),
            "z": float(pos[2]),
            "url": f"/api/v1/files/{task_id}/colmap/_erp_frames/erp_{erp_id:06d}.jpg",
            "path": f"colmap/_erp_frames/erp_{erp_id:06d}.jpg",
        }
        forwards = by_erp_forward.get(erp_id)
        if forwards:
            fwd = np.mean(np.stack(forwards, axis=0), axis=0)
            norm = float(np.linalg.norm(fwd))
            if norm > 1e-9:
                fwd = fwd / norm
                frame["forward"] = {
                    "x": float(fwd[0]),
                    "y": float(fwd[1]),
                    "z": float(fwd[2]),
                }
        frames.append(frame)

    asset_url = None
    if result_path and Path(result_path).is_file():
        asset_url = f"/api/v1/files/{task_id}/{Path(result_path).relative_to(task_dir).as_posix()}"

    manifest = {
        "task_id": task_id,
        "coordinate": "colmap",
        "viewer_type": viewer_type,
        "mesh_rotation": "x180",
        "asset_url": asset_url,
        "colmap_dir": str(colmap_dir),
        "frames": frames,
    }
    out = task_dir / "panorama_index.json"
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out


def load_manifest(task_dir: Path) -> dict:
    path = task_dir / "panorama_index.json"
    if not path.is_file():
        raise FileNotFoundError("panorama_index.json not built yet")
    return json.loads(path.read_text(encoding="utf-8"))


def nearest_frame(manifest: dict, x: float, y: float, z: float) -> dict | None:
    frames = manifest.get("frames") or []
    if not frames:
        return None
    p = np.array([x, y, z], dtype=np.float64)
    best = None
    best_d = float("inf")
    for fr in frames:
        c = np.array([fr["x"], fr["y"], fr["z"]], dtype=np.float64)
        d = float(np.linalg.norm(p - c))
        if d < best_d:
            best_d = d
            best = {**fr, "distance": d}
    return best
