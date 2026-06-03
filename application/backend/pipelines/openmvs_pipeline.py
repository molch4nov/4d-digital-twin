"""
OpenMVS Pipeline - Compatibility wrapper.

This module provides backward-compatible wrappers around the new mvs_pipeline.
For new code, use run_full_pipeline() directly.
"""

import logging
from pathlib import Path
from .mvs_pipeline import run_full_pipeline

logger = logging.getLogger(__name__)


def run_openmvs_pipeline(cubic_dir: str, task_id: str) -> str:
    """
    Run OpenMVS pipeline (compatibility wrapper).
    
    This function is a compatibility wrapper around run_full_pipeline().
    It assumes the OpenMVG part has already been done and only runs MVS stages.
    
    Note: This is a simplified wrapper. For full control, use run_full_pipeline().
    
    Args:
        cubic_dir: Directory containing cubic/perspective SfM data
        task_id: Task identifier
    
    Returns:
        Path to the dense point cloud file
    """
    logger.warning(
        "run_openmvs_pipeline() is deprecated. "
        "Use run_full_pipeline() with appropriate camera_type instead."
    )
    
    cubic_path = Path(cubic_dir)
    
    # Determine output directory (parent of cubic_dir)
    output_dir = cubic_path.parent
    
    # Check if this is spherical or pinhole based on directory structure
    is_spherical = (cubic_path / "sfm_data_perspective.bin").exists()
    
    if is_spherical:
        # For spherical, we need to run from the cubic conversion onwards
        # But since OpenMVG part is done, we need to reconstruct paths
        image_dir = output_dir / "images_sfm"  # Or original images
        if not image_dir.exists():
            image_dir = output_dir.parent / "frames"  # Try frames directory
    else:
        # Pinhole - images are in the original directory
        image_dir = output_dir.parent / "frames"
    
    camera_type = "spherical" if is_spherical else "pinhole"
    
    # Run full pipeline but it will skip already completed stages
    result = run_full_pipeline(
        image_dir=str(image_dir) if image_dir.exists() else str(output_dir),
        output_dir=str(output_dir),
        camera_type=camera_type,
        resize_long_edge=4096 if is_spherical else 0,
        cubic_size=1600 if is_spherical else 0,
    )
    
    return result.get("dense_mvs", str(output_dir / "mvs" / "scene_dense.mvs"))
