#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_URL="https://github.com/medianbc/pptx2png.git"
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

printf 'Installing macOS dependencies with Homebrew...\n'
brew install git python
brew install --cask libreoffice

if [[ -e "$INSTALL_DIR" ]]; then
    if [[ ! -d "$INSTALL_DIR/.git" ]]; then
        die "The destination exists and is not a Git checkout: $INSTALL_DIR"
    fi
    origin="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
    [[ "$origin" == "$REPOSITORY_URL" ]] ||
        die "A different Git repository already exists at $INSTALL_DIR."
    printf 'Using existing project checkout: %s\n' "$INSTALL_DIR"
else
    printf 'Cloning the project from GitHub...\n'
    git clone --branch main --single-branch "$REPOSITORY_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
PYTHON="$(brew --prefix)/bin/python3"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

printf 'Creating virtual environment and installing Python dependencies...\n'
"$PYTHON" -m venv .venv
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -e ./core
".venv/bin/python" -m pip install -r obs_local/requirements.txt

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
