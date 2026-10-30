"""Русские названия категорий Overture (dental_clinic -> Стоматологическая клиника) для интерфейса."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import yaml


def load_labels(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {str(k).strip().lower(): str(v).strip() for k, v in (data.get("labels") or {}).items() if v}


def make_labeler(labels: dict[str, str]) -> Callable[[str | None], str]:
    """Ключ категории -> русское название. Незнакомый ключ возвращается как есть (его можно добавить в словарь)."""
    def label(key: str | None) -> str:
        if not key:
            return "—"
        if re.search(r"[а-яё]", key, re.I):          # уже по-русски (например, название темы)
            return key
        return labels.get(key.strip().lower(), key)
    return label
