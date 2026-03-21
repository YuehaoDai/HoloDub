import asyncio
import logging
from functools import partial

from fastapi import APIRouter, Request

from app.models import TTSRequest, TTSResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tts", tags=["tts"])


@router.post("/run", response_model=TTSResponse)
async def run_tts(request: Request, payload: TTSRequest) -> TTSResponse:
    services = request.app.state.services
    loop = asyncio.get_running_loop()
    async with services.gpu_guard.acquire():
        # Run the blocking TTS synthesis in a thread pool so the asyncio event
        # loop stays free to handle healthz and other requests during long
        # model loading or inference (IndexTTS2 can take minutes on first run).
        return await loop.run_in_executor(None, partial(services.tts.synthesize, payload))
