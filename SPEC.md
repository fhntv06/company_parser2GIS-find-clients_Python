# SPEC: Этап 1 — «2GIS → база сайтов» (beta)

Цель: по списку городов и рубрик собрать карточки организаций из 2GIS, извлечь URL собственного сайта, сохранить в БД и отдать этапу 2 (анализатор сайтов).
Срок: beta за ~2 часа. Всё, что помечено **[v2]**, в бету не входит.

## 0. Правила для исполнителя (Claude Code)

1. Сначала напиши план и структуру проекта, потом реализуй по этапам.
2. После каждого этапа запускай тесты и мини-прогон (20 карточек).
3. Секреты только из `.env`. Ключ нигде не печатать, не логировать, не коммитить. Создать `.env.example` и `.gitignore` (`.env`, `*.db`, `logs/`).
4. Первый шаг, до любого кода: один тестовый запрос к API (см. п. 2.3) и вывод, какие поля реально пришли. Если `contact_groups` недоступен с демо-ключом, остановиться и сообщить.

## 1. Стек

| Слой | Выбор |
|---|---|
| Язык | Python 3.12 |
| HTTP, параллельность | asyncio + httpx |
| БД | SQLite (WAL) + aiosqlite, чистый SQL |
| Очередь | таблица `jobs` + asyncio-воркеры |
| Лимитер | token bucket (глобальный, на запросы к 2GIS) |
| UI | FastAPI + Jinja2 + HTMX + Pico.css (файлы локально, без CDN) |
| CLI | Typer |
| Конфиг | config.yaml + .env (pydantic-settings) |
| Утилиты | tldextract, tenacity |
| Качество | pytest, ruff |
| Запуск | tmux |
| **[v2]** | APScheduler, circuit breaker, `WebSource` (Playwright), ProcessPoolExecutor (`cpu_workers`), Postgres, Docker |

Не используем: Go/PHP, Celery/Redis, GPU, LLM в пайплайне, React/npm.

## 2. Источник данных: 2GIS Places API

### 2.1 Ключ
- Демо-ключ на 1 месяц, переменная `DGIS_KEY` в `.env`.
- Проверить условия ключа: лимиты запросов, максимальный `page_size`, право хранить данные в своей БД, доступные поля.

### 2.2 Интерфейс
```python
class Source(Protocol):
    async def search(self, city, rubric, area, page) -> Page: ...
    async def card(self, dgis_id) -> Card: ...   # если в search нет всех полей
```
Реализация беты: `ApiSource`. **[v2]** `WebSource`.

### 2.3 Эндпоинты (уточнить по документации 2GIS перед кодом)
- Поиск: `GET https://catalog.api.2gis.com/3.0/items?q=...&city_id=...&page=...&page_size=...&fields=items.contact_groups,items.rubrics,items.address_name,items.point&key=...`
- Города: определение `city_id`/`region_id` по названию через `GET /2.0/region/search` (или справочник).
- Сайт приходит в `contact_groups[].contacts[]` с `type == "website"` (поле `url`/`value`).
- Если выдача на запрос ограничена потолком, дробить область на квадраты/районы (`rect`/`point`+`radius`) и объединять результаты с дедупом по `id`.

## 3. Функциональные требования (beta)

1. **Сбор списка**: город × рубрика, пагинация, дробление области при упоре в потолок выдачи.
2. **Данные карточки**: `dgis_id`, название, адрес, город, рубрики, телефоны, сырые ссылки.
3. **Нормализация URL сайта**:
   - раскрыть редиректные ссылки 2GIS (`link.2gis.*`) без перехода по ним, где возможно (параметр с целевым URL), иначе одним HEAD/GET;
   - убрать `utm_*`, фрагменты, привести к `https://домен/`;
   - `domain` через tldextract (registered domain + поддомен, если это не `www`).
4. **Классификация `site_kind`**: `own` / `social` (vk, instagram, t.me, ok.ru, wa.me и т.п.) / `marketplace` (avito, yandex.ru/maps, flamp и т.п.) / `none`. Списки доменов в `config/domains.yaml`.
5. **Дедуп**: карточки по `dgis_id`, сайты по `domain` (связь многие-к-одному через `business_sites`).
6. **Проверка доступности** (`check_site`): GET с таймаутом 5 с, редиректы до 5, сохранить `http_status`, `final_url`. Лимит параллельности отдельный (по умолчанию 20), на 2GIS не влияет.
7. **Возобновляемость**: перезапуск продолжает с места остановки, без дублей.
8. **Ретраи и лимиты**: экспоненциальная пауза на 429/5xx (tenacity), глобальный token bucket на 2GIS.
9. **Экспорт**: CSV и JSONL по текущим фильтрам.

## 4. Схема БД

```sql
CREATE TABLE businesses(
  dgis_id TEXT PRIMARY KEY, name TEXT, address TEXT, city TEXT, rubric TEXT,
  phones TEXT /*JSON*/, raw_url TEXT, collected_at TEXT
);
CREATE TABLE sites(
  domain TEXT PRIMARY KEY, url TEXT, site_kind TEXT,
  http_status INT, final_url TEXT, checked_at TEXT,
  analysis_status TEXT DEFAULT 'new'      -- контракт с этапом 2: new -> analyzed
);
CREATE TABLE business_sites(dgis_id TEXT, domain TEXT, PRIMARY KEY(dgis_id, domain));
CREATE TABLE jobs(
  id INTEGER PRIMARY KEY, type TEXT,       -- collect_list | fetch_card | check_site
  payload TEXT /*JSON*/, priority INT DEFAULT 5,
  status TEXT DEFAULT 'queued',            -- queued|running|done|failed|cancelled
  attempts INT DEFAULT 0, max_attempts INT DEFAULT 5,
  run_after TEXT, locked_by TEXT, locked_at TEXT, error TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE INDEX ix_sites_kind ON sites(site_kind, analysis_status);
CREATE INDEX ix_biz_city_rubric ON businesses(city, rubric);
CREATE INDEX ix_jobs_status ON jobs(status, run_after, priority);
```
Дополнительно: таблица `runs` (id, config_snapshot, started_at, finished_at, status) для UI и статистики.

## 5. Очередь и воркеры (beta)

1. Воркер атомарно забирает задачу: `UPDATE jobs SET status='running', locked_by=?, locked_at=? WHERE id=(SELECT id FROM jobs WHERE status='queued' AND run_after<=now ORDER BY priority, id LIMIT 1) RETURNING *`.
2. `running` дольше N минут возвращаются в `queued` при старте и по таймеру.
3. Ретраи через `run_after`, после `max_attempts` → `failed`.
4. Задачи идемпотентны (upsert по `dgis_id`, `domain`).
5. Число воркеров = `concurrency` (по умолчанию 5). Любой `concurrency` не превышает глобальный rate limit.
6. **[v2]** circuit breaker, APScheduler и таблица `schedules`.

## 6. Конфиг

```yaml
cities: [Москва, Казань]
rubrics: ["Стоматологии", "Автосервисы"]
source: api            # api | web [v2]
concurrency: 5
check_concurrency: 20
rate_limit_rps: 3      # уточнить по лимитам ключа
page_size: 50          # уточнить максимум для демо-ключа
proxy: null
```
`.env`: `DGIS_KEY=`, `UI_USER=`, `UI_PASSWORD=`.

## 7. UI (FastAPI + HTMX)

| Экран | Содержимое |
|---|---|
| Запуск | города, рубрики (мультиселект/свободный ввод), concurrency; Старт / Пауза / Стоп. Ключ в UI не показывается |
| Прогресс | текущая задача, счётчики (карточек, с сайтом, own/social/none, ошибок), скорость/мин, ETA, лог последних 50 строк; HTMX polling раз в 2 с |
| База | таблица сайтов: домен, URL, название, город, рубрика, телефоны, `site_kind`, `http_status`, `analysis_status`; поиск, фильтры, сортировка, серверная пагинация (50 000+ строк) |
| Экспорт | CSV и JSONL по фильтрам, опция «только own и доступные» |
| Задачи | очередь по статусам, пауза/продолжить/отменить, «повторить упавшие» |

Требования: слушать `127.0.0.1` по умолчанию; для внешнего доступа HTTP Basic Auth из `.env` и reverse proxy/VPN; ключ и прокси не попадают в UI и логи. CLI и UI используют один `runner`.

## 8. Структура проекта

```
parser/
  cli.py            # Typer: run, ui, export, stats
  runner.py         # start/pause/stop, общий для CLI и UI
  config.py
  queue.py          # jobs, воркеры, ретраи
  limiter.py        # token bucket
  sources/
    base.py         # Source, Page, Card
    api.py          # ApiSource
  normalize.py      # URL, domain, site_kind
  checker.py        # check_site
  storage.py        # SQLite, upsert, миграции
  export.py
  ui/
    app.py
    templates/      # base, run, progress, db, jobs
    static/         # pico.css, htmx.min.js
tests/
config.yaml
config/domains.yaml
.env.example
README.md
```

## 9. Контракт с этапом 2

JSONL, одна строка на сайт:
```json
{"domain":"example.ru","url":"https://example.ru/","business_names":["..."],"city":"Казань","rubric":"Стоматологии","phones":["..."]}
```
Альтернатива: этап 2 читает `sites WHERE site_kind='own' AND http_status BETWEEN 200 AND 399 AND analysis_status='new'`.

## 10. Критерии приёмки beta

- [ ] Тестовый запрос с демо-ключом подтвердил наличие сайтов в ответе.
- [ ] Один город × одна рубрика обрабатываются без ручных действий.
- [ ] ≥90% карточек, у которых в 2GIS есть сайт, получили `url` и `domain`.
- [ ] Дублей по `domain` нет; повторный запуск не создаёт дублей и не перекачивает собранное.
- [ ] Остановка сервера посреди сбора и повторный запуск продолжают работу.
- [ ] UI: запуск/остановка, живые счётчики, фильтры, экспорт по фильтру.
- [ ] Экспорт JSONL читается этапом 2.
- [ ] Тесты на нормализацию URL, классификацию и очередь проходят.

## 11. План (≈2 часа)

| Время | Что |
|---|---|
| 0:00–0:10 | SPEC в проект, `.env`, тестовый запрос, проверка полей и лимитов ключа |
| 0:10–0:45 | `ApiSource`, пагинация, дробление области, `storage` |
| 0:45–1:10 | Нормализация, классификация, дедуп, `check_site` |
| 1:10–1:30 | Очередь, воркеры, лимитер, `runner`, экспорт, CLI |
| 1:30–1:55 | UI: запуск, прогресс, база, экспорт, задачи |
| 1:55–2:00 | Прогон: 1 город × 1 рубрика, правки по статистике |

Если время поджимает: срезать экран «Задачи» и ETA.

## 12. Вне беты

Расписания (APScheduler), circuit breaker, `WebSource`/Playwright, прокси-ротация, Postgres, Docker, email/ИНН/отзывы.

## 13. Риски

- Демо-ключ на 1 месяц: условия хранения данных и лимиты проверить до массового сбора; для продакшена нужен платный тариф.
- Потолок выдачи на запрос: решается дроблением области.
- Часть карточек без сайта или только с соцсетями: нормально, фильтруется по `site_kind`.
- Дальнейшее использование контактов для рассылки регулируется 38-ФЗ «О рекламе» (согласие адресата); заложить в этапе 3 реестр источника и отказов.