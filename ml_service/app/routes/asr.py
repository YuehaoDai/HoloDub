import asyncio
import logging
from functools import partial

from fastapi import APIRouter, HTTPException, Request

from app.models import SmartSplitRequest, SmartSplitResponse
from app.storage import resolve_data_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/asr", tags=["asr"])


@router.post("/smart_split", response_model=SmartSplitResponse)
async def smart_split(request: Request, payload: SmartSplitRequest) -> SmartSplitResponse:
    services = request.app.state.services
    audio_path = resolve_data_path(services.settings.data_root, payload.audio_relpath)
    logger.info(
        "smart_split request: audio_relpath=%s resolved=%s asr=%s vad=%s",
        payload.audio_relpath,
        audio_path,
        services.settings.ml_asr_backend,
        services.settings.ml_vad_backend,
    )
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail=f"audio file not found: {audio_path}")
    loop = asyncio.get_running_loop()

    def _run() -> SmartSplitResponse:
        speech_spans, vad_diagnostics = services.vad.analyze(audio_path)
        segments, asr_diagnostics = services.asr.smart_split(
            audio_path=audio_path,
            source_language=payload.source_language,
            min_segment_sec=payload.min_segment_sec,
            max_segment_sec=payload.max_segment_sec,
            speech_spans=speech_spans,
        )
        return SmartSplitResponse(segments=segments, diagnostics=vad_diagnostics + asr_diagnostics)

    logger.info("smart_split acquiring GPU guard, then running VAD+ASR...")
    async with services.gpu_guard.acquire():
        # Run blocking Whisper/Pyannote inference in a thread so healthz stays responsive.
        result = await loop.run_in_executor(None, _run)
    logger.info("smart_split done: %d segments", len(result.segments))
    return result
