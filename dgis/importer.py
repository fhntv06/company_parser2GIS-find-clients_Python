from __future__ import annotations

import csv
import hashlib
import re

from .ingest import store_card
from .sources.base import Card
from .storage import Storage

ALIASES = {
    "id": "id", "name": "name", "title": "name", "address": "address",
    "phone": "phones", "phones": "phones",
    "website": "websites", "websites": "websites", "url": "websites", "site": "websites",
    "category": "category", "rubric": "category", "city": "city",
}


def _split(v: str | None) -> list[str]:
    return [x for x in re.split(r"\s*[|;\n]\s*", (v or "").strip()) if x]


def import_csv(db: Storage, path: str, city: str, rubric: str | None = None, tag: str = "imp") -> dict:
    """Загрузка внешней таблицы (Overture, Чекко, Контур и т.д.) в базу. Повторный импорт идемпотентен."""
    sites_before = db.one("SELECT COUNT(*) n FROM sites")["n"]
    rows = with_site = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames:
            raise ValueError("Пустой CSV")
        cols = {h: ALIASES[h.strip().lower()] for h in rd.fieldnames if h and h.strip().lower() in ALIASES}
        if "websites" not in cols.values():
            raise ValueError(f"Нет колонки с сайтом (website/websites/url/site). Есть: {rd.fieldnames}")
        for raw in rd:
            r = {cols[h]: (raw.get(h) or "").strip() for h in cols}
            sites = _split(r.get("websites"))
            name, addr = r.get("name", ""), r.get("address", "")
            rows += 1
            with_site += bool(sites)
            key = r.get("id") or hashlib.sha1(f"{name}|{addr}|{'|'.join(sites)}".encode()).hexdigest()[:16]
            card = Card(dgis_id=f"{tag}:{key}", name=name, address=addr,
                        phones=_split(r.get("phones")), websites=sites)
            store_card(db, card, r.get("city") or city, r.get("category") or rubric or "")
    st = db.stats()
    return {"rows": rows, "with_site": with_site, "new_sites": st["sites"] - sites_before,
            "own": st["own"], "social": st["social"], "none": st["none"], "sites": st["sites"]}
