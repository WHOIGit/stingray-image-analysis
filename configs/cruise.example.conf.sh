###############################################################################
# Shared cruise configuration and artifact paths
###############################################################################

CRUISE="CHANGEME_CRUISE"
# Match the date parsed from the cruise media filenames (YYYYMMDD).
CRUISE_DATE="CHANGEME_YYYYMMDD"
CRUISE_COLLECTION="NESLTER"
CAMERA_STREAM="CHANGEME_CAMERA_STREAM"
VIDEO_SUFFIX=".avi"
RUN_NAME="${CRUISE_DATE}_${CRUISE}"

# The timestamp mode is part of both shared list names. Keeping it here ensures
# timestamp, inference, and abundance resolve the same artifacts.
TIMESTAMP_MODE="fast"
TIMESTAMP_FORMAT="%Y%m%dT%H%M%S.%f"

CVISION_ENV=".venv/cvision"
MODEL_ENV="$CVISION_ENV"

VIDEO_DATA_ROOT="CHANGEME_VIDEO_DATA_ROOT"
STINGRAY_DATA_ROOT="CHANGEME_STINGRAY_DATA_ROOT"
SENSOR_DATASET="CHANGEME_SENSOR_DATASET"
ABUNDANCE_DATASET="shadowgraph"

VIDEO_INPUT_DIR="${VIDEO_DATA_ROOT}/${CRUISE_COLLECTION}_${CRUISE}/${CAMERA_STREAM}"
MEDIA_LIST_DIR="${STINGRAY_DATA_ROOT}/media_list/${CAMERA_STREAM}"
ABUNDANCE_WORKSPACE_DIR="workspace/abundance/${CAMERA_STREAM}"
PREDICTION_PROJECT="workspace/prediction/${CAMERA_STREAM}"

TIMESTAMP_LIST_SUFFIX=""
if [[ "$TIMESTAMP_MODE" == "details" ]]; then
    TIMESTAMP_LIST_SUFFIX="_detailed"
fi
VIDEO_LIST_CSV="${MEDIA_LIST_DIR}/${RUN_NAME}_video_list${TIMESTAMP_LIST_SUFFIX}.csv"
FRAME_LIST_CSV="${MEDIA_LIST_DIR}/${RUN_NAME}_frame_list${TIMESTAMP_LIST_SUFFIX}.csv"
DETECTIONS_CSV="${ABUNDANCE_WORKSPACE_DIR}/${RUN_NAME}_detection_labels.csv"
CLASS_MAP_CSV="${ABUNDANCE_WORKSPACE_DIR}/${RUN_NAME}_class_map.csv"
SENSOR_CSV="${STINGRAY_DATA_ROOT}/dashboard_data/data/${SENSOR_DATASET}/${RUN_NAME}.csv"
ABUNDANCE_OUT_CSV="${STINGRAY_DATA_ROOT}/dashboard_data/data/${ABUNDANCE_DATASET}/${RUN_NAME}.csv"

###############################################################################
# Frame timestamp configuration
###############################################################################

TIMESTAMP_FILE_LIMIT=""
TIMESTAMP_MAX_WORKERS=""
TIMESTAMP_SUFFIXES=("$VIDEO_SUFFIX")

###############################################################################
# Model prediction configuration
###############################################################################

MODEL_WEIGHTS_PATH="CHANGEME_MODEL_WEIGHTS_PATH"
PREDICTION_VIDEO_STATUSES=("valid")
PREDICTION_FILE_LIMIT=""
LOCAL_PREDICTION_DEVICES="0"

PREDICTION_ARGS=(
    save=False
    save_txt=True
    save_conf=True
    agnostic_nms=True
    iou=0.5
    conf=0.1
    half=False
    imgsz=1280
    batch=64
    max_det=300
    vid_stride=1
    stream_buffer=False
    visualize=False
    augment=False
    retina_masks=False
    verbose=True
    show=False
    save_crop=False
    save_frames=False
    show_labels=True
    show_conf=True
    show_boxes=True
)

###############################################################################
# Image abundance configuration
###############################################################################

CLASS_YAML="CHANGEME_CLASS_NAMES_YAML"
MERGE_LABELS="1"
LABEL_DIRS=("$PREDICTION_PROJECT")

SCORE_THRESH="0.7"
BIN_WIDTH="5"
VOLUME_PER_FRAME="0.00225"
ADD_CI="0"
