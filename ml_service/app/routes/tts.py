import asyncio
import logging
from functools import partial

from fastapi import APIRouter, HTTPException, Request

from app.adapters.tts import UnsupportedTTSBackendError
from app.models import TTSRequest, TTSResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tts", tags=["tts"])


@router.post("/run", response_model=TTSResponse)
async def run_tts(request: Request, payload: TTSRequest) -> TTSResponse:
    services = request.app.state.services
    loop = asyncio.get_running_loop()
    async with services.gpu_guard.acquire():
        try:
            return await loop.run_in_executor(
                None, partial(services.tts.synthesize, payload)
            )
        except UnsupportedTTSBackendError as exc:
            logger.error("tts backend misconfigured: %s", exc)
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "tts_backend_unsupported",
                    "message": str(exc),
                    "backend": exc.backend,
                    "supported": list(exc.supported),
                },
            ) from exc
