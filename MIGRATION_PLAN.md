# План миграции: общий core + bot + local OBS

## Состояние миграции

Основное разделение реализовано в этом репозитории:

- общие модули находятся в `core/pptx2png_core/` и устанавливаются как пакет
  `pptx2png-core`;
- приложение Telegram находится в `bot/` и запускается командой `python -m bot`;
- локальное приложение находится в `obs_local/` и запускается командой
  `python -m obs_local`;
- настройки бота и локального режима разделены, а чтение старого корневого
  `config.ini` сохранено как переходная совместимость;
- `manage.sh` запускает пакет бота и добавляет локальный `core` в `PYTHONPATH`.

Общие зависимости объявлены в `core/pyproject.toml`; зависимость бота находится
в `bot/requirements.txt`. Извлечение заметок слайдов относится к core,
Telegram-утилиты — к `bot/utils.py`.

## Цель

Разделить проект на три уровня ответственности:

- `core` — общая логика, которая используется и ботом, и локальным OBS-пайплайном
- `bot` — Telegram-бот, работа с пользователями и событиями
- `obs_local` — локальный запуск для подготовки PNG/слайдов без Telegram

Это позволяет сохранить единое ядро, но при этом развивать локальный сценарий отдельно и не разносить бота и local-mode по разным веткам как “fork”.

---

## Почему это правильная архитектура для этого проекта

Проверка по текущей структуре показывает, что часть кода уже является общим слоем:

- `converter_engine.py` — общий конвертер
- `sermon_detector.py` — разбор заметок и выбор диапазонов
- `utils.py` — общие функции подготовки и извлечения данных
- `yandex_disk.py` — клиент Яндекс.Диска
- `structure.py` — вспомогательная логика, пригодная для разных сценариев

Локальный режим в `obs_local.py` использует тот же набор функций, но добавляет:

- диалог выбора презентации
- ручный выбор диапазона
- подготовку папки под OBS
- вывод итогового каталога для локального использования

Поэтому локальный режим — это не отдельный “второй продукт”, а отдельный runtime entry point поверх общего ядра.

---

## Рекомендуемая структура после миграции

```text
pptx2png/
├── core/
│   ├── README.md
│   ├── pyproject.toml
│   └── pptx2png_core/
│       ├── __init__.py
│       ├── converter_engine.py
│       ├── sermon_detector.py
│       ├── utils.py
│       ├── yandex_disk.py
│       └── structure.py
│
├── bot/
│   ├── README.md
│   ├── requirements.txt
│   ├── __init__.py
│   ├── __main__.py
│   ├── bot.py
│   ├── handlers.py
│   ├── yandex_flow.py
│   ├── yandex_state.py
│   ├── yandex_flow_core.py
│   ├── yandex_flow_handlers.py
│   ├── user_manager.py
│   ├── utils.py
│   ├── settings.ini
│   └── template.yaml
│
├── obs_local/
│   ├── README.md
│   ├── requirements.txt
│   ├── __init__.py
│   ├── __main__.py
│   ├── app.py
│   ├── run_obs_local.py
│   └── settings.ini
│
├── .github/
│   └── workflows/
├── .gitignore
├── README.md
└── requirements.txt
```

---

## Что переносить в core

### Обязательно в общий пакет

- `converter_engine.py`
- `sermon_detector.py`
- `utils.py`
- `yandex_disk.py`
- `structure.py` (если используется и в боте, и в local mode)

### Оставить в bot

- `bot.py`
- `handlers.py`
- `yandex_flow.py`
- `yandex_flow_core.py`
- `yandex_flow_handlers.py`
- `yandex_state.py`
- `user_manager.py`
- `template.yaml`
- Telegram-сценарии и маршрутизация

### Оставить в obs_local

- `obs_local.py`
- `run_obs_local.py`
- локальные CLI-параметры
- подбор файла/диапазона/выходной директории

---

## Что именно поменять в импортировании

### До

```python
from converter_engine import convert_all_pngs, ppt_to_pptx_crossplatform
from sermon_detector import find_category_range
from utils import extract_speaker_notes
from yandex_disk import YandexDiskClient
```

### После

```python
from pptx2png_core.converter_engine import convert_all_pngs, ppt_to_pptx_crossplatform
from pptx2png_core.sermon_detector import find_category_range
from pptx2png_core.utils import extract_speaker_notes
from pptx2png_core.yandex_disk import YandexDiskClient
```

Это минимальный и безопасный способ переноса без ломки кода.

---

## Пошаговый план миграции

### Этап 1. Создать структуру ядра

Создать папку `core/` и пакет `pptx2png_core/`.

Внутри поместить только те модули, которые реально используются и в боте, и в local-mode.

### Этап 2. Протестировать пакет локально

Установить его в editable mode:

```bash
cd core
pip install -e .
```

Проверить, что импорт работает:

```bash
python -c "from pptx2png_core.converter_engine import convert_all_pngs; print('ok')"
```

### Этап 3. Перевести bot на общие импорты

Обновить импорты в файлах бота:

- `bot.py`
- `handlers.py`
- `yandex_flow.py`
- `yandex_flow_core.py`
- `yandex_flow_handlers.py`

Не трогать логику бизнес-процессов пока только меняем импорты.

### Этап 4. Перевести obs_local на общие импорты

Обновить `obs_local.py` и `run_obs_local.py` на импорт из `pptx2png_core.*`.

### Этап 5. Разделить конфиги

Сделать отдельные конфиги:

- `bot/settings.ini`
- `bot/config.ini` (если нужен)
- `obs_local/settings.ini`

Не держать секреты в общем ядре.

### Этап 6. Проверить оба entry point

Проверить:

```bash
python -m bot
python -m obs_local
```

Запуски требуют конфигурации и внешних сервисов; для проверки импортов
используйте отдельные команды установки и smoke-check ниже.

### Этап 7. Удалить дубликаты только после стабилизации

Только когда оба режима работают, можно удалить дублирующий код и оставить чистую shared core-модель.

---

## GitHub: как вести разработку

### Минимальный безопасный вариант

Не менять ветки, а держать всё в одном repo, но в отдельных каталогах.

Структура релизов:

- `main` — основной рабочий код
- `test` — проверка integration
- `prod` — только боевой бот

### Более зрелый вариант

Если проект начнёт расти, тогда логично вынести:

- `pptx2png-core` как отдельный репозиторий
- `pptx2png-bot` как отдельный deploy repo
- `pptx2png-obs-local` как отдельный runtime repo

Но это уже второй этап, а не стартовый шаг.

---

## Что делать на локальной машине

### 1. Создать venv

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Установить core в editable mode

```bash
cd core
pip install -e .
```

### 3. Установить зависимости для каждого режима

```bash
pip install -r bot/requirements.txt
pip install -r obs_local/requirements.txt
```

### 4. Запускать как отдельные процессы

```bash
python -m bot
python -m obs_local
```

---

## Пример `bot/requirements.txt`

```txt
python-telegram-bot>=20.0
aiohttp
python-dotenv
Pillow
python-pptx
```

## Пример `obs_local/requirements.txt`

```txt
aiohttp
python-pptx
Pillow
```

> Это пример шаблона; точный набор зависимостей лучше подтвердить по реальным импорту и runtime после проверки в проекте.

---

## Риски и как их избежать

### Риск 1. Перемешивание логики

Избежать: переносить только действительно общий код в `core`.

### Риск 2. Сломать бот в процессе рефакторинга

Избежать: сначала сделать `core`, затем перевести импорты, затем проверить бота.

### Риск 3. Дублирование зависимостей

Избежать: все общие пакеты держим только в `core`, а боту/obs_local — только runtime-specific зависимости.

### Риск 4. Секретные данные в shared module

Избежать: хранить токены и локальные config только рядом с entry point.

---

## Итоговая рекомендация

Самый безопасный путь для вас:

1. сделать `core/`
2. перенести общий код
3. переключить импорты в bot и obs_local
4. зафиксировать это в GitHub как единый repo с разными рабочими каталогами
5. только потом решать, делать ли отдельный repo для ядра

Так вы сохраняете связь с базовыми библиотеками, но не смешиваете архитектуру в одну несвязную ветку.

---

## Проверка после изменений

```bash
python -m pip install -e ./core
python -m pip install -r bot/requirements.txt
python -c "from pptx2png_core.converter_engine import convert_all_pngs"
python -m bot --help
python -m obs_local --help
```

Для полноценного запуска бота нужны секреты и доступ к Telegram; локальному
сценарию также нужны LibreOffice и доступ к Яндекс.Диску.
