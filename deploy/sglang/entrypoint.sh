#!/usr/bin/env bash
set -Eeuo pipefail

TEXT_MODEL_PATH="${TEXT_MODEL_PATH:-cyankiwi/Qwen3.8-27B-AWQ-INT4}"
TEXT_MODEL_REVISION="${TEXT_MODEL_REVISION:-6e134bae811fb5adac50ee042ae5f029ac6779aa}"
TEXT_SERVED_MODEL_NAME="${TEXT_SERVED_MODEL_NAME:-qwen-text}"
TEXT_MEM_FRACTION="${TEXT_MEM_FRACTION:-0.50}"
TEXT_CONTEXT_LENGTH="${TEXT_CONTEXT_LENGTH:-16384}"
TEXT_TOOL_CALL_PARSER="${TEXT_TOOL_CALL_PARSER:-qwen3_coder}"
IMAGE_MODEL_PATH="${IMAGE_MODEL_PATH:-circulus/FLUX.2-klein-9B-bnb-4bit}"
IMAGE_MODEL_REVISION="${IMAGE_MODEL_REVISION:-58c2804f31af12c8888504b96250010c50b55e44}"
IMAGE_SERVED_MODEL_NAME="${IMAGE_SERVED_MODEL_NAME:-flux-klein}"

text_model_is_local=0
image_model_is_local=0
if [[ -d "$TEXT_MODEL_PATH" ]]; then
    text_model_is_local=1
fi
if [[ -d "$IMAGE_MODEL_PATH" ]]; then
    image_model_is_local=1
fi

# 텍스트·비전 모델은 transformers 규약이라 config.json 이 루트에 있고, 이미지 확산
# 모델은 diffusers 규약이라 model_index.json 이 루트에 있다. 둘을 같은 파일로 검사하면
# 정상적인 확산 모델을 불완전하다고 판정한다.
validate_local_model() {
    local model_role="$1"
    local model_path="$2"
    local marker="$3"

    if [[ -d "$model_path" && ! -f "$model_path/$marker" ]]; then
        echo "ERROR: ${model_role}_MODEL_PATH=${model_path} 는 디렉터리지만 ${marker} 이 없습니다." >&2
        echo "       S3 동기화가 끝나기 전에 파드가 뜬 것일 수 있습니다." >&2
        return 1
    fi
}

validate_local_model "TEXT" "$TEXT_MODEL_PATH" "config.json"
validate_local_model "IMAGE" "$IMAGE_MODEL_PATH" "model_index.json"

text_command=(
    sglang-python -m sglang.launch_server
    --model-path "$TEXT_MODEL_PATH"
)
if (( ! text_model_is_local )); then
    text_command+=(--revision "$TEXT_MODEL_REVISION")
fi
text_command+=(
    --served-model-name "$TEXT_SERVED_MODEL_NAME"
    --host 127.0.0.1
    --port 30000
    --mem-fraction-static "$TEXT_MEM_FRACTION"
    --context-length "$TEXT_CONTEXT_LENGTH"
    --trust-remote-code
    # The agent loop reads OpenAI-style tool_calls. Qwen3.5/3.8 chat templates emit the
    # XML form (<tool_call><function=…><parameter=…>), which SGLang parses as qwen3_coder.
    --tool-call-parser "$TEXT_TOOL_CALL_PARSER"
    # Stage 2026-10-01: text + image servers left 3.5 MiB of the L40S free, so
    # the cutout model and image preprocessing hit CUDA OOM. The prefill graphs
    # took 1.87 GB (and 4.6 min to capture) for one request at a time, and the
    # torchvision image processor allocates on the GPU; keep both off the GPU.
    --disable-prefill-cuda-graph
    --image-processor-backend pil
)
if [[ -n "${TEXT_MAX_RUNNING_REQUESTS:-}" ]]; then
    text_command+=(--max-running-requests "$TEXT_MAX_RUNNING_REQUESTS")
fi

image_command=(
    sglang serve
    --model-path "$IMAGE_MODEL_PATH"
)
if (( ! image_model_is_local )); then
    image_command+=(--revision "$IMAGE_MODEL_REVISION")
fi
image_command+=(
    --served-model-name "$IMAGE_SERVED_MODEL_NAME"
    --host 127.0.0.1
    --port 30001
    --num-gpus 1
    --dit-cpu-offload false
    --text-encoder-cpu-offload false
    # Stage 2026-10-01: the defaults save uploads to inputs/uploads and results
    # to outputs/ under the working directory, which is read-only in the pod
    # (OSError: [Errno 30] Read-only file system: 'inputs'), so every edit
    # returned 500. Empty values use a per-request temp dir in $TMPDIR (/tmp).
    --output-path ""
    --input-save-path ""
)

print_command() {
    local label="$1"
    local offline="$2"
    shift 2

    printf '%s:' "$label"
    if [[ "$offline" == "1" ]]; then
        printf ' HF_HUB_OFFLINE=1'
    fi
    printf ' %q' "$@"
    printf '\n'
}

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    print_command "TEXT" "$text_model_is_local" "${text_command[@]}"
    print_command "IMAGE" "$image_model_is_local" "${image_command[@]}"
    exit 0
fi

# A PVC mounted over this path hides the image-layer directories. The service
# code creates the SQLite parent and asset-store directories itself, while the
# entrypoint creates model/cache directories before SGLang starts on a blank
# PVC. This assumes the PVC is writable by UID/GID 10001 (for example via
# Kubernetes fsGroup/securityContext).
mkdir -p \
    "${ASSET_STORE_DIR:-/var/lib/detail-page-ai/assets}" \
    "${U2NET_HOME:-/var/lib/detail-page-ai/models/u2net}" \
    "${HF_HOME:-/var/lib/detail-page-ai/models/huggingface}" \
    "${HF_HUB_CACHE:-/var/lib/detail-page-ai/models/huggingface/hub}" \
    "${HF_DATASETS_CACHE:-/var/lib/detail-page-ai/models/huggingface/datasets}" \
    "${TRANSFORMERS_CACHE:-/var/lib/detail-page-ai/models/huggingface/hub}" \
    "${XDG_CACHE_HOME:-/var/lib/detail-page-ai/cache}" \
    "${TORCH_HOME:-/var/lib/detail-page-ai/models/torch}" \
    "${SGLANG_CACHE_DIR:-/var/lib/detail-page-ai/cache/sglang}" \
    "${FLASHINFER_CACHE_DIR:-/var/lib/detail-page-ai/cache/flashinfer}" \
    "${TRITON_CACHE_DIR:-/var/lib/detail-page-ai/cache/triton}" \
    "${TORCHINDUCTOR_CACHE_DIR:-/var/lib/detail-page-ai/cache/torchinductor}" \
    "${CUDA_CACHE_PATH:-/var/lib/detail-page-ai/cache/cuda}" \
    "$(dirname "${SQLITE_PATH:-/var/lib/detail-page-ai/state.sqlite3}")"

# kill -0 also succeeds for a zombie on Linux. Inspecting /proc lets the
# watcher notice a crashed server before it can leave the API running alone.
process_is_running() {
    local pid="$1"
    local state

    kill -0 "$pid" 2>/dev/null || return 1
    if [[ -r "/proc/$pid/stat" ]]; then
        state="$(cut -d ' ' -f3 "/proc/$pid/stat" 2>/dev/null || true)"
        [[ "$state" != "Z" ]] || return 1
    fi
}

log_vram() {
    local label="$1"
    local usage

    command -v nvidia-smi >/dev/null 2>&1 || return 0
    usage="$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null || true)"
    [[ -n "$usage" ]] || return 0
    usage="${usage//$'\n'/; }"
    printf 'VRAM %s: %s\n' "$label" "$usage" >&2
}

wait_for_model_ready() {
    local label="$1"
    local port="$2"
    local target_pid="$3"
    local timeout_seconds="$4"
    local deadline=$((SECONDS + timeout_seconds))
    local ready_check

    ready_check='from urllib.request import urlopen; import sys; response=urlopen("http://127.0.0.1:" + sys.argv[1] + "/v1/models", timeout=2); raise SystemExit(0 if response.status == 200 else 1)'
    while (( SECONDS < deadline )); do
        if ! process_is_running "$main_pid"; then
            echo "FastAPI service exited while waiting for SGLang ${label} server" >&2
            return 1
        fi
        if ! process_is_running "$text_pid"; then
            echo "SGLang text server exited before readiness" >&2
            return 1
        fi
        if (( target_pid != text_pid )) && ! process_is_running "$target_pid"; then
            echo "SGLang ${label} server exited before readiness" >&2
            return 1
        fi
        if sglang-python -c "$ready_check" "$port" >/dev/null 2>&1; then
            log_vram "${label}-ready"
            return 0
        fi
        sleep 1
    done

    echo "Timed out waiting ${timeout_seconds}s for SGLang ${label} server readiness" >&2
    return 1
}

text_pid=0
image_pid=0
main_pid=$$
TEXT_READY_TIMEOUT="${TEXT_READY_TIMEOUT:-1800}"
if ! [[ "$TEXT_READY_TIMEOUT" =~ ^[0-9]+$ ]] || (( TEXT_READY_TIMEOUT < 1 )); then
    echo "TEXT_READY_TIMEOUT must be a positive integer" >&2
    exit 1
fi

echo "Starting SGLang text server on 127.0.0.1:30000" >&2
if (( text_model_is_local )); then
    HF_HUB_OFFLINE=1 "${text_command[@]}" &
else
    "${text_command[@]}" &
fi
text_pid=$!

stop_servers_and_fail() {
    local reason="$1"

    echo "$reason" >&2
    kill -TERM "$text_pid" 2>/dev/null || true
    if (( image_pid > 0 )); then
        kill -TERM "$image_pid" 2>/dev/null || true
    fi
    stop_api
}

stop_api() {
    # serve-ai runs as the container's PID 1 (exec below), and the kernel drops
    # SIGKILL sent to PID 1 from inside the container: on Stage 2026-10-01 the
    # text server died and the pod stayed Running/NotReady instead of
    # restarting. uvicorn exits on TERM and force-exits on a second TERM.
    process_is_running "$main_pid" || return 0
    : > "$MODEL_FAILURE_MARKER"  # serve-ai then exits 1 instead of 0
    kill -TERM "$main_pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
        process_is_running "$main_pid" || return 0
        sleep 1
    done
    kill -TERM "$main_pid" 2>/dev/null || true
}

# Keep the API as PID 1/foreground while this background supervisor waits for
# the text model, starts the image model, and then watches both SGLang children.
# Docker stops the whole container when PID 1 exits; explicit child termination
# also makes local script tests clean.
watch_processes() {
    if ! wait_for_model_ready "text" 30000 "$text_pid" "$TEXT_READY_TIMEOUT"; then
        stop_servers_and_fail "SGLang text server did not become ready; stopping container"
        return
    fi

    echo "Starting SGLang image server on 127.0.0.1:30001" >&2
    if (( image_model_is_local )); then
        HF_HUB_OFFLINE=1 "${image_command[@]}" &
    else
        "${image_command[@]}" &
    fi
    image_pid=$!

    if ! wait_for_model_ready "image" 30001 "$image_pid" "$TEXT_READY_TIMEOUT"; then
        stop_servers_and_fail "SGLang image server did not become ready; stopping container"
        return
    fi

    while process_is_running "$text_pid" \
        && process_is_running "$image_pid" \
        && process_is_running "$main_pid"; do
        sleep 1
    done

    local server_failure=0
    if ! process_is_running "$text_pid"; then
        echo "SGLang text server exited; stopping container" >&2
        server_failure=1
    elif ! process_is_running "$image_pid"; then
        echo "SGLang image server exited; stopping container" >&2
        server_failure=1
    else
        echo "FastAPI service exited; stopping SGLang children" >&2
    fi

    kill -TERM "$text_pid" "$image_pid" 2>/dev/null || true
    if (( server_failure )); then
        stop_api
    fi
}

# /tmp is an emptyDir that survives container restarts; clear the last failure.
MODEL_FAILURE_MARKER="${MODEL_FAILURE_MARKER:-/tmp/model-server-failed}"
export MODEL_FAILURE_MARKER
rm -f "$MODEL_FAILURE_MARKER"

watch_processes &

echo "Starting FastAPI service on 0.0.0.0:8000" >&2
exec serve-ai
