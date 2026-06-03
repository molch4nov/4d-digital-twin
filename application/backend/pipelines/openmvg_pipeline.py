"""
OpenMVG Pipeline - Compatibility wrapper.

This module provides backward-compatible wrappers around the new mvs_pipeline.
For new code, use run_full_pipeline() or run_spherical_pipeline() directly.
"""

import logging
from pathlib import Path
from typing import Optional
from .mvs_pipeline import run_full_pipeline

logger = logging.getLogger(__name__)


def run_openmvg_pipeline(
    frames_dir: str, 
    dataset_name: str, 
    task_id: str,
    camera_type: str = "spherical",
    **kwargs
) -> dict:
    """
    Run OpenMVG pipeline (compatibility wrapper).
    
    This function is a compatibility wrapper around run_full_pipeline().
    It defaults to spherical camera model for 360° images.
    
    Args:
        frames_dir: Directory containing input images
        dataset_name: Name of the dataset (used for output directory)
        task_id: Task identifier
        camera_type: "spherical" (default) or "pinhole"
        **kwargs: Additional arguments passed to run_full_pipeline()
    
    Returns:
        Dictionary with paths to output files
    """
    logger.warning(
        "run_openmvg_pipeline() is deprecated. "
        "Use run_full_pipeline() or run_spherical_pipeline() instead."
    )
    
    output_dir = Path(frames_dir) / "reconstruction"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Set defaults for compatibility
    defaults = {
        "camera_type": camera_type,
        "resize_long_edge": 4096 if camera_type == "spherical" else 0,
        "cubic_size": 1600 if camera_type == "spherical" else 0,
        "sfm_engine": "INCREMENTAL",
        "feature_method": "SIFT",
        "nearest_matching": "AUTO",
    }
    defaults.update(kwargs)
    
    return run_full_pipeline(frames_dir, str(output_dir), **defaults)
