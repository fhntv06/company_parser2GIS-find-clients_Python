from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_config
from .export import iter_csv, iter_jsonl
from .logs import setup_logging
from .runner import Runner
from .storage import Storage


def _split(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dgis", description="2GIS -> база сайтов (этап 1)")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="тестовый запрос: что отдаёт ключ")
    r = sub.add_parser("run", help="собрать данные (или продолжить прерванный сбор)")
    r.add_argument("--cities"); r.add_argument("--rubrics")
    r.add_argument("--force", action="store_true", help="пересобрать уже выполненные")
    r.add_argument("--resume", action="store_true", help="только продолжить очередь")
    sub.add_parser("ui", help="веб-интерфейс")
    e = sub.add_parser("export", help="выгрузка для этапа 2")
    e.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    e.add_argument("--out", default="-")
    e.add_argument("--city"); e.add_argument("--rubric")
    e.add_argument("--status", choices=["ok", "bad", "unchecked"])
    sub.add_parser("stats", help="статистика")
    sub.add_parser("retry-failed", help="вернуть упавшие задачи в очередь")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    setup_logging(cfg.secrets)
    db = Storage(cfg.db_path)

    if a.cmd == "probe":
        from .sources.api import probe
        asyncio.run(probe(cfg))
    elif a.cmd == "run":
        runner = Runner(cfg, db)
        if not a.resume:
            cities, rubrics = _split(a.cities) or cfg.cities, _split(a.rubrics) or cfg.rubrics
            if not cities or not rubrics:
                print("Нужны города и рубрики (config.yaml или --cities/--rubrics)"); return 2
            runner.enqueue_collect(cities, rubrics, force=a.force)
        try:
            asyncio.run(runner.run())
        except KeyboardInterrupt:
            db.requeue_running(); print("\nПрервано. Продолжить: python -m dgis run --resume")
        _print_stats(db)
    elif a.cmd == "ui":
        import uvicorn
        from .ui.app import create_app
        uvicorn.run(create_app(cfg, db), host=cfg.ui_host, port=cfg.ui_port)
    elif a.cmd == "export":
        f = {"city": a.city, "rubric": a.rubric, "status": a.status}
        it = (iter_jsonl if a.format == "jsonl" else iter_csv)(db, f)
        out = sys.stdout if a.out == "-" else open(a.out, "w", encoding="utf-8", newline="")
        for chunk in it:
            out.write(chunk)
        if out is not sys.stdout:
            out.close(); print("Сохранено:", a.out)
    elif a.cmd == "stats":
        _print_stats(db)
    elif a.cmd == "retry-failed":
        print("Возвращено в очередь:", db.retry_failed())
    return 0


def _print_stats(db: Storage) -> None:
    s = db.stats()
    print(f"\nКарточек: {s['cards']} | с сайтом: {s['with_site']} "
          f"(own {s['own']}, social {s['social']}, marketplace {s['marketplace']}) | без сайта: {s['none']}")
    print(f"Собственных сайтов: {s['sites']} | доступны (2xx-3xx): {s['sites_ok']} | не проверены: {s['sites_unchecked']}")
    print(f"Задачи: {s['jobs']}")
