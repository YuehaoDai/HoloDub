from fastapi import APIRouter, Request

from app.models import SeparateRequest, SeparateResponse

router = APIRouter(prefix="/media", tags=["media"])


@router.post("/separate", response_model=SeparateResponse)
async def separate_media(request: Request, payload: SeparateRequest) -> SeparateResponse:
    services = request.app.state.services
    async with services.gpu_guard.acquire():
        return services.separator.separate(payload)
