# Telegram bot

This package contains the Telegram runtime, handlers, user state, and bot-specific
utilities. Conversion and Yandex Disk client code are provided by `core`.

## Install and run

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ./core
python -m pip install -r bot/requirements.txt
python -m bot
```

`manage.sh` starts the same package entry point and includes the local `core`
directory on `PYTHONPATH` for existing server environments.

## Configuration

- `bot/settings.ini` contains non-secret bot settings.
- Create `bot/config.ini` for secrets; if it is absent, the legacy
  repository-root `config.ini` is used.
- `bot/template.yaml` is the Yandex Disk folder template.
- `[FolderSetup]` in `bot/settings.ini` controls automatic folder creation.
  When enabled, the bot checks the configured tree at startup (to catch up on a
  missed run) and at Monday midnight in the server's local time for the
  upcoming Sunday. Failed attempts are retried at `retry_interval_sec`; the
  configured Yandex Disk base folder must already exist.
- The administrator can also start creation of the upcoming Sunday's missing
  folder tree from `/start` → `🛠 Доп. функции`; this manual action asks for
  confirmation and uses the same `template.yaml`.
- User allowlists and preferences remain in the repository root to preserve
  existing deployment state.

## User guide

See [BOT_USER_FLOWS.md](../BOT_USER_FLOWS.md) for the Russian user guide,
commands, menus, slide-range selection, `/sunday` and common errors.
