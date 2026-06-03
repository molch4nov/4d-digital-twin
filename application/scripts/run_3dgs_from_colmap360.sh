#!/usr/bin/env bash
# =============================================================================
# run_3dgs_from_colmap360.sh
#
# Запуск graphdeco-inria/gaussian-splatting на выходе run_colmap_360.sh.
#
# Ожидаемый layout после run_colmap_360.sh:
#   <colmap_dir>/images/          — перспективные виды (все)
#   <colmap_dir>/sparse/<id>/     — модели mapper
#   <colmap_dir>/dense/           — image_undistorter (рекомендуется для 3DGS)
#       images/
#       sparse/{cameras,images,points3D}.bin
#
# Скрипт собирает каталог для train.py (-s):
#   <scene_dir>/images/
#   <scene_dir>/sparse/0/*.bin
# и вызывает train.py из репозитория gaussian-splatting.
#
# Требования:
#   conda env gaussian_splatting (PyTorch + CUDA + submodules)
#
# Примеры:
#   conda activate gaussian_splatting
#   ./run_3dgs_from_colmap360.sh \
#       --colmap-dir /data/output_clear_colmap
#
#   ./run_3dgs_from_colmap360.sh \
#       --colmap-dir /data/output_clear_colmap \
#       --scene-dir /data/timoshkino_3dgs_scene \
#       --model-dir /data/timoshkino_3dgs_out \
#       -- --iterations 30000 --quiet
#
#   SKIP_TRAIN=1 ./run_3dgs_from_colmap360.sh --colmap-dir /data/out
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pipeline_timing.sh
source "${SCRIPT_DIR}/pipeline_timing.sh"
TIMING_SCRIPT_NAME="run_3dgs_from_colmap360.sh"

COLMAP_DIR=""
SCENE_DIR=""
MODEL_DIR=""
GS_REPO="${GS_REPO:-/root/work/gaussian-splatting}"
USE_DENSE=1
SPARSE_MODEL=""
SKIP_PREPARE=0
SKIP_TRAIN="${SKIP_TRAIN:-0}"
TRAIN_DATA_DEVICE="${TRAIN_DATA_DEVICE:-cpu}"
TRAIN_RESOLUTION="${TRAIN_RESOLUTION:-}"
TRAIN_EVAL=0
TRAIN_PASS=()

die() { echo "ERROR: $*" >&2; exit 1; }
print_step() { echo ""; echo "========== $* =========="; }

usage() {
    sed -n '2,40p' "$0"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --colmap-dir)   COLMAP_DIR="$2"; shift 2 ;;
        --scene-dir)    SCENE_DIR="$2"; shift 2 ;;
        --model-dir)    MODEL_DIR="$2"; shift 2 ;;
        --gs-repo)      GS_REPO="$2"; shift 2 ;;
        --sparse-model) SPARSE_MODEL="$2"; shift 2 ;;
        --use-dense)    USE_DENSE=1; shift ;;
        --no-dense)     USE_DENSE=0; shift ;;
        --skip-prepare) SKIP_PREPARE=1; shift ;;
        --skip-train)   SKIP_TRAIN=1; shift ;;
        --eval)         TRAIN_EVAL=1; shift ;;
        --data-device)  TRAIN_DATA_DEVICE="$2"; shift 2 ;;
        --resolution|-r) TRAIN_RESOLUTION="$2"; shift 2 ;;
        --) shift; TRAIN_PASS=("$@"); break ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

[[ -z "$COLMAP_DIR" ]] && die "--colmap-dir обязателен"
[[ -d "$COLMAP_DIR" ]] || die "Нет каталога: $COLMAP_DIR"
COLMAP_DIR="$(cd "$COLMAP_DIR" && pwd)"

timing_init "$COLMAP_DIR" "$TIMING_SCRIPT_NAME"
trap 'timing_finalize' EXIT

[[ -d "$GS_REPO" && -f "$GS_REPO/train.py" ]] || die "gaussian-splatting не найден: $GS_REPO (задайте GS_REPO)"

if [[ -z "$SCENE_DIR" ]]; then
    SCENE_DIR="${COLMAP_DIR}/_3dgs_scene"
fi
if [[ -z "$MODEL_DIR" ]]; then
    MODEL_DIR="${SCENE_DIR}/output"
fi
SCENE_DIR="$(mkdir -p "$SCENE_DIR" && cd "$SCENE_DIR" && pwd)"
MODEL_DIR="$(mkdir -p "$MODEL_DIR" && cd "$MODEL_DIR" && pwd)"

# Python для train.py (предпочитаем env gaussian_splatting, не base)
_GS_PY="/root/miniconda3/envs/gaussian_splatting/bin/python"
if [[ -z "${PYTHON:-}" ]]; then
    if [[ -n "${CONDA_PREFIX:-}" && "${CONDA_PREFIX##*/}" == "gaussian_splatting" && -x "${CONDA_PREFIX}/bin/python" ]]; then
        PYTHON="${CONDA_PREFIX}/bin/python"
    elif [[ -x "$_GS_PY" ]]; then
        PYTHON="$_GS_PY"
    elif [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/python" ]]; then
        PYTHON="${CONDA_PREFIX}/bin/python"
    else
        PYTHON="python3"
    fi
fi

export MKL_THREADING_LAYER="${MKL_THREADING_LAYER:-GNU}"

_count_sparse_images() {
    local images_bin="$1"
    [[ -f "$images_bin" ]] || return 1
    python3 -c "
import struct, sys
with open(sys.argv[1], 'rb') as f:
    print(struct.unpack('<Q', f.read(8))[0])
" "$images_bin"
}

_pick_best_sparse_model() {
    local sparse_root="$1" best="" n best_n=0
    for d in "$sparse_root"/*/; do
        [[ -f "${d}images.bin" ]] || continue
        n="$(_count_sparse_images "${d}images.bin")"
        if [[ "$n" -gt "$best_n" ]]; then
            best_n="$n"
            best="${d%/}"
        fi
    done
    [[ -n "$best" ]] || return 1
    echo "$best"
}

_prepare_scene() {
    timing_step_start "prepare_3dgs_scene"
    local src_images src_sparse
    rm -rf "$SCENE_DIR/images" "$SCENE_DIR/sparse"
    mkdir -p "$SCENE_DIR/images" "$SCENE_DIR/sparse/0"

    if [[ "$USE_DENSE" == "1" && -f "$COLMAP_DIR/dense/sparse/cameras.bin" ]]; then
        print_step "3DGS dataset: dense/ (undistorted, image_undistorter)"
        src_images="$COLMAP_DIR/dense/images"
        src_sparse="$COLMAP_DIR/dense/sparse"
    else
        local model_dir=""
        if [[ -n "$SPARSE_MODEL" ]]; then
            model_dir="$COLMAP_DIR/sparse/$SPARSE_MODEL"
            [[ -f "$model_dir/images.bin" ]] || die "sparse/$SPARSE_MODEL не найден"
        else
            model_dir="$(_pick_best_sparse_model "$COLMAP_DIR/sparse")" \
                || die "Нет sparse/*/images.bin в $COLMAP_DIR/sparse"
        fi
        print_step "3DGS dataset: sparse/$(basename "$model_dir") + images/"
        src_images="$COLMAP_DIR/images"
        src_sparse="$model_dir"
    fi

    [[ -d "$src_images" ]] || die "Нет каталога изображений: $src_images"
    local nimg
    nimg=$(find "$src_images" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.png' \) | wc -l)
    [[ "$nimg" -gt 0 ]] || die "Пустой $src_images"

    cp -a "$src_images/." "$SCENE_DIR/images/"
    cp -a "$src_sparse/cameras.bin" "$src_sparse/images.bin" "$src_sparse/points3D.bin" \
        "$SCENE_DIR/sparse/0/"

    local ncam
    ncam="$(_count_sparse_images "$SCENE_DIR/sparse/0/images.bin")"
    echo "[info] images on disk: $nimg"
    echo "[info] cameras in sparse/0: $ncam"
    [[ "$ncam" -ge 2 ]] || die "Слишком мало зарегистрированных камер ($ncam) — SfM не пригоден для 3DGS"
    timing_step_end ok
}

# ----------------------------- prepare ---------------------------------------
if [[ "$SKIP_PREPARE" == "0" ]]; then
    _prepare_scene
else
    [[ -f "$SCENE_DIR/sparse/0/cameras.bin" ]] || die "--skip-prepare: нет $SCENE_DIR/sparse/0/cameras.bin"
    timing_step_skip "prepare_3dgs_scene"
fi

# ----------------------------- train -----------------------------------------
if [[ "$SKIP_TRAIN" == "1" ]]; then
    timing_step_skip "train_3dgs"
    print_step "SKIP_TRAIN=1 — датасет готов"
    echo "  scene : $SCENE_DIR"
    echo "  train : cd \"$GS_REPO\" && $PYTHON train.py -s \"$SCENE_DIR\" -m \"$MODEL_DIR\" --data_device $TRAIN_DATA_DEVICE"
    exit 0
fi

if ! "$PYTHON" -c "import torch; assert torch.cuda.is_available()" >/dev/null 2>&1; then
    die "CUDA/PyTorch недоступны в $PYTHON — выполните: conda activate gaussian_splatting"
fi

print_step "train.py (3D Gaussian Splatting)"
timing_step_start "train_3dgs"
NCAM="$(_count_sparse_images "$SCENE_DIR/sparse/0/images.bin")"
echo "[info] repo   : $GS_REPO"
echo "[info] python : $PYTHON"
echo "[info] views  : $NCAM  data_device=$TRAIN_DATA_DEVICE  resolution=${TRAIN_RESOLUTION:-auto}"

TRAIN_ARGS=(--data_device "$TRAIN_DATA_DEVICE")
[[ -n "$TRAIN_RESOLUTION" ]] && TRAIN_ARGS+=(-r "$TRAIN_RESOLUTION")
[[ "$TRAIN_EVAL" == "1" ]] && TRAIN_ARGS+=(--eval)

if [[ "$NCAM" -gt 400 && "$TRAIN_DATA_DEVICE" == "cuda" ]]; then
    echo "[warn] ${NCAM} views + data_device=cuda часто даёт OOM; попробуйте --data-device cpu"
fi

cd "$GS_REPO"
"$PYTHON" train.py -s "$SCENE_DIR" -m "$MODEL_DIR" "${TRAIN_ARGS[@]}" "${TRAIN_PASS[@]}"
timing_step_end ok

print_step "Готово"
echo "  scene  : $SCENE_DIR"
echo "  model  : $MODEL_DIR"
echo "  ply    : $MODEL_DIR/point_cloud/iteration_*/point_cloud.ply"
echo "  render : cd \"$GS_REPO\" && $PYTHON render.py -m \"$MODEL_DIR\" -s \"$SCENE_DIR\""
