import subprocess
import logging
from pathlib import Path
from config.settings import settings

logger = logging.getLogger(__name__)


def extract_frames(video_path: str, output_dir: str, fps: int = None) -> tuple[str, int]:
    fps = fps or settings.FFMPEG_FPS
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pattern = str(output_dir / "%06d.jpg")

    cmd = [
        settings.FFMPEG_BIN,
        "-i", video_path,
        "-vf", f"fps={fps}",
        "-q:v", "2",
        "-vcodec", "mjpeg",
        pattern,
        "-y"
    ]

    logger.info(f"Extracting frames from {video_path} to {output_dir}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        logger.error(f"ffmpeg failed: {result.stderr}")
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")

    frame_files = sorted(output_dir.glob("*.jpg"))
    frame_count = len(frame_files)

    logger.info(f"Extracted {frame_count} frames")
    return str(output_dir), frame_count
