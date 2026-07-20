import asyncio
from functools import partial

from fastapi import APIRouter, Request

from app.models import SeparateRequest, SeparateResponse

router = APIRouter(prefix="/media", tags=["media"])


@router.post("/separate", response_model=SeparateResponse)
async def separate_media(request: Request, payload: SeparateRequest) -> SeparateResponse:
    services = request.app.state.services
    loop = asyncio.get_event_loop()
    async with services.gpu_guard.acquire():
        # Inline IndexTTS2 is often multi-GB resident after warm-up. Demucs runs
        # as a subprocess and loads its own model, so release TTS before long
        # separation work to avoid the OOM killer on local 16GB machines.
        services.tts.release_indextts2()
        # Run blocking Demucs separation in a thread pool.
        return await loop.run_in_executor(None, partial(services.separator.separate, payload))
