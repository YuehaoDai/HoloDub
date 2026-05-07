from fastapi import APIRouter, HTTPException, Request

from app.models import HealthResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    """Liveness probe.

    Returns 200 as long as the FastAPI event loop can answer a request,
    even while large models are still loading. Orchestrators should use
    this to avoid premature container restarts during multi-minute
    warm-up of IndexTTS2 / pyannote / whisper.
    """
    services = request.app.state.services
    warnings: list[str] = []
    manifest = services.model_manifest.read()
    if not manifest:
        warnings.append("model manifest not found")
    tts_warmup = (
        services.tts.get_indextts2_warmup_status()
        if services.tts.backend_name() == "indextts2"
        else "idle"
    )
    return HealthResponse(
        status="ok",
        adapters={
            "separator": services.separator.backend_name(),
            "asr": services.asr.backend_name(),
            "vad": services.vad.backend_name(),
            "tts": services.tts.backend_name(),
        },
        loaded_models=services.registry.status(),
        model_manifest=manifest,
        warnings=warnings,
        tts_warmup_status=tts_warmup,
    )


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    """Readiness probe.

    Returns 200 only when the service is actually able to serve work.
    Currently the only blocking dependency is the IndexTTS2 inline
    warm-up (when configured); ASR / VAD models are loaded lazily on
    first use through ``ModelRegistry`` so they do not gate readiness.

    Status mapping:
      - ``idle``    -> 200 (TTS backend is not indextts2-inline; nothing to wait for)
      - ``ready``   -> 200 (model fully loaded)
      - ``loading`` -> 503 (warm-up still in progress)
      - ``error``   -> 503 (warm-up failed; manual intervention required)
    """
    services = request.app.state.services
    if services.tts.backend_name() == "indextts2" and services.tts.is_indextts2_inline_enabled():
        status = services.tts.get_indextts2_warmup_status()
    else:
        status = "idle"

    payload = {
        "ready": status in ("idle", "ready"),
        "tts_warmup_status": status,
    }
    if status in ("idle", "ready"):
        return payload
    raise HTTPException(status_code=503, detail=payload)
