#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_URL="https://github.com/medianbc/pptx2png.git"
DEFAULT_INSTALL_DIR="${HOME}/pptx2png"
INSTALL_DIR="${1:-$DEFAULT_INSTALL_DIR}"
INSTALL_DIR="$(mkdir -p "$(dirname "$INSTALL_DIR")" && cd "$(dirname "$INSTALL_DIR")" && pwd)/$(basename "$INSTALL_DIR")"

die() {
    printf 'Ошибка: %s\n' "$*" >&2
    exit 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
    die "Этот установщик предназначен для Linux (Debian/Raspberry Pi OS/Ubuntu)."
fi
if [[ ! -r /etc/os-release ]]; then
    die "Не удалось определить дистрибутив Linux."
fi
# shellcheck disable=SC1091
source /etc/os-release
if ! command -v apt-get >/dev/null 2>&1; then
    die "Для автоматической установки системных компонентов требуется apt-get."
fi

if [[ "$(id -u)" -eq 0 ]]; then
    APT=(apt-get)
else
    command -v sudo >/dev/null 2>&1 || die "Установите пакеты от root или настройте sudo."
    APT=(sudo apt-get)
fi

printf 'Установка системных зависимостей...\n'
"${APT[@]}" update
"${APT[@]}" install -y git python3 python3-venv python3-pip libreoffice-impress

if [[ -e "$INSTALL_DIR" ]]; then
    if [[ ! -d "$INSTALL_DIR/.git" ]]; then
        die "Каталог уже существует и не является Git-копией проекта: $INSTALL_DIR"
    fi
    origin="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
    [[ "$origin" == "$REPOSITORY_URL" ]] ||
        die "В $INSTALL_DIR находится другой Git-репозиторий."
    printf 'Используется существующая копия проекта: %s\n' "$INSTALL_DIR"
else
    printf 'Загрузка проекта из GitHub...\n'
    git clone --branch main --single-branch "$REPOSITORY_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
printf 'Создание виртуального окружения и установка Python-зависимостей...\n'
python3 -m venv .venv
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
    printf '\nДля поиска материалов на Яндекс.Диске нужен OAuth-токен.\n'
    read -r -p "Настроить токен сейчас? [y/N] " configure_token
    if [[ "$configure_token" =~ ^[YyДд]$ ]]; then
        read -r -s -p "OAuth-токен Яндекс.Диска (ввод скрыт): " yandex_token
        printf '\n'
        if [[ -n "$yandex_token" ]]; then
            {
                printf '[YandexDisk]\n'
                printf 'token = %s\n' "$yandex_token"
            } > "$CONFIG_FILE"
            chmod 600 "$CONFIG_FILE"
            unset yandex_token
            printf 'Токен сохранён в config.ini с правами доступа только владельцу.\n'
        else
            printf '[YandexDisk]\ntoken =\n' > "$CONFIG_FILE"
            chmod 600 "$CONFIG_FILE"
            printf 'Пустой шаблон config.ini создан; токен можно добавить позже.\n'
        fi
    else
        printf '[YandexDisk]\ntoken =\n' > "$CONFIG_FILE"
        chmod 600 "$CONFIG_FILE"
        printf 'Создан config.ini; добавьте токен перед поиском по Яндекс.Диску.\n'
    fi
else
    printf 'Существующий config.ini сохранён без изменений.\n'
fi

mkdir -p song_assets/zip song_assets/pptx

printf '\nУстановка завершена.\n'
printf 'Проект: %s\n' "$INSTALL_DIR"
printf 'Локальный режим OBS: cd "%s" && ./start_obs_local.sh\n' "$INSTALL_DIR"
printf 'Подготовка песен:   cd "%s" && ./prepare_obs_songs.sh --debug\n' "$INSTALL_DIR"
printf 'Настройки OBS:       %s/obs_local/settings.ini\n' "$INSTALL_DIR"
printf 'Токен Яндекс.Диска:  %s/config.ini\n' "$INSTALL_DIR"
