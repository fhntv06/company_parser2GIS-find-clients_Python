import asyncio


class RateLimiter:
    """Глобальный лимит запросов в секунду + принудительная пауза (block)."""

    def __init__(self, rps: float):
        self.interval = 1.0 / max(rps, 0.01)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            start = max(now, self._next)
            self._next = start + self.interval
        wait = start - now
        if wait > 0:
            await asyncio.sleep(wait)

    def block(self, seconds: float) -> None:
        """Остановить все запросы на N секунд (при 429/403)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._next = max(self._next, loop.time() + seconds)
