#!/usr/bin/env bash
###############################################################################
# Build the media/frame timestamp CSV needed by abundance on a local machine or
# local HPC node.
###############################################################################

set -euxo pipefail

CONFIG_PATH="${1:-}"
if [[ -z "$CONFIG_PATH" ]]; then
    echo "[ERROR] Provide a cruise configuration file." >&2
    exit 2
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "[ERROR] Configuration does not exist: $CONFIG_PATH" >&2
    exit 2
fi
source "$CONFIG_PATH"

# Keep optional settings safe under `set -u`; older callers may not define them.
ENABLE_TIMESTAMPS="${ENABLE_TIMESTAMPS:-1}"
TIMESTAMP_FILE_LIMIT="${TIMESTAMP_FILE_LIMIT:-}"

require_value() {
    local name="$1"
    local value="$2"
    if [[ -z "$value" || "$value" == *CHANGEME* || "$value" == *DATE_CRUISE* || "$value" == *SENSOR_DATASET* ]]; then
        echo "[ERROR] Configure $name before running this workflow." >&2
        exit 2
    fi
}

require_dir() {
    local name="$1"
    local value="$2"
    require_value "$name" "$value"
    if [[ ! -d "$value" ]]; then
        echo "[ERROR] $name does not exist: $value" >&2
        exit 2
    fi
}

require_file() {
    local name="$1"
    local value="$2"
    require_value "$name" "$value"
    if [[ ! -f "$value" ]]; then
        echo "[ERROR] Expected output was not created: $name=$value" >&2
        exit 1
    fi
}

if [[ "$TIMESTAMP_MODE" != "fast" && "$TIMESTAMP_MODE" != "details" ]]; then
    echo "[ERROR] TIMESTAMP_MODE must be 'fast' or 'details'." >&2
    exit 2
fi

require_dir "STINGRAY_DATA_ROOT" "$STINGRAY_DATA_ROOT"
require_dir "VIDEO_INPUT_DIR" "$VIDEO_INPUT_DIR"
require_value "MEDIA_LIST_DIR" "$MEDIA_LIST_DIR"
require_value "CRUISE" "$CRUISE"

CPU_COUNT="${STINGRAY_CPU_COUNT:-$(nproc)}"
if [[ ! "$CPU_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] Available CPU count must be a positive integer: $CPU_COUNT" >&2
    exit 2
fi

WORKER_LIMIT=$((CPU_COUNT > 1 ? CPU_COUNT - 1 : 1))
MAX_WORKERS="${TIMESTAMP_MAX_WORKERS:-$WORKER_LIMIT}"
if [[ ! "$MAX_WORKERS" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] TIMESTAMP_MAX_WORKERS must be empty or a positive integer." >&2
    exit 2
fi
if ((MAX_WORKERS > WORKER_LIMIT)); then
    echo "[INFO] Limiting timestamp workers from $MAX_WORKERS to $WORKER_LIMIT for $CPU_COUNT available CPUs."
    MAX_WORKERS="$WORKER_LIMIT"
fi

echo "[INFO] Configuration: $CONFIG_PATH"
echo "[INFO] Environment: $CVISION_ENV"
source "$CVISION_ENV/bin/activate"

FRAME_ARGS=(
    --work-dir "$STINGRAY_DATA_ROOT"
    --cruise "$CRUISE"
    --media-dir "$VIDEO_INPUT_DIR"
    --out-dir "$MEDIA_LIST_DIR"
    --max-workers "$MAX_WORKERS"
    --suffix "${TIMESTAMP_SUFFIXES[@]}"
    --no-file-log
)

if [[ -n "$TIMESTAMP_FILE_LIMIT" ]]; then
    FRAME_ARGS+=(--file-limit "$TIMESTAMP_FILE_LIMIT")
fi

if [[ "$TIMESTAMP_MODE" == "details" ]]; then
    FRAME_ARGS+=(--details)
fi

stingray images frame-timestamp "${FRAME_ARGS[@]}"

require_file "VIDEO_LIST_CSV" "$VIDEO_LIST_CSV"
require_file "FRAME_LIST_CSV" "$FRAME_LIST_CSV"
echo "[DONE] Video list: $VIDEO_LIST_CSV"
echo "[DONE] Frame list: $FRAME_LIST_CSV"
