import asyncio
import json
import time
import unittest
from pathlib import Path

from dgis.config import Config
from dgis.pipeline.errors import RetryLater
from dgis.export import iter_csv, iter_jsonl
from dgis.pipeline.limiter import RateLimiter
from dgis.normalize import best_kind, normalize_site
from dgis.pipeline.runner import Runner
from dgis.sources.api2gis import parse_item
from dgis.sources.base import Card, Page
from dgis.storage import Storage


class TestNormalize(unittest.TestCase):
    def test_own(self):
        s = normalize_site("http://www.Example.ru/contacts?utm_source=2gis&a=1#x")
        self.assertEqual((s.domain, s.url, s.kind), ("example.ru", "https://example.ru/", "own"))

    def test_no_scheme(self):
        self.assertEqual(normalize_site("clinic24.ru").domain, "clinic24.ru")

    def test_social_market(self):
        self.assertEqual(normalize_site("https://vk.com/club1").kind, "social")
        self.assertEqual(normalize_site("https://m.vk.com/x").kind, "social")
        self.assertEqual(normalize_site("https://www.avito.ru/x").kind, "marketplace")
        self.assertEqual(normalize_site("https://notvk.com/").kind, "own")

    def test_junk(self):
        for bad in (None, "", "tel:+7900", "mailto:a@b.ru", "localhost", "http://link.2gis.ru/abc"):
            self.assertIsNone(normalize_site(bad), bad)

    def test_idn(self):
        s = normalize_site("https://стоматология.рф/о-нас")
        self.assertEqual(s.domain, "стоматология.рф")
        self.assertTrue(s.url.startswith("https://xn--"))

    def test_best_kind(self):
        self.assertEqual(best_kind(["social", "own"]), "own")
        self.assertEqual(best_kind([]), "none")


class TestParse(unittest.TestCase):
    def test_item(self):
        c = parse_item({"id": "1", "name": "A", "address_name": "ул. 1",
                        "rubrics": [{"name": "Стоматологии"}],
                        "contact_groups": [{"contacts": [
                            {"type": "phone", "value": "+78430000000"},
                            {"type": "website", "value": "http://a.ru", "url": "https://link.2gis.ru/x"},
                            {"type": "email", "value": "a@a.ru"}]}]})
        self.assertEqual((c.websites, c.phones), (["http://a.ru"], ["+78430000000"]))

    def test_real_2gis_sample(self):
        """Реальный ответ 2GIS: value = редирект link.2gis.ru, чистый адрес в url."""
        r = "http://link.2gis.ru/1.2/76CC/webapi/2026/project32/700/null/abc?"
        c = parse_item({"id": "70000001103939664", "name": "Мосцветок", "contact_groups": [
            {"contacts": [
                {"url": "http://moscvetok.ru", "text": "moscvetok.ru", "type": "website", "value": r + "http://moscvetok.ru"},
                {"url": "http://jivo.chat/Uz6xucBwLQ", "text": "Онлайн-чат с компанией", "type": "website",
                 "value": r + "http://jivo.chat/Uz6xucBwLQ"}]},
            {"contacts": [{"text": "+7‒936‒266‒82‒14", "type": "phone", "value": "+79362668214"}]}]})
        self.assertEqual(c.websites, ["http://moscvetok.ru", "http://jivo.chat/Uz6xucBwLQ"])
        self.assertEqual(c.phones, ["+79362668214"])
        kinds = [normalize_site(u).kind for u in c.websites]
        self.assertEqual(kinds, ["own", "marketplace"])   # чат не считается собственным сайтом

    def test_value_only_redirect(self):
        c = parse_item({"id": "1", "contact_groups": [{"contacts": [
            {"type": "website", "value": "http://link.2gis.ru/1.2/AA/x/y?http://a.ru"}]}]})
        self.assertEqual(c.websites, ["http://a.ru"])

    def test_empty(self):
        self.assertEqual(parse_item({"id": 5}).websites, [])


class TestQueue(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")

    def test_unique_and_force(self):
        self.assertTrue(self.db.enqueue("collect_list", {}, "u1"))
        self.assertFalse(self.db.enqueue("collect_list", {}, "u1"))
        j = self.db.claim(("collect_list",), "w")
        self.db.finish(j["id"])
        self.assertFalse(self.db.enqueue("collect_list", {}, "u1"))
        self.assertTrue(self.db.enqueue("collect_list", {}, "u1", force=True))

    def test_claim_once_and_type(self):
        self.db.enqueue("check_site", {"a": 1}, "c1")
        self.assertIsNone(self.db.claim(("collect_list",), "w"))
        self.assertIsNotNone(self.db.claim(("check_site",), "w"))
        self.assertIsNone(self.db.claim(("check_site",), "w2"))

    def test_retry_and_fail(self):
        self.db.enqueue("collect_list", {}, "u")
        for i in range(5):
            self.db.exec("UPDATE jobs SET run_after='2000-01-01T00:00:00' WHERE status='queued'")
            j = self.db.claim(("collect_list",), "w")
            st = self.db.retry(j, "boom", 0)
        self.assertEqual(st, "failed")
        self.assertEqual(self.db.retry_failed(), 1)

    def test_requeue_running(self):
        self.db.enqueue("collect_list", {}, "u")
        self.db.claim(("collect_list",), "w")
        self.assertEqual(self.db.requeue_running(), 1)
        self.assertEqual(self.db.active_jobs(), 1)


class TestLimiter(unittest.TestCase):
    def test_rate(self):
        async def go():
            lim = RateLimiter(20)  # 50 мс между запросами
            t = time.monotonic()
            await asyncio.gather(*(lim.acquire() for _ in range(5)))
            return time.monotonic() - t
        self.assertGreaterEqual(asyncio.run(go()), 0.19)

    def test_block(self):
        async def go():
            lim = RateLimiter(1000)
            lim.block(0.2)
            t = time.monotonic()
            await lim.acquire()
            return time.monotonic() - t
        self.assertGreaterEqual(asyncio.run(go()), 0.15)


def make_cfg(**kw):
    return Config(concurrency=3, check_concurrency=5, rate_limit_rps=1000, page_size=2, **kw)


class FakeSource:
    """3 страницы по 2 карточки; на 2-й странице один раз 429."""
    def __init__(self):
        self.calls, self.blocked_once = 0, False
        self.cards = [
            Card("1", "Клиника А", "a", ["+7843"], ["http://www.a-clinic.ru/?utm_x=1"]),
            Card("2", "Клиника Б (сеть)", "b", [], ["https://a-clinic.ru/filial"]),   # дубль домена
            Card("3", "ВК-салон", "c", [], ["https://vk.com/club9"]),
            Card("4", "Без сайта", "d", ["+7"], []),
            Card("5", "Мёртвый", "e", [], ["http://dead-site.ru"]),
        ]

    async def search(self, city, rubric, page):
        self.calls += 1
        if page == 2 and not self.blocked_once:
            self.blocked_once = True
            raise RetryLater(0.1, "429")
        return Page(len(self.cards), self.cards[(page - 1) * 2: page * 2])


async def fake_checker(url):
    return (0, None) if "dead-site" in url else (200, url)


class TestPipeline(unittest.TestCase):
    def test_end_to_end_and_resume(self):
        db = Storage(":memory:")
        src = FakeSource()
        r = Runner(make_cfg(), db, source=src, checker=fake_checker)
        r.enqueue_collect(["Казань"], ["Стоматологии"])
        asyncio.run(r.run())
        s = db.stats()
        self.assertEqual(s["cards"], 5)
        self.assertEqual((s["own"], s["social"], s["none"]), (3, 1, 1))
        self.assertEqual(s["sites"], 2)            # a-clinic.ru дедуплицирован
        self.assertEqual(s["sites_ok"], 1)         # dead-site.ru -> status 0
        self.assertEqual(s["sites_unchecked"], 0)
        self.assertEqual(db.job_counts().get("failed", 0), 0)
        self.assertEqual(r.state, "idle")

        # повторный запуск: ничего не качает заново
        calls = src.calls
        self.assertEqual(r.enqueue_collect(["Казань"], ["Стоматологии"]), 0)
        asyncio.run(r.run())
        self.assertEqual(src.calls, calls)
        self.assertEqual(db.stats()["cards"], 5)

        # экспорт
        rows = [json.loads(x) for x in iter_jsonl(db, {})]
        self.assertEqual([x["domain"] for x in rows], ["a-clinic.ru", "dead-site.ru"])
        self.assertEqual(sorted(rows[0]["business_names"]), ["Клиника А", "Клиника Б (сеть)"])
        self.assertEqual(rows[0]["phones"], ["+7843"])
        self.assertEqual([x["domain"] for x in map(json.loads, iter_jsonl(db, {"status": "ok"}))], ["a-clinic.ru"])
        csv_lines = "".join(iter_csv(db, {})).splitlines()
        self.assertEqual(len(csv_lines), 3)

        # выборки для UI
        self.assertEqual(db.count_sites({"q": "clinic"}), 1)
        self.assertEqual(db.list_sites({}, 10, 0, "cards", True)[0]["cards"], 2)

    def test_interrupt_resume(self):
        """Остановка посреди сбора и продолжение без дублей."""
        db = Storage(":memory:")
        src = FakeSource(); src.blocked_once = True
        r = Runner(make_cfg(), db, source=src, checker=fake_checker)
        r.enqueue_collect(["Казань"], ["Стоматологии"])

        async def go():
            t = asyncio.create_task(r.run())
            await asyncio.sleep(0.01)
            r.stop()
            await t
        asyncio.run(go())
        self.assertEqual(db.job_counts().get("running", 0), 0)   # зависших нет
        asyncio.run(Runner(make_cfg(), db, source=src, checker=fake_checker).run())
        self.assertEqual(db.stats()["cards"], 5)
        self.assertEqual(db.stats()["sites"], 2)


class TestTemplates(unittest.TestCase):
    def test_render(self):
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader("dgis/ui/templates"))
        db = Storage(":memory:")
        s = db.stats()
        ctx = {"s": s, "state": "idle", "log": ["x"], "nav": "run", "cfg": make_cfg(), "cities": "a", "rubrics": "b"}
        self.assertIn("Сбор из 2GIS API", env.get_template("run.html").render(**ctx))
        rows = [{"domain": "a.ru", "url": "https://a.ru/", "name": "A", "city": "K", "rubric": "R", "cards": 1,
                 "http_status": 200, "analysis_status": "new", "phones": "[]"}]
        html = env.get_template("db.html").render(nav="db", rows=rows, total=1, page=1, pages=1, f={}, qs="",
                                                  sort="domain", desc=0, cities=["K"], rubrics=["R"])
        self.assertIn("a.ru", html)
        env.get_template("jobs.html").render(nav="jobs", counts={}, failed=[], state="idle")


if __name__ == "__main__":
    unittest.main()
