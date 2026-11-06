# pptx2png

> English version: [README.md](README.md). Keep both READMEs aligned when the
> project documentation changes.

Репозиторий организован как общий core плюс два рантайма:

- `core/` — повторно используемый код для конвертации PPTX, разбора заметок и работы с Яндекс.Диском.
- `bot/` — Telegram-бот.
- `obs_local/` — локальный интерактивный рабочий процесс подготовки PNG-слайдов для OBS.

## Установка

Из корня репозитория:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ./core
python -m pip install -r bot/requirements.txt
```

Только для рабочего процесса OBS установите зависимости core и локального режима:

```bash
python -m pip install -e ./core
python -m pip install -r obs_local/requirements.txt
```

## Запуск

```bash
python -m bot
python -m obs_local
```

Бота также можно запускать и управлять им через `./manage.sh start`,
`./manage.sh stop` и `./manage.sh status`. Корневой запуск `run_obs_local.py`
по-прежнему доступен для совместимости.

## Конфигурация

Неконфиденциальные параметры находятся в `bot/settings.ini` и `obs_local/settings.ini`.
Создайте `bot/config.ini` и/или `obs_local/config.ini` для учётных данных. Обе
приложения также поддерживают устаревший корневой `config.ini` в период перехода на
новую конфигурацию. Не коммитьте учётные данные.

> Английская версия: [README.md](README.md). При изменениях в документации проекта
> обновляйте и этот файл, чтобы версии оставались синхронными.

## Документация

- [Архитектура проекта](ARCHITECTURE.md)
- [Руководство пользователя Telegram-бота](BOT_USER_FLOWS.md)
- [Локальная подготовка материалов для OBS на русском](obs_local/README.RU.md)
- [Локальная подготовка материалов для OBS на английском](obs_local/README.md)
- [Дорожная карта проекта](promt.md)
