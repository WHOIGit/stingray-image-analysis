#!/usr/bin/env bash
###############################################################################
# Merge space-separated label files into one canonical detection table.
#
# Each input directory is scanned for per-video _SUCCESS markers or a Prefect
# .completed_files.txt manifest. Only labels registered by one of those
# completion mechanisms are converted. Failed or incomplete outputs are
# ignored.
###############################################################################

set -euxo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

usage() {
    cat <<'EOF'
Usage:
  merge_detection_labels.sh --output-csv PATH --class-map-csv PATH --class-yaml PATH [options] LABEL_DIR [LABEL_DIR ...]

Required:
  --output-csv PATH       Output detection table.
  --class-map-csv PATH    Output class map table.
  --class-yaml PATH       YAML file containing a names section.

Options:
  --jobs N                Number of parallel workers. Defaults to CPU count - 1.
  --python-bin PATH       Python executable used by the class-map helper.
                          Default: python3.
  --temp-dir DIR          Parent directory for temporary files.
                          Defaults to SLURM_TMPDIR, then TMPDIR, then /tmp.
  -h, --help              Show this help message.

Example:
  merge_detection_labels.sh \
    --output-csv /path/to/DATE_CRUISE_detection_labels.csv \
    --class-map-csv /path/to/DATE_CRUISE_class_map.csv \
    --class-yaml /path/to/classes.yaml \
    --jobs 32 \
    /path/to/ml-run/worker0/labels \
    /path/to/ml-run/worker1/labels \
    /path/to/ml-run/worker2/labels
EOF
}

OUTPUT_CSV=""
CLASS_MAP_CSV=""
CLASS_YAML=""
PYTHON_BIN="${PYTHON_BIN:-python3}"
JOBS=""
TEMP_PARENT="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}"
INPUT_DIRS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-csv)
            if [[ $# -lt 2 ]]; then
                echo "[ERROR] --output-csv requires a path." >&2
                exit 2
            fi
            OUTPUT_CSV="$2"
            shift 2
            ;;
        --class-map-csv)
            if [[ $# -lt 2 ]]; then
                echo "[ERROR] --class-map-csv requires a path." >&2
                exit 2
            fi
            CLASS_MAP_CSV="$2"
            shift 2
            ;;
        --class-yaml)
            if [[ $# -lt 2 ]]; then
                echo "[ERROR] --class-yaml requires a path." >&2
                exit 2
            fi
            CLASS_YAML="$2"
            shift 2
            ;;
        --python-bin)
            if [[ $# -lt 2 ]]; then
                echo "[ERROR] --python-bin requires a path." >&2
                exit 2
            fi
            PYTHON_BIN="$2"
            shift 2
            ;;
        --jobs)
            if [[ $# -lt 2 ]]; then
                echo "[ERROR] --jobs requires a positive integer." >&2
                exit 2
            fi
            JOBS="$2"
            shift 2
            ;;
        --temp-dir)
            if [[ $# -lt 2 ]]; then
                echo "[ERROR] --temp-dir requires a directory." >&2
                exit 2
            fi
            TEMP_PARENT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            while [[ $# -gt 0 ]]; do
                INPUT_DIRS+=("$1")
                shift
            done
            ;;
        -*)
            echo "[ERROR] Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            INPUT_DIRS+=("$1")
            shift
            ;;
    esac
done

if [[ -z "$OUTPUT_CSV" ]]; then
    echo "[ERROR] Missing required --output-csv PATH." >&2
    usage >&2
    exit 2
fi

if [[ -z "$CLASS_MAP_CSV" ]]; then
    echo "[ERROR] Missing required --class-map-csv PATH." >&2
    usage >&2
    exit 2
fi

if [[ -z "$CLASS_YAML" ]]; then
    echo "[ERROR] Missing required --class-yaml PATH." >&2
    usage >&2
    exit 2
fi

if [[ ! -f "$CLASS_YAML" ]]; then
    echo "[ERROR] Class YAML does not exist: $CLASS_YAML" >&2
    exit 2
fi

if [[ ${#INPUT_DIRS[@]} -eq 0 ]]; then
    echo "[ERROR] Provide at least one label directory." >&2
    usage >&2
    exit 2
fi

if [[ -n "$JOBS" && ! "$JOBS" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] --jobs must be a positive integer." >&2
    exit 2
fi

CLASS_MAP_DIR=$(dirname "$CLASS_MAP_CSV")
OUTPUT_DIR=$(dirname "$OUTPUT_CSV")
mkdir -p "$OUTPUT_DIR" "$CLASS_MAP_DIR"

if [[ ! -d "$TEMP_PARENT" ]]; then
    echo "[ERROR] Temporary parent directory does not exist: $TEMP_PARENT" >&2
    exit 2
fi

TEMP_DIR=$(mktemp -d "${TEMP_PARENT%/}/merge_detection_labels.XXXXXX")
cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

NUM_CORES=$(nproc)
if [[ -z "$JOBS" ]]; then
    JOBS=$((NUM_CORES > 1 ? NUM_CORES - 1 : 1))
fi

echo "[INFO] Detected $NUM_CORES CPU cores; using $JOBS parallel workers."
echo "[INFO] Temporary workspace: $TEMP_DIR"
echo "[INFO] Class map CSV: $CLASS_MAP_CSV"
echo "[INFO] Class YAML: $CLASS_YAML"
echo "[INFO] Output CSV: $OUTPUT_CSV"

# Build a reproducible, sorted list of labels from completed video directories.
FILELIST="$TEMP_DIR/all_files.txt"
CLASS_MAP="$TEMP_DIR/class_map.tsv"
> "$FILELIST"

"$PYTHON_BIN" "$SCRIPT_DIR/build_class_map.py" \
    --class-yaml "$CLASS_YAML" \
    --class-map-csv "$CLASS_MAP_CSV" \
    --class-map-tsv "$CLASS_MAP"

DIR_INDEX=0
for DIR in "${INPUT_DIRS[@]}"; do
    if [[ -d "$DIR" ]]; then
        echo "[INFO] Scanning directory: $DIR" >&2

        # Native Slurm/local outputs keep one success marker beside each
        # completed video's labels.
        while IFS= read -r -d '' marker; do
            LABELS_DIR="$(dirname "$marker")/labels"
            if [[ -d "$LABELS_DIR" ]]; then
                find "$LABELS_DIR" -maxdepth 1 -type f -name "*.txt"
            fi
        done < <(find "$DIR" -type f -name "_SUCCESS" -print0)

        # The existing Prefect runner stores completed source paths centrally
        # and writes labels below gpuN/labels. Match label basenames against
        # that manifest so partial failed output remains excluded.
        PREFECT_MANIFEST="$DIR/.completed_files.txt"
        if [[ -f "$PREFECT_MANIFEST" ]]; then
            COMPLETED_MEDIA="$TEMP_DIR/prefect_completed_${DIR_INDEX}.txt"
            while IFS= read -r source_path; do
                source_name="${source_path##*/}"
                printf '%s\n' "${source_name%.*}"
            done < "$PREFECT_MANIFEST" | sort -u > "$COMPLETED_MEDIA"

            find "$DIR" -type f -path "*/gpu*/labels/*.txt" | awk \
                -v completed_media="$COMPLETED_MEDIA" '
                BEGIN {
                    while ((getline media < completed_media) > 0) {
                        completed[media] = 1
                    }
                    close(completed_media)
                }
                {
                    label = $0
                    filename = label
                    sub(/^.*\//, "", filename)
                    sub(/\.txt$/, "", filename)
                    media = filename
                    sub(/_[^_]*$/, "", media)
                    if (media in completed) {
                        print label
                    }
                }
            '
        fi
    else
        echo "[WARN] Directory not found; skipping: $DIR" >&2
    fi
    DIR_INDEX=$((DIR_INDEX + 1))
done | sort > "$FILELIST"

TOTAL_FILES=$(wc -l < "$FILELIST")
echo "[INFO] Total label files discovered: $TOTAL_FILES"

if [[ "$TOTAL_FILES" -eq 0 ]]; then
    echo "[ERROR] No label files found for registered completed videos." >&2
    exit 1
fi

# Write the header before parallel workers generate body rows.
echo "media,frame,class_id,x_center,y_center,width,height,confidence" > "$OUTPUT_CSV"

# Split the file list into static batches and process batches in parallel.
BATCH_SIZE=$(( (TOTAL_FILES + JOBS - 1) / JOBS ))
echo "[INFO] Batch size: $BATCH_SIZE files per worker."

PIDS=()
for ((i = 0; i < JOBS; i++)); do
    START=$((i * BATCH_SIZE + 1))
    END=$((START + BATCH_SIZE - 1))
    TMP_OUT="$TEMP_DIR/job_$i.txt"

    (
        sed -n "${START},${END}p" "$FILELIST" | while IFS= read -r file; do
            fname="${file##*/}"
            awk -v fname="$fname" -v class_map="$CLASS_MAP" '
                BEGIN {
                    FS = "[[:space:]]+"
                    while ((getline map_line < class_map) > 0) {
                        split(map_line, map_fields, "\t")
                        class_name[map_fields[1]] = map_fields[2]
                    }
                    close(class_map)

                    media = fname
                    sub(/\.txt$/, "", media)
                    frame = media
                    sub(/^.*_/, "", frame)
                    sub(/_[^_]*$/, "", media)
                }
                NF > 0 {
                    if (!($1 in class_name)) {
                        printf("[ERROR] Class ID missing from class YAML: %s in %s\n", $1, fname) > "/dev/stderr"
                        exit 3
                    }
                    confidence = (NF >= 6) ? $6 : 1
                    print media "," frame "," $1 "," $2 "," $3 "," $4 "," $5 "," confidence
                }
            ' class_map="$CLASS_MAP" "$file"
        done
    ) > "$TMP_OUT" &
    PIDS+=("$!")
done

for pid in "${PIDS[@]}"; do
    wait "$pid"
done

cat "$TEMP_DIR"/job_*.txt >> "$OUTPUT_CSV"

echo "[SUCCESS] Output CSV created: $OUTPUT_CSV"
echo "[DONE]"
