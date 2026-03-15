from fastapi import APIRouter, Request

from app.models import HealthResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
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
