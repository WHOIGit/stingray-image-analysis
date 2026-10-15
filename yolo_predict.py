#!/usr/bin/env python3
"""Run resumable YOLO video prediction with one persistent worker per GPU."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path

import yaml


CONTROLLED_ARGS = {
    "device",
    "exist_ok",
    "mode",
    "model",
    "name",
    "project",
    "source",
    "task",
}


def log(message: str) -> None:
    """Write an immediately visible worker message."""
    print(message, file=sys.stderr, flush=True)


def video_index(row: dict[str, str]) -> int:
    """Return a stable timestamp-list order when an index is available."""
    try:
        return int(row.get("video_index", ""))
    except (TypeError, ValueError):
        return 0


def read_eligible_videos(
    video_list: Path,
    statuses: set[str],
    suffix: str,
) -> list[Path]:
    """Read the canonical timestamp video list and apply prediction filters."""
    with video_list.open(newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None or "media_path" not in reader.fieldnames:
            raise ValueError(f"{video_list} is missing the media_path column")
        rows = list(reader)

    eligible = [
        row
        for row in rows
        if Path(row["media_path"]).suffix.lower() == suffix.lower()
        and row.get("status", "valid") in statuses
    ]
    eligible.sort(key=video_index)
    return [Path(row["media_path"]) for row in eligible]


def read_completed_videos(project: Path) -> set[str]:
    """Discover videos with an atomic per-video success marker."""
    completed: set[str] = set()
    runs_dir = project / "runs"
    if not runs_dir.exists():
        return completed

    for marker in runs_dir.glob("*/worker_*/*/_SUCCESS"):
        source_file = marker.parent / "source.txt"
        try:
            source = source_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            log(f"[WARN] Cannot read completed source {source_file}: {error}")
            continue
        if source:
            completed.add(source)
    return completed


def parse_predict_args(arguments: list[str]) -> dict[str, object]:
    """Convert existing Ultralytics key=value configuration into Python values."""
    parsed: dict[str, object] = {}
    for argument in arguments:
        if "=" not in argument:
            raise ValueError(f"Prediction argument must use key=value: {argument}")
        name, raw_value = argument.split("=", 1)
        if name in CONTROLLED_ARGS:
            continue
        parsed[name] = yaml.safe_load(raw_value)
    return parsed


def allocated_devices(selection: str) -> list[str]:
    """Resolve either locally selected devices or every Slurm-visible GPU."""
    if selection != "allocated":
        devices = [value.strip() for value in selection.split(",") if value.strip()]
        if not devices:
            raise ValueError("At least one local prediction device is required")
        return devices

    import torch

    count = torch.cuda.device_count()
    if count < 1:
        raise RuntimeError("Slurm did not expose any GPUs to this job")
    return [str(index) for index in range(count)]


def video_directory_name(video: Path) -> str:
    """Build a readable, collision-resistant directory name for one video."""
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", video.stem).strip("._-")
    digest = hashlib.sha1(str(video).encode("utf-8")).hexdigest()[:10]
    return f"{readable[:100]}_{digest}"


def mark_complete(output_dir: Path, video: Path) -> None:
    """Publish source metadata before atomically publishing the success marker."""
    source_file = output_dir / "source.txt"
    source_tmp = output_dir / "source.txt.tmp"
    source_tmp.write_text(f"{video}\n", encoding="utf-8")
    source_tmp.replace(source_file)

    marker = output_dir / "_SUCCESS"
    marker_tmp = output_dir / "_SUCCESS.tmp"
    marker_tmp.write_text("", encoding="utf-8")
    marker_tmp.replace(marker)


def process_queue(
    worker_id: int,
    device: str,
    work_queue,
    run_dir: Path,
    model_path: str,
    predict_kwargs: dict[str, object],
) -> None:
    """Load one model and process queued videos until none remain."""
    from ultralytics import YOLO

    log(f"[INFO] Worker {worker_id}: loading model on device {device}")
    model = YOLO(model_path)
    worker_dir = run_dir / f"worker_{worker_id}"
    worker_dir.mkdir(parents=True, exist_ok=True)

    completed = 0
    errors = 0
    while True:
        video_text = work_queue.get()
        if video_text is None:
            break

        video = Path(video_text)
        output_name = video_directory_name(video)
        output_dir = worker_dir / output_name
        log(f"[INFO] Worker {worker_id}: predicting {video}")
        try:
            results = model.predict(
                source=str(video),
                device=device,
                project=str(worker_dir),
                name=output_name,
                exist_ok=True,
                stream=True,
                **predict_kwargs,
            )
            seen = 0
            for _ in results:
                seen += 1
            if seen == 0:
                raise RuntimeError("Prediction returned no frames")
            output_dir.mkdir(parents=True, exist_ok=True)
            mark_complete(output_dir, video)
            completed += 1
            log(f"[DONE] Worker {worker_id}: {video}")
        except Exception as error:
            errors += 1
            log(f"[ERROR] Worker {worker_id}: {video}: {error}")

    log(f"[INFO] Worker {worker_id}: completed={completed} errors={errors}")
    if errors:
        raise RuntimeError(f"Worker {worker_id} had {errors} video errors")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-list", required=True, type=Path)
    parser.add_argument("--statuses", required=True)
    parser.add_argument("--suffix", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--devices", required=True)
    parser.add_argument("--file-limit", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("predict_args", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.file_limit is not None and args.file_limit < 1:
        raise ValueError("--file-limit must be a positive integer")
    predict_kwargs = parse_predict_args(args.predict_args)
    # Ultralytics label suffixes count processed frames, so with a stride they
    # no longer name source-video frames and every merged frame would be wrong.
    if predict_kwargs.get("vid_stride", 1) != 1:
        raise ValueError("vid_stride must be 1 so label frames match source-video frames")

    # Give Ultralytics an absolute project path so it cannot nest the configured
    # workspace beneath its default runs/detect directory.
    project = args.project.expanduser().resolve()

    statuses = {value for value in args.statuses.split(",") if value}
    eligible = read_eligible_videos(args.video_list, statuses, args.suffix)
    completed = read_completed_videos(project)
    pending = [video for video in eligible if str(video) not in completed]
    previously_completed = len(eligible) - len(pending)
    remaining = pending
    if args.file_limit is not None:
        remaining = remaining[: args.file_limit]

    log(f"[INFO] Eligible videos: {len(eligible)}")
    log(f"[INFO] Previously completed: {previously_completed}")
    log(f"[INFO] Remaining before file limit: {len(pending)}")
    log(f"[INFO] Selected for this run: {len(remaining)}")
    if not remaining:
        log("[DONE] No remaining videos to process")
        return 0

    devices = allocated_devices(args.devices)
    worker_count = min(len(devices), len(remaining))
    run_id = args.run_id or os.environ.get("SLURM_JOB_ID")
    if not run_id:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        run_id = f"{timestamp}_{os.getpid()}"
    run_dir = project / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    context = get_context("spawn")
    work_queue = context.Queue()
    for video in remaining:
        work_queue.put(str(video))
    for _ in range(worker_count):
        work_queue.put(None)

    log(f"[INFO] Starting {worker_count} workers in {run_dir}")
    processes = []
    for worker_id, device in enumerate(devices[:worker_count]):
        process = context.Process(
            target=process_queue,
            args=(
                worker_id,
                device,
                work_queue,
                run_dir,
                args.model,
                predict_kwargs,
            ),
        )
        process.start()
        processes.append(process)

    exit_code = 0
    for process in processes:
        process.join()
        if process.exitcode and exit_code == 0:
            exit_code = process.exitcode

    if exit_code == 0:
        log("[DONE] All prediction workers completed successfully")
    else:
        log("[ERROR] One or more prediction workers failed")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
