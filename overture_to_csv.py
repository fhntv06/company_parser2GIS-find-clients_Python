"""Выгрузка мест из Overture Maps (GeoParquet) в простой CSV для `python -m dgis import`.

Требуется: pip install duckdb
Пример:
  python overture_to_csv.py kazan.geoparquet --out places.csv --category dent --only-with-site

Скрипт сам определяет, какие колонки есть в вашем файле (схема Overture менялась между релизами),
и печатает, что именно использовал. Если колонку не нашёл, выведет схему файла.
"""
import argparse
import sys

import duckdb

ap = argparse.ArgumentParser()
ap.add_argument("parquet")
ap.add_argument("--out", default="places.csv")
ap.add_argument("--category", help="подстроки через запятую в категории или названии, например: dental,dentist. ОСТОРОЖНО: короткие подстроки дают лишнее (dent найдёт student_union)")
ap.add_argument("--min-confidence", type=float, default=0.0)
ap.add_argument("--only-with-site", action="store_true", help="только места с сайтом")
a = ap.parse_args()

con = duckdb.connect()
src = "read_parquet('%s')" % a.parquet.replace("\\", "/").replace("'", "''")
cols = {r[0]: r[1] for r in con.sql(f"DESCRIBE SELECT * FROM {src}").fetchall()}


def first_working(candidates, label):
    for expr in candidates:
        try:
            con.sql(f"SELECT {expr} FROM {src} LIMIT 1").fetchall()
            print(f"  {label}: {expr}")
            return expr
        except Exception:
            continue
    print(f"  {label}: НЕ НАЙДЕНО")
    return None


print("Колонки файла:", ", ".join(cols))
print("Использую:")
e_id = first_working(["CAST(id AS VARCHAR)"], "id")
e_name = first_working(["names.primary", "CAST(name AS VARCHAR)"], "name")
e_cat = first_working(["taxonomy.primary", "categories.primary", "basic_category"], "category")
e_site = first_working(["array_to_string(websites, '|')"], "websites")
e_phone = first_working(["array_to_string(phones, '|')"], "phones")
e_addr = first_working(["addresses[1].freeform", "CAST(address AS VARCHAR)"], "address")
e_conf = first_working(["confidence"], "confidence")
e_stat = first_working(["operating_status"], "operating_status")

if not (e_id and e_site):
    print("\nНе нашёл обязательные колонки id/websites. Схема файла:")
    for k, v in cols.items():
        print(" ", k, v)
    sys.exit(1)

where = ["1=1"]
if a.only_with_site:
    where.append("websites IS NOT NULL AND len(websites) > 0")
if a.min_confidence and e_conf:
    where.append(f"coalesce({e_conf}, 0) >= {a.min_confidence}")
if e_stat:
    where.append(f"coalesce({e_stat}, 'open') <> 'closed'")
if a.category:
    like = []
    for q in [x.strip().lower().replace("'", "''") for x in a.category.split(",") if x.strip()]:
        if e_cat:
            like.append(f"lower(coalesce({e_cat}, '')) LIKE '%{q}%'")
        if e_name:
            like.append(f"lower(coalesce({e_name}, '')) LIKE '%{q}%'")
    where.append("(" + " OR ".join(like) + ")")

sel = ", ".join([
    f"{e_id} AS id",
    f"{e_name or 'NULL'} AS name",
    f"{e_addr or 'NULL'} AS address",
    f"{e_phone or 'NULL'} AS phones",
    f"{e_site} AS websites",
    f"{e_cat or 'NULL'} AS category",
])
out = a.out.replace("\\", "/").replace("'", "''")
con.sql(f"COPY (SELECT {sel} FROM {src} WHERE {' AND '.join(where)}) TO '{out}' (HEADER, DELIMITER ',')")
n = con.sql(f"SELECT count(*) FROM read_csv('{out}')").fetchone()[0]
print(f"\nГотово: {n} строк -> {a.out}")
print(f'Дальше: python -m dgis import {a.out} --city "Казань" --tag ovt')
