"""Admin endpoints for the ml-service.

These are NOT exposed through the user-facing /jobs API. They are intended
for operators investigating GPU memory issues and for the future control
plane to react to capacity pressure (e.g. evict the IndexTTS2 model when
no TTS work has been seen for several minutes).

Routes:
  GET  /admin/models          - list currently resident models
  POST /admin/models/unload   - drop a specific model from the registry
  POST /admin/models/clear    - drop every cached model
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/models", tags=["admin"])


@router.get("")
async def list_models(request: Request) -> dict:
    services = request.app.state.services
    return {
        "loaded": services.registry.status(),
        "max_models": services.registry.max_models,
    }


@router.post("/unload")
async def unload_model(request: Request, key: str) -> dict:
    services = request.app.state.services
    removed = services.registry.unload(key)
    if not removed:
        raise HTTPException(status_code=404, detail=f"model {key!r} not loaded")
    return {"unloaded": key}


@router.post("/clear")
async def clear_models(request: Request) -> dict:
    services = request.app.state.services
    count = services.registry.clear()
    return {"cleared": count}
