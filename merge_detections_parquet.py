#!/usr/bin/env python3
"""Merge Ultralytics YOLO video labels into one canonical Parquet detection table.

The output follows the shared detection contract:

    media, frame, class, class_id, x, y, width, height, score

``frame`` is the 0-based source-video frame (Ultralytics names video labels
from 1), the box is a COCO pixel box (top-left x/y, width, height), and the
file metadata carries the model class list and the image size.

Each completed video is first written as its own Parquet part in a progress
folder beside the output, so an interrupted merge resumes where it stopped.
Exact repeated lines within a label file (Ultralytics appends when a video is
predicted twice into one folder) are removed and only counted. Problem lines
and files are skipped and listed in ``<output>_issues.csv``, one row per video
and issue type with file and line counts and the first example.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from multiprocessing import get_context
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from build_class_map import read_class_names

SCHEMA = pa.schema([
    ("media", pa.string()),
    ("frame", pa.int32()),
    ("class", pa.string()),
    ("class_id", pa.int16()),
    ("x", pa.float64()),
    ("y", pa.float64()),
    ("width", pa.float64()),
    ("height", pa.float64()),
    ("score", pa.float64()),
])
ISSUE_FIELDS = ["issue_type", "media", "source", "detail", "reason"]
# Combined row groups of about this many rows keep the output fast to filter.
ROW_GROUP_ROWS = 1_000_000

# Worker settings, set once per process by init_worker.
CLASS_NAMES: dict[int, str] = {}
IMAGE_WIDTH = 0
IMAGE_HEIGHT = 0


def log(message: str) -> None:
    """Write an immediately visible progress message."""
    print(message, file=sys.stderr, flush=True)


@dataclass
class VideoSource:
    """Label files of one completed video and when its run completed."""

    label_files: list[Path] = field(default_factory=list)
    completed_time: float | None = None
    origin: str = ""


def label_media(path: Path) -> str:
    """Return the video stem of an Ultralytics ``<stem>_<frame>.txt`` label."""
    return path.stem.rpartition("_")[0]


def find_completed_videos(
    label_dirs: list[Path],
    issues: list[dict[str, str]],
) -> dict[str, VideoSource]:
    """Find labels registered by a _SUCCESS marker or a Prefect manifest."""
    found: dict[str, list[VideoSource]] = {}

    for label_dir in label_dirs:
        if not label_dir.is_dir():
            log(f"[WARN] Directory not found; skipping: {label_dir}")
            continue
        log(f"[INFO] Scanning directory: {label_dir}")

        # Native Slurm/local outputs keep one success marker beside each
        # completed video's labels.
        for marker in sorted(label_dir.rglob("_SUCCESS")):
            labels = marker.parent / "labels"
            if not labels.is_dir():
                continue
            sources: dict[str, VideoSource] = {}
            for path in sorted(labels.glob("*.txt")):
                source = sources.setdefault(
                    label_media(path),
                    VideoSource(completed_time=marker.stat().st_mtime, origin=str(marker.parent)),
                )
                source.label_files.append(path)
            for media, source in sources.items():
                found.setdefault(media, []).append(source)

        # The Prefect runner stores completed source paths centrally and
        # writes labels below gpuN/labels; partial failed output is excluded.
        manifest = label_dir / ".completed_files.txt"
        if manifest.is_file():
            completed = {
                Path(line.strip()).stem
                for line in manifest.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            sources = {}
            for root, _, files in os.walk(label_dir):
                root_path = Path(root)
                if root_path.name != "labels" or not root_path.parent.name.startswith("gpu"):
                    continue
                for name in sorted(files):
                    if not name.endswith(".txt"):
                        continue
                    path = root_path / name
                    media = label_media(path)
                    if media in completed:
                        sources.setdefault(media, VideoSource(origin=str(manifest))).label_files.append(path)
            for media, source in sources.items():
                found.setdefault(media, []).append(source)

    videos: dict[str, VideoSource] = {}
    for media, sources in found.items():
        if len(sources) > 1:
            # Prefer the most recently completed run; Prefect sources carry no
            # completion time and rank last.
            sources.sort(key=lambda source: source.completed_time or 0.0, reverse=True)
            issues.append({
                "issue_type": "duplicate_video_source",
                "media": media,
                "source": sources[0].origin,
                "detail": "; ".join(source.origin for source in sources[1:]),
                "reason": "video completed in more than one run; the newest run was merged",
            })
        videos[media] = sources[0]
    return videos


def part_name(media: str) -> str:
    """Build a readable, collision-resistant part filename for one video."""
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", media).strip("._-")
    digest = hashlib.sha1(media.encode("utf-8")).hexdigest()[:10]
    return f"{readable[:100]}_{digest}"


def init_worker(class_names: dict[int, str], image_width: int, image_height: int) -> None:
    global CLASS_NAMES, IMAGE_WIDTH, IMAGE_HEIGHT
    CLASS_NAMES = class_names
    IMAGE_WIDTH = image_width
    IMAGE_HEIGHT = image_height


def frame_suffix(path: Path) -> int | None:
    """Return the integer frame suffix of a label filename, if it has one."""
    suffix = path.stem.rpartition("_")[2]
    return int(suffix) if suffix.isdigit() else None


ISSUE_REASONS = {
    "malformed_filename": "label name does not end in _<frame>.txt; file skipped",
    "frame_suffix_zero": "Ultralytics video labels start at frame 1; file skipped",
    "missing_confidence": "label has no confidence (run inference with save_conf=True); lines skipped",
    "malformed_line": "expected class, box and confidence as numbers; lines skipped",
    "unknown_class": "class ID is not in the class YAML; lines skipped",
}
# Parquet part metadata key holding how many repeated lines a video dropped.
DUPLICATES_KEY = b"duplicate_lines_removed"


def merge_video(task: tuple[str, list[str], str]) -> tuple[str, int, Counter, str]:
    """Parse one video's label files into a Parquet part and its issue list.

    Returns the media name, row count, issue counts, and an error message when
    the video could not be merged (it is then retried on the next run).
    """
    media, label_paths, part_base = task
    try:
        columns: dict[str, list] = {name: [] for name in SCHEMA.names}
        duplicates = 0
        # One issue row per video and type: how many files and lines, and the first example.
        found: dict[str, dict] = {}

        def note(issue_type: str, path: Path, lines: int, example: str) -> None:
            entry = found.setdefault(issue_type, {"files": 0, "lines": 0, "source": str(path), "example": example})
            entry["files"] += 1
            entry["lines"] += lines

        paths = [Path(path) for path in label_paths]
        for path in sorted(paths, key=lambda item: frame_suffix(item) or 0):
            suffix = frame_suffix(path)
            if suffix is None:
                note("malformed_filename", path, 0, path.name)
                continue
            if suffix == 0:
                note("frame_suffix_zero", path, 0, path.name)
                continue
            # Ultralytics numbers video labels from 1; the frame list, abundance
            # and the labeler count source-video frames from 0.
            frame = suffix - 1

            line_counts: Counter = Counter()
            first_example: dict[str, str] = {}
            seen_lines: set[str] = set()
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    fields = text.split()
                    problem = None
                    if len(fields) == 5:
                        problem = "missing_confidence"
                    elif len(fields) < 5:
                        problem = "malformed_line"
                    else:
                        try:
                            class_id = int(fields[0])
                            x_center, y_center, width, height, score = (float(value) for value in fields[1:6])
                        except ValueError:
                            problem = "malformed_line"
                        else:
                            if class_id not in CLASS_NAMES:
                                problem = "unknown_class"
                    if problem is None and text in seen_lines:
                        # Ultralytics appends to label files, so a video predicted twice
                        # into one folder repeats its lines exactly; keep one copy.
                        duplicates += 1
                        continue
                    if problem is not None:
                        line_counts[problem] += 1
                        first_example.setdefault(problem, f"line {line_number}: {text}")
                        continue
                    seen_lines.add(text)

                    columns["media"].append(media)
                    columns["frame"].append(frame)
                    columns["class"].append(CLASS_NAMES[class_id])
                    columns["class_id"].append(class_id)
                    columns["x"].append((x_center - width / 2.0) * IMAGE_WIDTH)
                    columns["y"].append((y_center - height / 2.0) * IMAGE_HEIGHT)
                    columns["width"].append(width * IMAGE_WIDTH)
                    columns["height"].append(height * IMAGE_HEIGHT)
                    columns["score"].append(score)
            for problem, count in line_counts.items():
                note(problem, path, count, first_example[problem])

        issues = [
            {
                "issue_type": issue_type,
                "media": media,
                "source": entry["source"],
                "detail": f"{entry['files']} file(s), {entry['lines']} line(s); first: {entry['example']}",
                "reason": ISSUE_REASONS[issue_type],
            }
            for issue_type, entry in sorted(found.items())
        ]
        # Write the issue list before the part: a finished part marks a finished video.
        write_issues(Path(f"{part_base}.issues.csv"), issues)
        part = Path(f"{part_base}.parquet")
        temporary = Path(f"{part_base}.parquet.tmp")
        schema = SCHEMA.with_metadata({DUPLICATES_KEY: str(duplicates).encode("utf-8")})
        pq.write_table(pa.table(columns, schema=schema), temporary, compression="zstd")
        os.replace(temporary, part)
        return media, len(columns["media"]), Counter(item["issue_type"] for item in issues), ""
    except Exception as error:  # one unreadable video must not stop the merge
        return media, 0, Counter(), f"{type(error).__name__}: {error}"


def write_issues(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ISSUE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def read_issues(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def prepare_parts_dir(parts_dir: Path, settings: dict, rebuild: bool) -> None:
    """Reuse finished parts only when they were made with the same settings."""
    settings_path = parts_dir / "settings.json"
    if parts_dir.is_dir():
        previous = None
        if settings_path.is_file():
            previous = json.loads(settings_path.read_text(encoding="utf-8"))
        if rebuild or previous != settings:
            reason = "--rebuild requested" if rebuild else "class list or image size changed"
            log(f"[INFO] Discarding previous progress ({reason}): {parts_dir}")
            shutil.rmtree(parts_dir)
    parts_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def combine_parts(
    output: Path,
    parts: list[Path],
    metadata: dict[bytes, bytes],
) -> tuple[int, int]:
    """Concatenate finished parts into one Parquet file, written atomically.

    Returns the rows written and the repeated lines the parts removed.
    """
    schema = SCHEMA.with_metadata(metadata)
    temporary = output.with_name(output.name + ".tmp")
    rows = 0
    duplicates = 0
    pending: list[pa.Table] = []
    pending_rows = 0
    try:
        with pq.ParquetWriter(temporary, schema, compression="zstd") as writer:
            for part in parts:
                table = pq.read_table(part)
                duplicates += int((table.schema.metadata or {}).get(DUPLICATES_KEY, b"0"))
                table = table.replace_schema_metadata(None)
                pending.append(table)
                pending_rows += table.num_rows
                if pending_rows >= ROW_GROUP_ROWS:
                    writer.write_table(pa.concat_tables(pending))
                    rows += pending_rows
                    pending, pending_rows = [], 0
            if pending:
                writer.write_table(pa.concat_tables(pending))
                rows += pending_rows
        os.replace(temporary, output)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    return rows, duplicates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-parquet", required=True, type=Path)
    parser.add_argument("--class-yaml", required=True, type=Path, help="YAML file containing a names section.")
    parser.add_argument("--image-width", required=True, type=int, help="Video frame width in pixels.")
    parser.add_argument("--image-height", required=True, type=int, help="Video frame height in pixels.")
    parser.add_argument("--jobs", type=int, help="Parallel workers. Defaults to CPU count - 1.")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Discard saved progress and merge every video again.",
    )
    parser.add_argument(
        "--keep-parts",
        action="store_true",
        help="Keep per-video parts after combining, so a later merge only parses new videos.",
    )
    parser.add_argument("label_dirs", nargs="+", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.image_width < 1 or args.image_height < 1:
        raise SystemExit("[ERROR] --image-width and --image-height must be positive integers")
    if args.jobs is not None and args.jobs < 1:
        raise SystemExit("[ERROR] --jobs must be a positive integer")
    jobs = args.jobs or max(1, (os.cpu_count() or 2) - 1)

    class_names = dict(read_class_names(args.class_yaml))
    output = args.output_parquet
    output.parent.mkdir(parents=True, exist_ok=True)
    parts_dir = output.with_name(output.name + ".parts")
    issues_path = output.with_name(f"{output.stem}_issues.csv")

    log(f"[INFO] Output Parquet: {output}")
    log(f"[INFO] Progress folder: {parts_dir}")
    log(f"[INFO] Classes: {len(class_names)}  Image size: {args.image_width}x{args.image_height}")

    run_issues: list[dict[str, str]] = []
    videos = find_completed_videos(args.label_dirs, run_issues)
    if not videos:
        raise SystemExit("[ERROR] No label files found for registered completed videos.")
    log(f"[INFO] Completed videos discovered: {len(videos)}")

    settings = {
        "classes": {str(class_id): name for class_id, name in sorted(class_names.items())},
        "image_width": args.image_width,
        "image_height": args.image_height,
    }
    prepare_parts_dir(parts_dir, settings, args.rebuild)

    tasks = []
    for media, source in sorted(videos.items()):
        part_base = parts_dir / part_name(media)
        part = Path(f"{part_base}.parquet")
        # A video predicted again after its part was written is merged again.
        if part.is_file() and (source.completed_time is None or source.completed_time <= part.stat().st_mtime):
            continue
        tasks.append((media, [str(path) for path in source.label_files], str(part_base)))
    log(f"[INFO] Already merged: {len(videos) - len(tasks)}  Remaining: {len(tasks)}  Workers: {jobs}")

    if tasks:
        issue_counts: Counter = Counter()
        context = get_context("spawn")
        with context.Pool(
            min(jobs, len(tasks)),
            initializer=init_worker,
            initargs=(class_names, args.image_width, args.image_height),
        ) as pool:
            results = pool.imap_unordered(merge_video, tasks)
            for done, (media, rows, counts, error) in enumerate(results, start=1):
                issue_counts.update(counts)
                if error:
                    log(f"[WARN] Could not merge {media}; it will be retried on the next run: {error}")
                    run_issues.append({
                        "issue_type": "video_not_merged",
                        "media": media,
                        "source": videos[media].origin,
                        "detail": error,
                        "reason": "left out of this output; rerun the merge to retry",
                    })
                if done % 100 == 0 or done == len(tasks):
                    log(f"[INFO] Merged {done}/{len(tasks)} videos (last: {media}, {rows} rows)")
        for issue_type, count in sorted(issue_counts.items()):
            log(f"[WARN] {issue_type}: {count} video(s) in this run")

    merged = [media for media in sorted(videos) if Path(f"{parts_dir / part_name(media)}.parquet").is_file()]
    if not merged:
        raise SystemExit("[ERROR] No video could be merged; see the warnings above.")
    parts = [Path(f"{parts_dir / part_name(media)}.parquet") for media in merged]
    metadata = {
        b"classes": json.dumps(settings["classes"]).encode("utf-8"),
        b"image_width": str(args.image_width).encode("utf-8"),
        b"image_height": str(args.image_height).encode("utf-8"),
    }
    rows, duplicates = combine_parts(output, parts, metadata)

    all_issues = list(run_issues)
    for media in merged:
        all_issues.extend(read_issues(Path(f"{parts_dir / part_name(media)}.issues.csv")))
    write_issues(issues_path, all_issues)
    for issue_type, count in sorted(Counter(item["issue_type"] for item in all_issues).items()):
        log(f"[WARN] {issue_type}: {count} video(s) in total")

    # Progress is kept while any video still needs merging, so the next run
    # only retries those.
    if len(merged) < len(videos):
        log(f"[WARN] {len(videos) - len(merged)} video(s) not merged; progress kept in {parts_dir}")
    elif not args.keep_parts:
        shutil.rmtree(parts_dir)

    log(f"[INFO] Detection rows written: {rows:,}")
    log(f"[INFO] Duplicate lines removed: {duplicates:,}")
    log(f"[INFO] Issues listed: {len(all_issues)} in {issues_path}")
    log(f"[SUCCESS] Output Parquet created: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
