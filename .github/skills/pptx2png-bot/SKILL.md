---
name: pptx2png-bot
description: "Develop and troubleshoot the PPTX2PNG Telegram bot: file and URL conversion, user settings, Yandex Disk /sunday categories and uploaded-file fallback, administrator folder setup, tests and deployment."
---

# PPTX2PNG Telegram Bot

Use this skill for changes to the Telegram bot. Treat the current source and
tests as authoritative; consult [ARCHITECTURE.md](../../../ARCHITECTURE.md),
[BOT_USER_FLOWS.md](../../../BOT_USER_FLOWS.md), and
[bot/README.md](../../../bot/README.md) for the current design and setup.
For a new computer/chat, use the
[initialization prompt](../../prompts/initialize-project.prompt.md); do not
assume previous session paths, credentials or running services exist.

## Code map

- `bot/bot.py`, `bot/__main__.py`: startup, configuration, polling/runtime.
- `bot/handlers.py`: uploaded files, URLs, slide-range input, conversion and
  user-facing settings.
- `bot/yandex_flow_core.py`, `bot/yandex_flow_handlers.py`, `bot/yandex_flow.py`:
  Yandex Disk workflow, `/sunday`, category detection and selection, conversion,
  prompts, cleanup and watchdogs.
- `bot/yandex_state.py`, `bot/user_manager.py`: in-memory task/session state,
  locking, access and user preferences.
- `bot/folder_setup.py`: scheduled and administrator-triggered creation of the
  upcoming Sunday's folder tree.
- `bot/utils.py`: bot-specific download, extraction and conversion helpers.
- `core/pptx2png_core/`: shared converter, Yandex Disk client, slide analysis,
  file structure and speaker-note handling. Coordinate changes here with the
  local OBS mode.

## Current interaction constraints

- Ordinary conversion accepts PPT/PPTX, ZIP and supported public links; users
  can convert all slides or enter slide numbers/ranges.
- `/sunday` searches the configured Yandex Disk structure for the upcoming
  Sunday, lets the user select files, analyzes slide categories, and uploads
  selected results back to Disk.
- If the Sunday/target folder structure exists but contains no presentations,
  `/sunday` can wait for an uploaded PPT/PPTX from the initiating user. Preserve
  ownership, `prompt_timeout_sec`, nearest-Sunday rollover and cancellation;
  expired waits return uploads to ordinary conversion.
- `/start` administrator tools can create missing Sunday folders after
  confirmation using `bot/template.yaml`. `[FolderSetup]` also controls startup
  catch-up and Monday scheduling with retries. This is folder creation, not
  automatic monitoring/conversion of new meeting programs.
- Category ranges can be manually edited through the single
  `✏️ Изменить диапазон` button, then category selection and text input.
  Preserve ownership checks, prompt nonces, timeouts, cancellation and the
  return to the category menu when changing this flow.
- Pending tasks and Yandex sessions are in process memory and are not restored
  after restart. Do not assume the bot is multi-process or horizontally
  scalable.

## Change and validation procedure

1. Trace the user action through its handler, state changes, callbacks and
   completion/cancellation paths before editing.
2. Preserve authorization and ownership checks; validate callback payloads and
   user input at the boundary. Treat external URLs and downloaded content as
   untrusted.
3. Make focused changes and update `BOT_USER_FLOWS.md` when user-visible
   behavior or menus change.
4. Run focused tests if available, then at minimum compile changed Python
   modules and run `git diff --check`. Use the repository's configured Python
   environment; do not install dependencies unless a required validation tool
   is missing.
   Existing tests are under `bot/tests`; for these scenarios use:

   ```bash
   python -m unittest bot.tests.test_sunday_upload bot.tests.test_admin_tools bot.tests.test_folder_setup bot.tests.test_cleanup -q
   ```

   Use synthetic fixtures/mocked APIs. For Linux RAM-disk validation set
   `TMPDIR=/dev/shm`; elsewhere use the platform temporary directory.
5. For bug reports, use the `evidence-driven-debugging` skill: establish the
   failing path and verify the fix rather than inferring from logs alone.

## Configuration and operational safety

- Non-secret bot settings are in `bot/settings.ini`; secrets belong in
  `bot/config.ini` or the supported legacy root `config.ini`.
- Never inspect, print, copy, or commit live tokens, access lists, user state,
  real presentations, or logs containing personal data.
- Use the existing deployment workflow and `manage.sh` conventions; do not
  restart production services or change deployment configuration unless asked.
- GitHub push deployment currently targets `test` and `main`, not `dev`.
  Confirm the [workflow](../../workflows/deploy.yml) before publishing.
  Prefer `gh`; inspect actual Greptile comments as well as check conclusions
  before declaring a requested review clean. Never force-push to synchronize
  branches or merge a PR unless requested.
- Do not create branches, commits, review reports or extra planning documents
  without an explicit request.
