#!/usr/bin/env python3
"""
SphereSfM (json87/SphereSfM) + OpenMVS pipeline for 360° / equirectangular video.

SphereSfM is a COLMAP fork (built locally at $SPHERESFM_BIN) that adds:
- ImageReader.camera_model SPHERE
- Mapper.sphere_camera flag
- sphere_cubic_reprojecer command

Pipeline:
  1. database_creator
  2. feature_extractor (SPHERE camera, single_camera=1)
  3. matching: sequential_matcher (video) | exhaustive_matcher | spatial_matcher
  4. mapper (sphere_camera=1, intrinsics frozen)
  5. sphere_cubic_reprojecer (per panorama -> 6 perspective faces + sparse model)
  6. image_undistorter --output_type COLMAP (writes a "dense"-style workspace)
  7. InterfaceCOLMAP -> scene.mvs
  8-9. run_openmvs_phase: DensifyPointCloud -> ReconstructMesh -> [RefineMesh] -> TextureMesh -> GLB

The output_dir layout is:
  <output_dir>/
    colmap/
      database.db
      sparse/0/        # mapper result
      sparse-cubic/    # sphere_cubic_reprojecer (images + sparse subdir)
      dense/           # image_undistorter --output_type COLMAP
    mvs/
      scene.mvs        # InterfaceCOLMAP output
      (DensifyPointCloud / ReconstructMesh / TextureMesh artefacts)
    scene.glb
"""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from config.settings import settings
from .mvs_pipeline import (
    _print_step,
    _require_exec,
    _require_file,
    _resolve_tool,
    run_openmvs_phase,
)

logger = logging.getLogger(__name__)


def _run_colmap(colmap_bin: Path, subcmd: str, args: list, label: str,
                check: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(colmap_bin), subcmd] + list(args)
    logger.info(f"{label}: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.stdout:
        logger.info(f"{label} stdout:\n{r.stdout}")
    if r.stderr:
        logger.info(f"{label} stderr:\n{r.stderr}")
    if check and r.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {r.returncode}):\n{r.stderr}")
    return r


def _is_cuda_arch_mismatch(text: str) -> bool:
    """Detect the `no kernel image is available for execution on the device`
    error that COLMAP/SiftGPU emits when the binary's CUDA archs don't include
    the current device's compute capability (e.g. SphereSfM built without
    sm_80 support, run on an A30/A100)."""
    return "no kernel image is available" in (text or "")


def _run_colmap_with_gpu_fallback(
    colmap_bin: Path,
    subcmd: str,
    args: list,
    label: str,
    *,
    gpu_args: list[str],
) -> tuple[subprocess.CompletedProcess, bool]:
    """Run a COLMAP subcommand; if the GPU SIFT kernels are missing for this
    device, transparently retry with --use_gpu 0 toggled on the listed flags.

    Returns (CompletedProcess, used_cpu_fallback).
    """
    r = _run_colmap(colmap_bin, subcmd, args, label, check=False)
    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    if r.returncode == 0 and not _is_cuda_arch_mismatch(combined):
        return r, False

    if _is_cuda_arch_mismatch(combined):
        logger.warning(
            f"{label}: detected CUDA arch mismatch ('no kernel image is available'). "
            f"This SphereSfM build does not include kernels for the current GPU. "
            f"Re-running on CPU (use_gpu=0). To fix permanently, rebuild SphereSfM "
            f"with -DCUDA_ARCHS=\"{_detect_cuda_arch() or '8.0'}\"."
        )
        new_args = list(args)
        # Flip all listed --*.use_gpu flags from 1 to 0.
        for flag in gpu_args:
            for i, a in enumerate(new_args):
                if a == flag and i + 1 < len(new_args):
                    new_args[i + 1] = "0"
                    break
            else:
                # Flag wasn't in args; append it.
                new_args.extend([flag, "0"])
        r2 = _run_colmap(colmap_bin, subcmd, new_args, label + " (CPU fallback)", check=False)
        if r2.returncode != 0:
            raise RuntimeError(
                f"{label} failed on both GPU and CPU fallback (exit {r2.returncode}):\n"
                f"{r2.stderr}"
            )
        return r2, True

    raise RuntimeError(f"{label} failed (exit {r.returncode}):\n{r.stderr}")


def _detect_cuda_arch() -> Optional[str]:
    """Best-effort: return the SM arch (e.g. '8.0') of the visible GPU."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        line = (r.stdout or "").strip().splitlines()[0].strip()
        return line or None
    except Exception:
        return None


def _read_first_image_size(image_dir: Path) -> tuple[int, int]:
    """Return (W, H) of the first image in image_dir."""
    from PIL import Image
    exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
    for f in sorted(image_dir.iterdir()):
        if f.suffix.lower() in exts:
            with Image.open(f) as im:
                return im.size  # (W, H)
    raise RuntimeError(f"No images found in {image_dir}")


def _resize_equirectangular(src: Path, dst: Path, target_long_edge: int):
    """Downscale equirectangular images so the long edge is `target_long_edge`."""
    from PIL import Image
    dst.mkdir(parents=True, exist_ok=True)
    exts = {".jpg", ".jpeg", ".png"}
    files = sorted(f for f in src.iterdir() if f.suffix.lower() in exts)
    for i, f in enumerate(files, 1):
        out = dst / f.name
        if out.exists():
            continue
        im = Image.open(f)
        w, h = im.size
        if max(w, h) > target_long_edge:
            new_w = target_long_edge
            new_h = int(round(h * (new_w / w)))
            im = im.resize((new_w, new_h), Image.LANCZOS)
        im.save(out, quality=95)
        if i % 25 == 0 or i == len(files):
            logger.info(f"  resized {i}/{len(files)}")


def run_sphere_colmap_pipeline(
    image_dir: str,
    output_dir: str,
    resize_long_edge: int = 4096,
    matcher: str = "sequential",
    sequential_overlap: int = 12,
    cubic_face_size: int = 0,
    cubic_field_of_view: float = 90.0,
    skip_refine: bool = False,
    clean_mvs: bool = False,
    use_gpu: bool = True,
    min_num_matches: int = 15,
    openmvs_max_threads: Optional[int] = None,
    openmvs_densify_extra: Optional[str] = None,
    openmvs_keep_depth_maps: Optional[bool] = None,
) -> dict:
    """
    Run the SphereSfM/COLMAP -> OpenMVS pipeline for 360° equirectangular images.

    Args:
        image_dir: Input directory with equirectangular .jpg/.png frames.
        output_dir: Output root directory.
        resize_long_edge: Downscale panoramas so long edge is this many px. 0 = no resize.
        matcher: "sequential" (best for video), "exhaustive" (small datasets), "spatial".
        sequential_overlap: Overlap window for sequential matcher.
        cubic_face_size: Pixel size of each cubic face. 0 = let SphereSfM pick.
        cubic_field_of_view: FOV (degrees) per cubic face. 90 covers the sphere with 6 faces.
        skip_refine: Skip OpenMVS RefineMesh.
        clean_mvs: Wipe derived MVS artefacts before starting.
        use_gpu: Use GPU for SIFT extraction/matching.
        min_num_matches: SiftMatching.min_num_inliers (default 15).
        openmvs_max_threads / openmvs_densify_extra / openmvs_keep_depth_maps:
            Forwarded to run_openmvs_phase.

    Returns:
        dict with paths to sparse model, dense scene, textured PLY/MVS, GLB.
    """
    image_dir = Path(image_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    threads = getattr(settings, "WORKER_THREADS", 4)
    mvs_threads = openmvs_max_threads or getattr(settings, "OPENMVS_MAX_THREADS", None) or threads
    densify_extra = openmvs_densify_extra or getattr(settings, "OPENMVS_DENSIFY_EXTRA", "")
    keep_depth_maps = openmvs_keep_depth_maps if openmvs_keep_depth_maps is not None else getattr(
        settings, "OPENMVS_KEEP_DEPTH_MAPS", False
    )

    # Defensive defaults: callers (the validation layer, environment variables,
    # or a half-filled UI form) may pass 0 here, which COLMAP rejects with
    # "Check failed: overlap > 0". Treat non-positive as "use sane default".
    if sequential_overlap is None or sequential_overlap < 1:
        sequential_overlap = 12
    if min_num_matches is None or min_num_matches < 1:
        min_num_matches = 15
    if cubic_field_of_view is None or cubic_field_of_view <= 0:
        cubic_field_of_view = 90.0

    spheresfm_bin = Path(settings.SPHERESFM_BIN) / "colmap"
    _require_exec(spheresfm_bin, "SphereSfM colmap")
    _require_file(image_dir, "image directory")

    # Layout
    colmap_dir = output_dir / "colmap"
    database_path = colmap_dir / "database.db"
    sparse_dir = colmap_dir / "sparse"
    cubic_dir = colmap_dir / "sparse-cubic"
    dense_dir = colmap_dir / "dense"
    mvs_dir = output_dir / "mvs"
    work_images_dir = output_dir / "images_sfm"

    for d in [colmap_dir, sparse_dir, mvs_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # 0. Optional resize of equirectangular images
    sfm_image_dir = image_dir
    if resize_long_edge > 0:
        _print_step(f"0/9 Resize equirectangular images to {resize_long_edge}px")
        _resize_equirectangular(image_dir, work_images_dir, resize_long_edge)
        sfm_image_dir = work_images_dir

    _print_step("Input")
    logger.info(f"Images       : {sfm_image_dir}")
    logger.info(f"Output       : {output_dir}")
    logger.info(f"SphereSfM    : {spheresfm_bin}")
    logger.info(f"Matcher      : {matcher} (overlap={sequential_overlap})")
    logger.info(f"Resize       : {resize_long_edge}px")
    logger.info(f"Cubic FOV    : {cubic_field_of_view}°, face size={cubic_face_size or 'auto'}")
    logger.info(f"Threads      : {threads}")
    logger.info(f"GPU SIFT     : {'on' if use_gpu else 'off'} (auto CPU-fallback on kernel mismatch)")
    cuda_arch = _detect_cuda_arch()
    if cuda_arch:
        logger.info(f"GPU compute  : {cuda_arch}")

    # Get image dims for SPHERE camera_params "1, cx, cy"
    img_w, img_h = _read_first_image_size(sfm_image_dir)
    sphere_params = f"1,{img_w / 2},{img_h / 2}"
    logger.info(f"Image size   : {img_w}x{img_h}  (SPHERE camera_params={sphere_params})")

    # 1. Database creator
    _print_step("1/9 database_creator")
    if not database_path.exists():
        _run_colmap(spheresfm_bin, "database_creator", [
            "--database_path", str(database_path),
        ], "1/9 database_creator")
    else:
        logger.info(f"Database already exists: {database_path}")

    # 2. Feature extraction (SPHERE camera, single_camera=1)
    _print_step("2/9 feature_extractor (SPHERE camera)")
    feat_args = [
        "--database_path", str(database_path),
        "--image_path", str(sfm_image_dir),
        "--ImageReader.camera_model", "SPHERE",
        "--ImageReader.camera_params", sphere_params,
        "--ImageReader.single_camera", "1",
        "--SiftExtraction.num_threads", str(threads),
        "--SiftExtraction.use_gpu", "1" if use_gpu else "0",
    ]
    _, used_cpu_fe = _run_colmap_with_gpu_fallback(
        spheresfm_bin, "feature_extractor", feat_args, "2/9 feature_extractor",
        gpu_args=["--SiftExtraction.use_gpu"],
    )
    if used_cpu_fe:
        # If extraction fell back, the matchers will fail the same way -- don't
        # even try GPU there.
        use_gpu = False

    # 3. Matching
    _print_step(f"3/9 {matcher}_matcher")
    common_match_args = [
        "--database_path", str(database_path),
        "--SiftMatching.num_threads", str(threads),
        "--SiftMatching.use_gpu", "1" if use_gpu else "0",
        "--SiftMatching.min_num_inliers", str(min_num_matches),
    ]
    if matcher == "sequential":
        match_args = common_match_args + [
            "--SequentialMatching.overlap", str(sequential_overlap),
            "--SequentialMatching.quadratic_overlap", "1",
        ]
        _run_colmap_with_gpu_fallback(
            spheresfm_bin, "sequential_matcher", match_args, "3/9 sequential_matcher",
            gpu_args=["--SiftMatching.use_gpu"],
        )
    elif matcher == "exhaustive":
        _run_colmap_with_gpu_fallback(
            spheresfm_bin, "exhaustive_matcher", common_match_args, "3/9 exhaustive_matcher",
            gpu_args=["--SiftMatching.use_gpu"],
        )
    elif matcher == "spatial":
        match_args = common_match_args + [
            "--SpatialMatching.is_gps", "0",
            "--SpatialMatching.max_num_neighbors", str(max(sequential_overlap, 20)),
        ]
        _run_colmap_with_gpu_fallback(
            spheresfm_bin, "spatial_matcher", match_args, "3/9 spatial_matcher",
            gpu_args=["--SiftMatching.use_gpu"],
        )
    else:
        raise RuntimeError(f"Unknown matcher: {matcher}")

    # 4. Mapper (sphere_camera=1, freeze intrinsics)
    _print_step("4/9 mapper (sphere_camera=1)")
    sparse_0 = sparse_dir / "0"
    if not (sparse_0 / "cameras.bin").exists():
        _run_colmap(spheresfm_bin, "mapper", [
            "--database_path", str(database_path),
            "--image_path", str(sfm_image_dir),
            "--output_path", str(sparse_dir),
            "--Mapper.num_threads", str(threads),
            "--Mapper.ba_refine_focal_length", "0",
            "--Mapper.ba_refine_principal_point", "0",
            "--Mapper.ba_refine_extra_params", "0",
            "--Mapper.sphere_camera", "1",
        ], "4/9 mapper")
    else:
        logger.info("Sparse model already exists, skipping mapper")

    if not (sparse_0 / "cameras.bin").exists():
        raise RuntimeError(f"Mapper produced no reconstruction (no cameras.bin in {sparse_0})")

    # Sanity check on registration
    _log_colmap_stats(spheresfm_bin, sparse_0, sfm_image_dir)

    # 5. Sphere -> cubic re-projection (writes <cubic_dir>/<name>_perspective_NNN.jpg
    #    and <cubic_dir>/sparse/{cameras,images,points3D}.bin)
    _print_step("5/9 sphere_cubic_reprojecer")
    cubic_dir.mkdir(parents=True, exist_ok=True)
    cubic_sparse = cubic_dir / "sparse"
    if not (cubic_sparse / "cameras.bin").exists():
        sphere_args = [
            "--image_path", str(sfm_image_dir),
            "--input_path", str(sparse_0),
            "--output_path", str(cubic_dir),
            "--field_of_view", str(int(cubic_field_of_view)),
        ]
        if cubic_face_size > 0:
            sphere_args.extend(["--image_size", str(cubic_face_size)])
        _run_colmap(spheresfm_bin, "sphere_cubic_reprojecer", sphere_args, "5/9 sphere_cubic_reprojecer")
    else:
        logger.info("Cubic reprojection already exists, skipping")

    # 6. Image undistorter -> COLMAP-format dense workspace
    _print_step("6/9 image_undistorter")
    dense_images = dense_dir / "images"
    dense_sparse = dense_dir / "sparse"
    if not (dense_sparse / "cameras.bin").exists():
        dense_dir.mkdir(parents=True, exist_ok=True)
        _run_colmap(spheresfm_bin, "image_undistorter", [
            "--image_path", str(cubic_dir),
            "--input_path", str(cubic_sparse),
            "--output_path", str(dense_dir),
            "--output_type", "COLMAP",
        ], "6/9 image_undistorter")
    else:
        logger.info("Undistorted dense workspace already exists, skipping")

    if not (dense_sparse / "cameras.bin").exists():
        raise RuntimeError(f"image_undistorter produced no sparse model in {dense_sparse}")
    if not dense_images.exists():
        raise RuntimeError(f"image_undistorter produced no images dir in {dense_images}")

    # 7. InterfaceCOLMAP -> scene.mvs
    _print_step("7/9 InterfaceCOLMAP (-> scene.mvs)")
    openmvs_bin = Path(settings.OPENMVS_BIN)
    interface_prefix = _resolve_tool(openmvs_bin, "InterfaceCOLMAP")
    if not interface_prefix:
        # Fall back to OpenMVS subdir layout used by this machine
        interface_prefix = _resolve_tool(openmvs_bin / "OpenMVS", "InterfaceCOLMAP")
    if not interface_prefix:
        raise RuntimeError(f"InterfaceCOLMAP not found in {openmvs_bin} (or its OpenMVS/ subdir) or PATH")

    scene_mvs = mvs_dir / "scene.mvs"
    if not scene_mvs.exists():
        cmd = interface_prefix + [
            "-w", str(mvs_dir),
            "-i", str(dense_dir),
            "-o", str(scene_mvs),
            "--image-folder", str(dense_images) + "/",
            "--max-threads", str(mvs_threads),
        ]
        logger.info(f"7/9 InterfaceCOLMAP: {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.stdout:
            logger.info(f"InterfaceCOLMAP stdout:\n{r.stdout}")
        if r.stderr:
            logger.info(f"InterfaceCOLMAP stderr:\n{r.stderr}")
        if r.returncode != 0:
            raise RuntimeError(f"InterfaceCOLMAP failed (exit {r.returncode}):\n{r.stderr}")
        time.sleep(0.5)
        if not scene_mvs.exists():
            raise RuntimeError(f"InterfaceCOLMAP exited 0 but {scene_mvs} not created")
        logger.info(f"InterfaceCOLMAP successful: {scene_mvs} ({scene_mvs.stat().st_size} bytes)")
    else:
        logger.info(f"scene.mvs already exists: {scene_mvs}")

    # 8-9. Reuse the shared OpenMVS phase
    mvs_result = run_openmvs_phase(
        scene_mvs=scene_mvs,
        mvs_dir=mvs_dir,
        output_dir=output_dir,
        mvs_threads=mvs_threads,
        densify_extra=densify_extra,
        keep_depth_maps=keep_depth_maps,
        skip_refine=skip_refine,
        clean_mvs=clean_mvs,
    )

    _print_step("Done (SphereSfM)")
    logger.info(f"Sparse model     : {sparse_0}")
    logger.info(f"Cubic dir        : {cubic_dir}")
    logger.info(f"Dense workspace  : {dense_dir}")
    logger.info(f"OpenMVS scene    : {scene_mvs}")
    logger.info(f"Dense scene      : {mvs_result['dense_mvs']}")
    logger.info(f"Textured scene   : {mvs_result.get('texture_mvs')}")
    logger.info(f"GLB              : {mvs_result.get('glb_path')}")

    return {
        "sparse_dir": str(sparse_0),
        "cubic_dir": str(cubic_dir),
        "dense_dir": str(dense_dir),
        "scene_mvs": str(scene_mvs),
        **mvs_result,
    }


def _log_colmap_stats(colmap_bin: Path, sparse_model_dir: Path, image_dir: Path) -> None:
    """Run `colmap model_analyzer` and log how many images registered.

    Raises if registration looks too thin for dense MVS.
    """
    try:
        r = subprocess.run(
            [str(colmap_bin), "model_analyzer", "--path", str(sparse_model_dir)],
            capture_output=True, text=True, timeout=60,
        )
        out = (r.stdout or "") + (r.stderr or "")
        logger.info(f"COLMAP model_analyzer:\n{out}")
        import re
        m_reg = re.search(r"Registered images:\s*(\d+)", out)
        m_obs = re.search(r"Observations:\s*(\d+)", out)
        n_reg = int(m_reg.group(1)) if m_reg else 0
        n_obs = int(m_obs.group(1)) if m_obs else 0

        # Count input images
        exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
        n_input = sum(1 for f in image_dir.iterdir() if f.suffix.lower() in exts)
        pct = 100.0 * n_reg / max(n_input, 1)
        logger.info(f"COLMAP stats: registered={n_reg}/{n_input} ({pct:.1f}%), observations={n_obs}")

        if n_reg < 8:
            raise RuntimeError(
                f"SphereSfM mapper registered only {n_reg}/{n_input} images. "
                f"This is too few for dense MVS. Try:\n"
                f"  * matcher='exhaustive' (small dataset) or larger sequential_overlap\n"
                f"  * disable resize (resize_long_edge=0) for tiny inputs\n"
                f"  * verify the input video frame coverage"
            )
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning(f"Could not run model_analyzer for sanity check: {e}")
