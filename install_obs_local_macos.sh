#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_URL="https://github.com/medianbc/pptx2png.git"
SOURCE_BRANCH="${PPTX2PNG_BRANCH:-main}"
CUSTOM_ARCHIVE_URL="${PPTX2PNG_ARCHIVE_URL:-}"
SOURCE_REVISION="${PPTX2PNG_SOURCE_REVISION:-}"
ARCHIVE_URL=""
ARCHIVE_DOWNLOADED=false
DEFAULT_INSTALL_DIR="${HOME}/pptx2png"
INSTALL_DIR="${1:-$DEFAULT_INSTALL_DIR}"
INSTALL_DIR="$(mkdir -p "$(dirname "$INSTALL_DIR")" && cd "$(dirname "$INSTALL_DIR")" && pwd)/$(basename "$INSTALL_DIR")"

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

[[ "$(uname -s)" == "Darwin" ]] ||
    die "This installer is for macOS only."

if ! command -v brew >/dev/null 2>&1; then
    die "Homebrew is required. Install it from https://brew.sh, then run this installer again."
fi

command -v curl >/dev/null 2>&1 || die "curl is required to download the project archive."
command -v tar >/dev/null 2>&1 || die "tar is required to extract the project archive."

EXISTING_INSTALLATION=false
if [[ -e "$INSTALL_DIR" ]]; then
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        command -v git >/dev/null 2>&1 ||
            die "Git is needed to validate the existing checkout at $INSTALL_DIR."
        origin="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
        normalized_origin="${origin%.git}"
        normalized_repository="${REPOSITORY_URL%.git}"
        [[ "${normalized_origin%/}" == "${normalized_repository%/}" ]] ||
            die "A different Git repository already exists at $INSTALL_DIR."
        printf 'Using existing project checkout: %s\n' "$INSTALL_DIR"
    elif [[ -f "$INSTALL_DIR/.pptx2png-archive-install" ]]; then
        printf 'Using existing archive installation: %s\n' "$INSTALL_DIR"
    else
        die "The destination exists and is not a recognized project installation: $INSTALL_DIR"
    fi
    EXISTING_INSTALLATION=true
fi

BREW_PREFIX="$(brew --prefix)"

find_compatible_python() {
    local formula_prefix candidate
    formula_prefix="$(brew --prefix python 2>/dev/null || true)"
    for candidate in \
        "$formula_prefix/bin/python3" \
        "$formula_prefix/libexec/bin/python3" \
        "$BREW_PREFIX/bin/python3" \
        "$(command -v python3 2>/dev/null || true)"; do
        if [[ -x "$candidate" ]] &&
            "$candidate" -c \
                'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
                >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

find_soffice() {
    local candidate
    for candidate in \
        "/Applications/LibreOffice.app/Contents/MacOS/soffice" \
        "$(command -v soffice 2>/dev/null || true)"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON="$(find_compatible_python || true)"
SOFFICE="$(find_soffice || true)"

if [[ -n "$PYTHON" ]]; then
    printf 'Using compatible Python: %s\n' "$PYTHON"
else
    printf 'Python 3.10+ with venv and pip was not found; installing it with Homebrew...\n'
    brew install python
    PYTHON="$(find_compatible_python || true)"
    [[ -n "$PYTHON" ]] ||
        die "Homebrew Python 3.10+ with venv and pip is not available. Check the Homebrew installation and PATH."
fi

if [[ -n "$SOFFICE" ]]; then
    printf 'Using existing LibreOffice: %s\n' "$SOFFICE"
else
    printf 'LibreOffice was not found; installing it with Homebrew...\n'
    brew install --cask libreoffice
    SOFFICE="$(find_soffice || true)"
    [[ -n "$SOFFICE" ]] ||
        die "LibreOffice was installed but its soffice executable was not found. Make it available in /Applications or PATH."
fi

if [[ "$EXISTING_INSTALLATION" != true ]]; then
    if [[ -z "$CUSTOM_ARCHIVE_URL" ]]; then
        if [[ -z "$SOURCE_REVISION" ]]; then
            SOURCE_REVISION="$(curl --fail --location --silent --show-error \
                "https://api.github.com/repos/medianbc/pptx2png/commits/${SOURCE_BRANCH}" |
                "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["sha"])')" ||
                die "Could not resolve the source branch to a commit SHA."
        fi
        [[ "$SOURCE_REVISION" =~ ^[0-9a-fA-F]{40,64}$ ]] ||
            die "Source revision must be a full Git commit SHA."
        ARCHIVE_URL="https://codeload.github.com/medianbc/pptx2png/tar.gz/${SOURCE_REVISION}"
    else
        [[ "$SOURCE_REVISION" =~ ^[0-9a-fA-F]{40,64}$ ]] ||
            die "PPTX2PNG_SOURCE_REVISION is required with a custom archive URL."
        ARCHIVE_URL="https://codeload.github.com/medianbc/pptx2png/tar.gz/${SOURCE_REVISION}"
        [[ "$CUSTOM_ARCHIVE_URL" == "$ARCHIVE_URL" ]] ||
            die "Custom archive URL must point to the official archive for PPTX2PNG_SOURCE_REVISION."
    fi
    printf 'Downloading the project archive...\n'
    staging_dir="$(mktemp -d "$(dirname "$INSTALL_DIR")/.pptx2png-install.XXXXXX")"
    cleanup_staging() {
        if [[ -n "${staging_dir:-}" && -d "$staging_dir" ]]; then
            rm -rf "$staging_dir"
        fi
    }
    trap cleanup_staging EXIT
    curl --fail --location --silent --show-error "$ARCHIVE_URL" \
        --output "$staging_dir/source.tar.gz"
    mkdir "$staging_dir/source"
    tar -xzf "$staging_dir/source.tar.gz" \
        --strip-components=1 \
        -C "$staging_dir/source"
    touch "$staging_dir/source/.pptx2png-archive-install"
    mv "$staging_dir/source" "$INSTALL_DIR"
    ARCHIVE_DOWNLOADED=true
fi

cd "$INSTALL_DIR"

printf 'Creating virtual environment and installing Python dependencies...\n'
"$PYTHON" -m venv .venv
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -e ./core
".venv/bin/python" -m pip install -r obs_local/requirements.txt

if [[ "$ARCHIVE_DOWNLOADED" == true ]]; then
    if [[ -n "$SOURCE_REVISION" ]]; then
        printf '{"branch":"%s","revision":"%s"}\n' \
            "$SOURCE_BRANCH" "$SOURCE_REVISION" > "$INSTALL_DIR/.pptx2png-version.json"
    fi

    "$PYTHON" - "$INSTALL_DIR" <<'PY'
import json
import os
import sys
from pathlib import Path

project = Path(sys.argv[1])
preserved = {
    ".git", ".venv", "config.ini", "obs_local/config.ini",
    "obs_local/settings.ini", "obs_local/song_title_aliases.json",
    "obs_png", "prepare_obs_songs.sh", "song_assets", "start_obs_local.sh",
    ".pptx2png-version.json", ".pptx2png-files.json",
    ".pptx2png-archive-install",
}
files = []
for current, directories, filenames in os.walk(project):
    current_path = Path(current)
    relative_dir = current_path.relative_to(project)
    directories[:] = [
        name for name in directories
        if (relative_dir / name).as_posix() not in preserved
        and name not in {".git", ".venv", "song_assets", "obs_png"}
    ]
    for filename in filenames:
        relative = (relative_dir / filename).as_posix()
        if relative not in preserved:
            files.append(relative)
(project / ".pptx2png-files.json").write_text(
    json.dumps(sorted(files), indent=2) + "\n", encoding="utf-8"
)
PY
fi

cat > start_obs_local.sh <<'LAUNCHER'
#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PROJECT_DIR/.venv/bin/python" -m obs_local "$@"
LAUNCHER
chmod +x start_obs_local.sh

cat > prepare_obs_songs.sh <<'LAUNCHER'
#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PROJECT_DIR/.venv/bin/python" -m obs_local.prepare_songs "$@"
LAUNCHER
chmod +x prepare_obs_songs.sh

CONFIG_FILE="$INSTALL_DIR/config.ini"
if [[ ! -f "$CONFIG_FILE" ]]; then
    if [[ "${CI:-}" == "true" || ! -t 0 ]]; then
        printf '[YandexDisk]\ntoken =\n' > "$CONFIG_FILE"
        chmod 600 "$CONFIG_FILE"
        printf 'Created config.ini template; configure a token before remote Disk operations.\n'
    else
        printf '\nA Yandex Disk OAuth token is needed for remote song search.\n'
        read -r -p "Configure the token now? [y/N] " configure_token
        if [[ "$configure_token" =~ ^[Yy]$ ]]; then
            read -r -s -p "Yandex Disk OAuth token (input hidden): " yandex_token
            printf '\n'
            if [[ -n "$yandex_token" ]]; then
                {
                    printf '[YandexDisk]\n'
                    printf 'token = %s\n' "$yandex_token"
                } > "$CONFIG_FILE"
                chmod 600 "$CONFIG_FILE"
                unset yandex_token
                printf 'Token saved to config.ini with owner-only permissions.\n'
            else
                printf '[YandexDisk]\ntoken =\n' > "$CONFIG_FILE"
                chmod 600 "$CONFIG_FILE"
                printf 'Empty config.ini template created; add the token later.\n'
            fi
        else
            printf '[YandexDisk]\ntoken =\n' > "$CONFIG_FILE"
            chmod 600 "$CONFIG_FILE"
            printf 'Created config.ini; add the token before remote Disk searches.\n'
        fi
    fi
else
    printf 'Existing config.ini preserved.\n'
fi

mkdir -p song_assets/zip song_assets/pptx

printf '\nInstallation complete.\n'
printf 'Project:             %s\n' "$INSTALL_DIR"
printf 'Local OBS workflow:  cd "%s" && ./start_obs_local.sh\n' "$INSTALL_DIR"
printf 'Prepare song slides: cd "%s" && ./prepare_obs_songs.sh --debug\n' "$INSTALL_DIR"
printf 'OBS settings:        %s/obs_local/settings.ini\n' "$INSTALL_DIR"
printf 'Yandex Disk token:   %s/config.ini\n' "$INSTALL_DIR"
