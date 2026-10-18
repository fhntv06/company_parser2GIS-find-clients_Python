# Сбор базы сайтов организаций (этап 1)

Собирает организации города по теме (стоматологии, автосервисы...), оставляет тех, у кого есть **собственный сайт**,
проверяет, что сайт открывается, и отдаёт чистую таблицу для следующего этапа (анализ сайтов на соответствие 152-ФЗ).

```
город + тема ──► источник ──► нормализация, дедуп ──► проверка сайта ──► база (SQLite) ──► экспорт CSV / JSONL
                 Overture (бесплатно)                  статус, парковка,
                 2GIS API (нужен платный ключ)         переезд на другой домен
                 свой CSV (Чекко, Контур, Excel)
```

## Быстрый старт (5 минут)

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m dgis ui          # откройте http://127.0.0.1:8000
```

На главной странице выберите **город** и **тему**, нажмите «Собрать». Через несколько минут вкладка **База** покажет
сайты, а кнопки CSV / JSONL выгрузят их. Подробно: [docs/quickstart.md](docs/quickstart.md).

## Документация

| Документ | О чём |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | установка (Windows и Linux), первый сбор через UI и командную строку |
| [docs/sources.md](docs/sources.md) | откуда берутся данные, что умеет и не умеет каждый источник, как выбрать |
| [docs/architecture.md](docs/architecture.md) | как устроен проект: модули, очередь, база данных |
| [docs/data-contract.md](docs/data-contract.md) | формат выгрузки для этапа 2 и как его читать |
| [docs/troubleshooting.md](docs/troubleshooting.md) | типичные ошибки и что с ними делать |
| [docs/integration.md](docs/integration.md) | связь с этапом 2 (Laravel): API, Docker, безопасность |
| [docs/legal.md](docs/legal.md) | на что обратить внимание по закону (реклама, персональные данные, лицензии) |

## Структура проекта

```
dgis/
  cli.py, config.py, logs.py     запуск, настройки, журнал
  storage.py                     база данных SQLite, очередь задач
  normalize.py, export.py        адреса сайтов и телефоны, выгрузка
  pipeline/                      очередь, воркеры, проверка сайтов, лимитер
  sources/                       Overture, 2GIS API, импорт CSV
  ui/                            веб-интерфейс
config.yaml                      настройки
config/presets.yaml              темы для выбора в UI
docs/                            документация
tools/                           диагностика: python tools/diagnose_site.py домен.ru
tests/                           тесты (python -m unittest discover -s tests)
data/, logs/                     база, кэш Overture и журналы (создаются сами, в git не попадают)
```

## Статус и что дальше

Готово (beta): сбор из Overture / 2GIS API / CSV, проверка сайтов, UI, экспорт, возобновление после остановки.
Не сделано: расписания (APScheduler), сбор через браузер (Playwright), ротация прокси, дедуп организаций по телефону,
стыковка с анализатором (этап 2).
