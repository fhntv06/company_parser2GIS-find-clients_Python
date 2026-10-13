from __future__ import annotations

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SiteCheck/1.0)"}


async def check_url(client, url: str) -> tuple[int, str | None]:
    """GET с редиректами. Пробует https, затем http. Возвращает (status, final_url); status=0 — недоступен."""
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://"):])
    for u in candidates:
        try:
            r = await client.get(u, headers=HEADERS)
            return r.status_code, str(r.url)
        except Exception:
            continue
    return 0, None
