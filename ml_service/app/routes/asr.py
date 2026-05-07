import asyncio
import logging
from functools import partial

from fastapi import APIRouter, HTTPException, Request

from app.models import (
    SmartSplitRequest,
    SmartSplitResponse,
    TranscribeSegmentRequest,
    TranscribeSegmentResponse,
)
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
            hard_max_segment_sec=payload.hard_max_segment_sec,
            close_gap_ms=payload.close_gap_ms,
            speech_spans=speech_spans,
        )
        return SmartSplitResponse(segments=segments, diagnostics=vad_diagnostics + asr_diagnostics)

    logger.info("smart_split acquiring GPU guard, then running VAD+ASR...")
    async with services.gpu_guard.acquire():
        # Run blocking Whisper/Pyannote inference in a thread so healthz stays responsive.
        result = await loop.run_in_executor(None, _run)
    logger.info("smart_split done: %d segments", len(result.segments))
    return result


@router.post("/transcribe_segment", response_model=TranscribeSegmentResponse)
async def transcribe_segment(
    request: Request, payload: TranscribeSegmentRequest
) -> TranscribeSegmentResponse:
    """Re-transcribe a single time window of an existing audio file.

    Used by the segment-review UI's per-segment "重新识别" button so a
    Whisper recognition error on one segment can be corrected without
    rerunning smart_split for the whole job (which would also wipe any
    manual merge / split / time-edit work the user has done).

    The handler intentionally bypasses smart_split's VAD + boundary
    heuristics: the caller already knows the boundaries, and short
    clips would otherwise be rejected by the default min_segment_sec.
    """
    services = request.app.state.services
    audio_path = resolve_data_path(services.settings.data_root, payload.audio_relpath)
    logger.info(
        "transcribe_segment request: audio_relpath=%s window=%d..%dms asr=%s",
        payload.audio_relpath,
        payload.start_ms,
        payload.end_ms,
        services.settings.ml_asr_backend,
    )
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail=f"audio file not found: {audio_path}")
    if payload.end_ms <= payload.start_ms:
        raise HTTPException(status_code=400, detail="end_ms must be greater than start_ms")
    if payload.start_ms < 0:
        raise HTTPException(status_code=400, detail="start_ms must be non-negative")

    loop = asyncio.get_running_loop()

    def _run() -> TranscribeSegmentResponse:
        text, diagnostics = services.asr.transcribe_window(
            audio_path=audio_path,
            source_language=payload.source_language,
            start_ms=payload.start_ms,
            end_ms=payload.end_ms,
        )
        return TranscribeSegmentResponse(text=text, diagnostics=diagnostics)

    async with services.gpu_guard.acquire():
        result = await loop.run_in_executor(None, _run)
    logger.info("transcribe_segment done: text_len=%d", len(result.text))
    return result
