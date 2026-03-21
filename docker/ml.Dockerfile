# CUDA 12.8 devel image includes nvcc, all CUDA headers, and development libs
# needed to compile third-party CUDA extensions (e.g. BigVGAN anti-alias kernel).
# The image is ~1 GB larger than the runtime variant but that is acceptable given
# the overall image size (PyTorch + ML libraries).
# For CPU-only environments switch to python:3.11-slim (remove --gpus flag too).
FROM nvidia/cuda:12.8.0-devel-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.11 python3.11-dev python3-pip \
        ffmpeg build-essential \
        libsndfile1-dev \
    && ln -sf python3.11 /usr/bin/python3 \
    && ln -sf python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ml_service/pyproject.toml /app/ml_service/pyproject.toml
COPY ml_service/app /app/ml_service/app
COPY ml_service/tests /app/ml_service/tests
COPY config /app/config

ARG PYTHON_EXTRAS=
# When ML_PYTHON_EXTRAS=real, install PyTorch first with the CUDA 12.8 index so
# the correct GPU wheels are pulled (avoids falling back to CPU-only torch).
RUN python -m pip install --no-cache-dir ninja

RUN if [ -n "$PYTHON_EXTRAS" ]; then \
        pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cu128 \
        && python -m pip install --no-cache-dir "/app/ml_service[$PYTHON_EXTRAS]"; \
       else \
        python -m pip install --no-cache-dir /app/ml_service; \
       fi

# Patch BigVGAN CUDA sources: remove the unused #include <cuda_profiler_api.h>
# that some package versions include but which is not strictly required.
RUN if [ -n "$PYTHON_EXTRAS" ]; then \
    find /usr/local/lib/python3.11/dist-packages/indextts -name "*.cu" \
         -exec sed -i 's|#include <cuda_profiler_api.h>|// cuda_profiler_api.h removed (unused)|g' {} \; \
    && echo "BigVGAN CUDA sources patched"; \
    fi

# Pre-compile BigVGAN's CUDA kernel at build time so every container start loads
# the cached .so directly instead of compiling on first TTS inference call (~2 min).
# Targets: 7.5=Turing, 8.0=A100, 8.6=RTX3080, 8.9=RTX4090, 12.0=RTX5080(Blackwell)
COPY docker/precompile_bigvgan.py /tmp/precompile_bigvgan.py
RUN if [ -n "$PYTHON_EXTRAS" ]; then \
        TORCH_CUDA_ARCH_LIST="7.5;8.0;8.6;8.9+PTX;12.0" \
        python3 /tmp/precompile_bigvgan.py \
        || echo "BigVGAN pre-compilation warning (non-fatal; will compile at first run)"; \
    fi

WORKDIR /app/ml_service
ENV DATA_ROOT=/data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
