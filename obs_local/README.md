# Local OBS workflow module

This folder is reserved for the local/manual presentation preparation flow.

## Purpose
- support OBS-oriented local rendering
- keep local UI/selection logic separate from the bot
- allow manual range selection and local file handling

## Suggested layout

```text
obs_local/
├── README.md
├── obs_local.py
├── run_obs_local.py
├── settings.ini
├── requirements.txt
└── .github/workflows/
```

## Dependency rule

The local mode must reuse the same core package as the bot, not duplicate conversion logic.
