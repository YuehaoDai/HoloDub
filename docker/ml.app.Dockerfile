# syntax=docker/dockerfile:1.7

# Fast app-layer rebuild for local ML-service iteration.
#
# Use this only after a full ml-service image with the real CUDA/PyTorch
# dependencies already exists locally. It avoids rebuilding the heavy ML base
# when only FastAPI app code, config, tests, or the LoRA trainer script changed.
ARG ML_APP_BASE_IMAGE=holodub-ml-service:latest
FROM ${ML_APP_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_HOME=/root/.cache/huggingface \
    TORCH_HOME=/root/.cache/torch \
    XDG_CACHE_HOME=/root/.cache

WORKDIR /app/ml_service
COPY ml_service/app /app/ml_service/app
COPY ml_service/tests /app/ml_service/tests
COPY config /app/config
COPY tools /app/tools
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-deps --no-build-isolation /app/ml_service

ENV DATA_ROOT=/data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
