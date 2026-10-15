"""Интеграционный тест UI.
Если FastAPI установлен — идёт через настоящий TestClient (ловит ошибки вроде 'request: Field required').
Если нет — через минимальные заглушки FastAPI (проверяет маршруты, формы и шаблоны)."""
import asyncio
import importlib
import json
import sys
import types
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode

import jinja2

from dgis.config import Config
from dgis.logs import LogBuffer
from dgis.storage import Storage

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    REAL = True
except Exception:
    REAL = False


def _stub_modules():
    class FastAPI:
        def __init__(self, **kw):
            self.routes, self.state = {}, SimpleNamespace()

        def _reg(self, method, path):
            def deco(fn):
                self.routes[(method, path)] = fn
                return fn
            return deco

        def get(self, path, **kw):
            return self._reg("GET", path)

        def post(self, path, **kw):
            return self._reg("POST", path)

        def on_event(self, name):
            return lambda fn: fn

    class Resp:
        def __init__(self, content=None, status_code=200, media_type=None, headers=None):
            self.content, self.status_code, self.headers = content, status_code, headers or {}

    class Redirect(Resp):
        def __init__(self, url, status_code=307):
            super().__init__(None, status_code)
            self.headers = {"location": url}

    class Templates:
        def __init__(self, directory):
            self.env = jinja2.Environment(loader=jinja2.FileSystemLoader(directory), autoescape=True)

        def TemplateResponse(self, request, name, ctx):
            return Resp(self.env.get_template(name).render(**ctx))

    m = {k: types.ModuleType(k) for k in ("fastapi", "fastapi.responses", "fastapi.security", "fastapi.templating")}
    m["fastapi"].FastAPI, m["fastapi"].Depends, m["fastapi"].Request = FastAPI, (lambda x=None: x), object
    m["fastapi"].HTTPException = type("HTTPException", (Exception,), {"__init__": lambda s, *a, **k: None})
    m["fastapi.responses"].HTMLResponse, m["fastapi.responses"].RedirectResponse = Resp, Redirect
    m["fastapi.responses"].StreamingResponse = Resp
    m["fastapi.security"].HTTPBasic = lambda **kw: None
    m["fastapi.security"].HTTPBasicCredentials = object
    m["fastapi.templating"].Jinja2Templates = Templates
    return m


class StubClient:
    def __init__(self, app):
        self.app = app

    def _call(self, method, path, data=None):
        base, _, qs = path.partition("?")
        from urllib.parse import parse_qsl
        q = dict(parse_qsl(qs))
        req = SimpleNamespace(query_params=q)

        async def body():
            return urlencode(data or {}).encode()
        req.body = body
        fn = self.app.routes[(method, base)]
        import inspect
        kw = {}
        for name, p in inspect.signature(fn).parameters.items():
            if name == "request":
                kw["request"] = req
            elif name in q and p.annotation in (int, "int"):
                kw[name] = int(q[name])
            elif name in q:
                kw[name] = q[name]
        r = asyncio.run(fn(**kw))
        text = r.content if isinstance(r.content, str) else "".join(r.content) if r.content is not None else ""
        return SimpleNamespace(status_code=r.status_code, text=text, headers=r.headers)

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, data=None):
        return self._call("POST", path, data)


class TestUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = {}
        if not REAL:
            for name, mod in _stub_modules().items():
                cls._saved[name] = sys.modules.get(name)
                sys.modules[name] = mod
        sys.modules.pop("dgis.ui.app", None)
        cls.mod = importlib.import_module("dgis.ui.app")
        cls.mod.setup_logging = lambda secrets: LogBuffer()      # без файлов логов

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("dgis.ui.app", None)
        for name, old in cls._saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    def setUp(self):
        self.db = Storage(":memory:")
        cfg = Config(presets_path="config/presets.yaml")
        self.app = self.mod.create_app(cfg, self.db)

        async def noop():                    # не запускаем воркеры/сеть
            return None
        self.app.state.runner.run = noop
        if REAL:
            self.c = TestClient(self.app, follow_redirects=False)
        else:
            self.c = StubClient(self.app)

    def post(self, path, data):
        if REAL:
            return self.c.post(path, data=data)
        return self.c.post(path, data)

    def test_pages_render(self):
        for path, needle in [("/", "Стоматологии"), ("/2gis", "Сбор из 2GIS API"), ("/db", "База сайтов"),
                             ("/jobs", "Задачи"), ("/progress", "карточек")]:
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(needle, r.text, path)

    def test_overture_start_preset(self):
        r = self.post("/overture/start", {"city": "Казань", "preset": "dental", "min_confidence": "0.2",
                                          "only_with_site": "on"})
        self.assertEqual(r.status_code, 303)
        job = self.db.recent_jobs("overture")[0]
        p = job["payload"]
        self.assertEqual((p["city"], p["rubric"], p["only_with_site"], p["min_confidence"]),
                         ("Казань", "Стоматологии", True, 0.2))
        self.assertIn("dental", p["terms"])
        self.assertIn("msg=", r.headers["location"])
        # повтор без force: не дублируется
        self.post("/overture/start", {"city": "казань", "preset": "dental", "only_with_site": "on"})
        self.assertEqual(len(self.db.recent_jobs("overture")), 1)

    def test_overture_custom_terms_and_flags(self):
        self.post("/overture/start", {"city": "Уфа", "terms": "auto_repair, шиномонтаж"})
        p = self.db.recent_jobs("overture")[0]["payload"]
        self.assertEqual(p["terms"], ["auto_repair", "шиномонтаж"])
        self.assertFalse(p["only_with_site"])                   # чекбокс не отмечен

    def test_overture_validation(self):
        for data in ({"city": "", "preset": "dental"}, {"city": "Казань"},
                     {"city": "Казань", "preset": "dental", "bbox": "1,2,3"}):
            r = self.post("/overture/start", data)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(self.db.recent_jobs("overture"), [], data)

    def test_2gis_start(self):
        r = self.post("/2gis/start", {"cities": "Казань", "rubrics": "Стоматологии"})
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/2gis"))
        self.assertEqual(self.db.job_counts().get("queued"), 1)

    def test_control_redirect_is_safe(self):
        self.assertEqual(self.post("/pause", {"next": "/2gis"}).headers["location"], "/2gis")
        self.assertEqual(self.post("/pause", {"next": "//evil.com"}).headers["location"], "/")
        self.assertEqual(self.post("/stop", {}).headers["location"], "/")

    def test_db_and_export(self):
        from dgis.sources.base import Card
        from dgis.pipeline.ingest import store_card
        store_card(self.db, Card("1", "Клиника", "", ["+7843"], ["https://a-clinic.ru"]), "Казань", "dental")
        r = self.c.get("/db?q=clinic&status=unchecked")
        self.assertIn("a-clinic.ru", r.text)
        if REAL:
            e = self.c.get("/export?format=jsonl")
            self.assertEqual(json.loads(e.text.splitlines()[0])["domain"], "a-clinic.ru")
            self.assertIn("domain", self.c.get("/export?format=csv").text)
        else:
            self.assertEqual(self.app.routes[("GET", "/export")].__name__, "export")


if __name__ == "__main__":
    unittest.main()
