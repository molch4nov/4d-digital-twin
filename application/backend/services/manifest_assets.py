"""Enrich panorama manifest with asset URLs."""

from typing import Any, Dict, List


def enrich_manifest(task_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure frame URLs point at task files (keep existing paths)."""
    if not manifest:
        return manifest

    frames = manifest.get("frames", [])
    for frame in frames:
        if frame.get("url"):
            continue
        path = frame.get("path")
        if path:
            frame["url"] = f"/api/v1/files/{task_id}/{path}"

    asset_url = manifest.get("asset_url")
    if not asset_url and manifest.get("result_path"):
        from pathlib import Path
        from services.task_results import result_file_url
        url = result_file_url(task_id, manifest["result_path"])
        if url:
            manifest["asset_url"] = url

    return manifest
