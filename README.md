# Stingray Image Analysis

This repository runs video processing for Stingray cruises:

1. build video and frame timestamps;
2. run YOLO detection and classification;
3. convert detections to time-binned abundance; and
4. train YOLO models from annotated images.

The workflow uses the reusable commands in
[stingraytools](https://github.com/WHOIGit/stingraytools).

## Data contract

Each cruise has one dashboard CSV:

```text
dashboard_data/data/<sensor_dataset>/<cruise>.csv
```

The abundance step reads the sensor-derived CSV and writes the merged
sensor+abundance product back to that same path. Re-running abundance replaces
the previous abundance columns instead of creating a second product file.

Raw sensor files remain the source from which StingrayTools can regenerate the
sensor-derived CSV.

## Requirements

Use Linux or WSL2 with Python 3.11 or newer. Install the workflow and its
dependencies in a project environment:

```bash
git clone https://github.com/WHOIGit/stingray-image-analysis.git
cd stingray-image-analysis
python3 -m venv .venv/cvision
source .venv/cvision/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install pyyaml pyarrow \
  "stingraytools[images] @ git+https://github.com/WHOIGit/stingraytools.git"
```

YOLO prediction and training also require:

```bash
python -m pip install ultralytics
```

## Configuration

Create a cruise configuration:

```bash
cp configs/cruise.example.conf.sh configs/my_cruise.conf.sh
```

Set the cruise identity, input paths, sensor dataset, model weights, class
names, and timestamp, prediction, and abundance parameters. `SENSOR_CSV` and
`ABUNDANCE_OUT_CSV` should resolve to the same dashboard CSV.

Create a training configuration when needed:

```bash
cp configs/yolo_train.example.conf.sh configs/my_training.conf.sh
```

## Run the workflow

Run the required stages in this order:

```bash
source .venv/cvision/bin/activate
bash frame_timestamps.sh configs/my_cruise.conf.sh
bash yolo_predict.sh configs/my_cruise.conf.sh
bash image_abundance.sh configs/my_cruise.conf.sh
```

The stages can be run independently when their inputs already exist. Train a
model separately:

```bash
bash yolo_train.sh configs/my_training.conf.sh
```

The Slurm wrappers use the same configuration files:

```bash
sbatch frame_timestamps.sbatch configs/my_cruise.conf.sh
sbatch yolo_predict.sbatch configs/my_cruise.conf.sh
sbatch image_abundance.sbatch configs/my_cruise.conf.sh
sbatch yolo_train.sbatch configs/my_training.conf.sh
```

## Outputs

- `VIDEO_LIST_CSV`: video timing and processing status.
- `FRAME_LIST_CSV`: timestamped frame records.
- `PREDICTION_PROJECT`: per-video detections and completion markers.
- `DETECTIONS_PARQUET`: combined detection table, with `<name>_issues.csv`
  listing skipped or suspicious label lines.
- `ABUNDANCE_OUT_CSV`: the updated dashboard CSV.

## Detection table

`merge_detections_parquet.py` turns Ultralytics video labels into one Parquet
table that other tools read without knowing the model:

| Column | Meaning |
|---|---|
| `media` | video file stem |
| `frame` | 0-based source-video frame (Ultralytics label names start at 1) |
| `class`, `class_id` | class name and model class ID |
| `x`, `y`, `width`, `height` | COCO pixel box: top-left corner, width, height |
| `score` | model confidence; empty for manual boxes |

The file metadata holds the full class list and the image size
(`IMAGE_WIDTH`, `IMAGE_HEIGHT`). Exact repeated lines within a label file
(Ultralytics appends when a video is predicted twice into the same folder) are
removed, and the merge log reports how many. Each video is merged into a part
under `<output>.parquet.parts/` first, so an interrupted merge continues where
it stopped; set `MERGE_REBUILD="1"` to start over, or `MERGE_KEEP_PARTS="1"` to
keep the parts so a later merge only parses new videos.

## Abundance calculation

Detections below `SCORE_THRESH` are discarded, matched to frame times, and
grouped into `BIN_WIDTH` intervals. For class \(c\) in bin \(b\):

$$
A_{b,c} = \frac{1}{V_f}\left(\frac{1}{n_b}\sum_{i=1}^{n_b} k_{i,c}\right),
$$

where `VOLUME_PER_FRAME` defines \(V_f\). Total abundance is the sum across
classes. When `ADD_CI="1"`, Poisson confidence intervals are calculated from
raw detection counts.

## License

Stingray Image Analysis is distributed under the GNU Affero General Public
License v3.0 (AGPL-3.0), because its YOLO prediction and training steps use
Ultralytics, which is licensed under the AGPL-3.0. See [LICENSE](LICENSE).
