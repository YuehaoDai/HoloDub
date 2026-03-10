# Use CUDA 12.8 runtime so the container can access the host GPU via nvidia runtime.
# Switch back to python:3.11-slim for CPU-only environments (remove --gpus flag too).
FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.11 python3.11-dev python3-pip \
        ffmpeg build-essential \
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
RUN python -m pip install --no-cache-dir --upgrade pip \
    && if [ -n "$PYTHON_EXTRAS" ]; then \
        pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu128 \
        && python -m pip install --no-cache-dir "/app/ml_service[$PYTHON_EXTRAS]"; \
       else \
        python -m pip install --no-cache-dir /app/ml_service; \
       fi

WORKDIR /app/ml_service
ENV DATA_ROOT=/data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
