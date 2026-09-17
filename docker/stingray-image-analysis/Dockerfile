FROM python:3.11-slim

ARG STINGRAYTOOLS_REF=main

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# OpenCV needs these runtime libraries to read video metadata and frames.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Match the environment path already used by the shared cruise configuration.
RUN python -m venv /app/.venv/cvision \
    && /app/.venv/cvision/bin/python -m pip install --upgrade pip setuptools wheel \
    && /app/.venv/cvision/bin/python -m pip install \
        pyyaml \
        "stingraytools[pipeline] @ https://github.com/anhph95/stingraytools/archive/${STINGRAYTOOLS_REF}.tar.gz"

# Reuse the same timestamp and abundance implementations as local and Slurm runs.
COPY frame_timestamps.sh image_abundance.sh merge_detection_labels.sh ./
COPY build_class_map.py ./

RUN chmod +x frame_timestamps.sh image_abundance.sh merge_detection_labels.sh
