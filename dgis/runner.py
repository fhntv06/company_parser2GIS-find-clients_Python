from __future__ import annotations

import asyncio
import logging

from .checker import check_url
from .config import Config
from .errors import RetryLater
from .limiter import RateLimiter
from .ingest import store_card
from .normalize import site_note
from .sources.base import Card
from .storage import Storage

log = logging.getLogger("dgis")


class _NoSource:
    async def search(self, *a, **k):
        raise RuntimeError("DGIS_KEY не задан в .env: сбор из 2GIS недоступен (импорт и проверка работают)")


class Runner:
    """Один раннер на CLI и UI: очередь jobs + воркеры. Состояния: idle | running | paused."""

    def __init__(self, cfg: Config, db: Storage, source=None, checker=None):
        self.cfg, self.db = cfg, db
        self.source, self.checker = source, checker
        self.limiter = RateLimiter(cfg.rate_limit_rps)
        self.state = "idle"
        self._stop = False
        self._clients: list = []

    # ---- управление
    def enqueue_collect(self, cities: list[str], rubrics: list[str], force: bool = False) -> int:
        n = 0
        for c in cities:
            for r in rubrics:
                if self.db.enqueue("collect_list", {"city": c, "rubric": r, "page": 1},
                                   uniq=f"collect:{c}:{r}:1", priority=1, force=force):
                    n += 1
        log.info("В очередь добавлено задач сбора: %d", n)
        return n

    def pause(self) -> None:
        if self.state == "running":
            self.state = "paused"; log.info("Пауза")

    def resume(self) -> None:
        if self.state == "paused":
            self.state = "running"; log.info("Продолжаем")

    def stop(self) -> None:
        self._stop = True
        log.info("Остановка...")

    # ---- запуск
    async def _build_clients(self) -> None:
        if self.source is None and not self.cfg.dgis_key:
            self.source = _NoSource()      # импорт из файла: ключ 2GIS не нужен
        if self.source is None:
            import httpx
            from .sources.api import ApiSource

            if self.cfg.source != "api":
                raise RuntimeError("source=%s пока не реализован (v2)" % self.cfg.source)
            c = httpx.AsyncClient(timeout=20, proxy=self.cfg.proxy)
            self._clients.append(c)
            self.source = ApiSource(self.cfg.dgis_key, c, self.cfg.page_size)
        if self.checker is None:
            import httpx

            c2 = httpx.AsyncClient(timeout=self.cfg.check_timeout, follow_redirects=True,
                                   max_redirects=5, verify=False, proxy=self.cfg.proxy,
                                   limits=httpx.Limits(max_connections=self.cfg.check_concurrency * 2))
            self._clients.append(c2)
            self.checker = lambda url: check_url(c2, url)

    async def run(self) -> None:
        if self.state != "idle":
            return
        self.state, self._stop = "running", False
        self.db.requeue_running()
        tasks: list[asyncio.Task] = []
        try:
            await self._build_clients()
            for i in range(self.cfg.concurrency):
                tasks.append(asyncio.create_task(self._worker(f"c{i}", ("collect_list",))))
            for i in range(self.cfg.check_concurrency):
                tasks.append(asyncio.create_task(self._worker(f"k{i}", ("check_site",))))
            log.info("Старт: collect=%d, check=%d воркеров", self.cfg.concurrency, self.cfg.check_concurrency)
            while not self._stop and self.db.active_jobs() > 0:
                await asyncio.sleep(0.3)
            log.info("Очередь пуста" if not self._stop else "Остановлено")
        except Exception as e:
            log.error("Ошибка запуска: %s", e)
        finally:
            self._stop = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.db.requeue_running()
            for c in self._clients:
                await c.aclose()
            self._clients.clear()
            self.state = "idle"

    # ---- воркеры
    async def _worker(self, name: str, types: tuple[str, ...]) -> None:
        while not self._stop:
            if self.state == "paused":
                await asyncio.sleep(0.5); continue
            job = self.db.claim(types, name)
            if not job:
                await asyncio.sleep(0.3); continue
            try:
                if job["type"] == "collect_list":
                    await self._collect(job["payload"])
                else:
                    await self._check(job["payload"])
                self.db.finish(job["id"])
            except asyncio.CancelledError:
                raise
            except RetryLater as e:
                self.limiter.block(e.delay)
                st = self.db.retry(job, str(e), e.delay)
                log.warning("Лимит/блокировка: пауза %ss (%s) -> %s", e.delay, e, st)
            except Exception as e:
                delay = min(5 * 2 ** job["attempts"], 300)
                st = self.db.retry(job, str(e), delay)
                log.warning("Ошибка задачи #%s %s: %s -> %s", job["id"], job["type"], e, st)

    async def _collect(self, p: dict) -> None:
        page = p.get("page", 1)
        await self.limiter.acquire()
        res = await self.source.search(p["city"], p["rubric"], page)
        for card in res.items:
            self._store_card(card, p["city"], p["rubric"])
        log.info("%s / %s, стр.%d: карточек %d (всего по запросу %d)",
                 p["city"], p["rubric"], page, len(res.items), res.total)
        if res.items and page * self.cfg.page_size < res.total and page < self.cfg.max_pages:
            nxt = {**p, "page": page + 1}
            self.db.enqueue("collect_list", nxt, uniq=f"collect:{p['city']}:{p['rubric']}:{page + 1}", priority=1)

    def _store_card(self, card: Card, city: str, rubric: str) -> None:
        store_card(self.db, card, city, rubric)

    async def _check(self, p: dict) -> None:
        status, final = await self.checker(p["url"])
        self.db.set_site_check(p["domain"], status, final, site_note(p["domain"], final))
