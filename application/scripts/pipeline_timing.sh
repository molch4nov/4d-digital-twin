# pipeline_timing.sh — замеры времени по шагам (source из пайплайнов application/scripts)
#
# Файлы в каталоге workspace (OUTPUT / COLMAP_DIR):
#   pipeline_timing.log  — человекочитаемый лог
#   pipeline_timing.tsv    — machine-readable (append)
#
# Env:
#   TIMING_DISABLE=1       — отключить замеры
#   TIMING_RUN_ID          — общий id прогона (пробрасывается в дочерние скрипты)
#   TIMING_WORKSPACE       — каталог логов (если задан до timing_init)

[[ -n "${_PIPELINE_TIMING_LOADED:-}" ]] && return 0
_PIPELINE_TIMING_LOADED=1

_timing_now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
_timing_now_sec() { date +%s; }

timing_init() {
    local workspace="${1:-}"
    local script_name="${2:-${TIMING_SCRIPT_NAME:-unknown}}"

    [[ "${TIMING_DISABLE:-0}" == "1" ]] && return 0
    [[ -n "$workspace" ]] || return 0

    TIMING_WORKSPACE="$workspace"
    TIMING_SCRIPT="$script_name"
    mkdir -p "$TIMING_WORKSPACE"

    TIMING_LOG="${TIMING_WORKSPACE}/pipeline_timing.log"
    TIMING_TSV="${TIMING_WORKSPACE}/pipeline_timing.tsv"

    if [[ -z "${TIMING_RUN_ID:-}" ]]; then
        TIMING_RUN_ID="$(_timing_now_utc)_$$"
        export TIMING_RUN_ID
    fi
    export TIMING_SCRIPT TIMING_WORKSPACE TIMING_LOG TIMING_TSV

    TIMING_PIPELINE_STARTED="$(_timing_now_utc)"
    TIMING_PIPELINE_START_SEC="$(_timing_now_sec)"

    if [[ ! -f "$TIMING_TSV" ]]; then
        printf 'run_id\tscript\tstep\tstatus\tseconds\tstart_utc\tend_utc\n' >>"$TIMING_TSV"
    fi

    {
        echo ""
        echo "========== pipeline run ${TIMING_RUN_ID} =========="
        echo "script   : ${TIMING_SCRIPT}"
        echo "started  : ${TIMING_PIPELINE_STARTED}"
        echo "workspace: ${TIMING_WORKSPACE}"
    } | tee -a "$TIMING_LOG"
}

timing_step_start() {
    [[ "${TIMING_DISABLE:-0}" == "1" ]] && return 0
    [[ -n "${TIMING_LOG:-}" ]] || return 0

    _TIMING_CUR_STEP="$1"
    _TIMING_CUR_START_SEC="$(_timing_now_sec)"
    _TIMING_CUR_START_UTC="$(_timing_now_utc)"
    echo "[timing] START  ${TIMING_SCRIPT} :: ${_TIMING_CUR_STEP}  (${_TIMING_CUR_START_UTC})" | tee -a "$TIMING_LOG"
}

timing_step_end() {
    local status="${1:-ok}"
    [[ "${TIMING_DISABLE:-0}" == "1" ]] && return 0
    [[ -n "${TIMING_TSV:-}" ]] || return 0
    [[ -n "${_TIMING_CUR_STEP:-}" ]] || return 0

    local end_sec end_utc dur
    end_sec="$(_timing_now_sec)"
    end_utc="$(_timing_now_utc)"
    dur=$((end_sec - _TIMING_CUR_START_SEC))

    printf '%s\t%s\t%s\t%s\t%d\t%s\t%s\n' \
        "${TIMING_RUN_ID}" "${TIMING_SCRIPT}" "${_TIMING_CUR_STEP}" "${status}" \
        "${dur}" "${_TIMING_CUR_START_UTC}" "${end_utc}" >>"$TIMING_TSV"

    echo "[timing] END    ${TIMING_SCRIPT} :: ${_TIMING_CUR_STEP}  ${status}  ${dur}s  (${end_utc})" | tee -a "$TIMING_LOG"
    unset _TIMING_CUR_STEP _TIMING_CUR_START_SEC _TIMING_CUR_START_UTC
}

timing_step_skip() {
    timing_step_start "$1"
    timing_step_end skip
}

# Выполнить команду с замером: timing_step "name" cmd arg...
timing_step() {
    local name="$1"
    shift
    timing_step_start "$name"
    if "$@"; then
        timing_step_end ok
    else
        timing_step_end fail
        return 1
    fi
}

timing_finalize() {
    [[ "${TIMING_DISABLE:-0}" == "1" ]] && return 0
    [[ -n "${TIMING_LOG:-}" ]] || return 0

    local end_utc total
    end_utc="$(_timing_now_utc)"
    total=$(( $(_timing_now_sec) - ${TIMING_PIPELINE_START_SEC:-0} ))

    {
        echo "finished : ${end_utc}"
        echo "wall     : ${total}s (${TIMING_SCRIPT})"
        echo "timing   : ${TIMING_TSV}"
        echo "========================================"
    } | tee -a "$TIMING_LOG"
}
