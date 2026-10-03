# Core package proposal

This directory is a staging area for extracting the shared business logic used by both the Telegram bot and the local OBS workflow.

## Purpose

Move shared logic here:
- converter_engine
- sermon_detector
- utils
- yandex_disk
- reusable config helpers

## Target layout

```text
core/
├── pyproject.toml
├── README.md
└── pptx2png_core/
    ├── __init__.py
    ├── converter_engine.py
    ├── sermon_detector.py
    ├── utils.py
    ├── yandex_disk.py
    └── ...
```

## Migration plan

1. Keep the current repo working.
2. Copy only reusable modules into the package.
3. Update imports in bot and obs_local to use the package.
4. Remove duplicate code only after both entry points work.
