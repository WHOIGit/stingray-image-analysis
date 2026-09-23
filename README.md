# Stingray Image Analysis

This project converts cruise video into time-resolved biological abundance.
Four independent jobs are available:

1. determine the acquisition time of every video frame;
2. detect and classify organisms with a YOLO model;
3. convert detections into abundance aligned with the sensor time series; and
4. train a YOLO model from annotated images.

The first three jobs share one cruise configuration. Run only the jobs needed
for a particular dataset, but preserve their order when running the complete
analysis:

```text
video -> frame timestamps -> detections -> abundance
```

## Requirements

Use Linux or WSL2 with Python 3.11 or newer. Run the following commands from
the directory where you want to keep the project:

```bash
git clone https://github.com/WHOIGit/stingray-image-analysis.git
cd stingray-image-analysis
python3 -m venv .venv/cvision
source .venv/cvision/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install --upgrade pyyaml "stingraytools[images] @ git+https://github.com/WHOIGit/stingraytools.git"
```

YOLO prediction and training additionally require Ultralytics:

```bash
source .venv/cvision/bin/activate
python -m pip install --upgrade ultralytics
```

On Slurm, create the same environment on a filesystem visible to the compute
nodes. The supplied jobs load `miniconda/25.9` before activating the configured
environment.

## Configure an analysis

Create a cruise configuration:

```bash
cp configs/cruise.example.conf.sh configs/my_cruise.conf.sh
```

Edit `configs/my_cruise.conf.sh` and set:

- cruise identity, date, collection, and camera stream;
- video and Stingray data directories;
- sensor dataset name; the abundance product is written back to the same
  dashboard CSV when processing completes;
- trained model weights and organism class names;
- timestamp, prediction, and abundance parameters.

The cruise date must match the date encoded in the media filenames. Relative
configuration, environment, input, workspace, and output paths are evaluated
from the current working directory. Run all commands from the project root.

Training uses a separate configuration:

```bash
cp configs/yolo_train.example.conf.sh configs/my_training.conf.sh
```

Set the training dataset, initial model, image size, batch size, epoch count,
and device selection in `TRAIN_ARGS`.

## Run locally

Activate the environment and run the required jobs from the project root:

```bash
source .venv/cvision/bin/activate
bash frame_timestamps.sh configs/my_cruise.conf.sh
bash yolo_predict.sh configs/my_cruise.conf.sh
bash image_abundance.sh configs/my_cruise.conf.sh
```

Train a model independently when new annotations are available:

```bash
bash yolo_train.sh configs/my_training.conf.sh
```

## Run with Slurm

Create the log directory before submission because Slurm opens the log files
before the job begins:

```bash
mkdir -p slogs
```

Submit the required jobs from the project root:

```bash
sbatch --mail-user=YOUR_EMAIL frame_timestamps.sbatch configs/my_cruise.conf.sh
sbatch --mail-user=YOUR_EMAIL yolo_predict.sbatch configs/my_cruise.conf.sh
sbatch --mail-user=YOUR_EMAIL image_abundance.sbatch configs/my_cruise.conf.sh
sbatch --mail-user=YOUR_EMAIL yolo_train.sbatch configs/my_training.conf.sh
```

The jobs may be submitted independently when their required inputs already
exist. For a complete analysis, wait for timestamps before prediction and wait
for prediction before abundance.

## Scientific calculations

### Frame timestamps

The timestamp job produces a video-level table and a frame-level table. Fast
mode estimates frame time from the media start time, frame index $i$, and
measured frame rate $f$:

$$
t_i = t_0 + \frac{i}{f}.
$$

Use `TIMESTAMP_MODE="fast"` for routine processing. Use
`TIMESTAMP_MODE="details"` when timestamps must be read for every individual
frame. Details mode is slower but records unreadable files and frames
explicitly.

The resulting `VIDEO_LIST_CSV` controls which videos are eligible for
prediction. `FRAME_LIST_CSV` provides the time coordinate used for abundance.

### Detection

YOLO predicts organism classes and confidence scores for each frame.
Detections are stored per video. A video is considered complete only after its
success marker is written, so interrupted runs can resume without repeating
completed videos. Outputs from incomplete videos are excluded from abundance.

`PREDICTION_FILE_LIMIT` can restrict a test run to the first specified number
of remaining videos. Leave it empty to process the full remaining dataset.

### Abundance

Detections with confidence below `SCORE_THRESH` are removed. The retained
detections are matched to frame times and grouped into intervals of width
`BIN_WIDTH`.

For class $c$ in time bin $b$, let $k_{i,c}$ be the number of detections in
frame $i$, let $n_b$ be the number of frames in the bin, and let $V_f$ be the
sample volume represented by one frame. The reported abundance is

$$
A_{b,c} = \frac{1}{V_f}\left(\frac{1}{n_b}\sum_{i=1}^{n_b} k_{i,c}\right).
$$

`VOLUME_PER_FRAME` defines $V_f$. Total abundance is the sum over all classes:

$$
A_{b,\mathrm{total}} = \sum_c A_{b,c}.
$$

When `ADD_CI="1"`, Poisson confidence intervals are calculated from the raw
detection counts. The abundance table is then aligned with `SENSOR_CSV` by
time and written to `ABUNDANCE_OUT_CSV`.

## Main outputs

- `VIDEO_LIST_CSV`: video metadata, status, timing, frame count, and frame rate.
- `FRAME_LIST_CSV`: one timestamped record per frame.
- `PREDICTION_PROJECT`: per-video YOLO detections and completion records.
- `DETECTIONS_CSV`: combined detection table used for abundance.
- `CLASS_MAP_CSV`: numerical model classes mapped to scientific class names.
- `ABUNDANCE_OUT_CSV`: sensor data with class-specific and total abundance.
