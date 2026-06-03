"""Panorama manifest and nearest-ERP lookup for viewer click."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from services.manifest_assets import enrich_manifest
from services.panorama_index import load_manifest, nearest_frame
from services.task_results import task_results_dir

router = APIRouter(prefix="/api/v1/tasks", tags=["panorama"])


@router.get("/{task_id}/manifest")
async def get_manifest(task_id: str):
    task_dir = task_results_dir(task_id)
    if not task_dir:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    try:
        return enrich_manifest(task_id, load_manifest(task_dir))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Манифест панорам ещё не готов")


@router.get("/{task_id}/panorama/nearest")
async def get_nearest_panorama(
    task_id: str,
    x: float = Query(..., description="COLMAP X"),
    y: float = Query(..., description="COLMAP Y"),
    z: float = Query(..., description="COLMAP Z"),
):
    task_dir = task_results_dir(task_id)
    if not task_dir:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    try:
        manifest = load_manifest(task_dir)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Индекс панорам не найден")

    hit = nearest_frame(manifest, x, y, z)
    if not hit:
        raise HTTPException(status_code=404, detail="Нет ERP-кадров в индексе")
    return {
        "frame_id": hit["id"],
        "distance": hit["distance"],
        "erp_url": hit["url"],
        "position": {"x": hit["x"], "y": hit["y"], "z": hit["z"]},
    }
