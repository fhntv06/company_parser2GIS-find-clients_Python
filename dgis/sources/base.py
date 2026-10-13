from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Card:
    dgis_id: str
    name: str = ""
    address: str = ""
    phones: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)
    rubrics: list[str] = field(default_factory=list)


@dataclass
class Page:
    total: int
    items: list[Card]


class Source(Protocol):
    async def search(self, city: str, rubric: str, page: int) -> Page: ...
