import asyncio
import sqlite3
import tempfile
import unittest
import os

from dgis.config import Config
from dgis.sources.csv_import import import_csv
from dgis.normalize import normalize_phone, site_note
from dgis.pipeline.runner import Runner
from dgis.storage import Storage
from tests.test_import import write_csv


class TestQuality(unittest.TestCase):
    def test_phone(self):
        self.assertEqual(normalize_phone("8 (843) 245-21-12"), "+78432452112")
        self.assertEqual(normalize_phone("+78432102525"), "+78432102525")
        self.assertEqual(normalize_phone("843 245-21-12"), "+78432452112")
        self.assertIsNone(normalize_phone("12-34"))
        self.assertIsNone(normalize_phone(""))

    def test_site_note(self):
        self.assertIsNone(site_note("a.ru", "https://www.a.ru/"))
        self.assertIsNone(site_note("a.ru", "http://a.ru/"))
        self.assertEqual(site_note("profclinik.ru", "https://vh398.timeweb.ru/parking/?ref=profclinik.ru"), "parked")
        self.assertEqual(site_note("dantist-kazan.ru", "https://doctorpromo.ru/"), "redirect:doctorpromo.ru")
        self.assertIsNone(site_note("a.ru", None))

    def test_pipeline_notes_export_and_filters(self):
        finals = {"ok-clinic.ru": ("https://www.ok-clinic.ru/", None),
                  "parked-clinic.ru": ("https://vh398.timeweb.ru/parking/?ref=x", "parked"),
                  "old-clinic.ru": ("https://new-clinic.ru/", "redirect:new-clinic.ru")}

        async def checker(url):
            host = url.split("//")[1].strip("/")
            return 200, finals[host][0]

        p = write_csv([
            ["1", "Ок", "", "8 (843) 245-21-12", "ok-clinic.ru", "dental_clinic"],
            ["2", "Парк", "", "", "parked-clinic.ru", "dental_clinic"],
            ["3", "Старый", "", "+78432452112", "old-clinic.ru", "dental_clinic"]])
        db = Storage(":memory:")
        import_csv(db, p, "Казань")
        asyncio.run(Runner(Config(concurrency=1, check_concurrency=3), db, source=object(), checker=checker).run())
        rows = {r["domain"]: r for r in db.export_sites({})}
        self.assertEqual({d: r["note"] for d, r in rows.items()},
                         {"ok-clinic.ru": None, "parked-clinic.ru": "parked", "old-clinic.ru": "redirect:new-clinic.ru"})
        self.assertEqual(rows["old-clinic.ru"]["target_url"], "https://new-clinic.ru/")
        self.assertEqual(rows["ok-clinic.ru"]["target_url"], "https://ok-clinic.ru/")
        self.assertEqual(rows["ok-clinic.ru"]["phones"], ["+78432452112"])
        self.assertEqual(sorted(r["domain"] for r in db.export_sites({"status": "ok"})), ["ok-clinic.ru", "old-clinic.ru"])
        self.assertEqual([r["domain"] for r in db.export_sites({"status": "parked"})], ["parked-clinic.ru"])
        self.assertEqual([r["domain"] for r in db.export_sites({"status": "redirect"})], ["old-clinic.ru"])
        self.assertEqual(db.list_sites({"status": "parked"}, 5)[0]["note"], "parked")
        # перепроверка ставит все сайты в очередь заново
        self.assertEqual(db.requeue_checks(), 3)
        os.unlink(p)

    def test_migration_adds_note_column(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.executescript("CREATE TABLE sites(domain TEXT PRIMARY KEY, url TEXT, http_status INT, final_url TEXT,"
                        " checked_at TEXT, analysis_status TEXT DEFAULT 'new');"
                        "INSERT INTO sites(domain,url) VALUES('a.ru','https://a.ru/');")
        c.close()
        db = Storage(path)
        self.assertIn("note", [r["name"] for r in db.rows("PRAGMA table_info(sites)")])
        self.assertEqual(db.one("SELECT COUNT(*) n FROM sites")["n"], 1)
        db.conn.close()


if __name__ == "__main__":
    unittest.main()
