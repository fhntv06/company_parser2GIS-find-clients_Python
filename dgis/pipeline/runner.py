from __future__ import annotations

import asyncio
import logging

from .checker import check_url, is_transient, make_client
from ..config import Config
from .errors import Defer, RetryLater
from .limiter import RateLimiter
from .ingest import store_card
from ..normalize import site_note
from ..sources.base import Card
from ..sources.csv_import import import_csv
from ..sources.overture import OvertureOps, csv_path
from ..storage import Storage

log = logging.getLogger("dgis")
CHECK_RETRIES = 2          # сколько раз откладывать проверку сайта при временном сбое
CHECK_RETRY_BASE = 20      # пауза перед повтором, секунд (20, 60, ...)


class _NoSource:
    async def search(self, *a, **k):
        raise RuntimeError("DGIS_KEY не задан в .env: сбор из 2GIS недоступен (импорт и проверка работают)")


class Runner:
    """Один раннер на CLI и UI: очередь jobs + воркеры. Состояния: idle | running | paused."""

    def __init__(self, cfg: Config, db: Storage, source=None, checker=None, overture=None):
        self.cfg, self.db, self.overture = cfg, db, overture
        self._heavy = None
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

    def enqueue_overture(self, city: str, terms: list[str], rubric: str | None = None, only_with_site: bool = True,
                         min_confidence: float = 0.0, bbox: str | None = None, force: bool = False) -> bool:
        terms = [t.strip().lower() for t in terms if t.strip()]
        if not city.strip() or not terms:
            raise ValueError("Нужны город и тема (подстроки категории)")
        payload = {"city": city.strip(), "terms": terms, "rubric": rubric, "only_with_site": only_with_site,
                   "min_confidence": min_confidence, "bbox": bbox or None}
        uniq = f"overture:{city.strip().lower()}:{','.join(sorted(terms))}:{bbox or ''}"
        ok = self.db.enqueue("overture", payload, uniq, priority=1, force=force, max_attempts=2)
        log.info("Overture: %s / %s -> %s", city, ",".join(terms), "в очереди" if ok else "уже выполнялась (нужен force)")
        return ok

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
            from ..sources.api2gis import ApiSource

            if self.cfg.source != "api":
                raise RuntimeError("source=%s пока не реализован (v2)" % self.cfg.source)
            c = httpx.AsyncClient(timeout=20, proxy=self.cfg.proxy)
            self._clients.append(c)
            self.source = ApiSource(self.cfg.dgis_key, c, self.cfg.page_size)
        if self.checker is None:
            c2 = make_client(self.cfg.check_timeout, self.cfg.check_concurrency * 2, self.cfg.proxy)
            self._clients.append(c2)
            self.checker = lambda url: check_url(c2, url)

    async def run(self) -> None:
        if self.state != "idle":
            return
        self.state, self._stop = "running", False
        self.db.requeue_running()
        self._heavy = asyncio.Lock()
        tasks: list[asyncio.Task] = []
        try:
            await self._build_clients()
            for i in range(self.cfg.concurrency):
                tasks.append(asyncio.create_task(self._worker(f"c{i}", ("collect_list", "overture"))))
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
                handler = {"collect_list": self._collect, "overture": self._overture,
                           "check_site": self._check}[job["type"]]
                await handler({**job["payload"], "_attempt": job["attempts"]})
                self.db.finish(job["id"])
            except asyncio.CancelledError:
                raise
            except Defer as e:
                st = self.db.retry(job, str(e), e.delay)
                log.info("Повтор проверки позже (%s): %s", st, e)
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

    async def _overture(self, p: dict) -> None:
        """Город -> рамка -> скачивание -> фильтр по теме -> CSV -> импорт. Тяжёлые шаги идут по одному."""
        ops = self.overture = self.overture or OvertureOps(self.cfg, self.db)
        bbox = await asyncio.to_thread(ops.resolve_bbox, p["city"], p.get("bbox"))
        log.info("Overture %s: рамка %s", p["city"], ",".join(f"{x:.3f}" for x in bbox))
        out = csv_path(self.cfg.overture_dir, p["city"], p["terms"])
        out.parent.mkdir(parents=True, exist_ok=True)
        async with self._heavy:
            log.info("Overture %s: загрузка данных (может занять несколько минут)...", p["city"])
            parquet = await asyncio.to_thread(ops.fetch, bbox)
            info = await asyncio.to_thread(ops.to_csv, parquet, out, p["terms"],
                                           p.get("only_with_site", True), p.get("min_confidence", 0.0))
        log.info("Overture %s: по теме найдено мест: %d", p["city"], info["rows"])
        if info["rows"] == 0:
            return
        res = await asyncio.to_thread(import_csv, self.db, str(out), p["city"], p.get("rubric"), "ovt")
        log.info("Overture %s: импортировано, новых доменов %d", p["city"], res["new_sites"])

    async def _check(self, p: dict) -> None:
        res = await self.checker(p["url"])
        status, final = res[0], res[1]
        err = res[2] if len(res) > 2 else None
        attempt = p.get("_attempt", 0)
        if status == 0 and is_transient(err) and attempt < CHECK_RETRIES:
            raise Defer(CHECK_RETRY_BASE * 3 ** attempt, f"{p['domain']}: {err}")
        self.db.set_site_check(p["domain"], status, final, site_note(p["domain"], final, status, err))
