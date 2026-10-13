from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Config:
    cities: list[str] = field(default_factory=list)
    rubrics: list[str] = field(default_factory=list)
    source: str = "api"
    concurrency: int = 3
    check_concurrency: int = 20
    rate_limit_rps: float = 2.0
    page_size: int = 10          # поднять до 50, если ключ позволяет (см. `probe`)
    max_pages: int = 50
    db_path: str = "data/dgis.db"
    proxy: str | None = None
    check_timeout: float = 12.0
    presets_path: str = "config/presets.yaml"
    overture_dir: str = "data/overture"
    overture_cache_days: int = 30
    geocode_country: str = "ru"
    ui_host: str = "127.0.0.1"
    ui_port: int = 8000
    # секреты: только из окружения
    dgis_key: str = ""
    ui_user: str = ""
    ui_password: str = ""

    @property
    def secrets(self) -> list[str]:
        return [s for s in (self.dgis_key, self.ui_password, self.proxy or "") if s]


def load_config(path: str = "config.yaml") -> Config:
    load_dotenv()
    data: dict = {}
    p = Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    known = {f.name for f in fields(Config)}
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    cfg.dgis_key = os.environ.get("DGIS_KEY", "")
    cfg.ui_user = os.environ.get("UI_USER", "")
    cfg.ui_password = os.environ.get("UI_PASSWORD", "")
    cfg.proxy = os.environ.get("PROXY_URL") or cfg.proxy
    return cfg
