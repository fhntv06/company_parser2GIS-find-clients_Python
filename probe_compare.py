"""Сравнение запросов: что именно мешает получить contact_groups с вашим ключом.
Запуск из папки проекта:  python probe_compare.py   (ключ из .env, в вывод не попадает)"""
import httpx

from dgis.config import load_config

cfg = load_config()
FIELDS = "items.point,items.address,items.contact_groups"
SANDBOX_ID = "70000001103939664"   # Мосцветок, Мытищи (из песочницы)


def run(title, host, path, params, c):
    p = {**params, "key": cfg.dgis_key}
    try:
        r = c.get(f"https://{host}{path}", params=p)
        d = r.json()
    except Exception as e:
        print(f"\n[{title}]\n  ошибка запроса: {type(e).__name__}"); return None
    meta = d.get("meta") or {}
    items = (d.get("result") or {}).get("items") or []
    cg = items[0].get("contact_groups") if items else None
    sites = [x.get("url") for g in (cg or []) for x in g.get("contacts", []) if x.get("type") == "website"]
    err = (meta.get("error") or {}).get("message")
    print(f"\n[{title}]\n  HTTP {r.status_code}, code={meta.get('code')}, items={len(items)}"
          f", contact_groups={'ЕСТЬ' if cg else 'нет'}, сайты={sites}" + (f", error={err}" if err else ""))
    return items


with httpx.Client(timeout=20, proxy=cfg.proxy) as c:
    # 1-3: ваш объект из песочницы, меняем по одному параметру
    run("1. byid Мытищи, .ru + locale (как в песочнице)", "catalog.api.2gis.ru", "/3.0/items/byid",
        {"id": SANDBOX_ID, "fields": FIELDS, "locale": "ru_RU"}, c)
    run("2. byid Мытищи, .com + locale", "catalog.api.2gis.com", "/3.0/items/byid",
        {"id": SANDBOX_ID, "fields": FIELDS, "locale": "ru_RU"}, c)
    run("3. byid Мытищи, .ru без locale", "catalog.api.2gis.ru", "/3.0/items/byid",
        {"id": SANDBOX_ID, "fields": FIELDS}, c)

    # 4-5: организация из Казани, домен .ru + locale
    items = run("4. поиск Казань, .ru + locale", "catalog.api.2gis.ru", "/3.0/items",
                {"q": "Стоматологии Казань", "type": "branch", "page_size": 10,
                 "fields": FIELDS, "locale": "ru_RU"}, c)
    if items:
        run("5. byid Казань, .ru + locale", "catalog.api.2gis.ru", "/3.0/items/byid",
            {"id": items[0]["id"], "fields": FIELDS, "locale": "ru_RU"}, c)

    # 6: поиск в Мытищах, чтобы отделить регион от домена
    run("6. поиск Мытищи, .ru + locale", "catalog.api.2gis.ru", "/3.0/items",
        {"q": "магазин цветов Мытищи", "type": "branch", "page_size": 10,
         "fields": FIELDS, "locale": "ru_RU"}, c)
print("\nПришлите этот вывод целиком.")