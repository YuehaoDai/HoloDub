from fastapi import APIRouter, Request

from app.models import SmartSplitRequest, SmartSplitResponse
from app.storage import resolve_data_path

router = APIRouter(prefix="/asr", tags=["asr"])


@router.post("/smart_split", response_model=SmartSplitResponse)
async def smart_split(request: Request, payload: SmartSplitRequest) -> SmartSplitResponse:
    services = request.app.state.services
    audio_path = resolve_data_path(services.settings.data_root, payload.audio_relpath)
    async with services.gpu_guard.acquire():
        speech_spans, vad_diagnostics = services.vad.analyze(audio_path)
        segments, asr_diagnostics = services.asr.smart_split(
            audio_path=audio_path,
            source_language=payload.source_language,
            min_segment_sec=payload.min_segment_sec,
            max_segment_sec=payload.max_segment_sec,
            speech_spans=speech_spans,
        )
    return SmartSplitResponse(segments=segments, diagnostics=vad_diagnostics + asr_diagnostics)
