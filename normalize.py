from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

SOCIAL = {
    "vk.com", "vk.ru", "vkontakte.ru", "ok.ru", "instagram.com", "facebook.com", "fb.com",
    "t.me", "telegram.me", "telegram.org", "wa.me", "whatsapp.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "dzen.ru", "zen.yandex.ru", "viber.com", "max.ru",
}
MARKETPLACE = {
    "2gis.ru", "2gis.com", "yandex.ru", "yandex.com", "ya.ru", "google.com", "google.ru",
    "avito.ru", "ozon.ru", "wildberries.ru", "flamp.ru", "zoon.ru", "yell.ru", "orgpage.ru",
    "prodoctorov.ru", "otzovik.com", "irecommend.ru", "linktr.ee", "taplink.cc", "taplink.ws",
    "clck.ru", "bit.ly", "goo.gl",
    # виджеты чатов/обратного звонка: 2GIS отдаёт их как type=website
    "jivo.chat", "jivo.ru", "livetex.ru", "talk-me.ru",
}


@dataclass(frozen=True)
class Site:
    domain: str   # юникод (для .рф)
    url: str      # https://<ascii-host>/
    kind: str     # own | social | marketplace


def _in(host: str, names: set[str]) -> bool:
    return any(host == n or host.endswith("." + n) for n in names)


def classify(host: str) -> str:
    if _in(host, SOCIAL):
        return "social"
    if _in(host, MARKETPLACE):
        return "marketplace"
    return "own"


def normalize_site(raw: str | None) -> Site | None:
    """Сырая ссылка из карточки -> Site или None (мусор / нераскрытый редирект 2GIS)."""
    if not raw:
        return None
    s = raw.strip()
    if not s or s.lower().startswith(("tel:", "mailto:", "javascript:")):
        return None
    if "://" not in s:
        s = "http://" + s.lstrip("/")
    try:
        host = (urlsplit(s).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    if host.startswith("www."):
        host = host[4:]
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if re.fullmatch(r"link\.2gis\.\w+", ascii_host):
        return None  # редирект 2GIS, целевой URL неизвестен
    try:
        domain = ascii_host.encode("ascii").decode("idna")
    except UnicodeError:
        domain = ascii_host
    return Site(domain=domain, url=f"https://{ascii_host}/", kind=classify(ascii_host))


def best_kind(kinds: list[str]) -> str:
    for k in ("own", "social", "marketplace"):
        if k in kinds:
            return k
    return "none"
