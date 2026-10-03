# Local OBS workflow

This package downloads a presentation from Yandex Disk, lets the operator
select a category and slide range, and writes PNG files to the configured
local OBS directory. Conversion and Yandex Disk operations are shared with the
bot through `core`.

## Install and run

From the repository root:

```bash
python -m pip install -e ./core
python -m pip install -r obs_local/requirements.txt
python -m obs_local
```

Alternatively, use the compatibility launcher `python run_obs_local.py` or
`python -m obs_local.run_obs_local`.

## Configuration

- `obs_local/settings.ini` contains local-mode options and its own
  `root_path`.
- Create `obs_local/config.ini` with a `[YandexDisk]` section and `token`.
  If absent, the legacy repository-root `config.ini` is used.
- Relative output paths are resolved from the repository root.
