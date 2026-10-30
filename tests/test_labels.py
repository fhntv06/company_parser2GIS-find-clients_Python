import os
import sqlite3
import tempfile
import unittest

from dgis.pipeline.ingest import store_card
from dgis.sources.base import Card
from dgis.sources.categories import load_labels, make_labeler
from dgis.sources.csv_import import import_csv
from dgis.storage import Storage
from tests.helpers_csv import write_csv


class TestLabels(unittest.TestCase):
    def test_real_dictionary(self):
        labels = load_labels("config/categories_ru.yaml")
        label = make_labeler(labels)
        self.assertEqual(label("dental_clinic"), "Стоматологическая клиника")
        self.assertEqual(label("GENERAL_DENTISTRY"), "Общая стоматология")
        self.assertEqual(label("health_care"), "Здравоохранение")
        self.assertEqual(label("student_union"), "Студенческое объединение")
        for key in ("dental_clinic", "medical_center", "automotive_repair", "beauty_salon", "gym", "school"):
            self.assertIn(key, labels, key)

    def test_fallbacks(self):
        label = make_labeler({"a_b": "Эй-Би"})
        self.assertEqual(label("a_b"), "Эй-Би")
        self.assertEqual(label("never_seen"), "never_seen")       # неизвестное как есть
        self.assertEqual(label("Стоматологии"), "Стоматологии")   # уже по-русски
        self.assertEqual(label(None), "—")
        self.assertEqual(label(""), "—")
        self.assertEqual(load_labels("no/such.yaml"), {})


class TestTopic(unittest.TestCase):
    def test_import_saves_topic_and_category(self):
        p = write_csv([["1", "Клиника", "", "", "a-clinic.ru", "dental_clinic"],
                       ["2", "Без категории", "", "", "b-clinic.ru", ""]])
        db = Storage(":memory:")
        import_csv(db, p, "Казань", rubric="Стоматологии", tag="t")
        rows = {r["domain"]: r for r in db.list_sites({}, 10)}
        self.assertEqual((rows["a-clinic.ru"]["topic"], rows["a-clinic.ru"]["rubric"]), ("Стоматологии", "dental_clinic"))
        self.assertEqual((rows["b-clinic.ru"]["topic"], rows["b-clinic.ru"]["rubric"]), ("Стоматологии", "Стоматологии"))
        self.assertEqual(db.filter_values()["topics"], ["Стоматологии"])
        self.assertEqual(db.count_sites({"topic": "Стоматологии"}), 2)
        self.assertEqual(db.count_sites({"topic": "Другая"}), 0)
        self.assertEqual(db.export_sites({"topic": "Стоматологии"})[0]["topic"], "Стоматологии")
        os.unlink(p)

    def test_topic_not_erased_by_reimport_without_topic(self):
        db = Storage(":memory:")
        c = Card("1", "A", "", [], ["https://a-clinic.ru"])
        store_card(db, c, "К", "dental_clinic", topic="Стоматологии")
        store_card(db, c, "К", "dental_clinic")                  # повторная запись без темы
        self.assertEqual(db.list_sites({}, 5)[0]["topic"], "Стоматологии")

    def test_migration_adds_topic_column(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.executescript("CREATE TABLE businesses(dgis_id TEXT PRIMARY KEY, name TEXT, address TEXT, city TEXT, rubric TEXT,"
                        " phones TEXT, raw_url TEXT, site_kind TEXT DEFAULT 'none', collected_at TEXT);"
                        "INSERT INTO businesses(dgis_id,name,city,rubric) VALUES('1','A','К','dental_clinic');")
        c.close()
        db = Storage(path)
        self.assertIn("topic", [r["name"] for r in db.rows("PRAGMA table_info(businesses)")])
        self.assertEqual(db.one("SELECT rubric, topic FROM businesses")["rubric"], "dental_clinic")
        db.conn.close()


if __name__ == "__main__":
    unittest.main()
