"""COLMAP 360 (CPU run_colmap_360.sh) + OpenMVS or 3DGS via application/scripts."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path(settings.SCRIPTS_DIR)
COLMAP360_SH = SCRIPTS_DIR / "run_colmap_360.sh"
MVS_WRAPPER = SCRIPTS_DIR / "run_colmap360_mvs.sh"
GS_WRAPPER = SCRIPTS_DIR / "run_colmap360_3dgs.sh"


def _colmap_env(use_gpu: bool) -> dict:
    """COLMAP stage: always run_colmap_360.sh; SIFT GPU only if requested and supported."""
    return {
        "COLMAP_SCRIPT": str(COLMAP360_SH),
        "COLMAP_USE_GPU": "1" if use_gpu else "0",
    }


def _run_script(script: Path, args: list[str], env: dict | None = None) -> None:
    if not script.is_file():
        raise FileNotFoundError(f"Pipeline script not found: {script}")
    if not os.access(script, os.X_OK):
        script.chmod(script.stat().st_mode | 0o111)

    run_env = os.environ.copy()
    run_env.setdefault("QT_QPA_PLATFORM", "offscreen")
    run_env["OPENMVS_BIN"] = settings.OPENMVS_BIN
    run_env["GS_REPO"] = settings.GAUSSIAN_ROOT
    if env:
        run_env.update(env)

    cmd = [str(script), *args]
    logger.info("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, env=run_env)
    if proc.stdout:
        logger.info(proc.stdout[-8000:] if len(proc.stdout) > 8000 else proc.stdout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-4000:]
        raise RuntimeError(f"{script.name} failed (code {proc.returncode}): {err}")


def _colmap_output_dir(task_dir: Path) -> Path:
    return task_dir / "colmap"


def _find_mvs_glb(colmap_dir: Path) -> Path | None:
    mvs = colmap_dir / "mvs"
    if not mvs.is_dir():
        return None
    for pattern in ("*_web.glb", "*_texture.glb", "*.glb"):
        hits = sorted(mvs.glob(pattern))
        if hits:
            return hits[-1]
    return None


def _find_3dgs_ply(colmap_dir: Path) -> Path | None:
    scene = colmap_dir / "_3dgs_scene" / "output"
    if not scene.is_dir():
        scene = colmap_dir / "output"
    candidates = sorted(scene.glob("point_cloud/iteration_*/point_cloud.ply"))
    if candidates:
        return candidates[-1]
    return None


def run_colmap360_openmvs(
    video_path: str,
    task_dir: str,
    *,
    fps: int = 2,
    mask_bottom: float = 0.0,
    use_gpu: bool = False,
    skip_refine: bool = False,
) -> dict:
    task_dir = Path(task_dir)
    output = _colmap_output_dir(task_dir)
    output.mkdir(parents=True, exist_ok=True)

    args = [
        "--input", video_path,
        "--output", str(output),
        "--fps", str(fps),
        "--mask-bottom", str(mask_bottom),
    ]
    if not use_gpu:
        args.append("--no-gpu")
    if skip_refine:
        args.extend(["--skip-refine"])

    _run_script(MVS_WRAPPER, args, env=_colmap_env(use_gpu))

    glb = _find_mvs_glb(output)
    erp_dir = output / "_erp_frames"
    return {
        "colmap_dir": str(output),
        "erp_frames_dir": str(erp_dir) if erp_dir.is_dir() else None,
        "glb_path": str(glb) if glb else None,
        "viewer_type": "mesh",
        "result_path": str(glb) if glb else None,
    }


def run_colmap360_3dgs(
    video_path: str,
    task_dir: str,
    *,
    fps: int = 2,
    mask_bottom: float = 0.0,
    use_gpu: bool = False,
    skip_train: bool = False,
    train_extra: list[str] | None = None,
) -> dict:
    task_dir = Path(task_dir)
    output = _colmap_output_dir(task_dir)
    output.mkdir(parents=True, exist_ok=True)

    args = [
        "--input", video_path,
        "--output", str(output),
        "--fps", str(fps),
        "--mask-bottom", str(mask_bottom),
    ]
    if not use_gpu:
        args.append("--no-gpu")
    if skip_train:
        args.append("--skip-3dgs")
    if train_extra:
        args.append("--")
        args.extend(train_extra)

    _run_script(GS_WRAPPER, args, env=_colmap_env(use_gpu))

    ply = _find_3dgs_ply(output)
    erp_dir = output / "_erp_frames"
    model_dir = output / "_3dgs_scene" / "output"
    return {
        "colmap_dir": str(output),
        "erp_frames_dir": str(erp_dir) if erp_dir.is_dir() else None,
        "gaussian_model_path": str(model_dir) if model_dir.is_dir() else None,
        "ply_path": str(ply) if ply else None,
        "viewer_type": "splat",
        "result_path": str(ply) if ply else None,
    }
