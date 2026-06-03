import threading
import time

from database import SessionLocal
from services.task_worker import process_task

_worker_thread = None


def start_worker():
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
        _worker_thread.start()


def _worker_loop():
    from models.database import Task, TaskStatus
    while True:
        db = SessionLocal()
        try:
            pending_task = db.query(Task).filter(Task.status == TaskStatus.PENDING).first()
            if pending_task:
                task_id = pending_task.id
                video_path = pending_task.video_path
                if not video_path:
                    db.close()
                    continue
                db.close()
                process_task(task_id, video_path, SessionLocal())
            else:
                db.close()
                time.sleep(2)
        except Exception:
            db.close()
            time.sleep(2)
