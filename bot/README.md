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
- User allowlists and preferences remain in the repository root to preserve
  existing deployment state.
