from __future__ import annotations

import re

from ..errors import RetryLater
from .base import Card, Page

ITEMS_URL = "https://catalog.api.2gis.com/3.0/items"
FIELDS = "items.contact_groups,items.rubrics,items.address_name,items.point"


_REDIR = re.compile(r"^https?://link\.2gis\.\w+/[^?]*\?(https?://.+)$")


def _website(c: dict) -> str | None:
    """Чистый URL сайта. В `value` у 2GIS обычно редирект link.2gis.*, настоящий адрес в `url`/`text`/хвосте value.
    Редирект в любом поле отбрасывается, берётся первое чистое значение."""
    u = c.get("url")
    if u and "link.2gis." not in u:
        return u
    v = c.get("value") or ""
    m = _REDIR.match(v)
    if m:
        return m.group(1)
    if v and "link.2gis." not in v:
        return v
    t = c.get("text") or ""
    return t if "." in t and " " not in t else None


def parse_item(it: dict) -> Card:
    sites, phones = [], []
    for g in it.get("contact_groups") or []:
        for c in g.get("contacts") or []:
            t = c.get("type")
            if t == "website":
                v = _website(c)
                if v:
                    sites.append(v)
            elif t == "phone":
                v = c.get("value") or c.get("text")
                if v:
                    phones.append(v.replace("tel:", ""))
    return Card(
        dgis_id=str(it.get("id")), name=it.get("name") or "",
        address=it.get("address_name") or it.get("full_name") or "",
        phones=phones, websites=sites,
        rubrics=[r.get("name", "") for r in it.get("rubrics") or []],
    )


class ApiSource:
    def __init__(self, key: str, client, page_size: int):
        self.key, self.client, self.page_size = key, client, page_size

    async def search(self, city: str, rubric: str, page: int) -> Page:
        params = {"q": f"{rubric} {city}", "type": "branch", "page": page,
                  "page_size": self.page_size, "fields": FIELDS, "key": self.key}
        r = await self.client.get(ITEMS_URL, params=params)
        if r.status_code in (429, 403):
            raise RetryLater(60, f"2GIS HTTP {r.status_code} (лимит/блокировка)")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"2GIS: не JSON, HTTP {r.status_code}")
        meta = data.get("meta") or {}
        code = meta.get("code", r.status_code)
        if code == 404:               # дальше результатов нет
            return Page(0, [])
        if code in (429, 403):
            raise RetryLater(60, f"2GIS code {code}: {(meta.get('error') or {}).get('message')}")
        if code != 200:
            raise RuntimeError(f"2GIS code {code}: {(meta.get('error') or {}).get('message')}")
        res = data.get("result") or {}
        return Page(int(res.get("total") or 0), [parse_item(i) for i in res.get("items") or []])


async def probe(cfg) -> None:
    """Первый шаг ТЗ: что реально отдаёт ключ. Ключ в вывод не попадает."""
    import httpx

    if not cfg.dgis_key:
        print("DGIS_KEY не задан (.env)"); return
    async with httpx.AsyncClient(timeout=20, proxy=cfg.proxy) as c:
        for size in (50, 10):
            params = {"q": "Стоматологии Казань", "type": "branch", "page": 1, "page_size": size,
                      "fields": FIELDS, "key": cfg.dgis_key}
            r = await c.get(ITEMS_URL, params=params)
            data = r.json() if "json" in r.headers.get("content-type", "") else {}
            meta = data.get("meta") or {}
            print(f"\n== page_size={size}: HTTP {r.status_code}, meta.code={meta.get('code')}")
            if meta.get("error"):
                print("error:", meta["error"]); continue
            items = (data.get("result") or {}).get("items") or []
            cards = [parse_item(i) for i in items]
            print(f"total={((data.get('result') or {}).get('total'))}, получено={len(items)}")
            print(f"с сайтом: {sum(1 for x in cards if x.websites)} из {len(cards)}")
            for x in cards[:3]:
                print(" -", x.name, "|", x.websites[:2], "|", x.phones[:1])
            if items:
                print("ключи item:", sorted(items[0].keys()))
                print("ВЕРДИКТ: page_size=%d работает; поставьте его в config.yaml" % size)
                break