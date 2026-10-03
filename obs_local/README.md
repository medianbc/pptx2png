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
  `root_path`. The `[Song preparation]` section configures the meeting document
  URL, local song ZIP/PPTX folders, and optional Yandex Disk search folders.
- The shared repository-root `config.ini` supplies the `[YandexDisk] token`
  used by the bot and OBS local mode. `obs_local/config.ini` is supported as a
  fallback when the shared file does not contain a token.
- Relative output paths are resolved from the repository root.

## Test song preparation for OBS

The separate test script reads the song tables for the selected Sunday from the
configured DOCX, searches for matching files, and prepares:

```text
<root_path>/Трансляция/Песни/01 - Song title/
```

Local ZIP files are checked first, then local PPTX files. Optional configured
Yandex Disk folders are searched afterward, ZIP before PPTX. ZIP archives must
contain PNG slides; PPTX files are rendered with the shared converter. A folder
is still created when no matching material is found.

Configure `[Song preparation]` in `obs_local/settings.ini` and place local files
in `local_zip_dir` or `local_pptx_dir`. To test with a local DOCX instead of
downloading the configured program:

```bash
python -m obs_local.prepare_songs \
  --program-file ./path/to/meeting-program.docx \
  --date 2026-10-04
```

To use the configured public Yandex document URL for the nearest Sunday:

```bash
python -m obs_local.prepare_songs
```

Optional remote search folders are configured as pipe-separated Yandex Disk
paths in `yandex_zip_paths` and `yandex_pptx_paths`. Remote search requires a
`[YandexDisk] token` in the repository-root `config.ini`, shared with the bot.
An `obs_local/config.ini` token is used only as a fallback.
When publishing a new result, the previous `Трансляция/Песни` directory is
retained as a timestamped backup; backups are not automatically deleted.

## Logs and troubleshooting

Song preparation writes rotating `bot.log` (INFO and above) and `debug.log`
(DEBUG and above) files to the directory configured by `[Logging] log_dir`.
The default on Raspberry Pi is:

```text
/dev/shm/pptx2png_tasks/obs_local/logs/
```

After a failed run, inspect the diagnostic log:

```bash
tail -n 100 /dev/shm/pptx2png_tasks/obs_local/logs/debug.log
```

Use `--debug` to also print DEBUG messages in the console, or `--log-dir PATH`
to select another log directory. The public document link and temporary
download URL are redacted from diagnostic messages.
The configured `program_url` must be a public file link from Yandex Disk in
`https://disk.yandex.ru/d/<public-key>` or `https://disk.yandex.ru/i/<public-key>`
format. A
`https://docs.yandex.ru/view/d/<id>` link opens the document viewer but its ID
is not the Yandex Disk API `public_key`; the two IDs cannot be converted into
one another. Use **Share** on the file in Yandex Disk and copy its public link.
`curl -I -L` returning HTTP 200 for the viewer page confirms the page is
reachable, not that the file is available through the Disk public-resource API.
An `/i/` link is accepted as-is; the downloaded content is checked to ensure
that it is actually a DOCX program rather than an image or CAPTCHA HTML.

The isolated tests use synthetic DOCX/ZIP fixtures and do not access Yandex Disk:

```bash
python -m unittest obs_local.tests.test_prepare_songs -v
```
