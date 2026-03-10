from fastapi import FastAPI

from app.config import get_settings
from app.routes.asr import router as asr_router
from app.routes.health import router as health_router
from app.routes.media import router as media_router
from app.routes.tts import router as tts_router
from app.runtime import ServiceContainer


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="HoloDub ML Service", version="0.1.0")
    app.state.services = ServiceContainer(settings)

    app.include_router(health_router)
    app.include_router(media_router)
    app.include_router(asr_router)
    app.include_router(tts_router)
    return app


app = create_app()
