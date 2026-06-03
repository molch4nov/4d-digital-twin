#!/usr/bin/env bash
# =============================================================================
# run_mvs_from_colmap.sh
#
# COLMAP workspace (run_colmap_360.sh) → OpenMVS dense mesh + textured GLB
# для просмотра в OPENMVGMVS_pipeline/scripts/viewer.html (three.js).
#
# Ожидаемый layout:
#   <colmap_dir>/images/              — исходные pinhole-виды
#   <colmap_dir>/sparse/<id>/         — SfM
#   <colmap_dir>/dense/               — image_undistorter (создаётся при необходимости)
#       images/
#       sparse/{cameras,images,points3D}.bin
#
# Выход (по умолчанию <colmap_dir>/mvs/):
#   scene.mvs, scene_dense*.mvs, *_texture.glb, *_web.glb (postprocess)
#
# Примеры:
#   ./run_mvs_from_colmap.sh --colmap-dir /data/scene_colmap
#
#   OPENMVS_BIN=/root/work/openMVS/make/bin ./run_mvs_from_colmap.sh --colmap-dir /data/out
#
#   # GPU: OpenMVS CUDA (Densify/Refine) + COLMAP через xvfb-run (COLMAP_USE_GPU=1)
#   ./run_mvs_from_colmap.sh --colmap-dir /data/out --gpu
#
#   ./run_mvs_from_colmap.sh --colmap-dir /data/out --skip-mvs --run-undistort
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pipeline_timing.sh
source "${SCRIPT_DIR}/pipeline_timing.sh"
TIMING_SCRIPT_NAME="run_mvs_from_colmap.sh"
PIPELINE_SCRIPTS="${PIPELINE_SCRIPTS:-/root/work/OPENMVGMVS_pipeline/scripts}"
POSTPROCESS_GLB="${POSTPROCESS_GLB:-${PIPELINE_SCRIPTS}/postprocess_glb.py}"
VIEWER_HTML="${VIEWER_HTML:-${PIPELINE_SCRIPTS}/viewer.html}"

COLMAP_DIR=""
MVS_DIR=""
SPARSE_MODEL=""
OPENMVS_BIN="${OPENMVS_BIN:-/root/work/openMVS/make/bin}"
THREADS="${THREADS:-$(nproc)}"
SKIP_UNDISTORT=0
SKIP_MVS=0
SKIP_REFINE="${SKIP_REFINE:-0}"
CLEAN_MVS="${CLEAN_MVS:-0}"
OPENMVS_TEXTURE_EXPORT="${OPENMVS_TEXTURE_EXPORT:-glb}"
OPENMVS_DENSIFY_EXTRA="${OPENMVS_DENSIFY_EXTRA:-}"
OPENMVS_RECON_EXTRA="${OPENMVS_RECON_EXTRA:-}"
OPENMVS_TEXTURE_EXTRA="${OPENMVS_TEXTURE_EXTRA:-}"
# Лимит граней ReconstructMesh (пусто = авто; 0 = без лимита).
OPENMVS_TARGET_FACE_NUM="${OPENMVS_TARGET_FACE_NUM:-}"
# RefineMesh: лимит граней (пусто = авто от числа кадров; иначе явное число).
OPENMVS_REFINE_MAX_FACES="${OPENMVS_REFINE_MAX_FACES:-}"
OPENMVS_REFINE_EXTRA="${OPENMVS_REFINE_EXTRA:-}"
# RefineMesh на CUDA (1 = --cuda-device -1, если сборка OpenMVS с CUDA).
OPENMVS_REFINE_CUDA="${OPENMVS_REFINE_CUDA:-1}"
# TextureMesh: лёгкий decimate только если mesh всё ещё очень тяжёлый.
OPENMVS_TEXTURE_MAX_FACES="${OPENMVS_TEXTURE_MAX_FACES:-1500000}"
OPENMVS_TEXTURE_DECIMATE="${OPENMVS_TEXTURE_DECIMATE:-}"
# CUDA для OpenMVS (DensifyPointCloud, RefineMesh). -1 = лучший GPU.
OPENMVS_CUDA="${OPENMVS_CUDA:-1}"
OPENMVS_CUDA_DEVICE="${OPENMVS_CUDA_DEVICE:--1}"
# COLMAP: xvfb-run при USE_GPU=1 (Qt/OpenGL headless).
USE_GPU=1
GLB_MAX_TEXTURE="${GLB_MAX_TEXTURE:-4096}"
GLB_ROTATION="${GLB_ROTATION:-auto}"
SKIP_GLB_POSTPROCESS="${SKIP_GLB_POSTPROCESS:-0}"

die() { echo "ERROR: $*" >&2; exit 1; }
print_step() { echo ""; echo "========== $* =========="; }

usage() {
    cat <<'EOF'
run_mvs_from_colmap.sh — COLMAP (pinhole) → OpenMVS → GLB для viewer.html

Обязательные:
  --colmap-dir PATH     Workspace run_colmap_360.sh

Опции:
  --mvs-dir PATH        Каталог OpenMVS (default: <colmap-dir>/mvs)
  --sparse-model ID     sparse/<id> если нет dense/ (default: лучшая модель)
  --skip-undistort      Не запускать image_undistorter
  --run-undistort       Только undistort, без OpenMVS
  --skip-mvs            Только подготовить dense/, без OpenMVS
  --skip-refine         Пропустить RefineMesh
  --clean-mvs           Удалить промежуточные артефакты MVS перед запуском
  --texture-export FMT  glb (default) | obj | gltf | ply
  --gpu / --no-gpu       OpenMVS CUDA + COLMAP xvfb (default: --gpu)
  OPENMVS_BIN, OPENMVS_CUDA, OPENMVS_CUDA_DEVICE, COLMAP_USE_GPU, COLMAP_BIN,
  OPENMVS_*_EXTRA, OPENMVS_TARGET_FACE_NUM, OPENMVS_REFINE_*, OPENMVS_TEXTURE_*, GLB_*

EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --colmap-dir)       COLMAP_DIR="$2"; shift 2 ;;
        --mvs-dir)          MVS_DIR="$2"; shift 2 ;;
        --sparse-model)     SPARSE_MODEL="$2"; shift 2 ;;
        --skip-undistort)   SKIP_UNDISTORT=1; shift ;;
        --run-undistort)    SKIP_MVS=1; shift ;;
        --skip-mvs)         SKIP_MVS=1; shift ;;
        --skip-refine)      SKIP_REFINE=1; shift ;;
        --clean-mvs)        CLEAN_MVS=1; shift ;;
        --texture-export)   OPENMVS_TEXTURE_EXPORT="$2"; shift 2 ;;
        --gpu)              USE_GPU=1; OPENMVS_CUDA=1; OPENMVS_REFINE_CUDA=1; shift ;;
        --no-gpu)           USE_GPU=0; OPENMVS_CUDA=0; OPENMVS_REFINE_CUDA=0; shift ;;
        -h|--help) usage ;;
        *) die "Неизвестный аргумент: $1" ;;
    esac
done

[[ -n "$COLMAP_DIR" ]] || die "--colmap-dir обязателен"
[[ -d "$COLMAP_DIR" ]] || die "Нет каталога: $COLMAP_DIR"
COLMAP_DIR="$(cd "$COLMAP_DIR" && pwd)"

timing_init "$COLMAP_DIR" "$TIMING_SCRIPT_NAME"
trap 'timing_finalize' EXIT

# COLMAP_USE_GPU=0|1 из env переопределяет --gpu / --no-gpu.
if [[ -n "${COLMAP_USE_GPU:-}" ]]; then
    USE_GPU="$COLMAP_USE_GPU"
fi
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

IMG_DIR="$COLMAP_DIR/images"
SPARSE_ROOT="$COLMAP_DIR/sparse"
DENSE_DIR="$COLMAP_DIR/dense"
[[ -n "$MVS_DIR" ]] || MVS_DIR="${COLMAP_DIR}/mvs"
mkdir -p "$MVS_DIR"
MVS_DIR="$(cd "$MVS_DIR" && pwd)"

export MKL_THREADING_LAYER="${MKL_THREADING_LAYER:-GNU}"

OPENBLAS_LAPACK_DIR="/usr/lib/x86_64-linux-gnu/openblas-openmp"
if [[ -d "$OPENBLAS_LAPACK_DIR" && -f "$OPENBLAS_LAPACK_DIR/liblapack.so.3" ]]; then
    case ":${LD_LIBRARY_PATH:-}:" in
        *":${OPENBLAS_LAPACK_DIR}:"*) ;;
        *) export LD_LIBRARY_PATH="${OPENBLAS_LAPACK_DIR}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
    esac
fi

_OPENMVS_HAS_CUDA=""
_openmvs_has_cuda() {
    [[ -n "$_OPENMVS_HAS_CUDA" ]] && { [[ "$_OPENMVS_HAS_CUDA" == "1" ]]; return; }
    if [[ -x "${OPENMVS_BIN}/DensifyPointCloud" ]] \
        && "${OPENMVS_BIN}/DensifyPointCloud" -h 2>&1 | grep -q 'cuda-device'; then
        _OPENMVS_HAS_CUDA=1
    else
        _OPENMVS_HAS_CUDA=0
    fi
    [[ "$_OPENMVS_HAS_CUDA" == "1" ]]
}

_openmvs_cuda_args() {
    [[ "$OPENMVS_CUDA" == "1" ]] && _openmvs_has_cuda || return 0
    echo "--cuda-device" "$OPENMVS_CUDA_DEVICE"
}

run_mvs() {
    local tool="$1"; shift
    if [[ -x "${OPENMVS_BIN}/${tool}" ]]; then
        "${OPENMVS_BIN}/${tool}" "$@"; return
    fi
    if command -v "${tool}" >/dev/null 2>&1; then
        "${tool}" "$@"; return
    fi
    die "${tool} не найден (OPENMVS_BIN=${OPENMVS_BIN})"
}

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
    local best="" n best_n=0
    for d in "$SPARSE_ROOT"/*/; do
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

_count_dense_images() {
    local dir="${1:-$DENSE_DIR/images}"
    [[ -d "$dir" ]] || dir="$IMG_DIR"
    find "$dir" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) 2>/dev/null | wc -l
}

_ply_face_count() {
    local ply="$1"
    [[ -f "$ply" ]] || return 1
    python3 -c "
import sys
n = 0
with open(sys.argv[1], 'rb') as f:
    for raw in f:
        line = raw.decode('ascii', errors='ignore').strip()
        if line.startswith('element face'):
            n = int(line.split()[2])
            break
        if line == 'end_header':
            break
print(n)
" "$ply"
}

_auto_target_face_num() {
    local n_images="$1"
    if [[ -n "$OPENMVS_TARGET_FACE_NUM" && "$OPENMVS_TARGET_FACE_NUM" != "auto" ]]; then
        echo "$OPENMVS_TARGET_FACE_NUM"
        return
    fi
    # Лимит под Refine+Texture на ~32GB RAM при сотнях кадров.
    if [[ "$n_images" -ge 400 ]]; then echo 500000
    elif [[ "$n_images" -ge 250 ]]; then echo 700000
    elif [[ "$n_images" -ge 120 ]]; then echo 900000
    else echo 0
    fi
}

_refine_safe_max_faces() {
    local n_images="$1"
    if [[ -n "$OPENMVS_REFINE_MAX_FACES" ]]; then
        echo "$OPENMVS_REFINE_MAX_FACES"
        return
    fi
    if [[ "$n_images" -ge 400 ]]; then echo 250000
    elif [[ "$n_images" -ge 250 ]]; then echo 350000
    elif [[ "$n_images" -ge 120 ]]; then echo 500000
    else echo 600000
    fi
}

_decimate_factor() {
    local faces="$1" max_faces="$2"
    python3 -c "f=int('$faces'); m=int('$max_faces'); print(f'{max(0.05,min(0.95,m/f)):.4f}')"
}

_refine_extra_args() {
    local faces="$1" n_images="$2"
    local safe_max
    safe_max="$(_refine_safe_max_faces "$n_images")"
    local -a args=(
        --reduce-memory 1
        --resolution-level 1
        --max-views 4
        --scales 1
    )
    if [[ "$n_images" -ge 350 ]]; then
        args=(
            --reduce-memory 1
            --resolution-level 2
            --max-views 3
            --scales 1
        )
    fi
    if [[ "$OPENMVS_REFINE_CUDA" == "1" ]] && _openmvs_has_cuda; then
        args+=(--cuda-device "$OPENMVS_CUDA_DEVICE")
    fi
    if [[ "$faces" -gt 0 && "$faces" -gt "$safe_max" ]]; then
        local d
        d="$(_decimate_factor "$faces" "$safe_max")"
        echo "[info] RefineMesh: ${faces} граней, лимит ${safe_max} (${n_images} кадров) → --decimate ${d}" >&2
        args+=(--decimate "$d")
    fi
    printf '%s\n' "${args[@]}"
}

_run_refine_mesh() {
    local faces="$1"
    local refine_mvs="$MVS_DIR/scene_dense_mesh_refine.mvs"
    local refine_ply="$MVS_DIR/scene_dense_mesh_refine.ply"
    local -a args

    # Попытка 1: CUDA/CPU по настройкам + авто-decimate
    mapfile -t args < <(_refine_extra_args "$faces" "$N_IMAGES")
    if run_mvs RefineMesh -w "$MVS_DIR" --max-threads "$MVS_THREADS" \
        "${args[@]}" \
        ${OPENMVS_REFINE_EXTRA:-} \
        "$MVS_DIR/scene_dense.mvs" \
        -m "$MVS_DIR/scene_dense_mesh.ply" \
        -o "$refine_mvs"; then
        [[ -f "$refine_ply" ]] && return 0
    fi

    # Попытка 2: CPU, меньше картинок/разрешение, сильнее decimate (460 кадров → OOM)
    local retry_max=200000
    [[ "$N_IMAGES" -lt 250 ]] && retry_max=350000
    local d_retry
    d_retry="$(_decimate_factor "${faces:-$retry_max}" "$retry_max")"
    echo "[warn] RefineMesh: повтор (CPU, decimate=${d_retry}, resolution-level=2)" >&2
    if run_mvs RefineMesh -w "$MVS_DIR" --max-threads "$MVS_THREADS" \
        --reduce-memory 1 \
        --resolution-level 2 \
        --max-views 3 \
        --scales 1 \
        --decimate "$d_retry" \
        ${OPENMVS_REFINE_EXTRA:-} \
        "$MVS_DIR/scene_dense.mvs" \
        -m "$MVS_DIR/scene_dense_mesh.ply" \
        -o "$refine_mvs"; then
        [[ -f "$refine_ply" ]] && return 0
    fi

    return 1
}

_texture_decimate_arg() {
    local faces="$1"
    local max_faces="$OPENMVS_TEXTURE_MAX_FACES"
    if [[ -n "$OPENMVS_TEXTURE_DECIMATE" ]]; then
        echo "--decimate $OPENMVS_TEXTURE_DECIMATE"
        return
    fi
    [[ "$faces" -gt 0 && "$max_faces" -gt 0 && "$faces" -gt "$max_faces" ]] || return 0
    python3 -c "import math; f=int('$faces'); m=int('$max_faces'); d=max(0.05,min(0.95,m/f)); print(f'--decimate {d:.4f}')"
}

_colmap_run() {
    local bin="${COLMAP_BIN:-}"
    if [[ -z "$bin" ]]; then
        bin="$(command -v colmap 2>/dev/null || true)"
    elif [[ -x "${bin}/colmap" ]]; then
        bin="${bin}/colmap"
    fi
    [[ -x "$bin" ]] || die "colmap не найден (COLMAP_BIN / PATH)"
    if [[ "$USE_GPU" == "1" ]]; then
        command -v xvfb-run >/dev/null || die "для COLMAP GPU нужен xvfb-run (apt install xvfb)"
        xvfb-run -a "$bin" "$@"
    else
        "$bin" "$@"
    fi
}

# ----------------------------- undistort ---------------------------------------
_ensure_dense_workspace() {
    if [[ -f "$DENSE_DIR/sparse/cameras.bin" && -d "$DENSE_DIR/images" ]]; then
        local n
        n=$(find "$DENSE_DIR/images" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.png' \) | wc -l)
        if [[ "$n" -gt 0 ]]; then
            timing_step_skip "colmap_undistort"
            return 0
        fi
    fi
    [[ "$SKIP_UNDISTORT" == "1" ]] && die "Нет $DENSE_DIR (undistorted) — уберите --skip-undistort или запустите run_colmap_360.sh с undistort"

    local model_dir=""
    if [[ -n "$SPARSE_MODEL" ]]; then
        model_dir="$SPARSE_ROOT/$SPARSE_MODEL"
        [[ -f "$model_dir/cameras.bin" ]] || die "sparse/$SPARSE_MODEL не найден"
    else
        model_dir="$(_pick_best_sparse_model)" || die "Нет sparse/*/cameras.bin в $SPARSE_ROOT"
    fi
    [[ -d "$IMG_DIR" ]] || die "Нет $IMG_DIR"

    print_step "COLMAP image_undistorter → $DENSE_DIR (модель: $model_dir)"
    timing_step_start "colmap_undistort"
    rm -rf "$DENSE_DIR"
    mkdir -p "$DENSE_DIR"
    _colmap_run image_undistorter \
        --image_path "$IMG_DIR" \
        --input_path "$model_dir" \
        --output_path "$DENSE_DIR" \
        --output_type COLMAP

    [[ -f "$DENSE_DIR/sparse/cameras.bin" ]] || die "image_undistorter не создал $DENSE_DIR/sparse/cameras.bin"
    timing_step_end ok
}

# ----------------------------- OpenMVS -----------------------------------------
if [[ "$SKIP_UNDISTORT" == "0" ]]; then
    _ensure_dense_workspace
else
    [[ -f "$DENSE_DIR/sparse/cameras.bin" ]] || die "--skip-undistort: нужен готовый $DENSE_DIR"
    timing_step_skip "colmap_undistort"
fi

if [[ "$SKIP_MVS" == "1" ]]; then
    print_step "Готово (без OpenMVS)"
    echo "  dense COLMAP : $DENSE_DIR"
    echo "  import       : InterfaceCOLMAP -w \"$MVS_DIR\" -i \"$DENSE_DIR/\" -o \"$MVS_DIR/scene.mvs\" --image-folder images"
    exit 0
fi

require_exec() { [[ -x "$1" ]] || die "Нет исполняемого: $1"; }
require_exec "${OPENMVS_BIN}/InterfaceCOLMAP"

MVS_THREADS="${OPENMVS_MAX_THREADS:-$THREADS}"
N_IMAGES="$(_count_dense_images)"
TARGET_FACE_NUM="$(_auto_target_face_num "$N_IMAGES")"
REFINE_SAFE_MAX="$(_refine_safe_max_faces "$N_IMAGES")"
echo "[info] Кадров: ${N_IMAGES}; target-face-num: ${TARGET_FACE_NUM:-off}; refine≤${REFINE_SAFE_MAX} граней; texture≤${OPENMVS_TEXTURE_MAX_FACES}"
if [[ "$USE_GPU" == "1" ]]; then
    echo "[info] COLMAP: xvfb-run (COLMAP_USE_GPU=1)"
else
    echo "[info] COLMAP: без xvfb (USE_GPU=0)"
fi
if [[ "$OPENMVS_CUDA" == "1" ]] && _openmvs_has_cuda; then
    echo "[info] OpenMVS CUDA: device=${OPENMVS_CUDA_DEVICE} (DensifyPointCloud, RefineMesh)"
elif [[ "$OPENMVS_CUDA" == "1" ]]; then
    echo "[warn] OpenMVS без CUDA в ${OPENMVS_BIN} — dense/refine на CPU"
    OPENMVS_CUDA=0
fi

print_step "OpenMVS: InterfaceCOLMAP"
timing_step_start "openmvs_interface_colmap"
if [[ "${OPENMVS_KEEP_DEPTH_MAPS:-}" != "1" ]]; then
    rm -f "$MVS_DIR"/depth*.dmap 2>/dev/null || true
fi
if [[ "$CLEAN_MVS" == "1" ]]; then
    rm -f "$MVS_DIR"/scene_dense* "$MVS_DIR"/scene_dense_mesh* \
          "$MVS_DIR"/scene_dense_mesh_refine* "$MVS_DIR"/*_texture* 2>/dev/null || true
fi

run_mvs InterfaceCOLMAP -w "$MVS_DIR" --max-threads "$MVS_THREADS" \
    -i "${DENSE_DIR}/" \
    -o "$MVS_DIR/scene.mvs" \
    --image-folder images
timing_step_end ok

print_step "OpenMVS: DensifyPointCloud"
timing_step_start "openmvs_densify"
DENSIFY_DEFAULTS=(
    --resolution-level 1
    --min-resolution 640
    --max-resolution 2400
    --number-views-fuse 3
    --filter-point-cloud 1
    --sub-resolution-levels 2
)
if [[ "$N_IMAGES" -ge 350 && -z "${OPENMVS_DENSIFY_EXTRA:-}" ]]; then
    DENSIFY_DEFAULTS=(--resolution-level 2 --min-resolution 640 --max-resolution 2000
        --number-views-fuse 3 --filter-point-cloud 1 --sub-resolution-levels 1)
    echo "[info] >= 350 кадров → Densify resolution-level 2 (меньше точек, тот же пайплайн)"
fi
mapfile -t DENSIFY_CUDA < <(_openmvs_cuda_args || true)
# shellcheck disable=SC2086
run_mvs DensifyPointCloud -w "$MVS_DIR" --max-threads "$MVS_THREADS" \
    "${DENSIFY_DEFAULTS[@]}" \
    "${DENSIFY_CUDA[@]}" \
    ${OPENMVS_DENSIFY_EXTRA:-} \
    "$MVS_DIR/scene.mvs"
timing_step_end ok

print_step "OpenMVS: ReconstructMesh"
timing_step_start "openmvs_reconstruct_mesh"
RECON_DEFAULTS=(
    --remove-spikes 1
    --close-holes 50
    --smooth 3
)
RECON_TARGET=()
if [[ -n "$TARGET_FACE_NUM" && "$TARGET_FACE_NUM" != "0" ]]; then
    RECON_TARGET=(--target-face-num "$TARGET_FACE_NUM")
fi
# shellcheck disable=SC2086
run_mvs ReconstructMesh -w "$MVS_DIR" --max-threads "$MVS_THREADS" \
    "${RECON_DEFAULTS[@]}" \
    "${RECON_TARGET[@]}" \
    ${OPENMVS_RECON_EXTRA:-} \
    "$MVS_DIR/scene_dense.mvs"
timing_step_end ok

MESH_PLY="$MVS_DIR/scene_dense_mesh.ply"
MESH_FACES=0
if [[ -f "$MESH_PLY" ]]; then
    MESH_FACES="$(_ply_face_count "$MESH_PLY" || echo 0)"
    echo "[info] ReconstructMesh: ${MESH_FACES} граней"
fi

if [[ "$SKIP_REFINE" == "1" ]]; then
    timing_step_skip "openmvs_refine_mesh"
    TEX_MESH_PLY="$MVS_DIR/scene_dense_mesh.ply"
    TEX_OUT_MVS="$MVS_DIR/scene_dense_mesh_texture.mvs"
else
    print_step "OpenMVS: RefineMesh"
    timing_step_start "openmvs_refine_mesh"
    if _run_refine_mesh "$MESH_FACES"; then
        TEX_MESH_PLY="$MVS_DIR/scene_dense_mesh_refine.ply"
        TEX_OUT_MVS="$MVS_DIR/scene_dense_mesh_refine_texture.mvs"
        MESH_FACES="$(_ply_face_count "$TEX_MESH_PLY" || echo "$MESH_FACES")"
        echo "[info] RefineMesh OK: ${MESH_FACES} граней"
        timing_step_end ok
    else
        timing_step_end fail
        die "RefineMesh упал после 2 попыток. Для 460 кадров: OPENMVS_TARGET_FACE_NUM=400000 OPENMVS_REFINE_MAX_FACES=200000 или rm mesh и перезапуск"
    fi
fi

case "${OPENMVS_TEXTURE_EXPORT,,}" in
    obj) TEX_MESH_EXPORT_EXT=".obj" ;;
    glb) TEX_MESH_EXPORT_EXT=".glb" ;;
    gltf) TEX_MESH_EXPORT_EXT=".gltf" ;;
    *) TEX_MESH_EXPORT_EXT=".ply" ;;
esac
TEX_MESH_EXPORT="${TEX_OUT_MVS%.mvs}${TEX_MESH_EXPORT_EXT}"

print_step "OpenMVS: TextureMesh (${OPENMVS_TEXTURE_EXPORT})"
timing_step_start "openmvs_texture_mesh"
[[ -f "$TEX_MESH_PLY" ]] || die "Нет mesh: $TEX_MESH_PLY"
if [[ "$MESH_FACES" == "0" ]]; then
    MESH_FACES="$(_ply_face_count "$TEX_MESH_PLY" || echo 0)"
fi
TEXTURE_DECIMATE="$(_texture_decimate_arg "$MESH_FACES")"
if [[ -n "$TEXTURE_DECIMATE" ]]; then
    echo "[info] TextureMesh: ${MESH_FACES} граней → ${TEXTURE_DECIMATE}"
fi
TEXTURE_DEFAULTS=(
    --max-texture-size 8192
    --global-seam-leveling 1
    --local-seam-leveling 1
    --sharpness-weight 0.7
    --outlier-threshold 0.06
)
# shellcheck disable=SC2086
run_mvs TextureMesh -w "$MVS_DIR" --max-threads "$MVS_THREADS" \
    --export-type "$OPENMVS_TEXTURE_EXPORT" \
    "${TEXTURE_DEFAULTS[@]}" \
    ${TEXTURE_DECIMATE:-} \
    ${OPENMVS_TEXTURE_EXTRA:-} \
    "$MVS_DIR/scene_dense.mvs" -m "$TEX_MESH_PLY" -o "$TEX_OUT_MVS" \
    || die "TextureMesh упал. Попробуйте OPENMVS_TEXTURE_MAX_FACES=1000000 или OPENMVS_TEXTURE_DECIMATE=0.5"
timing_step_end ok

WEB_GLB=""
if [[ "$SKIP_GLB_POSTPROCESS" != "1" ]] && [[ "${OPENMVS_TEXTURE_EXPORT,,}" == "glb" ]]; then
    if [[ -f "$TEX_MESH_EXPORT" && -f "$POSTPROCESS_GLB" ]]; then
        print_step "Post-process GLB (*_web.glb)"
        timing_step_start "glb_postprocess"
        WEB_GLB="${TEX_MESH_EXPORT%.glb}_web.glb"
        pp_args=( "$TEX_MESH_EXPORT" "$WEB_GLB" --rotation "$GLB_ROTATION" )
        if [[ "${GLB_MAX_TEXTURE:-0}" -gt 0 ]]; then
            pp_args+=( --max-texture "$GLB_MAX_TEXTURE" --jpeg )
        fi
        python3 "$POSTPROCESS_GLB" "${pp_args[@]}" || \
            echo "[warn] GLB post-process failed; raw: $TEX_MESH_EXPORT" >&2
        timing_step_end ok
    else
        timing_step_skip "glb_postprocess"
    fi
else
    timing_step_skip "glb_postprocess"
fi

print_step "Готово"
echo "  COLMAP dense : $DENSE_DIR"
echo "  OpenMVS      : $MVS_DIR/scene.mvs"
echo "  Textured     : $TEX_MESH_EXPORT"
[[ -n "$WEB_GLB" && -f "$WEB_GLB" ]] && echo "  Web GLB      : $WEB_GLB"
echo ""
echo "  timing       : $COLMAP_DIR/pipeline_timing.{log,tsv}"
echo ""
echo "Просмотр (HTTP, не file://):"
if [[ -n "$WEB_GLB" && -f "$WEB_GLB" ]]; then
    echo "  python3 -m http.server 8765 --directory \"$(dirname "$WEB_GLB")\""
    echo "  $VIEWER_HTML?src=http://127.0.0.1:8765/$(basename "$WEB_GLB")"
elif [[ -f "$TEX_MESH_EXPORT" ]]; then
    echo "  python3 -m http.server 8765 --directory \"$(dirname "$TEX_MESH_EXPORT")\""
    echo "  $VIEWER_HTML?src=http://127.0.0.1:8765/$(basename "$TEX_MESH_EXPORT")"
fi
