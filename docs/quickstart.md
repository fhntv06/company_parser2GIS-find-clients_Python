# Быстрый старт

## 1. Установка

**Windows (PowerShell):**
```powershell
winget install Python.Python.3.12        # если Python нет; затем откройте новое окно PowerShell
cd путь\к\проекту
py -m venv .venv
.venv\Scripts\Activate.ps1               # в начале строки появится (.venv)
python -m pip install -r requirements.txt
$env:PYTHONUTF8 = "1"                    # для кириллицы в консоли (в каждом новом окне)
```
Если `Activate.ps1` ругается на политику скриптов: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

**Linux / macOS:**
```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
```

Проверка: `python -m unittest discover -s tests` должно закончиться `OK`.

> Всегда работайте в активированном окружении `(.venv)`. Без него Python не увидит установленные пакеты.

## 2. Настройки

- `config.yaml`: параметры (параллельность, лимиты, путь к базе). Для первого запуска менять ничего не нужно.
- `.env` (нужен только для 2GIS API и защиты UI): скопируйте `.env.example` в `.env` и впишите `DGIS_KEY=...`.
  Файл `.env` не попадает в git; ключи не вставляйте в чаты и письма.

## 3. Первый сбор через UI

```powershell
python -m dgis ui
```
Откройте http://127.0.0.1:8000.

1. На главной выберите **тему** (или впишите свои подстроки) и введите **город**, например «Казань».
2. Нажмите **Собрать**. Первый запуск города скачивает данные Overture (несколько минут), повторные берут кэш.
3. Следите за блоком «Прогресс»: растут счётчики карточек, затем сайтов и «доступны».
4. Вкладка **База**: фильтры по городу, теме, доступности, поиск. Кнопки **CSV** и **JSONL** выгружают то, что отфильтровано.

Остановка и продолжение: «Пауза» / «Продолжить» / «Стоп». Если закрыть окно или перезагрузить компьютер,
при следующем `python -m dgis ui` прерванные задачи продолжатся сами.

## 4. То же из командной строки

```powershell
python -m dgis overture --city "Казань" --preset dental         # сбор по теме из config/presets.yaml
python -m dgis overture --city "Казань" --terms "dental,dentist"  # свои подстроки
python -m dgis overture --city "Казань" --list-categories       # какие категории есть в данных города
python -m dgis stats                                            # сводка по базе
python -m dgis export --format jsonl --out sites.jsonl --status alive
python -m dgis recheck                                          # перепроверить доступность всех сайтов
python -m dgis check etalon142.ru                               # проверить один сайт и увидеть причину отказа
```
Остальные команды: `import` (свой CSV), `run` (2GIS API), `probe` (что отдаёт ключ 2GIS), `retry-failed`, `ui`.
Полный список: `python -m dgis --help`.

## 5. Как выбрать тему

Темы лежат в `config/presets.yaml`. Подстроки ищутся в **категории** места (английской, например `dental_clinic`)
и в его **названии**. Проверена только тема `dental`; для остальных:

1. `python -m dgis overture --city "Казань" --list-categories` покажет реальные категории и их количество.
2. Поправьте `terms` в `config/presets.yaml`.

Не используйте слишком короткие подстроки: `dent` находит и `dental_clinic`, и `student_union`.

## 6. Другие города

Введите название города на главной странице. Рамка города определяется автоматически (OpenStreetMap Nominatim)
и запоминается. Если город найден неверно или нужна своя область, укажите рамку вручную: `xmin,ymin,xmax,ymax`
(долгота, широта, долгота, широта), например `48.9,55.65,49.35,55.9` для Казани.
Слишком большие области (целые регионы) система отклоняет: скачивание заняло бы часы.
