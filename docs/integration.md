# Интеграция с этапом 2 (Laravel)

Два контейнера в одной docker-сети. Этап 1 (`leadgen`) отправляет сайты по внутреннему API, этап 2 (`backend`) ставит их в очередь,
проверяет и хранит результаты. Базы раздельные: этап 1 не знает схему базы Laravel.

```
leadgen (SQLite, том) ──POST /api/internal/lead-targets──► backend ──► очередь `leads` ──► ScanJob
        ▲                                                      │
        └────────GET /api/internal/lead-targets/{domain}───────┘ (статус)
```

## Команды этапа 1
```
python -m dgis submit [--limit N]   # отправить новые живые сайты (alive, analysis=new)
python -m dgis sync                 # обновить статусы отправленных
```
То же кнопками на вкладке «База». Настройки: `LEADGEN_API_URL`, `LEADGEN_TOKEN` в `.env`; `leadgen_batch_size` в `config.yaml`.

## Контракт API
Авторизация: `Authorization: Bearer <LEADGEN_TOKEN>`. Только внутри docker-сети (см. «Безопасность»).

**Отправка пакета** `POST /api/internal/lead-targets` (до 50 элементов):
```json
{"batch_id": "uuid", "items": [{"domain": "a-clinic.ru", "target_url": "https://a-clinic.ru/",
  "business_names": ["Клиника А"], "city": "Казань", "rubric": "dental_clinic",
  "phones": ["+78432452112"], "scan_hint": "ok"}]}
```
Ответ `202`:
```json
{"batch_id": "uuid", "results": [
  {"domain": "a-clinic.ru", "status": "queued", "scan_id": "123"},
  {"domain": "b.ru", "status": "duplicate", "scan_id": "98"},
  {"domain": "c", "status": "rejected", "reason": "invalid_url"}]}
```
`status` в ответе: `queued`, `waiting` (ждет лимита подписки владельца, запустится сам), `duplicate`, `rejected`.
**Статус** `GET /api/internal/lead-targets/{domain}` → `200 {"domain": "...", "status": "queued|scanning|done|blocked|failed"}` или `404`.

Правила:
- Идемпотентность по домену: повторная отправка не создает вторую проверку (`duplicate`).
- `429`/`503` с `Retry-After`: клиент ждет и повторяет (до 3 раз).
- Внутренний API (сервисный токен) без лимита; лимит подписки применяется к импорту из админки.
- `scan_hint`: `ok` или `protected` (сайт жив, но не пускает ботов). Решение о нарушениях принимает только этап 2.
- Заглушки, капчи и блокировки этап 2 помечает `blocked` и не выдает по ним нарушений.

## Статусы в этапе 1 (`sites.analysis_status`)
`new → submitted → done | blocked | failed`; `rejected` (этап 2 отклонил адрес, причина в `scan_status`).
При ошибке связи отправка останавливается, неотправленные остаются `new`.

## Безопасность
- В вашем `docker-compose.yml` `ngrok` пробрасывает весь `backend:8000` в интернет, поэтому внутренние маршруты доступны и снаружи.
  Нужны: сервисный токен (сравнение `hash_equals`), проверка заголовка `Host` (`backend`, `backend:8000`), лимит запросов.
- Не публикуйте `postgres:5432` и `redis:6379` на хост в production, задайте пароль Redis.
- UI этапа 1 слушает `127.0.0.1:8100`; при `UI_HOST=0.0.0.0` без `UI_USER`/`UI_PASSWORD` он не запустится.

## Развертывание
1. Положите проект в `./leadgen/` рядом с `backend/` и `frontend/`.
2. Добавьте сервисы из `deploy/docker-compose.leadgen.yml` в `docker-compose.yml`, заполните `.env`.
3. `docker compose up -d --build leadgen queue_leads`.
