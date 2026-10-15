import asyncio
import csv
import os
import tempfile
import unittest

from dgis.config import Config
from dgis.sources.csv_import import import_csv
from dgis.pipeline.runner import Runner
from dgis.storage import Storage


def write_csv(rows, header=("id", "name", "address", "phones", "websites", "category")):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig", newline="")
    w = csv.writer(f); w.writerow(header); w.writerows(rows); f.close()
    return f.name


async def checker(url):
    return (0, None) if "dead" in url else (200, url)


class TestImport(unittest.TestCase):
    def test_import_dedup_and_check(self):
        p = write_csv([
            ["1", "Клиника А", "ул. 1", "+7843|+7900", "http://www.a-clinic.ru/?utm_x=1", "dentist"],
            ["2", "Клиника Б", "ул. 2", "", "https://a-clinic.ru/filial|https://vk.com/b", "dentist"],
            ["3", "Салон", "ул. 3", "", "https://vk.com/salon", "beauty"],
            ["4", "Без сайта", "ул. 4", "+7", "", "shop"],
            ["5", "Мёртвый", "ул. 5", "", "dead-site.ru", "shop"],
        ])
        db = Storage(":memory:")
        res = import_csv(db, p, "Казань", tag="ovt")
        self.assertEqual((res["rows"], res["with_site"], res["new_sites"]), (5, 4, 2))
        s = db.stats()
        self.assertEqual((s["cards"], s["own"], s["social"], s["none"]), (5, 3, 1, 1))
        asyncio.run(Runner(Config(concurrency=1, check_concurrency=3), db, source=object(), checker=checker).run())
        s = db.stats()
        self.assertEqual((s["sites"], s["sites_ok"], s["sites_unchecked"]), (2, 1, 0))
        rows = db.export_sites({})
        self.assertEqual(sorted(rows[0]["business_names"]), ["Клиника А", "Клиника Б"])
        self.assertEqual(rows[0]["phones"], ["+7843", "+7900"])
        self.assertEqual(rows[0]["city"], "Казань")
        self.assertEqual(rows[0]["rubric"], "dentist")
        # повторный импорт: без дублей, повторной проверки нет
        res2 = import_csv(db, p, "Казань", tag="ovt")
        self.assertEqual((res2["new_sites"], db.stats()["cards"]), (0, 5))
        self.assertEqual(db.active_jobs(), 0)
        os.unlink(p)

    def test_aliases_and_missing_column(self):
        p = write_csv([["Фирма", "firma.ru", "Москва"]], header=("Title", "URL", "City"))
        db = Storage(":memory:")
        self.assertEqual(import_csv(db, p, "X")["new_sites"], 1)
        self.assertEqual(db.list_sites({}, 5)[0]["city"], "Москва")   # city из строки приоритетнее
        bad = write_csv([["a"]], header=("name",))
        with self.assertRaises(ValueError):
            import_csv(db, bad, "X")

    def test_runner_without_key_is_ok(self):
        """После импорта проверка сайтов работает без DGIS_KEY."""
        p = write_csv([["1", "A", "", "", "okno-clinic.ru", ""]])
        db = Storage(":memory:")
        import_csv(db, p, "К")
        r = Runner(Config(concurrency=1, check_concurrency=2), db, checker=checker)
        asyncio.run(r.run())
        self.assertEqual(db.stats()["sites_ok"], 1)


if __name__ == "__main__":
    unittest.main()
