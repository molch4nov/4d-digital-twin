import subprocess
import logging
from pathlib import Path
from config.settings import settings

logger = logging.getLogger(__name__)


def run_colmap_sfm(frames_dir: str, output_dir: str, task_id: str) -> str:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sparse_dir = output_dir / "sparse" / "0"
    sparse_dir.mkdir(parents=True, exist_ok=True)

    database_path = output_dir / "database.db"

    cmd_mapper = [
        settings.COLMAP_BIN + "/colmap",
        "mapper",
        "--image_path", frames_dir,
        "--database_path", str(database_path),
        "--output_path", str(sparse_dir),
        "--Mapper.num_threads", str(settings.WORKER_THREADS)
    ]
    logger.info(f"Running COLMAP mapper: {' '.join(cmd_mapper)}")
    result = subprocess.run(cmd_mapper, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"COLMAP mapper failed: {result.stderr}")
        raise RuntimeError(f"COLMAP mapper failed: {result.stderr}")

    return str(sparse_dir)


def run_colmap_to_gaussian(colmap_dir: str, output_dir: str, task_id: str) -> str:
    colmap_dir = Path(colmap_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    sparse_dir = output_dir / "sparse" / "0"
    sparse_dir.mkdir(parents=True, exist_ok=True)

    frames_path = Path(colmap_dir).parent
    frames_images = list(sorted(frames_path.glob("*.jpg")) + sorted(frames_path.glob("*.png")))
    for img in frames_images:
        shutil_copy = subprocess.run(
            ["cp", str(img), str(images_dir / img.name)],
            capture_output=True
        )

    for bin_file in colmap_dir.glob("*.bin"):
        subprocess.run(["cp", str(bin_file), str(sparse_dir / bin_file.name)])

    model_path = output_dir
    cmd_train = [
        "python", str(Path(settings.GAUSSIAN_ROOT) / "train.py"),
        "-s", str(output_dir),
        "-m", str(model_path),
        "-i", "images",
        "--quiet"
    ]
    logger.info(f"Running Gaussian Splatting training: {' '.join(cmd_train)}")
    result = subprocess.run(cmd_train, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Gaussian training failed: {result.stderr}")
        raise RuntimeError(f"Gaussian training failed: {result.stderr}")

    return str(model_path)


def run_gaussian_pipeline(frames_dir: str, task_id: str) -> str:
    work_dir = Path(settings.RESULTS_DIR) / task_id / "gaussian"
    work_dir.mkdir(parents=True, exist_ok=True)

    colmap_sparse = run_colmap_sfm(frames_dir, work_dir, task_id)
    model_path = run_colmap_to_gaussian(colmap_sparse, work_dir, task_id)
    return model_path
