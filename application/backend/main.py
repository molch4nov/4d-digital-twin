from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
import uvicorn
import logging

from database import init_db
from routes.tasks import router as tasks_router
from routes.files import router as files_router
from routes.panorama import router as panorama_router
from worker import start_worker

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="3D Reconstruction API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": "healthy"}

# Включаем маршруты API ДО фронтенда
app.include_router(tasks_router)
app.include_router(files_router)
app.include_router(panorama_router)


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response: Response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


# Фронтенд монтируется ПОСЛЕ API маршрутов
if FRONTEND_DIR.is_dir():
    app.mount("/", NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


@app.on_event("startup")
def on_startup():
    init_db()
    start_worker()
    logger.info("Application started and worker launched")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
