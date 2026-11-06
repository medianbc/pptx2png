# Local OBS workflow

> Russian version: [README.RU.md](README.RU.md). Keep both files aligned whenever
> the local OBS workflow documentation changes.

This package downloads a presentation from Yandex Disk, lets the operator
select a category and slide range, and writes PNG files to the configured
local OBS directory. Conversion and Yandex Disk operations are shared with the
bot through `core`.

## Install and run

On a new machine, clone the stable `main` branch and run its installer:

```bash
git clone --branch main --single-branch \
  https://github.com/medianbc/pptx2png.git "$HOME/pptx2png"
cd "$HOME/pptx2png"
./install_obs_local.sh
```

For a different installation directory, pass it to the installer:

```bash
git clone --branch main --single-branch \
  https://github.com/medianbc/pptx2png.git "$HOME/my-pptx2png"
cd "$HOME/my-pptx2png"
./install_obs_local.sh "$HOME/my-pptx2png"
```

The Linux installer uses `apt-get` to install Git, Python/venv and LibreOffice
Impress. It creates `.venv`, installs dependencies, creates a private
`config.ini` for the Yandex Disk token, and generates launchers for presentation
and song preparation. Existing configuration is preserved. The installer does
not update source files in an existing checkout.

### macOS installation

#### Requirements

- macOS with Homebrew installed and available in Terminal.
- Internet access to GitHub and Homebrew/PyPI package repositories.
- Python 3.10 or later. The installer installs Python through Homebrew; the
  shared package declares `requires-python >= 3.10`.
- LibreOffice for PPT/PPTX-to-PDF/PNG conversion. The installer installs the
  application using the Homebrew cask.

The repository does not specify a minimum macOS version. The
`OBS macOS installation` GitHub Actions workflow installs the package on a
clean macOS runner and smoke-tests conversion. The installer has no
Intel/Apple Silicon-specific branches; verify the installation and conversion
on the target machine as well.

#### Installation steps

1. Install Homebrew using [brew.sh](https://brew.sh) and verify that it is
   available in Terminal:

   ```bash
   brew --version
   ```

2. Download and run the installer. Git is not required:

   ```bash
   curl -fL \
     https://raw.githubusercontent.com/medianbc/pptx2png/main/install_obs_local_macos.sh \
     -o "$HOME/install_obs_local_macos.sh"
   less "$HOME/install_obs_local_macos.sh"
   bash "$HOME/install_obs_local_macos.sh" "$HOME/pptx2png"
   cd "$HOME/pptx2png"
   ```

   The command downloads the `main` source archive and extracts it into
   `$HOME/pptx2png`. Replace that path to choose a different installation
   directory. The installer uses Homebrew to install Python and LibreOffice,
   creates `.venv`, installs `core` and local application dependencies, and
   creates `start_obs_local.sh` and `prepare_obs_songs.sh`. Git is not required
   for a fresh installation. If you run the installer
   from an existing Git checkout, Git is only needed to validate its origin.
   Rerunning the installer in an archive installation directory reuses the
   extracted source and does not automatically update it.

3. Check Python and LibreOffice:

   ```bash
   ./.venv/bin/python --version
   test -x /Applications/LibreOffice.app/Contents/MacOS/soffice
   ```

   If LibreOffice is installed elsewhere, ensure `soffice` is available on
   `PATH`.

4. Configure `obs_local/settings.ini` and `config.ini` as described below.
   Interactive presentation preparation requires a Yandex Disk OAuth token
   and `[YandexDisk] base_path`. The installer can prompt for the token and
   saves the secret file with owner-only permissions.

5. Run the desired workflow:

   ```bash
   ./start_obs_local.sh
   ```

The menu offers presentation preparation (`1`), song preparation (`2`), and
exit (`0`). To run song preparation directly with debug logging (for example,
when diagnosing an issue), use:

```bash
./prepare_obs_songs.sh --debug
```

From the repository root, run the menu with `python -m obs_local`. Direct
workflow entry points remain available through `python -m
obs_local.run_obs_local` and `python -m obs_local.prepare_songs`.

## Configuration

- `obs_local/settings.ini` contains non-secret options. `[YandexDisk]` defines
  `base_path` and `source_folder` used to fetch the upcoming Sunday's
  presentation.
- `[OBS Local paths]` defines `root_path` and quality (`standard`, `2k`, or
  `4k`). Relative `root_path` values resolve from the repository root.
- `[SlideCategories]` contains keywords used to detect slide categories in
  speaker notes.
- `[Song preparation]` defines `program_url`, `local_zip_dir`,
  `local_pptx_dir`, optional Yandex Disk search paths, and
  `cache_missing_yandex_song_zips`.
- The Yandex Disk token belongs in the root `config.ini` under `[YandexDisk]`;
  song preparation also supports `obs_local/config.ini` as a fallback. Do not
  put tokens in `settings.ini` or commit secret files.

Example secret file (replace the placeholder locally):

```ini
[YandexDisk]
token = YOUR_OAUTH_TOKEN
```

### Current implementation

`python -m obs_local` (or `start_obs_local.sh`) opens a menu to choose
presentation or song preparation. The presentation workflow lists
presentations for the upcoming Sunday from Yandex Disk and asks the operator to
select one (`0`, `q`, or `cancel` cancels the selection). It reads speaker notes and lets the
operator prepare all slides, select a detected category, or enter one or more
custom slide ranges (for example `1-4, 7, 9-11`). The chosen slides and output
category are shown for confirmation before rendering; the operator can return
to selection or cancel. Output is written to
`root_path/YYYY-MM-DD/presentation/run-id/category`. The interactive workflow
prompts for missing paths, quality and Yandex Disk token on first run, saves
ordinary settings to `settings.ini` and the token to a private `config.ini`.
The token is not stored in `settings.ini`.

If the upcoming Sunday's service folder or presentation is missing on Yandex
Disk, the workflow reports it and offers to retry the search (press Enter), use
a local `.ppt`/`.pptx` file (enter its path), or cancel (enter `esc` or `exit`).
Cancelling returns to the main menu, so song preparation can still be selected.

The song-preparation menu option reads song titles from the DOCX tables for the
selected Sunday, searches local PNG ZIPs and PPTX files, and
then searches Yandex Disk ZIP/PPTX files when remote paths and a token are
configured. PPTX files are rendered to PNG and placed in separate numbered
folders under `root_path/Трансляция/Песни`; missing songs are marked. Results
are published from a staging directory, and the previous output is retained as
a backup.

Yandex API search uses `yandex_zip_png_paths` and `yandex_pptx_paths` together
with an OAuth token. Configured shared-folder URLs do not replace these paths
and do not enable search by themselves. `program_url` selects the DOCX program;
if it is empty, the script errors and asks for a setting or `--program-file`.

## Test song preparation for OBS

The separate test script reads the song tables for the selected Sunday from the
configured DOCX, searches for matching files, and prepares:

```text
<root_path>/Трансляция/Песни/01 - Song title/
```

Local ZIP files are checked first, then local PPTX files. Optional configured
Yandex Disk folders are searched afterward, ZIP before PPTX. ZIP archives must
contain PNG slides; PPTX files are rendered with the shared converter. A folder
is still created and marked `(не найдено)` when no matching material is found.
Matching prefers a complete song title in the filename; if none is found, a
candidate with at least 75% of the title's words in common may be used.
Ambiguous candidates are not selected automatically.

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
paths in `yandex_zip_png_paths` and `yandex_pptx_paths`. Remote search requires a
`[YandexDisk] token` in the repository-root `config.ini`, shared with the bot.
An `obs_local/config.ini` token is used only as a fallback.
When publishing a new result, the previous `Трансляция/Песни` directory is
retained as a timestamped backup; backups are not automatically deleted.

When a matching local PPTX or remote ZIP/PPTX is used, its PNG slides are also
saved as a ZIP in `local_zip_dir` (remote files retain their relative folder
structure). To scan all configured remote ZIP and PPTX folders after processing
the current program and cache files that do not yet have a valid local PNG ZIP,
set `cache_missing_yandex_song_zips = true` in `[Song preparation]`.
`yandex_zip_png_url` is currently retained as configuration metadata; it does
not drive API search. Search uses the configured folder paths and OAuth token.

On macOS, song-preparation logs are written to
`~/Library/Logs/pptx2png/obs_local/` when the settings file contains the
repository's Linux `/dev/shm` default. On Linux, the default remains
`/dev/shm/pptx2png_tasks/obs_local/logs/`. A custom directory can be passed
with `--log-dir PATH`.

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
