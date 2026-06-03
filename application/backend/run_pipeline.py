#!/usr/bin/env python3
"""
Standalone pipeline runner.

Usage:
    python run_pipeline.py <pipeline> <video_path_or_image_dir> [task_id]
    
    pipeline: spherical_360 | pinhole | gaussian_splatting
"""

from pipelines import run_full_pipeline, run_gaussian_pipeline, extract_frames
from pathlib import Path

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python run_pipeline.py <pipeline> <video_path_or_image_dir> [task_id]")
        print("  pipeline: spherical_360 | pinhole | gaussian_splatting")
        print("")
        print("Environment variables:")
        print("  RESIZE_LONG_EDGE  - Downscale panoramas (spherical only, default: 4096)")
        print("  CUBIC_SIZE        - Size of each cubic face (spherical only, default: 1600)")
        print("  PAIR_WINDOW       - Sequential matching window (0 = all pairs)")
        print("  SKIP_REFINE       - Skip RefineMesh (1 = skip, default: 0)")
        print("  CLEAN_MVS         - Clean MVS artifacts (1 = clean, default: 0)")
        print("  FORCE_RECOMPUTE   - Force recompute features/matches (1 = force, default: 0)")
        print("  SFM_ENGINE        - SfM engine (INCREMENTAL, GLOBAL, INCREMENTALV2)")
        print("  INITIAL_PAIR_A    - First image for initial pair (spherical only)")
        print("  INITIAL_PAIR_B    - Second image for initial pair (spherical only)")
        print("  FOCAL_PIX         - Approximate focal length (pinhole only)")
        print("  OPENMVS_DENSIFY_EXTRA - Extra args for DensifyPointCloud")
        print("  OPENMVS_KEEP_DEPTH_MAPS - Keep depth maps (1 = keep)")
        print("  OPENMVS_MAX_THREADS - Max threads for OpenMVS (default: WORKER_THREADS)")
        sys.exit(1)

    pipeline = sys.argv[1]
    input_path = sys.argv[2]
    task_id = sys.argv[3] if len(sys.argv) > 3 else "standalone"
    
    # Parse environment variables (matching shell script behavior)
    import os
    
    # Extract frames if input is a video
    input_path_obj = Path(input_path)
    if input_path_obj.is_file() and input_path_obj.suffix.lower() in ['.mp4', '.avi', '.mov', '.mkv']:
        from config.settings import settings
        frames_dir = Path(settings.RESULTS_DIR) / task_id / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        print(f"Extracting frames from {input_path} to {frames_dir}")
        frames_dir, frame_count = extract_frames(input_path, str(frames_dir))
        print(f"Extracted {frame_count} frames")
        image_dir = frames_dir
        output_dir = Path(settings.RESULTS_DIR) / task_id / "reconstruction"
    else:
        image_dir = input_path
        output_dir = input_path_obj / "reconstruction"
    
    # Build kwargs from environment
    kwargs = {}
    
    if os.getenv("RESIZE_LONG_EDGE"):
        kwargs["resize_long_edge"] = int(os.getenv("RESIZE_LONG_EDGE"))
    if os.getenv("CUBIC_SIZE"):
        kwargs["cubic_size"] = int(os.getenv("CUBIC_SIZE"))
    if os.getenv("PAIR_WINDOW"):
        kwargs["pair_window"] = int(os.getenv("PAIR_WINDOW"))
    if os.getenv("SKIP_REFINE") == "1":
        kwargs["skip_refine"] = True
    if os.getenv("CLEAN_MVS") == "1":
        kwargs["clean_mvs"] = True
    if os.getenv("FORCE_RECOMPUTE"):
        kwargs["force_recompute"] = int(os.getenv("FORCE_RECOMPUTE"))
    if os.getenv("SFM_ENGINE"):
        kwargs["sfm_engine"] = os.getenv("SFM_ENGINE")
    if os.getenv("INITIAL_PAIR_A"):
        kwargs["initial_pair_a"] = os.getenv("INITIAL_PAIR_A")
    if os.getenv("INITIAL_PAIR_B"):
        kwargs["initial_pair_b"] = os.getenv("INITIAL_PAIR_B")
    if os.getenv("FOCAL_PIX"):
        kwargs["focal_pix"] = os.getenv("FOCAL_PIX")
    if os.getenv("OPENMVS_DENSIFY_EXTRA"):
        kwargs["openmvs_densify_extra"] = os.getenv("OPENMVS_DENSIFY_EXTRA")
    if os.getenv("OPENMVS_KEEP_DEPTH_MAPS") == "1":
        kwargs["openmvs_keep_depth_maps"] = True
    if os.getenv("OPENMVS_MAX_THREADS"):
        kwargs["openmvs_max_threads"] = int(os.getenv("OPENMVS_MAX_THREADS"))
    
    print(f"\nRunning {pipeline} pipeline...")
    print(f"Input: {image_dir}")
    print(f"Output: {output_dir}")
    print(f"Extra args: {kwargs}")
    print("")
    
    if pipeline == "spherical_360":
        kwargs.setdefault("camera_type", "spherical")
        result = run_full_pipeline(image_dir, str(output_dir), **kwargs)
    elif pipeline == "pinhole":
        kwargs.setdefault("camera_type", "pinhole")
        result = run_full_pipeline(image_dir, str(output_dir), **kwargs)
    elif pipeline == "gaussian_splatting":
        result_path = run_gaussian_pipeline(image_dir, task_id)
        result = {"gaussian_model_path": result_path}
    else:
        print(f"Unknown pipeline: {pipeline}")
        sys.exit(1)
    
    print("\n" + "="*50)
    print("Pipeline completed successfully!")
    print("="*50)
    for key, value in result.items():
        print(f"  {key}: {value}")
