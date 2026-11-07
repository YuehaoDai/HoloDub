# Requires Docker BuildKit (RUN cache mounts are used below). Omitting the
# external docker/dockerfile frontend keeps offline rebuilds on Docker Desktop
# from failing before locally cached base layers can be used.

# CUDA 12.8 devel image includes nvcc, all CUDA headers, and development libs
# needed to compile third-party CUDA extensions (e.g. BigVGAN anti-alias kernel).
# The image is ~1 GB larger than the runtime variant but that is acceptable given
# the overall image size (PyTorch + ML libraries).
# For CPU-only environments switch to python:3.11-slim (remove --gpus flag too).
FROM nvidia/cuda:12.8.2-devel-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_DEFAULT_TIMEOUT=1200 \
    PIP_RETRIES=10 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_HOME=/root/.cache/huggingface \
    TORCH_HOME=/root/.cache/torch \
    XDG_CACHE_HOME=/root/.cache

# Optional Ubuntu mirror base URL (e.g. https://mirrors.tuna.tsinghua.edu.cn/ubuntu)
# for hosts whose proxy drops long plain-HTTP downloads from archive.ubuntu.com.
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.11 python3.11-dev python3-pip \
        ffmpeg build-essential \
        libsndfile1-dev \
    && ln -sf python3.11 /usr/bin/python3 \
    && ln -sf python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ml_service/pyproject.toml /app/ml_service/pyproject.toml
COPY ml_service/README.md /app/ml_service/README.md

ARG PYTHON_EXTRAS=
# When ML_PYTHON_EXTRAS=real, install PyTorch first with the CUDA 12.8 index so
# the correct GPU wheels are pulled (avoids falling back to CPU-only torch).
# Keep torch/torchaudio/torchcodec as a tested set: torchaudio.save() imports
# TorchCodec, and newer TorchCodec CUDA wheels can require CUDA 13 libnvrtc even
# inside this CUDA 12.8 image.
ARG TORCH_VERSION=2.11.0
ARG TORCHAUDIO_VERSION=2.11.0
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install ninja

# Large CUDA wheels come from PyPI mirrors; default pip read timeout is too short on slow links.
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "$PYTHON_EXTRAS" ]; then \
        python -m pip install --retries 10 \
            torch==${TORCH_VERSION} torchaudio==${TORCHAUDIO_VERSION} \
            --index-url https://download.pytorch.org/whl/cu128; \
    fi

# Install third-party dependencies from pyproject metadata before copying source
# code. This keeps app/config/test edits from invalidating the multi-GB ML wheel
# layers; the local app package itself is installed later with --no-deps.
RUN PYTHON_EXTRAS="$PYTHON_EXTRAS" python - <<'PY'
import os
import re
import tomllib
from pathlib import Path

project = tomllib.loads(Path("/app/ml_service/pyproject.toml").read_text())
requirements = list(project["project"].get("dependencies", []))
extras = os.environ.get("PYTHON_EXTRAS", "").strip()
if extras:
    requirements.extend(project["project"].get("optional-dependencies", {}).get(extras, []))

# Torch/TorchAudio are installed from the CUDA-specific index above. Keeping
# them out of the generic PyPI pass prevents accidental CPU-wheel resolution if
# a future edit changes the resolver state.
skip = {"torch", "torchaudio"}
filtered: list[str] = []
for req in requirements:
    name = re.split(r"[<>=!~;\[]", req, maxsplit=1)[0].strip().lower().replace("_", "-")
    if name in skip:
        continue
    filtered.append(req)

Path("/tmp/ml-requirements.txt").write_text("\n".join(filtered) + ("\n" if filtered else ""))
PY

RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -s /tmp/ml-requirements.txt ]; then \
        python -m pip install --retries 10 -r /tmp/ml-requirements.txt; \
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
COPY ml_service/app /app/ml_service/app
COPY ml_service/tests /app/ml_service/tests
COPY config /app/config
COPY tools/lora_train_indextts2_smoke.py /app/tools/lora_train_indextts2_smoke.py
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-deps --no-build-isolation /app/ml_service

ENV DATA_ROOT=/data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
