import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Optional

from config.settings import settings
from database import SessionLocal
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel
from enum import Enum
from models.database import PipelineType, TaskStatus
from services.task_events import subscribe, unsubscribe
from services.task_notify import commit_and_notify, notify_task
from services.task_service import create_task, get_result_path, get_task
from services.cache import tasks_cache
from services.task_results import result_file_url

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


# Reconstruction overrides surfaced on the API. Stored on Task.extra_data and
# consumed by services.task_worker.process_task. Anything not provided here
# falls back to env vars and then to defaults inside the worker.
COLMAP360_PARAM_KEYS = {"fps", "mask_bottom", "use_gpu", "skip_refine"}

RECONSTRUCTION_PARAM_KEYS = {
    # openMVG/openMVS pipeline
    "camera_type",
    "resize_long_edge",
    "cubic_size",
    "pair_window",
    "skip_refine",
    "clean_mvs",
    "force_recompute",
    "sfm_engine",
    "initial_pair_a",
    "initial_pair_b",
    "openmvs_densify_extra",
    "openmvs_keep_depth_maps",
    "openmvs_max_threads",
    # SphereSfM/COLMAP + openMVS pipeline
    "matcher",
    "sequential_overlap",
    "cubic_face_size",
    "cubic_field_of_view",
    "use_gpu",
    "min_num_matches",
}


def _coerce_param(key: str, value):
    if value is None:
        return None
    if key in {"fps", "resize_long_edge", "cubic_size", "pair_window", "force_recompute",
               "openmvs_max_threads", "sequential_overlap", "cubic_face_size", "min_num_matches"}:
        return int(value)
    if key in {"cubic_field_of_view", "mask_bottom"}:
        return float(value)
    if key in {"skip_refine", "clean_mvs", "openmvs_keep_depth_maps", "use_gpu"}:
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}
    return value


def _validate_reconstruction_params(raw: dict) -> dict:
    cleaned: dict = {}
    unknown: list = []
    for k, v in raw.items():
        if v is None or v == "":
            continue
        allowed = RECONSTRUCTION_PARAM_KEYS | COLMAP360_PARAM_KEYS
        if k not in allowed:
            unknown.append(k)
            continue
        cleaned[k] = _coerce_param(k, v)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown reconstruction parameter(s): {', '.join(unknown)}. "
                   f"Allowed: {', '.join(sorted(RECONSTRUCTION_PARAM_KEYS))}",
        )
    if cleaned.get("camera_type") and cleaned["camera_type"] not in {"spherical", "pinhole"}:
        raise HTTPException(status_code=400, detail="camera_type must be 'spherical' or 'pinhole'")
    if cleaned.get("sfm_engine") and cleaned["sfm_engine"] not in {"INCREMENTAL", "INCREMENTALV2", "GLOBAL"}:
        raise HTTPException(status_code=400, detail="sfm_engine must be INCREMENTAL, INCREMENTALV2 or GLOBAL")
    if cleaned.get("matcher") and cleaned["matcher"] not in {"sequential", "exhaustive", "spatial"}:
        raise HTTPException(status_code=400, detail="matcher must be 'sequential', 'exhaustive' or 'spatial'")
    return cleaned


class TaskCreateRequest(BaseModel):
    pipeline: PipelineType


class TaskCreateResponse(BaseModel):
    task_id: str
    status: str


class RestartRequest(BaseModel):
    # Optional overrides; unset fields keep the existing extra_data value.
    camera_type: Optional[str] = None
    fps: Optional[int] = None
    mask_bottom: Optional[float] = None
    resize_long_edge: Optional[int] = None
    cubic_size: Optional[int] = None
    pair_window: Optional[int] = None
    skip_refine: Optional[bool] = None
    clean_mvs: Optional[bool] = None
    force_recompute: Optional[int] = None
    sfm_engine: Optional[str] = None
    initial_pair_a: Optional[str] = None
    initial_pair_b: Optional[str] = None
    openmvs_densify_extra: Optional[str] = None
    openmvs_keep_depth_maps: Optional[bool] = None
    openmvs_max_threads: Optional[int] = None
    # SphereSfM-only
    matcher: Optional[str] = None
    sequential_overlap: Optional[int] = None
    cubic_face_size: Optional[int] = None
    cubic_field_of_view: Optional[float] = None
    use_gpu: Optional[bool] = None
    min_num_matches: Optional[int] = None


@router.post("/", response_model=TaskCreateResponse)
async def create_task_endpoint(
    pipeline: str = Form(
        ...,
        description="colmap360_openmvs | colmap360_3dgs | openmvg_openmvs | sphere_colmap_openmvs | gaussian_splatting",
    ),
    file: UploadFile = File(
        description="Видеофайл для обработки (MP4, AVI, MOV)",
        content_type=["video/mp4", "video/x-msvideo", "video/quicktime"],
    ),
    # openMVG/openMVS overrides
    camera_type: Optional[str] = Form(None, description="'spherical' or 'pinhole'"),
    resize_long_edge: Optional[int] = Form(None),
    cubic_size: Optional[int] = Form(None),
    pair_window: Optional[int] = Form(None, description="Sequential match window. 0 = all pairs. Default 12 for 30+ frames."),
    skip_refine: Optional[bool] = Form(None),
    clean_mvs: Optional[bool] = Form(None),
    force_recompute: Optional[int] = Form(None),
    sfm_engine: Optional[str] = Form(None, description="INCREMENTAL, INCREMENTALV2 or GLOBAL"),
    initial_pair_a: Optional[str] = Form(None),
    initial_pair_b: Optional[str] = Form(None),
    # SphereSfM/COLMAP overrides
    matcher: Optional[str] = Form(None, description="sequential | exhaustive | spatial"),
    sequential_overlap: Optional[int] = Form(None),
    cubic_face_size: Optional[int] = Form(None),
    cubic_field_of_view: Optional[float] = Form(None),
    use_gpu: Optional[bool] = Form(None),
    min_num_matches: Optional[int] = Form(None),
    # COLMAP 360
    fps: Optional[int] = Form(None, description="ERP frames per second (default 2)"),
    mask_bottom: Optional[float] = Form(None, description="Helmet mask fraction 0..1"),
):
    db = SessionLocal()
    try:
        try:
            pipeline_type = PipelineType(pipeline)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Неверный тип обработки: {pipeline}. "
                       f"Доступные: {', '.join(p.value for p in PipelineType)}",
            )

        params = _validate_reconstruction_params({
            "camera_type": camera_type,
            "resize_long_edge": resize_long_edge,
            "cubic_size": cubic_size,
            "pair_window": pair_window,
            "skip_refine": skip_refine,
            "clean_mvs": clean_mvs,
            "force_recompute": force_recompute,
            "sfm_engine": sfm_engine,
            "initial_pair_a": initial_pair_a,
            "initial_pair_b": initial_pair_b,
            "matcher": matcher,
            "sequential_overlap": sequential_overlap,
            "cubic_face_size": cubic_face_size,
            "cubic_field_of_view": cubic_field_of_view,
            "use_gpu": use_gpu,
            "min_num_matches": min_num_matches,
            "fps": fps,
            "mask_bottom": mask_bottom,
        })

        task_id = str(uuid.uuid4())
        filename = file.filename or "video.mp4"
        video_path = Path(settings.UPLOAD_DIR) / task_id / filename
        video_path.parent.mkdir(parents=True, exist_ok=True)

        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        task = create_task(pipeline_type, filename, db, video_path=str(video_path), task_id=task_id)
        if params:
            task.extra_data = params
        commit_and_notify(db, task)
        
        tasks_cache.clear()
    finally:
        db.close()

    return {"task_id": task_id, "status": "pending"}


async def _task_event_stream(task_id: str | None = None):
    q = subscribe()
    try:
        while True:
            try:
                raw = await asyncio.wait_for(q.get(), timeout=25.0)
            except asyncio.TimeoutError:
                yield ServerSentEvent(comment="ping")
                continue
            if task_id is not None:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get("task_id") != task_id:
                    continue
            yield ServerSentEvent(raw_data=raw, event="task")
    finally:
        unsubscribe(q)


@router.get("/stream", response_class=EventSourceResponse)
async def stream_all_tasks():
    """SSE: updates for any task (replaces list polling)."""
    async for event in _task_event_stream(None):
        yield event


@router.get("/{task_id}/stream", response_class=EventSourceResponse)
async def stream_one_task(task_id: str):
    """SSE: updates for a single task."""
    async for event in _task_event_stream(task_id):
        yield event


@router.get("/")
async def list_tasks(limit: int = 30):
    """Последние задачи (для списка в UI)."""
    cache_key = f"tasks_list_{limit}"
    cached = tasks_cache.get(cache_key)
    if cached:
        return cached

    db = SessionLocal()
    try:
        from models.database import Task

        rows = (
            db.query(Task)
            .order_by(Task.updated_at.desc())
            .limit(min(max(limit, 1), 100))
            .all()
        )
        out = []
        for t in rows:
            extra = t.extra_data or {}
            item = {
                "task_id": t.id,
                "status": t.status.value,
                "progress": t.progress,
                "pipeline_type": t.pipeline_type.value,
                "original_filename": t.original_filename,
                "error_message": t.error_message,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            if t.status == TaskStatus.COMPLETED and t.result_path:
                url = result_file_url(t.id, t.result_path)
                if url:
                    item["result_url"] = url
            out.append(item)
        
        result = {"tasks": out}
        tasks_cache.set(cache_key, result)
        return result
    finally:
        db.close()


@router.get("/{task_id}")
async def get_task_status(task_id: str):
    db = SessionLocal()
    try:
        task = get_task(task_id, db)
        if not task:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        extra = task.extra_data or {}
        resp = {
            "task_id": task.id,
            "status": task.status.value,
            "progress": task.progress,
            "error_message": task.error_message,
            "pipeline_type": task.pipeline_type.value,
            "frames_count": task.frames_count,
            "extra_data": extra,
        }
        if task.status == TaskStatus.COMPLETED and task.result_path:
            resp["result_path"] = task.result_path
            url = result_file_url(task_id, task.result_path)
            if url:
                resp["result_url"] = url
        return resp
    finally:
        db.close()


@router.get("/{task_id}/result")
async def download_result(task_id: str):
    db = SessionLocal()
    try:
        task = get_task(task_id, db)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != task.status.COMPLETED:
            raise HTTPException(status_code=400, detail="Task not completed yet")

        result_path = get_result_path(task_id, db)
        if not result_path or not Path(result_path).exists():
            raise HTTPException(status_code=404, detail="Result not found")

        if Path(result_path).is_dir():
            archive_path = Path(settings.RESULTS_DIR) / f"{task_id}_result.tar.gz"
            import tarfile
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(result_path, arcname=Path(result_path).name)
            return FileResponse(archive_path, filename=f"{task_id}_result.tar.gz", media_type="application/gzip")

        return FileResponse(result_path, filename=Path(result_path).name, media_type="application/octet-stream")
    finally:
        db.close()


@router.get("/{task_id}/video")
async def download_video(task_id: str):
    db = SessionLocal()
    try:
        task = get_task(task_id, db)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if not task.video_path or not Path(task.video_path).exists():
            raise HTTPException(status_code=404, detail="Video not found")
        return FileResponse(task.video_path, filename=Path(task.video_path).name, media_type="video/mp4")
    finally:
        db.close()


@router.get("/{task_id}/log")
async def download_log(task_id: str):
    log_file = Path(settings.RESULTS_DIR) / "logs" / f"{task_id}.log"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log not found")
    return FileResponse(log_file, filename=f"{task_id}.log", media_type="text/plain")


@router.post("/{task_id}/restart")
async def restart_task(task_id: str, overrides: Optional[RestartRequest] = Body(None)):
    from models.database import TaskStatus
    db = SessionLocal()
    try:
        task = get_task(task_id, db)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status == TaskStatus.PROCESSING:
            raise HTTPException(status_code=400, detail="Task is already processing")
        if not task.video_path or not Path(task.video_path).exists():
            raise HTTPException(status_code=400, detail="No video file found, please upload again")

        task_dir = Path(settings.RESULTS_DIR) / task_id
        for name in ("colmap", "reconstruction", "gaussian", "mvs", "openmvg", "openmvs"):
            p = task_dir / name
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)

        if overrides is not None:
            raw = overrides.model_dump(exclude_none=True)
            new_params = _validate_reconstruction_params(raw)
            if new_params:
                merged = dict(task.extra_data or {})
                merged.update(new_params)
                task.extra_data = merged

        task.status = TaskStatus.PENDING
        task.error_message = None
        task.progress = 0.0
        task.openmvg_matches_path = None
        task.openmvg_reconstruction_path = None
        task.openmvs_scene_path = None
        task.gaussian_model_path = None
        task.result_path = None
        commit_and_notify(db, task)
        return {
            "task_id": task_id,
            "status": "pending",
            "extra_data": task.extra_data,
        }
    finally:
        db.close()
