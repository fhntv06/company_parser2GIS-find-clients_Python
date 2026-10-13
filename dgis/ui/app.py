from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from urllib.parse import parse_qs

from ..export import iter_csv, iter_jsonl
from ..logs import setup_logging
from ..runner import Runner
from ..storage import Storage

HERE = Path(__file__).parent
PAGE = 50


def _lines(text: str) -> list[str]:
    return [x.strip() for x in text.replace(",", "\n").splitlines() if x.strip()]


def create_app(cfg, db: Storage | None = None):
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
    from fastapi.security import HTTPBasic, HTTPBasicCredentials
    from fastapi.templating import Jinja2Templates

    db = db or Storage(cfg.db_path)
    buf = setup_logging(cfg.secrets)
    runner = Runner(cfg, db)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    basic = HTTPBasic(auto_error=False)

    def auth(cred: HTTPBasicCredentials | None = Depends(basic)):
        if not cfg.ui_user:
            return
        ok = cred and secrets.compare_digest(cred.username, cfg.ui_user) \
            and secrets.compare_digest(cred.password, cfg.ui_password)
        if not ok:
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    app = FastAPI(title="2GIS parser", dependencies=[Depends(auth)])
    app.state.runner = runner

    def progress_ctx():
        return {"s": db.stats(), "state": runner.state, "log": buf.tail(50)}

    async def form(request: Request) -> dict:
        raw = parse_qs((await request.body()).decode("utf-8"))
        return {k: v[0] for k, v in raw.items()}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request, "run.html", {
            "cfg": cfg, "cities": "\n".join(cfg.cities), "rubrics": "\n".join(cfg.rubrics),
            "nav": "run", **progress_ctx()})

    @app.get("/progress", response_class=HTMLResponse)
    async def progress(request: Request):
        return templates.TemplateResponse(request, "progress.html", progress_ctx())

    @app.post("/start")
    async def start(request: Request):
        f = await form(request)
        cities, rubrics = _lines(f.get("cities", "")), _lines(f.get("rubrics", ""))
        if cities and rubrics:
            if f.get("concurrency", "").isdigit() and runner.state == "idle":
                cfg.concurrency = max(1, min(int(f["concurrency"]), 10))
            runner.enqueue_collect(cities, rubrics, force=bool(f.get("force")))
        if runner.state == "idle":
            asyncio.create_task(runner.run())
        return RedirectResponse("/", 303)

    @app.post("/pause")
    async def pause():
        runner.pause(); return RedirectResponse("/", 303)

    @app.post("/resume")
    async def resume():
        runner.resume(); return RedirectResponse("/", 303)

    @app.post("/stop")
    async def stop():
        runner.stop(); return RedirectResponse("/", 303)

    def filters(request: Request) -> dict:
        q = request.query_params
        return {k: q.get(k) or None for k in ("city", "rubric", "status", "analysis", "q")}

    @app.get("/db", response_class=HTMLResponse)
    async def db_page(request: Request, page: int = 1, sort: str = "domain", desc: int = 0):
        f = filters(request)
        total = db.count_sites(f)
        pages = max(1, -(-total // PAGE))
        page = min(max(page, 1), pages)
        rows = db.list_sites(f, PAGE, (page - 1) * PAGE, sort, bool(desc))
        qs = "&".join(f"{k}={v}" for k, v in f.items() if v)
        return templates.TemplateResponse(request, "db.html", {
            "nav": "db", "rows": rows, "total": total, "page": page, "pages": pages,
            "f": f, "qs": qs, "sort": sort, "desc": desc, **db.filter_values()})

    @app.get("/export")
    async def export(request: Request, format: str = "jsonl"):
        f = filters(request)
        if format == "csv":
            return StreamingResponse(iter_csv(db, f), media_type="text/csv; charset=utf-8",
                                     headers={"Content-Disposition": "attachment; filename=sites.csv"})
        return StreamingResponse(iter_jsonl(db, f), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": "attachment; filename=sites.jsonl"})

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs(request: Request):
        return templates.TemplateResponse(request, "jobs.html", {
            "nav": "jobs", "counts": db.job_counts(), "failed": db.failed_jobs(), "state": runner.state})

    @app.post("/jobs/retry")
    async def jobs_retry():
        db.retry_failed()
        if runner.state == "idle":
            asyncio.create_task(runner.run())
        return RedirectResponse("/jobs", 303)

    @app.on_event("startup")
    async def _resume():
        # продолжить прерванное: если в очереди что-то осталось, поднимаем воркеры
        if db.requeue_running() or db.active_jobs():
            logging.getLogger("dgis").info("В очереди есть задачи — продолжаем")
            asyncio.create_task(runner.run())

    return app
