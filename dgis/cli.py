from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_config
from .export import iter_csv, iter_jsonl
from .logs import setup_logging
from .pipeline.runner import Runner
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
    ov = sub.add_parser("overture", help="бесплатный сбор из Overture Maps по городу и теме")
    ov.add_argument("--city", required=True)
    ov.add_argument("--preset", help="ключ темы из config/presets.yaml (dental, auto, ...)")
    ov.add_argument("--terms", help="подстроки категории/названия через запятую (вместо пресета)")
    ov.add_argument("--rubric", help="название рубрики в базе")
    ov.add_argument("--bbox", help="xmin,ymin,xmax,ymax вместо поиска города")
    ov.add_argument("--min-confidence", type=float, default=0.0)
    ov.add_argument("--all-places", action="store_true", help="включая места без сайта")
    ov.add_argument("--force", action="store_true", help="повторить уже выполненный запрос")
    ov.add_argument("--list-categories", action="store_true", help="показать категории в данных города и выйти")
    im = sub.add_parser("import", help="загрузить CSV (Overture, Чекко, Контур...) и проверить сайты")
    im.add_argument("file"); im.add_argument("--city", required=True)
    im.add_argument("--rubric"); im.add_argument("--tag", default="imp", help="префикс id источника")
    im.add_argument("--no-check", action="store_true", help="не проверять доступность сайтов")
    sub.add_parser("ui", help="веб-интерфейс")
    e = sub.add_parser("export", help="выгрузка для этапа 2")
    e.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    e.add_argument("--out", default="-")
    e.add_argument("--city"); e.add_argument("--rubric")
    e.add_argument("--status", choices=["ok", "alive", "protected", "error", "bad", "parked", "redirect", "unchecked"])
    ck = sub.add_parser("check", help="проверить один сайт и показать причину отказа")
    ck.add_argument("site")
    sub.add_parser("stats", help="статистика")
    sub.add_parser("recheck", help="перепроверить доступность всех сайтов (и проставить пометки)")
    sub.add_parser("retry-failed", help="вернуть упавшие задачи в очередь")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    setup_logging(cfg.secrets)
    db = Storage(cfg.db_path)

    if a.cmd == "probe":
        from .sources.api2gis import probe
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
    elif a.cmd == "overture":
        from .sources.overture import OvertureOps, list_categories, load_presets
        presets = load_presets(cfg.presets_path)
        terms, rubric = _split(a.terms), a.rubric
        if a.preset:
            if a.preset not in presets:
                print("Нет такой темы. Доступны:", ", ".join(presets) or "(config/presets.yaml пуст)"); return 2
            terms = terms or presets[a.preset]["terms"]
            rubric = rubric or presets[a.preset]["title"]
        ops = OvertureOps(cfg, db)
        if a.list_categories:
            parquet = ops.fetch(ops.resolve_bbox(a.city, a.bbox))
            for c, n in list_categories(parquet, 60):
                print(f"{n:6d}  {c}")
            return 0
        if not terms:
            print("Нужен --preset или --terms"); return 2
        runner = Runner(cfg, db, overture=ops)
        if not runner.enqueue_overture(a.city, terms, rubric, not a.all_places, a.min_confidence, a.bbox, a.force):
            print("Такой запрос уже выполнялся. Добавьте --force, чтобы повторить.")
        try:
            asyncio.run(runner.run())
        except KeyboardInterrupt:
            db.requeue_running(); print("\nПрервано. Продолжить: python -m dgis run --resume")
        _print_stats(db)
    elif a.cmd == "import":
        from .sources.csv_import import import_csv
        res = import_csv(db, a.file, a.city, a.rubric, a.tag)
        print(f"Импорт: строк {res['rows']}, с сайтом {res['with_site']}, новых доменов {res['new_sites']}")
        if not a.no_check:
            try:
                asyncio.run(Runner(cfg, db).run())
            except KeyboardInterrupt:
                db.requeue_running(); print("\nПрервано. Продолжить проверку: python -m dgis run --resume")
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
    elif a.cmd == "check":
        from .normalize import normalize_site, site_note
        from .pipeline.checker import check_url, make_client
        s = normalize_site(a.site)
        if not s:
            print("Не похоже на адрес сайта"); return 2

        async def one():
            c = make_client(cfg.check_timeout, 4, cfg.proxy)
            try:
                return await check_url(c, s.url)
            finally:
                await c.aclose()
        status, final, err = asyncio.run(one())
        print(f"{s.url}\n  HTTP: {status}\n  итоговый адрес: {final}\n  ошибка: {err}\n  пометка: {site_note(s.domain, final, status, err)}")
    elif a.cmd == "stats":
        _print_stats(db)
    elif a.cmd == "recheck":
        print("Поставлено на проверку:", db.requeue_checks())
        try:
            asyncio.run(Runner(cfg, db).run())
        except KeyboardInterrupt:
            db.requeue_running(); print("\nПрервано. Продолжить: python -m dgis run --resume")
        _print_stats(db)
    elif a.cmd == "retry-failed":
        print("Возвращено в очередь:", db.retry_failed())
    return 0


def _print_stats(db: Storage) -> None:
    s = db.stats()
    print(f"\nКарточек: {s['cards']} | с сайтом: {s['with_site']} "
          f"(own {s['own']}, social {s['social']}, marketplace {s['marketplace']}) | без сайта: {s['none']}")
    print(f"Собственных сайтов: {s['sites']} | доступны (2xx-3xx): {s['sites_ok']} | не проверены: {s['sites_unchecked']}")
    print(f"Защищены от ботов (403/429...): {s['sites_protected']} | не открылись: {s['sites_error']}"
          + (f" ({', '.join(f'{k}={v}' for k, v in s['error_reasons'].items())})" if s["error_reasons"] else ""))
    print(f"Задачи: {s['jobs']}")
