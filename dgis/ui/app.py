import asyncio
import logging
import secrets
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from ..export import iter_csv, iter_jsonl
from ..logs import setup_logging
from ..pipeline.runner import Runner
from ..sources.overture import load_presets, parse_bbox
from ..storage import Storage

HERE = Path(__file__).parent
PAGE = 50


def _lines(text: str) -> list[str]:
    return [x.strip() for x in text.replace(",", "\n").splitlines() if x.strip()]


def _safe_next(path: str | None) -> str:
    return path if path and path.startswith("/") and not path.startswith("//") else "/"


def create_app(cfg, db: Storage | None = None):
    db = db or Storage(cfg.db_path)
    buf = setup_logging(cfg.secrets)
    runner = Runner(cfg, db)
    presets = load_presets(cfg.presets_path)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    basic = HTTPBasic(auto_error=False)

    def auth(cred: HTTPBasicCredentials = Depends(basic)):
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

    def ensure_running():
        if runner.state == "idle":
            asyncio.create_task(runner.run())

    # ---- Overture (главная)
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request, "overture.html", {
            "nav": "overture", "presets": presets, "recent": db.recent_jobs("overture", 8),
            "msg": request.query_params.get("msg"), "cfg": cfg, **progress_ctx()})

    @app.post("/overture/start")
    async def overture_start(request: Request):
        f = await form(request)
        terms = _lines(f.get("terms", ""))
        rubric = (f.get("rubric") or "").strip() or None
        preset = presets.get(f.get("preset", ""))
        if preset:
            terms, rubric = terms or preset["terms"], rubric or preset["title"]
        try:
            bbox = (f.get("bbox") or "").strip() or None
            if bbox:
                parse_bbox(bbox)
            conf = float(f.get("min_confidence") or 0)
            ok = runner.enqueue_overture(f.get("city", ""), terms, rubric, bool(f.get("only_with_site")),
                                         conf, bbox, force=bool(f.get("force")))
            msg = "Задача поставлена в очередь" if ok else "Такой запрос уже выполнялся — отметьте «повторить»"
        except ValueError as e:
            ok, msg = False, str(e)
        if ok:
            ensure_running()
        return RedirectResponse("/?msg=" + quote(msg), 303)

    # ---- 2GIS API
    @app.get("/2gis", response_class=HTMLResponse)
    async def gis_page(request: Request):
        return templates.TemplateResponse(request, "run.html", {
            "cfg": cfg, "cities": "\n".join(cfg.cities), "rubrics": "\n".join(cfg.rubrics),
            "nav": "2gis", **progress_ctx()})

    @app.post("/2gis/start")
    async def gis_start(request: Request):
        f = await form(request)
        cities, rubrics = _lines(f.get("cities", "")), _lines(f.get("rubrics", ""))
        if cities and rubrics:
            if f.get("concurrency", "").isdigit() and runner.state == "idle":
                cfg.concurrency = max(1, min(int(f["concurrency"]), 10))
            runner.enqueue_collect(cities, rubrics, force=bool(f.get("force")))
        ensure_running()
        return RedirectResponse("/2gis", 303)

    # ---- управление
    @app.get("/progress", response_class=HTMLResponse)
    async def progress(request: Request):
        return templates.TemplateResponse(request, "progress.html", progress_ctx())

    async def control(request: Request, action):
        f = await form(request)
        action()
        return RedirectResponse(_safe_next(f.get("next")), 303)

    @app.post("/pause")
    async def pause(request: Request):
        return await control(request, runner.pause)

    @app.post("/resume")
    async def resume(request: Request):
        return await control(request, runner.resume)

    @app.post("/stop")
    async def stop(request: Request):
        return await control(request, runner.stop)

    # ---- база и экспорт
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
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in f.items() if v)
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

    # ---- задачи
    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs(request: Request):
        return templates.TemplateResponse(request, "jobs.html", {
            "nav": "jobs", "counts": db.job_counts(), "failed": db.failed_jobs(), "state": runner.state})

    @app.post("/jobs/retry")
    async def jobs_retry():
        db.retry_failed()
        ensure_running()
        return RedirectResponse("/jobs", 303)

    @app.on_event("startup")
    async def _resume():
        # продолжить прерванное: если в очереди что-то осталось, поднимаем воркеры
        if db.requeue_running() or db.active_jobs():
            logging.getLogger("dgis").info("В очереди есть задачи — продолжаем")
            asyncio.create_task(runner.run())

    return app
