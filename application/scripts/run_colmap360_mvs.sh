#!/usr/bin/env bash
# =============================================================================
# run_colmap360_mvs.sh
#
# Полный пайплайн: 360° видео/ERP → COLMAP (CPU, run_colmap_360.sh) → run_mvs_from_colmap.sh
# → textured GLB для OPENMVGMVS_pipeline/scripts/viewer.html (three.js).
#
# Примеры:
#   ./run_colmap360_mvs.sh \
#       --input  /data/pano.mp4 \
#       --output /data/scene_run \
#       --fps 1 --mask-bottom 0.10
#
#   # COLMAP уже готов:
#   ./run_colmap360_mvs.sh --output /data/scene_run --skip-colmap
#
#   # Только COLMAP:
#   ./run_colmap360_mvs.sh --input /data/pano.mp4 --output /data/scene_run --skip-mvs
#
# Env:
#   OPENMVS_BIN (default /root/work/openMVS/make/bin), SKIP_REFINE, COLMAP_USE_GPU, …
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pipeline_timing.sh
source "${SCRIPT_DIR}/pipeline_timing.sh"
TIMING_SCRIPT_NAME="run_colmap360_mvs.sh"
COLMAP_SH="${COLMAP_SCRIPT:-${SCRIPT_DIR}/run_colmap_360.sh}"
MVS_SH="${SCRIPT_DIR}/run_mvs_from_colmap.sh"

INPUT=""
OUTPUT=""
MVS_DIR=""
SKIP_COLMAP=0
SKIP_MVS=0

COLMAP_ARGS=()
MVS_ARGS=()

die() { echo "ERROR: $*" >&2; exit 1; }
print_step() { echo ""; echo "################################################################"; echo "# $*"; echo "################################################################"; }

usage() {
    cat <<'EOF'
run_colmap360_mvs.sh — 360° → COLMAP (pinhole views) → OpenMVS → GLB

Обязательные:
  --input PATH       Видео или папка ERP (для COLMAP)
  --output PATH      Рабочая папка COLMAP

Пайплайн:
  --skip-colmap      Пропустить run_colmap_360.sh
  --skip-mvs         Пропустить run_mvs_from_colmap.sh
  --mvs-dir PATH     Каталог OpenMVS (default: <output>/mvs)

COLMAP: те же флаги, что run_colmap_360.sh (--fps, --mask-bottom, --matcher, …)
OpenMVS: --skip-refine, --clean-mvs, --texture-export, OPENMVS_*_EXTRA

EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input)            INPUT="$2"; shift 2 ;;
        --output)           OUTPUT="$2"; shift 2 ;;
        --mvs-dir)          MVS_DIR="$2"; shift 2 ;;
        --skip-colmap)      SKIP_COLMAP=1; shift ;;
        --skip-mvs)         SKIP_MVS=1; shift ;;

        --fps|--face-size|--hfov|--vfov|--views|--mask-bottom|--matcher|--vocab-tree|--gpu-index)
            COLMAP_ARGS+=("$1" "$2"); shift 2 ;;
        --gpu|--no-gpu|--no-undistort|--skip-frames|--skip-projection|--skip-sfm|--from-mapper)
            COLMAP_ARGS+=("$1"); shift ;;

        --skip-refine|--clean-mvs|--skip-undistort|--run-undistort|--skip-glb-postprocess)
            MVS_ARGS+=("$1"); shift ;;
        --texture-export)
            MVS_ARGS+=("$1" "$2"); shift 2 ;;

        -h|--help) usage ;;
        *)
            die "Неизвестный аргумент: $1 (см. --help)"
            ;;
    esac
done

[[ -x "$COLMAP_SH" ]] || die "Нет $COLMAP_SH"
[[ -x "$MVS_SH" ]] || die "Нет $MVS_SH"

if [[ "$SKIP_COLMAP" == "0" ]]; then
    [[ -n "$INPUT" ]] || die "Нужен --input (или --skip-colmap)"
fi
[[ -n "$OUTPUT" ]] || die "Нужен --output"

mkdir -p "$OUTPUT"
OUTPUT="$(cd "$OUTPUT" && pwd)"

timing_init "$OUTPUT" "$TIMING_SCRIPT_NAME"
trap 'timing_finalize' EXIT

if [[ -n "$MVS_DIR" ]]; then
    MVS_ARGS+=(--mvs-dir "$MVS_DIR")
fi

if [[ "$SKIP_COLMAP" == "0" ]]; then
    print_step "Этап 1/2: COLMAP (run_colmap_360.sh)"
    timing_step_start "stage_colmap_360"
    "$COLMAP_SH" --input "$INPUT" --output "$OUTPUT" "${COLMAP_ARGS[@]}"
    timing_step_end ok
else
    timing_step_skip "stage_colmap_360"
    echo "[info] --skip-colmap: $OUTPUT"
    [[ -d "$OUTPUT/images" || -f "$OUTPUT/database.db" ]] \
        || die "--skip-colmap: в $OUTPUT нет COLMAP workspace"
fi

if [[ "$SKIP_MVS" == "0" ]]; then
    print_step "Этап 2/2: OpenMVS (run_mvs_from_colmap.sh)"
    timing_step_start "stage_mvs_from_colmap"
    "$MVS_SH" --colmap-dir "$OUTPUT" "${MVS_ARGS[@]}"
    timing_step_end ok
else
    timing_step_skip "stage_mvs_from_colmap"
    echo "[info] --skip-mvs: COLMAP workspace: $OUTPUT"
fi

MVS_DEFAULT="${MVS_DIR:-${OUTPUT}/mvs}"
print_step "Готово"
echo "  COLMAP : $OUTPUT"
echo "  OpenMVS: $MVS_DEFAULT"
echo "  viewer : file://${VIEWER_HTML:-/root/work/OPENMVGMVS_pipeline/scripts/viewer.html}"
