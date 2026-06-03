import uuid
import logging
import logging.handlers
import shutil
from pathlib import Path
from sqlalchemy.orm import Session

from models.database import Task, TaskStatus, PipelineType
from pipelines import (
    extract_frames,
    run_full_pipeline,
    run_gaussian_pipeline,
    run_sphere_colmap_pipeline,
    run_colmap360_openmvs,
    run_colmap360_3dgs,
)
from config.settings import settings
from services.task_notify import commit_and_notify

LOGS_DIR = Path(settings.RESULTS_DIR) / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def setup_task_logger(task_id: str) -> logging.Logger:
    logger = logging.getLogger(task_id)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    log_file = LOGS_DIR / f"{task_id}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def _apply_colmap360_result(task: Task, result: dict, logger: logging.Logger) -> None:
    from services.panorama_index import build_panorama_index

    picked = result.get("result_path")
    if picked and Path(picked).exists():
        task.result_path = picked
    else:
        raise RuntimeError("COLMAP360 pipeline finished without a result file")
    meta = dict(task.extra_data or {})
    meta.update({
        "colmap_dir": result.get("colmap_dir"),
        "erp_frames_dir": result.get("erp_frames_dir"),
        "viewer_type": result.get("viewer_type"),
    })
    if result.get("gaussian_model_path"):
        task.gaussian_model_path = result["gaussian_model_path"]
        meta["gaussian_model_path"] = result["gaussian_model_path"]
    if result.get("glb_path"):
        meta["glb_path"] = result["glb_path"]
    if result.get("ply_path"):
        meta["ply_path"] = result["ply_path"]
    colmap_dir = result.get("colmap_dir")
    if colmap_dir:
        try:
            manifest_path = build_panorama_index(
                task.id,
                Path(colmap_dir),
                result_path=picked,
                viewer_type=result.get("viewer_type") or "mesh",
            )
            meta["manifest_path"] = str(manifest_path)
            logger.info("Panorama index: %s (%d frames)", manifest_path, len(
                __import__("json").loads(manifest_path.read_text()).get("frames", [])
            ))
        except Exception as e:
            logger.warning("Panorama index failed: %s", e)
    task.extra_data = meta
    logger.info("COLMAP360 completed: %s", task.result_path)


def _pick_result_path(logger: logging.Logger, result: dict) -> str | None:
    """Pick the best result file from a pipeline result dict (GLB > PLY > MVS > dense)."""
    glb_path = result.get("glb_path")
    texture_ply = result.get("texture_ply")
    texture_mvs = result.get("texture_mvs")
    dense_mvs = result.get("dense_mvs")
    result_path = result.get("result_path")

    logger.info("Checking result files:")
    logger.info(f"  glb_path={glb_path}, exists={Path(glb_path).exists() if glb_path else False}")
    logger.info(f"  texture_ply={texture_ply}, exists={Path(texture_ply).exists() if texture_ply else False}")
    logger.info(f"  texture_mvs={texture_mvs}, exists={Path(texture_mvs).exists() if texture_mvs else False}")
    logger.info(f"  dense_mvs={dense_mvs}, exists={Path(dense_mvs).exists() if dense_mvs else False}")

    if glb_path and Path(glb_path).exists():
        logger.info(f"Using GLB result: {glb_path}")
        return glb_path
    if texture_ply and Path(texture_ply).exists():
        logger.info(f"Using textured PLY result: {texture_ply}")
        return texture_ply
    if texture_mvs and Path(texture_mvs).exists():
        logger.info(f"Using textured MVS result: {texture_mvs}")
        return texture_mvs
    if dense_mvs and Path(dense_mvs).exists():
        logger.warning(f"Using fallback dense_mvs result: {dense_mvs}")
        return dense_mvs
    if result_path and Path(result_path).exists():
        logger.info(f"Using result_path: {result_path}")
        return result_path
    return None


def process_task(task_id: str, video_path: str, db: Session):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return

    logger = setup_task_logger(task_id)
    task_dir = Path(settings.RESULTS_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        task.status = TaskStatus.PROCESSING
        commit_and_notify(db, task)
        logger.info(f"Task {task_id} started (pipeline: {task.pipeline_type.value})")

        colmap360 = task.pipeline_type in (
            PipelineType.COLMAP360_OPENMVS,
            PipelineType.COLMAP360_3DGS,
        )
        frames_dir = str(task_dir / "frames")
        if colmap360:
            task.progress = 0.05
            commit_and_notify(db, task)
            logger.info("COLMAP360: видео передаётся в shell-пайплайн без отдельного extract_frames")
        elif task.frames_path and Path(task.frames_path).exists():
            frame_count = len(list(Path(task.frames_path).glob("*.jpg")))
            task.frames_count = frame_count
            task.progress = 0.2
            commit_and_notify(db, task)
            frames_dir = task.frames_path
            logger.info(f"Skipping frame extraction, found {frame_count} frames")
        else:
            frames_dir, frames_count = extract_frames(video_path, frames_dir)
            task.frames_path = frames_dir
            task.frames_count = frames_count
            task.progress = 0.2
            commit_and_notify(db, task)
            logger.info(f"Extracted {frames_count} frames")

        # Common helpers for reading task-level overrides from task.extra_data
        # with env-var and default fallbacks. extra_data is JSON written by the
        # API layer (routes/tasks.py).
        import os
        extra = task.extra_data or {}
        frames_count = task.frames_count or 0

        def _opt(key: str, env: str, default, cast=None):
            if key in extra and extra[key] is not None:
                val = extra[key]
            else:
                val = os.getenv(env, default)
            if cast is not None and val is not None and not isinstance(val, cast):
                val = cast(val)
            return val

        def _opt_bool(key: str, env: str, default: bool = False) -> bool:
            if isinstance(extra.get(key), bool):
                return extra[key]
            return os.getenv(env, "1" if default else "0") == "1"

        if task.pipeline_type == PipelineType.OPENMVG_OPENMVS:
            logger.info("Starting full openMVG+openMVS pipeline")

            camera_type = _opt("camera_type", "PIPELINE_CAMERA_TYPE", "spherical", str)
            default_resize = "4096" if camera_type == "spherical" else "0"
            resize_long_edge = int(_opt("resize_long_edge", "RESIZE_LONG_EDGE", default_resize))
            cubic_size = int(_opt("cubic_size", "CUBIC_SIZE", "1600"))

            # Sequential video frames -> default to a sliding window matcher.
            default_pair_window = 12 if frames_count >= 30 else 0
            pair_window = int(_opt("pair_window", "PAIR_WINDOW", default_pair_window))

            skip_refine = _opt_bool("skip_refine", "SKIP_REFINE")
            clean_mvs = _opt_bool("clean_mvs", "CLEAN_MVS")
            force_recompute = int(_opt("force_recompute", "FORCE_RECOMPUTE", "0"))
            sfm_engine = _opt("sfm_engine", "SFM_ENGINE", "INCREMENTAL", str)
            initial_pair_a = extra.get("initial_pair_a") or os.getenv("INITIAL_PAIR_A") or None
            initial_pair_b = extra.get("initial_pair_b") or os.getenv("INITIAL_PAIR_B") or None
            openmvs_densify_extra = extra.get("openmvs_densify_extra") or os.getenv("OPENMVS_DENSIFY_EXTRA") or None
            openmvs_keep_depth_maps = _opt_bool("openmvs_keep_depth_maps", "OPENMVS_KEEP_DEPTH_MAPS")
            openmvs_max_threads = (
                int(extra["openmvs_max_threads"]) if extra.get("openmvs_max_threads")
                else int(os.getenv("OPENMVS_MAX_THREADS")) if os.getenv("OPENMVS_MAX_THREADS") else None
            )

            logger.info(
                f"Pipeline config: camera_type={camera_type}, resize={resize_long_edge}, "
                f"cubic_size={cubic_size}, pair_window={pair_window} "
                f"(default for {frames_count} frames was {default_pair_window}), "
                f"sfm_engine={sfm_engine}, initial_pair=({initial_pair_a},{initial_pair_b}), "
                f"skip_refine={skip_refine}, clean_mvs={clean_mvs}, force_recompute={force_recompute}"
            )

            result = run_full_pipeline(
                image_dir=frames_dir,
                output_dir=str(task_dir / "reconstruction"),
                camera_type=camera_type,
                resize_long_edge=resize_long_edge,
                cubic_size=cubic_size,
                pair_window=pair_window,
                skip_refine=skip_refine,
                clean_mvs=clean_mvs,
                sfm_engine=sfm_engine,
                force_recompute=force_recompute,
                initial_pair_a=initial_pair_a,
                initial_pair_b=initial_pair_b,
                openmvs_densify_extra=openmvs_densify_extra,
                openmvs_keep_depth_maps=openmvs_keep_depth_maps,
                openmvs_max_threads=openmvs_max_threads,
            )

            logger.info(f"Pipeline result: {result}")
            picked = _pick_result_path(logger, result)
            if not picked:
                logger.error("No valid result file found!")
                logger.error(f"Result dict: {result}")
                raise RuntimeError("Pipeline completed but no result file was created")
            task.result_path = picked
            task.openmvg_matches_path = result.get("sfm_bin")
            task.openmvs_scene_path = result.get("dense_mvs")
            task.progress = 0.9
            commit_and_notify(db, task)
            logger.info(f"openMVG+openMVS completed, result saved: {task.result_path}")

        elif task.pipeline_type == PipelineType.SPHERE_COLMAP_OPENMVS:
            logger.info("Starting SphereSfM(COLMAP) + OpenMVS pipeline")

            resize_long_edge = int(_opt("resize_long_edge", "RESIZE_LONG_EDGE", "4096"))
            matcher = _opt("matcher", "MATCHER", "sequential", str)
            sequential_overlap = int(_opt("sequential_overlap", "SEQUENTIAL_OVERLAP", "12"))
            cubic_face_size = int(_opt("cubic_face_size", "CUBIC_FACE_SIZE", "0"))
            cubic_field_of_view = float(_opt("cubic_field_of_view", "CUBIC_FIELD_OF_VIEW", "90"))
            use_gpu = _opt_bool("use_gpu", "USE_GPU", default=True)
            min_num_matches = int(_opt("min_num_matches", "MIN_NUM_MATCHES", "15"))
            skip_refine = _opt_bool("skip_refine", "SKIP_REFINE")
            clean_mvs = _opt_bool("clean_mvs", "CLEAN_MVS")
            openmvs_densify_extra = extra.get("openmvs_densify_extra") or os.getenv("OPENMVS_DENSIFY_EXTRA") or None
            openmvs_keep_depth_maps = _opt_bool("openmvs_keep_depth_maps", "OPENMVS_KEEP_DEPTH_MAPS")
            openmvs_max_threads = (
                int(extra["openmvs_max_threads"]) if extra.get("openmvs_max_threads")
                else int(os.getenv("OPENMVS_MAX_THREADS")) if os.getenv("OPENMVS_MAX_THREADS") else None
            )

            logger.info(
                f"SphereSfM config: resize={resize_long_edge}, matcher={matcher}, "
                f"sequential_overlap={sequential_overlap}, cubic_face_size={cubic_face_size}, "
                f"cubic_fov={cubic_field_of_view}, use_gpu={use_gpu}, "
                f"skip_refine={skip_refine}, clean_mvs={clean_mvs}"
            )

            result = run_sphere_colmap_pipeline(
                image_dir=frames_dir,
                output_dir=str(task_dir / "reconstruction"),
                resize_long_edge=resize_long_edge,
                matcher=matcher,
                sequential_overlap=sequential_overlap,
                cubic_face_size=cubic_face_size,
                cubic_field_of_view=cubic_field_of_view,
                skip_refine=skip_refine,
                clean_mvs=clean_mvs,
                use_gpu=use_gpu,
                min_num_matches=min_num_matches,
                openmvs_max_threads=openmvs_max_threads,
                openmvs_densify_extra=openmvs_densify_extra,
                openmvs_keep_depth_maps=openmvs_keep_depth_maps,
            )

            logger.info(f"Pipeline result: {result}")
            picked = _pick_result_path(logger, result)
            if not picked:
                logger.error("No valid result file found!")
                logger.error(f"Result dict: {result}")
                raise RuntimeError("Pipeline completed but no result file was created")
            task.result_path = picked
            task.openmvg_matches_path = result.get("sparse_dir")
            task.openmvs_scene_path = result.get("dense_mvs")
            task.progress = 0.9
            commit_and_notify(db, task)
            logger.info(f"SphereSfM+OpenMVS completed, result saved: {task.result_path}")

        elif task.pipeline_type == PipelineType.GAUSSIAN_SPLATTING:
            if task.gaussian_model_path and Path(task.gaussian_model_path).exists():
                task.progress = 0.9
                commit_and_notify(db, task)
                logger.info("Skipping Gaussian Splatting, already completed")
            else:
                logger.info("Starting COLMAP + Gaussian Splatting pipeline")
                gaussian_result = run_gaussian_pipeline(frames_dir, task_id)
                task.gaussian_model_path = gaussian_result
                task.result_path = gaussian_result
                task.progress = 0.9
                commit_and_notify(db, task)
                logger.info("Gaussian Splatting completed")

        elif task.pipeline_type == PipelineType.COLMAP360_OPENMVS:
            logger.info("COLMAP360 + OpenMVS")
            fps = int(_opt("fps", "FFMPEG_FPS", settings.FFMPEG_FPS))
            mask_bottom = float(_opt("mask_bottom", "MASK_BOTTOM", 0.0))
            use_gpu = _opt_bool("use_gpu", "USE_GPU", default=False)
            skip_refine = _opt_bool("skip_refine", "SKIP_REFINE")
            task.progress = 0.1
            commit_and_notify(db, task)
            result = run_colmap360_openmvs(
                video_path,
                str(task_dir),
                fps=fps,
                mask_bottom=mask_bottom,
                use_gpu=use_gpu,
                skip_refine=skip_refine,
            )
            task.progress = 0.9
            commit_and_notify(db, task)
            _apply_colmap360_result(task, result, logger)

        elif task.pipeline_type == PipelineType.COLMAP360_3DGS:
            logger.info("COLMAP360 + 3DGS")
            fps = int(_opt("fps", "FFMPEG_FPS", settings.FFMPEG_FPS))
            mask_bottom = float(_opt("mask_bottom", "MASK_BOTTOM", 0.0))
            use_gpu = _opt_bool("use_gpu", "USE_GPU", default=False)
            task.progress = 0.1
            commit_and_notify(db, task)
            result = run_colmap360_3dgs(
                video_path,
                str(task_dir),
                fps=fps,
                mask_bottom=mask_bottom,
                use_gpu=use_gpu,
            )
            task.progress = 0.9
            commit_and_notify(db, task)
            _apply_colmap360_result(task, result, logger)

        task.status = TaskStatus.COMPLETED
        task.progress = 1.0
        commit_and_notify(db, task)
        logger.info(f"Task {task_id} completed successfully")

    except Exception as e:
        logger.exception(f"Task {task_id} failed")
        task.status = TaskStatus.FAILED
        task.error_message = str(e)[:2048]
        commit_and_notify(db, task)
