# Архитектура проекта PPTX2PNG Bot

**Версия:** 2.0
**Дата обновления:** 23.09.2026
**Статус:** актуально

---

## 1. Общее назначение

Telegram-бот для подготовки презентаций к церковным трансляциям.

**Основные задачи:**
1. Конвертация PPTX/PPT/ZIP → PNG с выбором качества, dark mode, ZIP-упаковкой.
2. Подготовка трансляций с Яндекс.Диска (`/sunday`).
3. Проверка орфографии через Яндекс.Спеллер.

---

## 2. Топология окружений

| Окружение | Путь | Репозиторий | Ветка | Назначение |
|-----------|------|-------------|-------|-----------|
| **Development** | `dev/` | `pptx2png-public-dev` (публичный) | `main` | Песочница для разработки, без секретов |
| **Testing** | `test/` | `pptx2png` (приватный) | `test` | Интеграционные проверки |
| **Production** | `prod/` | `pptx2png` (приватный) | `main` | Боевой токен, живые пользователи |

**Деплой:**
- `dev` → ручной перенос через `git merge`.
- `test` и `prod` → автоматический через GitHub Actions Self-Hosted Runner.

---

## 3. Изоляция и пути в RAM

* `ENV_NAME` определяется из имени папки (`dev` / `test` / `prod`).
* Временные файлы задач: `/dev/shm/pptx2png_tasks/{ENV_NAME}/`.
* ZIP-архивы (при упаковке): `/tmp/pptx2png_yd_task_*` — **disk-backed**, чтобы не переполнять RAM.
* Логи: `/dev/shm/pptx2png_tasks/{ENV_NAME}/logs/`.
  * `bot.log` — INFO и выше.
  * `debug.log` — DEBUG и выше.
  * `sys_nohup.log` — STDOUT процесса.
* SD-карта защищена от износа (логи и временные — в RAM).

---

## 4. Компоненты системы

| Модуль | Назначение |
|--------|-----------|
| `bot.py` | Точка входа: конфиг, логи, Dispatcher, cleanup_loop |
| `handlers.py` | Telegram-роутер (обычная конвертация, файлы, ссылки, админ) |
| `yandex_flow.py` | Yandex-оркестрация (`/sunday`, picker, промпты, upload) |
| `yandex_state.py` | Глобальное состояние Yandex (config, sessions, locks) |
| `yandex_disk.py` | Асинхронный клиент REST API Яндекс.Диска |
| `converter_engine.py` | PPTX → PDF → PNG, dark mode, ZIP |
| `utils.py` | Извлечение текста, spellcheck, core_pipeline, speaker notes |
| `structure.py` | Работа с `template.yaml` (для CLI-скрипта) |
| `sermon_detector.py` | Поиск диапазона проповеди в заметках докладчика |
| `user_manager.py` | Настройки пользователей (quality, keep_pdf) |
| `manage.sh` | Управление процессом (start/stop/restart/logs/status) |

---

## 5. Слои и взаимодействие

┌──────────────────────────────────────────────────┐
│ bot.py │
│ (init, config, dispatcher, cleanup_loop) │
└──────────────────┬───────────────────────────────┘
│ include_router
┌──────────────────▼───────────────────────────────┐
│ handlers.py + yandex_flow.py │
│ (все Telegram-хендлеры) │
└──────────────────┬───────────────────────────────┘
│
┌──────────────────▼───────────────────────────────┐
│ yandex_state.py (глобальные sessions, locks) │
└──────────────────┬───────────────────────────────┘
│
┌──────────────────▼───────────────────────────────┐
│ yandex_disk.py + converter_engine.py + utils.py │
│ (интеграции) │
└──────────────────────────────────────────────────┘


---

## 6. Ключевые технические решения

### 6.1. Хранение сессий

В памяти, в `yandex_state.sessions`:

* Обычные задачи: `task_<chat>_<user>_<msg>_<hex>`.
* Yandex picker: `yd_<user>_<chat>`.
* Yandex задачи: `yd_task_<user>_<hex>`.

**Ограничение:** при рестарте бота незавершённые задачи теряются.

### 6.2. Блокировки

* `task_lock_manager` (`TaskLockManager`) — защита от дублей для обычных задач.
* `yd_session_lock` — глобальный lock для Yandex-сессий.
* `yd_active_sessions` — nonce-защита picker-сессий.
* `yd_active_tasks` — защита активных задач от cleaner.

### 6.3. Таймауты (из `settings.ini`)

| Параметр | Значение | Назначение |
|----------|----------|-----------|
| `prompt_timeout_sec` | 1800 | Ожидание ответа на промпт проповеди |
| `cleanup_interval_sec` | 300 | Интервал проверки cleaner |
| `cleanup_max_age_sec` | 7200 | Удаление папок старше этого возраста |

### 6.4. ZIP-загрузка на Яндекс.Диск

* PNG не загружаются по отдельности — только ZIP-архивы.
* Два архива на презентацию:
  * `*_проповедь.zip` → `проповедь - png/`.
  * `*_слайды.zip` → `pptx2png/<file_slug>/`.
* Временная папка для ZIP — `/tmp/pptx2png_yd_task_*` (disk-backed).

### 6.5. Идемпотентная очистка

`_yd_cleanup_task` безопасна при многократном вызове:
* Удаляет `task_dir` (`/dev/shm`).
* Убирает задачу из `sessions`.
* Сбрасывает `processing` в picker-сессии.
* Снимает `yd_active_tasks`.
* Освобождает `yd_release` (nonce-safe).

---

## 7. CI/CD (GitHub Actions)

### 7.1. Self-Hosted Runner

Работает на Raspberry Pi. Настроен как systemd-сервис.

**Что делает:**
- Для ветки `test`: `rsync` кода → `./manage.sh restart` → сбор логов в artifacts.
- Для ветки `main`: `rsync` кода → `./manage.sh restart` → проверка статуса.

### 7.2. Snapshot PDF (новое)

При создании релизного тега `v*.*.*`:
1. Генерируется PDF из `docs/SNAPSHOT.md`.
2. Файл сохраняется в `docs/snapshots/`.
3. Прикрепляется к GitHub Release как artifact.

См. `.github/workflows/snapshot.yml`.

---

## 8. Секреты и безопасность

### 8.1. .gitignore

Жёстко блокируются:
- `config.ini` (токены).
- `user_settings.json` (настройки пользователей).
- `*.log`.
- `__pycache__/`.

### 8.2. Изоляция путей

- `safe_filename()` — защита от path traversal.
- `validate_download_path()` — проверка нахождения в `task_dir`.
- `.owner` файл — `user_id:chat_id` для проверки прав.

### 8.3. Проверка доступа

- Whitelist в `user_manager.load_allowed_users()`.
- Админское одобрение через кнопки.

---

## 9. Мониторинг и логи

| Файл | Уровень | Назначение |
|------|---------|-----------|
| `bot.log` | INFO+ | Основные события, ошибки |
| `debug.log` | DEBUG+ | Детальная диагностика (HTTP-запросы, состояние) |
| `sys_nohup.log` | STDOUT | Вывод процесса |

**Просмотр:**
```bash
./manage.sh logs        # bot.log
./manage.sh debug-logs  # debug.log
./manage.sh status      # статус процесса

10. Структура каталогов

/app/pptx2png/
├── prod/                     # Production
│   ├── bot.py
│   ├── handlers.py
│   ├── yandex_flow.py
│   ├── yandex_state.py
│   ├── yandex_disk.py
│   ├── converter_engine.py
│   ├── utils.py
│   ├── manage.sh
│   ├── config.ini            # боевой токен (gitignored)
│   ├── settings.ini
│   ├── user_settings.json    # (gitignored)
│   └── .github/workflows/
├── test/                     # Testing
│   └── (то же самое, тестовый токен)
└── dev/                      # Development
    └── (публичная версия)

/dev/shm/pptx2png_tasks/
├── prod/
│   └── logs/
├── test/
│   └── logs/
└── dev/
    └── logs/

11. Известные ограничения
Ограничение	Причина
sessions в памяти	При рестарте теряются
Один процесс	Нет горизонтального масштабирования
Один Yandex-аккаунт	Multi-tenancy не реализован
Отмена upload	Догоняет на следующей проверке
Нет автотестов	Только ручное тестирование
12. Ссылки
Ресурс	Ссылка
Репозиторий (приватный)	github.com/medianbc/pptx2png
Репозиторий (dev)	github.com/medianbc/pptx2png-public-dev
Yandex.Disk API	yandex.ru/dev/disk/api/
aiogram 3	docs.aiogram.dev


---

### `SNAPSHOT.md` — источник для PDF

Это тот же документ, что ты уже видел как HTML, но в Markdown с pandoc-плейсхолдерами. Возьмём его из твоего HTML — секции 1–6 сохранены, плюс добавим обновления:

- **Раздел про deploy** — GitHub Actions.
- **Обновление статуса багов** — «reply-проверка удалена», «валидация timeout добавлена».
- **Метрики** — актуализировать.

Полный текст `SNAPSHOT.md` (генерируется в `docs/SNAPSHOT.md`):

```markdown
---
title: "PPTX2PNG Bot — Срез проекта"
date: "23.09.2026"
version: "1.9.0"
---

# PPTX2PNG Bot — Срез проекта

**Версия:** 1.9.0
**Дата среза:** 23.09.2026
**Ответственные:** Алексей Базров (заказчик), команда разработки

---

## 1. Общие сведения

| Параметр | Значение |
|----------|----------|
| Название проекта | PPTX2PNG Telegram Bot |
| Текущая версия | v1.9.0 |
| Дата среза | 23.09.2026 |
| Платформа | Raspberry Pi 4B, Debian GNU/Linux, Python 3.13.5 |
| Окружения | dev, test, prod |

### Краткое назначение

Telegram-бот для подготовки презентаций к церковным трансляциям:

1. **Конвертация** PPTX/PPT/ZIP → PNG (Standard/2K/4K, dark mode).
2. **Подготовка трансляций** с Яндекс.Диска (`/sunday`).
3. **Проверка орфографии** через Яндекс.Спеллер.

---

## 2. Серверная часть

### 2.1. Архитектура

Монолит, разделённый на слои:

- **`bot.py`** — точка входа, конфиг, dispatcher, cleanup_loop.
- **`handlers.py`** — Telegram-роутер (обычная конвертация).
- **`yandex_flow.py`** — Yandex-оркестрация.
- **`yandex_state.py`** — глобальное состояние.
- **`yandex_disk.py`** — клиент API.
- **`converter_engine.py`** — движок конвертации.
- **`utils.py`** — утилиты.

### 2.2. Технологический стек

| Компонент | Технология |
|-----------|-----------|
| Язык | Python 3.13.5 |
| Bot framework | aiogram 3.x |
| HTTP | aiohttp |
| PPTX | python-pptx |
| PDF/PNG | PyMuPDF (fitz), LibreOffice headless |
| Yandex API | REST API Яндекс.Диска v1 |
| Хранение настроек | JSON |
| Конфигурация | config.ini + settings.ini |

### 2.3. Окружения и деплой

| Окружение | Путь | Ветка | Деплой |
|-----------|------|-------|--------|
| dev | dev/ | main (public) | Ручной merge |
| test | test/ | test | GitHub Actions |
| prod | prod/ | main | GitHub Actions |

**CI/CD:** GitHub Actions Self-Hosted Runner на Raspberry Pi.

### 2.4. Реализованные модули

**Команды:** `/start`, `/sunday`, `/cancel_yd`.

**Callback (обычная конвертация):** `slides_all`, `slides_select`, `slides_convert`, `chk_spell`, `chk_conv`, `set_q_*`, `toggle_pdf`, `adm_*`.

**Callback (Yandex):** `yd_pick`, `yd_cancel`, `yd_sermon_ok`, `yd_sermon_edit`, `yd_sermon_skip`.

### 2.5. Модель данных

**В памяти:** `yandex_state.sessions` (task_*, yd_*, yd_task_*).

**На диске:** `user_settings.json`.

### 2.6. Внешние интеграции

| Сервис | Статус |
|--------|--------|
| Telegram Bot API | Готово |
| Yandex.Disk REST API | Готово |
| Яндекс.Спеллер | Готово |
| Google Docs | Готово |
| LibreOffice | Готово |

### 2.7. Техдолг

- `sessions` в памяти (теряются при рестарте).
- Нет автотестов.
- Параллельная загрузка ZIP не реализована.
- Отмена upload не мгновенная.

---

## 3. Клиентская часть

**Клиент — Telegram.** Отдельных web/mobile/desktop приложений нет.

### Реализованные сценарии

**Обычная конвертация:**
1. `/start` — приветствие + настройки.
2. Загрузка .pptx/.zip/ссылки.
3. Выбор диапазона.
4. Проверка орфографии (опционально).
5. Конвертация → ZIP + PDF.

**Yandex-трансляция (`/sunday`):**
1. Проверка Диска.
2. Список pptx на ближайшее воскресенье.
3. Выбор файла.
4. Download + convert + извлечение заметок.
5. Промпт подтверждения.
6. Upload ZIP в правильные папки.
7. Отчёт с ссылками.

---

## 4. Ключевые решения и статусы

### 4.1. Реализованный функционал

| Фича | Статус |
|------|--------|
| Конвертация PPTX/PPT/ZIP → PNG | Готово |
| Выбор диапазона слайдов | Готово |
| ZIP + PDF отправка | Готово |
| Проверка орфографии | Готово |
| `/sunday` | Готово |
| Определение проповеди | Готово |
| ZIP-загрузка на Диск | Готово |
| Watchdog таймаута промпта | Готово |
| Cleaner старых задач | Готово |
| Валидация timeouts | Готово |

### 4.2. Сознательно отложено

- Параллельная загрузка ZIP.
- Реестр истёкших промптов.
- Прогресс-бар.
- Multi-tenancy.
- Web-интерфейс.

### 4.3. Компромиссы

| Область | Компромисс |
|---------|-----------|
| Хранение сессий | В памяти |
| ZIP tempdir | `/tmp` (не в RAM) |
| Отмена upload | Не мгновенная |
| Cleaner | В боте, не отдельным сервисом |

### 4.4. Открытые вопросы

| Вопрос | Приоритет |
|--------|-----------|
| Нет автотестов | Medium |
| Медленный upload на Pi | Medium |
| Метрики и алертинг | Medium |

---

## 5. Приложения

### 5.1. Схема `/sunday`

(см. `docs/ARCHITECTURE.md`, раздел 5.1)

### 5.2. Метрики

| Метрика | Значение |
|---------|----------|
| `handlers.py` | ~1100 строк |
| `yandex_flow.py` | ~1500 строк |
| `yandex_disk.py` | ~400 строк |
| Пользователей | 1 |
| Среднее время (186 слайдов) | ~3.5 минуты |

---

## 6. Резюме

**Проект в рабочем состоянии.** Все критичные баги закрыты.

**Ближайшие задачи:**
1. Добавить автотесты (smoke-test).
2. Ускорить upload (параллельная загрузка).
3. Настроить метрики.

