#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_URL="https://github.com/medianbc/pptx2png.git"
SOURCE_BRANCH="${PPTX2PNG_BRANCH:-main}"
ARCHIVE_URL="${PPTX2PNG_ARCHIVE_URL:-https://codeload.github.com/medianbc/pptx2png/tar.gz/refs/heads/${SOURCE_BRANCH}}"
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

printf 'Installing macOS dependencies with Homebrew...\n'
brew install python
brew install --cask libreoffice

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
else
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
PYTHON="$(brew --prefix)/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

printf 'Creating virtual environment and installing Python dependencies...\n'
"$PYTHON" -m venv .venv
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -e ./core
".venv/bin/python" -m pip install -r obs_local/requirements.txt

if [[ "$ARCHIVE_DOWNLOADED" == true ]]; then
    if revision="$(curl --fail --location --silent --show-error \
        "https://api.github.com/repos/medianbc/pptx2png/commits/${SOURCE_BRANCH}" |
        "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["sha"])')"; then
        printf '{"branch":"%s","revision":"%s"}\n' \
            "$SOURCE_BRANCH" "$revision" > "$INSTALL_DIR/.pptx2png-version.json"
    else
        printf 'Warning: could not record the installed source revision.\n' >&2
    fi
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
