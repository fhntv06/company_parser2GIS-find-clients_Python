"""Спасение данных из повреждённой базы (database disk image is malformed).
Запуск из папки проекта:  python tools/rescue_db.py data/dgis.db data/dgis_rescued.db
Копирует всё, что читается; исходный файл не меняется."""
import sqlite3
import sys

from dgis.storage import Storage

if len(sys.argv) != 3:
    sys.exit(__doc__)
src_path, dst_path = sys.argv[1:3]
dst = Storage(dst_path)
src = sqlite3.connect(src_path)
for table in ("businesses", "sites", "business_sites", "geocache"):
    try:
        mine = [r[1] for r in dst.conn.execute(f"PRAGMA table_info({table})")]
        theirs = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError as e:
        print(f"{table}: схема не читается ({e})"); continue
    cols = [c for c in mine if c in theirs]
    if not cols:
        print(f"{table}: нет данных"); continue
    ok, err = 0, None
    try:
        cur = src.execute(f"SELECT {','.join(cols)} FROM {table}")
        while (row := cur.fetchone()) is not None:
            dst.conn.execute(f"INSERT OR IGNORE INTO {table}({','.join(cols)}) "
                             f"VALUES({','.join('?' * len(cols))})", row)
            ok += 1
    except sqlite3.DatabaseError as e:
        err = e
    print(f"{table}: спасено {ok}" + (f", дальше не читается ({err})" if err else ""))
print("\nДальше: переименуйте data/dgis.db в data/dgis_broken.db, а data/dgis_rescued.db в data/dgis.db,\n"
      "затем повторите сбор/импорт (дублей не будет) и выполните: python -m dgis recheck")
