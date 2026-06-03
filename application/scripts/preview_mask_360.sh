#!/usr/bin/env bash
# =============================================================================
# preview_mask_360.sh
#
# Превью того, как ляжет --mask-bottom (и выбранный набор --views / --hfov /
# --vfov / --face-size) на конкретном ERP-кадре. Полезно подобрать высоту
# маски «по каске» БЕЗ запуска полного пайплайна.
#
# Выходы (в --output-dir):
#   preview_grid.jpg        - сетка всех видов с красной полупрозрачной маской
#   preview_v<k>.jpg        - каждый вид отдельно с маской + подписью yaw/pitch
#   preview_v<k>_clean.jpg  - тот же вид без маски (для сравнения)
#   erp_used.jpg            - сам ERP-кадр, на котором делалось превью
#   camera_mask.png         - бинарная маска формата COLMAP (white=use, black=ignore)
#
# Примеры:
#   # из 360-видео, кадр на 12.5 секунде:
#   ./preview_mask_360.sh \
#       --input /data/insta360.mp4 \
#       --time 12.5 \
#       --output-dir /tmp/mask_preview \
#       --mask-bottom 0.10
#
#   # из готового ERP-снимка:
#   ./preview_mask_360.sh \
#       --input /data/erp/erp_000042.jpg \
#       --output-dir /tmp/mask_preview \
#       --mask-bottom 0.08 \
#       --views "0,0;72,0;144,0;216,0;288,0;0,75"
#
#   # из папки с ERP-кадрами, по номеру:
#   ./preview_mask_360.sh \
#       --input /data/erp_frames \
#       --frame 0 \
#       --output-dir /tmp/mask_preview \
#       --mask-bottom 0.12
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pipeline_timing.sh
source "${SCRIPT_DIR}/pipeline_timing.sh"
TIMING_SCRIPT_NAME="preview_mask_360.sh"

# ----------------------------- defaults --------------------------------------
INPUT=""
OUTPUT_DIR=""
TIME=""
FRAME_IDX=""
FACE_SIZE=1024
HFOV=90
VFOV=90
# yaw должен быть в [-180, 180] для ffmpeg v360 (216° -> -144°, 288° -> -72°)
VIEWS_SPEC="0,0;72,0;144,0;-144,0;-72,0;0,75"
MASK_BOTTOM=0.08
OVERLAY_ALPHA=0.45   # прозрачность красной заливки на превью

usage() {
    sed -n '2,45p' "$0"
    exit 1
}

# ----------------------------- args ------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --input)        INPUT="$2"; shift 2 ;;
        --output-dir)   OUTPUT_DIR="$2"; shift 2 ;;
        --time)         TIME="$2"; shift 2 ;;
        --frame)        FRAME_IDX="$2"; shift 2 ;;
        --face-size)    FACE_SIZE="$2"; shift 2 ;;
        --hfov)         HFOV="$2"; shift 2 ;;
        --vfov)         VFOV="$2"; shift 2 ;;
        --views)        VIEWS_SPEC="$2"; shift 2 ;;
        --mask-bottom)  MASK_BOTTOM="$2"; shift 2 ;;
        --alpha)        OVERLAY_ALPHA="$2"; shift 2 ;;
        -h|--help)      usage ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

[[ -z "$INPUT" ]]      && { echo "ERROR: --input не задан";      usage; }
[[ -z "$OUTPUT_DIR" ]] && { echo "ERROR: --output-dir не задан"; usage; }
command -v ffmpeg  >/dev/null || { echo "ERROR: ffmpeg не найден";  exit 2; }
command -v ffprobe >/dev/null || { echo "ERROR: ffprobe не найден"; exit 2; }

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
ERP_USED="$OUTPUT_DIR/erp_used.jpg"

timing_init "$OUTPUT_DIR" "$TIMING_SCRIPT_NAME"
trap 'timing_finalize' EXIT

# ----------------------------- step 1: pick ONE ERP frame --------------------
timing_step_start "erp_frame"
echo "[step 1] Готовлю один ERP-кадр -> $ERP_USED"
if [[ -f "$INPUT" ]]; then
    # Это файл. Если у него «видеошные» атрибуты — обрабатываем как видео.
    if ffprobe -v error -select_streams v:0 -count_packets \
              -show_entries stream=nb_read_packets -of csv=p=0 "$INPUT" \
              | awk '$1+0 > 1 {exit 0} {exit 1}'; then
        # больше одного кадра -> видео
        if [[ -n "$TIME" ]]; then
            ffmpeg -hide_banner -loglevel error -y -ss "$TIME" -i "$INPUT" \
                -frames:v 1 -q:v 2 "$ERP_USED"
        elif [[ -n "$FRAME_IDX" ]]; then
            ffmpeg -hide_banner -loglevel error -y -i "$INPUT" \
                -vf "select=eq(n\,${FRAME_IDX})" -vsync vfr -frames:v 1 -q:v 2 "$ERP_USED"
        else
            # по умолчанию — кадр в середине ролика
            DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$INPUT")
            MID=$(awk -v d="$DUR" 'BEGIN{printf "%.2f", d/2.0}')
            echo "[info] ни --time, ни --frame не заданы; беру кадр на t=${MID}s"
            ffmpeg -hide_banner -loglevel error -y -ss "$MID" -i "$INPUT" \
                -frames:v 1 -q:v 2 "$ERP_USED"
        fi
    else
        # одиночная картинка
        cp -f "$INPUT" "$ERP_USED"
    fi
elif [[ -d "$INPUT" ]]; then
    # папка с ERP-кадрами
    mapfile -t LIST < <(find "$INPUT" -maxdepth 1 -type f \
                          \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | sort)
    [[ ${#LIST[@]} -eq 0 ]] && { echo "ERROR: в $INPUT нет картинок"; exit 3; }
    IDX="${FRAME_IDX:-0}"
    if [[ "$IDX" -ge "${#LIST[@]}" ]]; then
        echo "ERROR: --frame $IDX вне диапазона (всего ${#LIST[@]})"
        exit 3
    fi
    cp -f "${LIST[$IDX]}" "$ERP_USED"
else
    echo "ERROR: --input должен быть видео/картинкой/папкой"
    exit 3
fi

ERP_WH=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
                 -of csv=p=0:s=x "$ERP_USED")
echo "[info] ERP размер: $ERP_WH"
timing_step_end ok

# ----------------------------- step 2: mask size in px -----------------------
MASK_PX=$(awk -v s="$FACE_SIZE" -v m="$MASK_BOTTOM" 'BEGIN{ printf "%d", s*m + 0.5 }')
if [[ "$MASK_PX" -le 0 ]]; then
    echo "[warn] mask-bottom=${MASK_BOTTOM} -> 0 px; превью покажет проекции без маски."
    MASK_PX=0
fi
echo "[info] face_size=${FACE_SIZE}, mask_bottom=${MASK_BOTTOM} -> ${MASK_PX} px снизу"

# ----------------------------- step 3: save COLMAP-style binary mask ---------
MASK_PATH="$OUTPUT_DIR/camera_mask.png"
if [[ "$MASK_PX" -gt 0 ]]; then
    timing_step_start "colmap_mask_png"
    ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i "color=c=white:s=${FACE_SIZE}x${FACE_SIZE}:d=1" \
        -vf "drawbox=x=0:y=$((FACE_SIZE-MASK_PX)):w=${FACE_SIZE}:h=${MASK_PX}:color=black:t=fill" \
        -frames:v 1 "$MASK_PATH"
    echo "[info] COLMAP-маска сохранена: $MASK_PATH"
    timing_step_end ok
else
    timing_step_skip "colmap_mask_png"
fi

# ----------------------------- step 4: render each view ----------------------
timing_step_start "view_previews"
IFS=';' read -r -a VIEWS_ARR <<< "$VIEWS_SPEC"
N_VIEWS=${#VIEWS_ARR[@]}
echo "[step 4] Рисую $N_VIEWS видов (size=${FACE_SIZE}, hfov=${HFOV}, vfov=${VFOV})"

# чистим прошлые превью, чтобы tile не подхватил мусор
rm -f "$OUTPUT_DIR"/preview_v*.jpg

for k in "${!VIEWS_ARR[@]}"; do
    spec="${VIEWS_ARR[$k]}"
    yaw="${spec%%,*}"
    pitch="${spec##*,}"
    kk=$(printf "%02d" "$k")
    OUT_CLEAN="$OUTPUT_DIR/preview_v${kk}_clean.jpg"
    OUT_OVER="$OUTPUT_DIR/preview_v${kk}.jpg"
    echo "  [view $k] yaw=${yaw}  pitch=${pitch}"

    # 4.1 «чистая» проекция (для сравнения)
    ffmpeg -hide_banner -loglevel error -y -i "$ERP_USED" \
        -vf "v360=e:flat:yaw=${yaw}:pitch=${pitch}:roll=0:h_fov=${HFOV}:v_fov=${VFOV}:w=${FACE_SIZE}:h=${FACE_SIZE}:interp=cubic" \
        -frames:v 1 -q:v 2 "$OUT_CLEAN"

    # 4.2 проекция + красная полупрозрачная заливка по маске + подпись.
    # drawtext опционален: на некоторых сборках ffmpeg без libfreetype упадёт —
    # в этом случае пытаемся ещё раз без подписи.
    BASE_FILTER="v360=e:flat:yaw=${yaw}:pitch=${pitch}:roll=0:h_fov=${HFOV}:v_fov=${VFOV}:w=${FACE_SIZE}:h=${FACE_SIZE}:interp=cubic"
    OVERLAY_FILTER=""
    if [[ "$MASK_PX" -gt 0 ]]; then
        OVERLAY_FILTER=",drawbox=x=0:y=$((FACE_SIZE-MASK_PX)):w=${FACE_SIZE}:h=${MASK_PX}:color=red@${OVERLAY_ALPHA}:t=fill,drawbox=x=0:y=$((FACE_SIZE-MASK_PX)):w=${FACE_SIZE}:h=1:color=red:t=2"
    fi
    LABEL="yaw=${yaw} pitch=${pitch}"
    [[ "$MASK_PX" -gt 0 ]] && LABEL="${LABEL}  mask=${MASK_PX}px (${MASK_BOTTOM})"
    TEXT_FILTER=",drawtext=text='${LABEL}':x=20:y=20:fontcolor=white:fontsize=28:box=1:boxcolor=black@0.55:boxborderw=8"

    if ! ffmpeg -hide_banner -loglevel error -y -i "$ERP_USED" \
            -vf "${BASE_FILTER}${OVERLAY_FILTER}${TEXT_FILTER}" \
            -frames:v 1 -q:v 2 "$OUT_OVER" 2>/dev/null; then
        # фоллбэк без drawtext
        ffmpeg -hide_banner -loglevel error -y -i "$ERP_USED" \
            -vf "${BASE_FILTER}${OVERLAY_FILTER}" \
            -frames:v 1 -q:v 2 "$OUT_OVER"
    fi
done
timing_step_end ok

# ----------------------------- step 5: grid ----------------------------------
timing_step_start "preview_grid"
# Подбираем сетку поплотнее к квадрату.
COLS=$(awk -v n="$N_VIEWS" 'BEGIN{ c=int(sqrt(n)); if (c*c<n) c++; print c }')
ROWS=$(awk -v n="$N_VIEWS" -v c="$COLS" 'BEGIN{ r=int(n/c); if (r*c<n) r++; print r }')
echo "[step 5] Собираю сетку ${COLS}x${ROWS} -> preview_grid.jpg"

GRID_PATH="$OUTPUT_DIR/preview_grid.jpg"
# tile ждёт «видеопоток»: подаём картинки как image2-секвенцию.
ffmpeg -hide_banner -loglevel error -y \
    -framerate 1 -i "$OUTPUT_DIR/preview_v%02d.jpg" \
    -frames:v 1 \
    -vf "tile=${COLS}x${ROWS}:padding=8:margin=8:color=black" \
    -q:v 2 "$GRID_PATH"
timing_step_end ok

# ----------------------------- summary ---------------------------------------
echo ""
echo "================================================================"
echo " DONE"
echo " ERP кадр       : $ERP_USED"
[[ "$MASK_PX" -gt 0 ]] && echo " COLMAP-маска   : $MASK_PATH (${MASK_PX} px снизу)"
echo " виды (с маской): $OUTPUT_DIR/preview_v*.jpg"
echo " виды (без)     : $OUTPUT_DIR/preview_v*_clean.jpg"
echo " сетка превью   : $GRID_PATH"
echo " timing log     : $OUTPUT_DIR/pipeline_timing.log"
echo " timing tsv     : $OUTPUT_DIR/pipeline_timing.tsv"
echo "================================================================"
echo "Подсказка: если каска НЕ полностью закрыта красным —"
echo "  увеличь --mask-bottom (например 0.10 -> 0.14)."
echo "Если красным закрыта полезная сцена — уменьшай или подними виды по pitch."
