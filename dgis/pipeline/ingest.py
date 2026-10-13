from __future__ import annotations

from ..normalize import best_kind, normalize_site
from ..sources.base import Card
from ..storage import Storage


def store_card(db: Storage, card: Card, city: str, rubric: str) -> None:
    """Общая запись карточки: используется и сбором из API, и импортом из файла."""
    sites = [s for s in (normalize_site(u) for u in card.websites) if s]
    kind = best_kind([s.kind for s in sites])
    db.upsert_business(card, city, rubric, card.websites[0] if card.websites else None, kind)
    for s in sites:
        if s.kind != "own":
            continue
        db.upsert_site(s.domain, s.url)
        db.link(card.dgis_id, s.domain)
        if db.site_needs_check(s.domain):
            db.enqueue("check_site", {"domain": s.domain, "url": s.url}, uniq=f"check:{s.domain}", priority=5, max_attempts=3)
