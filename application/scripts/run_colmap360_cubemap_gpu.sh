#!/usr/bin/env bash
# =============================================================================
# run_colmap360_cubemap_gpu.sh
#
# 360° ERP → перспективные виды (ffmpeg v360, как run_colmap_360.sh) → COLMAP (PINHOLE).
# Без SphereSfM / SPHERE camera / sphere_cubic_reprojecer.
#
# SIFT extract/match: GPU через OpenGL (xvfb-run на headless). Mapper — CPU.
# Dense mesh: OpenMVS (run_colmap360_mvs.sh), не COLMAP patch_match.
#
# Примеры:
#   ./run_colmap360_cubemap_gpu.sh --input pano.mp4 --output /data/out
#   ./run_colmap360_cubemap_gpu.sh --input pano.mp4 --output /data/out --preset fast
#
#   # дальше OpenMVS:
#   ./run_colmap360_mvs.sh --output /data/out --skip-colmap
#
# Env: COLMAP_BIN, COLMAP_USE_GPU=1|0, QT_QPA_PLATFORM=offscreen
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pipeline_timing.sh
source "${SCRIPT_DIR}/pipeline_timing.sh"
TIMING_SCRIPT_NAME="run_colmap360_cubemap_gpu.sh"

INPUT=""
OUTPUT=""
FPS=2
FACE_SIZE=1024
HFOV=90
VFOV=90
# Как run_colmap_360.sh: 5 горизонтальных + вверх (без nadir / каски снизу).
VIEWS_SPEC="0,0;72,0;144,0;-144,0;-72,0;0,75"
MASK_BOTTOM=0.0
MATCHER="sequential"
VOCAB_TREE_PATH=""
GPU_INDEX="-1"
USE_GPU=1
MAX_NUM_FEATURES=8192
SIFT_THREADS=-1
MATCH_THREADS=-1
MAPPER_THREADS=-1
PRESET=""
DO_UNDISTORT=1
SKIP_FRAME_EXTRACT=0
SKIP_PROJECTION=0
SKIP_SFM=0
FROM_MAPPER=0
COLMAP_BIN_OVERRIDE=""

usage() {
    cat <<'EOF'
run_colmap360_cubemap_gpu.sh — ERP → pinhole views → COLMAP (SIFT GPU + xvfb)

Обязательные:
  --input PATH       Видео или папка ERP-кадров
  --output PATH      Рабочая директория

Проекция (ffmpeg v360 e→flat, как run_colmap_360.sh):
  --fps N            Кадров ERP из видео (default 2)
  --face-size N      Сторона грани (default 1024)
  --hfov / --vfov    FOV грани в градусах (default 90)
  --views SPEC       yaw,pitch через ";" (default: 5 horiz + up 75°)
  --mask-bottom F    Доля высоты маски снизу (каска), 0..1

COLMAP / GPU:
  --gpu / --no-gpu   SIFT OpenGL GPU (default: GPU; colmap всегда через xvfb-run)
  --gpu-index N      -1 = авто
  --colmap-bin PATH  Бинарник COLMAP (default: colmap в PATH)
  --preset fast      face-size=768, max-features=4096
  --max-num-features N
  --matcher sequential|exhaustive|vocab_tree
  --vocab-tree PATH

Пропуск шагов:
  --skip-frames --skip-projection --skip-sfm --from-mapper
  --no-undistort

Dense: run_colmap360_mvs.sh (OpenMVS), не этот скрипт.
EOF
    exit 1
}

die() { echo "ERROR: $*" >&2; exit 1; }

_resolve_colmap_bin() {
    if [[ -n "$COLMAP_BIN_OVERRIDE" ]]; then
        echo "$COLMAP_BIN_OVERRIDE"
        return
    fi
    if [[ -n "${COLMAP_BIN:-}" ]]; then
        if [[ -x "${COLMAP_BIN}/colmap" ]]; then
            echo "${COLMAP_BIN}/colmap"
        elif [[ -x "${COLMAP_BIN}" ]]; then
            echo "${COLMAP_BIN}"
        else
            die "COLMAP_BIN не исполняем: ${COLMAP_BIN}"
        fi
        return
    fi
    command -v colmap || die "colmap не найден в PATH; задайте --colmap-bin или COLMAP_BIN"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input)              INPUT="$2"; shift 2 ;;
        --output)             OUTPUT="$2"; shift 2 ;;
        --fps)                FPS="$2"; shift 2 ;;
        --face-size)          FACE_SIZE="$2"; shift 2 ;;
        --hfov)               HFOV="$2"; shift 2 ;;
        --vfov)               VFOV="$2"; shift 2 ;;
        --views)              VIEWS_SPEC="$2"; shift 2 ;;
        --mask-bottom)        MASK_BOTTOM="$2"; shift 2 ;;
        --matcher)            MATCHER="$2"; shift 2 ;;
        --vocab-tree)         VOCAB_TREE_PATH="$2"; shift 2 ;;
        --gpu-index)          GPU_INDEX="$2"; shift 2 ;;
        --gpu)                USE_GPU=1; shift ;;
        --no-gpu)             USE_GPU=0; shift ;;
        --colmap-bin)         COLMAP_BIN_OVERRIDE="$2"; shift 2 ;;
        --preset)             PRESET="$2"; shift 2 ;;
        --max-num-features)   MAX_NUM_FEATURES="$2"; shift 2 ;;
        --no-undistort)       DO_UNDISTORT=0; shift ;;
        --skip-frames)        SKIP_FRAME_EXTRACT=1; shift ;;
        --skip-projection)    SKIP_PROJECTION=1; shift ;;
        --skip-sfm)           SKIP_SFM=1; shift ;;
        --from-mapper)        FROM_MAPPER=1; SKIP_FRAME_EXTRACT=1; SKIP_PROJECTION=1; shift ;;
        -h|--help)            usage ;;
        *) die "Неизвестный аргумент: $1" ;;
    esac
done

[[ -n "$INPUT" && -n "$OUTPUT" ]] || usage

if [[ -n "${COLMAP_USE_GPU:-}" ]]; then
    USE_GPU="$COLMAP_USE_GPU"
fi

case "$PRESET" in
    ""|none) ;;
    fast)
        FACE_SIZE=768
        MAX_NUM_FEATURES=4096
        echo "[preset fast] face-size=${FACE_SIZE}, max_num_features=${MAX_NUM_FEATURES}"
        ;;
    *) die "Неизвестный --preset: $PRESET (доступен: fast)" ;;
esac

command -v ffmpeg >/dev/null || die "ffmpeg не найден"
command -v python3 >/dev/null || die "python3 не найден"
COLMAP_BIN="$(_resolve_colmap_bin)"
[[ -x "$COLMAP_BIN" ]] || die "Не исполняем: $COLMAP_BIN"

export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

OPENBLAS_LAPACK_DIR="/usr/lib/x86_64-linux-gnu/openblas-openmp"
if [[ -d "$OPENBLAS_LAPACK_DIR" && -f "$OPENBLAS_LAPACK_DIR/liblapack.so.3" ]]; then
    case ":${LD_LIBRARY_PATH:-}:" in
        *":${OPENBLAS_LAPACK_DIR}:"*) ;;
        *) export LD_LIBRARY_PATH="${OPENBLAS_LAPACK_DIR}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
    esac
fi

# SIFT GPU = OpenGL; на headless — xvfb-run (как run_colmap_360.sh с --gpu).
_colmap_run() {
    if [[ "$USE_GPU" == "1" ]]; then
        command -v xvfb-run >/dev/null || die "для GPU нужен xvfb-run (apt install xvfb)"
        xvfb-run -a "$COLMAP_BIN" "$@"
    else
        "$COLMAP_BIN" "$@"
    fi
}

if [[ "$USE_GPU" == "1" ]] && ! command -v xvfb-run >/dev/null; then
    echo "[warn] Headless и нет xvfb-run: SIFT GPU недоступен, CPU."
    USE_GPU=0
fi

mkdir -p "$OUTPUT"
OUTPUT="$(cd "$OUTPUT" && pwd)"
ERP_DIR="$OUTPUT/_erp_frames"
IMG_DIR="$OUTPUT/images"
DB_PATH="$OUTPUT/database.db"
SPARSE_DIR="$OUTPUT/sparse"
DENSE_DIR="$OUTPUT/dense"
MASK_PATH="$OUTPUT/camera_mask.png"
LOG_DIR="$OUTPUT/_logs"
mkdir -p "$ERP_DIR" "$IMG_DIR" "$SPARSE_DIR" "$LOG_DIR"

timing_init "$OUTPUT" "$TIMING_SCRIPT_NAME"
trap 'timing_finalize' EXIT

_focal() {
    python3 -c "import math; s=float('$1'); fov=float('$2'); print(f'{(s/2)/math.tan(math.radians(fov/2)):.6f}')"
}
FX=$(_focal "$FACE_SIZE" "$HFOV")
FY=$(_focal "$FACE_SIZE" "$VFOV")
CX=$(python3 -c "print(f'{float(\"$FACE_SIZE\")/2:.6f}')")
CY=$(python3 -c "print(f'{float(\"$FACE_SIZE\")/2:.6f}')")
CAM_PARAMS="${FX},${FY},${CX},${CY}"

echo "================================================================"
echo " COLMAP 360 (GPU path)"
echo " COLMAP     : $COLMAP_BIN ($([ "$USE_GPU" == "1" ] && echo xvfb-run || echo CPU))"
echo " SIFT GPU   : use_gpu=${USE_GPU} gpu_index=${GPU_INDEX}"
echo " Dense MVS  : OpenMVS → run_colmap360_mvs.sh"
echo " views      : $VIEWS_SPEC"
echo " PINHOLE    : ${FACE_SIZE}px fx=${FX} fy=${FY}"
echo "================================================================"

# --- ERP frames ---
is_video=0
[[ -f "$INPUT" ]] && is_video=1
[[ -d "$INPUT" ]] && is_video=0
[[ -f "$INPUT" || -d "$INPUT" ]] || die "--input должен быть файлом или папкой"

if [[ "$SKIP_FRAME_EXTRACT" == "0" ]]; then
    timing_step_start "erp_extract"
    if [[ "$is_video" == "1" ]]; then
        echo "[step 1] ERP из видео @ ${FPS} fps"
        rm -f "$ERP_DIR"/erp_*.jpg 2>/dev/null || true
        ffmpeg -hide_banner -loglevel error -y -i "$INPUT" \
            -vf "fps=${FPS}" -q:v 2 -start_number 0 \
            "$ERP_DIR/erp_%06d.jpg"
    else
        echo "[step 1] ERP из $INPUT"
        rm -f "$ERP_DIR"/erp_*.jpg 2>/dev/null || true
        i=0
        while IFS= read -r f; do
            ln -sf "$f" "$(printf "%s/erp_%06d.jpg" "$ERP_DIR" "$i")"
            i=$((i + 1))
        done < <(find "$INPUT" -maxdepth 1 -type f \
            \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | sort)
    fi
    timing_step_end ok
else
    timing_step_skip "erp_extract"
fi

N_ERP=$(find "$ERP_DIR" -maxdepth 1 -name 'erp_*.jpg' | wc -l)
echo "[info] ERP кадров: $N_ERP"
[[ "$N_ERP" -ge 2 ]] || die "мало ERP кадров"

# --- ERP → perspective views ---
IFS=';' read -r -a VIEWS_ARR <<< "$VIEWS_SPEC"
N_VIEWS=${#VIEWS_ARR[@]}

if [[ "$SKIP_PROJECTION" == "0" ]]; then
    timing_step_start "erp_to_perspective"
    echo "[step 2] ERP → ${N_VIEWS} перспективных видов (${FACE_SIZE}px, FOV ${HFOV}x${VFOV})"
    rm -f "$IMG_DIR"/*.jpg 2>/dev/null || true
    for k in "${!VIEWS_ARR[@]}"; do
        spec="${VIEWS_ARR[$k]}"
        yaw="${spec%%,*}"
        pitch="${spec##*,}"
        echo "  [view $k] yaw=${yaw} pitch=${pitch}"
        TMP_VDIR="$OUTPUT/_proj_v${k}"
        mkdir -p "$TMP_VDIR"
        rm -f "$TMP_VDIR"/*.jpg 2>/dev/null || true
        ffmpeg -hide_banner -loglevel error -y \
            -f image2 -framerate 30 -i "$ERP_DIR/erp_%06d.jpg" \
            -vf "v360=e:flat:yaw=${yaw}:pitch=${pitch}:roll=0:h_fov=${HFOV}:v_fov=${VFOV}:w=${FACE_SIZE}:h=${FACE_SIZE}:interp=cubic" \
            -q:v 2 -start_number 0 "$TMP_VDIR/proj_%06d.jpg"
        while IFS= read -r f; do
            n=$(basename "$f" .jpg); n=${n#proj_}
            mv "$f" "$IMG_DIR/frame_${n}_v${k}.jpg"
        done < <(find "$TMP_VDIR" -maxdepth 1 -name 'proj_*.jpg' | sort)
        rmdir "$TMP_VDIR" 2>/dev/null || true
    done
    timing_step_end ok
else
    timing_step_skip "erp_to_perspective"
fi

N_IMG=$(find "$IMG_DIR" -maxdepth 1 -name '*.jpg' | wc -l)
echo "[info] pinhole изображений: $N_IMG (= ${N_ERP} ERP × ${N_VIEWS} видов)"

USE_MASK=0
MASK_ARG=()
if awk -v m="$MASK_BOTTOM" 'BEGIN{ exit !(m+0 > 0) }'; then
    MASK_PX=$(awk -v s="$FACE_SIZE" -v m="$MASK_BOTTOM" 'BEGIN{ printf "%d", s*m + 0.5 }')
    if [[ "$MASK_PX" -gt 0 ]]; then
        timing_step_start "helmet_mask"
        echo "[step 3] маска снизу ${MASK_PX}px"
        ffmpeg -hide_banner -loglevel error -y \
            -f lavfi -i "color=c=white:s=${FACE_SIZE}x${FACE_SIZE}:d=1" \
            -vf "drawbox=x=0:y=$((FACE_SIZE-MASK_PX)):w=${FACE_SIZE}:h=${MASK_PX}:color=black:t=fill" \
            -frames:v 1 "$MASK_PATH"
        USE_MASK=1
        MASK_ARG=(--ImageReader.camera_mask_path "$MASK_PATH")
        timing_step_end ok
    fi
else
    timing_step_skip "helmet_mask"
fi

# --- COLMAP SfM ---
if [[ "$SKIP_SFM" == "0" ]]; then
    if [[ "$FROM_MAPPER" == "1" ]]; then
        [[ -f "$DB_PATH" ]] || die "--from-mapper: нет $DB_PATH"
        echo "[step 4] только mapper"
        timing_step_skip "colmap_database_creator"
        timing_step_skip "colmap_feature_extractor"
        timing_step_skip "colmap_matcher"
    else
        timing_step_start "colmap_database_creator"
        rm -f "$DB_PATH"
        echo "[step 4.1] database_creator"
        _colmap_run database_creator --database_path "$DB_PATH"
        timing_step_end ok

        timing_step_start "colmap_feature_extractor"
        echo "[step 4.2] feature_extractor (SIFT GPU=${USE_GPU}, max_features=${MAX_NUM_FEATURES})"
        _colmap_run feature_extractor \
            --database_path "$DB_PATH" \
            --image_path "$IMG_DIR" \
            --ImageReader.single_camera 1 \
            --ImageReader.camera_model PINHOLE \
            --ImageReader.camera_params "$CAM_PARAMS" \
            "${MASK_ARG[@]}" \
            --SiftExtraction.use_gpu "$USE_GPU" \
            --SiftExtraction.gpu_index "$GPU_INDEX" \
            --SiftExtraction.num_threads "$SIFT_THREADS" \
            --SiftExtraction.max_num_features "$MAX_NUM_FEATURES" \
            2>&1 | tee "$LOG_DIR/feature_extractor.log"
        timing_step_end ok

        timing_step_start "colmap_matcher"
        OVERLAP=$(( N_VIEWS * 5 ))
        [[ $OVERLAP -lt 10 ]] && OVERLAP=10
        MATCH_COMMON=(
            --database_path "$DB_PATH"
            --SiftMatching.use_gpu "$USE_GPU"
            --SiftMatching.gpu_index "$GPU_INDEX"
            --SiftMatching.num_threads "$MATCH_THREADS"
        )
        case "$MATCHER" in
            sequential)
                echo "[step 4.3] sequential_matcher overlap=${OVERLAP}"
                _colmap_run sequential_matcher \
                    "${MATCH_COMMON[@]}" \
                    --SequentialMatching.overlap "$OVERLAP" \
                    --SequentialMatching.quadratic_overlap 1 \
                    2>&1 | tee "$LOG_DIR/matcher.log"
                ;;
            exhaustive)
                echo "[step 4.3] exhaustive_matcher"
                _colmap_run exhaustive_matcher \
                    "${MATCH_COMMON[@]}" \
                    2>&1 | tee "$LOG_DIR/matcher.log"
                ;;
            vocab_tree)
                [[ -n "$VOCAB_TREE_PATH" ]] || die "нужен --vocab-tree"
                echo "[step 4.3] vocab_tree_matcher"
                _colmap_run vocab_tree_matcher \
                    "${MATCH_COMMON[@]}" \
                    --VocabTreeMatching.vocab_tree_path "$VOCAB_TREE_PATH" \
                    2>&1 | tee "$LOG_DIR/matcher.log"
                ;;
            *) die "matcher: $MATCHER" ;;
        esac
        timing_step_end ok
    fi

    timing_step_start "colmap_mapper"
    rm -rf "$SPARSE_DIR"/*
    echo "[step 4.4] mapper (CPU, threads=${MAPPER_THREADS})"
    _colmap_run mapper \
        --database_path "$DB_PATH" \
        --image_path "$IMG_DIR" \
        --output_path "$SPARSE_DIR" \
        --Mapper.num_threads "$MAPPER_THREADS" \
        --Mapper.ba_refine_focal_length 0 \
        --Mapper.ba_refine_principal_point 0 \
        --Mapper.ba_refine_extra_params 0 \
        2>&1 | tee "$LOG_DIR/mapper.log"
    timing_step_end ok

    BEST_MODEL=""
    for d in "$SPARSE_DIR"/*; do
        [[ -d "$d" && ( -f "$d/cameras.bin" || -f "$d/cameras.txt" ) ]] && BEST_MODEL="$d" && break
    done

    if [[ -n "$BEST_MODEL" && "$DO_UNDISTORT" == "1" ]]; then
        timing_step_start "colmap_undistort"
        echo "[step 4.5] image_undistorter → $DENSE_DIR (для OpenMVS)"
        mkdir -p "$DENSE_DIR"
        _colmap_run image_undistorter \
            --image_path "$IMG_DIR" \
            --input_path "$BEST_MODEL" \
            --output_path "$DENSE_DIR" \
            --output_type COLMAP \
            2>&1 | tee "$LOG_DIR/undistort.log"
        timing_step_end ok
    else
        timing_step_skip "colmap_undistort"
    fi
else
    timing_step_skip "colmap_sfm"
fi

echo ""
echo "================================================================"
echo " DONE"
echo " workspace : $OUTPUT"
echo " images    : $IMG_DIR ($N_IMG views)"
echo " sparse    : $SPARSE_DIR"
[[ -d "$DENSE_DIR" ]] && echo " undistort : $DENSE_DIR  → run_colmap360_mvs.sh --output $OUTPUT --skip-colmap"
echo " timing    : $OUTPUT/pipeline_timing.{log,tsv}"
echo "================================================================"
