import asyncio
import logging
from functools import partial

from fastapi import APIRouter, HTTPException, Request

from app.models import (
    LoraCanaryRunRequest,
    LoraCanaryRunResponse,
    LoraRegressionRunRequest,
    LoraRegressionRunResponse,
    LoraTrainingRunRequest,
    LoraTrainingRunResponse,
)
from app.services.lora_training import LoraTrainingDisabledError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lora-training", tags=["lora-training"])


@router.post("/run-smoke", response_model=LoraTrainingRunResponse)
async def run_lora_training_smoke(
    request: Request, payload: LoraTrainingRunRequest
) -> LoraTrainingRunResponse:
    return await _run_lora_training(request, payload, mode="smoke")


@router.post("/run-full", response_model=LoraTrainingRunResponse)
async def run_lora_training_full(
    request: Request, payload: LoraTrainingRunRequest
) -> LoraTrainingRunResponse:
    return await _run_lora_training(request, payload, mode="full")


@router.post("/run-canary", response_model=LoraCanaryRunResponse)
async def run_lora_training_canary(
    request: Request, payload: LoraCanaryRunRequest
) -> LoraCanaryRunResponse:
    services = request.app.state.services
    loop = asyncio.get_running_loop()
    async with services.gpu_guard.acquire():
        try:
            return await loop.run_in_executor(
                None, partial(services.lora_training.run_canary, payload)
            )
        except LoraTrainingDisabledError as exc:
            raise HTTPException(
                status_code=403,
                detail={"error": str(exc), "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_lora_canary_request", "message": str(exc)},
            ) from exc
        except FileNotFoundError as exc:
            logger.error("lora canary runner misconfigured: %s", exc)
            raise HTTPException(
                status_code=503,
                detail={"error": "lora_canary_runner_unavailable", "message": str(exc)},
            ) from exc


@router.post("/run-regression", response_model=LoraRegressionRunResponse)
async def run_lora_training_regression(
    request: Request, payload: LoraRegressionRunRequest
) -> LoraRegressionRunResponse:
    services = request.app.state.services
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, partial(services.lora_training.run_regression, payload)
        )
    except LoraTrainingDisabledError as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": str(exc), "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_lora_regression_request", "message": str(exc)},
        ) from exc
    except FileNotFoundError as exc:
        logger.error("lora regression runner misconfigured: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "lora_regression_runner_unavailable", "message": str(exc)},
        ) from exc


async def _run_lora_training(
    request: Request, payload: LoraTrainingRunRequest, *, mode: str
) -> LoraTrainingRunResponse:
    services = request.app.state.services
    loop = asyncio.get_running_loop()
    async with services.gpu_guard.acquire():
        try:
            runner = services.lora_training.run_full if mode == "full" else services.lora_training.run_smoke
            return await loop.run_in_executor(
                None, partial(runner, payload)
            )
        except LoraTrainingDisabledError as exc:
            raise HTTPException(
                status_code=403,
                detail={"error": str(exc), "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_lora_training_request", "message": str(exc)},
            ) from exc
        except FileNotFoundError as exc:
            logger.error("lora training runner misconfigured: %s", exc)
            raise HTTPException(
                status_code=503,
                detail={"error": "lora_training_runner_unavailable", "message": str(exc)},
            ) from exc
