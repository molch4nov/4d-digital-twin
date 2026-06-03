#!/usr/bin/env python3
"""
OpenMVG + OpenMVS pipeline implementation.

Faithful Python port of:
- run_spherical_360_pipeline.sh (spherical camera pipeline)
- run_cognac_pinhole_pipeline.sh (pinhole camera pipeline)

The OpenMVS portion (dense -> mesh -> refine -> texture -> GLB) is exposed as
`run_openmvs_phase()` so the SphereSfM/COLMAP based pipeline can reuse it.
"""

import subprocess
import logging
import json
import shutil
import os
import re
import time
from pathlib import Path
from typing import Optional
import numpy as np
from config.settings import settings

logger = logging.getLogger(__name__)

# Avoid the well-known MKL/OpenMP conflict that crashes openMVG_main_SfM mid-run
# with: "libmkl_intel_thread.so: undefined symbol: __kmpc_global_thread_num".
# MKL's "intel" threading layer wants libiomp5; when libgomp is already loaded
# (or libiomp5 is missing) the resolver fails. Forcing the GNU layer makes MKL
# bind to libgomp instead.
os.environ.setdefault("MKL_THREADING_LAYER", "GNU")
os.environ.setdefault("MKL_SERVICE_FORCE_INTEL", "0")
os.environ.setdefault("OMP_NUM_THREADS", str(getattr(settings, "WORKER_THREADS", 4)))

CAMERA_MODEL_PINHOLE = 3
CAMERA_MODEL_SPHERICAL = 7


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def _require_exec(path: Path, label: str):
    if not path.exists():
        raise RuntimeError(f"Executable not found: {path} ({label})")
    if not os.access(path, os.X_OK):
        if not shutil.which(str(path)):
            raise RuntimeError(f"Executable not found in PATH: {path} ({label})")


def _require_file(path: Path, label: str):
    if not path.exists():
        raise RuntimeError(f"Required path not found: {path} ({label})")


def _print_step(step: str):
    logger.info("")
    logger.info(f"========== {step} ==========")


def _run(cmd: list, label: str = "", check: bool = True) -> subprocess.CompletedProcess:
    logger.info(f"{label}: {' '.join(str(c) for c in cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.stdout:
        logger.info(r.stdout)
    if r.returncode != 0:
        if check:
            raise RuntimeError(f"{label or cmd[0]} failed:\n{r.stderr}")
        else:
            logger.warning(f"{label} failed (best-effort, continuing): {r.stderr}")
    if r.stderr and r.returncode == 0:
        logger.debug(r.stderr)
    return r


def _run_best_effort(cmd: list, label: str = "") -> subprocess.CompletedProcess:
    return _run(cmd, label, check=False)


def _resolve_tool(bin_dir: Path, tool: str) -> Optional[list]:
    """Return cmd prefix for invoking tool from bin_dir, or PATH, or None."""
    tool_path = bin_dir / tool
    if tool_path.is_file() and os.access(tool_path, os.X_OK):
        return [str(tool_path)]
    if shutil.which(tool):
        return [tool]
    return None


def _build_pair_list(sfm_data: Path, pair_list: Path, window: int):
    with open(sfm_data) as f:
        sfm = json.load(f)
    ids = sorted(v["key"] for v in sfm["views"])
    with open(pair_list, "w") as f:
        for a in range(len(ids)):
            for b in range(a + 1, min(a + 1 + window, len(ids))):
                f.write(f"{ids[a]} {ids[b]}\n")
    logger.info(f"Wrote pair list: {pair_list} ({len(ids)} views, window={window})")


def _resize_equirectangular(src: Path, dst: Path, target: int):
    from PIL import Image
    dst.mkdir(parents=True, exist_ok=True)
    exts = {".jpg", ".jpeg", ".png"}
    files = sorted(f for f in src.iterdir() if f.suffix.lower() in exts)
    for i, name in enumerate(files, 1):
        out = dst / name.name
        if out.exists():
            continue
        im = Image.open(src / name)
        w, h = im.size
        if max(w, h) > target:
            new_w = target
            new_h = int(round(h * (new_w / w)))
            im = im.resize((new_w, new_h), Image.LANCZOS)
        im.save(out, quality=95)
        if i % 25 == 0 or i == len(files):
            logger.info(f"  resized {i}/{len(files)}")


# ---------------------------------------------------------------------------
# OpenMVS PLY loading + GLB conversion
# ---------------------------------------------------------------------------

def load_openmvs_ply(path: Path):
    """Load an OpenMVS-textured PLY (binary little-endian, with TextureFile comments)."""
    data = path.read_bytes()
    end = data.find(b"end_header\n")
    if end == -1:
        end = data.find(b"end_header\r\n")
    header = data[:end].decode("ascii", errors="ignore")

    nv = None
    tex_files = []
    vstride = 0
    big_endian = "binary_big_endian" in header

    for line in header.splitlines():
        line = line.strip()
        if line.startswith("element vertex"):
            nv = int(line.split()[-1])
        elif line.startswith("property float") or line.startswith("property uchar"):
            vstride += 4 if "float" in line else 1
        elif line.startswith("comment TextureFile"):
            tex_files.append(line.split(maxsplit=2)[-1].strip())

    if nv is None:
        raise RuntimeError(f"Could not parse vertex count from {path}")

    offset = end + (11 if b"\n" in data[end:end + 13] else 13)
    dt = np.dtype(">f4" if big_endian else "f4")
    verts = (
        np.frombuffer(data, dtype=np.uint8, count=nv * vstride, offset=offset)
        .view(dt)
        .reshape(nv, -1)[:, :3]
        .copy()
    )

    face_off = offset + nv * vstride
    faces = []
    while face_off < len(data):
        n = int(data[face_off])
        if n < 3:
            face_off += 1
            continue
        idx = np.frombuffer(data, dtype=np.uint32, count=n, offset=face_off + 1)
        face_off += 1 + n * 4
        nuv = int(data[face_off])
        uv = np.frombuffer(data, dtype=dt, count=nuv, offset=face_off + 1).reshape(-1, 2)
        face_off += 1 + nuv * 4
        faces.append({"idx": idx, "uv": uv})

    return verts, faces, tex_files


def convert_textured_to_glb(textured_mvs_path: Path, output_path: Path) -> Path:
    """Convert an OpenMVS textured PLY into a GLB scene."""
    try:
        import trimesh
        from PIL import Image
    except ImportError as e:
        logger.warning(f"trimesh/PIL not available, GLB conversion skipped: {e}")
        return textured_mvs_path

    verts, faces_list, tex_files = load_openmvs_ply(textured_mvs_path)
    logger.info(
        f"Loaded {len(verts):,} verts, {len(faces_list):,} faces, "
        f"{len(tex_files)} texture(s) from {textured_mvs_path.name}"
    )

    scene = trimesh.Scene()
    base = textured_mvs_path.parent
    vertex_offset = 0

    for tex_name in tex_files:
        tex_path = base / tex_name
        if not tex_path.exists():
            logger.warning(f"Texture missing: {tex_path}")
            continue

        logger.info(f"Processing atlas: {tex_name}")
        all_verts, all_uv, all_faces = [], [], []
        for f in faces_list:
            idx = f["idx"]
            uv = f["uv"]
            for k in range(1, len(idx) - 1):
                all_verts.extend(verts[idx[[0, k, k + 1]]])
                all_uv.extend(uv[[0, k, k + 1]])
                all_faces.append([vertex_offset, vertex_offset + 1, vertex_offset + 2])
                vertex_offset += 3
        if not all_faces:
            continue

        mesh = trimesh.Trimesh(
            vertices=np.array(all_verts, dtype=np.float32),
            faces=np.array(all_faces, dtype=np.int32),
            visual=trimesh.visual.TextureVisuals(
                uv=np.array(all_uv, dtype=np.float32),
                image=Image.open(tex_path),
            ),
            process=False,
        )
        scene.add_geometry(mesh)

    scene.export(str(output_path), file_type="glb")
    logger.info(f"GLB saved: {output_path} ({output_path.stat().st_size / (1024 * 1024):.1f} MB)")
    return output_path


# ---------------------------------------------------------------------------
# OpenMVS dense -> mesh -> texture -> GLB phase (reused by both pipelines)
# ---------------------------------------------------------------------------

def run_openmvs_phase(
    *,
    scene_mvs: Path,
    mvs_dir: Path,
    output_dir: Path,
    mvs_threads: int,
    densify_extra: str = "",
    keep_depth_maps: bool = False,
    skip_refine: bool = False,
    clean_mvs: bool = False,
    glb_name: str = "scene.glb",
) -> dict:
    """
    Run DensifyPointCloud -> ReconstructMesh -> [RefineMesh] -> TextureMesh -> GLB.

    Args:
        scene_mvs: Path to an existing scene.mvs (produced by openMVG2openMVS or InterfaceCOLMAP).
        mvs_dir: Working directory for OpenMVS (usually scene_mvs.parent).
        output_dir: Where to write the final GLB.
        mvs_threads: --max-threads for OpenMVS tools.
        densify_extra: Extra args inserted into DensifyPointCloud command.
        keep_depth_maps: If False, remove old depth*.dmap before DensifyPointCloud.
        skip_refine: Skip RefineMesh (e.g. no CUDA available).
        clean_mvs: Wipe scene_dense*, scene_dense_mesh*, *_texture* before MVS.
        glb_name: Filename for the final GLB in output_dir.

    Returns:
        dict with keys: dense_mvs, mesh_ply, texture_mvs, texture_ply, glb_path, result_path.
    """
    mvs_dir = Path(mvs_dir)
    output_dir = Path(output_dir)
    mvs = Path(settings.OPENMVS_BIN)

    if not scene_mvs.exists():
        raise RuntimeError(f"OpenMVS scene not found: {scene_mvs}")

    _print_step("8/9 Dense cloud and mesh")

    if not keep_depth_maps:
        for dmap in mvs_dir.glob("depth*.dmap"):
            logger.info(f"Removing old depth map: {dmap.name}")
            dmap.unlink()

    if clean_mvs:
        logger.info("clean_mvs=True: wiping derived MVS artefacts")
        for pattern in ["scene_dense*", "scene_dense_mesh*", "*_texture*"]:
            for p in mvs_dir.glob(pattern):
                logger.info(f"Removing: {p.name}")
                p.unlink(missing_ok=True)

    # 8a. DensifyPointCloud
    dense_mvs = mvs_dir / "scene_dense.mvs"
    if not dense_mvs.exists():
        prefix = _resolve_tool(mvs, "DensifyPointCloud")
        if not prefix:
            raise RuntimeError(f"DensifyPointCloud not found in {mvs} or PATH")
        cmd = prefix + ["-w", str(mvs_dir), "--max-threads", str(mvs_threads)]
        if densify_extra:
            cmd.extend(densify_extra.split())
        cmd.append(str(scene_mvs))

        logger.info(f"Starting DensifyPointCloud")
        logger.info(f"  Working dir: {mvs_dir}")
        logger.info(f"  Input: {scene_mvs} ({scene_mvs.stat().st_size} bytes)")
        logger.info(f"  Expected output: {dense_mvs}")
        logger.info(f"Command: {' '.join(cmd)}")

        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.stdout:
            logger.info(f"DensifyPointCloud stdout:\n{r.stdout}")
        if r.stderr:
            logger.info(f"DensifyPointCloud stderr:\n{r.stderr}")
        if r.returncode != 0:
            logger.error(f"DensifyPointCloud failed with exit code {r.returncode}")
            for f in sorted(mvs_dir.iterdir()):
                logger.info(f"  {f.name} ({f.stat().st_size} bytes)")
            raise RuntimeError(f"DensifyPointCloud failed: {r.stderr}")

        time.sleep(1)
        if not dense_mvs.exists():
            logger.error(f"DensifyPointCloud completed (exit 0) but output not found: {dense_mvs}")
            for f in sorted(mvs_dir.iterdir()):
                logger.info(f"  {f.name} ({f.stat().st_size} bytes)")
            raise RuntimeError(f"DensifyPointCloud failed: output not created at {dense_mvs}")
        logger.info(f"DensifyPointCloud successful: {dense_mvs} ({dense_mvs.stat().st_size} bytes)")
    else:
        logger.info(f"Dense point cloud already exists: {dense_mvs} ({dense_mvs.stat().st_size} bytes)")

    # 8b. ReconstructMesh
    mesh_ply = mvs_dir / "scene_dense_mesh.ply"
    if not mesh_ply.exists():
        if dense_mvs.stat().st_size == 0:
            raise RuntimeError(f"Input file for ReconstructMesh is empty: {dense_mvs}")
        prefix = _resolve_tool(mvs, "ReconstructMesh")
        if not prefix:
            raise RuntimeError(f"ReconstructMesh not found in {mvs} or PATH")
        cmd = prefix + ["-w", str(mvs_dir), "--max-threads", str(mvs_threads), str(dense_mvs)]

        logger.info(f"Starting ReconstructMesh")
        logger.info(f"  Working dir: {mvs_dir}")
        logger.info(f"  Input: {dense_mvs} ({dense_mvs.stat().st_size} bytes)")
        logger.info(f"  Expected output: {mesh_ply}")
        logger.info(f"Command: {' '.join(cmd)}")

        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.stdout:
            logger.info(f"ReconstructMesh stdout:\n{r.stdout}")
        if r.stderr:
            logger.info(f"ReconstructMesh stderr:\n{r.stderr}")
        if r.returncode != 0:
            logger.error(f"ReconstructMesh failed with exit code {r.returncode}")
            for f in sorted(mvs_dir.iterdir()):
                logger.info(f"  {f.name} ({f.stat().st_size} bytes)")
            raise RuntimeError(f"ReconstructMesh failed: {r.stderr}")
        time.sleep(1)
        if not mesh_ply.exists():
            logger.error(f"ReconstructMesh completed (exit 0) but output not found: {mesh_ply}")
            for f in sorted(mvs_dir.iterdir()):
                logger.info(f"  {f.name} ({f.stat().st_size} bytes)")
            raise RuntimeError(f"ReconstructMesh failed: output not created at {mesh_ply}")
        logger.info(f"ReconstructMesh successful: {mesh_ply} ({mesh_ply.stat().st_size} bytes)")
    else:
        logger.info(f"Mesh already exists: {mesh_ply} ({mesh_ply.stat().st_size} bytes)")

    # 8c. RefineMesh
    tex_mesh_ply = mesh_ply
    tex_out_mvs = mvs_dir / "scene_dense_mesh_texture.mvs"

    if skip_refine:
        logger.info("skip_refine=True -> texturing the raw reconstructed mesh")
    else:
        refine_mvs = mvs_dir / "scene_dense_mesh_refine.mvs"
        refine_ply = mvs_dir / "scene_dense_mesh_refine.ply"
        _print_step("8c/9 RefineMesh")
        prefix = _resolve_tool(mvs, "RefineMesh")
        if not prefix:
            logger.warning("RefineMesh not found, skipping refinement")
        else:
            cmd = prefix + [
                "-w", str(mvs_dir),
                "--max-threads", str(mvs_threads),
                str(dense_mvs),
                "-m", str(mesh_ply),
                "-o", str(refine_mvs),
            ]
            logger.info(f"Command: {' '.join(cmd)}")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.stdout:
                logger.info(f"RefineMesh stdout:\n{r.stdout}")
            if r.stderr:
                logger.info(f"RefineMesh stderr:\n{r.stderr}")
            if r.returncode == 0 and refine_ply.exists():
                tex_mesh_ply = refine_ply
                tex_out_mvs = mvs_dir / "scene_dense_mesh_refine_texture.mvs"
                logger.info(f"RefineMesh successful: {refine_ply} ({refine_ply.stat().st_size} bytes)")
            else:
                logger.warning(
                    f"RefineMesh failed (exit {r.returncode}, output exists={refine_ply.exists()}), "
                    f"falling back to raw mesh"
                )

    # 9. TextureMesh
    _print_step("9/9 Texture mesh")
    tex_out_ply = tex_out_mvs.with_suffix(".ply")
    if not tex_out_mvs.exists() and not tex_out_ply.exists():
        prefix = _resolve_tool(mvs, "TextureMesh")
        if not prefix:
            raise RuntimeError(f"TextureMesh not found in {mvs} or PATH")
        cmd = prefix + [
            "-w", str(mvs_dir),
            "--max-threads", str(mvs_threads),
            str(dense_mvs),
            "-m", str(tex_mesh_ply),
            "-o", str(tex_out_mvs),
        ]
        logger.info(f"Command: {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.stdout:
            logger.info(f"TextureMesh stdout:\n{r.stdout}")
        if r.stderr:
            logger.info(f"TextureMesh stderr:\n{r.stderr}")
        if r.returncode != 0:
            logger.error(f"TextureMesh failed with exit code {r.returncode}")
            for f in sorted(mvs_dir.iterdir()):
                logger.info(f"  {f.name} ({f.stat().st_size} bytes)")
            raise RuntimeError(f"TextureMesh failed: {r.stderr}")
        time.sleep(1)
        if not tex_out_mvs.exists() and not tex_out_ply.exists():
            for f in sorted(mvs_dir.iterdir()):
                logger.info(f"  {f.name} ({f.stat().st_size} bytes)")
            raise RuntimeError(f"TextureMesh failed: output file not created")
        if tex_out_mvs.exists():
            logger.info(f"TextureMesh created MVS: {tex_out_mvs} ({tex_out_mvs.stat().st_size} bytes)")
        if tex_out_ply.exists():
            logger.info(f"TextureMesh created PLY: {tex_out_ply} ({tex_out_ply.stat().st_size} bytes)")

    # GLB conversion
    glb_path = output_dir / glb_name
    ply_candidates = list(mvs_dir.glob("*_texture*.ply")) + list(mvs_dir.glob("*refine*.ply"))
    if ply_candidates:
        tex_out_ply_actual = max(ply_candidates, key=lambda p: p.stat().st_mtime)
    else:
        tex_out_ply_actual = tex_out_ply

    if tex_out_ply_actual.exists():
        try:
            convert_textured_to_glb(tex_out_ply_actual, glb_path)
        except Exception as e:
            logger.warning(f"GLB conversion failed: {e}")
            import traceback
            logger.warning(traceback.format_exc())
    else:
        logger.warning(f"Cannot convert to GLB: textured PLY not found at {tex_out_ply_actual}")

    actual_texture_mvs = str(tex_out_mvs) if tex_out_mvs.exists() else None
    actual_texture_ply = str(tex_out_ply_actual) if tex_out_ply_actual.exists() else None
    actual_glb = str(glb_path) if glb_path.exists() else None
    result_path = actual_glb or actual_texture_ply or actual_texture_mvs or str(dense_mvs)

    return {
        "dense_mvs": str(dense_mvs),
        "mesh_ply": str(tex_mesh_ply),
        "texture_mvs": actual_texture_mvs,
        "texture_ply": actual_texture_ply,
        "glb_path": actual_glb,
        "result_path": result_path,
    }


# ---------------------------------------------------------------------------
# OpenMVG-based pipeline (spherical / pinhole)
# ---------------------------------------------------------------------------

def run_full_pipeline(
    image_dir: str,
    output_dir: str,
    camera_type: str = "spherical",
    resize_long_edge: int = 4096,
    cubic_size: int = 1600,
    pair_window: int = 0,
    skip_refine: bool = False,
    clean_mvs: bool = False,
    initial_pair_a: Optional[str] = None,
    initial_pair_b: Optional[str] = None,
    sfm_engine: str = "INCREMENTAL",
    force_recompute: int = 0,
    focal_pix: Optional[str] = None,
    feature_method: str = "SIFT",
    nearest_matching: str = "AUTO",
    openmvs_max_threads: Optional[int] = None,
    openmvs_densify_extra: Optional[str] = None,
    openmvs_keep_depth_maps: Optional[bool] = None,
) -> dict:
    """Run the complete OpenMVG + OpenMVS pipeline (spherical or pinhole)."""
    image_dir = Path(image_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    is_spherical = camera_type == "spherical"
    camera_model = CAMERA_MODEL_SPHERICAL if is_spherical else CAMERA_MODEL_PINHOLE

    threads = getattr(settings, "WORKER_THREADS", 4)
    mvs_threads = openmvs_max_threads or getattr(settings, "OPENMVS_MAX_THREADS", None) or threads
    densify_extra = openmvs_densify_extra or getattr(settings, "OPENMVS_DENSIFY_EXTRA", "")
    keep_depth_maps = openmvs_keep_depth_maps if openmvs_keep_depth_maps is not None else getattr(
        settings, "OPENMVS_KEEP_DEPTH_MAPS", False
    )

    mvg = Path(settings.OPENMVG_BIN)
    mvs = Path(settings.OPENMVS_BIN)
    cam_db = Path(getattr(settings, "CAM_DB",
        "/root/work/openMVG/src/openMVG/exif/sensor_width_database/sensor_width_camera_database.txt"))

    openmvg_sfm_init = mvg / "openMVG_main_SfMInit_ImageListing"
    openmvg_features = mvg / "openMVG_main_ComputeFeatures"
    openmvg_matches = mvg / "openMVG_main_ComputeMatches"
    openmvg_filter = mvg / "openMVG_main_GeometricFilter"
    openmvg_sfm = mvg / "openMVG_main_SfM"
    openmvg_colorize = mvg / "openMVG_main_ComputeSfM_DataColor"
    openmvg_spherical2cubic = mvg / "openMVG_main_openMVGSpherical2Cubic"
    openmvg_to_mvs = mvg / "openMVG_main_openMVG2openMVS"
    openmvg_robust = mvg / "openMVG_main_ComputeStructureFromKnownPoses"

    matches_dir = output_dir / "matches"
    sfm_dir = output_dir / ("sfm" if is_spherical else "reconstruction_sequential")
    cubic_dir = output_dir / "cubic"
    mvs_dir = output_dir / "mvs"
    work_images_dir = output_dir / "images_sfm"

    matches_dir.mkdir(parents=True, exist_ok=True)
    sfm_dir.mkdir(parents=True, exist_ok=True)
    mvs_dir.mkdir(parents=True, exist_ok=True)

    _print_step("Verifying executables")
    _require_exec(openmvg_sfm_init, "SfMInit")
    _require_exec(openmvg_features, "ComputeFeatures")
    _require_exec(openmvg_matches, "ComputeMatches")
    _require_exec(openmvg_filter, "GeometricFilter")
    _require_exec(openmvg_sfm, "SfM")
    _require_exec(openmvg_colorize, "ComputeSfM_DataColor")
    _require_exec(openmvg_to_mvs, "openMVG2openMVS")
    if is_spherical:
        _require_exec(openmvg_spherical2cubic, "Spherical2Cubic")
    else:
        _require_exec(openmvg_robust, "ComputeStructureFromKnownPoses")

    logger.info("Checking OpenMVS binaries...")
    for tool in ["DensifyPointCloud", "ReconstructMesh", "RefineMesh", "TextureMesh"]:
        if _resolve_tool(mvs, tool):
            logger.info(f"  Found {tool}")
        else:
            logger.warning(f"  {tool} not found in {mvs} or PATH")

    _require_file(image_dir, "image directory")
    if not is_spherical:
        _require_file(cam_db, "camera database")

    exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
    images = sorted([f for f in image_dir.iterdir() if f.suffix.lower() in exts])
    if not images:
        raise RuntimeError(f"No input images found in {image_dir}")

    _print_step("Input")
    logger.info(f"Images      : {image_dir}")
    logger.info(f"Output      : {output_dir}")
    logger.info(f"Cam model   : {camera_model}")
    logger.info(f"Features    : {feature_method}")
    logger.info(f"Input count : {len(images)}")
    if is_spherical:
        logger.info(f"Resize 2:1  : {resize_long_edge}px long-edge (0 = off)")
        logger.info(f"Cubic face  : {cubic_size}px (0 = auto)")
        logger.info(f"SfM engine  : {sfm_engine}")
        if initial_pair_a and initial_pair_b:
            logger.info(f"Initial pair: {initial_pair_a} + {initial_pair_b}")
    logger.info(f"Pair window : {pair_window} (0 = all pairs)")

    # 0. Optional: resize equirectangular images
    sfm_image_dir = image_dir
    if is_spherical and resize_long_edge > 0:
        _print_step(f"0/9 Resize equirectangular images to {resize_long_edge}px")
        _resize_equirectangular(image_dir, work_images_dir, resize_long_edge)
        sfm_image_dir = work_images_dir

    # 1. SfMInit
    _print_step("1/9 SfMInit")
    sfm_data_json = matches_dir / "sfm_data.json"
    if not sfm_data_json.exists():
        cmd = [str(openmvg_sfm_init),
               "-i", str(sfm_image_dir),
               "-o", str(matches_dir),
               "-c", str(camera_model)]
        if is_spherical:
            cmd.extend(["-f", "1"])
        else:
            if focal_pix:
                cmd.extend(["-f", focal_pix])
            if cam_db.exists():
                cmd.extend(["-d", str(cam_db)])
        _run(cmd, "1/9 SfMInit")
    else:
        logger.info("SfM data already exists, skipping initialization")

    # 2. Compute features
    _print_step("2/9 Compute features")
    features_bin = matches_dir / "image_descrips.bin"
    if not features_bin.exists() or force_recompute:
        _run([str(openmvg_features),
              "-i", str(sfm_data_json),
              "-o", str(matches_dir),
              "-m", feature_method,
              "-n", str(threads),
              "-f", str(force_recompute)], "2/9 ComputeFeatures")
    else:
        logger.info("Features already computed, skipping")

    # 2b. Optional sequential pair list
    pair_list = None
    if pair_window > 0:
        pair_list = matches_dir / "pairs.txt"
        if not pair_list.exists():
            _print_step(f"2b/9 Build sequential pair list (window={pair_window})")
            _build_pair_list(sfm_data_json, pair_list, pair_window)
        else:
            logger.info("Pair list already exists")

    # 3. Putative matches
    _print_step("3/9 Compute putative matches")
    putative = matches_dir / "matches.putative.bin"
    if not putative.exists() or force_recompute:
        cmd = [str(openmvg_matches),
               "-i", str(sfm_data_json),
               "-o", str(putative),
               "-n", nearest_matching,
               "-f", str(force_recompute)]
        if pair_list and pair_list.exists():
            cmd.extend(["-p", str(pair_list)])
        _run(cmd, "3/9 ComputeMatches")
    else:
        logger.info("Putative matches already computed, skipping")

    # 4. Geometric filtering
    if is_spherical:
        geo_flag, geo_matches = "a", matches_dir / "matches.e.bin"
    else:
        geo_flag, geo_matches = "f", matches_dir / "matches.f.bin"
    _print_step(f"4/9 Geometric filtering ({'angular essential' if is_spherical else 'fundamental'}, -g {geo_flag})")
    if not geo_matches.exists():
        if not putative.exists():
            raise RuntimeError(f"Putative matches not found: {putative}. ComputeMatches may have failed.")
        _run([str(openmvg_filter),
              "-i", str(sfm_data_json),
              "-m", str(putative),
              "-g", geo_flag,
              "-o", str(geo_matches)], "4/9 GeometricFilter")
    else:
        logger.info("Geometric filtering already done, skipping")

    # 5. Structure from Motion (with GLOBAL -> INCREMENTAL fallback)
    _print_step(f"5/9 Structure from Motion ({sfm_engine})")
    sfm_bin = sfm_dir / "sfm_data.bin"
    if not sfm_bin.exists():
        if not geo_matches.exists():
            raise RuntimeError(f"Matches file not found: {geo_matches}. Geometric filtering may have failed.")

        def _build_sfm_cmd(engine: str) -> list:
            cmd = [str(openmvg_sfm),
                   "--sfm_engine", engine,
                   "--input_file", str(sfm_data_json),
                   "--match_dir", str(matches_dir),
                   "--output_dir", str(sfm_dir)]
            if is_spherical:
                cmd.extend(["--match_file", str(geo_matches)])
            if engine.startswith("INCREMENTAL") and initial_pair_a and initial_pair_b:
                cmd.extend(["-a", initial_pair_a, "-b", initial_pair_b])
            return cmd

        def _run_sfm(engine: str):
            cmd = _build_sfm_cmd(engine)
            logger.info(f"5/9 SfM ({engine}): {' '.join(str(c) for c in cmd)}")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.stdout:
                logger.info(r.stdout)
            if r.stderr:
                logger.info(f"SfM stderr:\n{r.stderr}")
            return r

        r = _run_sfm(sfm_engine)
        if r.returncode != 0:
            combined = (r.stdout or "") + "\n" + (r.stderr or "")
            cholesky_fail = (
                "Cholesky" in combined
                or "Linear solver failure" in combined
                or "translation averaging" in combined.lower()
            )
            if sfm_engine == "GLOBAL" and cholesky_fail:
                logger.warning(
                    "GLOBAL SfM failed with linear solver / Cholesky factorization. "
                    "Falling back to INCREMENTAL."
                )
                for stale in sfm_dir.glob("*"):
                    try:
                        if stale.is_file():
                            stale.unlink()
                    except OSError:
                        pass
                r = _run_sfm("INCREMENTAL")
                if r.returncode != 0:
                    raise RuntimeError(
                        "SfM failed on both GLOBAL and INCREMENTAL fallback.\n"
                        "Try a different pair_window (e.g. 8 or 20), set an explicit "
                        "initial_pair_a/initial_pair_b on two well-overlapping frames, "
                        "or reduce frame count.\nLast stderr:\n" + (r.stderr or "")
                    )
            else:
                hint = ""
                if cholesky_fail:
                    hint = (
                        "\nHint: linear solver failure typically indicates degenerate "
                        "camera motion. Try sfm_engine=INCREMENTAL with an explicit "
                        "initial_pair_a/initial_pair_b."
                    )
                elif "Connected component of size: 1" in combined or "Connected component of size: 2" in combined:
                    hint = (
                        "\nHint: the match graph is tiny. Increase pair_window, "
                        "or check that the input frames overlap."
                    )
                raise RuntimeError(f"5/9 SfM ({sfm_engine}) failed:\n{r.stderr}{hint}")
    else:
        logger.info("Reconstruction already exists, skipping")

    if not sfm_bin.exists():
        raise RuntimeError(f"SfM failed: {sfm_bin} not found")

    # Sanity check on registration
    report_html = sfm_dir / "SfMReconstruction_Report.html"
    if report_html.exists():
        try:
            text = report_html.read_text(errors="ignore")
            m_views = re.search(r"#views:\s*(\d+)", text)
            m_poses = re.search(r"#poses:\s*(\d+)", text)
            m_tracks = re.search(r"#tracks:\s*(\d+)", text)
            n_views = int(m_views.group(1)) if m_views else len(images)
            n_poses = int(m_poses.group(1)) if m_poses else 0
            n_tracks = int(m_tracks.group(1)) if m_tracks else 0
            logger.info(
                f"SfM stats: views={n_views} poses={n_poses} tracks={n_tracks} "
                f"({100.0 * n_poses / max(n_views, 1):.1f}% registered)"
            )
            min_poses = 8 if is_spherical else 12
            if n_poses < min_poses:
                raise RuntimeError(
                    f"SfM registered only {n_poses}/{n_views} views (tracks={n_tracks}). "
                    f"This is too few for dense MVS — DensifyPointCloud will produce 0 points "
                    f"and ReconstructMesh will emit an empty mesh.\n"
                    f"Try one of:\n"
                    f"  * pair_window=12 (for sequential video frames)\n"
                    f"  * sfm_engine='GLOBAL'\n"
                    f"  * initial_pair_a/initial_pair_b set to two well-overlapping frames\n"
                    f"  * fewer / better-spaced input frames"
                )
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning(f"Could not parse SfM report for sanity check: {e}")

    # 5b. Colorize sparse cloud
    _print_step("5b/9 Colorize sparse cloud")
    colorized = sfm_dir / "colorized.ply"
    if not colorized.exists():
        _run_best_effort([str(openmvg_colorize),
                          "-i", str(sfm_bin),
                          "-o", str(colorized)], "5b/9 Colorize")
    else:
        logger.info("Colorized point cloud already exists")

    # 5c. Robust re-triangulation (pinhole only)
    if not is_spherical:
        _print_step("5c/9 Robust re-triangulation")
        robust = sfm_dir / "robust.ply"
        if not robust.exists():
            _run_best_effort([str(openmvg_robust),
                              "-i", str(sfm_bin),
                              "-m", str(matches_dir),
                              "-o", str(robust)], "5c/9 Robust")
        else:
            logger.info("Robust point cloud already exists")

    # 6. Spherical -> Cubic (spherical only)
    if is_spherical:
        _print_step("6/9 Spherical to Cubic")
        cubic_dir.mkdir(parents=True, exist_ok=True)
        perspective_bin = cubic_dir / "sfm_data_perspective.bin"
        if not perspective_bin.exists():
            _run([str(openmvg_spherical2cubic),
                  "-i", str(sfm_bin),
                  "-o", str(cubic_dir),
                  "-s", str(cubic_size)], "6/9 Spherical2Cubic")
        else:
            logger.info("Cubic conversion already done")
        export_input = perspective_bin
    else:
        export_input = sfm_bin

    # 7. Export to OpenMVS
    _print_step("7/9 Export to OpenMVS")
    scene_mvs = mvs_dir / "scene.mvs"
    if not scene_mvs.exists():
        _run([str(openmvg_to_mvs),
              "-i", str(export_input),
              "-o", str(scene_mvs),
              "-d", str(mvs_dir / "images")], "7/9 Export to OpenMVS")
    else:
        logger.info("OpenMVS scene already exists")

    # 8-9. Dense + mesh + refine + texture + GLB
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

    _print_step("Done")
    logger.info(f"Sparse SfM         : {sfm_bin}")
    logger.info(f"Sparse colorized   : {colorized}")
    if is_spherical:
        logger.info(f"Cubic (perspective): {cubic_dir / 'sfm_data_perspective.bin'}")
    logger.info(f"OpenMVS scene      : {scene_mvs}")
    logger.info(f"Dense scene        : {mvs_result['dense_mvs']}")
    logger.info(f"Textured scene     : {mvs_result.get('texture_mvs')}")
    logger.info(f"GLB                : {mvs_result.get('glb_path')}")

    return {
        "sfm_bin": str(sfm_bin),
        "colorized_ply": str(colorized),
        "scene_mvs": str(scene_mvs),
        **mvs_result,
    }


def run_spherical_pipeline(image_dir: str, output_dir: str, **kwargs) -> dict:
    """Convenience wrapper with spherical defaults."""
    defaults = {
        "camera_type": "spherical",
        "resize_long_edge": 4096,
        "cubic_size": 1600,
        "pair_window": 0,
        "skip_refine": False,
        "clean_mvs": False,
        "sfm_engine": "INCREMENTAL",
        "force_recompute": 0,
        "feature_method": "SIFT",
        "nearest_matching": "AUTO",
    }
    defaults.update(kwargs)
    return run_full_pipeline(image_dir, output_dir, **defaults)


def run_pinhole_pipeline(image_dir: str, output_dir: str, **kwargs) -> dict:
    """Convenience wrapper with pinhole defaults."""
    defaults = {
        "camera_type": "pinhole",
        "resize_long_edge": 0,
        "cubic_size": 0,
        "pair_window": 0,
        "skip_refine": False,
        "clean_mvs": False,
        "sfm_engine": "INCREMENTAL",
        "force_recompute": 1,
        "feature_method": "SIFT",
        "nearest_matching": "AUTO",
    }
    defaults.update(kwargs)
    return run_full_pipeline(image_dir, output_dir, **defaults)
