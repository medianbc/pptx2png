# Local OBS workflow

> Russian version: [README.RU.md](README.RU.md). Keep both files aligned whenever
> the local OBS workflow documentation changes.

For everyday operation and the menu map, see the
[Russian user guide](USER_GUIDE.RU.md).

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
- Python 3.10 or later. The shared package declares
  `requires-python >= 3.10`; the installer uses a compatible Python already
  available and installs one through Homebrew only when needed.
- LibreOffice for PPT/PPTX-to-PDF/PNG conversion. If it is not found in a
  standard application location or on `PATH`, the installer installs it using
  the Homebrew cask. The selected path is saved in the generated launchers.
  Set `PPTX2PNG_LIBREOFFICE_PATH` when running the installer to use an
  executable in a non-standard location; it can also be overridden when
  launching a workflow.

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
   directory. Before installing anything, the installer validates the
   destination and checks for Python 3.10+ with `venv`/`pip` and LibreOffice.
   It skips components that are already available and uses Homebrew only for
   missing ones. The selected Python executable is used by its full path, even
   if another `python3` command shadows it in `PATH`. The installer then
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
   `PATH`, or set `PPTX2PNG_LIBREOFFICE_PATH` to the executable's full path
   when running the installer. The selected path is saved in both launchers
   and used by presentation and song conversion.

4. Configure `obs_local/settings.macos.ini` (created on first launch) and
   `config.ini` as described below.
   Interactive presentation preparation requires a Yandex Disk OAuth token
   and `[YandexDisk] base_path`. The installer can prompt for the token and
   saves the secret file with owner-only permissions.

5. Run the desired workflow:

   ```bash
   ./start_obs_local.sh
   ```

The menu offers presentation preparation (`1`), song preparation (`2`),
settings (`3`), and exit (`0`). Settings shows numbered Russian descriptions and
current values instead of configuration keys. Selecting a setting displays a
short and detailed explanation, the current value in brackets, and a prompt
for the replacement value on the next line. Unknown settings are labeled
"Additional setting" without exposing their technical key. Edits are saved
while preserving comments.
A separate numbered action accepts a hidden Yandex token and saves it in
private `config.ini` files. Another numbered action in Settings checks updates;
its number follows the token action. Empty input keeps the current parameter value.
If settings are missing, unreadable, or malformed, the menu reports the error
and still offers token entry (`1`), update checking (`2`), and back (`0`).
Update checking contacts GitHub only
after you select it. If a newer revision exists, the menu asks before changing
files. Git checkouts are updated with a fast-forward only; uncommitted changes
are stashed and restored, and local commits that cannot be fast-forwarded are
not overwritten. Archive installations download the matching branch while
preserving `.venv`, `config.ini`, all local OBS settings files, song assets, and
generated launchers. The updater tracks installed source files and removes
obsolete tracked files; installations predating the manifest begin tracking
files with their first update. Archive updates are staged and roll back source
files if installation fails. Git checkouts preserve both working-tree and
staged changes. After source updates, the current Python environment is
updated from `core` and `obs_local/requirements.txt`, then checked with
`pip check`. A successful update asks you to restart the workflow. If
dependency installation fails, a Git update returns to the previous revision
and restores local changes; an archive update restores the previous source
files. If local changes conflict with the new version, the update is cancelled
and the previous revision is restored. If local changes cannot be restored
automatically, their backup remains in `git stash`; follow the error message.
System dependencies such as LibreOffice are not installed automatically by
the updater; install those using the platform installer if a new version
requires them.

To run song preparation directly with debug logging (for example, when
diagnosing an issue), use:

```bash
./prepare_obs_songs.sh --debug
```

To open the interactive menu and show DEBUG output while preparing songs, use:

```bash
./start_obs_local.sh --debug
```

In normal mode, unexpected failures are shown as a concise error with the
`debug.log` location; full tracebacks remain in that log. With `--debug`,
DEBUG messages and tracebacks are also printed in the console.

From the repository root, run the menu with `python -m obs_local`. Direct
workflow entry points remain available through `python -m
obs_local.run_obs_local` and `python -m obs_local.prepare_songs`.

## Configuration

- Non-secret options live in independent `obs_local/settings.linux.ini`,
  `obs_local/settings.macos.ini`, and `obs_local/settings.windows.ini` files.
  The OS is selected automatically; there is no runtime merging between files.
  On first launch, the current OS file is created from the legacy
  `obs_local/settings.ini` with existing values and comments preserved.
  The standard Linux log path is replaced with the OS default during this
  one-time migration; custom paths are preserved. Later edits affect only the
  selected file. These local files are ignored by Git and preserved by updates.
  `settings.ini` remains only an initial template/legacy migration source.
  `[YandexDisk]` defines
  `base_path` and `source_folder` used to fetch the upcoming Sunday's
  presentation.
- `[OBS Local paths]` defines `root_path`, `category_output_path`, and quality
  (`standard`, `2k`, or `4k`). Relative `root_path` values resolve from the
  repository root; relative `category_output_path` values resolve from
  `root_path`. This option affects presentation categories only; songs remain
  under `root_path/Трансляция/Песни`.
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
select one (`0` or Enter cancels the selection). It reads speaker notes and lets the
operator prepare all slides, select a detected category, or enter one or more
custom slide ranges (for example `1-4, 7, 9-11`). The chosen slides and output
category are shown for confirmation before rendering; the operator can return
to selection or cancel. Category slides are written to stable Russian-named
folders under `category_output_path` (default `root_path/Трансляция`):
`Начало собрания`, `Молитва`, and `Проповедь`. Absolute
`category_output_path` values are used as-is; relative values resolve from
`root_path`. All-slide and custom-range results remain in
`root_path/YYYY-MM-DD/presentation/run-id/category`. The interactive workflow
prompts for missing paths, quality and Yandex Disk token on first run, saves
ordinary settings to the current OS file and the token to a private `config.ini`.
The token is not stored in `settings.ini`.

Manual category ranges survive returning to selection and switching categories
within the current presentation preparation, but are not persisted after exit.
Mode `2` converts one category. Mode `4` prepares multiple categories: `1`–`3`
edit a category and select it, `5` toggles selection by category number, and
`4` confirms the selected categories. Detected categories are selected initially;
missing categories need a manual range. Changing the confirmation retains edits
and selection. The renderer runs once and publishes each selected category to
its stable Russian-named folder. Replaced category folders are retained under
the hidden `category_output_path/.backups/<category>/<timestamp>` directory.
Only the latest generated backup per category is retained; older generated
backups are removed on the next conversion of that category. Categories not
selected in a batch remain unchanged. Existing run-specific
outputs are not moved or deleted. The reported batch result path is
`category_output_path`. All selected folders are staged before publication;
if a replacement fails, already replaced categories are restored. Concurrent
category publications are serialized with a cross-process lock.

If the upcoming Sunday's service folder or presentation is missing on Yandex
Disk, the workflow reports it and offers numbered actions: retry (`1`),
open a local `.ppt`/`.pptx` file (`2`, then enter its path), or cancel (`0` or Enter).
Cancelling returns to the main menu, so song preparation can still be selected.

The song submenu offers `1` to read the program, `2` to update the local song
library, and `0` to return. Library updating scans the configured Yandex Disk
directories and caches missing materials as PNG ZIPs without reading a program
or changing the meeting output. Existing valid cached ZIPs are kept.
Menu choices use numbers, not words or letters; bracketed defaults apply on Enter.
Reading the program extracts song titles from the DOCX tables for the
selected Sunday, searches local PNG ZIPs and PPTX files, and
then searches Yandex Disk ZIP/PPTX files when remote paths and a token are
configured. PPTX files are rendered to PNG and placed in separate numbered
folders under `root_path/Трансляция/Песни`; missing songs are marked. Results
are published from a staging directory, and the previous output is retained as
a backup.

Yandex API search uses `yandex_zip_png_paths` and `yandex_pptx_paths` together
with an OAuth token. Configured shared-folder URLs do not replace these paths
and do not enable search by themselves. `priority_cloud_disk` selects the
program source (`google` or `yandex`), using `program_google_url` or
`program_yandex_url`. There is no automatic switch to the other cloud on failure.
New installations default to Google. Existing settings without a priority retain
their `program_url` source; that legacy URL remains a fallback only for the
selected cloud when its dedicated URL is empty.
Saved program URLs and cloud priority are validated only when that configured
program is requested. Invalid program settings do not block a local DOCX,
an explicit `--program-url`, independent library updates, or the settings menu.
If the selected source has no URL, the interactive workflow asks for a public
link from that cloud and saves it to the current OS settings file. When a song from the program
cannot be found, the workflow reports the status of every song and lets the
operator enter an alternative title to retry the search. Successful
alternatives are stored locally in the ignored
`obs_local/song_title_aliases.json` file and reused on later runs. Enter an
empty response to skip an alternative. If a saved alias no longer finds a
match, the original program title is tried as a fallback. A prompted program
link is saved only after the selected service returns a valid DOCX. Non-interactive runs
can still use `--program-file` or `--program-url`.
If Yandex Disk responds with HTTP 429, the interactive workflow explains that
the public API is rate-limiting requests and lets the operator retry or provide
a local DOCX program instead. Network errors, timeouts, and an invalid downloaded
file also offer numbered actions: retry (`1`), use a local DOCX (`2`), or cancel
(`3` or Enter), returning to the songs submenu without reporting cancellation
as an error. Empty input at the local path prompt returns to the recovery menu.
The same menu handles failures of newly entered program links.
No automatic retry loop is started.
Interactive DOCX and PPT/PPTX path prompts accept paths with spaces, optional
surrounding quotes, and, on macOS/Linux, shell-escaped spaces (`\ `).
Existing literal file names and Windows path separators are preserved.
The shared link normally opens a viewer page rather than delivering DOCX bytes.
Even HTTP 200 may be CAPTCHA HTML; it does not guarantee a working API-free
download. The workflow uses the public API for shared links and validates the
downloaded DOCX; it does not bypass CAPTCHA.

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
Titles and filenames are normalized to Unicode NFC before matching, so
composed and decomposed letters (such as `й`) match identically.

Configure `[Song preparation]` in the current OS settings file and place local files
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

Log directories are initialized for the selected OS, as described below.
A custom directory can be passed with `--log-dir PATH`.

## Logs and troubleshooting

Song preparation writes rotating `obs_local.log` (INFO and above) and `debug.log`
(DEBUG and above) files to the directory configured by `[Logging] log_dir`.
Default directories:

- Linux: `/dev/shm/pptx2png_tasks/obs_local/logs/`.
- macOS: `~/Library/Logs/pptx2png/obs_local/`.
- Windows: `%LOCALAPPDATA%/pptx2png/obs_local/logs/` (falls back to
  `~/AppData/Local/pptx2png/obs_local/logs/`).

The console normally shows warnings/errors only; INFO and DEBUG remain in the
files. Existing `bot.log` files are left untouched; new runs use `obs_local.log`.
The Linux directory is:

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
The configured `program_url` accepts a public Google Docs link
`https://docs.google.com/document/d/<id>/edit` (including `?usp=sharing`).
Set sharing to **Anyone with the link** and allow viewers to download the
document. The workflow exports it as DOCX without Google credentials and applies
the same 50 MB limit and DOCX validation as Yandex downloads. Login/CAPTCHA pages
are rejected; download failures use the existing retry/local DOCX/cancel menu.
In the settings menu choose `priority_cloud_disk` → `1` (Google) or `2`
(Yandex), then enter the matching `program_google_url` or `program_yandex_url`.
The menu exposes these options even for older OS settings files. For example:

```ini
[Song preparation]
priority_cloud_disk = google
program_google_url = https://docs.google.com/document/d/<id>/edit?usp=sharing
program_yandex_url = https://disk.yandex.ru/d/<public-key>
```

`--program-url` overrides the selected source for one run; local
`--program-file` still takes priority. This does not change Yandex song search.

For Yandex Disk, use a public file link in
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
