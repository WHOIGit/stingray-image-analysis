#!/usr/bin/env bash
#SBATCH --job-name=frame_timestamps
#SBATCH --cpus-per-task=12
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --partition=compute
#SBATCH --output=slogs/%A.%x.out
#SBATCH --error=slogs/%A.%x.err
#SBATCH --mail-type=ALL

###############################################################################
# Build the media/frame timestamp tables (Parquet) needed by abundance.
###############################################################################

set -euxo pipefail

# Record the allocation context before starting the portable workhorse.
echo "Job ID: $SLURM_JOB_ID, JobName: $SLURM_JOB_NAME"
hostname
pwd
date

# Load the cluster environment required by the configured virtual environment.
module purge
module load miniconda/25.9

# Expose the Slurm CPU allocation; the workhorse reserves one CPU for control.
export STINGRAY_CPU_COUNT="${SLURM_CPUS_PER_TASK:-1}"

# Preserve the submission working directory for config, input, and output paths.
exec bash frame_timestamps.sh "$@"
