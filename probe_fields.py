"""Диагностика демо-ключа 2GIS: где можно получить сайт организации.
Запуск из папки проекта:  python probe_fields.py
Ключ берётся из .env и в вывод не попадает."""
import httpx

from dgis.config import load_config

cfg = load_config()
BASE = "https://catalog.api.2gis.com/3.0/items"


def show(title, r):
    try:
        d = r.json()
    except ValueError:
        print(f"\n== {title}: HTTP {r.status_code}, не JSON"); return None
    meta = d.get("meta") or {}
    print(f"\n== {title}: HTTP {r.status_code}, meta.code={meta.get('code')}")
    if meta.get("error"):
        print("   error:", meta["error"].get("message"))
    items = (d.get("result") or {}).get("items") or []
    if items:
        it = items[0]
        print("   ключи item:", sorted(it.keys()))
        cg = it.get("contact_groups")
        print("   contact_groups:", "ЕСТЬ" if cg else "нет")
        if cg:
            print("   типы контактов:", sorted({c.get("type") for g in cg for c in g.get("contacts", [])}))
    return items


with httpx.Client(timeout=20, proxy=cfg.proxy) as c:
    base = {"q": "Стоматологии Казань", "type": "branch", "page_size": 10, "key": cfg.dgis_key}

    items = show("A: поиск, fields=items.contact_groups",
                 c.get(BASE, params={**base, "fields": "items.contact_groups"}))
    show("B: поиск, fields=items.contact_groups,items.rubrics,items.address_name",
         c.get(BASE, params={**base, "fields": "items.contact_groups,items.rubrics,items.address_name"}))
    show("C: поиск без fields", c.get(BASE, params=base))

    first_id = (items or [{}])[0].get("id") if items else None
    if not first_id:
        r = c.get(BASE, params=base)
        its = (r.json().get("result") or {}).get("items") or []
        first_id = its[0]["id"] if its else None
    if first_id:
        show("D: карточка по id (byid), fields=items.contact_groups",
             c.get(BASE + "/byid", params={"id": first_id, "fields": "items.contact_groups", "key": cfg.dgis_key}))
        show("E: карточка по id, поиск q=<id>",
             c.get(BASE, params={"id": first_id, "fields": "items.contact_groups", "key": cfg.dgis_key}))
print("\nПришлите этот вывод целиком.")