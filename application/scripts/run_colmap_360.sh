#!/usr/bin/env bash
# =============================================================================
# run_colmap_360.sh
#
# Получить COLMAP-сцену из 360°-материала (ERP, equirectangular) ОРИГИНАЛЬНЫМ
# COLMAP'ом (без SphereSfM / OpenSfM / etc.).
#
# Идея: оригинальный COLMAP не знает камеру SPHERE, поэтому каждый ERP-кадр
# проецируется в несколько перспективных (pinhole) видов с известным фокусом.
# Дальше — обычный SfM: feature_extractor (PINHOLE, shared camera, mask) ->
# matcher -> mapper -> (опционально) image_undistorter.
#
# Каска снизу панорамы либо отрезается углами/FOV проекции, либо маскируется
# через --ImageReader.camera_mask_path (одна маска на все изображения).
#
# Требования: ffmpeg (с фильтром v360), colmap, python3.
#
# Примеры запуска:
#   # из 360-видео:
#   ./run_colmap_360.sh \
#       --input  /data/insta360.mp4 \
#       --output /data/scene_colmap \
#       --fps 2
#
#   # из готовых ERP-снимков:
#   ./run_colmap_360.sh \
#       --input  /data/erp_frames \
#       --output /data/scene_colmap
#
#   # тонкая настройка:
#   ./run_colmap_360.sh \
#       --input /data/video.mp4 --output /data/out \
#       --fps 3 --face-size 1024 --hfov 90 --vfov 90 \
#       --views "0,0;72,0;144,0;216,0;288,0;0,60" \
#       --mask-bottom 0.08 \
#       --matcher sequential
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pipeline_timing.sh
source "${SCRIPT_DIR}/pipeline_timing.sh"
TIMING_SCRIPT_NAME="run_colmap_360.sh"

# ----------------------------- defaults --------------------------------------
INPUT=""
OUTPUT=""
FPS=2
FACE_SIZE=1024
HFOV=90
VFOV=90
# yaw,pitch пары через ";". Пять "горизонтальных" + один "вверх" (без каски).
# yaw в [-180, 180] для ffmpeg v360 (216° -> -144°, 288° -> -72°)
VIEWS_SPEC="0,0;72,0;144,0;-144,0;-72,0;0,75"
# Доля высоты кадра, замаскированная снизу (на случай если каска "вылезает"
# в нижний край горизонтальных видов). 0 -> маска не создаётся.
MASK_BOTTOM=0.0
# sequential | exhaustive | vocab_tree
MATCHER="sequential"
VOCAB_TREE_PATH=""
# Число GPU-потоков (-1 = авто).
GPU_INDEX="-1"
# SIFT GPU в COLMAP = OpenGL, не CUDA; на headless без DISPLAY падает.
# По умолчанию CPU; включить: --gpu или COLMAP_USE_GPU=1 (+ xvfb/EGL).
USE_GPU=0
# Дополнительные шаги после mapper.
DO_UNDISTORT=1
SKIP_FRAME_EXTRACT=0
SKIP_PROJECTION=0
SKIP_SFM=0
# Только mapper (+ undistort): database.db и images уже есть.
FROM_MAPPER=0

usage() {
    sed -n '2,50p' "$0"
    exit 1
}

# ----------------------------- args ------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --input)            INPUT="$2"; shift 2 ;;
        --output)           OUTPUT="$2"; shift 2 ;;
        --fps)              FPS="$2"; shift 2 ;;
        --face-size)        FACE_SIZE="$2"; shift 2 ;;
        --hfov)             HFOV="$2"; shift 2 ;;
        --vfov)             VFOV="$2"; shift 2 ;;
        --views)            VIEWS_SPEC="$2"; shift 2 ;;
        --mask-bottom)      MASK_BOTTOM="$2"; shift 2 ;;
        --matcher)          MATCHER="$2"; shift 2 ;;
        --vocab-tree)       VOCAB_TREE_PATH="$2"; shift 2 ;;
        --gpu-index)        GPU_INDEX="$2"; shift 2 ;;
        --gpu)              USE_GPU=1; shift ;;
        --no-gpu)           USE_GPU=0; shift ;;
        --no-undistort)     DO_UNDISTORT=0; shift ;;
        --skip-frames)      SKIP_FRAME_EXTRACT=1; shift ;;
        --skip-projection)  SKIP_PROJECTION=1; shift ;;
        --skip-sfm)         SKIP_SFM=1; shift ;;
        --from-mapper)      FROM_MAPPER=1; SKIP_FRAME_EXTRACT=1; SKIP_PROJECTION=1; shift ;;
        -h|--help)          usage ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

[[ -z "$INPUT"  ]] && { echo "ERROR: --input не задан";  usage; }
[[ -z "$OUTPUT" ]] && { echo "ERROR: --output не задан"; usage; }

# COLMAP_USE_GPU=1|0 переопределяет --gpu / --no-gpu (как в run_sphere360_3dgs.sh).
if [[ -n "${COLMAP_USE_GPU:-}" ]]; then
    USE_GPU="$COLMAP_USE_GPU"
fi

command -v ffmpeg >/dev/null || { echo "ERROR: ffmpeg не найден в PATH"; exit 2; }
command -v colmap >/dev/null || { echo "ERROR: colmap не найден в PATH"; exit 2; }

COLMAP_BIN="${COLMAP_BIN:-$(command -v colmap)}"

# Headless / SSH: COLMAP (Qt) defaults to xcb and aborts without DISPLAY.
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

# Ceres тянет liblapack.so.3 → по умолчанию MKL (ломается: __kmpc_global_thread_num).
OPENBLAS_LAPACK_DIR="/usr/lib/x86_64-linux-gnu/openblas-openmp"
if [[ -d "$OPENBLAS_LAPACK_DIR" && -f "$OPENBLAS_LAPACK_DIR/liblapack.so.3" ]]; then
    case ":${LD_LIBRARY_PATH:-}:" in
        *":${OPENBLAS_LAPACK_DIR}:"*) ;;
        *) export LD_LIBRARY_PATH="${OPENBLAS_LAPACK_DIR}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
    esac
fi

# Обёртка colmap: headless SIFT GPU через xvfb-run (OpenGL, не CUDA).
_colmap_run() {
    if [[ "$USE_GPU" == "1" && -z "${DISPLAY:-}" ]] && command -v xvfb-run >/dev/null; then
        xvfb-run -a "$COLMAP_BIN" "$@"
    else
        "$COLMAP_BIN" "$@"
    fi
}

# SIFT GPU требует OpenGL; на headless без xvfb — CPU.
if [[ "$USE_GPU" == "1" && -z "${DISPLAY:-}" ]] && ! command -v xvfb-run >/dev/null; then
    echo "[warn] Headless и нет xvfb-run: SIFT GPU недоступен, CPU."
    USE_GPU=0
fi

# ----------------------------- workspace -------------------------------------
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

# ----------------------------- intrinsics ------------------------------------
# Для плоской (pinhole) проекции с заданным FOV:
#   fx = (W/2) / tan(hfov/2);  fy = (H/2) / tan(vfov/2)
# W = H = FACE_SIZE.
_focal() {
    python3 -c "import math; s=float('$1'); fov=float('$2'); print(f'{(s/2)/math.tan(math.radians(fov/2)):.6f}')"
}
FX=$(_focal "$FACE_SIZE" "$HFOV")
FY=$(_focal "$FACE_SIZE" "$VFOV")
CX=$(python3 -c "print(f'{float(\"$FACE_SIZE\")/2:.6f}')")
CY=$(python3 -c "print(f'{float(\"$FACE_SIZE\")/2:.6f}')")
CAM_PARAMS="${FX},${FY},${CX},${CY}"

echo "[info] PINHOLE intrinsics: W=H=${FACE_SIZE}, fx=${FX}, fy=${FY}, cx=${CX}, cy=${CY}"

# ----------------------------- step 1: ERP frames ----------------------------
is_video=0
if [[ -f "$INPUT" ]]; then
    is_video=1
elif [[ -d "$INPUT" ]]; then
    is_video=0
else
    echo "ERROR: --input должен быть файлом видео или папкой с ERP-кадрами"
    exit 3
fi

if [[ "$SKIP_FRAME_EXTRACT" == "0" ]]; then
    timing_step_start "erp_extract"
    if [[ "$is_video" == "1" ]]; then
        echo "[step 1] Извлекаю ERP-кадры из видео @ ${FPS} fps -> $ERP_DIR"
        rm -f "$ERP_DIR"/*.jpg 2>/dev/null || true
        ffmpeg -hide_banner -loglevel error -y \
            -i "$INPUT" \
            -vf "fps=${FPS}" \
            -q:v 2 \
            -start_number 0 \
            "$ERP_DIR/erp_%06d.jpg"
    else
        echo "[step 1] Использую готовые ERP-кадры из $INPUT (копирую/линкую в $ERP_DIR)"
        rm -f "$ERP_DIR"/*.jpg 2>/dev/null || true
        i=0
        # Сортируем по имени, перенумеровываем для стабильного порядка.
        while IFS= read -r f; do
            ln -sf "$f" "$(printf "%s/erp_%06d.jpg" "$ERP_DIR" "$i")"
            i=$((i+1))
        done < <(find "$INPUT" -maxdepth 1 -type f \
                    \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | sort)
    fi
    timing_step_end ok
else
    timing_step_skip "erp_extract"
fi

N_ERP=$(find "$ERP_DIR" -maxdepth 1 -name 'erp_*.jpg' | wc -l)
echo "[info] ERP-кадров: $N_ERP"
[[ "$N_ERP" -lt 2 ]] && { echo "ERROR: слишком мало ERP-кадров"; exit 4; }

# Узнаём размеры ERP (нужно для sanity-чека).
ERP_FIRST="$(find "$ERP_DIR" -maxdepth 1 -name 'erp_*.jpg' | sort | head -n1)"
ERP_WH=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
                 -of csv=p=0:s=x "$ERP_FIRST")
echo "[info] ERP размер: $ERP_WH"

# ----------------------------- step 2: ERP -> perspective views --------------
# Разбираем VIEWS_SPEC -> массив пар "yaw,pitch".
IFS=';' read -r -a VIEWS_ARR <<< "$VIEWS_SPEC"
N_VIEWS=${#VIEWS_ARR[@]}
echo "[info] Видов на кадр: $N_VIEWS  (spec: $VIEWS_SPEC)"

if [[ "$SKIP_PROJECTION" == "0" ]]; then
    timing_step_start "erp_to_perspective"
    echo "[step 2] Проецирую ERP -> $N_VIEWS перспективных видов (size=${FACE_SIZE}, hfov=${HFOV}, vfov=${VFOV})"
    rm -f "$IMG_DIR"/*.jpg 2>/dev/null || true

    # ВАЖНО: имена файлов формируем как frame_<NNNNNN>_v<K>.jpg,
    # чтобы при сортировке по имени соседние ракурсы одного кадра шли подряд.
    # Это даёт хорошее поведение sequential_matcher с overlap >= N_VIEWS.
    for k in "${!VIEWS_ARR[@]}"; do
        spec="${VIEWS_ARR[$k]}"
        yaw="${spec%%,*}"
        pitch="${spec##*,}"
        echo "  [view $k] yaw=${yaw}  pitch=${pitch}"

        TMP_VDIR="$OUTPUT/_proj_v${k}"
        mkdir -p "$TMP_VDIR"
        rm -f "$TMP_VDIR"/*.jpg 2>/dev/null || true

        # v360: e (equirect) -> flat (perspective).
        # yaw/pitch/roll в градусах; h_fov/v_fov — итоговое поле зрения.
        ffmpeg -hide_banner -loglevel error -y \
            -f image2 -framerate 30 -i "$ERP_DIR/erp_%06d.jpg" \
            -vf "v360=e:flat:yaw=${yaw}:pitch=${pitch}:roll=0:h_fov=${HFOV}:v_fov=${VFOV}:w=${FACE_SIZE}:h=${FACE_SIZE}:interp=cubic" \
            -q:v 2 -start_number 0 \
            "$TMP_VDIR/proj_%06d.jpg"

        # Переименовываем во "frame_<NNNNNN>_v<K>.jpg" в общую папку.
        while IFS= read -r f; do
            n=$(basename "$f" .jpg)
            n=${n#proj_}
            mv "$f" "$IMG_DIR/frame_${n}_v${k}.jpg"
        done < <(find "$TMP_VDIR" -maxdepth 1 -name 'proj_*.jpg' | sort)

        rmdir "$TMP_VDIR" || true
    done
    timing_step_end ok
else
    timing_step_skip "erp_to_perspective"
fi

N_IMG=$(find "$IMG_DIR" -maxdepth 1 -name '*.jpg' | wc -l)
echo "[info] Сгенерировано перспективных изображений: $N_IMG"
[[ "$N_IMG" -lt 20 ]] && echo "[warn] Очень мало изображений — реконструкция может развалиться."

# ----------------------------- step 3: mask (helmet) -------------------------
USE_MASK=0
MASK_ARG=()
if awk -v m="$MASK_BOTTOM" 'BEGIN{ exit !(m+0 > 0) }'; then
    MASK_PX=$(awk -v s="$FACE_SIZE" -v m="$MASK_BOTTOM" 'BEGIN{ printf "%d", s*m + 0.5 }')
    if [[ "$MASK_PX" -gt 0 ]]; then
        timing_step_start "helmet_mask"
        echo "[step 3] Делаю общую маску ${FACE_SIZE}x${FACE_SIZE}, нижние ${MASK_PX}px закрыты (каска)"
        # Белое = использовать, чёрное = игнорировать (формат COLMAP).
        ffmpeg -hide_banner -loglevel error -y \
            -f lavfi -i "color=c=white:s=${FACE_SIZE}x${FACE_SIZE}:d=1" \
            -vf "drawbox=x=0:y=$((FACE_SIZE-MASK_PX)):w=${FACE_SIZE}:h=${MASK_PX}:color=black:t=fill" \
            -frames:v 1 "$MASK_PATH"
        USE_MASK=1
        MASK_ARG=(--ImageReader.camera_mask_path "$MASK_PATH")
        timing_step_end ok
    fi
fi
[[ "$USE_MASK" == "0" ]] && { echo "[step 3] Маска не используется (mask-bottom=0)."; timing_step_skip "helmet_mask"; }

# ----------------------------- step 4: COLMAP SfM ----------------------------
if [[ "$SKIP_SFM" == "0" ]]; then
    echo "[info] COLMAP: $COLMAP_BIN"
    echo "[info] LAPACK: ${OPENBLAS_LAPACK_DIR:-system} (обход MKL/OpenMP)"
    echo "[info] SIFT use_gpu=${USE_GPU}"

    if [[ "$FROM_MAPPER" == "1" ]]; then
        [[ -f "$DB_PATH" ]] || { echo "ERROR: --from-mapper: нет $DB_PATH"; exit 7; }
        echo "[step 4] --from-mapper: пропускаю extract/match, только mapper"
        timing_step_skip "colmap_database_creator"
        timing_step_skip "colmap_feature_extractor"
        timing_step_skip "colmap_matcher"
    else
    # ---- 4.1 DB ----
    if [[ -f "$DB_PATH" ]]; then
        echo "[step 4.1] Удаляю старую БД $DB_PATH"
        rm -f "$DB_PATH"
    fi
    timing_step_start "colmap_database_creator"
    echo "[step 4.1] Создаю БД"
    _colmap_run database_creator --database_path "$DB_PATH"
    timing_step_end ok

    # ---- 4.2 feature_extractor ----
    timing_step_start "colmap_feature_extractor"
    echo "[step 4.2] feature_extractor (PINHOLE, single_camera=1, fx=${FX}, fy=${FY}, cx=${CX}, cy=${CY})"
    _colmap_run feature_extractor \
        --database_path  "$DB_PATH" \
        --image_path     "$IMG_DIR" \
        --ImageReader.single_camera 1 \
        --ImageReader.camera_model  PINHOLE \
        --ImageReader.camera_params "$CAM_PARAMS" \
        "${MASK_ARG[@]}" \
        --SiftExtraction.use_gpu  $USE_GPU \
        --SiftExtraction.gpu_index $GPU_INDEX \
        2>&1 | tee "$LOG_DIR/feature_extractor.log"
    timing_step_end ok

    # ---- 4.3 matching ----
    timing_step_start "colmap_matcher"
    case "$MATCHER" in
        sequential)
            # Каждый кадр даёт N_VIEWS соседних имён; берём с запасом.
            OVERLAP=$(( N_VIEWS * 5 ))
            [[ $OVERLAP -lt 10 ]] && OVERLAP=10
            echo "[step 4.3] sequential_matcher (overlap=$OVERLAP)"
            _colmap_run sequential_matcher \
                --database_path "$DB_PATH" \
                --SequentialMatching.overlap $OVERLAP \
                --SequentialMatching.quadratic_overlap 1 \
                --SiftMatching.use_gpu  $USE_GPU \
                --SiftMatching.gpu_index $GPU_INDEX \
                2>&1 | tee "$LOG_DIR/matcher.log"
            ;;
        exhaustive)
            echo "[step 4.3] exhaustive_matcher"
            _colmap_run exhaustive_matcher \
                --database_path "$DB_PATH" \
                --SiftMatching.use_gpu  $USE_GPU \
                --SiftMatching.gpu_index $GPU_INDEX \
                2>&1 | tee "$LOG_DIR/matcher.log"
            ;;
        vocab_tree)
            [[ -z "$VOCAB_TREE_PATH" ]] && { echo "ERROR: для vocab_tree нужен --vocab-tree PATH"; exit 5; }
            echo "[step 4.3] vocab_tree_matcher"
            _colmap_run vocab_tree_matcher \
                --database_path "$DB_PATH" \
                --VocabTreeMatching.vocab_tree_path "$VOCAB_TREE_PATH" \
                --SiftMatching.use_gpu  $USE_GPU \
                --SiftMatching.gpu_index $GPU_INDEX \
                2>&1 | tee "$LOG_DIR/matcher.log"
            ;;
        *)
            echo "ERROR: неизвестный --matcher: $MATCHER"; exit 6 ;;
    esac
    timing_step_end ok
    fi

    # ---- 4.4 mapper ----
    timing_step_start "colmap_mapper"
    rm -rf "$SPARSE_DIR"/*
    echo "[step 4.4] mapper (intrinsics зафиксированы)"
    _colmap_run mapper \
        --database_path "$DB_PATH" \
        --image_path    "$IMG_DIR" \
        --output_path   "$SPARSE_DIR" \
        --Mapper.ba_refine_focal_length    0 \
        --Mapper.ba_refine_principal_point 0 \
        --Mapper.ba_refine_extra_params    0 \
        2>&1 | tee "$LOG_DIR/mapper.log"
    timing_step_end ok

    # ---- 4.5 (опц.) undistort для дальнейшего dense / 3DGS ------------------
    # PINHOLE и так "без дисторсии", но image_undistorter готовит
    # стандартный workspace (dense/{images, sparse, stereo}) для patch_match.
    BEST_MODEL=""
    for d in "$SPARSE_DIR"/*; do
        [[ -d "$d" ]] || continue
        if [[ -f "$d/cameras.bin" || -f "$d/cameras.txt" ]]; then
            BEST_MODEL="$d"
            break
        fi
    done

    if [[ -z "$BEST_MODEL" ]]; then
        echo "[warn] mapper не построил ни одной модели; пропускаю undistort."
        timing_step_skip "colmap_undistort"
    elif [[ "$DO_UNDISTORT" == "1" ]]; then
        timing_step_start "colmap_undistort"
        echo "[step 4.5] image_undistorter -> $DENSE_DIR (модель: $BEST_MODEL)"
        mkdir -p "$DENSE_DIR"
        _colmap_run image_undistorter \
            --image_path   "$IMG_DIR" \
            --input_path   "$BEST_MODEL" \
            --output_path  "$DENSE_DIR" \
            --output_type  COLMAP \
            2>&1 | tee "$LOG_DIR/undistort.log"
        timing_step_end ok
    else
        timing_step_skip "colmap_undistort"
    fi
else
    timing_step_skip "colmap_sfm"
fi

# ----------------------------- summary ---------------------------------------
echo ""
echo "================================================================"
echo " DONE"
echo " workspace : $OUTPUT"
echo " ERP       : $ERP_DIR ($N_ERP кадров)"
echo " images    : $IMG_DIR ($N_IMG перспективных видов)"
[[ "$USE_MASK" == "1" ]] && echo " mask      : $MASK_PATH"
echo " sparse    : $SPARSE_DIR/<id>/   (открыть: colmap gui --import_path ...)"
[[ "$DO_UNDISTORT" == "1" && -d "$DENSE_DIR" ]] && echo " dense ws  : $DENSE_DIR  (готово для patch_match_stereo / 3DGS)"
echo " timing    : $OUTPUT/pipeline_timing.{log,tsv}"
echo "================================================================"
