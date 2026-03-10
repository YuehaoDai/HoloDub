import asyncio
from contextlib import asynccontextmanager


class GPUGuard:
    def __init__(self, concurrency: int = 1) -> None:
        self._semaphore = asyncio.Semaphore(max(concurrency, 1))

    @asynccontextmanager
    async def acquire(self):
        await self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()
