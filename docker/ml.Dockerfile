# CUDA 12.8 devel image includes nvcc, all CUDA headers, and development libs
# needed to compile third-party CUDA extensions (e.g. BigVGAN anti-alias kernel).
# The image is ~1 GB larger than the runtime variant but that is acceptable given
# the overall image size (PyTorch + ML libraries).
# For CPU-only environments switch to python:3.11-slim (remove --gpus flag too).
FROM nvidia/cuda:12.8.0-devel-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_DEFAULT_TIMEOUT=1200 \
    PIP_RETRIES=10

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

# Large CUDA wheels come from PyPI mirrors; default pip read timeout is too short on slow links.
RUN if [ -n "$PYTHON_EXTRAS" ]; then \
        pip install --no-cache-dir --retries 10 \
            torch torchaudio --index-url https://download.pytorch.org/whl/cu128 \
        && python -m pip install --no-cache-dir --retries 10 \
            "/app/ml_service[$PYTHON_EXTRAS]"; \
       else \
        python -m pip install --no-cache-dir --retries 10 /app/ml_service; \
       fi

# Patch BigVGAN CUDA sources: remove the unused #include <cuda_profiler_api.h>
# that some package versions include but which is not strictly required.
RUN if [ -n "$PYTHON_EXTRAS" ]; then \
    find /usr/local/lib/python3.11/dist-packages/indextts -name "*.cu" \
         -exec sed -i 's|#include <cuda_profiler_api.h>|// cuda_profiler_api.h removed (unused)|g' {} \; \
    && echo "BigVGAN CUDA sources patched"; \
    fi

# Pre-compile BigVGAN's anti-alias-activation CUDA kernel at build time and
# write the .so into the EXACT directory that indextts' upstream loader uses
# (``<site-packages>/indextts/s2mel/modules/bigvgan/alias_free_activation/cuda/build/``).
# This matters because that loader hard-codes its own build_directory, so any
# .so left under PyTorch's default cache (``~/.cache/torch_extensions``) is
# silently ignored at runtime, forcing a 1-3 minute JIT recompile on every
# fresh container --- which has been observed to deadlock indefinitely on
# RTX 50-class (sm_120) GPUs when the recompile is initiated from a FastAPI
# lifespan worker thread.
#
# BIGVGAN_TARGET_SM picks the deployment GPU's compute capability so the
# generated build.ninja matches what runtime IndexTTS will recompute, letting
# PyTorch's cache validity check skip the JIT path altogether.
#   70=V100, 75=Turing, 80=A100, 86=RTX3080, 89=RTX4090, 120=RTX5080(Blackwell)
ARG BIGVGAN_TARGET_SM=120
ENV BIGVGAN_TARGET_SM=${BIGVGAN_TARGET_SM}
COPY docker/precompile_bigvgan.py /tmp/precompile_bigvgan.py
RUN if [ -n "$PYTHON_EXTRAS" ]; then \
        python3 /tmp/precompile_bigvgan.py \
        && ls -la /usr/local/lib/python3.11/dist-packages/indextts/s2mel/modules/bigvgan/alias_free_activation/cuda/build/anti_alias_activation_cuda.so \
        || echo "BigVGAN pre-compilation warning (non-fatal; will compile at first run, may hang on sm_120)"; \
    fi

WORKDIR /app/ml_service
ENV DATA_ROOT=/data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
