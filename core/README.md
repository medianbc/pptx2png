# Shared core package

This is the installable package shared by the Telegram bot and the local OBS
workflow.

```text
core/
├── pyproject.toml
└── pptx2png_core/
    ├── __init__.py
    ├── converter_engine.py
    ├── sermon_detector.py
    ├── utils.py
    ├── yandex_disk.py
    └── structure.py
```

Install for local development with `python -m pip install -e ./core`.
Dependencies required by the shared modules are declared in `pyproject.toml`.

`utils.py` intentionally contains only presentation-note extraction that is
shared by both applications. Telegram-specific helpers remain in `bot/utils.py`.
