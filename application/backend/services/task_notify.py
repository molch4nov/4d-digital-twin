import json
import asyncio
from sqlalchemy.orm import Session
from models.database import Task
from services.task_events import publish

async def notify_task(task: Task):
    data = {
        "task_id": task.id,
        "status": task.status.value,
        "progress": task.progress,
        "error_message": task.error_message,
    }
    await publish(json.dumps(data))

def commit_and_notify(db: Session, task: Task):
    db.commit()
    db.refresh(task)
    try:
        asyncio.create_task(notify_task(task))
    except RuntimeError:
        pass
