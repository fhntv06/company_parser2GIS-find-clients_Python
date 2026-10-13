# 2GIS → база сайтов (этап 1, beta)

## Установка
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # впишите DGIS_KEY=...
```

## Порядок запуска
```bash
python -m dgis probe                      # 1) что отдаёт ключ: сайты, page_size, поля
# по результату probe поправьте page_size / rate_limit_rps в config.yaml
python -m dgis run --cities "Казань" --rubrics "Стоматологии"   # 2) мини-прогон
python -m dgis stats
python -m dgis export --format jsonl --out sites.jsonl --status ok
python -m dgis ui                         # UI: http://127.0.0.1:8000
```
Прервали (Ctrl+C / перезапуск) — `python -m dgis run --resume` продолжит без дублей.
`python -m dgis retry-failed` возвращает упавшие задачи.

## Что внутри
- `jobs` в SQLite = очередь; воркеры: `collect_list` (2GIS, глобальный лимитер) и `check_site` (отдельный пул).
- 429/403 → пауза всех запросов на 60 с и повтор задачи; прочие ошибки — экспоненциальные ретраи, после 5 попыток `failed`.
- Таблица `sites` — только собственные сайты (own). Соцсети/маркетплейсы учитываются в `businesses.site_kind`.
- Контракт с этапом 2: JSONL (`export`) или `SELECT * FROM sites WHERE analysis_status='new' AND http_status BETWEEN 200 AND 399`.

## Известные ограничения beta
- Запрос идёт как `q="<рубрика> <город>"`; лимит глубины выдачи 2GIS на один запрос не обходится (дробление по районам — v2). Сравнивайте «всего по запросу» в логе с собранным.
- Ссылки `link.2gis.*` без целевого URL пропускаются.
- Не реализовано (v2): расписания, `WebSource`/Playwright, ротация прокси.
- Проверка сайтов идёт с `verify=False` (нужен только статус ответа).
- Ключ только в `.env`; в логах и UI маскируется.

### Использование
- Почта: cloudecode1@devjumpstart.com