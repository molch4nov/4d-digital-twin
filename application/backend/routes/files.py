"""Serve task result files over HTTP (viewer, ERP, GLB)."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from services.task_results import task_results_dir

router = APIRouter(prefix="/api/v1/files", tags=["files"])


@router.get("/{task_id}/{file_path:path}")
async def serve_task_file(task_id: str, file_path: str):
    root = task_results_dir(task_id)
    if not root:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    root = root.resolve()
    rel = Path(file_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(status_code=404, detail="Файл не найден")

    logical = root
    for part in rel.parts:
        logical = logical / part
    try:
        logical.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="Файл не найден")
    if not logical.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    target = logical.resolve()

    media = "application/octet-stream"
    low = target.suffix.lower()
    if low == ".glb":
        media = "model/gltf-binary"
    elif low in {".jpg", ".jpeg"}:
        media = "image/jpeg"
    elif low == ".png":
        media = "image/png"
    elif low == ".ply":
        media = "application/octet-stream"

    return FileResponse(
        target,
        media_type=media,
        headers={"Access-Control-Allow-Origin": "*"},
    )
