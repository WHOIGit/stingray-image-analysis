"""Thin Prefect flows for the containerized timestamp and abundance jobs."""

from __future__ import annotations

from pathlib import Path

import docker
from prefect import flow, get_run_logger, task


DEFAULT_IMAGE = "ghcr.io/WHOIGit/stingray-image-analysis:latest"
CONTAINER_CONFIG = "/run/cruise.conf.sh"
CONTAINER_WORKSPACE = "/app/workspace"


def _existing_path(path: str, description: str) -> Path:
    """Resolve a required host path before passing it to Docker."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"{description} does not exist: {resolved}")
    return resolved


@task(log_prints=True)
def run_image_analysis_job(
    job: str,
    config_path: str,
    data_roots: list[str],
    image: str,
    workspace_dir: str | None = None,
) -> None:
    """Pull the processing image and run one existing shell job inside it."""
    commands = {
        "frame_timestamps": "frame_timestamps.sh",
        "image_abundance": "image_abundance.sh",
    }
    if job not in commands:
        raise ValueError(f"Unsupported image-analysis job: {job}")

    config = _existing_path(config_path, "Cruise configuration")
    volumes: dict[str, dict[str, str]] = {
        str(config): {"bind": CONTAINER_CONFIG, "mode": "ro"},
    }

    # Bind configured data roots at identical paths so shell configuration
    # values work inside the container without translation.
    for data_root in data_roots:
        root = _existing_path(data_root, "Data root")
        volumes[str(root)] = {"bind": str(root), "mode": "rw"}

    # Preserve abundance intermediates after the short-lived container exits.
    if workspace_dir is not None:
        workspace = Path(workspace_dir).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        volumes[str(workspace)] = {"bind": CONTAINER_WORKSPACE, "mode": "rw"}

    client = docker.from_env()
    logger = get_run_logger()
    logger.info("Pulling container image: %s", image)
    client.images.pull(image)

    command = ["bash", commands[job], CONTAINER_CONFIG]
    logger.info("Running image-analysis job: %s", job)
    try:
        output = client.containers.run(
            image,
            command,
            volumes=volumes,
            working_dir="/app",
            remove=True,
            detach=False,
            stdout=True,
            stderr=True,
            stream=True,
        )
        for line in output:
            logger.info(line.decode("utf-8").rstrip())
    except docker.errors.ContainerError as error:
        message = error.stderr.decode("utf-8") if error.stderr else "No stderr"
        logger.error("Container failed: %s", message)
        raise RuntimeError(
            f"Container failed with exit code {error.exit_status}"
        ) from error


@flow(log_prints=True)
def frame_timestamps(
    config_path: str,
    data_roots: list[str],
    image: str = DEFAULT_IMAGE,
) -> None:
    """Create the shared video and frame timestamp lists."""
    run_image_analysis_job("frame_timestamps", config_path, data_roots, image)


@flow(log_prints=True)
def image_abundance(
    config_path: str,
    data_roots: list[str],
    workspace_dir: str,
    image: str = DEFAULT_IMAGE,
) -> None:
    """Merge detection labels and compute image abundance."""
    run_image_analysis_job(
        "image_abundance",
        config_path,
        data_roots,
        image,
        workspace_dir,
    )
