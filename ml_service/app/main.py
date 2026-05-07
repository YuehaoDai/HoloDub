import logging
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

from app.observability import (  # noqa: E402
    RequestIDMiddleware,
    install_log_filter,
    metrics_response,
    set_warmup_status,
)
from app.routes.admin import router as admin_router  # noqa: E402
from app.routes.asr import router as asr_router  # noqa: E402
from app.routes.health import router as health_router  # noqa: E402
from app.routes.media import router as media_router  # noqa: E402
from app.routes.tts import router as tts_router  # noqa: E402
from app.runtime import ServiceContainer  # noqa: E402


WARMUP_HEARTBEAT_SEC = 30.0
WARMUP_HARD_TIMEOUT_SEC = 30 * 60.0


def _flush_logging() -> None:
    """Force flush every logging handler.

    Daemon threads do not run ``logging.shutdown`` at process exit; if a
    background warm-up thread terminates abruptly any buffered log
    records are silently dropped, which makes failures invisible. We
    flush proactively at every important transition so operators never
    have to guess whether silence means "still working" or "thread
    crashed".
    """
    for handler in list(logging.getLogger().handlers):
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.flush()
    sys.stderr.flush()


def _spawn_indextts2_warmup(services: ServiceContainer) -> None:
    """Start the IndexTTS2 warm-up thread plus a watchdog companion.

    The warm-up itself runs as a daemon thread so that a never-ending
    load never blocks the asyncio loop or the orchestrator's liveness
    probe. The watchdog runs in a *separate* daemon thread and is
    responsible for two failure modes that the warm-up thread cannot
    catch by itself:

      1. The warm-up thread terminates abruptly (segfault in a CUDA
         extension, OS-level kill of a child process, or any path that
         escapes the ``try/except`` block) leaving the warm-up status
         stuck on ``loading`` forever. The watchdog flips it to
         ``error`` once it observes the thread is gone but the status
         was never resolved.
      2. The warm-up runs longer than ``WARMUP_HARD_TIMEOUT_SEC``. We do
         not abort the inflight load (Python cannot safely kill a
         thread that owns CUDA state), but we do publish a hard error
         so ``/readyz`` stops returning 503 indefinitely and operators
         see a clear signal.

    The watchdog also emits a heartbeat log every
    ``WARMUP_HEARTBEAT_SEC`` so the absence of progress is observable
    in `docker logs` even when the underlying loader is silent.
    """

    def _warm() -> None:
        try:
            services.tts.warm_up_indextts2()
            final = services.tts.get_indextts2_warmup_status()
            set_warmup_status(final)
            logger.info("IndexTTS2 warm-up complete: status=%s", final)
        except Exception as exc:  # noqa: BLE001
            set_warmup_status("error")
            logger.exception("IndexTTS2 warm-up raised: %s", exc)
        finally:
            # If the inner ``warm_up_indextts2`` ever returns without
            # transitioning the adapter status away from "loading", we
            # would otherwise leave callers waiting forever. Force the
            # status to "error" in that pathological case so /readyz
            # gives a deterministic 503 rather than hanging clients.
            if services.tts.get_indextts2_warmup_status() == "loading":
                services.tts.force_indextts2_load_error(
                    "warm-up thread returned without resolving status"
                )
                set_warmup_status("error")
            _flush_logging()

    warm_thread = threading.Thread(
        target=_warm, daemon=True, name="indextts2-warmup"
    )
    warm_thread.start()

    def _watchdog() -> None:
        start = time.monotonic()
        while True:
            time.sleep(WARMUP_HEARTBEAT_SEC)
            elapsed = time.monotonic() - start
            status = services.tts.get_indextts2_warmup_status()
            alive = warm_thread.is_alive()

            if status in ("ready", "error"):
                logger.info(
                    "IndexTTS2 warm-up watchdog stopping (final status=%s after %.1fs)",
                    status,
                    elapsed,
                )
                _flush_logging()
                return

            if not alive:
                services.tts.force_indextts2_load_error(
                    f"warm-up thread vanished after {elapsed:.1f}s while status={status}"
                )
                set_warmup_status("error")
                _flush_logging()
                return

            if elapsed > WARMUP_HARD_TIMEOUT_SEC:
                services.tts.force_indextts2_load_error(
                    f"warm-up exceeded hard timeout {WARMUP_HARD_TIMEOUT_SEC:.0f}s "
                    f"(status={status}); thread may still hold GPU memory, "
                    "restart the container to recover"
                )
                set_warmup_status("error")
                _flush_logging()
                return

            logger.info(
                "IndexTTS2 warm-up still in progress (elapsed=%.1fs status=%s)",
                elapsed,
                status,
            )
            _flush_logging()

    threading.Thread(
        target=_watchdog, daemon=True, name="indextts2-warmup-watchdog"
    ).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    services = app.state.services

    # IndexTTS2 inline mode: kick off the load in a *background* thread
    # so the asyncio loop stays free during the multi-minute
    # initialisation (otherwise /healthz times out and orchestrators
    # mark the pod unhealthy before warm-up finishes). The actual
    # loader is a single ``ensure_indextts2_loaded()`` path guarded by
    # a threading.Lock, so the first user-triggered TTS request will
    # not race with this background warm-up — it will simply observe
    # the cached model.
    #
    # /readyz reads ``tts_warmup_status`` so callers can tell when it
    # is safe to send traffic; the Prometheus
    # ``holodub_ml_tts_warmup_status`` gauge mirrors the same state for
    # dashboards. A watchdog thread guarantees the status will always
    # converge to ``ready`` or ``error`` even if the load thread dies
    # abruptly or hangs indefinitely.
    if services.tts.is_indextts2_inline_enabled():
        logger.info("warming up IndexTTS2 (inline) in background thread...")
        set_warmup_status("loading")
        _flush_logging()
        _spawn_indextts2_warmup(services)
    yield


def create_app() -> FastAPI:
    install_log_filter()
    settings = get_settings()
    app = FastAPI(title="HoloDub ML Service", version="0.1.0", lifespan=lifespan)
    app.state.services = ServiceContainer(settings)

    app.add_middleware(RequestIDMiddleware)
    app.include_router(health_router)
    app.include_router(media_router)
    app.include_router(asr_router)
    app.include_router(tts_router)
    app.include_router(admin_router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics():  # type: ignore[no-redef]
        return metrics_response()

    return app


app = create_app()
