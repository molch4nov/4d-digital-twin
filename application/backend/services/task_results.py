"""Resolve on-disk task result folders (demo tasks may exist without DB row)."""

from pathlib import Path

from config.settings import settings


def task_results_dir(task_id: str) -> Path | None:
    root = Path(settings.RESULTS_DIR) / task_id
    if root.is_dir():
        return root
    return None


def result_file_url(task_id: str, result_path: str | None) -> str | None:
    """Публичный URL файла результата относительно папки задачи."""
    if not result_path:
        return None
    root = task_results_dir(task_id)
    if not root:
        return None

    rp = Path(result_path)
    if rp.is_file():
        try:
            rel = rp.resolve().relative_to(root.resolve())
            return f"/api/v1/files/{task_id}/{rel.as_posix()}"
        except ValueError:
            pass

    name = rp.name
    for candidate in root.rglob(name):
        if candidate.is_file():
            rel = candidate.relative_to(root)
            return f"/api/v1/files/{task_id}/{rel.as_posix()}"

    return None
