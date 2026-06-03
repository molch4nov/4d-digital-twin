import uuid
import logging
from pathlib import Path
from sqlalchemy.orm import Session

from models.database import Task, TaskStatus, PipelineType
from config.settings import settings

logger = logging.getLogger(__name__)


def create_task(pipeline_type: PipelineType, original_filename: str, db: Session, video_path: str = None, commit: bool = True, task_id: str = None) -> Task:
    if task_id is None:
        task_id = str(uuid.uuid4())

    task = Task(
        id=task_id,
        pipeline_type=pipeline_type,
        status=TaskStatus.PENDING,
        original_filename=original_filename,
        video_path=video_path,
    )
    db.add(task)
    if commit:
        db.commit()
        db.refresh(task)
    return task


def get_task(task_id: str, db: Session) -> Task | None:
    return db.query(Task).filter(Task.id == task_id).first()


def update_task_progress(task_id: str, progress: float, db: Session):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task.progress = progress
        db.commit()


def get_result_path(task_id: str, db: Session) -> str | None:
    task = db.query(Task).filter(Task.id == task_id).first()
    if task and task.status == TaskStatus.COMPLETED:
        return task.result_path
    return None
