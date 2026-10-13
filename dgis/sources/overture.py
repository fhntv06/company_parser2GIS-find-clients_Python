"""Overture Maps Places: бесплатный источник организаций с сайтами.

Цепочка: город -> рамка (Nominatim, кэш) -> скачивание GeoParquet (CLI `overturemaps`)
-> фильтр по теме (duckdb) -> CSV -> импорт в базу (sources/csv_import.py).
Всё внешнее (сеть, процесс, duckdb) вынесено в отдельные функции, чтобы оркестрацию можно было тестировать.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

BBOX_MAX_AREA = 4.0          # град², защита от скачивания целой области
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "dgis-parser/0.2"
Bbox = tuple[float, float, float, float]   # xmin(lon), ymin(lat), xmax, ymax


# ---------- темы
def load_presets(path: str) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = {}
    for key, v in (data.get("presets") or {}).items():
        terms = [str(t).strip().lower() for t in (v.get("terms") or []) if str(t).strip()]
        out[key] = {"title": v.get("title", key), "terms": terms}
    return out


# ---------- рамка города
def parse_bbox(s: str) -> Bbox:
    try:
        x1, y1, x2, y2 = (float(x) for x in s.replace(";", ",").split(","))
    except ValueError:
        raise ValueError("bbox: нужно 4 числа «xmin,ymin,xmax,ymax» (долгота,широта,долгота,широта)")
    if not (-180 <= x1 < x2 <= 180 and -90 <= y1 < y2 <= 90):
        raise ValueError("bbox: ожидается xmin<xmax, ymin<ymax, долгота -180..180, широта -90..90")
    return (x1, y1, x2, y2)


def check_bbox(b: Bbox) -> Bbox:
    area = (b[2] - b[0]) * (b[3] - b[1])
    if area > BBOX_MAX_AREA:
        raise ValueError(f"Область слишком большая ({area:.1f} град²): скачивание займёт часы. "
                         "Укажите город поменьше или задайте bbox вручную.")
    return b


def _httpx_get(url, params, headers):
    import httpx
    r = httpx.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r


def geocode_city(city: str, country: str = "ru", http_get=_httpx_get) -> Bbox:
    params = {"q": city, "format": "jsonv2", "limit": 1}
    if country:
        params["countrycodes"] = country
    data = http_get(NOMINATIM, params, {"User-Agent": USER_AGENT}).json()
    if not data:
        raise ValueError(f"Город не найден: {city}. Задайте bbox вручную.")
    south, north, west, east = (float(x) for x in data[0]["boundingbox"])
    return (west, south, east, north)


# ---------- скачивание
def slug(text: str) -> str:
    return re.sub(r"[^\w]+", "_", text.lower()).strip("_")[:40] or "x"


def cache_path(dir_: str, bbox: Bbox) -> Path:
    return Path(dir_) / ("places_%.3f_%.3f_%.3f_%.3f.geoparquet" % bbox)


def csv_path(dir_: str, city: str, terms: list[str]) -> Path:
    h = hashlib.sha1(",".join(sorted(terms)).encode()).hexdigest()[:8]
    return Path(dir_) / f"{slug(city)}_{h}.csv"


def overture_cli() -> str:
    exe = shutil.which("overturemaps")
    if not exe:
        cand = Path(sys.executable).parent / ("overturemaps.exe" if os.name == "nt" else "overturemaps")
        exe = str(cand) if cand.exists() else None
    if not exe:
        raise RuntimeError("Не найдена команда overturemaps. Установите: pip install overturemaps")
    return exe


def download_places(bbox: Bbox, dest: Path, run=subprocess.run, timeout: int = 3600) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    cmd = [overture_cli(), "download", "--bbox=" + ",".join(f"{x:.6f}" for x in bbox),
           "-f", "geoparquet", "--type=place", "-o", str(tmp)]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError("overturemaps завершился с ошибкой: " + (r.stderr or "")[-500:])
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return dest


def is_fresh(path: Path, max_age_days: int) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400


# ---------- фильтрация (duckdb)
def _src(parquet: str) -> str:
    return "read_parquet('%s')" % str(parquet).replace("\\", "/").replace("'", "''")


CANDIDATES = {
    "id": ["CAST(id AS VARCHAR)"],
    "name": ["names.primary", "CAST(name AS VARCHAR)"],
    "cat": ["taxonomy.primary", "categories.primary", "basic_category"],
    "site": ["array_to_string(websites, '|')"],
    "phone": ["array_to_string(phones, '|')"],
    "addr": ["addresses[1].freeform", "CAST(address AS VARCHAR)"],
    "conf": ["confidence"],
    "stat": ["operating_status"],
}


def detect_columns(con, src: str) -> dict:
    """Схема Overture менялась между релизами: берём первое выражение, которое работает."""
    found = {}
    for key, cands in CANDIDATES.items():
        found[key] = None
        for expr in cands:
            try:
                con.sql(f"SELECT {expr} FROM {src} LIMIT 1").fetchall()
                found[key] = expr
                break
            except Exception:
                continue
    if not (found["id"] and found["site"]):
        cols = [r[0] for r in con.sql(f"DESCRIBE SELECT * FROM {src}").fetchall()]
        raise RuntimeError("В файле нет колонок id/websites. Колонки: " + ", ".join(cols))
    return found


def build_query(src: str, e: dict, terms: list[str], only_with_site=True, min_confidence=0.0) -> str:
    where = ["1=1"]
    if only_with_site:
        where.append("websites IS NOT NULL AND len(websites) > 0")
    if min_confidence and e["conf"]:
        where.append(f"coalesce({e['conf']}, 0) >= {float(min_confidence)}")
    if e["stat"]:
        where.append(f"coalesce({e['stat']}, 'open') <> 'closed'")
    like = []
    for t in terms:
        q = t.strip().lower().replace("'", "''")
        for col in (e["cat"], e["name"]):
            if q and col:
                like.append(f"lower(coalesce({col}, '')) LIKE '%{q}%'")
    if terms:
        if not like:
            raise RuntimeError("Нет колонок категории/названия для фильтра по теме")
        where.append("(" + " OR ".join(like) + ")")
    sel = ", ".join([f"{e['id']} AS id", f"{e['name'] or 'NULL'} AS name", f"{e['addr'] or 'NULL'} AS address",
                     f"{e['phone'] or 'NULL'} AS phones", f"{e['site']} AS websites",
                     f"{e['cat'] or 'NULL'} AS category"])
    return f"SELECT {sel} FROM {src} WHERE {' AND '.join(where)}"


def parquet_to_csv(parquet, out, terms, only_with_site=True, min_confidence=0.0) -> dict:
    import duckdb
    con = duckdb.connect()
    src = _src(parquet)
    e = detect_columns(con, src)
    out = str(out).replace("\\", "/").replace("'", "''")
    con.sql(f"COPY ({build_query(src, e, terms, only_with_site, min_confidence)}) TO '{out}' (HEADER, DELIMITER ',')")
    n = con.sql(f"SELECT count(*) FROM read_csv('{out}')").fetchone()[0]
    return {"rows": n, "columns": {k: v for k, v in e.items() if v}}


def list_categories(parquet, limit: int = 50) -> list[tuple[str, int]]:
    import duckdb
    con = duckdb.connect()
    src = _src(parquet)
    e = detect_columns(con, src)
    if not e["cat"]:
        return []
    return con.sql(f"SELECT coalesce({e['cat']}, '?') c, count(*) n FROM {src} "
                   f"GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}").fetchall()


# ---------- оркестрация для раннера
class OvertureOps:
    def __init__(self, cfg, db, http_get=_httpx_get, run=subprocess.run):
        self.cfg, self.db, self.http_get, self.run = cfg, db, http_get, run

    def resolve_bbox(self, city: str, bbox_str: str | None = None) -> Bbox:
        if bbox_str:
            return check_bbox(parse_bbox(bbox_str))
        cached = self.db.get_geocache(city.strip().lower())
        if cached:
            return check_bbox(tuple(json.loads(cached)))
        b = check_bbox(geocode_city(city, self.cfg.geocode_country, self.http_get))
        self.db.set_geocache(city.strip().lower(), json.dumps(b))
        return b

    def fetch(self, bbox: Bbox) -> Path:
        dest = cache_path(self.cfg.overture_dir, bbox)
        if is_fresh(dest, self.cfg.overture_cache_days):
            return dest
        return download_places(bbox, dest, self.run)

    def to_csv(self, parquet, out, terms, only_with_site=True, min_confidence=0.0) -> dict:
        return parquet_to_csv(parquet, out, terms, only_with_site, min_confidence)
