from fastapi import APIRouter, Request

from app.models import TTSRequest, TTSResponse

router = APIRouter(prefix="/tts", tags=["tts"])


@router.post("/run", response_model=TTSResponse)
async def run_tts(request: Request, payload: TTSRequest) -> TTSResponse:
    services = request.app.state.services
    async with services.gpu_guard.acquire():
        return services.tts.synthesize(payload)
