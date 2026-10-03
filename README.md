# pptx2png

The repository is organized as a shared core plus two runtimes:

- `core/` — reusable PPTX conversion, notes parsing, and Yandex Disk code.
- `bot/` — Telegram bot.
- `obs_local/` — local interactive workflow that prepares PNG slides for OBS.

## Setup

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ./core
python -m pip install -r bot/requirements.txt
```

For only the OBS workflow, install the core and local requirements:

```bash
python -m pip install -e ./core
python -m pip install -r obs_local/requirements.txt
```

## Run

```bash
python -m bot
python -m obs_local
```

The bot can also be started and managed with `./manage.sh start`,
`./manage.sh stop`, and `./manage.sh status`. The `run_obs_local.py` root
launcher remains available for compatibility.

## Configuration

Non-secret options live in `bot/settings.ini` and `obs_local/settings.ini`.
Create `bot/config.ini` and/or `obs_local/config.ini` for credentials. Both
applications also accept the legacy root-level `config.ini` during the
configuration transition. Do not commit credentials.
