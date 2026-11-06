---
name: pptx2png-obs-local
description: "Develop and troubleshoot the local PPTX2PNG workflows for OBS, including interactive slide preparation, Sunday song preparation, Yandex Disk search, PNG ZIP caching, installation and configuration."
---

# PPTX2PNG Local OBS Tools

Use this skill for local, non-Telegram preparation of OBS materials. The source
of truth is the current implementation; consult
[ARCHITECTURE.md](../../../ARCHITECTURE.md),
[obs_local/README.md](../../../obs_local/README.md), and the relevant settings
template before changing behavior.

## Code map

- `obs_local/app.py`: interactive local preparation of presentation slides.
- `obs_local/__main__.py`, `obs_local/run_obs_local.py`,
  `run_obs_local.py`: supported launch entry points and compatibility wrappers.
- `obs_local/prepare_songs.py`: DOCX program parsing, song lookup, PNG rendering,
  output folder publication and optional local cache population.
- `obs_local/settings.ini`: non-secret local paths, output settings, song sources
  and optional caching behavior.
- `install_obs_local.sh`, `install_obs_local_macos.sh`: Linux and macOS setup.
- `core/pptx2png_core/`: shared conversion and Yandex Disk implementation;
  changes here can also affect the Telegram bot.

## Current workflow and behavior

- `python -m obs_local` runs the interactive slide-preparation CLI.
- `python -m obs_local.prepare_songs` processes song titles from the configured
  public DOCX program for the nearest Sunday; `--program-file` and `--date`
  support a local program and explicit date.
- Song search prioritizes local PNG ZIPs, then local PPTX, then configured
  Yandex Disk ZIP/PPTX sources. Matching supports strong partial title matches;
  ambiguous candidates are not selected automatically.
- Missing matches produce folders marked `(не найдено)`.
- Rendered local/remote material may be cached as PNG ZIPs in the configured
  local ZIP library. The broader remote cache scan is opt-in through
  `cache_missing_yandex_song_zips`.
- Publishing replaces the current song-output folder while retaining a
  timestamped backup. Preserve that safety behavior unless explicitly changing
  retention policy.

## Configuration and safety

- `obs_local/settings.ini` contains local paths and workflow settings.
- The Yandex Disk token is read from the project-root `config.ini`, with
  `obs_local/config.ini` as a fallback. Never expose or commit either secrets
  file.
- The configured program URL must be a public Yandex Disk file link accepted by
  the public-resource API; a Yandex document viewer URL is not necessarily an
  API public key.
- Treat DOCX, ZIP, PPTX and remote metadata as untrusted. Preserve size/count
  limits, archive path checks, content validation and temporary-file cleanup
  when changing download or extraction behavior.
- Resolve relative configured paths consistently with the existing
  implementation. Do not alter real OBS output or song libraries during tests.

## Change and validation procedure

1. Identify whether the change affects the interactive CLI, song-preparation
   script, installer, shared core, or more than one of these.
2. Keep OBS behavior separate from bot-only state and dependencies. For shared
   core changes, check both callers where practical.
3. Use synthetic fixtures for tests; do not access live Yandex Disk or overwrite
   OBS directories during routine validation. Put temporary test artifacts in
   `/dev/shm` on this machine, not `/tmp`.
4. Run focused tests, normally:

   ```bash
   python -m unittest obs_local.tests.test_prepare_songs -v
   ```

   Also compile changed Python modules and run `git diff --check`.
5. For external-service failures, distinguish local code errors from Yandex
   API availability, permissions, CAPTCHA responses and invalid public links.
   Use `evidence-driven-debugging` when diagnosing runtime failures.

## Documentation and scope

- Update `obs_local/README.md` for changes to installation, configuration,
  launch commands or song-preparation behavior.
- Update `ARCHITECTURE.md` when module boundaries or the shared-core design
  changes.
- Do not create extra reports, branches or commits unless requested.
