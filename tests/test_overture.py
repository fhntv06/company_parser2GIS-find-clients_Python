import asyncio
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dgis.config import Config
from dgis.pipeline.runner import Runner
from dgis.sources import overture as ov
from dgis.storage import Storage


class FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class TestBbox(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ov.parse_bbox("48.9, 55.65, 49.35, 55.9"), (48.9, 55.65, 49.35, 55.9))
        for bad in ("1,2,3", "a,b,c,d", "10,10,5,20", "0,0,200,10"):
            with self.assertRaises(ValueError, msg=bad):
                ov.parse_bbox(bad)

    def test_too_big(self):
        with self.assertRaises(ValueError):
            ov.check_bbox((30.0, 50.0, 40.0, 56.0))
        self.assertEqual(ov.check_bbox((48.9, 55.65, 49.35, 55.9)), (48.9, 55.65, 49.35, 55.9))

    def test_geocode(self):
        seen = {}

        def get(url, params, headers):
            seen.update(params=params, headers=headers)
            return FakeResp([{"boundingbox": ["55.6", "55.9", "48.8", "49.4"]}])
        self.assertEqual(ov.geocode_city("Казань", "ru", get), (48.8, 55.6, 49.4, 55.9))
        self.assertEqual(seen["params"]["countrycodes"], "ru")
        self.assertIn("User-Agent", seen["headers"])
        with self.assertRaises(ValueError):
            ov.geocode_city("Нигдеград", "ru", lambda u, p, h: FakeResp([]))


class TestPresets(unittest.TestCase):
    def test_real_file(self):
        pr = ov.load_presets("config/presets.yaml")
        self.assertIn("dental", pr)
        self.assertEqual(pr["dental"]["title"], "Стоматологии")
        self.assertNotIn("dent", pr["dental"]["terms"])        # короткая подстрока дала бы student_union
        self.assertEqual(ov.load_presets("no/such/file.yaml"), {})


E = {"id": "CAST(id AS VARCHAR)", "name": "names.primary", "cat": "taxonomy.primary", "site": "array_to_string(websites, '|')",
     "phone": "array_to_string(phones, '|')", "addr": "addresses[1].freeform", "conf": "confidence", "stat": "operating_status"}


class TestQuery(unittest.TestCase):
    def test_build(self):
        q = ov.build_query("read_parquet('x')", E, ["dental", "o'brien"], True, 0.3)
        self.assertIn("len(websites) > 0", q)
        self.assertIn("coalesce(confidence, 0) >= 0.3", q)
        self.assertIn("<> 'closed'", q)
        self.assertIn("lower(coalesce(taxonomy.primary, '')) LIKE '%dental%'", q)
        self.assertIn("LIKE '%o''brien%'", q)                  # кавычка экранирована
        self.assertIn("names.primary", q)

    def test_no_terms_no_site_filter(self):
        q = ov.build_query("src", E, [], only_with_site=False)
        self.assertNotIn("LIKE", q)
        self.assertNotIn("len(websites)", q)

    def test_paths(self):
        self.assertTrue(str(ov.cache_path("d", (48.9, 55.65, 49.35, 55.9))).endswith("places_48.900_55.650_49.350_55.900.geoparquet"))
        a, b = ov.csv_path("d", "Казань", ["b", "a"]), ov.csv_path("d", "Казань", ["a", "b"])
        self.assertEqual(a, b)                                  # порядок подстрок не важен


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._old = ov.overture_cli
        ov.overture_cli = lambda: "overturemaps"

    def tearDown(self):
        ov.overture_cli = self._old

    def test_ok(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"PAR1")
            return SimpleNamespace(returncode=0, stderr="")
        dest = ov.download_places((48.9, 55.65, 49.35, 55.9), self.dir / "k.geoparquet", run)
        self.assertTrue(dest.exists())
        self.assertFalse((self.dir / "k.geoparquet.part").exists())
        cmd = calls[0]
        self.assertIn("--bbox=48.900000,55.650000,49.350000,55.900000", cmd)
        self.assertIn("--type=place", cmd)

    def test_failure_cleans_part(self):
        def run(cmd, **kw):
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"x")
            return SimpleNamespace(returncode=1, stderr="boom network")
        with self.assertRaises(RuntimeError) as cm:
            ov.download_places((1, 1, 2, 2), self.dir / "k.geoparquet", run)
        self.assertIn("boom network", str(cm.exception))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_fresh(self):
        p = self.dir / "f"
        self.assertFalse(ov.is_fresh(p, 30))
        p.write_text("x")
        self.assertTrue(ov.is_fresh(p, 30))
        os.utime(p, (1, 1))
        self.assertFalse(ov.is_fresh(p, 30))


class FakeOps:
    """Вместо сети/duckdb: возвращает готовый набор мест."""
    def __init__(self):
        self.geo_calls = self.fetch_calls = 0

    def resolve_bbox(self, city, bbox=None):
        self.geo_calls += 1
        return (48.9, 55.65, 49.35, 55.9)

    def fetch(self, bbox):
        self.fetch_calls += 1
        return "fake.geoparquet"

    def to_csv(self, parquet, out, terms, only_with_site=True, min_confidence=0.0):
        rows = [["1", "Клиника А", "ул.1", "8 (843) 245-21-12", "http://a-clinic.ru/?utm_x=1", "dental_clinic"],
                ["2", "Сеть Б", "ул.2", "", "https://a-clinic.ru/b|https://vk.com/b", "dental_clinic"],
                ["3", "Мёртвая", "ул.3", "", "https://dead-clinic.ru", "dental_clinic"]]
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "name", "address", "phones", "websites", "category"])
            w.writerows(rows)
        return {"rows": len(rows), "columns": {}}


async def checker(url):
    return (0, None) if "dead" in url else (200, url)


class TestOvertureJob(unittest.TestCase):
    def make(self):
        cfg = Config(concurrency=2, check_concurrency=3, overture_dir=tempfile.mkdtemp())
        db = Storage(":memory:")
        ops = FakeOps()
        return db, ops, Runner(cfg, db, source=object(), checker=checker, overture=ops)

    def test_job_end_to_end(self):
        db, ops, r = self.make()
        self.assertTrue(r.enqueue_overture("Казань", ["dental", "dentist"], "Стоматологии"))
        asyncio.run(r.run())
        s = db.stats()
        self.assertEqual((s["cards"], s["sites"], s["sites_ok"], s["sites_unchecked"]), (3, 2, 1, 0))
        self.assertEqual(db.job_counts(), {"done": 1 + 2})                # 1 overture + 2 проверки сайтов
        rows = db.export_sites({})
        self.assertEqual((rows[0]["city"], rows[0]["rubric"]), ("Казань", "dental_clinic"))
        self.assertEqual(rows[0]["phones"], ["+78432452112"])
        job = db.recent_jobs("overture")[0]
        self.assertEqual((job["status"], job["payload"]["city"]), ("done", "Казань"))
        # повтор: без дублей и без повторной загрузки, force повторяет
        self.assertFalse(r.enqueue_overture("казань", ["dentist", "dental"]))
        self.assertTrue(r.enqueue_overture("Казань", ["dental", "dentist"], force=True))
        asyncio.run(r.run())
        self.assertEqual(db.stats()["cards"], 3)
        self.assertEqual(ops.fetch_calls, 2)

    def test_failure_retries_then_failed(self):
        db, ops, r = self.make()

        def boom(*a, **k):
            raise RuntimeError("нет сети")
        ops.fetch = boom
        r.enqueue_overture("Казань", ["dental"])
        asyncio.run(asyncio.wait_for(self._run_until_failed(db, r), 30))
        job = db.recent_jobs("overture")[0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["attempts"], 2)                               # max_attempts=2 для тяжёлых задач
        self.assertIn("нет сети", job["error"])

    async def _run_until_failed(self, db, r):
        task = asyncio.create_task(r.run())
        while db.recent_jobs("overture")[0]["status"] != "failed":
            db.exec("UPDATE jobs SET run_after='2000-01-01T00:00:00' WHERE status='queued'")   # не ждём backoff
            await asyncio.sleep(0.05)
        r.stop()
        await task

    def test_validation(self):
        db, ops, r = self.make()
        with self.assertRaises(ValueError):
            r.enqueue_overture("", ["dental"])
        with self.assertRaises(ValueError):
            r.enqueue_overture("Казань", [" "])


class TestOpsCache(unittest.TestCase):
    def test_geocode_cached(self):
        db = Storage(":memory:")
        calls = []

        def get(url, params, headers):
            calls.append(1)
            return FakeResp([{"boundingbox": ["55.6", "55.9", "48.8", "49.4"]}])
        ops = ov.OvertureOps(Config(), db, http_get=get)
        a, b = ops.resolve_bbox("Казань"), ops.resolve_bbox(" КАЗАНЬ ")
        self.assertEqual((a, len(calls)), (b, 1))
        self.assertEqual(ops.resolve_bbox("x", "48.9,55.65,49.35,55.9"), (48.9, 55.65, 49.35, 55.9))


if __name__ == "__main__":
    unittest.main()
