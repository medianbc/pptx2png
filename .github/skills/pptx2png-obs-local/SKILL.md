---
name: pptx2png-obs-local
description: "Develop and troubleshoot local PPTX2PNG for OBS: numeric menus, presentation and Sunday song preparation, DOCX recovery, Yandex Disk search, PNG ZIP cache, OS configuration, macOS installation and safe updates. Use when continuing OBS work on another computer."
---

# PPTX2PNG Local OBS Tools

Use this skill for local, non-Telegram preparation of OBS materials. The source
of truth is the current implementation; consult
[ARCHITECTURE.md](../../../ARCHITECTURE.md),
[obs_local/README.md](../../../obs_local/README.md),
[obs_local/README.RU.md](../../../obs_local/README.RU.md), and the
[operator guide](../../../obs_local/USER_GUIDE.RU.md) before changing behavior.
Use [promt.md](../../../promt.md) for planned development, not as proof that a
feature exists. For a new checkout/chat, use the
[initialization prompt](../../prompts/initialize-project.prompt.md).

## Code map

- `obs_local/app.py`: interactive local preparation of presentation slides.
- `obs_local/menu.py`: main, songs, settings and update menus.
- `obs_local/__main__.py`, `obs_local/run_obs_local.py`,
  `run_obs_local.py`: supported launch entry points and compatibility wrappers.
- `obs_local/prepare_songs.py`: DOCX program parsing, song lookup, PNG rendering,
  output folder publication and optional local cache population.
- `obs_local/config_utils.py`: OS settings selection, one-time migration and
  comment-preserving atomic INI updates.
- `obs_local/settings.ini`: initial template/legacy migration source, not the
  active settings after migration.
- `obs_local/updater.py`: Git/archive updates, dependency validation, rollback
  and preservation of local data.
- `install_obs_local.sh`, `install_obs_local_macos.sh`: Linux and macOS setup.
- `core/pptx2png_core/`: shared conversion and Yandex Disk implementation;
  changes here can also affect the Telegram bot.

## Current workflow and behavior

- `python -m obs_local` opens the main menu: `1` presentations, `2` songs,
  `3` updates, `4` settings, `0`/Enter exit.
- The songs submenu offers `1` read program, `2` update local song library,
  `0`/Enter back. Library updates work independently of the meeting program:
  download missing configured remote materials and create PNG ZIP cache,
  skipping existing valid cache and reporting partial failures.
- `python -m obs_local.prepare_songs` processes song titles from the configured
  public DOCX program for the nearest Sunday; `--program-file` and `--date`
  support a local program and explicit date.
- Program download failures (including HTTP 429, network errors and invalid
  DOCX) offer `1` retry, `2` local DOCX, `3`/Enter cancel to the songs submenu.
  Empty local-path input returns to this choice; EOF cancels. Both saved and
  newly entered URLs use the same recovery path. Save a URL only after valid
  download; local fallback must not persist an unverified URL.
- Song cancellation uses `prepare_songs.OperationCancelled`; both the menu and
  direct CLI handle it normally without an error or traceback. Presentation
  recovery keeps its own `1` retry, `2` local file, `0` cancel numbering.
- Song search prioritizes local PNG ZIPs, then local PPTX, then configured
  Yandex Disk ZIP/PPTX sources. Matching supports strong partial title matches;
  ambiguous candidates are not selected automatically.
- Title normalization uses NFC, case folding, `ё` to `е`, and punctuation
  removal. Saved aliases remain compatible: current normalized key first,
  then legacy original and legacy NFD keys. Do not discard old alias mappings
  when changing normalization.
- Missing matches produce folders marked `(не найдено)`.
- Rendered local/remote material may be cached as PNG ZIPs in the configured
  local ZIP library. The broader remote cache scan is opt-in through
  `cache_missing_yandex_song_zips`.
- Publishing replaces the current song-output folder while retaining a
  timestamped backup. Preserve that safety behavior unless explicitly changing
  retention policy.
- OBS source selection is still manual; automatic scene/configuration
  integration is a future task, not implemented behavior.

## Configuration and safety

- Active non-secret settings are independent `obs_local/settings.linux.ini`,
  `settings.macos.ini`, and `settings.windows.ini`. `get_settings_path` selects
  the OS and migrates the legacy template only when that OS file is absent.
  Preserve values/comments and custom log paths; do not merge OS files at
  runtime. These files are Git-ignored and preserved by updates.
- Presentation token lookup prefers `obs_local/config.ini`, then root
  `config.ini`; song lookup prefers root, then local. The settings action
  writes the private root file and updates a local override if present.
  Preserve these existing precedence rules; never expose or commit secrets.
- Song logs are `obs_local.log` (INFO+) and `debug.log` (DEBUG+); normal console
  is WARNING+, `--debug` enables DEBUG. Default directories:
  Linux `/dev/shm/pptx2png_tasks/obs_local/logs`, macOS
  `~/Library/Logs/pptx2png/obs_local`, Windows
  `%LOCALAPPDATA%/pptx2png/obs_local/logs` with a home AppData fallback.
  The old Linux default is replaced on migration to another OS; custom paths
  are retained, so verify them on a new computer. Do not delete old logs.
- The configured program URL must be a public Yandex Disk file link accepted by
  the public-resource API; a Yandex document viewer URL is not necessarily an
  API public key.
- Public program download still uses the public-resource API. A direct
  `?download=1` page can return HTML CAPTCHA with HTTP 200, not DOCX; validate
  content and do not bypass CAPTCHA.
- macOS installation reuses compatible Python 3.10+ and LibreOffice before
  requesting Homebrew installation. Controlled PATH/fake-brew tests cover
  old/shadowed Python and custom LibreOffice. The converter honors
  `PPTX2PNG_LIBREOFFICE_PATH`; installers persist it in generated launchers.
- Treat DOCX, ZIP, PPTX and remote metadata as untrusted. Preserve size/count
  limits, archive path checks, content validation and temporary-file cleanup
  when changing download or extraction behavior.
- Resolve relative configured paths consistently with the existing
  implementation. Do not alter real OBS output or song libraries during tests.
- Updates must preserve all OS settings, secrets, launchers and material
  libraries; retain fast-forward guards, source/dependency rollback and
  restoration of staged and unstaged changes.

## Change and validation procedure

1. Identify whether the change affects the interactive CLI, song-preparation
   script, installer, shared core, or more than one of these.
2. Keep OBS behavior separate from bot-only state and dependencies. For shared
   core changes, check both callers where practical.
3. On another computer, locate a compatible Python environment and check
   dependencies first. Never reuse a previous session's absolute venv path.
   Do not run a production installer or start workflows merely to initialize
   a development chat.
4. Use synthetic fixtures; do not access live Yandex Disk or overwrite OBS
   directories during routine validation. On the Linux RAM-disk environment,
   set `TMPDIR=/dev/shm`; on macOS/Windows use that platform's temporary
   directory (CI uses `runner.temp`). Do not hard-code Linux paths in tests
   intended for macOS.
5. Run the smallest relevant selectors together, for example:

   ```bash
   python -m unittest obs_local.tests.test_prepare_songs obs_local.tests.test_menu -q
   ```

   For a full OBS regression check:

   ```bash
   python -m unittest discover -s obs_local/tests -q
   python -m obs_local.sync_readme_ru --check
   git diff --check
   ```

   Compile changed Python modules as well. The
   [macOS CI](../../workflows/test-obs-macos.yml) covers installation and
   synthetic PPTX rendering; CI success does not prove behavior on a user's Mac.
6. For external-service failures, distinguish local code errors from Yandex
   API availability, permissions, CAPTCHA responses and invalid public links.
   Use `evidence-driven-debugging` when diagnosing runtime failures.

## Documentation and scope

- Keep both OBS READMEs aligned for installation/configuration changes; update
  the Russian operator guide for menus and everyday behavior.
- Update `ARCHITECTURE.md` when module boundaries or the shared-core design
  changes.
- Do not create extra reports, branches or commits unless requested.
