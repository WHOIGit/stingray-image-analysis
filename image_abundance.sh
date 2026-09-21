#!/usr/bin/env bash
###############################################################################
# Run post-inference image abundance processing on a local machine or HPC node.
###############################################################################

set -euxo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

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

require_value() {
    local name="$1"
    local value="$2"
    if [[ -z "$value" || "$value" == *CHANGEME* || "$value" == *DATE_CRUISE* || "$value" == *SENSOR_DATASET* ]]; then
        echo "[ERROR] Configure $name before running this workflow." >&2
        exit 2
    fi
}

require_writable_location() {
    local name="$1"
    local value="$2"
    require_value "$name" "$value"

    local target_dir
    target_dir="$(dirname "$value")"
    local probe_dir="$target_dir"
    while [[ ! -d "$probe_dir" ]]; do
        local parent_dir
        parent_dir="$(dirname "$probe_dir")"
        if [[ "$parent_dir" == "$probe_dir" ]]; then
            echo "[ERROR] Cannot find an existing parent directory for $name: $value" >&2
            exit 2
        fi
        probe_dir="$parent_dir"
    done

    local probe_file
    if ! probe_file="$(mktemp "$probe_dir/.stingray-write-test.XXXXXX")"; then
        echo "[ERROR] Output location for $name is not writable: $value" >&2
        echo "[ERROR] Update the configuration before rerunning this workflow." >&2
        exit 2
    fi
    rm -f "$probe_file"
}

require_file() {
    local name="$1"
    local value="$2"
    require_value "$name" "$value"
    if [[ ! -f "$value" ]]; then
        echo "[ERROR] $name does not exist: $value" >&2
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

require_switch() {
    local name="$1"
    local value="$2"
    if [[ "$value" != "0" && "$value" != "1" ]]; then
        echo "[ERROR] $name must be 0 or 1; received: $value" >&2
        exit 2
    fi
}

require_switch "MERGE_LABELS" "$MERGE_LABELS"
require_switch "ADD_CI" "$ADD_CI"

require_dir "STINGRAY_DATA_ROOT" "$STINGRAY_DATA_ROOT"
require_file "CLASS_YAML" "$CLASS_YAML"
require_writable_location "DETECTIONS_CSV" "$DETECTIONS_CSV"
require_writable_location "CLASS_MAP_CSV" "$CLASS_MAP_CSV"
require_writable_location "ABUNDANCE_OUT_CSV" "$ABUNDANCE_OUT_CSV"
require_file "SENSOR_CSV" "$SENSOR_CSV"
require_file "FRAME_LIST_CSV" "$FRAME_LIST_CSV"
require_value "DETECTIONS_CSV" "$DETECTIONS_CSV"
require_value "CLASS_MAP_CSV" "$CLASS_MAP_CSV"
require_value "ABUNDANCE_OUT_CSV" "$ABUNDANCE_OUT_CSV"

if [[ "$MERGE_LABELS" == "1" ]]; then
    if [[ ${#LABEL_DIRS[@]} -eq 0 ]]; then
        echo "[ERROR] Add at least one label directory to LABEL_DIRS." >&2
        exit 2
    fi

    for label_dir in "${LABEL_DIRS[@]}"; do
        require_dir "LABEL_DIR" "$label_dir"
    done
else
    require_file "DETECTIONS_CSV" "$DETECTIONS_CSV"
fi

CPU_COUNT="${STINGRAY_CPU_COUNT:-$(nproc)}"
if [[ ! "$CPU_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] Available CPU count must be a positive integer: $CPU_COUNT" >&2
    exit 2
fi

WORKER_LIMIT=$((CPU_COUNT > 1 ? CPU_COUNT - 1 : 1))
JOBS="${JOBS:-$WORKER_LIMIT}"
if [[ ! "$JOBS" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] JOBS must be empty or a positive integer." >&2
    exit 2
fi
if ((JOBS > WORKER_LIMIT)); then
    echo "[INFO] Limiting label workers from $JOBS to $WORKER_LIMIT for $CPU_COUNT available CPUs."
    JOBS="$WORKER_LIMIT"
fi

echo "[INFO] Configuration: $CONFIG_PATH"
echo "[INFO] Environment: $CVISION_ENV"
source "$CVISION_ENV/bin/activate"

mkdir -p "$(dirname "$DETECTIONS_CSV")" "$(dirname "$CLASS_MAP_CSV")" "$(dirname "$ABUNDANCE_OUT_CSV")"

if [[ "$MERGE_LABELS" == "1" ]]; then
    echo "[INFO] Merging detection labels."
    bash "$SCRIPT_DIR/merge_detection_labels.sh" \
        --output-csv "$DETECTIONS_CSV" \
        --class-map-csv "$CLASS_MAP_CSV" \
        --class-yaml "$CLASS_YAML" \
        --python-bin "$(command -v python)" \
        --jobs "$JOBS" \
        "${LABEL_DIRS[@]}"
elif [[ ! -f "$CLASS_MAP_CSV" ]]; then
    echo "[INFO] Class map CSV not found; creating it from CLASS_YAML."
    python "$SCRIPT_DIR/build_class_map.py" \
        --class-yaml "$CLASS_YAML" \
        --class-map-csv "$CLASS_MAP_CSV"
else
    echo "[INFO] Reusing existing class map CSV: $CLASS_MAP_CSV"
fi

echo "[INFO] Computing abundance."
ABUNDANCE_ARGS=(
    --detections-csv "$DETECTIONS_CSV"
    --class-map-csv "$CLASS_MAP_CSV"
    --sensor-csv "$SENSOR_CSV"
    --media-csv "$FRAME_LIST_CSV"
    --out-csv "$ABUNDANCE_OUT_CSV"
    --score-thresh "$SCORE_THRESH"
    --bin-width "$BIN_WIDTH"
    --volume-per-frame "$VOLUME_PER_FRAME"
)

if [[ "$ADD_CI" == "1" ]]; then
    ABUNDANCE_ARGS+=(--add-ci)
fi

stingray images abundance "${ABUNDANCE_ARGS[@]}" --no-file-log

echo "[DONE] Image abundance output: $ABUNDANCE_OUT_CSV"
