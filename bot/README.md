# Bot module

This folder is reserved for Telegram bot runtime code.

## Purpose
- keep bot entry points isolated
- avoid mixing bot-only logic with shared conversion logic
- allow independent deployment and testing

## Suggested layout

```text
bot/
├── README.md
├── bot.py
├── handlers.py
├── yandex_flow.py
├── yandex_state.py
├── settings.ini
├── requirements.txt
└── .github/workflows/
```

## Dependency rule

The bot must depend on shared code from the core package, not reimplement conversion logic.
