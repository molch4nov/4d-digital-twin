#!/usr/bin/env bash
# =============================================================================
# run_colmap360_3dgs.sh
#
# Полный пайплайн: 360° видео/ERP → COLMAP (CPU, run_colmap_360.sh) → run_3dgs_from_colmap360.sh
#
# Примеры:
#   conda activate gaussian_splatting   # для этапа 3DGS
#
#   ./run_colmap360_3dgs.sh \
#       --input  /data/pano.mp4 \
#       --output /data/scene_run \
#       --fps 1 --mask-bottom 0.10
#
#   ./run_colmap360_3dgs.sh \
#       --input /data/pano.mp4 --output /data/scene_run \
#       --data-device cuda \
#       -- --iterations 30000 --quiet
#
#   # COLMAP уже готов — только 3DGS:
#   ./run_colmap360_3dgs.sh --output /data/scene_run --skip-colmap
#
#   # Только COLMAP:
#   ./run_colmap360_3dgs.sh --input /data/pano.mp4 --output /data/scene_run --skip-3dgs
#
# Env (пробрасываются в дочерние скрипты):
#   COLMAP_USE_GPU, GS_REPO, TRAIN_DATA_DEVICE, PYTHON, SKIP_TRAIN
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pipeline_timing.sh
source "${SCRIPT_DIR}/pipeline_timing.sh"
TIMING_SCRIPT_NAME="run_colmap360_3dgs.sh"
COLMAP_SH="${COLMAP_SCRIPT:-${SCRIPT_DIR}/run_colmap_360.sh}"
GS_SH="${SCRIPT_DIR}/run_3dgs_from_colmap360.sh"

INPUT=""
OUTPUT=""
SCENE_DIR=""
MODEL_DIR=""
SKIP_COLMAP=0
SKIP_3DGS=0

COLMAP_ARGS=()
GS_ARGS=()
TRAIN_PASS=()

die() { echo "ERROR: $*" >&2; exit 1; }
print_step() { echo ""; echo "################################################################"; echo "# $*"; echo "################################################################"; }

usage() {
    cat <<'EOF'
run_colmap360_3dgs.sh — 360° → COLMAP → 3D Gaussian Splatting

Обязательные:
  --input PATH       Видео или папка ERP (для COLMAP)
  --output PATH      Рабочая папка COLMAP (images/, sparse/, dense/)

Пайплайн:
  --skip-colmap      Пропустить run_colmap_360.sh (output/ уже готов)
  --skip-3dgs        Пропустить run_3dgs_from_colmap360.sh
  --scene-dir PATH   Датасет для train.py (по умолчанию: <output>/_3dgs_scene)
  --model-dir PATH   Чекпоинты 3DGS (по умолчанию: <scene-dir>/output)

COLMAP (run_colmap_360.sh):
  --fps N  --face-size N  --hfov N  --vfov N  --views SPEC
  --mask-bottom FRAC  --matcher sequential|exhaustive|vocab_tree
  --vocab-tree PATH  --gpu-index N  --gpu  --no-gpu  --no-undistort
  --skip-frames  --skip-projection  --skip-sfm  --from-mapper

3DGS (run_3dgs_from_colmap360.sh):
  --gs-repo PATH  --sparse-model ID  --use-dense  --no-dense
  --skip-prepare  --skip-train  --eval
  --data-device cpu|cuda  -r|--resolution N

train.py (после --):
  ./run_colmap360_3dgs.sh ... -- --iterations 30000 --quiet

EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input)            INPUT="$2"; shift 2 ;;
        --output)           OUTPUT="$2"; shift 2 ;;
        --scene-dir)        SCENE_DIR="$2"; shift 2 ;;
        --model-dir)        MODEL_DIR="$2"; shift 2 ;;
        --skip-colmap)      SKIP_COLMAP=1; shift ;;
        --skip-3dgs)        SKIP_3DGS=1; shift ;;

        # --- COLMAP ---
        --fps|--face-size|--hfov|--vfov|--views|--mask-bottom|--matcher|--vocab-tree|--gpu-index)
            COLMAP_ARGS+=("$1" "$2"); shift 2 ;;
        --gpu|--no-gpu|--no-undistort|--skip-frames|--skip-projection|--skip-sfm|--from-mapper)
            COLMAP_ARGS+=("$1"); shift ;;

        # --- 3DGS ---
        --gs-repo|--sparse-model|--data-device)
            GS_ARGS+=("$1" "$2"); shift 2 ;;
        --resolution|-r)
            GS_ARGS+=("--resolution" "$2"); shift 2 ;;
        --use-dense|--no-dense|--skip-prepare|--skip-train|--eval)
            GS_ARGS+=("$1"); shift ;;

        --)
            shift
            TRAIN_PASS=("$@")
            break
            ;;
        -h|--help) usage ;;
        *)
            die "Неизвестный аргумент: $1 (см. --help)"
            ;;
    esac
done

[[ -x "$COLMAP_SH" ]] || die "Нет $COLMAP_SH"
[[ -x "$GS_SH" ]] || die "Нет $GS_SH"

if [[ "$SKIP_COLMAP" == "0" ]]; then
    [[ -n "$INPUT" ]] || die "Нужен --input (или --skip-colmap)"
fi
[[ -n "$OUTPUT" ]] || die "Нужен --output"

mkdir -p "$OUTPUT"
OUTPUT="$(cd "$OUTPUT" && pwd)"

timing_init "$OUTPUT" "$TIMING_SCRIPT_NAME"
trap 'timing_finalize' EXIT

if [[ -n "$SCENE_DIR" ]]; then
    GS_ARGS+=(--scene-dir "$SCENE_DIR")
fi
if [[ -n "$MODEL_DIR" ]]; then
    GS_ARGS+=(--model-dir "$MODEL_DIR")
fi

# ----------------------------- stage 1: COLMAP -------------------------------
if [[ "$SKIP_COLMAP" == "0" ]]; then
    print_step "Этап 1/2: COLMAP (run_colmap_360.sh)"
    timing_step_start "stage_colmap_360"
    "$COLMAP_SH" --input "$INPUT" --output "$OUTPUT" "${COLMAP_ARGS[@]}"
    timing_step_end ok
else
    timing_step_skip "stage_colmap_360"
    echo "[info] --skip-colmap: использую готовый $OUTPUT"
    [[ -d "$OUTPUT/images" || -f "$OUTPUT/database.db" ]] \
        || die "--skip-colmap: в $OUTPUT нет признаков COLMAP (images/ или database.db)"
fi

# ----------------------------- stage 2: 3DGS ---------------------------------
if [[ "$SKIP_3DGS" == "0" ]]; then
    print_step "Этап 2/2: 3DGS (run_3dgs_from_colmap360.sh)"
    timing_step_start "stage_3dgs_from_colmap"
    GS_CMD=( "$GS_SH" --colmap-dir "$OUTPUT" "${GS_ARGS[@]}" )
    if [[ ${#TRAIN_PASS[@]} -gt 0 ]]; then
        GS_CMD+=( -- "${TRAIN_PASS[@]}" )
    fi
    "${GS_CMD[@]}"
    timing_step_end ok
else
    timing_step_skip "stage_3dgs_from_colmap"
    echo "[info] --skip-3dgs: COLMAP workspace: $OUTPUT"
fi

# ----------------------------- summary ---------------------------------------
SCENE_DEFAULT="${SCENE_DIR:-${OUTPUT}/_3dgs_scene}"
MODEL_DEFAULT="${MODEL_DIR:-${SCENE_DEFAULT}/output}"

print_step "Готово"
echo "  COLMAP   : $OUTPUT"
echo "  3DGS -s  : $SCENE_DEFAULT"
echo "  3DGS -m  : $MODEL_DEFAULT"
echo "  render   : cd \"\${GS_REPO:-/root/work/gaussian-splatting}\" && python render.py -m \"$MODEL_DEFAULT\" -s \"$SCENE_DEFAULT\""
